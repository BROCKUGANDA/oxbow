"""Self-edges in the graph layer: kept as events, absent from every traversal.

The canonical contract now admits ``account_from == account_to`` (DEV-013 -- 591,212
of IBM-AML's 5,078,345 rows are self-transfers, mostly reinvestments). Rule 01 P3
asks for those rows to be *excluded from cycle and fan detection while kept as a
feature*, so this file checks both halves of that sentence on hand-built canonical
frames: an ``A -> A`` event is a real event with real money in it, and it names no
counterparty, so it must contribute nothing to a degree, a loop, a community, a
PageRank, a betweenness walk, a neighbourhood hop or a downstream set.

Nothing here reads the IBM file or the ingest layer -- the frames are written row by
row, and every expected number is computed by hand beside the fixture that produces
it (00 §B: a fixture whose expected value came from the code it tests proves nothing).
The fixtures deliberately mix self-edges with rails, externals, singletons and real
loops, because the failure mode this guards is a rule that holds for one node type and
quietly does not hold for another.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl
import pytest

from oxbow.config import load_pipeline_config
from oxbow.graph import (
    NODE_TYPE_EXTERNAL,
    NODE_TYPE_MEMBER,
    NODE_TYPE_RAIL,
    SELF_TRANSFER_EXCLUDED_REASON,
    AccountGraph,
    build_graph,
    degree_measurement,
    load_graph,
    require_single_currency,
    write_graph,
)
from oxbow.graph.cycles import enumerate_cycles
from oxbow.graph.model import CycleSearch, EdgeLeg
from oxbow.graph.settings import CycleSettings

BASE: Final = datetime(2024, 1, 1, tzinfo=UTC)

# 12-hex-shaped keys, because that is what ingest emits and this layer treats them
# as opaque text.
ALICE: Final = "a11ce0000000"
BOB: Final = "b0b000000000"
CAROL: Final = "c0c500000000"
DAVE: Final = "d0de00000000"
MALL: Final = "ma1100000000"
HUB: Final = "4ab000000000"
EXT: Final = "e47000000000"
SOLO: Final = "501000000000"  # the account whose only events are self-transfers

EVENT_SCHEMA: Final = {
    "txn_id": pl.Utf8,
    "event_ts_utc": pl.Datetime("us", "UTC"),
    "txn_type": pl.Utf8,
    "amount_minor": pl.Int64,
    "currency": pl.Utf8,
    "account_from": pl.Utf8,
    "account_to": pl.Utf8,
}

# The columns that describe structure. A self-transfer may move none of them: the
# degrees, the counterparty counts, the typing the traversal depends on, and every
# aggregate derived from a walk. ``first_ts_us`` / ``last_ts_us`` are presence rather
# than structure, and ``self_transfer_count`` is the one column a self-transfer exists
# to move, so neither is in this list.
STRUCTURAL_COLUMNS: Final[tuple[str, ...]] = (
    "account",
    "total_degree",
    "in_degree",
    "out_degree",
    "unique_counterparties",
    "fan_in",
    "fan_out",
    "is_originator",
    "is_rail",
    "is_external",
    "node_type",
    "in_graph",
    "community_id",
    "cycle_count",
    "in_cycle",
    "local_density",
    "pagerank",
)


def event(
    txn_id: str,
    src: str,
    dst: str,
    amount_minor: int,
    minute: int,
    *,
    currency: str = "EUR",
    txn_type: str = "TRANSFER",
) -> dict[str, object]:
    return {
        "txn_id": txn_id,
        "event_ts_utc": BASE + timedelta(minutes=minute),
        "txn_type": txn_type,
        "amount_minor": amount_minor,
        "currency": currency,
        "account_from": src,
        "account_to": dst,
    }


def frame(events: Iterable[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(list(events), schema=EVENT_SCHEMA)


def two_leg_settings() -> CycleSettings:
    """A search that will report a 2-leg loop, which the shipped config will not.

    ``graph.cycles.min_length`` is floored at 3 by settings validation, so the only
    honest way to ask "would a real A -> B -> A still be found once these accounts
    also self-transfer?" is to build the value object the search reads. The floor is a
    policy about laundering loops; it is not the thing under test, and each assertion
    below says which of the two is doing the work.
    """
    return CycleSettings(
        min_length=2,
        max_length=6,
        value_retention_floor=0.6,
        require_non_increasing=True,
        ignore_rails=True,
        excluded_txn_types=(),
        max_visits_per_component=10_000,
        max_cycles_reported=100,
        timeout_seconds=25,
    )


def restricted_neighbours(
    graph: AccountGraph, candidates: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """The component restriction ``_run_cycle_search`` builds for its candidates."""
    allowed = set(candidates)
    return {
        account: tuple(
            sorted(
                partner
                for partner in graph.simple_neighbours.get(account, frozenset())
                if partner in allowed
            )
        )
        for account in sorted(allowed)
    }


def search_over(
    graph: AccountGraph, candidates: Iterable[str], settings: CycleSettings
) -> CycleSearch:
    """Re-run the bounded search over a built graph's own adjacency."""
    names = sorted(set(candidates))
    return enumerate_cycles(
        out_edges=graph.out_edges,
        candidates=names,
        undirected_adjacency=restricted_neighbours(graph, names),
        settings=settings,
        rails_excluded=0,
    )


