"""SSE stage events: one Pydantic model shared by the stream, the schema and the DB.

Plan §13 requires a "shared Pydantic model" for a specific reason: the ledger in the
UI renders ``stage``, ``status``, ``rows`` and ``elapsed_ms`` as rows *filling*, and if
the streamed shape and the OpenAPI shape differ, the client either casts to ``any`` or
the ledger renders a field that was never sent. One model, three consumers.

The event ``id`` is the ``stage_event`` table's primary key, not a timestamp or a
counter reset per run. That is what makes ``Last-Event-ID`` resume work: reconnecting
asks the database for ``id > last``, gets the rows it missed in order, and cannot
receive a row it already saw — because the cursor is durable state rather than the
publisher's memory (03 §J).

A stage that the pipeline has not built yet is reported as ``unavailable`` with the
reason, not as zero rows in ``complete``. The failure being prevented is a progress UI
that lies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

StageName = Literal["ingest", "graph", "score", "backtest", "warehouse"]
StageStatus = Literal["running", "complete", "failed", "unavailable"]

TERMINAL_STATUSES: Final = ("complete", "failed", "unavailable")
SSE_RETRY_MS: Final = 3000


class StageEvent(BaseModel):
    """One row of the pipeline ledger, as streamed and as stored."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(
        ge=1,
        description="Global, monotonic cursor. Sent as the SSE `id:` field and echoed "
        "back by clients as Last-Event-ID.",
    )
    run_id: str = Field(min_length=26, max_length=26, description="ULID (DEV-003).")
    stage: StageName
    status: StageStatus
    rows: int = Field(ge=0, description="Rows this stage committed. Measured, never estimated.")
    elapsed_ms: int = Field(ge=0, description="Wall-clock time this stage actually took.")
    detail: str | None = Field(
        default=None,
        description="Present for a failed or unavailable stage: what happened, in the "
        "words of the component that found out.",
    )
    emitted_at: datetime


class RunProgress(BaseModel):
    """The whole ledger for a run, so a client joining late sees the past too.

    A client that subscribes after a stage finished would otherwise watch a blank
    panel fill in from nowhere, which is the same lie as a fake progress bar.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: str
    events: list[StageEvent] = Field(default_factory=list)

    @property
    def last_event_id(self) -> int:
        return self.events[-1].id if self.events else 0

    @property
    def finished(self) -> bool:
        return self.state in {"complete", "failed", "superseded"}


class JobSubmission(BaseModel):
    """What queuing a job returns. The job id is the client's only handle."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    run_id: str
    queue: str
    position: int | None = None
    stages: list[str] = Field(default_factory=list)
    stream_path: str = Field(description="SSE path to follow for this run's stage events.")
    submitted_at: datetime
    trace_id: str | None = None


def to_sse_frame(event: StageEvent) -> dict[str, Any]:
    """The three lines one event becomes on the wire.

    ``id`` first, then ``event``, then ``data``: an EventSource client that receives a
    frame without an ``id`` cannot resume from it, so the id is emitted even for the
    first event and even when a proxy strips nothing.
    """
    return {
        "id": str(event.id),
        "event": f"{event.stage}.{event.status}",
        "data": event.model_dump_json(),
    }


__all__ = [
    "SSE_RETRY_MS",
    "TERMINAL_STATUSES",
    "JobSubmission",
    "RunProgress",
    "StageEvent",
    "StageName",
    "StageStatus",
    "to_sse_frame",
]
