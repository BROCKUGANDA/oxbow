"""The two fold-scoped providers plan §8 asks for, wired to the layers that own them.

WHAT THIS MODULE IS. ``oxbow.features.fold_scope`` declares the seam: a
``GraphFeatureProvider`` whose only method returns a table *sealed to one fold*, and a
``RuleHitProvider`` whose only method returns the hits that fold's rules produced inside
it. Neither protocol had an implementation, so every declared ``graph_node`` and
``rule_field`` column was null by construction and the graph-features ablation row would
have been measuring a no-op. This module implements them: it walks the graph layer and the
rules layer once per fold and hands back the node attributes and severities the registry
declares.

THE DOCTRINE, AND WHY IT IS STRUCTURAL RATHER THAN ASPIRATIONAL. Plan §8: graph features
are "recomputed per fold from that fold's edges only, never once over the full graph".
Operationally that means ``build_graph`` is called on
``events.filter(event_ts_utc <= fold.graph_as_of_ts)`` — the fold's own graph, with its own
rail typing, its own communities, its own bounded cycle search — and the result is passed
through :func:`oxbow.features.fold_scope.seal_node_table` together with the edge frame that
produced it, which refuses a table fed from after the cutoff. A whole-corpus graph cannot
be sealed for an earlier fold, so it cannot reach a matrix at all: the guard is the type,
not a comment. ``fold.graph_as_of_ts`` is ``fold.train_end_ts`` — the structure a fold's
model sees ends where its training data ends, not at the test start.

EVERY COLUMN IS DERIVED, NOTHING IS INVENTED. The registry declares sixteen node fields;
each comes from a graph-layer primitive and obeys its null policy:

``in_degree`` / ``out_degree`` / ``fan_in_effective`` / ``fan_out_effective`` /
``self_transfer_count`` / ``pagerank`` / ``community_id`` / ``is_external``
    read straight off the node table, including its documented nulls (an external node has
    no observable out-degree; a rail has no fan score; PageRank is skipped when the fold's
    non-singleton subgraph has no remaining edges).
``local_density_bps``
    the layer's density scaled by 10 000 as an exact integer: a dimensionless share times a
    power of ten is not money, and the ``*_bps`` discipline says int64.
``community_size``
    how many nodes carry the same community id, counted from the node table that is actually
    joined. Null where no community was assigned: "no community" and "a community of one"
    are different claims.
``betweenness_approx``
    :func:`oxbow.graph.betweenness_centrality` over the fold's non-singleton population with
    ``graph.betweenness.pivots`` and the run seed. Asked for here rather than inside every
    graph build because its cost is pivots x (V + E).
``k_core_number``
    the Batagelj-Zaversnik decomposition of the fold's undirected in-graph projection - the
    same "who do they deal with" view degree, density and communities use. A leaf is pruned
    out of the decomposition, so it reports null rather than a false zero.
``cycle_participation_count`` / ``max_cycle_retention_bps`` / ``longest_chain_length``
    read off the fold's bounded, time-respecting, reversal-excluded cycle search. A zero
    participation is published as zero — the search ran and found none, which is DEV-011's
    finding — while retention and chain length are null for a node on no loop, because a
    statistic over an empty set has no value.
``downstream_outflow_24h_minor``
    the money that left the node *and the nodes it pays directly* in the 24 hours ending at
    the fold's cutoff. A fold-scoped column has no per-row instant to anchor on, so the
    fold's own cutoff is the only honest one.

MONEY. Only ``downstream_outflow_24h_minor`` sums money, and it sums across a *set of
accounts*, so the currency partition cannot be inherited from the registry's ``group_by``.
What makes it legal is :func:`assert_single_currency_per_account`, run on the corpus in this
module's constructor: a corpus in which one account shows two currencies refuses the build
before a single fold is graphed, so the summed set is a single-currency sum by construction.
No exchange rate is invented anywhere in this file, and every amount stays Int64.

DETERMINISM. Node attributes are keyed by account and written in sorted account order; the
24-hour window and the rule fit window are integer microseconds derived from the fold's own
boundaries (:func:`to_micros` keeps the conversion out of float space); the betweenness
pivot sample and the community detection take the configured seed. Nothing reads a wall
clock, so two runs over the same events produce byte-identical sealed tables.

The rule side is described where it is implemented, in
:class:`FoldRuleHitProvider`'s own docstring; what belongs here is only that the rules
layer is *called*, not restated: ``oxbow.rules.registry.evaluate_rules`` is P3b's and this
module owns no threshold, no severity curve and no rule.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import networkx as nx
import polars as pl

from oxbow.backtest.splits import Fold
from oxbow.config import PipelineConfig
from oxbow.dtypes import PolarsDtype
from oxbow.features.build import assert_input_contract, assert_single_currency_per_account
from oxbow.features.fold_scope import (
    ACCOUNT,
    CONTRIBUTING_TS_COLUMN,
    RULE_COLUMN,
    SEVERITY_COLUMN,
    FoldScopedTable,
    GraphFeatureProvider,
    GraphScopeError,
    RuleHitProvider,
    seal_node_table,
    seal_rule_hit_table,
)
from oxbow.features.kinds import EVENT_TS, TXN_ID
from oxbow.features.registry import FeatureRegistry
from oxbow.graph import betweenness_centrality, build_graph, load_graph_settings
from oxbow.graph.events import DERIVED_SELF_COLUMN, DERIVED_TS_COLUMN
from oxbow.graph.model import AccountGraph
from oxbow.graph.settings import GraphSettings
from oxbow.rules.events import Window
from oxbow.rules.registry import evaluate_rules

EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
ONE_MICROSECOND: Final = timedelta(microseconds=1)
#: A share scaled to integer basis points: the registry types every ``*_bps`` column int64
#: and pins the scale at 10 000 (``registry._validate_entry``).
BASIS_POINTS: Final = 10_000
#: The window ``downstream_outflow_24h_minor`` reads, ending at the fold's graph cutoff.
DOWNSTREAM_WINDOW_US: Final = int(timedelta(hours=24).total_seconds()) * 1_000_000
#: The node attribute an account absent from the fold's graph carries nothing for.
_GRAPH_KIND: Final = "graph_node"
_RULE_KIND: Final = "rule_field"


class FoldProviderError(RuntimeError):
    """A fold could not be given its own graph or its own rule hits."""


def to_micros(moment: datetime) -> int:
    """Integer microseconds since the epoch in UTC, with no float in the path.

    ``datetime.timestamp()`` returns a float, and a float near 1.7e15 carries ~250 ns of
    slop: enough for a boundary row to fall on the wrong side of a window on one run and
    the right side on another. Subtracting the epoch is exact integer arithmetic, which is
    what a total order over ``(event_ts_utc, txn_id)`` deserves.
    """
    if moment.tzinfo is None:
        raise FoldProviderError(f"{moment!r} is naive; every instant in this layer is tz-aware")
    return int((moment.astimezone(UTC) - EPOCH) // ONE_MICROSECOND)


def from_micros(value: int) -> datetime:
    """The inverse of :func:`to_micros`, for a window whose end must be stated as an instant."""
    return EPOCH + timedelta(microseconds=int(value))


@dataclass(frozen=True, slots=True)
class FoldBuild:
    """One fold's graph and the edge frame that produced it, held together on purpose.

    The pair is the evidence :func:`~oxbow.features.fold_scope.seal_node_table` demands: a
    node table cannot be sealed without naming the edges behind it, so caching them beside
    the graph makes the honest path the only path available.
    """

    fold_id: str
    graph: AccountGraph
    edges: pl.DataFrame
    events: pl.DataFrame

    @property
    def node_count(self) -> int:
        return int(self.graph.nodes.height)

    @property
    def edge_count(self) -> int:
        return int(self.graph.stats.edge_count)


@dataclass(frozen=True, slots=True)
class FoldProviderReport:
    """What the providers actually computed, per fold. Printed, never inferred."""

    folds_seen: int
    folds_with_graph: int
    folds_without_graph: int
    node_rows: int
    edge_rows: int
    rule_hit_rows: int
    graph_fields_declared: int
    graph_fields_populated: int
    rule_ids_declared: int
    rule_ids_hit: int
    per_fold: Mapping[str, tuple[int, int, int]]
    populated_graph_fields: tuple[str, ...]
    hit_rule_ids: tuple[str, ...]

    def sentence(self) -> str:
        empty = ", ".join(
            fold_id for fold_id, (nodes, _, _) in sorted(self.per_fold.items()) if nodes == 0
        )
        return (
            f"fold-scoped providers: {self.folds_with_graph}/{self.folds_seen} fold(s) graphed"
            + (f" (no events in {empty})" if self.folds_without_graph else "")
            + f"; {self.node_rows} node rows and {self.edge_rows} edge rows fed the joins; "
            f"{self.graph_fields_populated}/{self.graph_fields_declared} graph fields and "
            f"{self.rule_ids_hit}/{self.rule_ids_declared} rule ids carried a value "
            f"({self.rule_hit_rows} hit rows)"
        )


@dataclass(slots=True)
class FoldGraphSource:
    """Builds each fold's graph once and derives the node columns the registry declares.

    Shared by both providers: the rule hits need the same fold graph the node attributes do
    (R4 and R12 read cycles, R2/R3/R11 read the typed fan), so one fold means one
    ``build_graph`` call rather than two — and two providers cannot disagree about which
    graph a fold had.
    """

    events: pl.DataFrame
    registry: FeatureRegistry
    cfg: PipelineConfig
    settings: GraphSettings
    seed: int
    builds: dict[str, FoldBuild | None] = field(default_factory=dict, repr=False)
    rule_hits: dict[str, pl.DataFrame] = field(default_factory=dict, repr=False)
    graph_fields_populated: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        assert_input_contract(self.events)
        # Here and not only inside the builder: this module sums money over a *set* of
        # accounts, and the single-currency invariant is what makes that sum legal rather
        # than an invented exchange rate (plan §8).
        assert_single_currency_per_account(self.events)
        self.events = self.events.sort([EVENT_TS, TXN_ID])

    # --- the fold's graph -------------------------------------------------
    def fold_events(self, fold: Fold) -> pl.DataFrame:
        """The events this fold may read: at or before its own graph cutoff.

        Expanding-window by construction — the filter has an upper bound only — so a later
        fold sees strictly more history than an earlier one and no fold sees the future.
        """
        return self.events.filter(pl.col(EVENT_TS) <= fold.graph_as_of_ts)

    def build(self, fold: Fold) -> FoldBuild | None:
        """The fold's graph, built once and cached, or None when the fold has no events."""
        if fold.fold_id in self.builds:
            return self.builds[fold.fold_id]
        events = self.fold_events(fold)
        if events.height == 0:
            self.builds[fold.fold_id] = None
            return None
        build = FoldBuild(
            fold_id=fold.fold_id,
            graph=build_graph(events, self.cfg),
            edges=events,
            events=events,
        )
        self.builds[fold.fold_id] = build
        return build

    # --- the node table ---------------------------------------------------
    def node_table(self, fold: Fold) -> tuple[pl.DataFrame, pl.DataFrame] | None:
        """``(node table, the edges that fed it)`` for one fold, or None.

        The edges are returned rather than dropped because the seal checks them: a node
        table's provenance is part of what makes it fold-scoped.
        """
        build = self.build(fold)
        if build is None:
            return None
        return self.derive_node_columns(fold, build.graph), build.edges

    def derive_node_columns(self, fold: Fold, graph: AccountGraph) -> pl.DataFrame:
        """The declared node fields, computed from this fold's graph only."""
        nodes = graph.nodes
        if ACCOUNT not in nodes.columns:
            raise GraphScopeError(
                f"the graph node table for fold {fold.fold_id!r} has no {ACCOUNT!r} column"
            )
        accounts = [str(account) for account in nodes[ACCOUNT].to_list()]
        ordered = sorted(accounts)
        if accounts != ordered:
            raise GraphScopeError(
                f"the graph node table for fold {fold.fold_id!r} is not in account order; "
                "the sealed table's row order would depend on the build that produced it"
            )
        in_graph = frozenset(
            str(account) for account in nodes.filter(pl.col("in_graph"))[ACCOUNT].to_list()
        )
        densities = _density_bps(nodes)
        betweenness = _betweenness(graph, in_graph, self.settings, self.seed)
        cores = _core_numbers(graph, in_graph)
        communities = _community_sizes(nodes)
        retention, chains = _cycle_readouts(graph)
        downstream = _downstream_outflow(fold, graph)
        columns: dict[str, pl.Series] = {
            ACCOUNT: pl.Series(ACCOUNT, accounts, pl.String),
            "in_degree": _column(nodes, "in_degree", accounts, pl.Int64, "in_degree"),
            "out_degree": _column(nodes, "out_degree", accounts, pl.Int64, "out_degree"),
            "fan_in_effective": _column(
                nodes, "fan_in_effective", accounts, pl.Int64, "fan_in_effective"
            ),
            "fan_out_effective": _column(
                nodes, "fan_out_effective", accounts, pl.Int64, "fan_out_effective"
            ),
            "self_transfer_count": _column(
                nodes, "self_transfer_count", accounts, pl.Int64, "self_transfer_count"
            ),
            "pagerank": _column(nodes, "pagerank", accounts, pl.Float64, "pagerank"),
            "community_id": _column(nodes, "community_id", accounts, pl.Int64, "community_id"),
            "community_size": pl.Series(
                "community_size", [communities.get(account) for account in accounts], pl.Int64
            ),
            "local_density_bps": pl.Series(
                "local_density_bps", [densities.get(account) for account in accounts], pl.Int64
            ),
            "betweenness_approx": pl.Series(
                "betweenness_approx",
                [betweenness.get(account) for account in accounts],
                pl.Float64,
            ),
            "k_core_number": pl.Series(
                "k_core_number", [cores.get(account) for account in accounts], pl.Int64
            ),
            "cycle_participation_count": _column(
                nodes, "cycle_count", accounts, pl.Int64, "cycle_participation_count"
            ),
            "max_cycle_retention_bps": pl.Series(
                "max_cycle_retention_bps",
                [retention.get(account) for account in accounts],
                pl.Int64,
            ),
            "longest_chain_length": pl.Series(
                "longest_chain_length", [chains.get(account) for account in accounts], pl.Int64
            ),
            "downstream_outflow_24h_minor": pl.Series(
                "downstream_outflow_24h_minor",
                [downstream.get(account) for account in accounts],
                pl.Int64,
            ),
            "is_external": _column(nodes, "is_external", accounts, pl.Boolean, "is_external"),
        }
        declared = list(dict.fromkeys(self.registry.graph_fields))
        missing = sorted(field_name for field_name in declared if field_name not in columns)
        undeclared = sorted(set(columns) - {ACCOUNT} - set(declared))
        if missing or undeclared:
            raise GraphScopeError(
                f"the fold graph table for {fold.fold_id!r} does not match the registry: "
                f"missing {missing}, undeclared {undeclared}. A node may be absent; a column "
                "may not be."
            )
        table = pl.DataFrame([columns[name] for name in [ACCOUNT, *declared]])
        _assert_field_dtypes(table, self.registry, fold)
        for field_name in declared:
            if int(table[field_name].null_count()) < table.height:
                self.graph_fields_populated.add(field_name)
        return table


