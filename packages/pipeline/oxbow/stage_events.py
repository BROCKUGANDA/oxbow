"""The one stage-event emitter, shared by the CLI and the API's ledger.

Plan §13's requirement is that the streamed shape, the stored shape and the OpenAPI
shape are one thing. They already are, in two places that this module joins rather
than duplicates:

* ``apps/api/schemas/events.py::StageEvent`` declares the row the UI renders —
  ``id, run_id, stage, status, rows, elapsed_ms, detail, emitted_at``;
* ``WarehouseSink.record_stage_event`` (``oxbow/ports/warehouse.py``) is the pipeline's
  existing producer of exactly that row, implemented by ``NullWarehouse`` (append-only
  ``out/warehouse/stage_events.jsonl``) and by the Postgres adapter's ``stage_event``
  table, whose CHECK constraints on ``status``/``rows``/``elapsed_ms`` are the same
  contract the schema declares.

So this module adds **no event type**. It owns the three things a stage body should not
have to write by hand each time: the monotonic id (minted by the sink, never here), the
elapsed measurement (wall clock of the block that actually ran), and the terminal-status
discipline — a stage that could not run is ``unavailable`` with a reason, never
``complete`` with zero rows, which is the difference between an honest ledger and a
progress bar that lies.

The ``id`` is the sink's, deliberately. A per-run counter would collide across runs and
break ``Last-Event-ID`` resume (DEV-003/C3 is why the run id is a ULID for the same
reason), so an event's identity is whatever the durable store says it is.

Deterministic serialisation goes through :func:`oxbow.adapters.io.dumps` — sorted keys,
no insignificant whitespace, the one encoder the audit chain digests. Nothing here
re-implements it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final

from oxbow.adapters.io import dumps
from oxbow.ports.warehouse import (
    RunState,
    WarehouseSink,
    WarehouseTableError,
    assert_run_id,
)

RUNNING: Final = "running"
COMPLETE: Final = "complete"
FAILED: Final = "failed"
UNAVAILABLE: Final = "unavailable"

TERMINAL_STAGE_STATUSES: Final = (COMPLETE, FAILED, UNAVAILABLE)

DEFAULT_PROVENANCE: Final = "pipeline"


class StageEventError(RuntimeError):
    """An emitted event violated the ledger contract, or the sink returned no row."""


@lru_cache(maxsize=1)
def stage_vocabulary() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(STAGE_NAMES, STAGE_STATUSES)`` from the single DDL declaration.

    Imported lazily and cached rather than restated here. The tuples live in
    :mod:`oxbow.adapters.warehouse.models` and are CHECK constraints on ``stage_event``,
    so a third copy in this module would be a list that can disagree with the database
    that enforces it. The lazy import also keeps SQLAlchemy out of ``oxbow --help``.
    """
    from oxbow.adapters.warehouse.models import STAGE_NAMES, STAGE_STATUSES

    return tuple(STAGE_NAMES), tuple(STAGE_STATUSES)


def assert_stage_status(stage: str, status: str) -> None:
    """Refuse a stage or status outside the ledger's own vocabulary."""
    stages, statuses = stage_vocabulary()
    if stage not in stages:
        raise StageEventError(
            f"stage {stage!r} is not one of the declared ledger stages {list(stages)}. "
            "A stage the schema does not know is a stage the UI cannot render."
        )
    if status not in statuses:
        raise StageEventError(
            f"status {status!r} is not one of {list(statuses)}; a status outside the "
            "contract would pass the sink's CHECK constraint only by accident."
        )


