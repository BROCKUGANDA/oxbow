"""P3a cycle tests: planted loops, hand-computed, one variable at a time.

Every expected value below was computed by hand from the fixture and is written
next to it. 00 §B: a fixture whose expected value came from the code it tests proves
nothing — so the comments say what the arithmetic is, and the assertions check the
code against the arithmetic rather than against itself.

The four discriminators, each isolated so only one thing differs between a case and
its control:

* **time** — equal amounts everywhere, so only ordering can reject the loop.
* **retention** — increasing timestamps, so only the value floor can reject it.
* **monotonicity** — a loop that clears the floor but grows on one hop.
* **budget** — a fixture small enough to count exactly, searched twice with
  different limits, to prove the bound bites and the pipeline still terminates.

Account keys are 12-hex-shaped because that is what ingest emits; the graph treats
them as opaque text, so readability here costs nothing.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Final

import polars as pl
import pytest

from oxbow.config import PipelineConfig, load_pipeline_config
from oxbow.graph import build_graph
from oxbow.graph.cycles import BUDGET_REASON, REPORT_CAP_REASON, TIMEOUT_REASON
from oxbow.graph.model import Cycle, CycleSearch

BASE: Final = datetime(2024, 1, 1, tzinfo=UTC)

A: Final = "a11ce0000000"
B: Final = "b0b000000000"
C: Final = "c0c500000000"
D: Final = "d0de00000000"
E: Final = "e5e500000000"
HUB: Final = "hub000000000"
LEAF: Final = "1ea700000000"

EVENT_SCHEMA: Final = {
    "txn_id": pl.Utf8,
    "event_ts_utc": pl.Datetime("us", "UTC"),
    "txn_type": pl.Utf8,
    "amount_minor": pl.Int64,
    "currency": pl.Utf8,
    "account_from": pl.Utf8,
    "account_to": pl.Utf8,
}


def event(
    txn_id: str,
    src: str,
    dst: str,
    amount_minor: int,
    stamp: timedelta,
    *,
    currency: str = "EUR",
    txn_type: str = "TRANSFER",
) -> dict[str, object]:
    """One canonical event. ``stamp`` is the offset from ``BASE``, in whole minutes."""
    return {
        "txn_id": txn_id,
        "event_ts_utc": BASE + stamp,
        "txn_type": txn_type,
        "amount_minor": amount_minor,
        "currency": currency,
        "account_from": src,
        "account_to": dst,
    }


def frame(events: Iterable[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(list(events), schema=EVENT_SCHEMA)


CYCLE_KEYS: Final = frozenset(
    {
        "min_length",
        "max_length",
        "value_retention_floor",
        "ignore_rails",
        "max_visits_per_component",
        "max_cycles_reported",
        "timeout_seconds",
    }
)


def config_with(**overrides: object) -> PipelineConfig:
    """The real pipeline config with named ``graph:`` values replaced.

    Used only where a test needs a different *bound*, never a different rule: the
    defaults under test everywhere else are the committed ones.
    """
    cfg = load_pipeline_config()
    raw = copy.deepcopy(cfg.raw)
    for key, value in overrides.items():
        if key in CYCLE_KEYS:
            raw["graph"]["cycles"][key] = value
        else:
            raw["graph"][key] = value
    return PipelineConfig(
        root=cfg.root, seed=cfg.seed, deployment_timezone=cfg.deployment_timezone, raw=raw
    )


def only(search: CycleSearch) -> Cycle:
    assert len(search.cycles) == 1, f"expected exactly one cycle, got {search.cycles}"
    return search.cycles[0]


# --- the planted loop ----------------------------------------------------

# Legs in traversal order, with the amounts and minutes written out so the
# arithmetic below is checkable by eye:
#   A -> B   1000 @ t=1
#   B -> C    900 @ t=2
#   C -> D    800 @ t=3
#   D -> A    700 @ t=4
# Timestamps strictly increasing -> time-respecting.
# Amounts non-increasing (1000 >= 900 >= 800 >= 700) -> value-retaining.
# Retention = min/max = 700/1000 = 0.70 >= 0.60 floor -> survives.
# Length = 4 nodes, inside [3, 6].
PLANTED: Final = [
    event("c1", A, B, 1000, timedelta(minutes=1)),
    event("c2", B, C, 900, timedelta(minutes=2)),
    event("c3", C, D, 800, timedelta(minutes=3)),
    event("c4", D, A, 700, timedelta(minutes=4)),
]


def test_planted_four_node_cycle_is_found_exactly() -> None:
    graph = build_graph(frame(PLANTED), load_pipeline_config())
    cycle = only(graph.search)
    assert cycle.path == (A, B, C, D)
    assert cycle.canonical_key == (A, B, C, D)  # A is the smallest key, so no rotation
    assert cycle.length == 4
    assert cycle.amounts_minor == (1000, 900, 800, 700)
    assert cycle.txn_ids == ("c1", "c2", "c3", "c4")
    assert cycle.currency == "EUR"
    assert cycle.first_amount_minor == 1000
    assert cycle.last_amount_minor == 700
    # 700 / 1000 = 0.7, hand-computed above.
    assert cycle.value_retention == pytest.approx(0.7)
    assert graph.stats.cycle_count == 1
    assert graph.stats.cycle_nodes == 4
    assert graph.stats.cycle_search_truncated is False
    assert graph.search.truncation_reason is None


def test_cycle_participation_lands_on_every_member_and_nobody_else() -> None:
    graph = build_graph(frame(PLANTED), load_pipeline_config())
    rows = {str(row["account"]): row for row in graph.nodes.iter_rows(named=True)}
    # The fixture has exactly four accounts, all of them on the loop, so the
    # expected participant set is the whole node table and nothing is left over.
    assert sorted(rows) == sorted([A, B, C, D])
    for account in (A, B, C, D):
        assert rows[account]["in_cycle"] is True
        assert rows[account]["cycle_count"] == 1
        assert rows[account]["max_cycle_length"] == 4
    assert graph.nodes.filter(~pl.col("in_cycle")).height == 0


# --- time-respecting: one reversed timestamp returns nothing -------------

# Identical to PLANTED except that leg c2 now happens at t=3 and c3 at t=2.
# Cyclic timestamp order around the loop is (1, 3, 2, 4). Every rotation of that
# sequence has a descent, so no walk can respect time:
#   start A: A->B(1), B->C(3), then C->D(2) is 2 < 3 -> dead
#   start B: B->C(3), C->D(2)? 2 < 3 -> dead immediately
#   start C: C->D(2), D->A(4), A->B(1)? 1 < 4 -> dead
#   start D: D->A(4), A->B(1)? dead
# Amounts are all equal here, so nothing except ordering can reject this loop.
TIME_REVERSED: Final = [
    event("r1", A, B, 1000, timedelta(minutes=1)),
    event("r2", B, C, 1000, timedelta(minutes=3)),
    event("r3", C, D, 1000, timedelta(minutes=2)),
    event("r4", D, A, 1000, timedelta(minutes=4)),
]


def test_time_reversed_loop_returns_nothing() -> None:
    graph = build_graph(frame(TIME_REVERSED), load_pipeline_config())
    assert graph.search.cycles == ()
    assert graph.stats.cycle_count == 0
    assert graph.nodes["in_cycle"].to_list() == [False, False, False, False]


def test_same_loop_with_the_timestamp_restored_returns_one_cycle() -> None:
    """The control: only the ordering differs between this and the case above."""
    restored = [
        event("r1", A, B, 1000, timedelta(minutes=1)),
        event("r2", B, C, 1000, timedelta(minutes=2)),
        event("r3", C, D, 1000, timedelta(minutes=3)),
        event("r4", D, A, 1000, timedelta(minutes=4)),
    ]
    graph = build_graph(frame(restored), load_pipeline_config())
    cycle = only(graph.search)
    assert cycle.path == (A, B, C, D)
    # Equal amounts -> 1000/1000 = 1.0, and the monotonicity rule is satisfied by
    # equality, which is the intended reading: a loop that neither grows nor shrinks
    # is a perfect round-robin, the strongest form of the signal.
    assert cycle.value_retention == pytest.approx(1.0)


# --- value retention ----------------------------------------------------


# Increasing timestamps, amounts (1000, 500, 400, 300): monotone, so only the
# retention floor can reject it. Retention = 300/1000 = 0.30 < 0.60 -> rejected.
def test_loop_that_fails_the_retention_floor_returns_nothing() -> None:
    losing = [
        event("v1", A, B, 1000, timedelta(minutes=1)),
        event("v2", B, C, 500, timedelta(minutes=2)),
        event("v3", C, D, 400, timedelta(minutes=3)),
        event("v4", D, A, 300, timedelta(minutes=4)),
    ]
    graph = build_graph(frame(losing), load_pipeline_config())
    assert graph.search.cycles == ()


def test_retention_exactly_at_the_floor_is_kept() -> None:
    """0.6 >= 0.6 passes. The boundary belongs to the signal, not to the rejection."""
    boundary = [
        event("b1", A, B, 1000, timedelta(minutes=1)),
        event("b2", B, C, 800, timedelta(minutes=2)),
        event("b3", C, D, 700, timedelta(minutes=3)),
        event("b4", D, A, 600, timedelta(minutes=4)),
    ]
    graph = build_graph(frame(boundary), load_pipeline_config())
    cycle = only(graph.search)
    assert cycle.value_retention == pytest.approx(0.6)
    assert cycle.amounts_minor == (1000, 800, 700, 600)


def test_growing_loop_is_rejected_even_though_it_clears_the_floor() -> None:
    """Amounts (700, 800, 900, 1000): retention 700/1000 = 0.7 clears 0.6.

    Only monotonicity rejects it, and the trace is hand-computed: a hop that
    forwards more than it received is funded from outside the loop, which is a
    different typology and not this evidence.
    """
    growing = [
        event("g1", A, B, 700, timedelta(minutes=1)),
        event("g2", B, C, 800, timedelta(minutes=2)),
        event("g3", C, D, 900, timedelta(minutes=3)),
        event("g4", D, A, 1000, timedelta(minutes=4)),
    ]
    graph = build_graph(frame(growing), load_pipeline_config())
    assert graph.search.cycles == ()


# --- currency, length and reversals -------------------------------------


def test_mixed_currency_loop_is_not_a_cycle() -> None:
    """Three EUR legs and one USD leg, all otherwise identical to the planted loop.

    Comparing 700 USD against 1000 EUR would be an implicit FX rate, so the loop is
    not a candidate at all rather than a candidate that fails a threshold.
    """
    mixed = [
        event("m1", A, B, 1000, timedelta(minutes=1)),
        event("m2", B, C, 900, timedelta(minutes=2)),
        event("m3", C, D, 800, timedelta(minutes=3)),
        event("m4", D, A, 700, timedelta(minutes=4), currency="USD"),
    ]
    graph = build_graph(frame(mixed), load_pipeline_config())
    assert graph.search.cycles == ()
    assert graph.stats.currencies == ("EUR", "USD")


def test_two_node_round_trip_is_below_the_minimum_length() -> None:
    """A -> B -> A is a payment and its refund, not a laundering loop: length 2 < 3."""
    round_trip = [
        event("p1", A, B, 1000, timedelta(minutes=1)),
        event("p2", B, A, 1000, timedelta(minutes=2)),
    ]
    graph = build_graph(frame(round_trip), load_pipeline_config())
    assert graph.search.cycles == ()


def test_five_node_loop_is_inside_the_configured_range() -> None:
    legs = [
        event("f1", A, B, 1000, timedelta(minutes=1)),
        event("f2", B, C, 950, timedelta(minutes=2)),
        event("f3", C, D, 900, timedelta(minutes=3)),
        event("f4", D, E, 850, timedelta(minutes=4)),
        event("f5", E, A, 800, timedelta(minutes=5)),
    ]
    graph = build_graph(frame(legs), load_pipeline_config())
    cycle = only(graph.search)
    assert cycle.length == 5
    # 800 / 1000 = 0.8
    assert cycle.value_retention == pytest.approx(0.8)


def test_seven_node_loop_is_outside_the_configured_range() -> None:
    keys = [f"n{i:09d}" for i in range(7)]
    legs = [
        event(f"s{i}", keys[i], keys[(i + 1) % 7], 1000, timedelta(minutes=i + 1)) for i in range(7)
    ]
    graph = build_graph(frame(legs), load_pipeline_config())
    assert graph.search.cycles == ()


def test_reversal_leg_is_excluded_by_configuration() -> None:
    """A -> B -> C -> A with the closing leg typed REVERSAL.

    Times and amounts would otherwise pass: 1000 >= 900 >= 800, retention 0.8, and
    ordering increasing. Only the txn_type rule rejects it, which is the point — a
    reversal is money coming back, not a loop that retained value.
    """
    legs = [
        event("x1", A, B, 1000, timedelta(minutes=1)),
        event("x2", B, C, 900, timedelta(minutes=2)),
        event("x3", C, A, 800, timedelta(minutes=3), txn_type="REVERSAL"),
    ]
    graph = build_graph(frame(legs), load_pipeline_config())
    assert graph.search.cycles == ()
    assert graph.pairs.filter(pl.col("txn_types").list.contains("REVERSAL")).height == 1


# --- rails --------------------------------------------------------------


def test_loop_through_a_rail_is_not_counted() -> None:
    """Two loops share A; one runs through a supernode.

    Degrees: A 4 (out to B and HUB, in from C and D), B 2, C 2, D 2, HUB 8 (one in
    from A, one out to D, plus six leaf transfers), and six leaves at 1 each.
    Sorted: [1,1,1,1,1,1,2,2,2,4,8], n = 11. At the 90th percentile the nearest rank
    is index int(0.90 * 11) = 9 -> value 4, so a rail is anything above 4: HUB only.
    A, B, C, D sit at or below the threshold and stay searchable.
    """
    leaves = [f"{LEAF}{i:02d}" for i in range(6)]
    offset = 5
    legs = [
        event("t1", A, B, 1000, timedelta(minutes=1)),
        event("t2", B, C, 1000, timedelta(minutes=2)),
        event("t3", C, A, 1000, timedelta(minutes=3)),
        event("t4", A, HUB, 1000, timedelta(minutes=4)),
        event("t5", HUB, D, 1000, timedelta(minutes=5)),
        event("t6", D, A, 1000, timedelta(minutes=6)),
    ]
    legs += [
        event(f"leaf{i}", leaves[i], HUB, 100, timedelta(minutes=offset + i)) for i in range(6)
    ]
    rail_config = config_with(rail_degree_percentile=90.0)
    graph = build_graph(frame(legs), rail_config)
    assert graph.node_types[HUB] == "rail"
    assert graph.stats.rail_degree_threshold == 4
    cycle = only(graph.search)
    assert cycle.path == (A, B, C)
    assert graph.search.rails_excluded == 1

    untyped = build_graph(frame(legs), load_pipeline_config())
    assert untyped.node_types[HUB] == "member"
    assert untyped.stats.rail_count == 0
    # With no rail to stop it, both loops are visible: the exclusion above was the
    # rail rule, not an artefact of the fixture.
    assert untyped.stats.cycle_count == 2


# --- the budget bites ---------------------------------------------------

# A -> B -> C -> A where every hop has two parallel alternatives, at times
# 1,2 / 3,4 / 5,6, plus four reverse legs (C->B at 7 and 10, B->A at 8, A->C at 9).
# Counted by hand, in traversal order:
#   forward A->B->C->A: 2 choices per hop, and every hop's times precede the next
#     hop's, so all 2 * 2 * 2 = 8 selections are time-respecting.
#   reverse C->B->A->C: C->B(7) -> B->A(8) -> A->C(9) closes, times increasing -> 1.
#   reverse B->A->C->B: B->A(8) -> A->C(9) -> C->B(10) closes -> 1.
#   other reverse starts fail on ordering: A->C(9) -> C->B(10) -> B->A needs a leg at
#     t >= 10 and there is none; B->C and C->A are forward-only.
# Total 8 + 1 + 1 = 10 cycles, all of length 3, all with equal amounts so retention
# is 1.0 and only ordering decides.
OCTAD: Final = [
    event("k1", A, B, 1000, timedelta(minutes=1)),
    event("k2", A, B, 1000, timedelta(minutes=2)),
    event("k3", B, C, 1000, timedelta(minutes=3)),
    event("k4", B, C, 1000, timedelta(minutes=4)),
    event("k5", C, A, 1000, timedelta(minutes=5)),
    event("k6", C, A, 1000, timedelta(minutes=6)),
    event("k7", C, B, 1000, timedelta(minutes=7)),
    event("k8", B, A, 1000, timedelta(minutes=8)),
    event("k9", A, C, 1000, timedelta(minutes=9)),
    event("k10", C, B, 1000, timedelta(minutes=10)),
]


def test_parallel_legs_multiply_the_cycle_count_exactly() -> None:
    graph = build_graph(frame(OCTAD), load_pipeline_config())
    assert graph.stats.cycle_count == 10
    lengths = {cycle.length for cycle in graph.search.cycles}
    assert lengths == {3}
    assert graph.search.truncated is False
    # 8 of the 10 are the forward loop, and they are the forward loop's 2*2*2 choices.
    forward = [cycle for cycle in graph.search.cycles if cycle.path == (A, B, C)]
    assert len(forward) == 8
    assert {(cycle.txn_ids[0], cycle.txn_ids[1], cycle.txn_ids[2]) for cycle in forward} == {
        (a, b, c) for a in ("k1", "k2") for b in ("k3", "k4") for c in ("k5", "k6")
    }


def test_report_cap_truncates_and_is_reported_as_a_lower_bound() -> None:
    graph = build_graph(frame(OCTAD), config_with(max_cycles_reported=3))
    assert graph.stats.cycle_count == 3
    assert graph.stats.cycle_search_truncated is True
    assert graph.stats.cycle_search_reason == REPORT_CAP_REASON
    # Traversal enters at the smallest node key and takes the earliest leg first, so
    # the three survivors are the first three of the eight forward combinations.
    assert [cycle.txn_ids for cycle in graph.search.cycles] == [
        ("k1", "k3", "k5"),
        ("k1", "k3", "k6"),
        ("k1", "k4", "k5"),
    ]


def test_per_component_visit_budget_stops_the_search_early() -> None:
    """Traced by hand with ``max_visits_per_component = 2``.

    The component loop enters at A and, for A's first leg k1, recurses through both
    B->C legs and both C->A legs, recording 4 cycles; k2 does the same for 4 more;
    then the budget is checked at the end of the start node and trips, so B's and C's
    own walks (the two reverse loops) are never searched. Found: 8 of 10, flagged.
    """
    graph = build_graph(frame(OCTAD), config_with(max_visits_per_component=2))
    assert graph.stats.cycle_search_truncated is True
    assert graph.stats.cycle_search_reason == BUDGET_REASON
    assert graph.stats.cycle_count == 8
    assert all(cycle.path == (A, B, C) for cycle in graph.search.cycles)


def test_timeout_flag_is_set_instead_of_the_pipeline_hanging() -> None:
    """A dense component that a naive enumeration would never finish.

    Fifteen nodes, every ordered pair present (210 edges), equal amounts and equal
    timestamps: with ties permitted by the ordering rule, every simple walk of length
    up to six is a candidate, which is combinatorially explosive. The visit budget is
    set out of reach so only the deadline can end the search, and the assertion that
    matters is that this returns at all, with the flag set — not the count it managed
    before the clock ran out.
    """
    nodes = [f"d{i:09d}" for i in range(15)]
    legs = [
        event(f"e{position:04d}", src, dst, 1000, timedelta(minutes=0))
        for position, (src, dst) in enumerate(
            (left, right) for left in nodes for right in nodes if left != right
        )
    ]
    started = time.monotonic()
    graph = build_graph(
        frame(legs),
        config_with(timeout_seconds=1, max_visits_per_component=10**9, max_cycles_reported=10**9),
    )
    elapsed = time.monotonic() - started
    assert graph.stats.cycle_search_truncated is True
    assert graph.stats.cycle_search_reason == TIMEOUT_REASON
    assert graph.search.visits > 0
    # Loose on purpose: this asserts "the budget terminated the search", not a
    # performance target. The pytest global timeout is 300 s.
    assert elapsed < 60.0


def test_zero_value_loop_is_rejected_and_counted_not_silently_dropped() -> None:
    """Four zero-amount legs: retention is min/max with max = 0, i.e. no denominator.

    Reporting 0/0 as a perfect round-robin would invent a laundering loop out of four
    bookkeeping entries, so it is rejected — and the rejection is counted, because an
    unexplained zero is how a rule looks fine while being blind.
    """
    zeros = [
        event("z1", A, B, 0, timedelta(minutes=1)),
        event("z2", B, C, 0, timedelta(minutes=2)),
        event("z3", C, D, 0, timedelta(minutes=3)),
        event("z4", D, A, 0, timedelta(minutes=4)),
    ]
    graph = build_graph(frame(zeros), load_pipeline_config())
    assert graph.search.cycles == ()
    assert graph.search.zero_value_loops_rejected == 1


def test_cycle_search_is_deterministic_across_runs_and_input_order() -> None:
    shuffled = list(reversed(PLANTED))
    first = build_graph(frame(PLANTED), load_pipeline_config()).search
    second = build_graph(frame(shuffled), load_pipeline_config()).search
    assert [(cycle.canonical_key, cycle.txn_ids) for cycle in first.cycles] == [
        (cycle.canonical_key, cycle.txn_ids) for cycle in second.cycles
    ]