class FoldGraphFeatureProvider:
    """A ``GraphFeatureProvider`` over a real corpus: one sealed node table per fold."""

    def __init__(self, source: FoldGraphSource, registry: FeatureRegistry) -> None:
        self._source = source
        self._registry = registry

    @property
    def source(self) -> FoldGraphSource:
        """The shared build cache, exposed for the run report rather than for mutation."""
        return self._source

    def fold_graph(self, fold: Fold) -> FoldScopedTable | None:
        produced = self._source.node_table(fold)
        if produced is None:
            return None
        table, edges = produced
        return seal_node_table(fold=fold, table=table, edges=edges, registry=self._registry)


class FoldRuleHitProvider:
    """A ``RuleHitProvider`` over a real corpus: the hits this fold's rules produced.

    ``evaluate_rules`` is called on the fold's own events with the fold's training period
    as its fit window, so ``tau`` and the structuring threshold are fitted on data this
    fold may see; the hit-rate ceiling stays enforced rather than opted out of, because a
    rule that flags a third of the fold must fail the run with its suggestion instead of
    shipping a column nobody can read. Each hit's ``last_contributing_ts_utc`` is the
    latest timestamp among the transactions that produced it - the column
    ``seal_rule_hit_table`` requires and checks against the fold cutoff, because a hit that
    cannot name its events cannot be shown to be fold-scoped.
    """

    def __init__(self, source: FoldGraphSource, registry: FeatureRegistry) -> None:
        self._source = source
        self._registry = registry

    @property
    def source(self) -> FoldGraphSource:
        """The shared build cache, exposed for the run report rather than for mutation."""
        return self._source

    def fold_rule_hits(self, fold: Fold) -> FoldScopedTable | None:
        build = self._source.build(fold)
        if build is None:
            self._source.rule_hits[fold.fold_id] = _empty_hit_frame()
            return None
        frame = self.hit_frame(fold, build)
        self._source.rule_hits[fold.fold_id] = frame
        return seal_rule_hit_table(fold=fold, hits=frame, registry=self._registry)

    def hit_frame(self, fold: Fold, build: FoldBuild) -> pl.DataFrame:
        """One row per (account, rule) hit, with the instant that contributed to it.

        ``evaluate_rules`` is called on the fold's own events, so no hit here can name a
        transaction the fold may not read; the seal checks that claim rather than trusting
        it, which is the whole point of ``last_contributing_ts_utc`` being mandatory.
        """
        stamps = {
            str(txn_id): moment
            for txn_id, moment in build.events.select([TXN_ID, EVENT_TS]).iter_rows()
        }
        result = evaluate_rules(
            build.events,
            build.graph,
            self._source.cfg,
            fit_window=Window(
                start_us=to_micros(fold.train_start_ts),
                end_us=to_micros(fold.train_end_ts),
                label=f"fold {fold.fold_id} training window",
            ),
        )
        rows: list[dict[str, object]] = []
        for hit in result.hits:
            observed = [stamps[txn_id] for txn_id in hit.txn_ids if txn_id in stamps]
            contributing = max(observed) if observed else from_micros(hit.window.end_us)
            rows.append(
                {
                    ACCOUNT: str(hit.account_key),
                    RULE_COLUMN: str(hit.rule_id),
                    SEVERITY_COLUMN: hit.severity,
                    CONTRIBUTING_TS_COLUMN: contributing,
                }
            )
        frame = pl.DataFrame(rows, schema=_HIT_SCHEMA)
        return frame.sort([ACCOUNT, RULE_COLUMN, CONTRIBUTING_TS_COLUMN, SEVERITY_COLUMN])