def multigraph_legs(graph: AccountGraph) -> list[tuple[str, str, str]]:
    return [
        (str(u), str(v), str(data.get("txn_id")))
        for u, v, data in graph.multigraph.edges(keys=False, data=True)
    ]


# --- a corpus that is nothing but self-edges ------------------------------


# Four A -> A transfers of 500 and nothing else: one account, degree 0, no
# counterparty, no neighbour, no loop -- and 2,000 minor EUR that is real money and so
# must be counted. A self-transfer gives an account *degree* without giving it a
# *counterparty*, which is precisely why DEV-013 reports the excluded figure.
def self_only() -> list[dict[str, object]]:
    return [event(f"s{i}", ALICE, ALICE, 500, i + 1, txn_type="REINVESTMENT") for i in range(4)]


def test_self_only_corpus_has_no_relationship_and_no_loop() -> None:
    graph = build_graph(frame(self_only()), load_pipeline_config())

    # Kept: as events, as a pair aggregate, and as a counted per-currency total.
    assert graph.events.frame.height == 4
    assert graph.stats.event_count == 4
    assert graph.stats.self_transfer_count == 4
    assert graph.stats.self_transfer_value_minor == (("EUR", 2000),)
    assert graph.node_row(ALICE)["self_transfer_count"][0] == 4
    assert graph.self_transfer_count(ALICE) == 4
    pairs = graph.pairs.to_dicts()
    assert len(pairs) == 1, "the self-pair is retained, not dropped"
    assert pairs[0]["is_self_pair"] is True
    assert pairs[0]["edge_count"] == 4
    assert pairs[0]["total_value_minor"] == 2000

    # Absent from every traversal structure.
    assert graph.stats.edge_count == 0
    assert graph.degree_of(ALICE) == 0
    assert graph.node_row(ALICE)["unique_counterparties"][0] == 0
    assert graph.neighbours(ALICE) == ()
    assert graph.simple_neighbours[ALICE] == frozenset()
    assert ALICE not in graph.out_edges, "no self leg may be indexed for walking"
    assert ALICE not in graph.in_edges
    assert graph.multigraph.number_of_edges(ALICE, ALICE) == 0
    assert graph.multigraph.number_of_edges() == 0
    assert graph.downstream(ALICE, 3) == (), "an account is not its own downstream"

    # Absent from every aggregate, and said so out loud.
    assert graph.stats.node_count == 1
    assert graph.stats.member_count == 1
    assert graph.stats.in_graph_node_count == 0
    assert graph.stats.singleton_count == 1
    assert graph.stats.cycle_count == 0
    assert graph.search.cycles == ()
    assert graph.stats.community_count == 0
    assert graph.stats.community_skipped_reason is not None
    assert graph.stats.pagerank_skipped_reason is not None
    assert graph.node_row(ALICE)["community_id"][0] is None
    assert graph.node_row(ALICE)["pagerank"][0] is None
    assert graph.node_row(ALICE)["local_density"][0] is None
    assert graph.node_row(ALICE)["in_cycle"][0] is False
    assert graph.node_row(ALICE)["cycle_count"][0] == 0


def test_flow_table_is_a_counterparty_table_and_says_so() -> None:
    """Declared, not fixed: ``_flows`` sums non-self rows only, per currency.

    A self-transfer is an outflow and an inflow of the same account at the same
    instant, so it is excluded from ``flows`` with the rest of the traversal
    aggregates. The consequence is that an account whose only events are
    self-transfers gets no ``flows`` row at all, and its value is visible in the stats
    total and the pair table instead. This test exists so the decision stays a
    decision rather than a surprise for a consumer of flows.parquet.
    """
    graph = build_graph(frame(self_only()), load_pipeline_config())
    assert graph.flows.height == 0
    assert graph.flow_window(window_start_us=0, window_end_us=10**15).height == 0
    assert graph.stats.self_transfer_value_minor == (("EUR", 2000),)


def test_self_leg_cannot_close_a_loop_even_if_a_caller_indexes_one() -> None:
    """The search's own guard, checked rather than inherited.

    ``_adjacency`` is the single place this layer excludes self-transfers, and
    ``enumerate_cycles`` independently refuses a leg whose destination is its own
    start. Both are asserted, because "the exclusion lives in one function" is only
    safety if the second line of defence is real: a hand-built adjacency that *does*
    carry a self leg must still report the genuine 2-leg loop and nothing else -- no
    1-cycle out of an ``A -> A`` row.
    """
    legs = {
        ALICE: (
            EdgeLeg(1_700_000_000_000_000, "x1", ALICE, 1000, "EUR", "TRANSFER", 0),
            EdgeLeg(1_700_000_060_000_000, "a1", BOB, 1000, "EUR", "TRANSFER", 1),
        ),
        BOB: (EdgeLeg(1_700_000_120_000_000, "b1", ALICE, 1000, "EUR", "TRANSFER", 2),),
    }
    search = enumerate_cycles(
        out_edges=legs,
        candidates=[ALICE, BOB],
        undirected_adjacency={ALICE: (BOB,), BOB: (ALICE,)},
        settings=two_leg_settings(),
        rails_excluded=0,
    )
    assert search.count == 1, "the self leg adds no loop"
    assert search.cycles[0].canonical_key == (ALICE, BOB)
    assert search.cycles[0].txn_ids == ("a1", "b1")
    assert all(cycle.length >= 2 for cycle in search.cycles), "no 1-cycle is reportable"
    assert "x1" not in {txn for cycle in search.cycles for txn in cycle.txn_ids}


