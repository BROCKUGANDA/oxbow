"""The WarehouseSink port: the pipeline's only handoff to the API (02 §B seam 1).

Postgres is the single handoff between the two halves of the system: the pipeline
computes and writes, the API reads and never recomputes. That division only holds
if the write side refuses to write nonsense, so this port enforces the three
identity and money conventions that DECISIONS.md fixed and that are otherwise
maintained by memory:

* ``run_id`` is a 26-character ULID stored as text, never a native uuid
  (DEV-003/C3) — because SSE resume and "rescoring creates a new run" both need a
  key that sorts by time on its own;
* ``txn_id`` is corpus-namespaced text, ``paysim:12345`` (DEV-004/C4) — because a
  bigint primary key would silently merge two corpora's row 41;
* every money column ends in ``_minor`` and holds an integer (DEV-005/C5) —
  because ``0.1 + 0.2`` summed over six million rows is a reconciliation failure
  that stays invisible until the column is quoted in a packet.

The convention check is on the *name*, deliberately: it catches a new money column
in a new table without anyone editing a schema list, which is how a float money
field actually gets introduced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Final, Protocol, runtime_checkable

# Read-model tables the pipeline writes and the API serves. The list is the
# handoff vocabulary; column truth lives with the ORM in
# ``oxbow.adapters.warehouse.models``, which is the only place DDL is defined.
WAREHOUSE_TABLES: Final = (
    "dataset_snapshot",
    "transaction",
    "account",
    "score",
    "scorecard_point",
    "rule_hit",
    "shap_contribution",
    "economics",
    "evidence_event",
    "graph_edge",
    "community",
    "account_membership",
    "scorecard_bin",
    "band_definition",
    "drift_period",
    "model_disagreement",
    "policy_allocation",
    "backtest_fold",
    "ablation_row",
    "validation_metric",
    "fairness_row",
    "perturbation_row",
)

MONEY_COLUMN_SUFFIX: Final = "_minor"

# ULID alphabet (Crockford base32, minus I/L/O/U). 26 characters, no dashes.
ULID_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH: Final = 26

TXN_NAMESPACE: Final = ":"


class WarehouseTableError(ValueError):
    """An unknown table, a bad row, or a write to a run that is no longer open."""


class RunState(str, Enum):
    """Lifecycle of a run. A completed run is immutable: rescoring is a new run.

    ``superseded`` exists because the packet must be able to say "this was decided
    on a run a later one replaced" (plan §15), which is a fact about the run, not
    about the decision.
    """

    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SUPERSEDED = "superseded"


IMMUTABLE_RUN_STATES: Final = frozenset({RunState.COMPLETE, RunState.SUPERSEDED})


def is_ulid(value: str) -> bool:
    """Whether ``value`` is a canonical 26-character ULID."""
    return len(value) == ULID_LENGTH and all(char in ULID_ALPHABET for char in value)


def assert_run_id(run_id: str) -> str:
    """Fail loud on a run id that is not a ULID.

    A UUID here would not crash anything; it would just make the stage-event
    cursor sort wrongly, and the SSE client would skip or repeat rows in a way
    that looks like a flaky network (03 §J).
    """
    if not is_ulid(run_id):
        raise WarehouseTableError(
            f"run_id {run_id!r} is not a {ULID_LENGTH}-character ULID (DEV-003). Postgres "
            "stores it as char(26) text, not as a native uuid."
        )
    return run_id


def assert_money_is_integer_minor(table: str, row: Mapping[str, Any]) -> None:
    """Every ``*_minor`` column holds an integer, never a float or a bool.

    ``bool`` is checked explicitly because Python's ``True`` is an ``int`` and
    would otherwise pass as one minor unit. The check is on the *name* so a new
    money column is covered the moment it is named, with nobody editing a list.
    """
    for key, value in row.items():
        if not key.endswith(MONEY_COLUMN_SUFFIX) or value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise WarehouseTableError(
                f"{table}.{key}={value!r} is {type(value).__name__}. Money is integer "
                f"minor units in every layer, including the warehouse (DEV-005), and a "
                f"float does not crash: it drifts."
            )


def assert_txn_id_namespaced(table: str, row: Mapping[str, Any]) -> None:
    """Corpus-qualified transaction ids only (DEV-004).

    Two corpora coexist in one table. ``41`` from PaySim and ``41`` from IBM-AML
    are different transactions, and an unqualified key would merge them into one
    row that belongs to neither.
    """
    if table != "transaction":
        return
    txn_id = row.get("txn_id")
    if isinstance(txn_id, str) and TXN_NAMESPACE not in txn_id:
        raise WarehouseTableError(
            f"transaction.txn_id={txn_id!r} is not corpus-namespaced. Expected "
            "'paysim:12345' or 'ibmaml:8842' (DEV-004)."
        )


def assert_writable_table(table: str) -> str:
    """Refuse an unknown table rather than creating one implicitly."""
    if table not in WAREHOUSE_TABLES:
        raise WarehouseTableError(
            f"unknown warehouse table {table!r}. Declared handoff tables: "
            f"{', '.join(WAREHOUSE_TABLES)}"
        )
    return table


@runtime_checkable
class WarehouseSink(Protocol):
    """Writes the pipeline's results where the API can read them.

    The null implementation writes Parquet/JSON under ``out/warehouse/`` and the
    Postgres implementation writes rows; both pass the same round-trip contract
    test, which is what lets the demo run with nothing else up (02 §H).
    """

    @property
    def sink_id(self) -> str:
        """Which warehouse this is (``null``, ``postgres``)."""
        ...

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
        """Register a run before any row references it.

        ``provenance`` separates a pipeline run from a fixture run in the API's
        own responses, so demo data can never be quoted as a measurement.
        """
        ...

    def run_state(self, run_id: str) -> RunState:
        """Current lifecycle state; raises for an unknown run."""
        ...

    def write(self, table: str, run_id: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Write rows to one table and return the count actually committed.

        Refuses a run in a completed/superseded state: a run is immutable once
        complete, which is the only reason a packet can pin one and stay truthful
        forever (plan §15).
        """
        ...

    def read(self, table: str, run_id: str, *, limit: int = 1000) -> Sequence[Mapping[str, Any]]:
        """Rows as written, in the table's declared order. Used by resume and tests."""
        ...

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
        """Persist one stage event and return its monotonic, stable id.

        The id is what makes SSE resume possible and duplicate-free: the client
        reconnects with ``Last-Event-ID`` and the server backfills from the stored
        row rather than re-emitting anything already sent (plan §13).
        """
        ...

    def stage_events(self, run_id: str, *, after_id: int = 0) -> Sequence[Mapping[str, Any]]:
        """Stored events with ``id > after_id``, ascending. The resume backfill."""
        ...

    def complete_run(self, run_id: str, state: RunState, *, error: str | None = None) -> None:
        """Move a run to its terminal state. ``COMPLETE`` freezes further writes."""
        ...


__all__ = [
    "IMMUTABLE_RUN_STATES",
    "MONEY_COLUMN_SUFFIX",
    "TXN_NAMESPACE",
    "ULID_ALPHABET",
    "ULID_LENGTH",
    "WAREHOUSE_TABLES",
    "RunState",
    "WarehouseSink",
    "WarehouseTableError",
    "assert_money_is_integer_minor",
    "assert_run_id",
    "assert_txn_id_namespaced",
    "assert_writable_table",
    "is_ulid",
]
