"""The event view the rules read, and the arithmetic they do on it.

``evaluate_rules`` is handed the same canonical event v1 frame the graph was built
from — not the graph's internal edge table. That distinction is the whole point of
this module: :func:`oxbow.graph.events.require_events` deliberately narrows the
carried columns to the seven the *graph* needs, and the columns it drops
(``local_hour``, ``event_date_local``) are precisely the ones §9 requires the
human-hours rules to read. A rule that worked around a missing ``local_hour`` by
binning ``event_ts_utc`` is the defect 03 C names, so the rule layer refuses an
event frame without the local columns and derives its local calendar from
``local_hour`` rather than from the instant.

What is established here once, for all twelve rules:

* **Money is Int64.** ``amount_minor`` must arrive as an integer minor unit and is
  kept as one in every tuple below. A float amount is a contract failure, not a
  rounding opportunity (01 B, DEV-005).
* **The local day is derived from ``local_hour``, never from a zone lookup.** Each
  row's offset is ``local_hour`` minus the UTC hour, normalised to whole hours in
  ``(-12, 12]``. That is exact for whole-hour deployments like the configured
  ``Africa/Kampala`` (UTC+3, no DST), and it makes the derivation auditable from
  the two columns it uses instead of from a table nobody can see.
* **Guards are pre-computed flags**, so no rule can forget one. ``is_self_transfer``,
  ``is_zero_value`` and ``is_reversal`` are attached once; the per-rule exclusion
  lists in ``config/rules.yaml`` are then consulted by id at the call site.
* **Ordering is total and never a wall clock.** Tuples come out in
  ``(event_ts_utc, txn_id)`` order, which is the same order the graph indexes, so
  a rule and the graph cannot disagree about which leg came first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, NamedTuple

import polars as pl

from oxbow.graph.events import (
    MICROSECONDS_PER_HOUR,
    MICROSECONDS_PER_SECOND,
    to_epoch_us,
)
from oxbow.rules.errors import RuleContractError

MICROSECONDS_PER_DAY: Final[int] = 24 * MICROSECONDS_PER_HOUR
# A minute is sixty seconds, not a sixtieth of a second: derived by multiplication
# because the integer division the other way truncates to 16,666 us and silently
# turns a 60-minute rule window into a one-second one.
MICROSECONDS_PER_MINUTE: Final[int] = 60 * MICROSECONDS_PER_SECOND

# The columns a rule cannot be evaluated without. ``txn_id`` is the tie-break that
# makes every signature below stable, ``local_hour`` is the only acceptable source
# of human-hours reasoning, and ``event_date_local`` is demanded because its
# absence is how a double conversion hides.
REQUIRED_RULE_COLUMNS: Final[tuple[str, ...]] = (
    "txn_id",
    "event_ts_utc",
    "event_date_local",
    "local_hour",
    "txn_type",
    "amount_minor",
    "currency",
    "account_from",
    "account_to",
)

REVERSAL_TXN_TYPES: Final[frozenset[str]] = frozenset({"REVERSAL", "REVERSAL_OF"})
# Accepted integer dtypes for ``local_hour``. Spelled out rather than tested against a
# base class because polars 1.32 exposes no public ``Integer`` predicate, and an
# ``Int64`` hour column is what the golden fixture's strict CSV loader emits while
# canonical v1 declares ``Int8`` — both are an hour of the day, and refusing one would
# couple this layer to a dtype the ingest layer owns.
INTEGER_DTYPES: Final[frozenset[pl.DataType]] = frozenset(
    {
        pl.Int8(),
        pl.Int16(),
        pl.Int32(),
        pl.Int64(),
        pl.UInt8(),
        pl.UInt16(),
        pl.UInt32(),
        pl.UInt64(),
    }
)

_MAX_OFFSET_HOURS: Final[int] = 12

# Exact ratio comparisons. ``0.8 * 400_000`` is not an integer in binary floating
# point, and a rule that fires on ``>=`` at the boundary while its neighbour fails
# ``<=`` is a rule nobody can reproduce. Thresholds are therefore converted to a
# Fraction and compared as integers; float division is used only for severity, where
# the result is a rank and never a money figure.
_RATIO_DENOMINATOR_CAP: Final[int] = 1_000_000


def hours_to_us(hours: float) -> int:
    return int(round(hours * MICROSECONDS_PER_HOUR))


def minutes_to_us(minutes: float) -> int:
    return int(round(minutes * MICROSECONDS_PER_MINUTE))


def days_to_us(days: float) -> int:
    return int(round(days * MICROSECONDS_PER_DAY))


def ratio_at_least(numerator: int, denominator: int, threshold: float) -> bool:
    """``numerator / denominator >= threshold`` without a float money product."""
    if denominator <= 0:
        return False
    fraction = Fraction(threshold).limit_denominator(_RATIO_DENOMINATOR_CAP)
    return numerator * fraction.denominator >= denominator * fraction.numerator


def ratio_of(numerator: int, denominator: int) -> float:
    """A dimensionless share, or 0.0 when there is no denominator to speak of."""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


class Event(NamedTuple):
    """One canonical event, as the rules see it.

    ``peer`` is the counterparty relative to ``side``: for an originated event the
    destination, for a received one the originator. Carrying a single peer field
    instead of both endpoints is what keeps a rule from quietly scoring an inbound
    leg as if the account had sent it — the direction error that turns every
    receiver into a suspect.
    """

    ts_us: int
    local_day_us: int
    local_hour: int
    txn_id: str
    amount_minor: int
    currency: str
    txn_type: str
    channel: str
    peer: str
    side: str
    is_self_transfer: bool
    is_zero_value: bool
    is_reversal: bool


ORIGINATED: Final[str] = "originated"
RECEIVED: Final[str] = "received"


@dataclass(frozen=True, slots=True)
class Window:
    """An evaluation or fit window, in epoch microseconds.

    Half-open on the left, closed on the right — the convention
    :meth:`oxbow.graph.model.AccountGraph.flow_window` already uses, restated here so
    a hit's window and the graph's window mean the same thing when Module B renders
    them side by side.
    """

    start_us: int
    end_us: int
    label: str = "run"

    def contains(self, ts_us: int) -> bool:
        """Membership, closed on both edges.

        Closed rather than half-open because the rule windows here are anchored on an
        observation and must include the observation that anchored them; the graph's
        half-open ``flow_window`` is a different question (what arrived during a period)
        and the two are not interchangeable.
        """
        return self.start_us <= ts_us <= self.end_us

    @property
    def span_us(self) -> int:
        return self.end_us - self.start_us


@dataclass(frozen=True, slots=True)
class RuleEvents:
    """The validated event frame plus the per-account indexes every rule walks."""

    frame: pl.DataFrame
    events: tuple[Event, ...]
    originated: Mapping[str, tuple[Event, ...]]
    received: Mapping[str, tuple[Event, ...]]
    incident: Mapping[str, tuple[Event, ...]]
    accounts: tuple[str, ...]
    window: Window
    self_transfer_count: int
    zero_value_count: int
    reversal_count: int
    local_offset_hours: tuple[float, ...] = ()
    currencies: tuple[str, ...] = ()

    def account_set(self) -> frozenset[str]:
        return frozenset(self.accounts)

    def legs(
        self,
        account: str,
        side: str,
        *,
        drop_reversals: bool = False,
        drop_self: bool = False,
        drop_zero: bool = False,
    ) -> tuple[Event, ...]:
        """One account's legs on one side, with the guards already applied.

        Filtering lives here rather than in each rule because a guard applied in
        eleven places is eleven chances to forget one.
        """
        table = self.originated if side == ORIGINATED else self.received
        legs = table.get(account, ())
        if not (drop_reversals or drop_self or drop_zero):
            return legs
        return tuple(
            event
            for event in legs
            if not (
                (drop_reversals and event.is_reversal)
                or (drop_self and event.is_self_transfer)
                or (drop_zero and event.is_zero_value)
            )
        )


def require_rule_events(events: pl.DataFrame) -> RuleEvents:
    """Validate a canonical event frame and build the rule-side indexes over it.

    Raises :class:`~oxbow.rules.errors.RuleContractError` for anything short of the
    contract. There is no path in which a frame missing ``local_hour`` yields a run:
    the alternative is the ODD_HOUR rule silently reading UTC and flagging every
    East African morning, which is the failure 03 C was written about.
    """
    if not isinstance(events, pl.DataFrame):
        raise RuleContractError(
            f"evaluate_rules takes a polars DataFrame of canonical events, got "
            f"{type(events).__name__}. Convert at the caller."
        )
    missing = [name for name in REQUIRED_RULE_COLUMNS if name not in events.columns]
    if missing:
        raise RuleContractError(
            f"the event frame is missing columns the rules read: {missing}. Present: "
            f"{sorted(events.columns)}. local_hour and event_date_local are not optional "
            "here — 03 C requires human-hours rules to read the local column."
        )
    if events.height == 0:
        raise RuleContractError(
            "no events to evaluate. An empty frame and a quiet corpus produce the same "
            "zero hit count, and only one of them is a result."
        )

    schema = events.collect_schema()
    if schema["amount_minor"] != pl.Int64():
        raise RuleContractError(
            f"amount_minor must be Int64 minor units, got {schema['amount_minor']}. Money "
            "is never a float in this project (01 B, DEV-005), and no rule coerces it."
        )
    if schema["local_hour"] not in INTEGER_DTYPES:
        raise RuleContractError(
            f"local_hour must be an integer hour 0-23, got {schema['local_hour']}"
        )
    ts_dtype = schema["event_ts_utc"]
    if not isinstance(ts_dtype, pl.Datetime):
        raise RuleContractError(f"event_ts_utc must be a tz-aware datetime, got {ts_dtype}")
    if ts_dtype.time_zone != "UTC":
        raise RuleContractError(
            f"event_ts_utc carries time zone {ts_dtype.time_zone!r}; the column name is the "
            "contract. Convert in ingest, where the choice is auditable."
        )
    negatives = events.filter(pl.col("amount_minor") < 0).height
    if negatives:
        raise RuleContractError(
            f"{negatives} rows carry a negative amount_minor. A reversal is a typed event, "
            "not a sign: a sign-encoded reversal cannot be linked back to what it reverses."
        )
    bad_hours = events.filter((pl.col("local_hour") < 0) | (pl.col("local_hour") > 23)).height
    if bad_hours:
        raise RuleContractError(
            f"{bad_hours} rows carry a local_hour outside 0..23; a value there means the "
            "timezone conversion ran twice, which is exactly how an odd-hour rule starts "
            "flagging ordinary business hours."
        )
    duplicates = events.group_by("txn_id").len().filter(pl.col("len") > 1).height
    if duplicates:
        raise RuleContractError(
            f"txn_id is not unique across {duplicates} ids; the total order "
            "(event_ts_utc, txn_id) is what makes every signature and window stable."
        )

    ordered = events.sort(["event_ts_utc", "txn_id"])
    enriched = ordered.with_columns(
        to_epoch_us(ordered.get_column("event_ts_utc")).cast(pl.Int64).alias("_ts_us"),
        pl.col("event_ts_utc").dt.hour().cast(pl.Int64).alias("_utc_hour"),
    )
    typed = _rows_from(enriched)
    return _assemble(
        ordered,
        typed,
        window_start_us=typed[0].ts_us,
        window_end_us=typed[-1].ts_us,
    )


def _rows_from(enriched: pl.DataFrame) -> tuple[_Row, ...]:
    """Column-wise extraction into typed rows, once, in the frame's total order."""
    ts_us = enriched.get_column("_ts_us").cast(pl.Int64).to_list()
    utc_hour = enriched.get_column("_utc_hour").cast(pl.Int64).to_list()
    local_hour = enriched.get_column("local_hour").cast(pl.Int64).to_list()
    txn_id = enriched.get_column("txn_id").to_list()
    txn_type = enriched.get_column("txn_type").to_list()
    amount = enriched.get_column("amount_minor").cast(pl.Int64).to_list()
    currency = enriched.get_column("currency").to_list()
    origin = enriched.get_column("account_from").to_list()
    destination = enriched.get_column("account_to").to_list()
    channel = (
        enriched.get_column("channel").to_list()
        if "channel" in enriched.columns
        else ["" for _ in range(enriched.height)]
    )
    return tuple(
        _Row(
            ts_us=int(ts_us[index]),
            utc_hour=int(utc_hour[index]),
            local_hour=int(local_hour[index]),
            txn_id=str(txn_id[index]),
            txn_type=str(txn_type[index]),
            amount_minor=int(amount[index]),
            currency=str(currency[index]),
            origin=str(origin[index]),
            destination=str(destination[index]),
            channel="" if channel[index] is None else str(channel[index]),
        )
        for index in range(enriched.height)
    )


