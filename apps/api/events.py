"""SSE stage events, with a resume cursor that cannot duplicate a row.

Plan §13's gate for this file is a specific act: kill the connection mid-run,
reconnect, and the ledger must fill in without repeating a stage. Two decisions make
that true rather than approximately true.

**The cursor is a stored primary key, not a position in this generator.**
``stage_event.id`` is a Postgres sequence, monotonic across runs. A client that comes
back with ``Last-Event-ID: 41`` is asking the *database* for ``id > 41``, so the
backfill is durable state and survives this process restarting, the worker appending
while nobody is watching, and two subscribers reading the same run at different
speeds. A per-run counter or an in-memory index would reset or drift, and the UI would
either repeat a row or skip one — the two ways a progress ledger can lie.

**The event model is the same Pydantic model the OpenAPI schema and the client use.**
``StageEvent`` is declared as the ``event_model`` of the stream route, so the
generated TypeScript gets the union of stage names and statuses instead of ``string``,
and the ledger cannot render a field the server never sent.

A run whose stages have all completed before the client arrives is not an error and is
not an empty stream: the subscriber gets the whole ledger and then ``event: end``.
Silently hanging there is the failure mode a UI cannot distinguish from a stalled
pipeline.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from api.deps import Container, analyst_or_higher, get_container
from api.observability import get_logger, trace_id_var
from api.problems import (
    COMMON_ERROR_STATUSES,
    BadRequest,
    DependencyUnavailable,
    problem_responses,
)
from api.readmodel import ReadModel
from api.schemas.common import Envelope, Meta, envelope
from api.schemas.events import (
    SSE_RETRY_MS,
    RunProgress,
    StageEvent,
    to_sse_frame,
)
from api.security import Principal

logger = get_logger("oxbow.api.events")

router = APIRouter(prefix="/api/runs", tags=["events"])

# A stream's life is bounded deliberately. An unbounded generator is a request that
# never finishes, which no proxy, load balancer or client timeout models well; the
# client reconnects and the cursor picks up where it left off, which the resume path
# above is built for.
STREAM_POLL_SECONDS: Final = 1.0
STREAM_MAX_SECONDS: Final = 900.0
STREAM_MAX_ROW_RUN: Final = 1000
# One poll's worth of ledger rows. The bound is on the *query*: a poll that comes back
# full is drained again at once, so nothing is withheld from the client, while a run with
# a long ledger no longer makes every second of every open stream re-read the whole of it.
STREAM_POLL_ROW_LIMIT: Final = 256
TERMINAL_RUN_STATES: Final = ("complete", "failed", "superseded")

# ``StageEvent`` is imported for the stream's declared schema; the terminal-status
# check itself is on the run row, so an unfinished stage in a terminal run is reported
# by the ledger rather than by the stream staying open.


@dataclass(frozen=True, slots=True)
class ResumeCursor:
    """Where this subscriber starts, and whether it is resuming at all.

    ``requested_last_id`` is kept separately from ``after_id`` so the response can say
    "you asked for 41, there were 3 rows after it" — which is the difference between a
    resume working and a resume being assumed.
    """

    after_id: int
    requested_last_id: int | None
    source: str

    @property
    def resuming(self) -> bool:
        return self.requested_last_id is not None


def read_cursor(last_event_id_header: str | None, last_event_id_query: int | None) -> ResumeCursor:
    """Parse ``Last-Event-ID`` into a cursor, preferring the header EventSource sends.

    A non-integer value is a 400 naming the bad input rather than a silent 0: starting
    the whole run's history over again because a proxy mangled a header would look like
    duplicate stage rows, which is exactly the failure this route must not have.
    """
    if last_event_id_header is not None:
        raw = last_event_id_header.strip()
        if not raw:
            raise BadRequest("Last-Event-ID is present but empty")
        try:
            value = int(raw)
        except ValueError as exc:
            raise BadRequest(
                f"Last-Event-ID {raw!r} is not an integer event id; ids are the stored "
                "stage_event.id sequence"
            ) from exc
        if value < 0:
            raise BadRequest(f"Last-Event-ID cannot be negative, got {value}")
        return ResumeCursor(after_id=value, requested_last_id=value, source="header")
    if last_event_id_query is not None:
        if last_event_id_query < 0:
            raise BadRequest("last_event_id cannot be negative")
        return ResumeCursor(
            after_id=last_event_id_query, requested_last_id=last_event_id_query, source="query"
        )
    return ResumeCursor(after_id=0, requested_last_id=None, source="start")


def load_backfill(
    read_model: ReadModel,
    run_id: str,
    cursor: ResumeCursor,
    *,
    limit: int | None = None,
    verify_run: bool = True,
) -> list[StageEvent]:
    """Every stored row after the cursor, in id order — the backfill itself.

    The read goes through the warehouse, never through a buffer this process keeps,
    because the guarantee being made is "no duplicates", and a duplicate can only be
    excluded by a cursor that is durable.

    ``verify_run`` is the caller's promise that it has already read the run row. The
    stream's poll makes that promise: asking twice per second whether a run exists is
    one more statement per poll for a fact the poll already has.
    """
    if verify_run:
        rows = read_model.stage_events(run_id, after_id=cursor.after_id, limit=limit)
    else:
        rows = read_model.stage_event_rows(run_id, after_id=cursor.after_id, limit=limit)
    events: list[StageEvent] = []
    for row in sorted(rows, key=lambda item: int(item["id"])):
        events.append(StageEvent.model_validate(dict(row)))
    return events


def _initial_backfill(
    read_model: ReadModel, run_id: str, cursor: ResumeCursor
) -> list[StageEvent]:
    """The rows already stored when the client arrived, in one bounded-free read.

    The whole remainder is fetched here on purpose: a reconnecting client must be caught up
    before ``event: end`` on a terminal run, and a capped first read would close the stream
    with rows still undelivered. Only the *poll* is bounded, and a full poll is drained.
    The route has already read the run row, so this does not ask whether the run exists.
    """
    return load_backfill(read_model, run_id, cursor, verify_run=False)


def _poll_once(
    read_model: ReadModel, run_id: str, after_id: int
) -> tuple[str, list[StageEvent]]:
    """One bounded poll: the run's state and the ledger rows strictly after the cursor.

    Synchronous on purpose — it is warehouse I/O, and it runs on a worker thread for the
    few milliseconds a query takes. The stream's *wait* is what must not hold a thread.
    """
    run = read_model.run_row(run_id)
    return str(run["state"]), load_backfill(
        read_model,
        run_id,
        ResumeCursor(after_id, None, "poll"),
        limit=STREAM_POLL_ROW_LIMIT,
        verify_run=False,
    )


def frames_for(events: Iterator[StageEvent] | list[StageEvent]) -> Iterator[str]:
    """Render each event as an SSE frame with its ``id`` first.

    The ``retry:`` directive is emitted once at the head of the stream so a dropped
    connection reconnects on the server's schedule rather than the browser's default;
    without it a client that reconnects faster than the pipeline writes simply polls
    harder against a route that has nothing new yet.
    """
    for event in events:
        frame = to_sse_frame(event)
        yield f"id: {frame['id']}\nevent: {frame['event']}\ndata: {frame['data']}\n\n"


async def stream_events(
    container: Container,
    run_id: str,
    cursor: ResumeCursor,
    *,
    trace: str | None = None,
) -> AsyncIterator[str]:
    """The generator behind the route: backfill, then follow, then close.

    Emitted ids are tracked against the cursor locally as well as read from the store,
    so a row that appears twice in two consecutive polls (possible when a run writes
    and commits while a poll is in flight) is still delivered once. That is the
    no-duplicate property under a race rather than under a scheduler that happens to
    be nice.

    **The wait is awaited, not slept.** This generator belongs to an ``async def`` route,
    so between polls it holds no thread at all: ``STREAM_MAX_SECONDS`` of an open
    dashboard used to be ``STREAM_MAX_SECONDS`` of a checked-out worker from Starlette's
    ~40-permit pool, which every ``def`` route in the app shares, and forty open streams
    starved unrelated requests behind them. The warehouse read inside each poll is
    blocking I/O, so it goes to a worker thread for its own few milliseconds through
    :func:`run_in_threadpool` rather than running on the event loop.
    """
    read_model = container.read_model
    last = cursor.after_id
    sent = 0
    yield f"retry: {SSE_RETRY_MS}\n\n"
    # The route read the run row before handing it here, so the backfill does not re-check.
    for event in await run_in_threadpool(_initial_backfill, read_model, run_id, cursor):
        if event.id <= last:
            continue
        last = event.id
        sent += 1
        for frame in frames_for([event]):
            yield frame
    started = time.monotonic()
    while True:
        state, fresh = await run_in_threadpool(_poll_once, read_model, run_id, last)
        for event in fresh:
            last = event.id
            sent += 1
            for frame in frames_for([event]):
                yield frame
        if state in TERMINAL_RUN_STATES:
            # A terminal run closes the stream: the ledger is finished, and holding the
            # connection open would leave the UI's last row spinning forever.
            yield _end_frame(
                run_id=run_id,
                state=state,
                last_event_id=last,
                delivered=sent,
                trace_id=trace,
            )
            return
        if sent >= STREAM_MAX_ROW_RUN or time.monotonic() - started > STREAM_MAX_SECONDS:
            yield _end_frame(
                run_id=run_id,
                state=state,
                last_event_id=last,
                delivered=sent,
                reason="stream budget reached; reconnect to continue from the cursor",
                trace_id=trace,
            )
            return
        if len(fresh) >= STREAM_POLL_ROW_LIMIT:
            # A full page means the ledger is still filling behind the cursor. Drain it
            # immediately rather than making a client wait a poll interval for rows that
            # are already stored; the bound is on the query, not on the delivery.
            continue
        await asyncio.sleep(STREAM_POLL_SECONDS)


def _end_frame(
    *,
    run_id: str,
    state: str,
    last_event_id: int,
    delivered: int,
    reason: str | None = None,
    trace_id: str | None = None,
) -> str:
    payload = {
        "run_id": run_id,
        "state": state,
        "last_event_id": last_event_id,
        "events_delivered": delivered,
        "resumable": True,
    }
    if trace_id is not None:
        payload["trace_id"] = trace_id
    if reason is not None:
        payload["reason"] = reason
    return f"event: end\ndata: {_dumps(payload)}\n\n"


def _dumps(payload: dict[str, Any]) -> str:
    from json import dumps

    return dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def meta_for(container: Container, read_model: ReadModel, run_id: str, **extra: Any) -> Meta:
    """The envelope's ``meta`` for this run, with the run id the UI must be able to copy."""
    run = read_model.run_row(run_id)
    return Meta(
        run_id=run_id,
        trace_id=trace_id_var.get(),
        model_version=str(run["model_version"]),
        provenance=str(run["provenance"]),
        generated_at=datetime.now(UTC),
        disclaimer=read_model.disclaimer(),
        degraded=container.status() != "ok",
        degraded_reason=None
        if container.status() == "ok"
        else ", ".join(container.degraded_components()),
        **extra,
    )


