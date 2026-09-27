"""The OXBOW API: one FastAPI app, one seam to the warehouse, no recomputation.

Assembly notes that matter to a reader of this file rather than to a framework:

* **CORS is off by default.** This is a local, same-origin API behind a proxy in
  production; opening origins here is a security decision, and 01 §A rule 8 puts
  security defaults in config with a loud comment. ``OXBOW_CORS_ORIGINS`` turns it on,
  and turning it on logs the exact origins.
* **the problem handlers are registered before the routers matter.** Every router passes
  ``responses=problem_responses(...)``, and the handlers produce the same
  ``ProblemDetail`` body, so the OpenAPI schema and the wire format cannot drift —
  which is what makes the generated client's error union typed instead of ``any``.
* **the lifespan builds the container and reports what it chose.** A process that
  silently came up on the null warehouse while an operator believed it was reading
  Postgres is the failure this log line exists to prevent; the same choice is on
  ``/healthz`` and in every response's ``meta``.
* **unhandled errors become problem documents with a trace id.** The traceback goes to
  the log, the trace id goes to the user, and the response body says where to look —
  plan §14's "every error surface prints the run_id with a copy button" applies to the
  500s most of all.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

# ``uvicorn main:app --app-dir apps/api`` puts ``apps/api`` on the path, which makes
# this module ``main`` but leaves the ``api`` package itself unimportable. Adding the
# parent directory keeps one import style (``api.*``) everywhere — in the app, the
# worker, the tests and alembic — instead of half the modules using sibling names and
# the other half using package names, which is how a rename quietly breaks one side.
_APPS_DIR = str(Path(__file__).resolve().parents[1])
if _APPS_DIR not in sys.path:
    sys.path.insert(0, _APPS_DIR)

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from api import jobs
from api.deps import Container, analyst_or_higher, build_container, get_container
from api.events import router as events_router
from api.observability import configure_logging, get_logger, new_trace_id, run_id_var, trace_id_var
from api.problems import problem_responses, register_problem_handlers
from api.routers import (
    alerts,
    auth,
    cases,
    dashboard,
    decisions,
    graph,
    meta,
    policy,
    runs,
    validation,
)
from api.schemas.common import Envelope, envelope
from api.schemas.health import ComponentStatus, HealthResponse
from api.security import Principal
from api.settings import get_settings

API_VERSION: Final = "0.1.0"
SERVICE_NAME: Final = "oxbow-api"
OPENAPI_SCHEMA_VERSION: Final = "1.0"
TRACE_HEADER: Final = "X-Trace-Id"
CORS_ORIGINS_ENV: Final = "OXBOW_CORS_ORIGINS"
DESCRIPTION: Final = (
    "OXBOW scores transaction-monitoring alerts for mobile-money networks and prices the "
    "review queue under a capacity budget. Research prototype: historical, de-identified data "
    "only; no live transactions, no trading, no financial advice. Monetary figures are model "
    "estimates under the assumptions in config/economics.yaml. Read ``meta`` on every response "
    "for run id, provenance and assumptions; status lives in the HTTP code, and every error "
    "body is RFC 9457 ``application/problem+json``."
)

logger = get_logger("oxbow.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the container once, say what it chose, and dispose on shutdown."""
    settings = get_settings()
    configure_logging(json_output=_json_logs_enabled())
    container = build_container(settings)
    app.state.container = container
    app.state.startup_report = {
        "warehouse_backend": container.backend,
        "write_path_enabled": container.write_path_enabled,
        "auth_mode": _auth_mode(settings),
        "degraded": container.degraded_components(),
        "case_sink": type(container.case_sink_factory()).__name__,
        "object_store": type(container.object_store_factory()).__name__,
    }
    logger.info(
        "oxbow api starting",
        version=API_VERSION,
        warehouse_backend=container.backend,
        write_path=container.write_path_enabled,
        auth_mode=app.state.startup_report["auth_mode"],
        degraded=container.degraded_components(),
        object_store=container.object_store_factory().__class__.__name__,
    )
    if container.backend == "null-file":
        logger.warning(
            "running on the null-file warehouse: reads work with nothing else up, decision "
            "writes answer 503, and every response says warehouse_backend=null-file"
        )
    if not settings.oidc_issuer:
        logger.warning(
            "no OIDC issuer configured, so the local HS256 demo token is the only identity "
            "this API accepts; it is labelled source=local-jwt everywhere it appears"
        )
    try:
        yield
    finally:
        container.close()
        logger.info("oxbow api stopped", warehouse_backend=container.backend)