# --- real loops must survive the exclusion --------------------------------


# A -> B -> A twice over, on two accounts that also reinvest into themselves.
# Non-self degrees: ALICE 4 (two out to BOB, two in from BOB) and BOB 4 -- both above
# the singleton cut, and neither above the 99th-percentile rail threshold over two
# nodes (sorted [4, 4], index int(0.99*2) = 1 -> value 4 -> a rail needs degree > 4),
# so the only thing that could make the loop go missing is the exclusion under test.
def round_trip_with_selfs() -> list[dict[str, object]]:
    return [
        event("ab1", ALICE, BOB, 1000, 1),
        event("ba1", BOB, ALICE, 1000, 2),
        event("ab2", ALICE, BOB, 1000, 3),
        event("ba2", BOB, ALICE, 1000, 4),
        event("sa1", ALICE, ALICE, 700, 5),
        event("sa2", ALICE, ALICE, 600, 6),
        event("sb1", BOB, BOB, 500, 7),
    ]


def test_two_node_round_trip_is_still_a_loop_once_the_floor_allows_it() -> None:
    graph = build_graph(frame(round_trip_with_selfs()), load_pipeline_config())

    # The committed floor is 3 legs (a round trip is a payment and its refund), so
    # nothing is reported here -- and that is the floor speaking, not the exclusion.
    assert graph.stats.cycle_count == 0
    assert graph.stats.self_transfer_count == 3
    assert graph.stats.self_transfer_value_minor == (("EUR", 1800),)
    assert graph.degree_of(ALICE) == 4
    assert graph.degree_of(BOB) == 4

    # Both legs of the real loop are indexed, and no self leg is.
    assert [leg.txn_id for leg in graph.out_edges[ALICE]] == ["ab1", "ab2"]
    assert [leg.txn_id for leg in graph.out_edges[BOB]] == ["ba1", "ba2"]
    assert all(leg.dst != ALICE for leg in graph.out_edges[ALICE])
    assert all(leg.dst != BOB for leg in graph.out_edges[BOB])

    relaxed = search_over(graph, [ALICE, BOB], two_leg_settings())
    # Four loops, counted by hand: a loop is one outbound leg plus a return leg that is
    # not earlier, so the pairs are (ab1,ba1) at minutes 1/2, (ab1,ba2) at 1/4 and
    # (ab2,ba2) at 3/4 entered from ALICE, plus (ba1,ab2) at 2/3 entered from BOB. The
    # combination ab2 then ba1 is not time-respecting and is absent. What is also absent
    # is any loop built from sa1, sa2 or sb1: excluding the self-transfers hid nothing,
    # and reading a self-transfer as a loop would have invented something.
    assert relaxed.count == 4, "excluding self-edges must not hide a real loop"
    assert {cycle.canonical_key for cycle in relaxed.cycles} == {(ALICE, BOB)}
    assert {frozenset(cycle.txn_ids) for cycle in relaxed.cycles} == {
        frozenset({"ab1", "ba1"}),
        frozenset({"ab1", "ba2"}),
        frozenset({"ab2", "ba2"}),
        frozenset({"ba1", "ab2"}),
    }
    assert all(cycle.length == 2 for cycle in relaxed.cycles)
    assert relaxed.truncated is False
    self_txns = {"sa1", "sa2", "sb1"}
    assert all(not self_txns & set(cycle.txn_ids) for cycle in relaxed.cycles)


# A 4-ring, one leg per hop, amounts 1000/900/800/700 (retention 700/1000 = 0.7 above
# the 0.6 floor, non-increasing throughout), plus one self-transfer on every member.
# Degrees stay 2 for all four, so all four clear the singleton cut, and none clears the
# rail threshold (sorted [2,2,2,2], index int(0.99*4) = 3 -> value 2 -> a rail needs
# > 2). Exactly one loop is reported, and it is the ring.
def ring_with_selfs() -> list[dict[str, object]]:
    return [
        event("r1", ALICE, BOB, 1000, 1),
        event("r2", BOB, CAROL, 900, 2),
        event("r3", CAROL, DAVE, 800, 3),
        event("r4", DAVE, ALICE, 700, 4),
        event("sa", ALICE, ALICE, 600, 5),
        event("sb", BOB, BOB, 500, 6),
        event("sc", CAROL, CAROL, 400, 7),
        event("sd", DAVE, DAVE, 300, 8),
    ]