class _Row(NamedTuple):
    """One frame row, resolved to the types the rule layer works in.

    Built once, here, rather than read out of a ``dict[str, object]`` at every call site:
    the alternative is a cast per field per rule, and a cast that cannot fail is the one
    nobody ever checks.
    """

    ts_us: int
    utc_hour: int
    local_hour: int
    txn_id: str
    txn_type: str
    amount_minor: int
    currency: str
    origin: str
    destination: str
    channel: str


def _normalised_offset(local_hour: int, utc_hour: int) -> int:
    """The whole-hour offset a row implies, in ``(-12, 12]``.

    Derived from the two hour columns rather than from a zone database, so the
    derivation is auditable from the frame alone and never reaches for a
    ``event_ts_utc``-based conversion the rules are forbidden to make.
    """
    offset = local_hour - utc_hour
    if offset > _MAX_OFFSET_HOURS:
        offset -= 24
    elif offset < -_MAX_OFFSET_HOURS:
        offset += 24
    return offset


def _floor_day(ts_us: int) -> int:
    return (ts_us // MICROSECONDS_PER_DAY) * MICROSECONDS_PER_DAY


def _assemble(
    frame: pl.DataFrame,
    rows: Sequence[_Row],
    *,
    window_start_us: int,
    window_end_us: int,
) -> RuleEvents:
    originated_index: dict[str, list[Event]] = {}
    received_index: dict[str, list[Event]] = {}
    incident_index: dict[str, list[Event]] = {}
    observed_offsets: set[int] = set()
    for row in rows:
        observed_offsets.add(_normalised_offset(row.local_hour, row.utc_hour))
        local_day = _floor_day(
            row.ts_us + _normalised_offset(row.local_hour, row.utc_hour) * MICROSECONDS_PER_HOUR
        )
        is_self = row.origin == row.destination
        is_reversal = row.txn_type in REVERSAL_TXN_TYPES
        outbound = Event(
            ts_us=row.ts_us,
            local_day_us=local_day,
            local_hour=row.local_hour,
            txn_id=row.txn_id,
            amount_minor=row.amount_minor,
            currency=row.currency,
            txn_type=row.txn_type,
            channel=row.channel,
            peer=row.destination,
            side=ORIGINATED,
            is_self_transfer=is_self,
            is_zero_value=row.amount_minor == 0,
            is_reversal=is_reversal,
        )
        inbound = Event(
            ts_us=row.ts_us,
            local_day_us=local_day,
            local_hour=row.local_hour,
            txn_id=row.txn_id,
            amount_minor=row.amount_minor,
            currency=row.currency,
            txn_type=row.txn_type,
            channel=row.channel,
            peer=row.origin,
            side=RECEIVED,
            is_self_transfer=is_self,
            is_zero_value=row.amount_minor == 0,
            is_reversal=is_reversal,
        )
        originated_index.setdefault(row.origin, []).append(outbound)
        received_index.setdefault(row.destination, []).append(inbound)
        incident_index.setdefault(row.origin, []).append(outbound)
        if row.origin != row.destination:
            incident_index.setdefault(row.destination, []).append(inbound)

    accounts = sorted(set(originated_index) | set(received_index))
    self_transfers = sum(1 for row in rows if row.origin == row.destination)
    zero_values = sum(1 for row in rows if row.amount_minor == 0)
    reversals = sum(1 for row in rows if row.txn_type in REVERSAL_TXN_TYPES)
    currencies = tuple(sorted({row.currency for row in rows}))
    return RuleEvents(
        frame=frame.select([name for name in REQUIRED_RULE_COLUMNS if name in frame.columns]),
        events=tuple(
            sorted(
                (leg for legs in incident_index.values() for leg in legs),
                key=lambda event: (event.ts_us, event.txn_id, event.side),
            )
        ),
        originated={
            key: tuple(sorted(legs, key=_event_sort_key)) for key, legs in originated_index.items()
        },
        received={
            key: tuple(sorted(legs, key=_event_sort_key)) for key, legs in received_index.items()
        },
        incident={
            key: tuple(sorted(legs, key=_event_sort_key)) for key, legs in incident_index.items()
        },
        accounts=tuple(accounts),
        window=Window(start_us=window_start_us, end_us=window_end_us, label="run"),
        self_transfer_count=int(self_transfers),
        zero_value_count=int(zero_values),
        reversal_count=int(reversals),
        local_offset_hours=tuple(sorted(float(offset) for offset in observed_offsets)),
        currencies=currencies,
    )


def _event_sort_key(event: Event) -> tuple[int, str, str]:
    return (event.ts_us, event.txn_id, event.side)


__all__ = [
    "MICROSECONDS_PER_DAY",
    "MICROSECONDS_PER_HOUR",
    "MICROSECONDS_PER_MINUTE",
    "MICROSECONDS_PER_SECOND",
    "ORIGINATED",
    "RECEIVED",
    "REQUIRED_RULE_COLUMNS",
    "Event",
    "RuleEvents",
    "Window",
    "days_to_us",
    "hours_to_us",
    "minutes_to_us",
    "ratio_at_least",
    "ratio_of",
    "require_rule_events",
]