def create_app() -> FastAPI:
    """The app factory. A function so tests can build one per environment."""
    app = FastAPI(
        title="OXBOW API",
        version=API_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
        # The generated client is the contract, so the schema is the deliverable and
        # the docs route is a convenience with no bearing on it.
        openapi_tags=_TAGS,
        contact={"name": "OXBOW", "url": "https://oxbow.dev"},
        license_info={"name": "MIT", "identifier": "MIT"},
    )
    app.add_middleware(RequestContextMiddleware)
    _install_cors_if_configured(app)
    register_problem_handlers(app)

    for router in (
        meta.router,
        runs.router,
        events_router,
        alerts.router,
        cases.router,
        decisions.router,
        graph.router,
        policy.router,
        validation.router,
        validation.scorecard_router,
        dashboard.router,
        jobs.router,
        auth.router,
        auth.me_router,
    ):
        app.include_router(router)

    @app.get(
        "/healthz",
        response_model=Envelope[HealthResponse],
        tags=["health"],
        # The one error this route can produce without the request being wrong is a failure to
        # build the container it reads its component list from, which reaches the generic
        # handler as a 500. Declaring nothing would leave the generated client's error branch
        # for the boot-probe unnamed — and this is the route `make demo` waits on, so a
        # failure here has to be readable without a token and without guessing.
        responses=problem_responses((500,)),
        summary="Liveness and the degraded-component list, without authentication",
        description=(
            "Unauthenticated on purpose: a banner that needs a token to explain why the "
            "API is unusable is not a banner. No operational data is exposed here — the "
            "component list names dependencies and what is rendered instead of them."
        ),
    )
    def healthz(request: Request) -> dict[str, Any]:
        container: Container = getattr(request.app.state, "container", None) or _detached_container(
            request
        )
        return envelope(_health_body(container), **_health_meta(container).model_dump())

    @app.get(
        "/api/health",
        response_model=Envelope[HealthResponse],
        tags=["health"],
        summary="The same components, re-probed, with per-component detail and timings",
    )
    def health_detail(
        request: Request,
        force: bool = True,
        container: Container = Depends(get_container),
        principal: Principal = Depends(analyst_or_higher),
    ) -> dict[str, Any]:
        if force:
            for name in container.degraded_components():
                container.component(name, force=True)
        return envelope(_health_body(container), **_health_meta(container).model_dump())

    @app.get("/openapi-typed-probe", include_in_schema=False)
    def typed_error_probe() -> JSONResponse:
        """A route that always fails, so the error union is proven on the wire.

        Plan §13's gate is "a forced failure in each router returns valid
        ``problem+json`` with a run id". Every router can be forced through its own
        path; this one exists so the *shape* — media type, ``run_id``, ``trace_id`` —
        is checkable with a single request even before any run exists.
        """
        from api.problems import RunNotFound

        raise RunNotFound(
            "this route always fails: it exists so the problem+json shape, the media type "
            "and the correlation fields can be checked with one request"
        )

    return app


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a trace id (and a run id when the path carries one) to the request context.

    Every structlog line inside the request then has both, and a 500's body can name a
    trace id that actually finds the traceback. The run id is read off the path or the
    query because the routes that need it already take it as a parameter — inventing a
    "current run" here would be a second source of truth about which run a user is
    looking at.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> JSONResponse:
        incoming = request.headers.get(get_settings().request_id_header) or new_trace_id()
        trace_token = trace_id_var.set(incoming)
        run_token = run_id_var.set(_run_id_from(request))
        try:
            response = await call_next(request)
        finally:
            trace_id_var.reset(trace_token)
            run_id_var.reset(run_token)
        response.headers[TRACE_HEADER] = incoming
        run_value = run_id_var.get() or _run_id_from(request)
        if run_value:
            response.headers["OXBOW-Run-Id"] = run_value
        logger.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            trace_id=incoming,
            run_id=run_value,
        )
        return response


def _run_id_from(request: Request) -> str | None:
    for candidate in (
        request.path_params.get("run_id"),
        request.query_params.get("run_id"),
    ):
        if candidate and len(str(candidate)) == 26:
            return str(candidate)
    return None