def test_four_node_loop_survives_self_edges_on_every_member() -> None:
    graph = build_graph(frame(ring_with_selfs()), load_pipeline_config())
    assert graph.stats.cycle_count == 1
    assert graph.search.cycles[0].path == (ALICE, BOB, CAROL, DAVE)
    assert graph.stats.cycle_nodes == 4
    assert graph.stats.self_transfer_count == 4
    # 600 + 500 + 400 + 300, in that order of accounts, summed once per currency.
    assert graph.stats.self_transfer_value_minor == (("EUR", 1800),)
    for account in (ALICE, BOB, CAROL, DAVE):
        assert graph.degree_of(account) == 2
        assert graph.node_row(account)["self_transfer_count"][0] == 1
    # Each member's counterparties are its two ring neighbours, never itself.
    assert graph.neighbours(ALICE) == (BOB, DAVE)
    assert graph.neighbours(BOB) == (ALICE, CAROL)
    assert graph.neighbours(CAROL) == (BOB, DAVE)
    assert graph.neighbours(DAVE) == (ALICE, CAROL)
    # Self rows are excluded from the inbound index too: each member's only inbound
    # leg is the ring edge that precedes it (r1 A->B, r2 B->C, r3 C->D, r4 D->A).
    assert [leg.txn_id for leg in graph.in_edges[CAROL]] == ["r2"]
    assert [leg.txn_id for leg in graph.in_edges[ALICE]] == ["r4"]
    assert [leg.txn_id for leg in graph.in_edges[BOB]] == ["r1"]
    assert [leg.txn_id for leg in graph.in_edges[DAVE]] == ["r3"]


# --- the exclusion must not change anything structural --------------------


# Six real edges: a 3-ring (ALICE -> BOB -> CAROL -> ALICE, amounts 1000/900/800 ->
# retention 0.8, non-increasing, one loop), an external that only receives (EXT), a
# two-edge chain through the merchant (DAVE -> MALL -> BOB), and self-transfers planted
# on accounts of several kinds.
def real_edges() -> list[dict[str, object]]:
    return [
        event("n1", ALICE, BOB, 1000, 1),
        event("n2", BOB, CAROL, 900, 2),
        event("n3", CAROL, ALICE, 800, 3),
        event("n4", ALICE, EXT, 700, 4),
        event("n5", DAVE, MALL, 600, 5),
        event("n6", MALL, BOB, 500, 6),
    ]


def real_edges_plus_selfs_on_originators() -> list[dict[str, object]]:
    """Self rows only on accounts that already originate a real edge (ALICE, BOB).

    That restriction is what makes the comparison below about traversal rather than
    about typing: an account that originates *only* self-transfers changes
    ``node_type`` (it stops being an external the corpus cannot see send), and that
    consequence has its own test two below. Here every structural column of every
    account must be the same decision, row for row, while three more events and two
    more currencies' worth of money arrive.
    """
    return [
        *real_edges(),
        event("z1", ALICE, ALICE, 400, 7),
        event("z2", ALICE, ALICE, 300, 8),
        event("z7", BOB, BOB, 50, 9, currency="USD", txn_type="REINVESTMENT"),
    ]


def real_edges_plus_selfs() -> list[dict[str, object]]:
    """The full mixed fixture: self rows on a member, an external and a nobody."""
    return [
        *real_edges_plus_selfs_on_originators(),
        # an "external" that self-transfers becomes an originator -- see its own test
        event("z3", EXT, EXT, 200, 10),
        # an account whose ONLY events are self-transfers
        event("z4", SOLO, SOLO, 100, 11),
        event("z5", SOLO, SOLO, 100, 12),
        event("z6", SOLO, SOLO, 100, 13),
    ]


def test_structure_is_identical_with_or_without_self_transfer_rows() -> None:
    cfg = load_pipeline_config()
    without = build_graph(frame(real_edges()), cfg)
    with_selfs = build_graph(frame(real_edges_plus_selfs_on_originators()), cfg)

    plain = without.nodes.select(STRUCTURAL_COLUMNS).sort("account")
    mixed = with_selfs.nodes.select(STRUCTURAL_COLUMNS).sort("account")
    assert mixed.height == 6
    assert mixed.equals(plain), "a self-transfer changed an account's structure"

    # The multigraph is the traversal projection: the same six legs, none of them a
    # self-loop. Retention lives in the event and pair frames, which is where a reader
    # of the artifact looks for it.
    assert multigraph_legs(with_selfs) == multigraph_legs(without)
    assert all(u != v for u, v, _ in multigraph_legs(with_selfs))
    assert with_selfs.multigraph.number_of_edges() == 6

    assert with_selfs.search.cycles == without.search.cycles
    assert with_selfs.stats.cycle_count == 1, "ALICE -> BOB -> CAROL -> ALICE"
    assert (
        with_selfs.pairs.filter(~pl.col("is_self_pair"))
        .sort(["account_from", "account_to", "currency"])
        .equals(without.pairs.sort(["account_from", "account_to", "currency"]))
    )
    assert with_selfs.flows.equals(without.flows), "the flow table is a counterparty table"
    # And the self rows are visible exactly where they should be.
    assert with_selfs.stats.self_transfer_count == 3
    assert with_selfs.stats.self_transfer_value_minor == (("EUR", 700), ("USD", 50))
    assert with_selfs.stats.edge_count == without.stats.edge_count == 6


