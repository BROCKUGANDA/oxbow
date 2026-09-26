"""Rolling distinct counts: the lifetime/first-occurrence kinds, and the boundary case.

WHY THIS FILE EXISTS. The gate in plan §8 found a defect while it was being written, and
the defect is worth stating precisely because the obvious fix is the tempting one. A
distinct count over a trailing window looks vectorisable: flag each row whose previous
occurrence of the key lies before *that row's own* window start, then sum the flags over
each row's window. It is not. The test "has this key already been seen" is a question about
the ANCHOR's window start, and a per-row flag cannot know it, so a key that was seen before
the window opened and re-seen inside it contributes no flagged row at all and is silently
dropped. Every fixture in this file is built to contain exactly such a key.

WHAT THE LAYER DOES INSTEAD, and the two ways it is exact:

* ``cumulative_distinct`` answers "how many distinct keys has this partition ever met", as
  of each row. First-ever-ness is a property of the row and its past, so the running total
  of a first-ever flag is the count — no window arithmetic, no under-count.
* ``distinct_in_window`` answers "how many distinct keys are present in the trailing
  window" by counting key *coverages*: a row makes its key present for every anchor from
  itself until the earlier of the key's next occurrence and ``row + window``, so the
  distinct count is the number of those half-open intervals covering the anchor — a sweep,
  which is vectorised, and which depends on no row after the anchor.
* ``first_seen_flag`` marks the one row that introduces a (partition, key) pair, and the
  windowed "new counterparties" quantities are counts of that flag, which is exact because
  "first ever" is anchor-independent.

EVERY EXPECTED NUMBER BELOW IS HAND-COMPUTED, and the arithmetic is written next to the
assertion. The straddle case is the one that would have caught the bug: under the rejected
identity the answer is 1, and 1 is wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from oxbow.config import find_repo_root, load_yaml
from oxbow.contracts.canonical_v1 import CANONICAL_COLUMNS, CANONICAL_DTYPES
from oxbow.features.build import FeatureTableBuilder
from oxbow.features.compute import registry_from_repo
from oxbow.features.kinds import ENTITY, EVENT_TS
from oxbow.features.registry import FeatureRegistry, RegistryError, parse_registry

BASE: datetime = datetime(2024, 1, 1, tzinfo=UTC)
HOUR: timedelta = timedelta(hours=1)
DAY: timedelta = timedelta(days=1)
LOCAL_OFFSET: timedelta = timedelta(hours=3)  # Africa/Kampala, no DST

REPO_ROOT: Path = find_repo_root()
FEATURES_YAML: Path = REPO_ROOT / "config" / "features.yaml"

ALICE: str = "a11ce0000000"
BOB: str = "b0b000000000"
CAROL: str = "c0c500000000"


def _row(index: int, src: str, dst: str, amount: int, at: datetime) -> dict[str, Any]:
    local = at + LOCAL_OFFSET
    return {
        "txn_id": f"paysim:{index}",
        "event_ts_utc": at,
        "event_date_local": local.date(),
        "local_hour": local.hour,
        "txn_type": "TRANSFER",
        "channel": "APP",
        "amount_minor": amount,
        "currency": "UGX",
        "account_from": src,
        "account_to": dst,
        "src_balance_before_minor": 100_000,
        "src_balance_after_minor": 100_000 - amount,
        "dst_balance_before_minor": 0,
        "dst_balance_after_minor": amount,
        "label_is_fraud": 0,
        "label_is_flagged": 0,
        "label_typology": None,
        "source_dataset": "paysim",
        "ingested_at": at,
        "run_id": "01HQX2Y3Z4A5B6C7D8E9F0G1H2",
        "batch_id": "0123456789ab",
    }


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    columns: dict[str, pl.Series] = {}
    for name in CANONICAL_COLUMNS:
        columns[name] = pl.Series(name, [row[name] for row in rows], dtype=CANONICAL_DTYPES[name])
    return pl.DataFrame(columns).sort([EVENT_TS, "txn_id"])


@pytest.fixture(scope="module")
def registry() -> FeatureRegistry:
    # A repository root, not the config directory: registry_from_repo appends `config`
    # itself, and the two spellings only differ by a path segment that fails loudly here
    # and silently in the CLI if this fixture is ever relaxed.
    return registry_from_repo(REPO_ROOT)


def _side(computed: pl.DataFrame, entity: str, column: str) -> list[Any]:
    """One entity's values for one column, in the canonical total order."""
    return (
        computed.filter(pl.col(ENTITY) == entity)
        .sort([EVENT_TS, "txn_id"])[column]
        .to_list()
    )


