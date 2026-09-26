"""Health, degraded-component reporting and job status.

Plan §14's "degraded, not broken" rule needs one thing from the API: a machine-readable
answer to "what is missing right now, and what do you render instead of it?". A pane
that silently shows a greedy allocation because the solver is down is the failure; a
pane that shows a **labelled** degraded banner with the greedy allocation is the
requirement. So each component reports the fallback it is using, in words, from here.

Two distinctions this file refuses to blur:

* ``available`` is not ``healthy``. A dependency can be reachable and still refused
  (Keycloak up but the realm missing this client), which is reported as unavailable
  with the reason, because a banner that says "sign-in unavailable: realm oxbow has no
  client oxbow-web" is actionable and a banner that says "degraded" is not.
* ``status`` is ``ok`` or ``degraded`` — never ``false`` with an error field. The
  HTTP code carries success or failure for a *request*; this object describes the
  deployment, and the two must not be confused (a 200 whose body is a failure is the
  standing rule this endpoint exists to obey).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Every name ``api.deps.PROBED_COMPONENTS`` can report. This list and that one are the
# same contract seen from two sides, so ``test_health_schema_covers_every_probed_component``
# in tests/integration/test_p7_api.py fails if either grows without the other: an omitted
# name is not a validation warning, it is ``/healthz`` answering 500 with a Pydantic
# LiteralError, which is the one endpoint a degraded deployment is relying on.
ComponentName = Literal[
    "warehouse",
    "redis",
    "mlflow",
    "solver",
    "summariser",
    "keycloak",
    "objectstore",
    "watchlist",
]
ComponentState = Literal["available", "degraded", "unavailable"]

# The degraded-banner copy each component owes the UI when it is not available.
# Stated as data here so three routers and one client cannot paraphrase it three ways.
FALLBACKS: dict[str, str] = {
    "solver": "greedy allocation by EV density; no optimality gap is available",
    "summariser": "template narrative built from stored reason codes and rule hits",
    "mlflow": "run provenance from the warehouse row only; no experiment metadata",
    "keycloak": "local HS256 demo tokens, minted by this process and labelled as such",
    "objectstore": "no evidence bundle download; evidence rows still render inline",
    "redis": "jobs run synchronously or not at all; the outbox is not drained",
    "warehouse": "no operational data at all — every data route answers 503",
    "watchlist": "no screening: a clean screen is unavailable rather than reported as clean",
}


class ComponentStatus(BaseModel):
    """One dependency: reachable or not, and what is rendered instead if not."""

    model_config = ConfigDict(extra="forbid")

    name: ComponentName
    state: ComponentState
    detail: str = Field(
        description="What was actually probed, and what was found. 'checked http://.../health "
        "and got 200', not 'ok'."
    )
    fallback: str | None = Field(
        default=None,
        description="Set whenever ``state`` is not ``available``: what the UI shows instead, "
        "so the degraded banner can name the substitution rather than hide it.",
    )
    checked_at: str
    latency_ms: int | None = None


class HealthResponse(BaseModel):
    """The body of ``/healthz`` and ``/api/health``.

    ``deployment_status`` is the labelled degraded flag the banner keys off.
    ``auth_mode`` and ``warehouse_backend`` are included because an operator
    debugging a 401 or an empty queue has to be able to tell, from one response,
    whether they are looking at the demo fallback or the real thing.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    service: str
    version: str
    openapi_schema_version: str
    deployment_status: str = Field(
        description="'ok' or a degraded label naming which optional dependency is missing."
    )
    auth_mode: Literal["oidc", "local-jwt-fallback", "oidc-with-local-fallback"]
    warehouse_backend: Literal["postgres", "null-file"]
    write_path_enabled: bool = Field(
        description="Whether decision writes are possible in this deployment. False on the "
        "null-file warehouse, which is read-only by construction, and the reason is in "
        "the ``warehouse`` component's detail rather than implied by a missing field."
    )
    audit_chain_ok: bool | None = Field(
        default=None,
        description="Result of the last chain walk performed by this process, or null when "
        "no verification has run since startup. ``make verify-audit`` is the authoritative "
        "check; this is the same arithmetic with a shorter reach.",
    )
    components: list[ComponentStatus] = Field(default_factory=list)
    degraded_components: list[str] = Field(default_factory=list)


class JobStatus(BaseModel):
    """One queued job as the database knows it, not as RQ remembers it.

    ``job_run`` exists precisely because RQ's own state lives in Redis and cannot be
    joined to a run: the ledger here is what makes "what did we run, when, and what
    came out" answerable after a flush.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    job_id: str
    kind: str
    run_id: str | None
    queue: str
    state: str
    created_at: Any
    finished_at: Any | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    trace_id: str | None = None


class StreamState(BaseModel):
    """What an SSE subscriber is attached to, and where its cursor is.

    Returned by ``GET /api/runs/{run_id}/events`` alongside the ledger so a client
    that joined late can tell the difference between "nothing has happened" and
    "everything happened before you arrived".
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    resume_from: int
    last_event_id: int | None
    reconnect: bool = Field(
        description="True when the caller sent ``Last-Event-ID`` and got a backfill from "
        "stored rows rather than a re-emission of anything already delivered."
    )


__all__ = [
    "FALLBACKS",
    "ComponentName",
    "ComponentState",
    "ComponentStatus",
    "HealthResponse",
    "JobStatus",
    "StreamState",
]
