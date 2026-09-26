"""Null warehouse: the pipeline's handoff written to ``out/warehouse/`` (plan §13).

This is the class that makes "the demo runs with nothing else up" true rather than
aspirational: a completed pipeline can hand its read model over as Parquet (or JSON
lines when there are no rows to infer a schema from) and the API's warehouse-backed
routes can serve from the same shapes.

It enforces the same three conventions the Postgres sink does — a completed run
takes no further writes, ``*_minor`` is always an integer, ``txn_id`` is
corpus-namespaced — because a null adapter that skipped the checks would let a bad
row through in the demo and only fail in production, which is the inverse of what a
null adapter is for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from oxbow.adapters.io import (
    append_jsonl,
    dumps,
    read_jsonl,
    resolve_out_root,
    write_bytes_atomic,
    write_json_atomic,
    write_rows_tabular,
)
from oxbow.ports.warehouse import (
    IMMUTABLE_RUN_STATES,
    RunState,
    WarehouseTableError,
    assert_money_is_integer_minor,
    assert_run_id,
    assert_txn_id_namespaced,
    assert_writable_table,
)

_PORT_DIRNAME = "warehouse"
_RUNS_FILE = "runs.jsonl"
_STAGE_EVENTS_FILE = "stage_events.jsonl"


class NullWarehouse:
    """A file-backed warehouse rooted at ``out/warehouse/``."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = resolve_out_root(root) / _PORT_DIRNAME
        self._root.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, dict[str, Any]] = {
            str(record["run_id"]): record for record in read_jsonl(self._root / _RUNS_FILE)
        }
        self._events: list[dict[str, Any]] = list(read_jsonl(self._root / _STAGE_EVENTS_FILE))

    @property
    def sink_id(self) -> str:
        return "null"

    @property
    def base_dir(self) -> Path:
        return self._root

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
        if run_id in self._runs:
            raise WarehouseTableError(f"run {run_id} is already open in this warehouse")
        self._runs[run_id] = {
            "run_id": run_id,
            "state": RunState.RUNNING.value,
            "seed": seed,
            "timezone": timezone,
            "provenance": provenance,
            "config_hash": config_hash,
            "model_version": model_version,
            "dataset_ref": dataset_ref,
            "created_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "error": None,
        }
        self._persist_runs()

    def run_state(self, run_id: str) -> RunState:
        record = self._runs.get(run_id)
        if record is None:
            raise WarehouseTableError(f"unknown run {run_id!r}: nothing was opened under it")
        return RunState(str(record["state"]))

    def write(self, table: str, run_id: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Land rows for one table and return the count written."""
        assert_writable_table(table)
        assert_run_id(run_id)
        state = self.run_state(run_id)
        if state in IMMUTABLE_RUN_STATES:
            raise WarehouseTableError(
                f"run {run_id} is {state.value}: a completed run is immutable and rows may not "
                "be added to it. Rescoring creates a new run (plan §13)."
            )
        normalised: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["run_id"] = run_id
            assert_money_is_integer_minor(table, record)
            assert_txn_id_namespaced(table, record)
            normalised.append(record)
        path = write_rows_tabular(self._root / table, f"run={run_id}", normalised)
        write_json_atomic(
            self._root / table / f"run={run_id}.manifest.json",
            {
                "table": table,
                "run_id": run_id,
                "rows": len(normalised),
                "artifact": path.name,
                "written_at": datetime.now(UTC).isoformat(),
            },
        )
        return len(normalised)

    def read(self, table: str, run_id: str, *, limit: int = 1000) -> Sequence[Mapping[str, Any]]:
        assert_writable_table(table)
        rows: list[Mapping[str, Any]] = []
        for record in read_jsonl(self._root / table / f"run={run_id}.jsonl"):
            rows.append(record)
            if len(rows) >= limit:
                break
        if rows:
            return rows
        parquet = self._root / table / f"run={run_id}.parquet"
        if not parquet.is_file():
            return []
        return list(pq.read_table(parquet).to_pylist())[:limit]

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
        """Append one ledger event and return its stable, monotonic id.

        The id is the position in the JSONL file, which is why resume cannot
        duplicate rows: a client reconnecting with ``Last-Event-ID=41`` is told
        "everything from 42", and 42 is a different line every time it is asked.
        """
        assert_run_id(run_id)
        event = {
            "id": len(self._events) + 1,
            "run_id": run_id,
            "stage": stage,
            "status": status,
            "rows": rows,
            "elapsed_ms": elapsed_ms,
            "detail": detail,
            "emitted_at": datetime.now(UTC).isoformat(),
        }
        self._events.append(event)
        append_jsonl(self._root / _STAGE_EVENTS_FILE, (event,))
        return int(event["id"])

    def stage_events(self, run_id: str, *, after_id: int = 0) -> Sequence[Mapping[str, Any]]:
        return [
            dict(event)
            for event in self._events
            if event["run_id"] == run_id and int(event["id"]) > after_id
        ]

    def complete_run(self, run_id: str, state: RunState, *, error: str | None = None) -> None:
        record = self._runs.get(run_id)
        if record is None:
            raise WarehouseTableError(f"cannot complete unknown run {run_id!r}")
        if str(record["state"]) in {RunState.COMPLETE.value, RunState.SUPERSEDED.value}:
            raise WarehouseTableError(
                f"run {run_id} is already {record['state']}; a completed run does not change state"
            )
        record["state"] = state.value
        record["finished_at"] = datetime.now(UTC).isoformat()
        record["error"] = error
        self._persist_runs()

    def _persist_runs(self) -> None:
        """Rewrite the run ledger whole.

        Rewritten rather than appended because a run's ``state`` is the one field
        that legitimately changes after its row exists; everything else in it is
        frozen, which is the same rule the ``run`` table enforces with a trigger.
        """
        payload = "\n".join(dumps(record) for record in self._runs.values()) + "\n"
        write_bytes_atomic(self._root / _RUNS_FILE, payload.encode("utf-8"))


__all__ = ["NullWarehouse"]
