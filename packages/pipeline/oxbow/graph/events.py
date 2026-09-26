"""The event contract this layer consumes, checked once at the boundary.

``build_graph`` is handed canonical event v1 rows and nothing else. It does not
read raw corpora, does not repair them, and does not accept a frame that looks
close enough — the three ways a graph layer quietly starts describing a corpus
that does not exist.

The checks that carry weight:

* ``amount_minor`` must be ``Int64``. 01 B / DEV-005: money is integer minor
  units everywhere, because ``0.1 + 0.2`` is not ``0.3`` and a graph that sums
  parallel edges is exactly where that drift becomes a quoted number. A float
  column is refused here, by name, so the failure is not an arithmetic surprise
  three stages later.
* ``event_ts_utc`` must be tz-aware UTC. It is the only ordering column
  (03 C); ``local_hour`` is a *reading* column for human-hours rules and is never
  sorted on. The two are not interchangeable and this module is where that stops
  being a convention.
* ``txn_id`` must be unique. The total order is ``(event_ts_utc, txn_id)``; two
  rows sharing an id can swap between runs, and every sort, edge id and cycle
  walk downstream inherits the swap.
* Amounts are never summed across ``currency``. Currency is a group key on every
  aggregate this layer emits, and the helper that flattens to one number raises
  when a pair holds more than one currency.
* ``account_from == account_to`` is a **legitimate row**, not a contract violation
  (DEV-013: 591,212 of IBM-AML's 5,078,345 rows are self-transfers, mostly
  reinvestments). It is flagged here as ``is_self_transfer`` and stays a row: the
  graph keeps it in the event frame and the aggregate table, counts it per account
  and per currency, and excludes it from every adjacency structure in one place
  (:func:`oxbow.graph.build._adjacency`), which is the "kept as a feature, not used
  for cycle and fan detection" split rule 01 P3 asks for. Refusing it at *this*
  boundary was the over-enforcement that made a whole corpus un-ingestable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import polars as pl

from oxbow.contracts.canonical_v1 import CANONICAL_COLUMNS
from oxbow.graph.errors import EventContractError, MixedCurrencyError

# Canonical event v1, bound to the single published list rather than re-typed here:
# the ingest layer owns the shape, 02 B seam 2 makes it additive-only, and a second
# copy is how two modules start meaning different things by the same name. The graph
# reads a declared subset of it (``REQUIRED_EVENT_COLUMNS``) and ignores the rest.
CANONICAL_EVENT_V1_COLUMNS: Final[tuple[str, ...]] = CANONICAL_COLUMNS

# Every column the graph actually reads, and it reads all of them.
REQUIRED_EVENT_COLUMNS: Final[tuple[str, ...]] = (
    "txn_id",
    "event_ts_utc",
    "txn_type",
    "amount_minor",
    "currency",
    "account_from",
    "account_to",
)

# Carried through the edge table when present, for lineage on a multi-corpus run
# (DEV-004: two corpora coexist in one table).
OPTIONAL_EVENT_COLUMNS: Final[tuple[str, ...]] = ("source_dataset", "batch_id", "channel")

DERIVED_TS_COLUMN: Final = "ts_us"
DERIVED_EDGE_ID_COLUMN: Final = "edge_id"
DERIVED_SELF_COLUMN: Final = "is_self_transfer"

# Microseconds in a second. The edge table keeps one integer time unit so that
# window arithmetic never touches a datetime dtype.
MICROSECONDS_PER_SECOND: Final[int] = 1_000_000
MICROSECONDS_PER_HOUR: Final[int] = 3_600 * MICROSECONDS_PER_SECOND


@dataclass(frozen=True, slots=True)
class OrderedEvents:
    """The validated event frame, sorted on the canonical total order.

    ``frame`` is already in ``(event_ts_utc, txn_id)`` order, so ``edge_id`` is a
    function of the data rather than of the input's arrival order. Any caller that
    sorts again is re-sorting a sequence that cannot change.
    """

    frame: pl.DataFrame

    @property
    def height(self) -> int:
        return self.frame.height

    @property
    def self_transfer_count(self) -> int:
        return int(self.frame.filter(pl.col(DERIVED_SELF_COLUMN)).height)


def require_events(events: pl.DataFrame, order_columns: Sequence[str]) -> OrderedEvents:
    """Validate and canonically order a canonical event frame.

    Raises :class:`~oxbow.graph.errors.EventContractError` for anything short of
    the contract. There is no path in which a bad frame yields a graph.
    """
    if not isinstance(events, pl.DataFrame):
        raise EventContractError(
            f"build_graph takes a polars DataFrame of canonical events, got "
            f"{type(events).__name__}. Convert at the caller, not by loosening this."
        )
    if len(order_columns) != 2:
        raise EventContractError(f"the total order needs two columns, got {tuple(order_columns)}")
    if events.height == 0:
        raise EventContractError(
            "no canonical events to build a graph from. `ingest.allow_empty_batch` is "
            "false for the same reason: a run that scored nothing and a run that found "
            "nothing look identical downstream, and only one of them is true."
        )

    missing = [name for name in REQUIRED_EVENT_COLUMNS if name not in events.columns]
    if missing:
        raise EventContractError(
            f"canonical event frame is missing columns the graph reads: {missing}. "
            f"Columns present: {sorted(events.columns)}"
        )

    _check_dtypes(events)
    _check_nulls(events)
    _check_currency_codes(events)
    _check_unique_txn_id(events)

    ordered = events.select(_carried_columns(events, order_columns)).sort(list(order_columns))

    ts_us = to_epoch_us(ordered[order_columns[0]])
    enriched = ordered.with_columns(
        ts_us.cast(pl.Int64).alias(DERIVED_TS_COLUMN),
        pl.int_range(pl.len(), dtype=pl.Int64).alias(DERIVED_EDGE_ID_COLUMN),
        (pl.col("account_from") == pl.col("account_to")).alias(DERIVED_SELF_COLUMN),
    )
    return OrderedEvents(frame=enriched)


def _carried_columns(events: pl.DataFrame, order_columns: Sequence[str]) -> list[str]:
    """The columns kept, in a fixed order that does not depend on input order.

    Selection is explicit so two runs whose ingest emitted columns in different
    orders produce identical edge tables.
    """
    wanted = [
        "txn_id",
        "event_ts_utc",
        "txn_type",
        "amount_minor",
        "currency",
        "account_from",
        "account_to",
    ]
    wanted += [column for column in OPTIONAL_EVENT_COLUMNS if column in events.columns]
    wanted += list(order_columns)
    return list(dict.fromkeys(wanted))


def _check_dtypes(events: pl.DataFrame) -> None:
    """Assert each required column's dtype, with the money and time rules named."""
    expected: Mapping[str, pl.DataType] = {
        "txn_id": pl.String(),
        "txn_type": pl.String(),
        "currency": pl.String(),
        "account_from": pl.String(),
        "account_to": pl.String(),
        "amount_minor": pl.Int64(),
    }
    for column, dtype in expected.items():
        actual = events.schema[column]
        if actual != dtype:
            raise EventContractError(
                f"column {column!r} must be {dtype}, got {actual}."
                + (
                    " Money is integer minor units and never a float: a float amount "
                    "cannot be summed to the cent, so it is refused rather than rounded "
                    "(01 B, DEV-005)."
                    if column == "amount_minor" and isinstance(actual, pl.Float32 | pl.Float64)
                    else ""
                )
            )

    ts_dtype = events.schema["event_ts_utc"]
    if not isinstance(ts_dtype, pl.Datetime):
        raise EventContractError(
            f"column 'event_ts_utc' must be a tz-aware Datetime, got {ts_dtype}. The "
            "graph is time-stamped: an unparseable instant is an unordered edge."
        )
    if ts_dtype.time_zone is None:
        raise EventContractError(
            "column 'event_ts_utc' is a naive datetime. The ordering column must be "
            "UTC-aware; a naive timestamp is a hidden assumption about a timezone, and "
            "03 C is precisely about that assumption being wrong (an ODD_HOUR rule "
            "against UTC flags an entire East-African morning)."
        )
    if ts_dtype.time_zone != "UTC":
        raise EventContractError(
            f"column 'event_ts_utc' carries time zone {ts_dtype.time_zone!r}. The column "
            "name is the contract: convert in ingest, where the choice is auditable, "
            "not here, where it would be invisible."
        )

    negative = events.filter(pl.col("amount_minor") < 0).height
    if negative:
        raise EventContractError(
            f"{negative} rows carry a negative amount_minor. Value moved is a magnitude; "
            "a reversal is a *typed* event (txn_type), not a sign, because sign-encoded "
            "reversals cannot be linked back to what they reverse."
        )