def test_self_transfer_rows_move_only_the_counts_that_should_move() -> None:
    cfg = load_pipeline_config()
    without = build_graph(frame(real_edges()), cfg)
    with_selfs = build_graph(frame(real_edges_plus_selfs()), cfg)

    assert with_selfs.stats.event_count == without.stats.event_count + 7
    assert with_selfs.stats.edge_count == without.stats.edge_count, "relationships unchanged"
    assert with_selfs.stats.self_transfer_count == 7
    # 400 + 300 (ALICE) + 50 (BOB, USD) + 200 (EXT) + 100 x 3 (SOLO).
    assert with_selfs.stats.self_transfer_value_minor == (("EUR", 1200), ("USD", 50))
    assert without.stats.self_transfer_count == 0
    assert without.stats.self_transfer_value_minor == ()
    # One extra account (SOLO) and one extra self-pair row per (account, currency):
    # ALICE, EXT and SOLO in EUR, BOB in USD.
    assert with_selfs.stats.node_count == without.stats.node_count + 1 == 7
    assert with_selfs.stats.pair_count - without.stats.pair_count == 4
    # Degrees, counted by hand over the six real edges: ALICE 3 (out to BOB and EXT, in
    # from CAROL), BOB 2, CAROL 2, MALL 2, EXT 1, DAVE 1, SOLO 0. Four clear the
    # singleton cut, three do not. The control has two singletons (EXT, DAVE).
    assert without.stats.singleton_count == 2
    assert with_selfs.stats.singleton_count == 3
    assert with_selfs.stats.in_graph_node_count == 4
    assert with_selfs.stats.degree_all_nodes.max == 3 == without.stats.degree_all_nodes.max
    assert with_selfs.stats.currencies == ("EUR", "USD")
    assert without.stats.currencies == ("EUR",)


def test_self_only_account_is_typed_counted_and_excluded_in_one_build() -> None:
    """SOLO's row is the whole rule in miniature.

    Three ``SOLO -> SOLO`` events and nothing else: it originates them, so it is a
    ``member`` rather than an ``external`` (the corpus does see it send -- to itself),
    its degree is 0, it falls below the singleton cut so it leaves every aggregate, and
    the 300 minor it moved is still counted. An account with forty reinvestments must
    not be able to reach a community, a PageRank mass or a cycle by bookkeeping alone.
    """
    graph = build_graph(frame(real_edges_plus_selfs()), load_pipeline_config())
    solo = graph.node_row(SOLO)
    assert solo["node_type"][0] == NODE_TYPE_MEMBER
    assert solo["total_degree"][0] == 0
    assert solo["self_transfer_count"][0] == 3
    assert solo["in_graph"][0] is False
    assert solo["community_id"][0] is None
    assert solo["pagerank"][0] is None
    assert graph.degree_of(SOLO) == 0
    assert graph.neighbours(SOLO) == ()
    assert graph.downstream(SOLO, 2) == ()
    assert SOLO not in graph.graph_nodes
    assert SOLO not in graph.out_edges and SOLO not in graph.in_edges
    assert graph.stats.node_count == 7
    assert graph.stats.in_graph_node_count == 4
    assert graph.stats.singleton_count == 3


def test_external_that_self_transfers_becomes_an_originator_not_a_null_row() -> None:
    """The typing consequence, stated rather than left implicit.

    ``external`` means "this corpus never sees the account originate". An account that
    originates only self-transfers *is* seen originating, so it is typed ``member``
    with a real ``out_degree`` of 0 instead of a null one: "it sent nothing to anyone
    else" is an observation this corpus can make, and the null policy stays honest
    because the zero is measured. In the control build the same account is an external
    with the documented nulls, which is what makes the assertion above about the
    self-transfer and not about the fixture.
    """
    graph = build_graph(frame(real_edges_plus_selfs()), load_pipeline_config())
    ext = graph.node_row(EXT)
    assert graph.node_types[EXT] == NODE_TYPE_MEMBER
    assert ext["out_degree"][0] == 0, "a zero the corpus observed, not a null it could not"
    assert ext["unique_counterparties"][0] == 1, "ALICE sent to it; itself is not a counterparty"
    assert ext["self_transfer_count"][0] == 1
    assert ext["fan_out"][0] == 0
    control = build_graph(frame(real_edges()), load_pipeline_config())
    assert control.node_types[EXT] == NODE_TYPE_EXTERNAL
    assert control.node_row(EXT)["out_degree"][0] is None
    assert control.node_row(EXT)["fan_out"][0] is None


# --- rails and externals take the same rule ------------------------------


# A supernode population large enough for the *committed* 99th percentile to produce a
# rail, so the test is about the rule and not about a knob: 150 leaves pay MALL, 10 pay
# HUB, MALL also takes one leg from ALICE and sends one to BOB, and HUB reinvests into
# itself forty times.
#   Degrees over non-self rows: MALL 152, HUB 10, and 162 accounts at degree 1
#   (the 160 leaves, ALICE, BOB) -- 164 nodes, sorted [1 x 162, 10, 152], index
#   int(0.99*164) = 162 -> value 10. So the threshold is 10, MALL (152) is the only
#   rail, and HUB sits exactly on the threshold and stays untyped.
#   Had HUB's forty reinvestments been counted its degree would be 50, the sorted
#   population would end [1 x 162, 50, 152], and the threshold every account is compared
#   against would read 50 -- a rail policy decided by bookkeeping. The assertion that
#   pins this layer is ``rail_degree_threshold == 10``.
def rail_fixture_with_selfs() -> list[dict[str, object]]:
    legs: list[dict[str, object]] = []
    minute = 0
    for index in range(150):
        minute += 1
        legs.append(event(f"m{index:03d}", f"mall{index:06d}", MALL, 100, minute))
    for index in range(10):
        minute += 1
        legs.append(event(f"h{index:03d}", f"hub{index:07d}", HUB, 100, minute))
    for index in range(40):
        minute += 1
        legs.append(event(f"x{index:03d}", HUB, HUB, 900, minute, txn_type="REINVESTMENT"))
    legs.append(event("chain1", ALICE, MALL, 1000, minute + 1))
    legs.append(event("chain2", MALL, BOB, 1000, minute + 2))
    return legs