def fold_providers(
    events: pl.DataFrame, registry: FeatureRegistry, cfg: PipelineConfig
) -> tuple[FoldGraphFeatureProvider, FoldRuleHitProvider]:
    """The pair the feature builder wants, sharing one graph build per fold.

    Returned together because they are built together: two callers each constructing their
    own source would graph every fold twice and could then disagree about what the fold's
    network looked like.
    """
    source = FoldGraphSource(
        events=events,
        registry=registry,
        cfg=cfg,
        settings=load_graph_settings(cfg),
        seed=int(cfg.seed),
    )
    return FoldGraphFeatureProvider(source, registry), FoldRuleHitProvider(source, registry)


# Checked at import rather than at the call site: the whole value of the seam is that the
# builder's argument *type* is the guard, and a provider that drifted away from the
# protocol would otherwise be discovered by a runtime refusal in the middle of a run.
# ``runtime_checkable`` protocols match on member presence, which is exactly what a class
# body either has or does not have, so this costs nothing and fails loudly.
if not (
    isinstance(FoldGraphFeatureProvider, GraphFeatureProvider)
    and isinstance(FoldRuleHitProvider, RuleHitProvider)
):  # pragma: no cover - an import-time failure, not a runtime one
    raise FoldProviderError(
        "FoldGraphFeatureProvider / FoldRuleHitProvider no longer satisfy "
        "oxbow.features.fold_scope's protocols: the fold-scoped seam the feature builder "
        "types its argument on has moved."
    )


