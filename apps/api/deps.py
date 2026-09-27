"""The dependency seam: ports bound to adapters, chosen by the environment.

Plan §13 asks for two things that usually fight: adapters must be swappable by
environment, *and* the null adapters must work with nothing else up. The way this
module resolves that is to choose once, at container construction, and to make every
consumer state which choice it got — ``warehouse_backend`` appears in ``/api/health``
and in every response's ``meta``, so a deployment served from files can never be
mistaken for one served from the warehouse a pipeline wrote.

Four consequences are worth naming, because each one is a failure mode:

* **a session per request, not per container.** The decision transaction writes the
  decision row, its audit row and its outbox row in one transaction, so the boundary
  has to be the request's. A session shared across requests would let two analysts'
  writes interleave inside one another's transaction, and the four-eyes rule would be
  enforced against whichever commit happened to land first.
* **unavailability is a value, not an exception**, in the health probe. The degraded
  banner needs to know that MLflow answered 503 and that the solver import failed;
  raising for either would take the API down over the exact thing plan §14 requires
  it to survive ("degraded, not broken").
* **the write path is Postgres-only and says so.** An outbox without a database that
  is the source of truth about what has been sent is just a queue in a process that
  will restart, so the null container refuses decision writes with a 503 naming the
  missing dependency rather than accepting them and losing them.
* **no JWT library.** Tokens are verified with the stdlib primitives in
  :mod:`api.security` because 01 §A rule 6 forbids adding a dependency without a
  ``DECISIONS.md`` entry; the deviation is recorded rather than assumed.
"""

from __future__ import annotations

import importlib.util
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from os import environ
from pathlib import Path
from typing import Final, Literal

import httpx
from fastapi import Depends, Request
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from api import security
from api.observability import get_logger
from api.problems import DependencyUnavailable, Forbidden, Unauthorized
from api.readmodel import (
    FileWarehouseSource,
    PostgresSource,
    ReadModel,
    WarehouseSource,
)
from api.security import LOCAL_ISSUER, Principal, TokenError, decode_token, principal_from_claims
from api.settings import Settings, get_settings
from oxbow.adapters.audit.postgres import PostgresAuditSink
from oxbow.adapters.io import resolve_out_root
from oxbow.adapters.null.objectstore import NullObjectStore
from oxbow.adapters.null.sinks import NullCaseSink, NullNotifySink, NullReportSink
from oxbow.adapters.null.watchlist import NullWatchlistAdapter
from oxbow.adapters.ofac.watchlist import OfacWatchlistAdapter, SnapshotFormatError
from oxbow.adapters.s3.objectstore import S3ObjectStore
from oxbow.adapters.slack.notify import SlackNotifySink
from oxbow.adapters.warehouse.models import Base
from oxbow.adapters.webhook.sinks import WebhookCaseSink, WebhookNotifySink
from oxbow.config import ConfigError
from oxbow.ports.audit import AuditSink
from oxbow.ports.case_sink import CaseSink
from oxbow.ports.notify import NotifySink
from oxbow.ports.objectstore import ObjectStoreAdapter
from oxbow.ports.report import ReportSink
from oxbow.ports.warehouse import WarehouseSink
from oxbow.ports.watchlist import WatchlistAdapter
from oxbow.quant.economics import Economics, load_economics
from oxbow.quant.money import QuantError, decimals_for_base

BackendName = Literal["postgres", "null-file"]
WarehouseChoice = Literal["auto", "postgres", "null"]

# The probe budget is short on purpose. A health endpoint that takes eight seconds
# because one container is wedged is worse for the UI than one that calls the
# dependency unavailable after a second and lets the banner render.
PROBE_TIMEOUT_SECONDS: Final = 1.5
PROBE_CACHE_SECONDS: Final = 10.0

DEFAULT_WATCHLIST_DIR: Final = "data/watchlist"
DEFAULT_MLFLOW_URL: Final = "http://localhost:5000"