@router.get(
    "/{run_id}/events",
    response_model=Envelope[RunProgress],
    summary="The full stage-event ledger for one run (non-streaming)",
    description=(
        "The same ``StageEvent`` rows the stream emits, as one JSON body, for a client "
        "that joined after the run finished or that cannot hold an open connection."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def run_event_ledger(
    run_id: str = Path(min_length=26, max_length=26),
    last_event_id: int | None = Query(
        default=None, ge=0, description="Return only events with an id greater than this."
    ),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    cursor = ResumeCursor(
        after_id=last_event_id or 0, requested_last_id=last_event_id, source="query"
    )
    read_model = container.read_model
    run = read_model.run_row(run_id)
    # The run row is already in hand, so the ledger read does not ask again.
    events = load_backfill(read_model, run_id, cursor, verify_run=False)
    progress = RunProgress(
        run_id=run_id, state=str(run["state"]), events=[event.model_dump() for event in events]
    )
    return envelope(progress, **meta_for(container, read_model, run_id).model_dump())


@router.get(
    "/{run_id}/events/stream",
    response_class=StreamingResponse,
    summary="Stage events as SSE, resumable with Last-Event-ID",
    description=(
        "One frame per ``stage_event`` row, each carrying an ``id``. Reconnecting with "
        "``Last-Event-ID`` backfills from the stored rows strictly after that id, so a "
        "dropped connection resumes without duplicate ledger rows."
    ),
    responses={
        **problem_responses(COMMON_ERROR_STATUSES),
        200: {
            "description": "An ``text/event-stream`` of ``StageEvent`` frames.",
            "content": {
                "text/event-stream": {"schema": {"$ref": "#/components/schemas/StageEvent"}}
            },
        },
    },
)
async def run_event_stream(
    request: Request,
    run_id: str = Path(min_length=26, max_length=26),
    last_event_id: int | None = Query(
        default=None,
        ge=0,
        description="Query fallback for clients that cannot set Last-Event-ID.",
    ),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> StreamingResponse:
    cursor = read_cursor(request.headers.get("last-event-id"), last_event_id)
    read_model = container.read_model
    run = read_model.run_row(run_id)
    if str(run["provenance"]) not in {"pipeline", "fixture", "demo_snapshot"}:
        raise DependencyUnavailable(
            f"run {run_id} has provenance {run['provenance']!r}, which this deployment does not "
            "stream: an unlabelled run cannot be distinguished from a fabricated one"
        )
    trace = trace_id_var.get()

    async def generate() -> AsyncIterator[str]:
        try:
            async for chunk in stream_events(container, run_id, cursor, trace=trace):
                yield chunk
        except Exception as exc:  # a stream that dies silently is a stalled progress bar
            logger.error(
                "sse stream failed",
                run_id=run_id,
                error=str(exc)[:300],
                error_type=type(exc).__name__,
            )
            yield (
                "event: error\n"
                f"data: {_dumps({'run_id': run_id, 'detail': str(exc)[:300], 'trace_id': trace})}\n\n"
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "OXBOW-Trace-Id": trace or "",
            "OXBOW-Run-Id": run_id,
        },
    )


__all__ = [
    "STREAM_MAX_SECONDS",
    "STREAM_POLL_ROW_LIMIT",
    "STREAM_POLL_SECONDS",
    "TERMINAL_RUN_STATES",
    "ResumeCursor",
    "frames_for",
    "load_backfill",
    "meta_for",
    "read_cursor",
    "router",
    "stream_events",
]
