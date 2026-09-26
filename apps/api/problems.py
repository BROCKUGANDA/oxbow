"""RFC 9457 ``application/problem+json`` for every error this API can produce.

Plan §13 requires one thing that most FastAPI apps get wrong: the problem body has to
be in the **OpenAPI schema**, not just on the wire, because the generated TypeScript
client's error union is built from the declared response models. A handler that returns
a correct body for an undeclared schema leaves the client with ``unknown`` and the UI
with a cast — which is the ``any`` the plan forbids.

So three things live together here: the ``ProblemDetail`` model, the exception
hierarchy, and the ``responses=`` mapping each router passes to FastAPI. They cannot
drift apart, and the contract test asserts both halves: that a forced failure returns a
body matching the schema, and that the schema declares that body for every route.

Two fields are extensions beyond RFC 9457 and both are load-bearing (02 §F, plan §14):

* ``run_id`` — "every error surface prints the run_id with a copy button". An
  investigation tool whose failures cannot be attributed to a run cannot be reported.
* ``trace_id`` — the same line of structlog JSON that produced the failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.observability import get_logger, new_trace_id, run_id_var, trace_id_var

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
PROBLEM_TYPE_BASE: Final = "https://oxbow.dev/problems/"

# The status codes every route can answer with, declared once so the generated client
# gets the same union everywhere instead of a per-route approximation.
COMMON_ERROR_STATUSES: Final = (400, 401, 403, 404, 409, 422, 500, 502, 503)

_titles: dict[int, str] = {
    400: "Bad request",
    401: "Authentication required",
    403: "Not permitted",
    404: "Not found",
    409: "Conflict",
    412: "Precondition failed",
    422: "Request could not be processed",
    429: "Too many requests",
    500: "Internal error",
    502: "Upstream failure",
    503: "Dependency unavailable",
}


class ProblemFieldError(BaseModel):
    """One field-level failure, inside a 422."""

    model_config = ConfigDict(extra="forbid")

    location: str = Field(description="Dotted path to the offending field.")
    message: str
    value: Any | None = None


class ProblemDetail(BaseModel):
    """RFC 9457 problem details, plus the two OXBOW correlation fields.

    ``extra`` is allowed to be absent rather than empty: an unset ``run_id`` is a
    different claim from ``run_id: null`` in a response a human reads.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: str = Field(default="about:blank", description="A URI identifying the problem type.")
    title: str
    status: int = Field(ge=100, le=599)
    detail: str | None = None
    instance: str | None = Field(default=None, description="The request URI that produced it.")
    run_id: str | None = None
    trace_id: str | None = None
    errors: list[ProblemFieldError] | None = None
    retryable: bool = Field(
        default=False,
        description="Whether the same request may succeed later. Never true for a 4xx "
        "(plan §18: a retry loop around a 4xx is a hammer pointed at a validation error).",
    )

    def to_response(self) -> JSONResponse:
        """The wire form, with the problem media type set."""
        return JSONResponse(
            status_code=self.status,
            content=self.model_dump(exclude_none=True),
            media_type=PROBLEM_MEDIA_TYPE,
        )