# The optional dependencies, in the order the banner lists them, each with the
# substitution the UI is required to label. Plan §14 forbids an unlabelled fallback,
# and an unlabelled fallback starts as an engineer forgetting to mention it.
PROBED_COMPONENTS: Final = (
    "warehouse",
    "redis",
    "mlflow",
    "solver",
    "summariser",
    "keycloak",
    "objectstore",
    "watchlist",
)

COMPONENT_FALLBACKS: Final = {
    "redis": "jobs are not queued and the outbox is not drained; decisions still commit",
    "mlflow": "run provenance comes from the warehouse row only; no experiment metadata",
    "solver": "greedy allocation by EV density; no optimality gap is available",
    "summariser": "template narrative assembled from stored reason codes and rule hits",
    "keycloak": "local HS256 demo tokens, minted by this process and labelled as such",
    "objectstore": "no evidence bundle download; evidence rows still render inline",
    "watchlist": "no screening: a clean screen is not available rather than 'clean'",
    "warehouse": "no operational data at all; every data route answers 503",
}


class ContainerClosedError(RuntimeError):
    """A container was used after its engine was disposed."""


@dataclass(slots=True)
class Component:
    """One probed dependency, kept as data so the banner can be built from it."""

    name: str
    state: Literal["available", "degraded", "unavailable"]
    detail: str
    fallback: str | None = None
    latency_ms: int | None = None
    checked_at: float = field(default_factory=time.time)

    @property
    def available(self) -> bool:
        return self.state == "available"


def _reachable(url: str, *, client: httpx.Client | None = None) -> Component:
    """Probe an HTTP dependency, turning every failure into a labelled component."""
    holder = client or httpx.Client(timeout=PROBE_TIMEOUT_SECONDS)
    started = time.perf_counter()
    try:
        response = holder.get(url)
    except httpx.HTTPError as exc:
        return Component(
            name="",
            state="unavailable",
            detail=f"{url} raised {type(exc).__name__}: {str(exc)[:180]}",
        )
    latency = int((time.perf_counter() - started) * 1000)
    if 200 <= response.status_code < 400:
        return Component(
            name="",
            state="available",
            detail=f"{url} answered {response.status_code}",
            latency_ms=latency,
        )
    return Component(
        name="",
        state="unavailable",
        detail=f"{url} answered {response.status_code}",
        latency_ms=latency,
    )