def _check_nulls(events: pl.DataFrame) -> None:
    nulls = {
        column: int(events[column].null_count())
        for column in REQUIRED_EVENT_COLUMNS
        if events[column].null_count() > 0
    }
    if nulls:
        raise EventContractError(
            f"required canonical columns carry nulls: {nulls}. A null node key does not "
            "mean an unknown counterparty, it means an account silently disappears from "
            "the graph, and 'absent' is the claim a null must never be allowed to make."
        )


def _check_currency_codes(events: pl.DataFrame) -> None:
    """Currency is part of every amount, so it must be a comparable token.

    Lowercase ``eur`` and ``EUR`` would split one currency into two aggregates,
    which reads as conservative but is a silent double count of the money supply.
    """
    bad = (
        events.select(pl.col("currency"))
        .unique()
        .filter(~pl.col("currency").str.contains(r"^[A-Z]{2,5}$"))
        .get_column("currency")
        .to_list()
    )
    if bad:
        raise EventContractError(
            f"currency codes must be 2-5 uppercase ISO-style characters, found {bad[:5]}. "
            "Money is corpus-qualified and currency is a group key on every aggregate; a "
            "case or padding variant splits one currency into two totals."
        )


def _check_unique_txn_id(events: pl.DataFrame) -> None:
    duplicates = events.group_by("txn_id").len().filter(pl.col("len") > 1).sort("txn_id").head(5)
    if duplicates.height:
        examples = ", ".join(f"{row[0]!r} x{row[1]}" for row in duplicates.iter_rows())
        raise EventContractError(
            f"txn_id is not unique ({int(duplicates['len'].sum())} rows across "
            f"{duplicates.height} ids, e.g. {examples}). The total order is "
            "(event_ts_utc, txn_id); a duplicate id leaves two events unordered, and "
            "every sort, edge id and cycle walk inherits that ambiguity."
        )