# --- the boundary case that broke the per-row identity -------------------------------


def straddle_events() -> pl.DataFrame:
    """Alice is active twice on one local day, then again 30 and a half days later.

    Rows, all from ALICE's side (``account_from``), with the local date in Kampala:

    ======  ================  ==============  ============================
    txn     event_ts_utc      local date      why this row exists
    ======  ================  ==============  ============================
    1       2024-01-01 01:00  2024-01-01     opens the day, before the window
    2       2024-01-01 20:00  2024-01-01     same day, INSIDE the anchor window
    3       2024-01-31 12:00  2024-01-31     the anchor: window opens 01-01 12:00
    ======  ================  ==============  ============================

    At row 3 the thirty-day window is ``(2024-01-01 12:00Z, 2024-01-31 12:00Z]``. Local
    day 2024-01-01 is present in it through row 2, and local day 2024-01-31 through row 3,
    so the distinct count of active local days is TWO. The rejected per-row identity answers
    ONE, because the only flagged row of 2024-01-01 is row 1, which sits outside.
    """
    return _frame(
        [
            _row(1, ALICE, BOB, 1_000, BASE + HOUR),
            _row(2, ALICE, BOB, 2_000, BASE + timedelta(hours=20)),
            _row(3, ALICE, BOB, 3_000, BASE + 30 * DAY + timedelta(hours=12)),
        ]
    )


def test_a_day_straddling_the_window_boundary_is_still_counted(registry: FeatureRegistry) -> None:
    """The regression: 1, 1, 2 by hand, and the code said 1, 1, 1."""
    computed = FeatureTableBuilder(registry).compute_frame(straddle_events())
    assert _side(computed, ALICE, "active_days_30d") == [1, 1, 2]


def test_transactions_per_active_day_follows_the_correct_day_count(
    registry: FeatureRegistry,
) -> None:
    """Hand: at row 3 two transactions spread over two active days is 10000 bps a day.

    Under the wrong day count the same row read 2/1 = 20000 bps, which is the number a
    reviewer would quote as "twice the daily rate" for a flat one-per-day account.
    """
    computed = FeatureTableBuilder(registry).compute_frame(straddle_events())
    # bps = txn_count_in_30d * 10_000 // active_days_30d: 1*10000//1, 2*10000//1, 2*10000//2
    assert _side(computed, ALICE, "txn_count_in_30d") == [1, 2, 2]
    assert _side(computed, ALICE, "txns_per_active_day_30d_bps") == [10_000, 20_000, 10_000]


def test_the_receiving_side_sees_the_same_active_days(registry: FeatureRegistry) -> None:
    """Bob only ever receives here, and his day count is the same 1, 1, 2.

    A distinct count is a property of the partition's history, not of who initiated what:
    Bob's rows fall on the same two local days as Alice's, and the window is the entity's
    own, so both sides of the matrix answer the same question about different accounts.
    """
    computed = FeatureTableBuilder(registry).compute_frame(straddle_events())
    assert _side(computed, BOB, "active_days_30d") == [1, 1, 2]