def provider_report(
    source: FoldGraphSource, rules: FoldRuleHitProvider, folds: Iterable[Fold]
) -> FoldProviderReport:
    """What the providers computed over ``folds``, read off their own bookkeeping.

    Takes the rule provider only to make the call site state both halves of the seam; the
    hits it needs are already recorded on the shared source.
    """
    _ = rules
    seen = [fold.fold_id for fold in folds]
    per_fold: dict[str, tuple[int, int, int]] = {}
    hits: set[str] = set()
    for fold_id in seen:
        build = source.builds.get(fold_id)
        frame = source.rule_hits.get(fold_id, _empty_hit_frame())
        hits.update(str(rule_id) for rule_id in frame[RULE_COLUMN].unique().to_list())
        per_fold[fold_id] = (
            0 if build is None else build.node_count,
            0 if build is None else build.edge_count,
            int(frame.height),
        )
    declared_rules = set(source.registry.rule_ids)
    return FoldProviderReport(
        folds_seen=len(seen),
        folds_with_graph=sum(1 for fold_id in seen if source.builds.get(fold_id) is not None),
        folds_without_graph=sum(1 for fold_id in seen if source.builds.get(fold_id) is None),
        node_rows=sum(rows[0] for rows in per_fold.values()),
        edge_rows=sum(rows[1] for rows in per_fold.values()),
        rule_hit_rows=sum(rows[2] for rows in per_fold.values()),
        graph_fields_declared=len(dict.fromkeys(source.registry.graph_fields)),
        graph_fields_populated=len(source.graph_fields_populated),
        rule_ids_declared=len(declared_rules),
        rule_ids_hit=len(hits & declared_rules),
        per_fold=per_fold,
        populated_graph_fields=tuple(sorted(source.graph_fields_populated)),
        hit_rule_ids=tuple(sorted(hits)),
    )


