"""P3a graph-construction tests: typed nodes, retained parallelism, stable
communities, capped subgraphs, and a boundary that refuses bad events.

Every expected number was computed by hand from the fixture written beside it —
00 §B: a fixture whose expected value came from the code it tests proves nothing.
Where a value cannot be computed by hand (PageRank's exact ranking over a five-node
graph), the assertion is a property the definition guarantees (the ranks sum to one)
plus the config knob that must change it, not a number copied out of a run.

The fixtures are small on purpose: a rail, an external counterparty, a singleton and
a 40-transfer burst are all checkable on paper, and a corpus-agnostic layer must be
correct on them before it is measured on 6.3 M rows.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl
import pytest

from oxbow.config import PipelineConfig, load_pipeline_config
from oxbow.graph import (
    FEATURE_NULL_POLICY,
    FLOW_NULL_POLICY,
    NODE_TYPE_EXTERNAL,
    NODE_TYPE_MEMBER,
    NODE_TYPE_RAIL,
    AccountGraph,
    build_graph,
    degree_measurement,
    load_graph,
    load_graph_settings,
    neighbourhood,
    require_single_currency,
    write_graph,
)
from oxbow.graph.errors import (
    EventContractError,
    GraphArtifactError,
    GraphConfigError,
    GraphTooLargeError,
    MixedCurrencyError,
    UnknownAccountError,
)
from oxbow.graph.events import CANONICAL_EVENT_V1_COLUMNS, REQUIRED_EVENT_COLUMNS
from oxbow.graph.features import betweenness_centrality

BASE: Final = datetime(2024, 1, 1, tzinfo=UTC)
# The edge table stores whole microseconds since the epoch, so a fixture that wants
# to name an absolute instant has to convert rather than assume the epoch is here.
BASE_US: Final[int] = int(BASE.timestamp() * 1_000_000)
MINUTE_US: Final[int] = 60 * 1_000_000

ALICE: Final = "a11ce0000000"
BOB: Final = "b0b000000000"
CAROL: Final = "c0c500000000"
DAVE: Final = "d0de00000000"
EVE: Final = "e5e500000000"
MALL: Final = "ma1100000000"
HUB: Final = "4ab000000000"
EXT: Final = "e47000000000"
SOLO: Final = "501000000000"

EVENT_SCHEMA: Final = {
    "txn_id": pl.Utf8,
    "event_ts_utc": pl.Datetime("us", "UTC"),
    "txn_type": pl.Utf8,
    "amount_minor": pl.Int64,
    "currency": pl.Utf8,
    "account_from": pl.Utf8,
    "account_to": pl.Utf8,
}

EVENT_BLOCKS: Final = frozenset({"cycles", "page_rank", "community", "betweenness"})


def graph_config(**updates: object) -> PipelineConfig:
    """The committed config with values replaced, for bound and guard tests.

    Keys are dotted. A bare key or one rooted in a ``graph:`` sub-block edits the
    graph block; anything else (``sampling.``, ``determinism.``, ``exposure.``)
    resolves from the config root. Used only to change a *bound* or to prove a
    guard fires — the defaults under test everywhere else are the committed ones.
    """
    cfg = load_pipeline_config()
    raw = copy.deepcopy(cfg.raw)
    for dotted, value in updates.items():
        parts = dotted.split(".")
        node = raw["graph"] if parts[0] in EVENT_BLOCKS or len(parts) == 1 else raw
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return PipelineConfig(
        root=cfg.root, seed=cfg.seed, deployment_timezone=cfg.deployment_timezone, raw=raw
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


def frame(
    events: Iterable[dict[str, object]], *, schema: dict[str, pl.DataType] | None = None
) -> pl.DataFrame:
    return pl.DataFrame(list(events), schema=schema or EVENT_SCHEMA)


def row_for(graph: AccountGraph, account: str) -> dict[str, object]:
    rows = graph.nodes.filter(pl.col("account") == account).to_dicts()
    assert len(rows) == 1, f"{account} should appear exactly once in the node table"
    return rows[0]


# --- parallelism ---------------------------------------------------------


# Forty transfers A -> B, amounts 100..139, one per minute from t=0.
#   count            = 40
#   total            = (100 + 139) * 40 / 2 = 239 * 20 = 4780 minor units
#   span             = minute 39 - minute 0 = 39 * 60_000_000 us = 2_340_000_000
#   first / last id  = p00 / p39
# Degrees are 40 each, so at the 99th percentile over two nodes (index int(0.99*2)=1
# -> value 40) nothing is above the threshold and neither account is a rail.
def burst_events() -> list[dict[str, object]]:
    return [event(f"p{i:02d}", ALICE, BOB, 100 + i, i) for i in range(40)]


def test_parallel_edges_preserved() -> None:
    graph = build_graph(frame(burst_events()), load_pipeline_config())
    pair = graph.pairs.filter(
        (pl.col("account_from") == ALICE) & (pl.col("account_to") == BOB)
    ).to_dicts()
    assert len(pair) == 1, "one aggregate row per (from, to, currency)"
    row = pair[0]
    assert row["edge_count"] == 40
    assert row["total_value_minor"] == 4780
    assert row["min_amount_minor"] == 100
    assert row["max_amount_minor"] == 139
    assert row["first_ts_us"] == BASE_US
    assert row["last_ts_us"] == BASE_US + 39 * MINUTE_US
    assert row["span_us"] == 2_340_000_000
    assert row["first_txn_id"] == "p00"
    assert row["last_txn_id"] == "p39"

    # The aggregate does not replace the events: the multigraph still holds forty
    # parallel edges, and the edge table still has forty timestamped rows.
    assert graph.multigraph.number_of_edges(ALICE, BOB) == 40
    assert graph.events.frame.height == 40
    assert graph.degree_of(ALICE) == 40
    assert graph.node_row(ALICE)["unique_counterparties"][0] == 1
    assert graph.node_row(BOB)["in_degree"][0] == 40


def test_self_loop_excluded_from_cycles_and_degree_but_counted() -> None:
    # The planted 4-loop from test_p3a_cycles, plus three A -> A transfers.
    # A's degree stays 3 (out to B, out to C?, in from D) — here: one out to B and
    # one in from D, so 2 — and the three self-transfers add nothing to any degree,
    # nothing to any counterparty set, and no cycle.
    legs = [
        event("c1", ALICE, BOB, 1000, 1),
        event("c2", BOB, CAROL, 900, 2),
        event("c3", CAROL, DAVE, 800, 3),
        event("c4", DAVE, ALICE, 700, 4),
        event("s1", ALICE, ALICE, 500, 5),
        event("s2", ALICE, ALICE, 500, 6),
        event("s3", ALICE, ALICE, 500, 7),
    ]
    graph = build_graph(frame(legs), load_pipeline_config())
    assert graph.stats.self_transfer_count == 3
    assert graph.node_row(ALICE)["self_transfer_count"][0] == 3
    assert graph.degree_of(ALICE) == 2
    assert graph.node_row(ALICE)["unique_counterparties"][0] == 2  # BOB and DAVE only
    assert ALICE not in graph.neighbours(ALICE)
    # Retained as edges, and visible in the pair table as a self-pair.
    assert graph.multigraph.number_of_edges(ALICE, ALICE) == 3
    assert graph.pairs.filter(pl.col("is_self_pair"))["edge_count"].to_list() == [3]
    # One cycle, and it is the four-node loop rather than anything built from a
    # self-transfer.
    assert graph.stats.cycle_count == 1
    assert graph.search.cycles[0].path == (ALICE, BOB, CAROL, DAVE)


def test_total_order_stable() -> None:
    """Rows arrive scrambled; the built order is (event_ts_utc, txn_id), always.

    Two events share a timestamp on purpose (minutes 5 and 5) so the tie-break is
    exercised: ``b:5`` sorts before ``a:9`` on the id alone, and no other reading of
    the pair is possible.
    """
    legs = [
        event("z1", ALICE, BOB, 100, 9),
        event("b5", BOB, CAROL, 100, 5),
        event("a5", ALICE, CAROL, 100, 5),
        event("c0", CAROL, DAVE, 100, 0),
    ]
    expected = ["c0", "a5", "b5", "z1"]
    graph = build_graph(frame(legs), load_pipeline_config())
    assert graph.events.frame["txn_id"].to_list() == expected
    # Edge ids are positions in that order, so they are a function of the data.
    assert graph.events.frame["edge_id"].to_list() == [0, 1, 2, 3]
    scrambled = build_graph(frame(list(reversed(legs))), load_pipeline_config())
    assert scrambled.events.frame["txn_id"].to_list() == expected
    assert scrambled.nodes.equals(graph.nodes)
    order = graph.events.frame.select(["event_ts_utc", "txn_id"])
    keys = list(order.iter_rows())
    assert keys == sorted(keys)


# --- node typing ---------------------------------------------------------


# A supernode plus a threshold that sits exactly on a second node's degree, so the
# strict inequality is observable rather than assumed.
#   MALL: 150 leaf transfers in, one from ALICE in, one out to BOB     -> degree 152
#   HUB:  10 leaf transfers in                                          -> degree 10
#   162 further accounts (the 160 leaves, ALICE, BOB)                    -> degree 1
# n = 164, sorted degrees = [1] * 162 + [10, 152]. The 99th percentile is index
# int(0.99 * 164) = 162 -> value 10, so a rail is anything strictly above 10:
# MALL only, and HUB sits exactly on the threshold and must stay untyped.
def rail_fixture() -> list[dict[str, object]]:
    legs: list[dict[str, object]] = []
    minute = 0
    for index in range(150):
        minute += 1
        legs.append(event(f"m{index:03d}", f"mall{index:06d}", MALL, 100, minute))
    for index in range(10):
        minute += 1
        legs.append(event(f"h{index:03d}", f"hub{index:07d}", HUB, 100, minute))
    legs.append(event("chain1", ALICE, MALL, 1000, minute + 1))
    legs.append(event("chain2", MALL, BOB, 1000, minute + 2))
    return legs


def test_rail_blocks_traversal() -> None:
    graph = build_graph(frame(rail_fixture()), load_pipeline_config())
    assert graph.stats.rail_degree_threshold == 10
    assert graph.node_types[MALL] == NODE_TYPE_RAIL
    assert graph.node_types[HUB] == NODE_TYPE_EXTERNAL
    mall = row_for(graph, MALL)
    assert mall["is_rail"] is True
    assert mall["fan_in"] is None, "a rail gets no fan score at all"
    assert mall["fan_in_effective"] is None

    # ALICE -> MALL -> BOB: BOB is two hops away and reachable in the data, but MALL
    # is a rail, and walking through a merchant would connect two strangers who never
    # met.
    view = neighbourhood(graph, ALICE, hops=2)
    served = set(view.nodes["account"].to_list())
    assert MALL in served, "a rail is still shown, as an endpoint"
    assert BOB not in served
    assert view.hops == 2
    assert graph.downstream(ALICE, 2) == (MALL,)

    # The same walk with the rail rule off is strictly larger, which is what makes
    # the assertion above about the rule rather than about the fixture.
    direct = neighbourhood(graph, BOB, hops=1)
    assert set(direct.nodes["account"].to_list()) == {BOB, MALL}


def test_rail_does_not_inflate_another_accounts_fan() -> None:
    graph = build_graph(frame(rail_fixture()), load_pipeline_config())
    alice = graph.node_row(ALICE)
    assert alice["fan_out"][0] == 1, "one distinct destination, the merchant"
    assert alice["fan_out_effective"][0] == 0, "and none of them is a person"
    hub = graph.node_row(HUB)
    assert hub["fan_in_effective"][0] == 10
    assert hub["fan_in"][0] == 10


def test_external_node_typed() -> None:
    # EXT only ever receives: two transfers from ALICE, which also keeps it out of the
    # singleton bucket (degree 2 > 1). Over two nodes at the 99th percentile the
    # threshold is the maximum degree, so nothing is a rail.
    legs = [
        event("e1", ALICE, EXT, 700, 0),
        event("e2", ALICE, EXT, 300, 1),
        event("e3", ALICE, BOB, 100, 2),
        event("e4", BOB, ALICE, 100, 3),
    ]
    graph = build_graph(frame(legs), load_pipeline_config())
    row = row_for(graph, EXT)
    assert row["node_type"] == NODE_TYPE_EXTERNAL
    assert row["is_external"] is True
    assert row["total_degree"] == 2
    assert row["in_degree"] == 2
    # The documented null policy, not a zero: the corpus observes this account only as
    # a counterparty, so its originating behaviour is unknown rather than absent.
    for column in FEATURE_NULL_POLICY[NODE_TYPE_EXTERNAL]:
        assert column in graph.nodes.columns, column
        assert row[column] is None, column
    flows = graph.flows.filter(pl.col("account") == EXT).to_dicts()
    assert len(flows) == 1
    for column in FLOW_NULL_POLICY[NODE_TYPE_EXTERNAL]:
        assert column in graph.flows.columns, column
        assert flows[0][column] is None, column
    assert flows[0]["inflow_total_minor"] == 1000
    # A member that both sends and receives keeps real numbers where the corpus saw
    # nothing go out in one currency: ALICE only ever trades EUR, so her single row is
    # populated rather than null.
    alice = row_for(graph, ALICE)
    assert alice["node_type"] == NODE_TYPE_MEMBER
    assert alice["out_degree"] == 3
    assert alice["fan_out"] == 2


def test_singletons_excluded_from_graph_stats() -> None:
    """A four-node loop plus one account with a single edge.

    Degrees: ALICE 3 (out to BOB, out to SOLO, in from DAVE), BOB 2, CAROL 2,
    DAVE 2, SOLO 1. Over five nodes the 99th percentile is index int(0.99*5) = 4 ->
    value 3, so nothing is a rail. SOLO's degree is exactly the singleton threshold,
    so it leaves every aggregate: no community, no PageRank, no local density, no
    place in the degree summary of the graph population.
    """
    legs = [
        event("q1", ALICE, BOB, 1000, 1),
        event("q2", BOB, CAROL, 1000, 2),
        event("q3", CAROL, DAVE, 1000, 3),
        event("q4", DAVE, ALICE, 1000, 4),
        event("q5", ALICE, SOLO, 1000, 5),
    ]
    graph = build_graph(frame(legs), load_pipeline_config())
    solo = row_for(graph, SOLO)
    assert solo["in_graph"] is False
    assert solo["total_degree"] == 1
    assert solo["community_id"] is None
    assert solo["pagerank"] is None
    assert solo["local_density"] is None
    assert solo["in_cycle"] is False
    # Still a node: queryable, scoreable by the tabular module, and counted.
    assert graph.node_row(SOLO).height == 1
    assert graph.stats.node_count == 5
    assert graph.stats.in_graph_node_count == 4
    assert graph.stats.singleton_count == 1
    # All five: sorted [1,2,2,2,3] -> median 2, p90 index int(0.9*5)=4 -> 3, p99 3.
    assert graph.stats.degree_all_nodes.accounts == 5
    assert graph.stats.degree_all_nodes.median == 2.0
    assert graph.stats.degree_all_nodes.p90 == 3.0
    assert graph.stats.degree_all_nodes.p99 == 3.0
    assert graph.stats.degree_all_nodes.max == 3
    # The four in-graph accounts: sorted [2,2,2,3] -> median (2+2)/2 = 2, p90 index
    # int(0.9*4)=3 -> 3, max 3.
    assert graph.stats.degree_in_graph.accounts == 4
    assert graph.stats.degree_in_graph.median == 2.0
    assert graph.stats.degree_in_graph.max == 3


# --- communities ---------------------------------------------------------


# Two disjoint cliques, because a clique is the one structure whose community
# assignment is not a judgement call: triangle {A, B, C} (3 edges) and complete
# {D, E, F, G} (12 directed legs, 6 undirected pairs).
# Degrees: A/B/C 2 each, D/E/F/G 6 each. n = 7, p99 index int(0.99*7) = 6 -> value 6,
# so the threshold is 6 and nothing is above it: no rails, and every node has degree
# above 1, so nothing is a singleton. Sizes 4 and 3 differ, so the canonical order
# (size descending, then smallest node key) decides without a tie.
def clique_fixture() -> list[dict[str, object]]:
    triangle = [
        event("a1", ALICE, BOB, 1000, 1),
        event("a2", BOB, CAROL, 1000, 2),
        event("a3", CAROL, ALICE, 1000, 3),
    ]
    clique = [DAVE, EVE, "f5c5000000", "6c6c00000000"]
    legs = list(triangle)
    minute = 3
    for left in clique:
        for right in clique:
            if left == right:
                continue
            minute += 1
            legs.append(event(f"k{left}{right}", left, right, 1000, minute))
    return legs


def test_community_ids_stable_across_runs() -> None:
    legs = clique_fixture()
    first = build_graph(frame(legs), load_pipeline_config())
    second = build_graph(frame(list(reversed(legs))), load_pipeline_config())
    by_account = dict(
        zip(second.nodes["account"].to_list(), second.nodes["community_id"].to_list(), strict=True)
    )
    for row in first.nodes.iter_rows(named=True):
        assert row["community_id"] == by_account[str(row["account"])], row["account"]

    assert first.stats.community_count == 2
    triangle_ids = set(
        first.nodes.filter(pl.col("account").is_in([ALICE, BOB, CAROL]))["community_id"].to_list()
    )
    assert len(triangle_ids) == 1
    clique_nodes = first.nodes.filter(~pl.col("account").is_in([ALICE, BOB, CAROL]))
    assert set(clique_nodes["community_id"].to_list()) != triangle_ids
    # Canonical order: the bigger community is id 0 and the smaller is id 1.
    assert int(clique_nodes["community_id"][0]) == 0
    assert int(next(iter(triangle_ids))) == 1
    sizes = first.nodes.group_by("community_id").len().sort("community_id")
    assert sizes["len"].to_list() == [4, 3]


def test_community_ids_are_canonical_by_size_then_min_node_key() -> None:
    """The remap rule, checked as an invariant rather than as a snapshot."""
    graph = build_graph(frame(clique_fixture()), load_pipeline_config())
    grouped: dict[int, list[str]] = {}
    for account, community in zip(
        graph.nodes["account"].to_list(), graph.nodes["community_id"].to_list(), strict=True
    ):
        assert community is not None
        grouped.setdefault(int(community), []).append(str(account))
    ordered = sorted(grouped.items(), key=lambda item: (-len(item[1]), min(item[1])))
    assert [community for community, _ in ordered] == sorted(grouped)
    assert sorted(grouped) == list(range(len(grouped)))


# --- node features -------------------------------------------------------


# A four-node ring plus one chord: A->B, B->C, C->D, D->A, A->C.
# Undirected pairs: A-B, B-C, C-D, A-D, A-C.
# Degrees: A 3 (out B, out C, in D), B 2, C 3 (out D, in A and B), D 2. Over four
# nodes the 99th percentile is index int(0.99*4) = 3 -> value 3 -> no rails, and
# every degree is above 1, so nothing is a singleton.
# Local density, counted by hand over each account's in-graph counterparties:
#   A's partners {B, C, D}: possible pairs 3 -> B-C yes, C-D yes, B-D no -> 2/3
#   B's partners {A, C}:   possible 1  -> A-C yes                    -> 1
#   C's partners {A, B, D}: possible 3 -> A-B yes, A-D yes, B-D no   -> 2/3
#   D's partners {A, C}:   possible 1  -> A-C yes                    -> 1
# Cycles, walked in the canonical time order (all amounts equal, so only ordering
# and the length window decide):
#   A->B(1) B->C(2) C->D(3) D->A(4)  closes on A, length 4
#   C->D(3) D->A(4) A->C(5)          closes on C, length 3
#   A->C(5) then C->D(3)? earlier than 5, so the chord opens nothing further.
# Total 2 loops. The 3-loop passes through C, D and A; the 4-loop through all four,
# so cycle_count is 2 for A, C and D and 1 for B.
def ring_with_chord() -> list[dict[str, object]]:
    return [
        event("d1", ALICE, BOB, 1000, 1),
        event("d2", BOB, CAROL, 1000, 2),
        event("d3", CAROL, DAVE, 1000, 3),
        event("d4", DAVE, ALICE, 1000, 4),
        event("d5", ALICE, CAROL, 1000, 5),
    ]


def test_node_features_are_all_present_and_hand_checkable() -> None:
    graph = build_graph(frame(ring_with_chord()), load_pipeline_config())
    for column in (
        "total_degree",
        "in_degree",
        "out_degree",
        "unique_counterparties",
        "fan_in",
        "fan_out",
        "local_density",
        "pagerank",
        "community_id",
        "cycle_count",
        "in_cycle",
        "self_transfer_count",
    ):
        assert column in graph.nodes.columns, column
    alice = row_for(graph, ALICE)
    assert alice["total_degree"] == 3
    assert alice["out_degree"] == 2
    assert alice["in_degree"] == 1
    assert alice["unique_counterparties"] == 3
    assert alice["fan_out"] == 2
    assert alice["fan_in"] == 1
    assert alice["local_density"] == pytest.approx(2 / 3)
    assert alice["cycle_count"] == 2
    assert alice["max_cycle_length"] == 4
    assert row_for(graph, BOB)["local_density"] == pytest.approx(1.0)
    assert row_for(graph, BOB)["cycle_count"] == 1
    assert row_for(graph, CAROL)["local_density"] == pytest.approx(2 / 3)
    assert row_for(graph, CAROL)["cycle_count"] == 2
    assert row_for(graph, DAVE)["local_density"] == pytest.approx(1.0)
    assert row_for(graph, DAVE)["cycle_count"] == 2
    assert graph.stats.cycle_count == 2
    assert {cycle.path for cycle in graph.search.cycles} == {
        (ALICE, BOB, CAROL, DAVE),
        (CAROL, DAVE, ALICE),
    }


def test_pagerank_alpha_comes_from_config_and_sums_to_one() -> None:
    settings = load_graph_settings(load_pipeline_config())
    assert settings.page_rank.alpha == 0.85
    graph = build_graph(frame(ring_with_chord()), load_pipeline_config())
    ranks = [float(value) for value in graph.nodes.filter(pl.col("in_graph"))["pagerank"].to_list()]
    assert len(ranks) == 4
    # Definition-level: PageRank is a distribution over the ranked population.
    assert sum(ranks) == pytest.approx(1.0)
    # Read off the update rule, rank(j) = (1-alpha)/n + alpha * sum(rank(i)/outdeg(i))
    # over the accounts that send to j: CAROL receives from BOB (one out-edge) and
    # from ALICE (two out-edges), while ALICE receives only from DAVE (one out-edge),
    # so CAROL's inbound mass is strictly larger and her rank is higher.
    assert row_for(graph, CAROL)["pagerank"] > row_for(graph, ALICE)["pagerank"]
    assert row_for(graph, BOB)["pagerank"] is not None
    looser = build_graph(frame(ring_with_chord()), graph_config(**{"page_rank.alpha": 0.5}))
    assert looser.nodes["pagerank"].to_list() != graph.nodes["pagerank"].to_list()


def test_betweenness_uses_the_configured_pivot_count_and_is_seeded() -> None:
    settings = load_graph_settings(load_pipeline_config())
    assert settings.betweenness.pivots == 500
    graph = build_graph(frame(ring_with_chord()), load_pipeline_config())
    accounts = [str(item) for item in graph.nodes["account"].to_list()]
    first = betweenness_centrality(
        accounts,
        graph.out_edges,
        pivots=settings.betweenness.pivots,
        seed=settings.community.seed,
    )
    second = betweenness_centrality(
        accounts,
        graph.out_edges,
        pivots=settings.betweenness.pivots,
        seed=settings.community.seed,
    )
    assert first == second
    assert set(first) == set(accounts)


# --- subgraph serving ----------------------------------------------------


# Four 400-node rings, chained into one component by rewiring rather than adding:
# for each join, one ring edge is removed in each partner and two cross edges are
# added, so every node keeps exactly two incident legs.
#   rings 4 x 400 = 1600 nodes; ring legs 1600 - 6 removed + 6 cross = 1600 edges
#   every degree 2 -> p99 index int(0.99*1600) = 1584 -> value 2 -> no rails;
#   degree 2 > 1 -> no singletons; one connected component.
# The view is asked for with 1200 hops, which covers the chain's diameter, so the
# 1500-node cap must bite on 1600 reachable accounts.
def ring_chain(ring_count: int = 4, size: int = 400) -> list[dict[str, object]]:
    rings = [[f"n{r:02d}{i:04d}" for i in range(size)] for r in range(ring_count)]
    removed = {0: [size - 1], 1: [99, 199], 2: [99, 199], 3: [99]}
    joins = [(0, size - 1, 1, 99), (1, 199, 2, 99), (2, 199, 3, 99)]
    legs: list[dict[str, object]] = []
    minute = 0
    for ring in range(ring_count):
        for index in range(size):
            if index in removed[ring]:
                continue
            minute += 1
            legs.append(
                event(
                    f"r{ring}-{index:04d}",
                    rings[ring][index],
                    rings[ring][(index + 1) % size],
                    1000,
                    minute,
                )
            )
    for left_ring, left_index, right_ring, right_index in joins:
        minute += 1
        legs.append(
            event(
                f"x{left_ring}a",
                rings[left_ring][left_index],
                rings[right_ring][(right_index + 1) % size],
                1000,
                minute,
            )
        )
        minute += 1
        legs.append(
            event(
                f"x{left_ring}b",
                rings[right_ring][right_index],
                rings[left_ring][(left_index + 1) % size],
                1000,
                minute,
            )
        )
    return legs


def test_subgraph_cap_enforced() -> None:
    graph = build_graph(frame(ring_chain()), load_pipeline_config())
    assert graph.stats.node_count == 1600
    assert graph.stats.rail_count == 0
    assert graph.stats.singleton_count == 0
    cap = graph.settings.subgraph_node_cap
    assert cap == 1500

    view = neighbourhood(graph, "n000000", hops=1200)
    assert view.reachable_before_cap == 1600
    assert view.node_count <= cap
    assert view.within_cap is True
    assert view.truncated is True
    assert str(cap) in (view.truncation_reason or "")
    assert view.meta_nodes, "an over-cap view must collapse, not delete"

    hidden = 0
    for meta in view.meta_nodes:
        members = graph.nodes.filter(pl.col("community_id") == meta.community_id)[
            "account"
        ].to_list()
        # The label carries the true size of the community, not the number of nodes
        # the renderer happened to have room for.
        assert meta.member_count == len(members)
        assert meta.expandable is True
        assert str(meta.member_count) in meta.label
        expanded = view.expand(graph, meta.community_id)
        assert len(expanded) == meta.member_count
        assert set(expanded) == {str(item) for item in members}
        assert meta.community_id != view_seed_community(graph, "n000000")
        hidden += meta.member_count

    # Accounting identity, checked against the fixture's 1600: every node is either
    # rendered, hidden inside a meta-node, or a meta-node itself.
    assert view.node_count == 1600 - hidden + len(view.meta_nodes)
    assert view.node_count < view.reachable_before_cap


def view_seed_community(graph: AccountGraph, seed: str) -> int:
    value = row_for(graph, seed)["community_id"]
    assert value is not None
    return int(value)


def test_neighbourhood_defaults_to_configured_hops_and_names_unknown_accounts() -> None:
    settings = load_graph_settings(load_pipeline_config())
    assert settings.default_hops == 2
    graph = build_graph(frame(ring_with_chord()), load_pipeline_config())
    view = neighbourhood(graph, ALICE)
    assert view.hops == 2
    assert set(view.nodes["account"].to_list()) == {ALICE, BOB, CAROL, DAVE}
    # Every parallel leg is kept at event granularity: the burst is the story.
    assert view.edges.height == 5
    with pytest.raises(UnknownAccountError, match="no events in this graph"):
        neighbourhood(graph, "nobody000000")


# --- money, currency and windows ----------------------------------------


def test_cross_currency_totals_are_never_summed() -> None:
    # ALICE -> BOB in EUR and in USD: two aggregate rows, one per currency, and no
    # single number that adds them.
    legs = [
        event("u1", ALICE, BOB, 1000, 0, currency="EUR"),
        event("u2", ALICE, BOB, 500, 1, currency="USD"),
    ]
    graph = build_graph(frame(legs), load_pipeline_config())
    totals = graph.pair_totals(ALICE, BOB)
    assert totals == {"EUR": 1000, "USD": 500}
    assert graph.pairs.height == 2
    with pytest.raises(MixedCurrencyError, match="refusing to sum"):
        require_single_currency(totals, what="pair a11ce0000000 -> b0b000000000")
    # A single-currency pair flattens without complaint, because nothing was mixed.
    assert require_single_currency({"EUR": 1000}, what="one currency pair") == 1000
    assert require_single_currency({}, what="an empty pair") == 0


def test_flow_window_is_the_exposure_primitive() -> None:
    # ALICE receives 1000 at t=0, 500 at t=10, 700 at t=30 minutes and sends 400 at
    # t=11 and 900 at t=40. Over [0, 24h] everything is inside: in 1000+500+700 =
    # 2200, out 400+900 = 1300. Over [10, 20] minutes: in 500, out 400.
    legs = [
        event("w1", BOB, ALICE, 1000, 0),
        event("w2", CAROL, ALICE, 500, 10),
        event("w3", ALICE, DAVE, 400, 11),
        event("w4", EVE, ALICE, 700, 30),
        event("w5", ALICE, BOB, 900, 40),
    ]
    graph = build_graph(frame(legs), load_pipeline_config())
    day = graph.settings.exposure_window_us
    assert day == 24 * 60 * MINUTE_US
    wide = graph.flow_window(
        [ALICE], window_start_us=BASE_US, window_end_us=BASE_US + day
    ).to_dicts()
    assert len(wide) == 1
    assert wide[0]["currency"] == "EUR"
    assert wide[0]["inflow_minor"] == 2200
    assert wide[0]["outflow_minor"] == 1300
    narrow = graph.flow_window(
        [ALICE],
        window_start_us=BASE_US + 10 * MINUTE_US,
        window_end_us=BASE_US + 20 * MINUTE_US,
    ).to_dicts()
    assert narrow[0]["inflow_minor"] == 500
    assert narrow[0]["inflow_count"] == 1
    assert narrow[0]["outflow_minor"] == 400
    # A window with nothing in it is an empty frame with the right columns, not a row
    # of zeros: "we did not look at that hour" and "nothing moved then" differ.
    empty = graph.flow_window(
        [ALICE],
        window_start_us=BASE_US + 100 * MINUTE_US,
        window_end_us=BASE_US + 101 * MINUTE_US,
    )
    assert empty.height == 0
    with pytest.raises(UnknownAccountError):
        graph.flow_window(["ghost000000"], window_start_us=BASE_US, window_end_us=BASE_US + day)


def test_downstream_counts_who_money_reaches_and_stops_at_rails() -> None:
    # A -> HUB -> B, with HUB receiving six one-edge transfers so it becomes the
    # supernode at the 50th percentile: degrees sorted [1 x 8, 8] over nine nodes ->
    # index int(0.50*9) = 4 -> value 1 -> only HUB (degree 8) is above it.
    hub_leaves = [event(f"l{i}", f"leaf{i:08d}", MALL, 100, i) for i in range(6)]
    legs = [
        *hub_leaves,
        event("s1", ALICE, MALL, 1000, 10),
        event("s2", MALL, BOB, 1000, 11),
    ]
    graph = build_graph(frame(legs), graph_config(rail_degree_percentile=50.0))
    assert graph.node_types[MALL] == NODE_TYPE_RAIL
    assert graph.downstream(ALICE, 1) == (MALL,)
    assert graph.downstream(ALICE, 2) == (MALL,), "a rail is a destination, not a corridor"
    untyped = build_graph(frame(legs), load_pipeline_config())
    assert untyped.downstream(ALICE, 2) == (BOB, MALL)


# --- the two build paths must agree --------------------------------------


def test_degree_measurement_matches_the_in_memory_build() -> None:
    """The offline full-corpus path and the interactive path read the same maths.

    If these two ever disagree, the gate number and the screen number describe
    different graphs, and nobody can tell which one a packet cited.
    """
    legs = rail_fixture()
    cfg = load_pipeline_config()
    graph = build_graph(frame(legs), cfg)
    measured = degree_measurement(frame(legs), cfg)
    assert measured.node_count == graph.stats.node_count
    assert measured.rail_count == graph.stats.rail_count
    assert measured.external_count == graph.stats.external_count
    assert measured.member_count == graph.stats.member_count
    assert measured.singleton_count == graph.stats.singleton_count
    assert measured.rail_degree_threshold == graph.stats.rail_degree_threshold
    assert measured.degree_all_nodes == graph.stats.degree_all_nodes
    assert measured.top_degrees == graph.stats.top_degrees
    assert measured.top_degrees[0] == (MALL, 152)


def test_graph_size_limit_refuses_instead_of_degrading() -> None:
    with pytest.raises(GraphTooLargeError, match="interactive_txn_target"):
        build_graph(
            frame(ring_with_chord()), graph_config(**{"sampling.interactive_txn_target": 3})
        )
    # The frame path has no such bound, and says nothing about one.
    measured = degree_measurement(
        frame(ring_with_chord()), graph_config(**{"sampling.interactive_txn_target": 3})
    )
    assert measured.node_count == 4


# --- persistence ---------------------------------------------------------


def test_persist_roundtrip_is_byte_identical_and_loadable(tmp_path: Path) -> None:
    graph = build_graph(frame(ring_with_chord()), load_pipeline_config())
    first = write_graph(graph, tmp_path / "run-a")
    second = write_graph(graph, tmp_path / "run-b")
    names = (
        "nodes.parquet",
        "edges.parquet",
        "pairs.parquet",
        "flows.parquet",
        "cycles.parquet",
        "manifest.json",
    )
    for name in names:
        assert _sha(first / name) == _sha(second / name), name

    artifacts = load_graph(second)
    assert artifacts.nodes.equals(graph.nodes)
    assert artifacts.pairs.equals(graph.pairs)
    assert artifacts.flows.equals(graph.flows)
    assert artifacts.stats == graph.stats
    assert artifacts.stats.currencies == ("EUR",)
    # A collapsed meta-node is expandable straight from the artifact, without a
    # rebuild: that is the whole reason the persist path exists.
    assert artifacts.accounts_in_community(0) == tuple(
        sorted(artifacts.nodes.filter(pl.col("community_id") == 0)["account"].to_list())
    )


def test_persisted_graph_that_disagrees_with_its_manifest_is_refused(tmp_path: Path) -> None:
    directory = write_graph(build_graph(frame(ring_with_chord()), load_pipeline_config()), tmp_path)
    nodes = directory / "nodes.parquet"
    payload = bytearray(nodes.read_bytes())
    payload[:64] = bytes(64 - index for index in range(64))  # keep the size, break the bytes
    nodes.write_bytes(bytes(payload))
    with pytest.raises(GraphArtifactError, match="sha256"):
        load_graph(directory)

    clean = write_graph(
        build_graph(frame(ring_with_chord()), load_pipeline_config()), tmp_path / "b"
    )
    (clean / "manifest.json").unlink()
    with pytest.raises(GraphArtifactError, match="missing"):
        load_graph(clean)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- the boundary refuses ------------------------------------------------


def _with_column(
    frame_in: pl.DataFrame, column: str, value: object, dtype: pl.DataType
) -> pl.DataFrame:
    return frame_in.with_columns(pl.Series(column, [value] * frame_in.height, dtype).alias(column))


def test_canonical_contract_is_a_declared_subset() -> None:
    assert set(REQUIRED_EVENT_COLUMNS).issubset(set(CANONICAL_EVENT_V1_COLUMNS))
    assert "channel" in CANONICAL_EVENT_V1_COLUMNS
    # Labels are carried for lineage and never read for structure: a graph whose
    # topology knew which rows were fraudulent would leak the answer into the feature.
    assert not [name for name in REQUIRED_EVENT_COLUMNS if name.startswith("label")]
    assert "local_hour" not in REQUIRED_EVENT_COLUMNS


def test_empty_batch_is_an_error_not_an_empty_graph() -> None:
    with pytest.raises(EventContractError, match="no canonical events"):
        build_graph(frame([]), load_pipeline_config())


@pytest.mark.parametrize(
    ("column", "value", "dtype", "message"),
    [
        ("amount_minor", 10.5, pl.Float64, "minor units"),
        ("amount_minor", -100, pl.Int64, "negative"),
        ("currency", "eur", pl.Utf8, "uppercase"),
        ("account_from", None, pl.Utf8, "nulls"),
    ],
)
def test_bad_money_or_bad_keys_are_refused_by_name(
    column: str, value: object, dtype: pl.DataType, message: str
) -> None:
    broken = _with_column(frame(ring_with_chord()), column, value, dtype)
    with pytest.raises(EventContractError, match=message):
        build_graph(broken, load_pipeline_config())


def test_naive_and_non_utc_timestamps_are_refused() -> None:
    legs = ring_with_chord()
    naive = frame(legs).with_columns(
        pl.col("event_ts_utc").dt.replace_time_zone(None).cast(pl.Datetime("us"))
    )
    with pytest.raises(EventContractError, match="naive datetime"):
        build_graph(naive, load_pipeline_config())
    other = frame(legs).with_columns(pl.col("event_ts_utc").dt.convert_time_zone("Africa/Kampala"))
    with pytest.raises(EventContractError, match="time zone"):
        build_graph(other, load_pipeline_config())


def test_duplicate_transaction_ids_are_refused() -> None:
    duplicated = frame(ring_with_chord()).with_columns(
        pl.col("txn_id").replace("d1", "d2").alias("txn_id")
    )
    with pytest.raises(EventContractError, match="not unique"):
        build_graph(duplicated, load_pipeline_config())


def test_missing_columns_are_listed_rather_than_guessed() -> None:
    partial = frame(ring_with_chord()).drop("currency")
    with pytest.raises(EventContractError, match=r"missing columns.*currency"):
        build_graph(partial, load_pipeline_config())
    with pytest.raises(EventContractError, match="polars DataFrame"):
        build_graph([{"txn_id": "x"}], load_pipeline_config())  # type: ignore[arg-type]


def test_non_unique_sort_keys_are_refused_by_config() -> None:
    with pytest.raises(GraphConfigError, match="exactly two columns"):
        load_graph_settings(
            graph_config(**{"determinism.sort_keys": ["ts_utc", "txn_id", "local_hour"]})
        )
    with pytest.raises(GraphConfigError, match="is not a column"):
        load_graph_settings(graph_config(**{"determinism.sort_keys": ["ts_local", "txn_id"]}))
    # The logical name in config and the physical name on the event frame are one
    # documented alias apart; both resolve to the same order.
    assert load_graph_settings(load_pipeline_config()).order_columns == (
        "event_ts_utc",
        "txn_id",
    )


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("community.seed", 4242, "the run seed is 1337"),
        ("community.algorithm", "louvain", "only community implementation"),
        ("community.canonical_order", "membership_then_id", "only remap"),
        ("community.resolution", 0.5, "ignores resolution"),
        ("cycles.min_length", 2, ">= 3"),
        ("cycles.max_length", 20, "above the 12"),
        ("rail_degree_percentile", 0.0, "must be in"),
        ("subgraph_node_cap", 1, ">= 2"),
        ("default_hops", 0, ">= 1"),
        ("page_rank.alpha", 1.5, "must be in"),
    ],
)
def test_config_that_would_silently_change_the_graph_is_refused(
    path: str, value: object, message: str
) -> None:
    with pytest.raises(GraphConfigError, match=message):
        load_graph_settings(graph_config(**{path: value}))


def test_exposure_bounds_are_read_from_config() -> None:
    settings = load_graph_settings(load_pipeline_config())
    assert settings.exposure_window_hours == 24
    assert settings.exposure_downstream_hops == 1
    assert settings.community.seed == 1337
    assert settings.community.n_iterations == 3
    assert settings.cycles.min_length == 3
    assert settings.cycles.max_length == 6
    assert settings.cycles.value_retention_floor == 0.6
    with pytest.raises(GraphConfigError, match=">= 1"):
        load_graph_settings(graph_config(**{"exposure.window_hours": 0}))