@dataclass(slots=True)
class StageHandle:
    """What a running stage writes into while it works: measured rows, and why not.

    ``rows`` is only ever assigned a number the stage counted. Two refusals are stated
    rather than inferred from a zero, because they are different claims the ledger has to
    keep apart:

    * :meth:`mark_unavailable` — this stage did not run, because something it depends on
      does not exist yet. The plan §13 schema names this status for exactly that case.
    * :meth:`mark_failed` — this stage ran and measured a failure (a quarantine, an empty
      batch, a hash disagreement, an unwired seam partway through its scope).
    """

    stage: str
    rows: int = 0
    detail: str | None = None
    unavailable_reason: str | None = None
    failed_reason: str | None = None

    def add_rows(self, count: int) -> None:
        if count < 0:
            raise StageEventError(f"{self.stage}: cannot add {count} rows to a ledger count")
        self.rows += int(count)

    def mark_unavailable(self, reason: str) -> None:
        """Say why this stage did not run, in the words the operator needs."""
        if not reason.strip():
            raise StageEventError(f"{self.stage}: an unavailable stage must name a reason")
        self.unavailable_reason = reason

    def mark_failed(self, reason: str) -> None:
        """Say what this stage measured that makes the run red."""
        if not reason.strip():
            raise StageEventError(f"{self.stage}: a failed stage must name what failed")
        self.failed_reason = reason

    @property
    def refused(self) -> bool:
        return self.unavailable_reason is not None

    @property
    def failed(self) -> bool:
        return self.failed_reason is not None