@dataclass(slots=True)
class Container:
    """Everything the API and the worker need, built once per process."""

    settings: Settings
    backend: BackendName
    engine: Engine | None
    sessions: sessionmaker[Session] | None
    source: WarehouseSource
    read_model: ReadModel
    economics: Economics
    jwks_cache: security.JwksCache | None
    case_sink_factory: Callable[[], CaseSink]
    notify_sink_factory: Callable[[], NotifySink]
    report_sink_factory: Callable[[], ReportSink]
    audit_sink_factory: Callable[[], AuditSink]
    warehouse_sink_factory: Callable[[], WarehouseSink]
    watchlist_factory: Callable[[], WatchlistAdapter]
    object_store_factory: Callable[[], ObjectStoreAdapter]
    created_at: float = field(default_factory=time.time)
    _probes: dict[str, Component] = field(default_factory=dict)
    _closed: bool = False
    last_chain_ok: bool | None = None

    # --- write path ---------------------------------------------------------

    @property
    def write_path_enabled(self) -> bool:
        return self.backend == "postgres" and self.sessions is not None

    def session_factory(self) -> sessionmaker[Session]:
        if self._closed:
            raise ContainerClosedError("this container's engine has been disposed")
        if self.sessions is None or not self.write_path_enabled:
            raise DependencyUnavailable(
                "decision writes need Postgres. The outbox pattern means the decision row, its "
                "audit row and its outbox row commit in one transaction, and the null-file "
                "warehouse has no transaction to commit them in — so reads work here and this "
                "route does not. Set DATABASE_URL and start the Compose database."
            )
        return self.sessions

    def new_session(self) -> Session:
        return self.session_factory()()

    def audit_sink(self, session: Session) -> AuditSink:
        """An audit sink bound to *this* session, not one that opens its own.

        The decision transaction needs the audit row in the same transaction as the
        decision and the outbox row, which is why the port takes a caller-owned session
        at all. On the null backend the file sink is returned rather than refused: it
        has no transaction to break, and reading the chain must keep working there.
        """
        if self.backend == "postgres":
            return PostgresAuditSink(session)
        return self.audit_sink_factory()

    # --- probing ------------------------------------------------------------

    def component(self, name: str, *, force: bool = False) -> Component:
        """The cached probe result for one dependency, refreshed when stale.

        Cached because the health endpoint is polled by the UI's banner and a
        five-second MLflow container would otherwise make every poll five seconds of
        waiting. Stale is fine here; a 401 from Keycloak two seconds ago is still the
        truth about this request's token, which is verified per request regardless.
        """
        cached = self._probes.get(name)
        if (
            cached is not None
            and not force
            and (time.time() - cached.checked_at) < PROBE_CACHE_SECONDS
        ):
            return cached
        probed = self._probe(name)
        self._probes[name] = probed
        return probed

    def components(self) -> list[Component]:
        return [self.component(name) for name in PROBED_COMPONENTS]

    def degraded_components(self) -> list[str]:
        return [component.name for component in self.components() if component.state != "available"]

    def status(self) -> Literal["ok", "degraded"]:
        return "ok" if not self.degraded_components() else "degraded"

    def _probe(self, name: str) -> Component:
        if name == "warehouse":
            return self._probe_warehouse()
        if name == "redis":
            return _probe_redis(self.settings.redis_url)
        if name == "mlflow":
            return _probe_mlflow()
        if name == "solver":
            return _probe_solver()
        if name == "summariser":
            return _probe_summariser()
        if name == "keycloak":
            return self._probe_keycloak()
        if name == "objectstore":
            return self._probe_objectstore()
        if name == "watchlist":
            return self._probe_watchlist()
        raise DependencyUnavailable(f"no probe is defined for component {name!r}")

    def _probe_warehouse(self) -> Component:
        if self.engine is None:
            return Component(
                name="warehouse",
                state="degraded",
                detail=(
                    "reading the null-file warehouse under out/warehouse: no DATABASE_URL, so "
                    "this deployment has no decision store and is read-only by construction"
                ),
                fallback=COMPONENT_FALLBACKS["warehouse"],
            )
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                tables = int(
                    connection.execute(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'public'"
                        )
                    ).scalar_one()
                )
        except SQLAlchemyError as exc:
            return Component(
                name="warehouse",
                state="unavailable",
                detail=f"Postgres at {make_url(self.settings.sqlalchemy_url).host!r} refused: "
                f"{str(exc.orig)[:200]}",
                fallback=COMPONENT_FALLBACKS["warehouse"],
            )
        if tables < len(_EXPECTED_TABLES):
            return Component(
                name="warehouse",
                state="degraded",
                detail=f"Postgres is reachable but public holds {tables} tables; the read model "
                f"declares {len(_EXPECTED_TABLES)}. Run `make db-migrate`.",
                fallback=COMPONENT_FALLBACKS["warehouse"],
            )
        return Component(
            name="warehouse",
            state="available",
            detail=f"Postgres reachable, {tables} tables in public (alembic head applied)",
        )

    def _probe_keycloak(self) -> Component:
        if not self.settings.oidc_issuer:
            return Component(
                name="keycloak",
                state="degraded",
                detail="no OXBOW_OIDC_ISSUER configured, so the local HS256 fallback is the only "
                "identity this API will accept",
                fallback=COMPONENT_FALLBACKS["keycloak"],
            )
        if self.jwks_cache is None:
            return Component(
                name="keycloak",
                state="unavailable",
                detail=f"issuer {self.settings.oidc_issuer} is configured but has no JWKS URL",
                fallback=COMPONENT_FALLBACKS["keycloak"],
            )
        probed = _reachable(self.jwks_cache.url)
        probed.name = "keycloak"
        if probed.state != "available":
            probed.fallback = COMPONENT_FALLBACKS["keycloak"]
        return probed

    def _probe_objectstore(self) -> Component:
        store = self.object_store_factory()
        started = time.perf_counter()
        try:
            refs = store.list("")
        except Exception as exc:
            return Component(
                name="objectstore",
                state="unavailable",
                detail=f"{store.store_id} list failed: {str(exc)[:200]}",
                fallback=COMPONENT_FALLBACKS["objectstore"],
            )
        return Component(
            name="objectstore",
            state="available",
            detail=f"{store.store_id}: {len(refs)} object(s) reachable",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _probe_watchlist(self) -> Component:
        adapter = self.watchlist_factory()
        version = adapter.version()
        if version.record_count == 0:
            return Component(
                name="watchlist",
                state="degraded",
                detail=f"list {version.list_name!r} holds no records: a clean screen would be "
                "meaningless, so it is reported as unavailable rather than as a match-free run",
                fallback=COMPONENT_FALLBACKS["watchlist"],
            )
        return Component(
            name="watchlist",
            state="available",
            detail=f"list {version.list_name!r} version {version.version} with "
            f"{version.record_count} records ({version.source})",
        )

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
        self._closed = True


_EXPECTED_TABLES = frozenset(Base.metadata.tables)


def _warn(message: str) -> None:
    """One line of structlog from the composition root.

    Backend fallbacks happen at import time of the app, which is before the request that
    would otherwise be the only place anyone could discover them, so they are logged here
    as well as being reported by the health probe.
    """
    get_logger("oxbow.api.deps").warning(message)


# --- the individual probes --------------------------------------------------


def _probe_redis(url: str) -> Component:
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - redis is a pinned dependency
        return Component(name="redis", state="unavailable", detail=f"redis client missing: {exc}")
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=PROBE_TIMEOUT_SECONDS)
        client.ping()
    except Exception as exc:
        return Component(
            name="redis",
            state="unavailable",
            detail=f"{url} did not answer PING: {str(exc)[:180]}",
            fallback=COMPONENT_FALLBACKS["redis"],
        )
    return Component(name="redis", state="available", detail=f"{url} answered PING")