# --- derivation helpers ---------------------------------------------------

_HIT_SCHEMA: Final[Mapping[str, PolarsDtype]] = {
    ACCOUNT: pl.String,
    RULE_COLUMN: pl.String,
    SEVERITY_COLUMN: pl.Float64,
    CONTRIBUTING_TS_COLUMN: pl.Datetime("us", UTC),
}


def _empty_hit_frame() -> pl.DataFrame:
    """A fold with no hits still has a hit table: four columns and no rows.

    Returning None would silently null the rule columns; returning an empty frame keeps
    "the rules ran inside this fold and nothing fired" a statement the artifact can make.
    """
    return pl.DataFrame([], schema=_HIT_SCHEMA)


def _column(
    nodes: pl.DataFrame,
    source: str,
    accounts: Iterable[str],
    dtype: pl.DataType,
    name: str,
) -> pl.Series:
    """One node column, re-keyed to the caller's account order under the declared name.

    Positional reads across five derived mappings and one frame are how a column ends up
    one row off and entirely plausible; every value here is looked up by account, and an
    account the fold's graph does not carry comes back null rather than as somebody else's
    number. ``name`` is the *registry's* field name, which is not always the node table's
    (``cycle_count`` is published as ``cycle_participation_count``).
    """
    wanted = {str(account) for account in accounts}
    values = {
        str(account): value
        for account, value in nodes.select([ACCOUNT, source]).iter_rows()
        if str(account) in wanted
    }
    return pl.Series(name, [values.get(account) for account in accounts], dtype)