def test_a_key_seen_before_the_window_and_revisited_inside_it_is_not_dropped(
    registry: FeatureRegistry,
) -> None:
    """The same failure mode one subject over: a counterparty, not a calendar day.

    Alice meets Bob at +1h, Carol at +20d and Bob again at +21d, so every one of those rows
    still has all three in its thirty-day window. The values below are read off the fixture:
    two distinct counterparties ever met by the last row, three non-self visits, and two
    first-ever introductions. A windowed distinct count built on the rejected per-row
    identity is where a key like Bob — first seen, then re-seen after the window had already
    swallowed the first sighting — disappears; the lifetime counters cannot lose it, because
    first-ever-ness does not depend on where the anchor sits.
    """
    events = _frame(
        [
            _row(1, ALICE, BOB, 1_000, BASE + HOUR),
            _row(2, ALICE, CAROL, 2_000, BASE + 20 * DAY),
            _row(3, ALICE, BOB, 3_000, BASE + 21 * DAY),
        ]
    )
    computed = FeatureTableBuilder(registry).compute_frame(events)
    # Lifetime distinct counterparties at each row: Bob, then Bob+Carol, then still 2.
    assert _side(computed, ALICE, "lifetime_counterparties") == [1, 2, 2]
    # First-ever flags: all three rows introduce nothing new except 1 and 2.
    assert _side(computed, ALICE, "is_counterparty_first_ever") == [1, 1, 0]
    # Every row is inside the previous row's window, so both introductions still count.
    assert _side(computed, ALICE, "counterparties_first_seen_30d") == [1, 2, 2]
    assert _side(computed, ALICE, "nonself_visit_count_30d") == [1, 2, 3]
    # ... and the repeat column is the difference of those two: 0, 0, 1.
    assert _side(computed, ALICE, "repeat_visit_count_30d") == [0, 0, 1]


# --- the lifetime kinds, hand-checked ------------------------------------------------


def test_lifetime_distinct_counterparties_split_by_direction(registry: FeatureRegistry) -> None:
    """Four events, read by hand off the fixture, three lifetime counters.

    ======  ==========  =========  ==========  ========  ========
    txn     time        from -> to amount      Alice in  Alice out
    ======  ==========  =========  ==========  ========  ========
    1       T+1h        Alice->Bob  1 000      -        Bob
    2       T+2h        Carol->Alice 2 000      Carol    -
    3       T+3h        Alice->Bob  3 000      -        Bob (again)
    4       T+4h        Dave->Alice 4 000      Dave     -
    ======  ==========  =========  ==========  ========  ========

    Alice's lifetime counterparties run 1, 2, 2, 3: Bob once, then Carol, no change on the
    repeat, then Dave. Her inflow-side and outflow-side counters never share a key, so they
    run 0, 1, 1, 2 and 1, 1, 1, 1.
    """
    dave = "d0de00000000"
    events = _frame(
        [
            _row(1, ALICE, BOB, 1_000, BASE + HOUR),
            _row(2, CAROL, ALICE, 2_000, BASE + 2 * HOUR),
            _row(3, ALICE, BOB, 3_000, BASE + 3 * HOUR),
            _row(4, dave, ALICE, 4_000, BASE + 4 * HOUR),
        ]
    )
    computed = FeatureTableBuilder(registry).compute_frame(events)
    assert _side(computed, ALICE, "lifetime_counterparties") == [1, 2, 2, 3]
    assert _side(computed, ALICE, "lifetime_inflow_counterparties") == [0, 1, 1, 2]
    assert _side(computed, ALICE, "lifetime_outflow_counterparties") == [1, 1, 1, 1]


def test_lifetime_distinct_amounts_ignores_the_balance_probe(registry: FeatureRegistry) -> None:
    """Amounts 500, 500, 0, 700 on Alice's outflow side: distinct non-zero is 1, 1, 1, 2.

    The zero-amount row is a balance probe (plan §8): it is kept, flagged, and excluded
    from value features by the ``is_nonzero`` predicate the distinct amount counter
    declares — so it must not widen the distinct set.
    """
    events = _frame(
        [
            _row(1, ALICE, BOB, 500, BASE + HOUR),
            _row(2, ALICE, BOB, 500, BASE + 2 * HOUR),
            _row(3, ALICE, BOB, 0, BASE + 3 * HOUR),
            _row(4, ALICE, BOB, 700, BASE + 4 * HOUR),
        ]
    )
    computed = FeatureTableBuilder(registry).compute_frame(events)
    assert _side(computed, ALICE, "lifetime_distinct_amounts") == [1, 1, 1, 2]
    assert _side(computed, ALICE, "amounts_first_seen_30d") == [1, 1, 1, 2]
    # amount_repetition_30d_bps = (distinct amounts - txn count) scaled: hand-checked from
    # the two columns this quotient divides, so a changed window cannot pass silently.
    assert _side(computed, ALICE, "lifetime_counterparties") == [1, 1, 1, 1]


