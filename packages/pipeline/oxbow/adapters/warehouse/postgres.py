"""Postgres warehouse sink: the pipeline's handoff into the API's read model.

This is ``02 §B seam 1`` as an executable thing: the pipeline writes rows here, the
API reads the same tables, and neither calls the other. Two properties are enforced
rather than trusted:

* the port-level conventions (a completed run takes no writes, ``*_minor`` is an
  integer, ``txn_id`` is namespaced) are checked in Python **and** backed by the
  triggers from migration 0002, so the rule survives someone writing SQL by hand;
* the column set comes from the SQLAlchemy metadata, so a row with an unknown key
  fails at the database rather than being silently dropped by an ORM that only maps
  what it knows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ulid import ULID

from oxbow.adapters.warehouse.models import Base, Run, StageEvent
from oxbow.ports.warehouse import (
    IMMUTABLE_RUN_STATES,
    RunState,
    WarehouseTableError,
    assert_money_is_integer_minor,
    assert_run_id,
    assert_txn_id_namespaced,
    assert_writable_table,
)

_TABLES = Base.metadata.tables


class PostgresWarehouseSink:
    """Writes and reads the warehouse tables through a caller-owned session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def sink_id(self) -> str:
        return "postgres"

    def open_run(
        self,
        run_id: str,
        *,
        seed: int,
        timezone: str,
        provenance: str,
        config_hash: str,
        model_version: str,
        dataset_ref: str | None = None,
    ) -> None:
        assert_run_id(run_id)
        if self._session.get(Run, run_id) is not None:
            raise WarehouseTableError(f"run {run_id} already exists")
        self._session.add(
            Run(
                run_id=run_id,
                state=RunState.RUNNING.value,
                seed=seed,
                timezone=timezone,
                provenance=provenance,
                config_hash=config_hash,
                model_version=model_version,
                dataset_ref=dataset_ref,
            )
        )
        self._session.flush()

    def run_state(self, run_id: str) -> RunState:
        assert_run_id(run_id)
        state = self._session.scalar(select(Run.state).where(Run.run_id == run_id))
        if state is None:
            raise WarehouseTableError(f"unknown run {run_id!r}: nothing was opened under it")
        return RunState(str(state))

    def write(self, table: str, run_id: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Bulk-insert rows for one run, refusing a closed run or a bad row."""
        assert_writable_table(table)
        assert_run_id(run_id)
        state = self.run_state(run_id)
        if state in IMMUTABLE_RUN_STATES:
            raise WarehouseTableError(
                f"run {run_id} is {state.value}: a completed run is immutable, and rescoring "
                "creates a new run (plan §13). Migration 0002 enforces this in the database too."
            )
        target = _TABLES[table]
        records: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["run_id"] = run_id
            assert_money_is_integer_minor(table, record)
            assert_txn_id_namespaced(table, record)
            records.append(record)
        if not records:
            return 0
        try:
            with self._session.begin_nested():
                self._session.execute(insert(target), records)
                self._session.flush()
        except IntegrityError as exc:
            raise WarehouseTableError(
                f"write to {table} for run {run_id} was rejected by the database: {exc.orig}"
            ) from exc
        return len(records)

    def read(self, table: str, run_id: str, *, limit: int = 1000) -> Sequence[Mapping[str, Any]]:
        assert_writable_table(table)
        assert_run_id(run_id)
        target = _TABLES[table]
        result = self._session.execute(
            select(target).where(target.c.run_id == run_id).limit(limit)
        )
        return [dict(mapping) for mapping in result.mappings().all()]

    def record_stage_event(
        self,
        run_id: str,
        *,
        stage: str,
        status: str,
        rows: int,
        elapsed_ms: int,
        detail: str | None = None,
    ) -> int:
        """Persist one ledger row and return its global sequence id.

        ``stage_event.id`` is a Postgres sequence, so ids are monotonic across runs
        and a client reconnecting with ``Last-Event-ID`` names an exact position in
        durable state. That is the whole resume mechanism, and it is why the id is
        not a per-run counter (plan §13).
        """
        assert_run_id(run_id)
        event = StageEvent(
            id=None,
            run_id=run_id,
            stage=stage,
            status=status,
            rows=rows,
            elapsed_ms=elapsed_ms,
            detail=detail,
            emitted_at=datetime.now(UTC),
        )
        self._session.add(event)
        self._session.flush()
        return int(event.id)

    def stage_events(self, run_id: str, *, after_id: int = 0) -> Sequence[Mapping[str, Any]]:
        assert_run_id(run_id)
        rows = self._session.execute(
            select(StageEvent)
            .where(StageEvent.run_id == run_id, StageEvent.id > after_id)
            .order_by(StageEvent.id)
        ).scalars()
        return [
            {
                "id": int(event.id),
                "run_id": event.run_id,
                "stage": event.stage,
                "status": event.status,
                "rows": int(event.rows),
                "elapsed_ms": int(event.elapsed_ms),
                "detail": event.detail,
                "emitted_at": event.emitted_at,
            }
            for event in rows
        ]

    def complete_run(self, run_id: str, state: RunState, *, error: str | None = None) -> None:
        assert_run_id(run_id)
        run = self._session.get(Run, run_id)
        if run is None:
            raise WarehouseTableError(f"cannot complete unknown run {run_id!r}")
        run.state = state.value
        run.finished_at = datetime.now(UTC)
        run.error = error
        self._session.flush()


def new_run_id() -> str:
    """A fresh ULID in the canonical text form this schema stores (DEV-003)."""
    return str(ULID()).upper()


__all__ = ["PostgresWarehouseSink", "new_run_id"]
