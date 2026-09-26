"""``build_graph``: canonical events in, typed time-stamped multigraph out.

The shape of the build is a deliberate order of operations, and each step depends
on the previous one being finished, not merely started:

1. validate and canonically order the events (:mod:`oxbow.graph.events`);
2. aggregate — parallel edges are *retained* as individual time-stamped edges and
   summarised per ``(from, to, currency)`` pair, so a burst of 40 transfers is
   both visible as 40 events and quotable as one total (P3a);
3. compute the node skeleton, from which the rail threshold falls out;
4. type nodes (rail / external / member) and mark singletons;
5. run the bounded cycle search, which needs rails excluded to be meaningful;
6. run Leiden, which needs the singleton-excluded population;
7. compute local and global features, which need both of the above;
8. assemble stats, whose ``*_skipped_reason`` fields say out loud whatever the
   build chose not to compute.

Two honesty rules run through the file. Currency is part of every amount, so every
aggregate is keyed by currency and the one function that flattens currencies
refuses when a pair spans two (:func:`oxbow.graph.events.require_single_currency`).
And a corpus with no network (DEV-011: PaySim's median account degree is 1.0) must
produce a graph that *says* it has no network — which is why so much of this module
is about what gets excluded, and why every exclusion is counted rather than dropped.

Self-transfers are the third entry in that list, and the one with a corpus attached:
a canonical row may have ``account_from == account_to`` (DEV-013 — 591,212 of IBM-
AML's 5,078,345 rows are, mostly reinvestments). Rule 01 P3 asks for them to be kept
as a feature while staying out of cycle and fan detection, so this module keeps them
in every frame (events, pairs, flows keyed by currency) and keeps them out of every
adjacency structure, in exactly one place — :func:`_adjacency` — which is also where
the per-account degree is checked against the legs that walk (:func:`degree_measurement`
and :func:`build_graph` must never disagree, and :func:`_check_degree_adjacency`
makes that a build failure rather than a hope).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import polars as pl

from oxbow.config import PipelineConfig
from oxbow.graph.build_frames import (
    build_multigraph,
    build_node_skeleton,
    pair_aggregates,
    self_transfer_totals,
    split_currency_pairs,
)
from oxbow.graph.communities import detect_communities
from oxbow.graph.cycles import enumerate_cycles
from oxbow.graph.errors import GraphError, GraphTooLargeError
from oxbow.graph.events import (
    DERIVED_EDGE_ID_COLUMN,
    DERIVED_SELF_COLUMN,
    DERIVED_TS_COLUMN,
    require_events,
)
from oxbow.graph.features import (
    CycleParticipation,
    GlobalFeatures,
    compute_cycle_participation,
    compute_effective_fan,
    compute_global_features,
    compute_local_density,
)
from oxbow.graph.model import (
    FEATURE_NULL_POLICY,
    FLOW_NULL_POLICY,
    NODE_TYPE_EXTERNAL,
    NODE_TYPE_MEMBER,
    NODE_TYPE_RAIL,
    SINGLETON_DEGREE,
    AccountGraph,
    CycleSearch,
    DegreeSummary,
    EdgeLeg,
    GraphStats,
    percentile_nearest_rank,
    summarize_degrees,
)
from oxbow.graph.settings import GraphSettings, load_graph_settings

# 00 §D day-3 gate: "degree distribution and top-20 degrees printed". Twenty is
# the number the gate names, so it is a constant with a citation rather than a
# magic figure a later screen can quietly widen.
TOP_DEGREES_REPORTED: Final[int] = 20


@dataclass(frozen=True, slots=True)
class DegreeMeasurement:
    """The day-3 gate numbers, computed without any traversal structure.

    This is the path that scales to a full corpus. ``build_graph`` keeps a
    NetworkX multigraph plus an igraph projection because the explorer and the
    cycle search need them, and that is bounded by
    ``sampling.interactive_txn_target``; asking NetworkX to hold six million edges
    is not a slower version of the same thing, it is a different machine. The
    offline full-corpus metrics of ``sampling.full_corpus_metrics`` run here.
    """

    event_count: int
    self_transfer_count: int
    self_transfer_value_minor: tuple[tuple[str, int], ...]
    node_count: int
    rail_count: int
    external_count: int
    member_count: int
    singleton_count: int
    rail_degree_threshold: int
    degree_all_nodes: DegreeSummary
    degree_in_graph: DegreeSummary
    top_degrees: tuple[tuple[str, int], ...]
    settings: GraphSettings


def build_graph(events: pl.DataFrame, cfg: PipelineConfig) -> AccountGraph:
    """Build the typed, time-stamped directed multigraph for one corpus slice.

    Raises :class:`~oxbow.graph.errors.EventContractError` if the frame is not
    canonical event v1, and :class:`~oxbow.graph.errors.GraphTooLargeError` if
    it is larger than the interactive target — never a truncated graph that looks
    complete.
    """
    settings = load_graph_settings(cfg)
    ordered = require_events(events, settings.order_columns)
    if ordered.height > settings.interactive_txn_target:
        raise GraphTooLargeError(
            f"{ordered.height:,} events exceeds sampling.interactive_txn_target "
            f"({settings.interactive_txn_target:,}). build_graph holds a NetworkX "
            "multigraph plus an igraph projection, which is the interactive artifact. "
            "Sample a connected subcorpus per config.sampling and use "
            "degree_measurement() for the full-corpus numbers."
        )

    frame = ordered.frame
    skeleton = build_node_skeleton(frame)
    pairs = pair_aggregates(frame)
    # One computation of "how many rows are relationships", used by the invariant
    # check below and by the stats, so neither can drift from the other.
    edge_count = int(frame.filter(~pl.col(DERIVED_SELF_COLUMN)).height)
    self_value = self_transfer_totals(frame)
    rail_threshold = percentile_nearest_rank(
        skeleton.table["total_degree"], settings.rail_degree_percentile / 100.0
    )

    typed = compute_effective_fan(
        _typed_nodes(skeleton.table, rail_threshold), skeleton.counterparty_links
    )
    node_types: dict[str, str] = {
        str(account): str(kind)
        for account, kind in typed.select(["account", "node_type"]).iter_rows()
    }
    in_graph = frozenset(
        account for account in typed.filter(pl.col("in_graph"))["account"].cast(pl.Utf8).to_list()
    )

    out_edges, in_edges, undirected = _adjacency(frame, node_types)
    multigraph = build_multigraph(typed, out_edges)
    # Degrees come from the frame, legs come from the adjacency, and self-transfers
    # are the one row class where the two could part ways. They may not.
    _check_degree_adjacency(typed, out_edges, in_edges, edge_count=edge_count)

    search = _run_cycle_search(
        out_edges=out_edges,
        undirected=undirected,
        in_graph=in_graph,
        node_types=node_types,
        settings=settings,
    )
    communities = detect_communities(
        nodes=sorted(in_graph),
        weighted_pairs=split_currency_pairs(pairs),
        settings=settings.community,
    )
    density = compute_local_density(node_types.keys(), undirected, in_graph)
    globals_ = compute_global_features(multigraph, in_graph, settings.page_rank)
    participation = compute_cycle_participation(search, node_types.keys())
    complete = _attach_features(
        typed, density.local_density, globals_, participation, communities.ids
    )
    flows = _flows(frame, complete)

    stats = GraphStats(
        event_count=frame.height,
        self_transfer_count=ordered.self_transfer_count,
        self_transfer_value_minor=self_value,
        edge_count=edge_count,
        pair_count=int(pairs.height),
        node_count=complete.height,
        member_count=_count_type(node_types, NODE_TYPE_MEMBER),
        external_count=_count_type(node_types, NODE_TYPE_EXTERNAL),
        rail_count=_count_type(node_types, NODE_TYPE_RAIL),
        singleton_count=int(complete.height - len(in_graph)),
        in_graph_node_count=len(in_graph),
        rail_degree_threshold=rail_threshold,
        rail_degree_percentile=settings.rail_degree_percentile,
        currencies=_currencies(frame),
        cycle_count=search.count,
        cycle_search_truncated=search.truncated,
        cycle_search_reason=search.truncation_reason,
        cycle_visits=search.visits,
        cycle_nodes=len(search.participating_nodes),
        community_count=communities.community_count,
        community_skipped_reason=communities.skipped_reason,
        pagerank_skipped_reason=globals_.skipped_reason,
        degree_all_nodes=summarize_degrees(complete["total_degree"]),
        degree_in_graph=summarize_degrees(complete.filter(pl.col("in_graph"))["total_degree"]),
        top_degrees=top_degrees(complete),
        settings_fingerprint=fingerprint(settings),
    )

    return AccountGraph(
        settings=settings,
        events=ordered,
        nodes=complete,
        pairs=pairs,
        flows=flows,
        search=search,
        multigraph=multigraph,
        stats=stats,
        out_edges=out_edges,
        in_edges=in_edges,
        simple_neighbours=undirected,
        node_types=node_types,
        graph_nodes=in_graph,
    )


def degree_measurement(events: pl.DataFrame, cfg: PipelineConfig) -> DegreeMeasurement:
    """Degree distribution and top degrees over an unbounded corpus.

    Same ordering, typing and singleton rules as :func:`build_graph` — only the
    traversal structures are absent, which is what lets it run over 6.3 M rows.

    The degrees here are the same numbers the traversal path walks, and that is a
    claim rather than a hope: both read ``total_degree`` from the one skeleton built
    by :func:`oxbow.graph.build_frames.build_node_skeleton`, which sums non-self rows
    only, and :func:`_check_degree_adjacency` fails every ``build_graph`` whose
    indexed legs stop matching those counts. Self-transfers are the row class that
    could split the two — they touch an account without giving it a counterparty — so
    they are excluded from the degrees, from the rail threshold above them (an
    account that reinvests into itself a thousand times is not a rail), and from the
    singleton cut, while being counted here by row and by per-currency minor-unit
    total, with the reason in
    :data:`~oxbow.graph.model.SELF_TRANSFER_EXCLUDED_REASON`.
    """
    settings = load_graph_settings(cfg)
    ordered = require_events(events, settings.order_columns)
    frame = ordered.frame
    skeleton = build_node_skeleton(frame)
    rail_threshold = percentile_nearest_rank(
        skeleton.table["total_degree"], settings.rail_degree_percentile / 100.0
    )
    table = _typed_nodes(skeleton.table, rail_threshold)
    node_types = {
        str(account): str(kind)
        for account, kind in table.select(["account", "node_type"]).iter_rows()
    }
    return DegreeMeasurement(
        event_count=frame.height,
        self_transfer_count=ordered.self_transfer_count,
        self_transfer_value_minor=self_transfer_totals(frame),
        node_count=table.height,
        rail_count=_count_type(node_types, NODE_TYPE_RAIL),
        external_count=_count_type(node_types, NODE_TYPE_EXTERNAL),
        member_count=_count_type(node_types, NODE_TYPE_MEMBER),
        singleton_count=int(table.height - int(table["in_graph"].sum())),
        rail_degree_threshold=rail_threshold,
        degree_all_nodes=summarize_degrees(table["total_degree"]),
        degree_in_graph=summarize_degrees(table.filter(pl.col("in_graph"))["total_degree"]),
        top_degrees=top_degrees(table),
        settings=settings,
    )


def _typed_nodes(skeleton: pl.DataFrame, rail_threshold: int) -> pl.DataFrame:
    """Apply rail / external / member typing and the singleton exclusion.

    Rail beats external beats member in ``node_type`` because it is the stronger
    claim — it changes traversal and removes the fan score — while ``is_external``
    keeps the weaker claim true. A merchant that only ever receives is both, and the
    explorer needs both facts.
    """
    return (
        skeleton.with_columns(
            (pl.col("total_degree") > rail_threshold).alias("is_rail"),
            (~pl.col("is_originator")).alias("is_external"),
        )
        .with_columns(
            pl.when(pl.col("is_rail"))
            .then(pl.lit(NODE_TYPE_RAIL))
            .when(pl.col("is_external"))
            .then(pl.lit(NODE_TYPE_EXTERNAL))
            .otherwise(pl.lit(NODE_TYPE_MEMBER))
            .alias("node_type"),
            (pl.col("total_degree") > SINGLETON_DEGREE).alias("in_graph"),
        )
        .with_columns(
            # Documented null policy (FEATURE_NULL_POLICY): an account that only ever
            # appears as a counterparty has no observable originating behaviour, so
            # its origin-side features are null. Zero would claim we watched it send
            # nothing, which is a different statement and one the corpus cannot make.
            pl.when(pl.col("is_external"))
            .then(None)
            .otherwise(pl.col("out_degree"))
            .cast(pl.Int64)
            .alias("out_degree"),
            # Rails get no fan score at all: their fan-in is the size of the economy,
            # which makes a fan-in rule fire on every account simultaneously.
            pl.when(pl.col("is_rail"))
            .then(None)
            .otherwise(pl.col("fan_in"))
            .cast(pl.Int64)
            .alias("fan_in"),
            pl.when(pl.col("is_external") | pl.col("is_rail"))
            .then(None)
            .otherwise(pl.col("fan_out"))
            .cast(pl.Int64)
            .alias("fan_out"),
        )
        .sort("account")
    )


def _attach_features(
    table: pl.DataFrame,
    density: Mapping[str, float | None],
    centrality: GlobalFeatures,
    participation: CycleParticipation,
    communities: Mapping[str, int],
) -> pl.DataFrame:
    """Join the derived node columns, in sorted account order.

    Every value is looked up by key rather than positionally: a positional join
    across five dictionaries is exactly the kind of code that silently shifts one
    column by a row and produces a plausible, wrong table.
    """
    accounts = [str(account) for account in table["account"].to_list()]
    return table.with_columns(
        pl.Series("local_density", [density.get(account) for account in accounts], pl.Float64),
        pl.Series(
            "pagerank", [centrality.pagerank.get(account) for account in accounts], pl.Float64
        ),
        pl.Series("community_id", [communities.get(account) for account in accounts], pl.Int64),
        pl.Series(
            "cycle_count",
            [int(participation.counts.get(account, 0)) for account in accounts],
            pl.Int64,
        ),
        pl.Series(
            "max_cycle_length",
            [participation.max_lengths.get(account) for account in accounts],
            pl.Int64,
        ),
        pl.Series(
            "in_cycle",
            [account in participation.participants for account in accounts],
            pl.Boolean,
        ),
    )


def _adjacency(
    frame: pl.DataFrame, node_types: Mapping[str, str]
) -> tuple[
    Mapping[str, tuple[EdgeLeg, ...]],
    Mapping[str, tuple[EdgeLeg, ...]],
    Mapping[str, frozenset[str]],
]:
    """Index the edge table for traversal, in the canonical total order.

    The frame is already sorted on ``(event_ts_utc, txn_id)``, so appending legs in
    row order yields per-node lists in that same order — which is what lets the
    cycle search binary-search the time-respecting suffix instead of scanning every
    older edge. ``in_edges`` stores the same events seen from the receiving side,
    with ``dst`` replaced by the sender, so a backward query reads like a forward
    one.

    One rule decides the whole function, and it is the only place in this layer that
    applies it: a self-transfer contributes no leg to any of the three structures.
    ``account_from == account_to`` is tested before the leg is built, so it is
    excluded identically whatever the account is typed as — rail, external or member
    — and ``node_types`` is consulted here only to seed the neighbour map with every
    known account. A→A is a movement between two balances of one customer, not a
    counterparty, so it cannot close a loop, join a community, carry betweenness,
    make an account its own downstream, or hold a place in the multigraph that
    PageRank walks. The row is *not* dropped: it stays in ``events``, in ``pairs``
    as a flagged self-pair, in the node table's ``self_transfer_count``, and in
    :class:`~oxbow.graph.model.GraphStats` as a count and a per-currency total
    (01 P3, DEV-013).

    The invariant this keeps intact: a leg enters ``out_edges`` or ``in_edges``
    exactly when the same row was counted in that account's degree by
    :func:`oxbow.graph.build_frames.build_node_skeleton`, because both read the same
    predicate on the same frame. :func:`_check_degree_adjacency` verifies it per
    build rather than trusting it.
    """
    out: dict[str, list[EdgeLeg]] = {}
    incoming: dict[str, list[EdgeLeg]] = {}
    neighbours: dict[str, set[str]] = {account: set() for account in node_types}
    columns = [
        "txn_id",
        DERIVED_TS_COLUMN,
        "account_from",
        "account_to",
        "amount_minor",
        "currency",
        "txn_type",
        DERIVED_EDGE_ID_COLUMN,
        DERIVED_SELF_COLUMN,
    ]
    for txn_id, ts_us, src, dst, amount, currency, txn_type, edge_id, is_self in frame.select(
        columns
    ).iter_rows():
        if bool(is_self):
            # The one self-transfer rule, applied before a leg exists at all: an
            # A -> A row adds nothing to `out`, nothing to `in`, nothing to the
            # neighbour map, whatever A is typed as. Testing it here rather than
            # after the leg is built is what keeps the rail / external / member
            # cases identical — there is no ordering in which one of them could
            # slip a self-edge into a traversal structure.
            continue
        account_from = str(src)
        account_to = str(dst)
        leg = EdgeLeg(
            ts_us=int(ts_us),
            txn_id=str(txn_id),
            dst=account_to,
            amount_minor=int(amount),
            currency=str(currency),
            txn_type=str(txn_type),
            edge_id=int(edge_id),
        )
        out.setdefault(account_from, []).append(leg)
        incoming.setdefault(account_to, []).append(leg._replace(dst=account_from))
        neighbours.setdefault(account_from, set()).add(account_to)
        neighbours.setdefault(account_to, set()).add(account_from)
    return (
        {account: tuple(legs) for account, legs in out.items()},
        {account: tuple(legs) for account, legs in incoming.items()},
        {account: frozenset(partners) for account, partners in neighbours.items()},
    )


def _check_degree_adjacency(
    nodes: pl.DataFrame,
    out_edges: Mapping[str, tuple[EdgeLeg, ...]],
    in_edges: Mapping[str, tuple[EdgeLeg, ...]],
    *,
    edge_count: int,
) -> None:
    """Fail the build if the degree table and the traversal adjacency disagree.

    Two paths answer "how many relationships does this account have":
    :func:`oxbow.graph.build_frames.build_node_skeleton` groups the frame (and is the
    path :func:`degree_measurement` alone runs), while :func:`_adjacency` indexes the
    same frame for walking. They share one predicate — non-self rows only — and
    self-transfers are the only row class that could make them part ways, because a
    self-transfer touches an account without giving it a counterparty. Should they
    ever diverge, the day-3 gate's degree and the explorer's degree would describe
    different graphs and no number in either artifact would say so.

    Cost is O(nodes + legs) over a slice already bounded by
    ``sampling.interactive_txn_target``, so it runs on every build rather than only
    under test. It checks the interactive path against the shared skeleton, which is
    the same table the offline path sums, so it pins both paths to one rule.
    """
    legs_by_account: dict[str, int] = {}
    for origin, legs in out_edges.items():
        legs_by_account[origin] = legs_by_account.get(origin, 0) + len(legs)
    for destination, legs in in_edges.items():
        legs_by_account[destination] = legs_by_account.get(destination, 0) + len(legs)

    mismatched: list[tuple[str, int, int]] = []
    for account, degree in nodes.select(["account", "total_degree"]).iter_rows():
        key = str(account)
        indexed = legs_by_account.pop(key, 0)
        if indexed != int(degree):
            mismatched.append((key, int(degree), indexed))
    if mismatched:
        shown = ", ".join(
            f"{account}: degree {degree} but {indexed} indexed legs"
            for account, degree, indexed in sorted(mismatched)[:5]
        )
        raise GraphError(
            f"{len(mismatched)} accounts' degrees do not match the traversal adjacency "
            f"({shown}). The frame-level count and the indexed legs agreed on different "
            "row sets, which is how a gate number and a screen number quietly stop "
            "describing one graph. Fix the shared self-transfer predicate; do not "
            "reconcile the symptom here."
        )
    if legs_by_account:
        orphans = sorted(legs_by_account)[:5]
        raise GraphError(
            f"the adjacency indexes {len(legs_by_account)} account(s) absent from the "
            f"node table (e.g. {orphans}); a leg with no node row means the two paths "
            "disagree about the population, not just about a count."
        )
    total_legs = sum(len(legs) for legs in out_edges.values()) + sum(
        len(legs) for legs in in_edges.values()
    )
    if total_legs != 2 * edge_count:
        raise GraphError(
            f"the adjacency holds {total_legs} legs where the {edge_count} non-self "
            "events predict exactly two each (one out, one in). Either a self-transfer "
            "got indexed as a relationship or a relationship went missing."
        )


def _run_cycle_search(
    *,
    out_edges: Mapping[str, tuple[EdgeLeg, ...]],
    undirected: Mapping[str, frozenset[str]],
    in_graph: frozenset[str],
    node_types: Mapping[str, str],
    settings: GraphSettings,
) -> CycleSearch:
    """Search only where a loop could mean something, and report what was skipped.

    Singletons are out (one edge cannot close), rails are out when
    ``graph.cycles.ignore_rails`` (a loop through a supernode is the whole economy,
    not a conspiracy), and both exclusions are reported as counts rather than
    vanishing into an empty result.
    """
    rails = frozenset(account for account, kind in node_types.items() if kind == NODE_TYPE_RAIL)
    excluded = rails if settings.cycles.ignore_rails else frozenset()
    candidates = sorted(in_graph - excluded)
    if not candidates:
        return CycleSearch(
            cycles=(),
            truncated=False,
            truncation_reason=(
                "no candidate accounts survive the singleton and rail exclusions, so "
                "there is nothing to search"
            ),
            visits=0,
            components_searched=0,
            rails_excluded=len(rails & in_graph) if settings.cycles.ignore_rails else 0,
            zero_value_loops_rejected=0,
        )
    allowed = frozenset(candidates)
    restricted: dict[str, tuple[str, ...]] = {
        account: tuple(
            sorted(
                partner for partner in undirected.get(account, frozenset()) if partner in allowed
            )
        )
        for account in candidates
    }
    return enumerate_cycles(
        out_edges=out_edges,
        candidates=candidates,
        undirected_adjacency=restricted,
        settings=settings.cycles,
        rails_excluded=len(rails & in_graph) if settings.cycles.ignore_rails else 0,
    )


def _flows(frame: pl.DataFrame, nodes: pl.DataFrame) -> pl.DataFrame:
    """Value moved per ``(account, currency)``, inbound and outbound.

    Keyed by currency at the row level, so no sum in here can cross one. An
    account's presence in a currency comes from either side, which makes a member's
    ``outflow = 0`` a real observation ("in EUR they only received") while an
    external account's origin-side columns stay null — the corpus never sees them
    send, and that is the difference between zero and unknown (03 A rule 2).
    """
    edges = frame.filter(~pl.col(DERIVED_SELF_COLUMN))
    outbound = (
        edges.group_by("account_from", "currency")
        .agg(
            pl.col("amount_minor").sum().alias("outflow_total_minor"),
            pl.len().alias("outflow_count"),
            pl.col("amount_minor").max().alias("max_single_outflow_minor"),
        )
        .rename({"account_from": "account"})
    )
    inbound = (
        edges.group_by("account_to", "currency")
        .agg(
            pl.col("amount_minor").sum().alias("inflow_total_minor"),
            pl.len().alias("inflow_count"),
            pl.col("amount_minor").max().alias("max_single_inflow_minor"),
        )
        .rename({"account_to": "account"})
    )
    present = pl.concat(
        [
            edges.select(pl.col("account_from").alias("account"), "currency"),
            edges.select(pl.col("account_to").alias("account"), "currency"),
        ],
        how="vertical",
    ).unique()
    externals = sorted(
        str(account)
        for account in nodes.filter(pl.col("node_type") == NODE_TYPE_EXTERNAL)["account"].to_list()
    )
    flows = present.join(inbound, on=["account", "currency"], how="left").join(
        outbound, on=["account", "currency"], how="left"
    )
    if externals:
        flags = pl.col("account").is_in(externals)
        flows = flows.with_columns(
            pl.when(flags)
            .then(None)
            .otherwise(pl.col("outflow_total_minor"))
            .cast(pl.Int64)
            .alias("outflow_total_minor"),
            pl.when(flags)
            .then(None)
            .otherwise(pl.col("outflow_count"))
            .cast(pl.Int64)
            .alias("outflow_count"),
            pl.when(flags)
            .then(None)
            .otherwise(pl.col("max_single_outflow_minor"))
            .cast(pl.Int64)
            .alias("max_single_outflow_minor"),
        )
    return flows.sort(["account", "currency"])


def top_degrees(table: pl.DataFrame, n: int = TOP_DEGREES_REPORTED) -> tuple[tuple[str, int], ...]:
    """Highest-degree accounts, tie-broken by node key so the list cannot shuffle."""
    ordered = table.sort(["total_degree", "account"], descending=[True, False])
    return tuple(
        (str(account), int(degree))
        for account, degree in ordered.select(["account", "total_degree"]).head(n).iter_rows()
    )


def fingerprint(settings: GraphSettings) -> dict[str, object]:
    """The numbers that decided this graph, recorded next to its output.

    Written into the persisted manifest, so an explanation can be re-derived from
    the artifact rather than from a config file that may since have moved on.
    """
    return {
        "rail_degree_percentile": settings.rail_degree_percentile,
        "subgraph_node_cap": settings.subgraph_node_cap,
        "default_hops": settings.default_hops,
        "order_columns": list(settings.order_columns),
        "community": {
            "algorithm": settings.community.algorithm,
            "seed": settings.community.seed,
            "n_iterations": settings.community.n_iterations,
            "objective_function": settings.community.objective_function,
            "resolution": settings.community.resolution,
            "canonical_order": settings.community.canonical_order,
        },
        "page_rank": {
            "alpha": settings.page_rank.alpha,
            "max_iter": settings.page_rank.max_iter,
        },
        "betweenness": {"pivots": settings.betweenness.pivots},
        "cycles": {
            "min_length": settings.cycles.min_length,
            "max_length": settings.cycles.max_length,
            "value_retention_floor": settings.cycles.value_retention_floor,
            "require_non_increasing": settings.cycles.require_non_increasing,
            "ignore_rails": settings.cycles.ignore_rails,
            "excluded_txn_types": list(settings.cycles.excluded_txn_types),
            "max_visits_per_component": settings.cycles.max_visits_per_component,
            "max_cycles_reported": settings.cycles.max_cycles_reported,
        },
        "exposure": {
            "window_hours": settings.exposure_window_hours,
            "downstream_hops": settings.exposure_downstream_hops,
        },
        "feature_null_policy": {
            "nodes": {kind: list(columns) for kind, columns in FEATURE_NULL_POLICY.items()},
            "flows": {kind: list(columns) for kind, columns in FLOW_NULL_POLICY.items()},
        },
    }


def _count_type(node_types: Mapping[str, str], wanted: str) -> int:
    return sum(1 for kind in node_types.values() if kind == wanted)


def _currencies(frame: pl.DataFrame) -> tuple[str, ...]:
    return tuple(sorted(str(item) for item in frame["currency"].unique().to_list()))


__all__ = [
    "TOP_DEGREES_REPORTED",
    "DegreeMeasurement",
    "build_graph",
    "degree_measurement",
    "fingerprint",
    "top_degrees",
]
