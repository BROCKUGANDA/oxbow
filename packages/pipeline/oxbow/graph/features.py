"""Node features that need the typed structure: density, centrality, cycles, fan.

These run after typing because each one depends on who is a rail and who is only a
leaf:

* **local density** measures whether an account's counterparties know each other —
  the structural signature of a closed group. It is computed over the *in-graph*
  population, because a leaf's "neighbourhood" is one edge and a density computed
  over that is noise dressed as a number.
* **PageRank** is the global-centrality feature, damped by
  ``graph.page_rank.alpha``. Rails stay in: removing them would silently drop the
  destinations that real money reaches, and a merchant's inbound share is exactly
  the kind of concentration this feature is supposed to surface.
* **cycle participation** reads the bounded search's result, so it inherits the
  search's honesty: a truncated count reports as many cycles as were found and says
  so.
* **effective fan** counts distinct *non-rail* counterparties. This is the number a
  fan-in rule should read: raw fan-in on a payment gateway is the customer base of
  a country, which is the false-positive fire hose ``graph.rail_degree_percentile``
  exists to prevent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

import networkx as nx
import polars as pl

from oxbow.graph.build_frames import DIRECTION_RECEIVED_FROM, DIRECTION_SENT_TO
from oxbow.graph.model import CycleSearch, EdgeLeg
from oxbow.graph.settings import PageRankSettings

# A density below this many counterparties is undefined, not zero: two accounts
# that have never met cannot be measured for closedness, and reporting 0.0 would
# say "we checked and they are not closed" when the true answer is "there is
# nothing to check".
MIN_NEIGHBOURS_FOR_DENSITY: Final[int] = 2


@dataclass(frozen=True, slots=True)
class FeatureTables:
    """One mapping per derived column, keyed by account, nulls where undefined."""

    local_density: Mapping[str, float | None]


@dataclass(frozen=True, slots=True)
class GlobalFeatures:
    """PageRank plus the reason it may be absent, so a null column is explainable."""

    pagerank: Mapping[str, float | None]
    skipped_reason: str | None


@dataclass(frozen=True, slots=True)
class CycleParticipation:
    """How many surviving loops each account sits on, and how long the longest was."""

    counts: Mapping[str, int]
    max_lengths: Mapping[str, int]

    @property
    def participants(self) -> frozenset[str]:
        return frozenset(account for account, count in self.counts.items() if count > 0)


def compute_local_density(
    accounts: Iterable[str],
    undirected: Mapping[str, frozenset[str]],
    in_graph: frozenset[str],
) -> FeatureTables:
    """Fraction of possible links that exist among an account's counterparties.

    ``edges_among_neighbours / (k * (k - 1) / 2)`` over the in-graph population.
    Determined entirely by the adjacency, so it is reproducible from the persisted
    edge table and never depends on an algorithm's iteration order.
    """
    densities: dict[str, float | None] = {}
    for account in sorted(accounts):
        partners = sorted(undirected.get(account, frozenset()) & in_graph)
        if len(partners) < MIN_NEIGHBOURS_FOR_DENSITY:
            densities[account] = None
            continue
        possible = len(partners) * (len(partners) - 1) / 2
        observed = sum(
            1
            for position, left in enumerate(partners)
            for right in partners[position + 1 :]
            if right in undirected.get(left, frozenset())
        )
        densities[account] = observed / possible
    return FeatureTables(local_density=densities)


def compute_global_features(
    multigraph: nx.MultiDiGraph,
    in_graph: frozenset[str],
    settings: PageRankSettings,
) -> GlobalFeatures:
    """PageRank over the non-singleton subgraph, or the reason there is none.

    The population is the in-graph nodes: including singletons would add a mass
    floor proportional to how many one-edge accounts the corpus happens to contain,
    which measures ingest volume rather than centrality. ``tol`` is deliberately
    left at the library default — it is a convergence criterion, not a policy, and
    ``config`` owns the two numbers that are policy (alpha and max_iter).
    """
    population = sorted(in_graph)
    if not population:
        return GlobalFeatures(
            pagerank={},
            skipped_reason="no non-singleton accounts, so there is nothing to rank",
        )
    sub: nx.MultiDiGraph = multigraph.subgraph(population).copy()
    if sub.number_of_edges() == 0:
        return GlobalFeatures(
            pagerank={account: None for account in population},
            skipped_reason=(
                "no edges remain between the non-singleton accounts: a PageRank over "
                "isolated nodes would be a uniform number with no meaning (DEV-011)"
            ),
        )
    try:
        raw = nx.pagerank(sub, alpha=settings.alpha, max_iter=settings.max_iter)
    except nx.PowerIterationFailedConvergence as exc:
        return GlobalFeatures(
            pagerank={account: None for account in population},
            skipped_reason=f"PageRank did not converge within {settings.max_iter} iterations: {exc}",
        )
    return GlobalFeatures(
        pagerank={account: float(raw[account]) for account in population}, skipped_reason=None
    )


def compute_cycle_participation(search: CycleSearch, accounts: Iterable[str]) -> CycleParticipation:
    """Count surviving cycles per account and the longest one it appears in."""
    counts: dict[str, int] = {account: 0 for account in accounts}
    lengths: dict[str, int] = {}
    for cycle in search.cycles:
        for node in cycle.path:
            counts[node] = counts.get(node, 0) + 1
            lengths[node] = max(lengths.get(node, 0), cycle.length)
    return CycleParticipation(counts=counts, max_lengths=lengths)


def compute_effective_fan(table: pl.DataFrame, links: pl.DataFrame) -> pl.DataFrame:
    """Add ``fan_in_effective`` / ``fan_out_effective``: distinct non-rail partners.

    Rails are removed as *counterparties*, not as a concept: an account that sends
    to one merchant and five people has effective fan-out 5, which is the number a
    fan-out rule should fire on. Computed after typing, hence its own function.
    """
    rails = table.filter(pl.col("node_type") == "rail")["account"].to_list()
    rail_set = set(rails)
    usable = links.filter(~pl.col("counterparty").is_in(sorted(rail_set))) if rail_set else links
    per_account = usable.group_by("account").agg(
        pl.col("counterparty")
        .filter(pl.col("direction") == DIRECTION_SENT_TO)
        .n_unique()
        .alias("fan_out_effective"),
        pl.col("counterparty")
        .filter(pl.col("direction") == DIRECTION_RECEIVED_FROM)
        .n_unique()
        .alias("fan_in_effective"),
    )
    joined = table.join(per_account, on="account", how="left").with_columns(
        pl.col("fan_in_effective").fill_null(0).cast(pl.Int64),
        pl.col("fan_out_effective").fill_null(0).cast(pl.Int64),
    )
    # A rail gets no fan score at all, effective or otherwise; an external account
    # keeps its inbound figure but cannot have an outbound one, per the null policy.
    return joined.with_columns(
        pl.when(pl.col("is_rail"))
        .then(None)
        .otherwise(pl.col("fan_in_effective"))
        .cast(pl.Int64)
        .alias("fan_in_effective"),
        pl.when(pl.col("is_rail") | pl.col("is_external"))
        .then(None)
        .otherwise(pl.col("fan_out_effective"))
        .cast(pl.Int64)
        .alias("fan_out_effective"),
    )


def betweenness_centrality(
    accounts: Iterable[str],
    out_edges: Mapping[str, tuple[EdgeLeg, ...]],
    *,
    pivots: int,
    seed: int,
) -> Mapping[str, float]:
    """Sampled betweenness over the named population, using ``graph.betweenness``.

    Requested on demand rather than computed in every build: with 500 pivots the
    cost is ``pivots * (V + E)``, which is trivial over the 1,500-node subgraph the
    explorer renders and absurd over a whole corpus. Pivot selection is seeded, so
    two calls on the same population return the same number — an unseeded sample
    would put a different central account on screen on every refresh.
    """
    population = sorted(set(accounts))
    if len(population) < 2:
        return {account: 0.0 for account in population}
    inside = frozenset(population)
    graph = nx.DiGraph()
    graph.add_nodes_from(population)
    for account in population:
        for leg in out_edges.get(account, ()):
            if leg.dst in inside and leg.dst != account:
                graph.add_edge(account, leg.dst, weight=1)
    if graph.number_of_edges() == 0:
        return {account: 0.0 for account in population}
    k = pivots if pivots < graph.number_of_nodes() else None
    raw = nx.betweenness_centrality(graph, k=k, seed=seed, normalized=True)
    return {account: float(raw[account]) for account in population}


__all__ = [
    "MIN_NEIGHBOURS_FOR_DENSITY",
    "CycleParticipation",
    "FeatureTables",
    "GlobalFeatures",
    "betweenness_centrality",
    "compute_cycle_participation",
    "compute_effective_fan",
    "compute_global_features",
    "compute_local_density",
]