def _assert_field_dtypes(table: pl.DataFrame, registry: FeatureRegistry, fold: Fold) -> None:
    """Each node field must arrive already typed as the entry that reads it declares.

    The fold join casts with ``strict=False``, so an int64 field reaching a float64 entry —
    or the other way round — would become a column of nulls on every row while the build
    stayed green. Asserting here turns that silent null into a message naming the field and
    both dtypes.
    """
    problems: list[str] = []
    for entry in registry.entries:
        if entry.kind != _GRAPH_KIND or not entry.graph_field:
            continue
        if entry.graph_field not in table.columns:
            continue
        actual = table.schema[entry.graph_field]
        if actual != entry.polars_dtype:
            problems.append(
                f"{entry.graph_field} (read by {entry.id}) is {actual}, registry declares "
                f"{entry.polars_dtype}"
            )
    if problems:
        raise GraphScopeError(
            f"the fold graph table for {fold.fold_id!r} is typed wrong: {'; '.join(problems)}"
        )


def _density_bps(nodes: pl.DataFrame) -> Mapping[str, int]:
    """Local density in integer basis points; an undefined density stays undefined."""
    out: dict[str, int] = {}
    for account, density in nodes.select([ACCOUNT, "local_density"]).iter_rows():
        if density is None:
            continue
        out[str(account)] = int(round(float(density) * BASIS_POINTS))
    return out