def test_rail_typing_and_threshold_read_real_edges_only() -> None:
    graph = build_graph(frame(rail_fixture_with_selfs()), load_pipeline_config())
    assert graph.degree_of(HUB) == 10, "forty self-transfers add nothing to the degree"
    assert graph.node_row(HUB)["unique_counterparties"][0] == 10
    assert graph.node_row(HUB)["self_transfer_count"][0] == 40
    assert graph.stats.rail_degree_threshold == 10, "the threshold is not lifted by bookkeeping"
    assert graph.stats.rail_count == 1
    assert graph.node_types[MALL] == NODE_TYPE_RAIL
    assert graph.node_types[HUB] != NODE_TYPE_RAIL, "degree 10 is on the threshold, not above it"
    assert graph.node_row(MALL)["fan_in"][0] is None, "a rail gets no fan score at all"
    assert graph.node_row(HUB)["fan_in"][0] == 10, "and HUB keeps a real one"

    # HUB originates forty events and every one is a self-transfer, so it has no
    # outbound leg at all: the key is absent from the index, which is a stronger
    # statement than an empty list. This is the rail-shaped case of the single rule in
    # _adjacency -- it is excluded because it is a self-transfer, not because of the
    # order the typing happened to run in.
    assert HUB not in graph.out_edges, "a high-degree account's self legs may not be indexed"
    assert graph.node_row(HUB)["out_degree"][0] == 0
    assert len(graph.in_edges[HUB]) == graph.node_row(HUB)["in_degree"][0] == 10
    assert all(leg.dst != HUB for leg in graph.in_edges[HUB])
    assert HUB not in graph.simple_neighbours[HUB]
    assert graph.neighbours(HUB) == tuple(sorted(f"hub{index:07d}" for index in range(10)))
    assert graph.downstream(HUB, 1) == ()
    assert graph.stats.self_transfer_count == 40
    assert graph.stats.self_transfer_value_minor == (("EUR", 36000),)


def test_no_traversal_structure_holds_a_self_leg_for_any_node_type() -> None:
    """The single-rule claim, checked on every leg of every fixture in this file."""
    fixtures = (
        self_only(),
        round_trip_with_selfs(),
        ring_with_selfs(),
        real_edges_plus_selfs(),
        rail_fixture_with_selfs(),
    )
    for legs in fixtures:
        graph = build_graph(frame(legs), load_pipeline_config())
        self_txns = {
            str(txn)
            for txn in graph.events.frame.filter(pl.col("is_self_transfer"))["txn_id"].to_list()
        }
        for adjacency in (graph.out_edges, graph.in_edges):
            for account, indexed in adjacency.items():
                for leg in indexed:
                    assert leg.dst != account, f"{account} indexed its own leg"
                    assert leg.txn_id not in self_txns, "a self leg reached the adjacency"
                    assert isinstance(leg.amount_minor, int) and leg.amount_minor >= 0
        for account, partners in graph.simple_neighbours.items():
            assert account not in partners, f"{account} is its own neighbour"
        assert graph.multigraph.number_of_edges() == graph.stats.edge_count
        assert all(u != v for u, v, _ in multigraph_legs(graph))
        # The invariant the two build paths share: a leg is indexed exactly when the
        # same row was counted in that account's degree.
        for row in graph.nodes.iter_rows(named=True):
            account = str(row["account"])
            indexed = len(graph.out_edges.get(account, ())) + len(graph.in_edges.get(account, ()))
            assert int(row["total_degree"]) == indexed, account
            # One hop is one leg, and a leg that returns to its own account is by
            # definition a self-transfer: no fixture may reach itself in a single hop.
            # (Two hops are a different matter -- the ring does return, legitimately.)
            assert account not in graph.downstream(account, 1), account


# --- the two build paths must agree, self-edges included ------------------