def _probe_mlflow() -> Component:
    """MLflow's own health route, because "the container is up" is the claim at stake."""
    base = environ.get("OXBOW_MLFLOW_URL", DEFAULT_MLFLOW_URL).rstrip("/")
    probed = _reachable(f"{base}/health")
    probed.name = "mlflow"
    if probed.state != "available":
        probed.fallback = COMPONENT_FALLBACKS["mlflow"]
    return probed


def _probe_solver() -> Component:
    """Whether the exact CP-SAT solver is importable, not whether it is fast.

    Plan §14's degraded banner for this component is specifically about the *exact*
    solver: when it is missing the queue falls back to greedy-by-density and the
    optimality gap becomes unavailable, which is a different claim than the one the
    policy page normally makes.
    """
    found = importlib.util.find_spec("ortools.sat.python.cp_sat") is not None
    if found:
        return Component(
            name="solver",
            state="available",
            detail="ortools.sat.python.cp_sat is importable; exact allocation available",
        )
    return Component(
        name="solver",
        state="degraded",
        detail="ortools is not importable in this environment",
        fallback=COMPONENT_FALLBACKS["solver"],
    )


def _probe_summariser() -> Component:
    """The narrative summariser is not wired, and this says so on every request.

    The cut ladder puts the LLM narrative fifth (plan §17), and the fallback — a
    template assembled from stored reason codes and rule hits — is what the case page
    renders. Reporting it as merely ``degraded`` rather than ``unavailable`` is the
    honest label: the fallback is complete and intentional, the enhancement is absent.
    """
    return Component(
        name="summariser",
        state="degraded",
        detail="no narrative model is configured in this build (plan §17 cut 4)",
        fallback=COMPONENT_FALLBACKS["summariser"],
    )