def to_epoch_us(instants: pl.Series) -> pl.Series:
    """Cast a UTC-aware datetime column to microseconds since the epoch.

    One integer time unit for the whole layer: window arithmetic on a Datetime
    dtype silently depends on the time unit the column happens to carry, and
    ingest emits microseconds today without promising to tomorrow.
    """
    if not isinstance(instants.dtype, pl.Datetime):
        raise EventContractError(f"cannot derive an integer instant from a {instants.dtype} column")
    try:
        return instants.cast(pl.Int64)
    except pl.exceptions.PolarsError as exc:  # pragma: no cover - defensive
        raise EventContractError(f"cannot cast event_ts_utc to epoch microseconds: {exc}") from exc


def require_single_currency(totals: Mapping[str, int], *, what: str) -> int:
    """Flatten per-currency totals to one number, or refuse.

    The only place a currency dimension disappears, which is why it is a function
    with a name rather than a ``sum()`` at a call site.
    """
    if not totals:
        return 0
    if len(totals) > 1:
        rendered = ", ".join(f"{currency}={value}" for currency, value in sorted(totals.items()))
        raise MixedCurrencyError(
            f"{what} spans {len(totals)} currencies ({rendered}); refusing to sum them. "
            "There is no implicit FX in this project — pick a currency or report the "
            "per-currency totals (01 B, spec 5.2)."
        )
    return next(iter(totals.values()))


__all__ = [
    "CANONICAL_EVENT_V1_COLUMNS",
    "DERIVED_EDGE_ID_COLUMN",
    "DERIVED_SELF_COLUMN",
    "DERIVED_TS_COLUMN",
    "MICROSECONDS_PER_HOUR",
    "MICROSECONDS_PER_SECOND",
    "OPTIONAL_EVENT_COLUMNS",
    "REQUIRED_EVENT_COLUMNS",
    "OrderedEvents",
    "require_events",
    "require_single_currency",
    "to_epoch_us",
]