def test_degree_measurement_and_build_graph_agree_when_self_edges_are_present() -> None:
    """The important cross-path assertion: one rule, two callers, no drift.

    ``degree_measurement`` is the vectorised full-corpus path the day-3 gate prints
    from; ``build_graph`` is the interactive path the explorer and the cycle search
    read. On a frame mixing self-edges with a rail-shaped supernode, an external,
    singletons and normal edges, every degree-shaped number the two report must be the
    same number, because they read the one skeleton and the one predicate.
    """
    cfg = load_pipeline_config()
    legs = real_edges_plus_selfs()
    graph = build_graph(frame(legs), cfg)
    measured = degree_measurement(frame(legs), cfg)

    assert measured.event_count == graph.stats.event_count
    assert measured.self_transfer_count == graph.stats.self_transfer_count
    assert measured.self_transfer_value_minor == graph.stats.self_transfer_value_minor
    assert measured.node_count == graph.stats.node_count
    assert measured.rail_count == graph.stats.rail_count
    assert measured.external_count == graph.stats.external_count
    assert measured.member_count == graph.stats.member_count
    assert measured.singleton_count == graph.stats.singleton_count
    assert measured.rail_degree_threshold == graph.stats.rail_degree_threshold
    assert measured.degree_all_nodes == graph.stats.degree_all_nodes
    assert measured.degree_in_graph == graph.stats.degree_in_graph
    assert measured.top_degrees == graph.stats.top_degrees

    # The same fixture through a population where typing is a decision rather than a
    # default: MALL is the rail on 152 real counterparties, HUB stays untyped on
    # exactly the threshold, and both paths must agree on all of it -- including the
    # 40 reinvestments they are both counting and both excluding.
    rails = rail_fixture_with_selfs()
    rail_graph = build_graph(frame(rails), cfg)
    rail_measured = degree_measurement(frame(rails), cfg)
    assert rail_measured.rail_count == rail_graph.stats.rail_count == 1
    assert rail_measured.rail_degree_threshold == rail_graph.stats.rail_degree_threshold == 10
    assert rail_measured.node_count == rail_graph.stats.node_count == 164
    assert rail_measured.singleton_count == rail_graph.stats.singleton_count == 162
    assert rail_measured.top_degrees[0] == (MALL, 152)
    assert rail_measured.top_degrees == rail_graph.stats.top_degrees
    assert rail_measured.degree_all_nodes == rail_graph.stats.degree_all_nodes
    assert rail_measured.degree_all_nodes.max == 152
    assert rail_measured.self_transfer_count == rail_graph.stats.self_transfer_count == 40
    assert rail_measured.self_transfer_value_minor == (("EUR", 36000),)
    assert rail_measured.degree_in_graph == rail_graph.stats.degree_in_graph


def test_self_transfer_rows_cannot_change_the_rail_threshold() -> None:
    """Same real edges, thirty-five self rows added: threshold and typing stay put.

    At the committed 99th percentile a two-account fixture puts the threshold on the
    maximum degree, so nobody is a rail -- and that is the point. A build that counted
    the 35 reinvestments as relationships would have read ALICE's degree as 36, lifted
    the threshold with it, and removed a fan score from an account for a bookkeeping
    reason.
    """
    cfg = load_pipeline_config()
    quiet = [event("k1", ALICE, BOB, 1000, 1), event("k2", BOB, CAROL, 900, 2)]
    noisy = quiet + [event(f"v{i}", ALICE, ALICE, 800, 10 + i) for i in range(35)]

    plain = build_graph(frame(quiet), cfg)
    inflated = build_graph(frame(noisy), cfg)
    assert plain.stats.rail_degree_threshold == inflated.stats.rail_degree_threshold == 2
    assert plain.node_types[ALICE] == inflated.node_types[ALICE] == NODE_TYPE_MEMBER
    # ALICE's degree is the one edge she sends to BOB, never the 35 she sends to herself.
    assert plain.degree_of(ALICE) == 1
    assert inflated.degree_of(ALICE) == 1
    assert inflated.stats.self_transfer_count == 35
    assert inflated.stats.self_transfer_value_minor == (("EUR", 28000),)
    assert inflated.stats.self_transfer_exclusion_reason == SELF_TRANSFER_EXCLUDED_REASON
    assert plain.stats.self_transfer_exclusion_reason is None, "nothing was excluded"
    assert inflated.stats.edge_count == plain.stats.edge_count == 2
    # SOLO-shaped degenerate case: an account whose whole history is self-transfers is
    # a singleton at degree 0, so it leaves the population the threshold is computed
    # over rather than nudging it.
    only_selfs = [event(f"z{i}", SOLO, SOLO, 100, i + 1) for i in range(4)]
    mixed = build_graph(frame(quiet + only_selfs), cfg)
    assert mixed.degree_of(SOLO) == 0
    assert mixed.stats.singleton_count == plain.stats.singleton_count + 1


def test_zero_self_edge_corpus_reports_zero_everywhere() -> None:
    """PaySim-shaped corpus: no such row exists, and every count must read 0.

    The same forty-transfer burst ``test_p3a_graph`` uses -- one pair, forty real
    edges, no self-transfer. This is the assertion that makes the PaySim artifact
    bytes provable rather than merely plausible: the fields this change adds are 0 and
    empty, and the degrees are the degrees the old build already reported.
    """
    legs = [event(f"p{i:02d}", ALICE, BOB, 100 + i, i) for i in range(40)]
    cfg = load_pipeline_config()
    graph = build_graph(frame(legs), cfg)
    measured = degree_measurement(frame(legs), cfg)

    assert graph.stats.self_transfer_count == 0
    assert graph.stats.self_transfer_value_minor == ()
    assert graph.stats.self_transfer_exclusion_reason is None
    assert measured.self_transfer_count == 0
    assert measured.self_transfer_value_minor == ()
    assert graph.events.frame.filter(pl.col("is_self_transfer")).height == 0
    assert graph.node_row(ALICE)["self_transfer_count"][0] == 0
    assert graph.node_row(BOB)["self_transfer_count"][0] == 0
    # The structures are exactly the relationships: 40 legs out, 40 in, 40 in the pair
    # aggregate, and degrees 40/40 that both paths report identically.
    assert graph.stats.edge_count == 40
    assert len(graph.out_edges[ALICE]) == 40
    assert len(graph.in_edges[BOB]) == 40
    assert measured.degree_all_nodes == graph.stats.degree_all_nodes
    assert measured.top_degrees == graph.stats.top_degrees == ((ALICE, 40), (BOB, 40))


