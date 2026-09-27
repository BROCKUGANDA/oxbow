"""The SSE stream must not be a sleeping thread, and its poll must be a bounded query.

The audit's finding: `run_event_stream` is a plain `def`, so Starlette iterates its
generator on a worker thread from the shared ~40-permit anyio pool. The generator's wait
is `time.sleep(1)` inside a `while True` that can live for `STREAM_MAX_SECONDS = 900`, so
one open dashboard stream is one blocked thread for fifteen minutes, and forty of them
starve every unrelated request in the process. Each poll also read the run row twice and
pulled the run's *entire* stage ledger to filter one cursor's worth of it in Python.

The assertions are structural, not temporal: the route is a coroutine function, the
generator is an async generator, the wait is an awaited sleep rather than `time.sleep`,
every poll read carries a `limit`, and the run row is read once per poll. Nothing here
measures wall time, because a timing gate on a shared machine is not a gate.

The SSE frame *shape* is asserted literally — `retry:`, then `id:`/`event:`/`data:` per
row, then `event: end`. Frames are legitimately outside the `{data, meta}` envelope rule
(03 §A, `tests/integration/test_envelope_doctrine.py`); this test exists so a performance
fix cannot quietly change the wire format the client parses.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import api.events as events  # noqa: E402
from api.schemas.events import SSE_RETRY_MS  # noqa: E402

RUN_ID = ("01EVT" + "0" * 30)[:26]
EMITTED = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _row(event_id: int, stage: str, status: str, *, available_from: int) -> dict[str, Any]:
    """A stored `stage_event` row, plus when the fake starts to show it.

    `available_from` is fixture metadata, not a column: it is stripped before the row is
    handed back, because ``StageEvent`` is ``extra="forbid"`` and the read model would
    otherwise be validating a field the warehouse never writes.
    """
    return {
        "id": event_id,
        "run_id": RUN_ID,
        "stage": stage,
        "status": status,
        "rows": 10 * event_id,
        "elapsed_ms": 100 * event_id,
        "detail": None,
        "emitted_at": EMITTED,
        "available_from": available_from,
    }


class _FakeReadModel:
    """The two reads the stream makes, with the cursor honoured and the poll bounded.

    `available_from` is the poll index at which a row becomes visible, which is how a
    test writes a ledger that fills in as the run proceeds rather than one that is
    already complete.
    """

    def __init__(self, *, rows: list[dict[str, Any]], states: list[str]) -> None:
        self._rows = rows
        self._states = states
        self.run_row_calls = 0
        self.ledger_calls: list[dict[str, Any]] = []

    def run_row(self, run_id: str) -> dict[str, Any]:
        assert run_id == RUN_ID
        index = self.run_row_calls
        self.run_row_calls += 1
        state = self._states[index] if index < len(self._states) else self._states[-1]
        return {
            "run_id": run_id,
            "state": state,
            "model_version": "shape-1",
            "provenance": "pipeline",
        }

    def _read(self, *, after_id: int, limit: int | None, **kwargs: Any) -> list[dict[str, Any]]:
        self.ledger_calls.append({"after_id": after_id, "limit": limit, **kwargs})
        visible = [
            {key: value for key, value in row.items() if key != "available_from"}
            for row in self._rows
            if int(row["id"]) > after_id and int(row["available_from"]) <= self.run_row_calls
        ]
        visible.sort(key=lambda row: int(row["id"]))
        return visible[:limit] if limit is not None else visible

    def stage_events(
        self, run_id: str, *, after_id: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        assert run_id == RUN_ID
        return self._read(after_id=after_id, limit=limit)

    def stage_event_rows(
        self, run_id: str, *, after_id: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """The bounded poll read: the same rows, without re-checking the run exists."""
        assert run_id == RUN_ID
        return self._read(after_id=after_id, limit=limit)


class _Container:
    def __init__(self, read_model: _FakeReadModel) -> None:
        self.read_model = read_model


@pytest.fixture()
def sleeps(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Record which sleep the stream uses, and refuse the blocking one outright."""
    record = {"awaited": 0, "blocked": 0}
    real_sleep = asyncio.sleep

    async def _counting_sleep(delay: float, *args: Any, **kwargs: Any) -> Any:
        record["awaited"] += 1
        return await real_sleep(0, *args, **kwargs)

    def _blocked(_seconds: float) -> None:
        record["blocked"] += 1
        raise AssertionError(
            f"the stream blocked a worker thread with time.sleep({_seconds}); forty open "
            "streams would consume forty permits from the shared anyio pool"
        )

    monkeypatch.setattr(
        getattr(events, "asyncio", None)
        or pytest.fail(
            "api.events does not import asyncio, so the stream's wait cannot be awaited "
            "and it holds a worker thread between polls"
        ),
        "sleep",
        _counting_sleep,
    )
    monkeypatch.setattr(events.time, "sleep", _blocked)
    monkeypatch.setattr(events, "STREAM_POLL_SECONDS", 0)
    return record


async def _drain(stream: Any) -> str:
    return "".join([chunk async for chunk in stream])


def _scenario() -> _FakeReadModel:
    """A ledger that fills in as the run proceeds, then reaches `complete`."""
    return _FakeReadModel(
        rows=[
            _row(1, "ingest", "complete", available_from=0),
            _row(2, "graph", "complete", available_from=1),
            _row(3, "score", "running", available_from=2),
        ],
        states=["running", "running", "complete"],
    )