def _health_body(container: Container) -> HealthResponse:
    components = [
        ComponentStatus(
            name=component.name,
            state=component.state,
            detail=component.detail,
            fallback=component.fallback,
            latency_ms=component.latency_ms,
            checked_at=_iso(component.checked_at),
        )
        for component in container.components()
    ]
    degraded = [item.name for item in components if item.state != "available"]
    settings = container.settings
    return HealthResponse(
        status="ok" if not degraded else "degraded",
        service=SERVICE_NAME,
        version=API_VERSION,
        openapi_schema_version=OPENAPI_SCHEMA_VERSION,
        deployment_status="ok" if not degraded else "degraded: " + ", ".join(degraded),
        auth_mode=_auth_mode(settings),
        warehouse_backend=container.backend,
        write_path_enabled=container.write_path_enabled,
        audit_chain_ok=container.last_chain_ok,
        components=components,
        degraded_components=degraded,
    )


def _health_meta(container: Container) -> Any:
    from api.readmodel import ReadModel
    from api.schemas.common import Meta

    return Meta(
        run_id=None,
        trace_id=trace_id_var.get(),
        provenance="deployment",
        generated_at=_now(),
        disclaimer=ReadModel.disclaimer(),
        degraded=container.status() != "ok",
        degraded_reason=None
        if container.status() == "ok"
        else ", ".join(container.degraded_components()),
    )


def _detached_container(request: Request) -> Container:
    """A container built because the lifespan never ran.

    That happens in exactly one situation worth handling: a probe against an app object
    that was constructed but not started. It builds one, reports the truth about it, and
    says which it is — it does not pretend to be healthy.
    """
    container = getattr(request.app.state, "container", None)
    if container is not None:
        return container
    logger.error(
        "health requested before the lifespan built a container; probing freshly and caching "
        "the result on app.state so a repeated probe does not open another engine"
    )
    built = build_container(get_settings())
    request.app.state.container = built
    return built


def _install_cors_if_configured(app: FastAPI) -> None:
    raw = os.environ.get(CORS_ORIGINS_ENV, "").strip()
    if not raw:
        logger.info(
            "CORS is not installed: same-origin only, which is the local default. Set "
            f"{CORS_ORIGINS_ENV} to a comma-separated origin list to change that."
        )
        return
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if "*" in origins:
        raise RuntimeError(
            f"{CORS_ORIGINS_ENV} contains '*', which with credentials allowed means any page "
            "can read this API as the signed-in user. Name the origins."
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            get_settings().request_id_header,
        ],
        expose_headers=[TRACE_HEADER, "OXBOW-Run-Id"],
    )
    logger.warning(
        "CORS enabled for named origins — this is a security decision, not a convenience",
        origins=origins,
    )


def _auth_mode(settings: Any) -> str:
    if settings.oidc_issuer and settings.local_jwt_enabled:
        return "oidc-with-local-fallback"
    if settings.oidc_issuer:
        return "oidc"
    return "local-jwt-fallback"


def _json_logs_enabled() -> bool:
    return os.environ.get("OXBOW_LOG_FORMAT", "json").strip().lower() != "console"


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat()


_TAGS: Final = [
    {"name": "meta", "description": "Dataset card, licence, economics assumptions, disclaimer"},
    {"name": "runs", "description": "Immutable pipeline runs and their stage ledgers"},
    {"name": "events", "description": "SSE stage events with Last-Event-ID resume"},
    {"name": "alerts", "description": "The queue: server-filtered, ranked, cut off by capacity"},
    {"name": "cases", "description": "One account's evidence, scorecard, SHAP and decisions"},
    {"name": "decisions", "description": "Append-only decisions, four-eyes, outbox ledger"},
    {"name": "graph", "description": "Bounded subgraphs with communities collapsed"},
    {"name": "policy", "description": "The active policy and a real re-allocation"},
    {
        "name": "validation",
        "description": "Folds, ablation, calibration, fairness, scorecard studio",
    },
    {"name": "dashboard", "description": "Currency KPIs with their assumption bands"},
    {"name": "jobs", "description": "Pipeline and backtest jobs"},
    {"name": "auth", "description": "OIDC discovery, demo token, current principal"},
    {"name": "health", "description": "Liveness and labelled degraded components"},
]


app = create_app()


__all__ = [
    "API_VERSION",
    "CORS_ORIGINS_ENV",
    "DESCRIPTION",
    "SERVICE_NAME",
    "TRACE_HEADER",
    "app",
    "create_app",
]