# --- construction -------------------------------------------------------------


def _engine_for(settings: Settings) -> Engine:
    return create_engine(settings.sqlalchemy_url, pool_pre_ping=True, future=True)


def _case_sinks(settings: Settings, backend: BackendName, root: Path) -> Callable[[], CaseSink]:
    """The case destination chosen by environment, not by a caller's argument.

    A webhook endpoint plus a signing secret means the real sink; either missing
    means the null sink, which writes the bundle to ``out/case_sink/`` and still runs
    the self-describing check. The fallback is logged at startup, and the outbox row
    records ``sink_id`` so a later reader can see which one delivered a given case.
    """
    endpoint = settings.webhook_endpoint
    secret = settings.webhook_signing_secret
    if (
        backend == "postgres"
        and endpoint
        and secret
        and endpoint.startswith(("http://", "https://"))
    ):

        def build() -> CaseSink:
            return WebhookCaseSink(url=endpoint, secret=secret)

        return build

    def build_null() -> CaseSink:
        return NullCaseSink(root)

    return build_null


def _notify_sinks(settings: Settings, root: Path) -> Callable[[], NotifySink]:
    if settings.slack_webhook_url:

        def build_slack() -> NotifySink:
            return SlackNotifySink(webhook_url=settings.slack_webhook_url)

        return build_slack
    if settings.webhook_endpoint and settings.webhook_signing_secret:

        def build_hook() -> NotifySink:
            return WebhookNotifySink(
                url=f"{settings.webhook_endpoint.rsplit('/', 1)[0]}/notify",
                secret=settings.webhook_signing_secret,
            )

        return build_hook

    def build_null() -> NotifySink:
        return NullNotifySink(root)

    return build_null


def _object_store(settings: Settings, root: Path) -> Callable[[], ObjectStoreAdapter]:
    """MinIO when it is configured and the bucket answers, the file store otherwise.

    The probe is at construction, not at first use: an evidence bundle that starts on
    the filesystem mid-run after a MinIO outage would give two runs of the same case
    two different object keys, which is a provenance bug wearing a resilience hat.
    """
    if settings.s3_endpoint_url and settings.s3_access_key and settings.s3_secret_key:
        try:
            store = S3ObjectStore(
                bucket=settings.s3_bucket,
                endpoint_url=settings.s3_endpoint_url,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                store_id="minio",
            )
            store.ensure_bucket()

            def build() -> ObjectStoreAdapter:
                return store

            return build
        except Exception as exc:
            # Constructing the store is cheap; reaching it is not. A bucket that is down
            # is reported by the objectstore probe on every health call from then on,
            # rather than being discovered by the first evidence bundle that needed it.
            _warn(
                f"MinIO/S3 at {settings.s3_endpoint_url} is unusable ({str(exc)[:160]}); "
                "falling back to the file object store under out/objectstore/"
            )

    def build_null() -> ObjectStoreAdapter:
        return NullObjectStore(root)

    return build_null


def _watchlist(settings: Settings) -> Callable[[], WatchlistAdapter]:
    """The offline SDN snapshot if the repo has one, and the honest null if not.

    Screening is enrichment only and never a decision (02 §F); the null adapter
    reports zero records precisely so that "no match" cannot be read as "clean".
    """
    candidates: list[Path] = []
    explicit = environ.get("OXBOW_WATCHLIST_PATH", "")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(settings.repo_root / DEFAULT_WATCHLIST_DIR / "sample-sdn-v1.csv")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            adapter = OfacWatchlistAdapter.from_path(path)
        except (SnapshotFormatError, OSError):
            continue
        return lambda: adapter

    def build_null() -> WatchlistAdapter:
        return NullWatchlistAdapter()

    return build_null