def test_the_stream_route_is_a_coroutine_function() -> None:
    """`def` puts the generator on a threadpool worker for the life of the stream.

    Every other router in this app is `def` and that is correct for a short request; a
    response that lasts `STREAM_MAX_SECONDS` is the one route that cannot be.
    """
    assert inspect.iscoroutinefunction(events.run_event_stream), (
        "run_event_stream is a plain def: Starlette iterates it on an anyio worker "
        "thread, so each open dashboard stream holds a thread for up to 900 seconds"
    )
    assert inspect.isasyncgenfunction(events.stream_events), (
        "stream_events is not an async generator, so its wait cannot be awaited and it "
        "keeps the worker thread between polls"
    )


def test_the_poll_sleeps_by_awaiting_not_by_blocking(sleeps: dict[str, int]) -> None:
    record = sleeps
    read_model = _scenario()
    stream = events.stream_events(
        _Container(read_model), RUN_ID, events.ResumeCursor(0, None, "start"), trace="t-1"
    )
    body = asyncio.run(_drain(stream))
    assert body.startswith(f"retry: {SSE_RETRY_MS}\n\n"), "the reconnect directive is gone"
    assert record["blocked"] == 0
    assert (
        record["awaited"] >= 1
    ), "the stream never awaited a sleep, so it polls as fast as the loop allows"


def test_every_poll_reads_the_ledger_with_a_limit(sleeps: dict[str, int]) -> None:
    """An unbounded ledger read per poll is the other half of the finding.

    The one read allowed to be unbounded is the first: a client resuming into a terminal
    run must be caught up before ``event: end``, and a capped catch-up would close the
    stream with rows still undelivered. A poll that comes back full is drained again at
    once, so the bound is on the query, never on what the client receives.
    """
    read_model = _scenario()
    stream = events.stream_events(
        _Container(read_model), RUN_ID, events.ResumeCursor(0, None, "start"), trace="t-1"
    )
    asyncio.run(_drain(stream))
    reads = read_model.ledger_calls
    assert reads, "the stream never read the ledger at all"
    unbounded = [index for index, call in enumerate(reads) if not call["limit"]]
    assert unbounded in ([], [0]), (
        f"ledger reads {unbounded} of {len(reads)} ran with no row limit; only the initial "
        f"backfill (index 0) may be unbounded, so a long run's ledger is re-read whole "
        f"every second: {reads}"
    )
    polls = reads[1:] if unbounded == [0] else reads
    assert polls, "the stream never polled: nothing here proved the poll is bounded"
    assert all(call["limit"] for call in polls), polls


def test_the_run_row_is_read_once_per_poll_not_twice(sleeps: dict[str, int]) -> None:
    """`stage_events` re-checked the run the loop had just read, doubling the statements."""
    read_model = _scenario()
    stream = events.stream_events(
        _Container(read_model), RUN_ID, events.ResumeCursor(0, None, "start"), trace="t-1"
    )
    asyncio.run(_drain(stream))
    polls = len(read_model.ledger_calls)
    assert read_model.run_row_calls <= polls, (
        f"{read_model.run_row_calls} run-row reads for {polls} ledger reads: each poll is "
        "asking whether the run exists more than once"
    )


def test_the_served_frames_are_unchanged(sleeps: dict[str, int]) -> None:
    """The wire format the client parses is the contract; async is an implementation detail."""
    read_model = _scenario()
    stream = events.stream_events(
        _Container(read_model), RUN_ID, events.ResumeCursor(0, None, "start"), trace="t-1"
    )
    body = asyncio.run(_drain(stream))
    expected = (
        f"retry: {SSE_RETRY_MS}\n\n"
        + "id: 1\nevent: ingest.complete\ndata: "
        + _json(1, "ingest", "complete")
        + "\n\n"
        + "id: 2\nevent: graph.complete\ndata: "
        + _json(2, "graph", "complete")
        + "\n\n"
        + "id: 3\nevent: score.running\ndata: "
        + _json(3, "score", "running")
        + "\n\n"
        + "event: end\ndata: {"
        + f'"events_delivered":3,"last_event_id":3,"resumable":true,"run_id":"{RUN_ID}",'
        + '"state":"complete","trace_id":"t-1"'
        + "}\n\n"
    )
    assert body == expected, f"frame shape changed:\n{body!r}\n!=\n{expected!r}"


def _json(event_id: int, stage: str, status: str) -> str:
    # Percent formatting on purpose: the payload is JSON, and an f-string would have to double
    # every brace in it, which hides the frame shape this test exists to pin.
    return (
        '{"id":%d,"run_id":"%s","stage":"%s","status":"%s","rows":%d,"elapsed_ms":%d,'  # noqa: UP031
        '"detail":null,"emitted_at":"2026-01-15T12:00:00Z"}'
        % (event_id, RUN_ID, stage, status, 10 * event_id, 100 * event_id)
    )


def test_resume_from_a_cursor_delivers_only_later_rows(sleeps: dict[str, int]) -> None:
    """The cursor rule the frame format buys: a reconnect repeats nothing."""
    read_model = _scenario()
    stream = events.stream_events(
        _Container(read_model), RUN_ID, events.ResumeCursor(2, 2, "header"), trace="t-2"
    )
    body = asyncio.run(_drain(stream))
    assert "id: 1\n" not in body and "id: 2\n" not in body, body
    assert "id: 3\n" in body