def _betweenness(
    graph: AccountGraph, in_graph: frozenset[str], settings: GraphSettings, seed: int
) -> Mapping[str, float]:
    """Sampled betweenness over the fold's non-singleton population.

    Leaves are outside it because the layer excludes them from every aggregate: a
    betweenness of 0 for the one counterparty an account has is a tautology, and putting
    0.0 there would overwrite the registry's stated reason with a number.
    """
    if not in_graph:
        return {}
    return betweenness_centrality(
        sorted(in_graph), graph.out_edges, pivots=settings.betweenness.pivots, seed=seed
    )


def _core_numbers(graph: AccountGraph, in_graph: frozenset[str]) -> Mapping[str, int]:
    """k-core number per account, over the fold's undirected in-graph projection."""
    if not in_graph:
        return {}
    work = nx.Graph()
    work.add_nodes_from(sorted(in_graph))
    for account in sorted(in_graph):
        for partner in sorted(graph.simple_neighbours.get(account, frozenset())):
            if partner != account and partner in in_graph:
                work.add_edge(account, partner)
    if work.number_of_edges() == 0:
        return {}
    return {str(account): int(value) for account, value in nx.core_number(work).items()}


def _community_sizes(nodes: pl.DataFrame) -> Mapping[str, int]:
    """How many nodes carry each community id, keyed by account.

    Counted from the node table that is actually joined rather than read from the
    detector's own report: a size that disagreed with the membership published beside it
    would be a second truth about the same fold.
    """
    sized = (
        nodes.filter(pl.col("community_id").is_not_null())
        .group_by("community_id")
        .agg(pl.len().alias("members"))
    )
    sizes = {int(community): int(members) for community, members in sized.iter_rows()}
    return {
        str(account): sizes.get(int(community))
        for account, community in nodes.select([ACCOUNT, "community_id"]).iter_rows()
        if community is not None
    }


def _cycle_readouts(graph: AccountGraph) -> tuple[Mapping[str, int], Mapping[str, int]]:
    """Retention in basis points, and longest chain, per account on a surviving loop.

    Both are absent — therefore null — for a node on no loop: the registry's null_reason is
    explicit that the bounded search returning nothing is a finding, and a zero retention
    would describe a loop whose value held rather than no loop at all.
    """
    retention: dict[str, int] = {}
    chains: dict[str, int] = {}
    for cycle in graph.search.cycles:
        points = int(round(cycle.value_retention * BASIS_POINTS))
        for node in cycle.path:
            key = str(node)
            retention[key] = max(retention.get(key, points), points)
            chains[key] = max(chains.get(key, cycle.length), cycle.length)
    return retention, chains


def _downstream_outflow(fold: Fold, graph: AccountGraph) -> Mapping[str, int]:
    """Money leaving each node *and its direct downstream* in the fold's last 24 hours.

    The downstream set is the fold graph's own out-edges (self-transfers carry no leg, so
    an account is never its own downstream); the money is each account's outflow in the
    window ending at the fold's cutoff, Int64 minor units throughout, summed once over the
    set because the constructor proved no account in the corpus carries two currencies.
    An account with no downstream edge is absent from the result, which the join publishes
    as null — the registry's stated reason, not a zero.
    """
    cutoff_us = to_micros(fold.graph_as_of_ts)
    window_start_us = cutoff_us - DOWNSTREAM_WINDOW_US
    legs = graph.events.frame.filter(
        (pl.col(DERIVED_TS_COLUMN) > window_start_us)
        & (pl.col(DERIVED_TS_COLUMN) <= cutoff_us)
        & ~pl.col(DERIVED_SELF_COLUMN)
    )
    outflow = {
        str(account): int(value or 0)
        for account, value in legs.group_by("account_from")
        .agg(pl.col("amount_minor").sum().alias("out_minor"))
        .iter_rows()
    }
    totals: dict[str, int] = {}
    for account in sorted(graph.node_types):
        targets = sorted(
            {leg.dst for leg in graph.out_edges.get(account, ()) if leg.dst != account}
        )
        if not targets:
            continue
        totals[account] = int(outflow.get(account, 0)) + sum(
            int(outflow.get(target, 0)) for target in targets
        )
    return totals


__all__ = [
    "DOWNSTREAM_WINDOW_US",
    "FoldBuild",
    "FoldGraphFeatureProvider",
    "FoldGraphSource",
    "FoldProviderError",
    "FoldProviderReport",
    "FoldRuleHitProvider",
    "fold_providers",
    "from_micros",
    "provider_report",
    "to_micros",
]