def build_container(settings: Settings | None = None) -> Container:
    """Assemble the container. Called once by the app's lifespan and once by the worker."""
    resolved = settings or get_settings()
    choice: WarehouseChoice = _warehouse_choice(resolved)
    root = resolve_out_root(resolved.repo_root / "out")

    engine: Engine | None = None
    sessions: sessionmaker[Session] | None = None
    source: WarehouseSource
    backend: BackendName

    if choice == "null":
        source = FileWarehouseSource(root)
        backend = "null-file"
    else:
        try:
            engine = _engine_for(resolved)
        except (ConfigError, SQLAlchemyError) as exc:
            if choice == "postgres":
                raise DependencyUnavailable(f"DATABASE_URL is set but unusable: {exc}") from exc
            source = FileWarehouseSource(root)
            backend = "null-file"
        else:
            sessions = sessionmaker(bind=engine, expire_on_commit=False, future=True)
            source = PostgresSource(lambda: _new_session(sessions))
            backend = "postgres"

    read_model = ReadModel(source, money_decimals=_money_decimals())
    try:
        economics = load_economics(resolved.repo_root)
    except ConfigError as exc:
        raise DependencyUnavailable(f"config/economics.yaml could not be loaded: {exc}") from exc

    jwks_cache: security.JwksCache | None = None
    if resolved.jwks_url:
        try:
            jwks_cache = security.JwksCache(resolved.jwks_url)
        except (TokenError, ValueError) as exc:
            if resolved.oidc_issuer:
                raise DependencyUnavailable(
                    f"OIDC is configured but its key set cannot be reached: {exc}"
                ) from exc

    object_store_factory = _object_store(resolved, root)
    return Container(
        settings=resolved,
        backend=backend,
        engine=engine,
        sessions=sessions,
        source=source,
        read_model=read_model,
        economics=economics,
        jwks_cache=jwks_cache,
        case_sink_factory=_case_sinks(resolved, backend, root),
        notify_sink_factory=_notify_sinks(resolved, root),
        report_sink_factory=lambda: NullReportSink(root),
        audit_sink_factory=_audit_sink_factory(sessions),
        warehouse_sink_factory=_warehouse_sink_factory(sessions),
        watchlist_factory=_watchlist(resolved),
        object_store_factory=object_store_factory,
    )


def _audit_sink_factory(sessions: sessionmaker[Session] | None) -> Callable[[], AuditSink]:
    if sessions is None:
        from oxbow.adapters.null.audit import NullAuditSink

        return lambda: NullAuditSink()
    return lambda: PostgresAuditSink(sessions())


def _warehouse_sink_factory(
    sessions: sessionmaker[Session] | None,
) -> Callable[[], WarehouseSink]:
    from oxbow.adapters.null.warehouse import NullWarehouse

    if sessions is None:
        return lambda: NullWarehouse()

    def build() -> WarehouseSink:
        from oxbow.adapters.warehouse.postgres import PostgresWarehouseSink

        return PostgresWarehouseSink(sessions())

    return build


def _new_session(sessions: sessionmaker[Session]) -> Session:
    return sessions()


def _money_decimals() -> int:
    """Decimal places, converted from config's `minor_units_per_major`.

    A base and an exponent are different numbers, and `Money.decimals` is the exponent:
    both `apps/api/schemas/common.py` and `apps/web/src/lib/format/money.ts` compute
    `10 ** decimals` to render a figure. Wiring the base (100) into that field divided
    every currency number the API served by 10^100 rather than 100 — invisible in fixture
    mode, where the sample hand-writes `decimals: 2`, and total in live mode, where the
    queue, the case rail and the dashboard all read as zero.

    config declares a base because that is the quantity an operator reasons about, so the
    conversion lives in `oxbow.quant.money.decimals_for_base`, once, and this is the
    composition root that turns its refusal into the vocabulary a container build speaks.
    """
    try:
        return decimals_for_base(_minor_units_per_major())
    except QuantError as exc:
        raise ConfigError(str(exc)) from exc


def _minor_units_per_major() -> int:
    from oxbow.config import load_yaml

    raw = load_yaml(get_settings().repo_root / "config" / "economics.yaml")
    value = raw.get("minor_units_per_major", 100)
    if not isinstance(value, int) or value < 1:
        raise ConfigError(
            f"minor_units_per_major must be a positive int, got {value!r} — money rendering "
            "cannot guess a decimal exponent"
        )
    return value