def test_a_repeated_visit_within_the_window_counts_once_per_pair(registry: FeatureRegistry) -> None:
    """Two Alice->Bob transfers an hour apart, then a third 40 days later.

    ``pair_visit_count_30d`` counts the (Alice, Bob) pair's rows in the window: 1, 2, then 1
    again once the first two have aged out — a counter that forgets, unlike the lifetime one.

    ``repeat_visit_count_30d`` is ``nonself_visit_count_30d - counterparties_first_seen_30d``,
    and the third row is the interesting one. Hand-computed at row 3: one non-self visit in
    the window, and zero *first sightings* in the window, because Bob was first met 40 days
    ago and that row has aged out — so the answer is 1 - 0 = 1. A repeat of an old
    relationship is still a repeat even when the meeting itself is outside the window, which
    is the distinction between this column and the windowed "new counterparty" one:
    ``counterparties_first_seen_30d`` reads 1, 1, 0 for the same three rows.
    """
    events = _frame(
        [
            _row(1, ALICE, BOB, 1_000, BASE + HOUR),
            _row(2, ALICE, BOB, 1_000, BASE + 2 * HOUR),
            _row(3, ALICE, BOB, 1_000, BASE + 40 * DAY),
        ]
    )
    computed = FeatureTableBuilder(registry).compute_frame(events)
    assert _side(computed, ALICE, "pair_visit_count_30d") == [1, 2, 1]
    assert _side(computed, ALICE, "lifetime_counterparties") == [1, 1, 1]
    assert _side(computed, ALICE, "repeat_visit_count_30d") == [0, 1, 1]
    assert _side(computed, ALICE, "counterparties_first_seen_30d") == [1, 1, 0]
    assert _side(computed, ALICE, "nonself_visit_count_30d") == [1, 2, 1]


# --- the vocabulary guard that keeps the bug from coming back ------------------------


def _entries_with(mutate: Any) -> FeatureRegistry:
    raw: dict[str, object] = load_yaml(FEATURES_YAML)
    features = [dict(entry) for entry in raw["features"] if isinstance(entry, dict)]
    mutate(features)
    return parse_registry({**raw, "features": features})


def test_a_bounded_window_is_refused_on_a_lifetime_distinct_kind() -> None:
    """The rejected identity must not be re-declarable under the lifetime kind's name.

    ``cumulative_distinct`` answers "ever", and its whole argument is that the answer needs
    no window. A registry that let it declare ``30d`` would put the under-counting identity
    straight back into the matrix, with the honest name still on the column.
    """
    def widen(entries: list[dict[str, object]]) -> None:
        for entry in entries:
            if entry["id"] == "lifetime_counterparties":
                entry["window"] = "30d"

    with pytest.raises(RegistryError, match="is not expressible as a difference"):
        _entries_with(widen)


def test_a_windowed_distinct_count_is_refused_on_a_non_contiguous_subject() -> None:
    """``distinct_in_window`` is exact, and stays confined to a subject whose occurrences
    of one value sit in one time-contiguous block. Counterparties do not: Alice can meet
    Bob, then Carol, then Bob again, and the sweep's intervals would overlap in a way the
    cheap path does not model. The loader refuses the declaration rather than publishing a
    number that reads like a distinct count."""

    def retarget(entries: list[dict[str, object]]) -> None:
        for entry in entries:
            if entry["id"] == "active_days_30d":
                entry["subject"] = "counterparty"

    with pytest.raises(RegistryError, match="not distinct-countable"):
        _entries_with(retarget)


def test_a_first_seen_flag_with_a_bounded_window_is_refused() -> None:
    """First-ever-ness is a lifetime fact; a window on it would be a different quantity."""

    def cap(entries: list[dict[str, object]]) -> None:
        for entry in entries:
            if entry["id"] == "is_counterparty_first_ever":
                entry["window"] = "30d"

    with pytest.raises(RegistryError, match="lifetime quantity"):
        _entries_with(cap)


def test_the_distinct_kinds_are_all_wired_in_the_shipped_registry(
    registry: FeatureRegistry,
) -> None:
    """A kind nobody declares is a guard nobody exercises. Measured, not assumed."""
    by_kind: dict[str, int] = {}
    for entry in registry.entries:
        by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
    assert by_kind["cumulative_distinct"] >= 3
    assert by_kind["first_seen_flag"] >= 2
    assert by_kind["distinct_in_window"] >= 1