class OxbowError(RuntimeError):
    """Base for every error this API turns into a problem document.

    Subclasses set ``status`` and ``code``; the message is the ``detail`` a user sees,
    so it is written to be read by someone deciding what to do next.
    """

    status: int = 500
    code: str = "internal-error"
    title: str = "Internal error"
    retryable: bool = False

    def __init__(
        self,
        detail: str,
        *,
        run_id: str | None = None,
        errors: Sequence[ProblemFieldError] | None = None,
        title: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.run_id = run_id
        self.errors = list(errors) if errors else None
        self.title_override = title

    def to_problem(self, request: Request | None = None) -> ProblemDetail:
        return ProblemDetail(
            type=f"{PROBLEM_TYPE_BASE}{self.code}",
            title=self.title_override or self.title,
            status=self.status,
            detail=self.detail,
            instance=str(request.url.path) if request is not None else None,
            run_id=self.run_id or run_id_var.get(),
            trace_id=trace_id_var.get() or new_trace_id(),
            errors=self.errors,
            retryable=self.retryable,
        )


class BadRequest(OxbowError):
    status, code, title = 400, "bad-request", "Bad request"


class Unauthorized(OxbowError):
    status, code, title = 401, "unauthenticated", "Authentication required"


class Forbidden(OxbowError):
    status, code, title = 403, "forbidden", "Not permitted"


class NotFound(OxbowError):
    status, code, title = 404, "not-found", "Not found"


class RunNotFound(NotFound):
    code, title = "run-not-found", "Run not found"


class CaseNotFound(NotFound):
    code, title = "case-not-found", "Case not found"


class Conflict(OxbowError):
    status, code, title, retryable = 409, "conflict", "Conflict", False


class VersionConflict(Conflict):
    """A decision write lost the race. The payload offers the merge view (plan §13)."""

    code, title = "version-conflict", "Case changed since you loaded it"

    def __init__(
        self,
        detail: str,
        *,
        run_id: str | None = None,
        expected_version: int | None = None,
        current_version: int | None = None,
        current: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail, run_id=run_id)
        self.expected_version = expected_version
        self.current_version = current_version
        self.current = dict(current or {})

    def to_problem(self, request: Request | None = None) -> ProblemDetail:
        problem = super().to_problem(request)
        payload = problem.model_dump(exclude_none=True)
        payload["expected_version"] = self.expected_version
        payload["current_version"] = self.current_version
        # The merge view: what is stored now, so the UI can show both side by side
        # rather than telling the analyst their work vanished.
        payload["current"] = self.current
        return ProblemDetail.model_validate(payload)


class Unprocessable(OxbowError):
    status, code, title = 422, "unprocessable", "Request could not be processed"


class UpstreamFailure(OxbowError):
    status, code, title, retryable = 502, "upstream-failure", "Upstream failure", True


class DependencyUnavailable(OxbowError):
    """A dependency is down and the route refuses to fake the answer.

    The plan's "degraded, not broken" rule (plan §14) is honoured one layer up: a list
    endpoint with an optional dependency returns a *labelled* degraded payload, while an
    endpoint whose whole answer depends on the missing piece fails loudly here. The
    distinction matters because a silently empty list reads as "no findings", and a
    4xx-shaped lie is worse than a 503 an operator can act on.
    """

    status, code, title, retryable = 503, "dependency-unavailable", "Dependency unavailable", True


def problem_responses(
    statuses: Sequence[int] = COMMON_ERROR_STATUSES, *, description: str = "RFC 9457 problem"
) -> dict[int | str, dict[str, Any]]:
    """The ``responses=`` argument every router passes.

    Built from ``ProblemDetail`` so the declared schema and the emitted body are the
    same object. If this returns nothing for a status, a client typed against the
    generated union gets ``unknown`` for that branch — which is the failure the plan
    names as an ``any``.
    """
    return {
        status: {
            "model": ProblemDetail,
            "description": f"{description} ({_titles.get(status, str(status))})",
        }
        for status in statuses
    }


def _log_unhandled(request: Request, exc: BaseException) -> None:
    logger = get_logger("oxbow.api.error")
    logger.error(
        "unhandled exception",
        path=str(request.url.path),
        method=request.method,
        error_type=type(exc).__name__,
        error=str(exc)[:500],
    )


def _rekey_problem_content(responses: Mapping[str, Any]) -> int:
    """Move every ``ProblemDetail`` response onto ``application/problem+json``.

    FastAPI derives the media type of an extra ``responses=`` entry from the route's
    *success* response class, so declaring ``model=ProblemDetail`` for a 400 writes
    ``content: {"application/json": ...}`` into the document while the handler emits
    ``application/problem+json``. That is precisely the drift this module's docstring says
    cannot happen, and it is not cosmetic: a generated client builds its error union from
    the declared media type, so a client would parse a problem document as a plain JSON
    body and never reach the ``type``/``title`` fields.

    Re-keying after generation keeps one source of truth — the ``ProblemDetail`` model,
    still registered through ``model=`` — and fixes only the content key. It is idempotent,
    so it is safe to run against FastAPI's cached schema.
    """
    moved = 0
    for status, definition in responses.items():
        if not isinstance(definition, dict) or status == "default":
            continue
        content = definition.get("content")
        if not isinstance(content, dict):
            continue
        json_body = content.get("application/json")
        if not isinstance(json_body, dict):
            continue
        if json_body.get("schema", {}).get("$ref", "").endswith("/ProblemDetail"):
            content.pop("application/json")
            content[PROBLEM_MEDIA_TYPE] = json_body
            moved += 1
    return moved


def install_problem_media_type(app: FastAPI) -> None:
    """Make the declared error media type the one the handlers actually send.

    Wrapping ``app.openapi`` rather than post-processing one document means every consumer
    of the schema — the docs route, the generated client, the doctrine test and this suite —
    reads the same corrected document.
    """
    original = app.openapi

    def openapi() -> dict[str, Any]:
        document = original()
        moved = 0
        for path_item in document.get("paths", {}).values():
            if not isinstance(path_item, dict):
                continue
            for operation in path_item.values():
                if isinstance(operation, dict) and isinstance(operation.get("responses"), dict):
                    moved += _rekey_problem_content(operation["responses"])
        app.openapi_schema = document
        if moved:
            get_logger("oxbow.api.problems").info(
                "declared error bodies re-keyed to the problem media type",
                responses=moved,
                media_type=PROBLEM_MEDIA_TYPE,
            )
        return document

    app.openapi = openapi  # type: ignore[method-assign]


def register_problem_handlers(app: FastAPI) -> None:
    """Wire every failure path to the same problem shape.

    The catch-all is the last handler for a reason: without it FastAPI returns a bare
    ``Internal Server Error`` text body, which is not a problem document, is not typed
    in the schema, and gives a user nothing to copy. The exception itself goes to the
    log with its traceback; the response gets a trace id to find it by.

    The schema is corrected in the same call as the handlers are installed, because they
    are one contract: a body shape and the media type that says what the body is.
    """
    install_problem_media_type(app)

    @app.exception_handler(OxbowError)
    async def _oxbow_error(request: Request, exc: OxbowError) -> JSONResponse:
        if exc.status >= 500:
            _log_unhandled(request, exc)
        return exc.to_problem(request).to_response()

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            ProblemFieldError(
                location=".".join(str(part) for part in issue.get("loc", ())[1:]) or "body",
                message=str(issue.get("msg", "invalid value")),
                value=issue.get("input"),
            )
            for issue in exc.errors()
        ]
        problem = Unprocessable(
            "the request did not satisfy this route's schema", errors=errors
        ).to_problem(request)
        return problem.to_response()

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        return _map_http_status(exc.status_code, detail).to_problem(request).to_response()

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        _log_unhandled(request, exc)
        trace = trace_id_var.get() or new_trace_id()
        problem = ProblemDetail(
            type=f"{PROBLEM_TYPE_BASE}internal-error",
            title=_titles[500],
            status=500,
            detail=(
                "the request failed inside the API. The traceback is in the server log "
                f"under trace_id {trace}; the body deliberately does not repeat it."
            ),
            instance=str(request.url.path),
            run_id=run_id_var.get(),
            trace_id=trace,
            retryable=True,
        )
        return problem.to_response()


def _map_http_status(status: int, detail: str) -> OxbowError:
    for candidate in (BadRequest, Unauthorized, Forbidden, Conflict, Unprocessable):
        if candidate.status == status:
            return candidate(detail)
    return DependencyUnavailable(detail) if status >= 500 else BadRequest(detail)


__all__ = [
    "COMMON_ERROR_STATUSES",
    "PROBLEM_MEDIA_TYPE",
    "PROBLEM_TYPE_BASE",
    "BadRequest",
    "CaseNotFound",
    "Conflict",
    "DependencyUnavailable",
    "Forbidden",
    "NotFound",
    "OxbowError",
    "ProblemDetail",
    "ProblemFieldError",
    "RunNotFound",
    "Unprocessable",
    "UpstreamFailure",
    "VersionConflict",
    "problem_responses",
    "install_problem_media_type",
    "register_problem_handlers",
]