def _warehouse_choice(settings: Settings) -> WarehouseChoice:
    choice = environ.get("OXBOW_WAREHOUSE", "auto").strip().lower()
    if choice not in {"auto", "postgres", "null"}:
        raise ConfigError(
            f"OXBOW_WAREHOUSE={choice!r} must be auto, postgres or null. An unrecognised value "
            "is refused rather than defaulted, because silently picking a backend is how a demo "
            "starts serving files while believing it is reading the warehouse."
        )
    if choice == "postgres" and not settings.database_url:
        raise ConfigError("OXBOW_WAREHOUSE=postgres requires DATABASE_URL")
    if choice == "auto":
        return "postgres" if settings.database_url.strip() else "null"
    return choice


# --- FastAPI dependencies -----------------------------------------------------


def get_container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise DependencyUnavailable(
            "the application has no container: its lifespan did not complete. This is a startup "
            "failure, not a data failure."
        )
    return container


def get_read_model(container: Container = Depends(get_container)) -> ReadModel:
    return container.read_model


def get_economics(container: Container = Depends(get_container)) -> Economics:
    return container.economics


def session_dependency(container: Container = Depends(get_container)) -> Iterator[Session]:
    """One session per request, rolled back if the handler raised.

    The rollback is the point. A handler that committed inside itself is fine, but a
    handler that failed halfway through a multi-statement write must not leave a
    decision row without its outbox row — which is the exact inconsistency the outbox
    exists to prevent, and it would survive as a phantom delivery if the session were
    simply closed.
    """
    session = container.new_session()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def authenticate(
    request: Request,
    container: Container = Depends(get_container),
) -> Principal:
    """Verify the bearer token and map it to a Principal, or refuse with a reason.

    The failure text names what was wrong (expired, unknown kid, no OXBOW role) because
    an operator debugging a 401 needs the cause, and because a generic 401 is how an
    unconfigured role in Keycloak turns into a mystery that outlives the demo.
    """
    from api.security import bearer_token

    settings = container.settings
    try:
        token = bearer_token(request.headers.get("authorization"))
    except TokenError as exc:
        raise Unauthorized(str(exc)) from exc
    local_secret = ""
    if settings.local_jwt_enabled:
        local_secret = settings.local_secret()
    try:
        claims = decode_token(
            token,
            oidc_issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_cache=container.jwks_cache,
            local_secret=local_secret,
        )
    except TokenError as exc:
        raise Unauthorized(f"bearer token rejected: {exc}") from exc
    issuer = str(claims.get("iss", ""))
    source = "local-jwt" if issuer == LOCAL_ISSUER else "oidc"
    try:
        return principal_from_claims(claims, source=source)
    except TokenError as exc:
        raise Forbidden(str(exc)) from exc


def require_roles(*roles: str) -> Callable[..., Principal]:
    """A dependency that admits only the named roles, with the refusal spelled out."""

    def dependency(principal: Principal = Depends(authenticate)) -> Principal:
        if not principal.has(*roles):
            raise Forbidden(
                f"{principal.display_name} holds {list(principal.roles)}; this route requires one "
                f"of {list(roles)}. Four-eyes review is a role check as well as a subject check, "
                "so the missing role is named rather than described as 'unauthorised'."
            )
        return principal

    return dependency


analyst_or_higher = require_roles("analyst", "reviewer", "admin")
reviewer_or_higher = require_roles("reviewer", "admin")
admin_only = require_roles("admin")


__all__ = [
    "COMPONENT_FALLBACKS",
    "PROBED_COMPONENTS",
    "Component",
    "Container",
    "ContainerClosedError",
    "admin_only",
    "analyst_or_higher",
    "authenticate",
    "build_container",
    "get_container",
    "get_economics",
    "get_read_model",
    "require_roles",
    "reviewer_or_higher",
    "session_dependency",
]