@dataclass(slots=True)
class StageEventEmitter:
    """Produces the ledger rows for one run and, when streaming, renders each as SSE.

    One emitter per run: the run id is fixed at construction, every event is written
    through the sink before it is echoed, and the in-memory list is what the CLI prints
    at the end. The sink is the durable copy — the API reads its rows, not this list.
    """

    sink: WarehouseSink
    run_id: str
    stream: bool = True
    echo: Callable[[str], None] = print
    events: list[dict[str, Any]] = field(default_factory=list)
    _opened: bool = False

    def __post_init__(self) -> None:
        self.run_id = assert_run_id(self.run_id)

    # --- run lifecycle ----------------------------------------------------

    def ensure_run_open(
        self,
        *,
        seed: int,
        timezone: str,
        config_hash: str,
        model_version: str,
        provenance: str = DEFAULT_PROVENANCE,
        dataset_ref: str | None = None,
    ) -> str:
        """Open the run, or report that this ``run_id`` is being resumed.

        Returns ``"opened"`` or ``"resumed"``. Resume is the whole point of a durable
        run id: ``--run-id`` on a second invocation must add rows to the same ledger
        rather than silently starting a second run nobody asked for.
        """
        if self._opened:
            return "opened"
        try:
            state = self.sink.run_state(self.run_id)
        except WarehouseTableError:
            self.sink.open_run(
                self.run_id,
                seed=seed,
                timezone=timezone,
                provenance=provenance,
                config_hash=config_hash,
                model_version=model_version,
                dataset_ref=dataset_ref,
            )
            self._opened = True
            return "opened"
        if state in (RunState.RUNNING, RunState.FAILED):
            self.echo(f"[{self.run_id[:8]}] resuming run in state {state.value}")
            self._opened = True
            return "resumed"
        raise StageEventError(
            f"run {self.run_id} is {state.value} and its ledger is immutable; a completed "
            "run is never rewritten. Re-run under a new run id (plan §13: rescoring is a "
            "new run)."
        )

    def completed_event(self, stage: str) -> Mapping[str, Any] | None:
        """The last ``complete`` row for ``stage`` under this run, if the ledger has one.

        This is the resume test a stage body asks before doing the work: the artifact
        for a stage that already reported its committed row count exists, and rebuilding
        it silently is a second run pretending to be one.
        """
        for event in reversed(list(self.sink.stage_events(self.run_id))):
            if event["stage"] == stage and event["status"] == COMPLETE:
                return event
        return None

    # --- emitting ---------------------------------------------------------

    def emit(
        self,
        stage: str,
        status: str,
        *,
        rows: int,
        elapsed_ms: int,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Write one ledger row through the sink and return the stored row.

        The row is read back from the sink rather than assembled here, because ``id`` and
        ``emitted_at`` are the sink's to assign; a locally-fabricated copy would be a
        second truth about the same event.
        """
        assert_stage_status(stage, status)
        if rows < 0:
            raise StageEventError(
                f"{stage}: rows={rows} is negative; the ledger's rows are counted"
            )
        if elapsed_ms < 0:
            raise StageEventError(f"{stage}: elapsed_ms={elapsed_ms} is negative")
        event_id = self.sink.record_stage_event(
            self.run_id,
            stage=stage,
            status=status,
            rows=int(rows),
            elapsed_ms=int(elapsed_ms),
            detail=detail,
        )
        stored = [
            event for event in self.sink.stage_events(self.run_id) if int(event["id"]) == event_id
        ]
        if not stored:
            raise StageEventError(
                f"{stage}.{status} was recorded as id {event_id} but the sink returned no such "
                "row; a ledger event that cannot be read back is not a recorded event"
            )
        event = dict(stored[0])
        self.events.append(event)
        if self.stream:
            self.echo(sse_frame(event))
        return event

    @contextmanager
    def stage(self, stage: str) -> Iterator[StageHandle]:
        """Emit ``running``, then exactly one terminal status for the block.

        An exception becomes ``failed`` with the exception's own message and is
        re-raised: the stage that blew up must be visible in the ledger, not just in a
        traceback the operator scrolled past.
        """
        handle = StageHandle(stage=stage)
        self.emit(stage, RUNNING, rows=0, elapsed_ms=0)
        started = time.monotonic()
        try:
            yield handle
        except BaseException as exc:
            elapsed_ms = int(round((time.monotonic() - started) * 1000))
            detail = f"{type(exc).__name__}: {exc}"
            self.emit(stage, FAILED, rows=handle.rows, elapsed_ms=elapsed_ms, detail=detail[:900])
            raise
        elapsed_ms = int(round((time.monotonic() - started) * 1000))
        if handle.refused:
            reason = handle.unavailable_reason or "no reason recorded"
            self.emit(
                stage, UNAVAILABLE, rows=handle.rows, elapsed_ms=elapsed_ms, detail=reason[:900]
            )
            return
        if handle.failed:
            reason = handle.failed_reason or "the stage reported a failure without a reason"
            self.emit(stage, FAILED, rows=handle.rows, elapsed_ms=elapsed_ms, detail=reason[:900])
            return
        self.emit(
            stage,
            COMPLETE,
            rows=handle.rows,
            elapsed_ms=elapsed_ms,
            detail=handle.detail,
        )

    def finish(self, state: RunState, *, error: str | None = None) -> None:
        """Move the run to its terminal state so the SSE stream can close."""
        self.sink.complete_run(self.run_id, state, error=error)


def sse_frame(event: Mapping[str, Any]) -> str:
    """One event as the three lines an EventSource client expects.

    ``id`` first, then ``event``, then ``data`` — the same order
    ``apps/api/schemas/events.py::to_sse_frame`` documents and the same body
    :func:`oxbow.adapters.io.dumps` produces for the audit chain. A frame without an
    ``id`` cannot be resumed from, which is the whole reason the first event carries one.
    """
    stage = str(event["stage"])
    status = str(event["status"])
    return f"id: {event['id']}\n" f"event: {stage}.{status}\n" f"data: {dumps(dict(event))}\n\n"


def elapsed_ms_since(started_monotonic: float) -> int:
    """Milliseconds spent since ``time.monotonic()`` was read.

    A helper rather than an inline expression because every stage prints its own elapsed
    time *and* emits it, and the two must be the same number read from one clock.
    """
    return int(round((time.monotonic() - started_monotonic) * 1000))


__all__ = [
    "COMPLETE",
    "DEFAULT_PROVENANCE",
    "FAILED",
    "RUNNING",
    "TERMINAL_STAGE_STATUSES",
    "UNAVAILABLE",
    "StageEventEmitter",
    "StageEventError",
    "StageHandle",
    "assert_stage_status",
    "elapsed_ms_since",
    "sse_frame",
    "stage_vocabulary",
]