# --- persistence ----------------------------------------------------------


def test_self_transfer_statistics_survive_the_parquet_roundtrip(tmp_path: Path) -> None:
    graph = build_graph(frame(real_edges_plus_selfs()), load_pipeline_config())
    directory = write_graph(graph, tmp_path / "run")
    artifacts = load_graph(directory)
    assert artifacts.stats == graph.stats
    assert artifacts.stats.self_transfer_count == 7
    assert artifacts.stats.self_transfer_value_minor == (("EUR", 1200), ("USD", 50))
    assert artifacts.edges.equals(graph.events.frame), "the self rows persist as events"
    assert artifacts.edges.filter(pl.col("is_self_transfer")).height == 7
    assert artifacts.pairs.filter(pl.col("is_self_pair")).height == 4


def test_manifest_carries_the_counts_and_nothing_else_changes(tmp_path: Path) -> None:
    graph = build_graph(frame(self_only()), load_pipeline_config())
    directory = write_graph(graph, tmp_path)
    payload = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert payload["stats"]["self_transfer_count"] == 4
    assert payload["stats"]["self_transfer_value_minor"] == [["EUR", 2000]]
    assert payload["stats"]["edge_count"] == 0
    assert {entry["name"] for entry in payload["files"]} == {
        "nodes.parquet",
        "edges.parquet",
        "pairs.parquet",
        "flows.parquet",
        "cycles.parquet",
    }
    # The exclusion *sentence* is a rule of this layer, identical on every corpus, so it
    # is not duplicated into every manifest: it is a property on the stats object, read
    # when the count says the exclusion did anything.
    assert "self_transfer_exclusion_reason" not in payload["stats"]


def test_a_manifest_that_omits_the_self_transfer_field_is_refused(tmp_path: Path) -> None:
    """The stats contract is checked key by key, and the new key is part of it."""
    directory = write_graph(
        build_graph(frame(real_edges_plus_selfs()), load_pipeline_config()), tmp_path
    )
    manifest = directory / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    del payload["stats"]["self_transfer_value_minor"]
    manifest.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    with pytest.raises(Exception, match="self_transfer_value_minor"):
        load_graph(directory)


# --- money ----------------------------------------------------------------


def test_self_transfer_value_is_integer_minor_units_per_currency() -> None:
    """No float touches an amount, and no two currencies are ever added (01 B, DEV-005)."""
    legs = [
        event("c1", ALICE, ALICE, 1_234_567_890_123, 1),
        event("c2", ALICE, ALICE, 1, 2),
        event("c3", BOB, BOB, 99, 3, currency="USD"),
        event("c4", BOB, CAROL, 50, 4),
    ]
    graph = build_graph(frame(legs), load_pipeline_config())
    totals = dict(graph.stats.self_transfer_value_minor)
    assert totals == {"EUR": 1_234_567_890_124, "USD": 99}
    assert all(isinstance(value, int) for value in totals.values())
    measured = degree_measurement(frame(legs), load_pipeline_config())
    assert dict(measured.self_transfer_value_minor) == totals
    # Flattening them is the one operation this layer refuses, self-transfers included.
    with pytest.raises(Exception, match="currencies"):
        require_single_currency(totals, what="self-transfer total")


def test_mixed_fixture_degrees_are_exactly_the_non_self_rows() -> None:
    """An arithmetic check, not a snapshot: degree is the rows that name a counterparty."""
    legs = real_edges_plus_selfs()
    graph = build_graph(frame(legs), load_pipeline_config())
    events = graph.events.frame
    for row in graph.nodes.iter_rows(named=True):
        account = str(row["account"])
        outgoing = int(
            events.filter(
                (pl.col("account_from") == account) & (pl.col("account_to") != account)
            ).height
        )
        incoming = int(
            events.filter(
                (pl.col("account_to") == account) & (pl.col("account_from") != account)
            ).height
        )
        self_rows = int(
            events.filter(
                (pl.col("account_from") == account) & (pl.col("account_to") == account)
            ).height
        )
        # ``out_degree`` is null, not zero, for an account the corpus never sees send
        # (FEATURE_NULL_POLICY), which is why the disjunction is spelled out.
        out_degree = row["out_degree"]
        assert out_degree is None or int(out_degree) == outgoing, account
        assert int(row["total_degree"]) == outgoing + incoming, account
        assert int(row["in_degree"]) == incoming, account
        assert int(row["self_transfer_count"]) == self_rows, account
    assert graph.stats.edge_count == sum(
        1 for row in legs if row["account_from"] != row["account_to"]
    )
    assert graph.stats.edge_count == 6
    assert graph.stats.self_transfer_count == 7
    assert graph.stats.event_count == 13
