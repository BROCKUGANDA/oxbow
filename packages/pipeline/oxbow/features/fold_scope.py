"""The fold-scoped seam for graph and rule features: a type, not a comment.

WHY THIS FILE IS THE POINT OF THE PHASE. Plan §8 requires graph features to be
"recomputed **per fold from that fold's edges only**, never once over the full graph",
and requires the guard to be structural. A comment saying "do not pass the full graph"
holds until someone passes the full graph; an argument that cannot *express* a full
graph holds always. So the feature builder's parameter is typed ``GraphFeatureProvider``,
whose only method returns a ``FoldScopedTable`` sealed to one fold, and a
``pl.DataFrame`` has no conversion into either. The single place a table can be sealed
enforces the two properties that make it fold-scoped:

1. every edge that fed it is at or before ``fold.graph_as_of_ts``. A whole-corpus table
   necessarily contains later edges, so it cannot be sealed for an earlier fold; and
2. the table records the fold id it was sealed for, and reading it back requires that
   same fold. A table sealed for fold 2 and handed to fold 3 is refused at the read —
   which is where that mistake actually gets made, since the write happened upstream.

NULLS ARE EXPLAINED, NOT IMPLIED. DEV-011 measured PaySim as star-shaped: median
counterparty degree 1.0, no surviving time-respecting cycles. So a graph feature may
legitimately be null for a node the fold's graph does not carry, and every such entry in
config/features.yaml states a ``null_reason``. What is *not* allowed is a silently
missing column: a field the registry declares and the provider does not supply fails the
build naming the field, because the difference between "this account has no edges" and
"nobody computed this" is the one a reader cannot recover from afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, runtime_checkable

import polars as pl

from oxbow.backtest.splits import Fold
from oxbow.features.registry import FeatureRegistry

ACCOUNT: Final = "account"
EDGE_TS_COLUMNS: Final = ("event_ts_utc", "ts_utc", "edge_ts_us")
RULE_COLUMN: Final = "rule_id"
SEVERITY_COLUMN: Final = "severity"
CONTRIBUTING_TS_COLUMN: Final = "last_contributing_ts_utc"

# Rule roll-ups are not rules, and the registry names them as such: COUNT is how many
# distinct rules fired and MAX the strongest severity.
ROLLUP_COUNT: Final = "COUNT"
ROLLUP_MAX: Final = "MAX"


class GraphScopeError(RuntimeError):
    """Raised when a table or provider would give a fold more history than it owns.

    This is the leakage guard firing. It carries the numbers — how many rows were past
    the cutoff, and the latest timestamp among them — because "the graph was too new" is
    not something an operator can act on.
    """


def edge_timestamps(edges: pl.DataFrame) -> pl.Series:
    """The timestamp column of an edge frame, accepting the graph layer's names.

    P3a carries microseconds-since-epoch on its hot path and UTC datetimes on its
    tables, so both are read and neither is guessed. An edge frame with neither fails:
    an unsealable edge set is a worse state than a rejected one.
    """
    for name in EDGE_TS_COLUMNS:
        if name not in edges.columns:
            continue
        column = edges[name]
        if isinstance(column.dtype, pl.Datetime):
            return column
        if column.dtype == pl.Int64:
            return column.cast(pl.Datetime("us", "UTC"))
    raise GraphScopeError(
        f"the edge frame has no recognised timestamp column; looked for {list(EDGE_TS_COLUMNS)}"
    )


def _fold_cutoff(fold: Fold) -> datetime:
    return fold.graph_as_of_ts


@dataclass(frozen=True, slots=True)
class FoldScopedTable:
    """Node-level attributes usable by exactly one fold, reachable by no other route.

    Built only by :func:`seal_node_table` and :func:`seal_rule_hit_table`.
    """

    fold_id: str
    as_of_ts: datetime
    kind: str
    frame: pl.DataFrame

    def rows_for(self, fold: Fold) -> pl.DataFrame:
        """The table, or a refusal when ``fold`` is not the fold it was sealed for."""
        if fold.fold_id != self.fold_id:
            raise GraphScopeError(
                f"a {self.kind} table sealed for fold {self.fold_id!r} was handed to fold "
                f"{fold.fold_id!r}; fold-scoped attributes do not cross folds"
            )
        if self.as_of_ts > _fold_cutoff(fold):
            raise GraphScopeError(
                f"the {self.kind} table for fold {self.fold_id!r} is as of "
                f"{self.as_of_ts.isoformat()}, after that fold's graph cutoff "
                f"{_fold_cutoff(fold).isoformat()}"
            )
        return self.frame

    @property
    def height(self) -> int:
        """How many nodes the fold's table carries — reported so a thin fold is visible."""
        return self.frame.height


def seal_node_table(
    *,
    fold: Fold,
    table: pl.DataFrame,
    edges: pl.DataFrame,
    registry: FeatureRegistry,
) -> FoldScopedTable:
    """Validate and seal a node-level graph table for one fold.

    ``edges`` is the set the table was computed from, not a formality: the seal asserts
    every edge timestamp is at or before the fold's graph cutoff, which is what makes a
    full-graph feature table unusable here rather than merely discouraged.
    """
    cutoff = _fold_cutoff(fold)
    if ACCOUNT not in table.columns:
        raise GraphScopeError(
            f"the graph table for fold {fold.fold_id!r} has no {ACCOUNT!r} column, so it cannot "
            "be joined onto the scored entity"
        )
    missing = [field for field in registry.graph_fields if field not in table.columns]
    if missing:
        raise GraphScopeError(
            f"the graph table for fold {fold.fold_id!r} does not carry the declared fields "
            f"{missing}. A node may be absent; a column may not be."
        )
    beyond = 0
    latest = None
    if edges.height:
        stamps = edge_timestamps(edges)
        latest = stamps.max()
        if latest is None:
            raise GraphScopeError(f"every edge timestamp for fold {fold.fold_id!r} is null")
        beyond = int((stamps > cutoff).sum())
        if beyond:
            raise GraphScopeError(
                f"the graph table for fold {fold.fold_id!r} was fed {beyond} edge(s) at or after "
                f"{cutoff.isoformat()} (latest {latest.isoformat()}). That is a whole-corpus "
                "graph reaching into a fold's future: rebuild it from this fold's edges only "
                "(plan §8, spec §7.2)."
            )
    keep = [ACCOUNT, *registry.graph_fields]
    return FoldScopedTable(
        fold_id=fold.fold_id,
        as_of_ts=cutoff,
        kind="graph",
        frame=table.select(keep).unique(maintain_order=True, subset=[ACCOUNT]),
    )


def seal_rule_hit_table(
    *, fold: Fold, hits: pl.DataFrame, registry: FeatureRegistry
) -> FoldScopedTable:
    """Seal a rule-hit frame for one fold.

    A hit carries the timestamp of the last event that contributed to it, and that
    column is mandatory: without it there is no way to show the hit did not read the test
    window, and an unverifiable hit is as bad as a leaking one.
    """
    cutoff = _fold_cutoff(fold)
    for column in (ACCOUNT, RULE_COLUMN, SEVERITY_COLUMN, CONTRIBUTING_TS_COLUMN):
        if column not in hits.columns:
            raise GraphScopeError(
                f"the rule hit table for fold {fold.fold_id!r} is missing {column!r}; a hit that "
                "cannot say which events produced it cannot be sealed to a fold"
            )
    stamps = hits[CONTRIBUTING_TS_COLUMN]
    if not isinstance(stamps.dtype, pl.Datetime):
        raise GraphScopeError(
            f"the rule hit column {CONTRIBUTING_TS_COLUMN!r} must be a datetime, got {stamps.dtype}"
        )
    beyond = int((stamps > cutoff).sum())
    if beyond:
        raise GraphScopeError(
            f"{beyond} rule hit(s) for fold {fold.fold_id!r} were produced by events at or after "
            f"{cutoff.isoformat()}; rule hits must come from inside the fold"
        )
    unexpected = sorted(set(hits[RULE_COLUMN].cast(pl.String).unique().to_list()) - set(registry.rule_ids))
    if unexpected:
        raise GraphScopeError(
            f"the rule hit table for fold {fold.fold_id!r} reports rules {unexpected} that the "
            "registry does not declare"
        )
    return FoldScopedTable(
        fold_id=fold.fold_id,
        as_of_ts=cutoff,
        kind="rule hits",
        frame=hits.select([ACCOUNT, RULE_COLUMN, SEVERITY_COLUMN, CONTRIBUTING_TS_COLUMN]),
    )


@runtime_checkable
class GraphFeatureProvider(Protocol):
    """The P3a seam: given a fold, hand back that fold's own graph attributes."""

    def fold_graph(self, fold: Fold) -> FoldScopedTable | None:
        """Node attributes computed from ``fold``'s edges only, or None for "no graph"."""
        ...


@runtime_checkable
class RuleHitProvider(Protocol):
    """The P3b seam: given a fold, hand back the hits its rules produced inside it."""

    def fold_rule_hits(self, fold: Fold) -> FoldScopedTable | None:
        """Per-account rule severities inside ``fold``, or None when rules are not built."""
        ...


GraphProducer = Callable[[Fold], tuple[pl.DataFrame, pl.DataFrame] | None]
"""``fold -> (node table, the edges that produced it)``, which the seal then checks."""


def graph_provider_from_callable(produce: GraphProducer, registry: FeatureRegistry) -> GraphFeatureProvider:
    """Adapt a plain ``fold -> (nodes, edges)`` callable to the provider protocol.

    P3a hands over account-keyed tables today; this lets the seam be wired without
    asking that layer for a class it does not need, and the seal still runs on every
    fold, so the adapter cannot become a back door.
    """

    @dataclass(frozen=True, slots=True)
    class _Adapter:
        def fold_graph(self, fold: Fold) -> FoldScopedTable | None:
            produced = produce(fold)
            if produced is None:
                return None
            table, edges = produced
            return seal_node_table(fold=fold, table=table, edges=edges, registry=registry)

    return _Adapter()


def rule_provider_from_callable(
    produce: Callable[[Fold], pl.DataFrame | None], registry: FeatureRegistry
) -> RuleHitProvider:
    """Adapt a ``fold -> hit frame`` callable, sealing each fold's hits."""

    @dataclass(frozen=True, slots=True)
    class _Adapter:
        def fold_rule_hits(self, fold: Fold) -> FoldScopedTable | None:
            produced = produce(fold)
            if produced is None:
                return None
            return seal_rule_hit_table(fold=fold, hits=produced, registry=registry)

    return _Adapter()


def table_rows(table: FoldScopedTable, fold: Fold) -> pl.DataFrame:
    """The sealed frame, after the fold identity and cutoff have been re-checked."""
    return table.rows_for(fold)


def node_lookup(table: FoldScopedTable, fold: Fold, field: str) -> Mapping[str, object | None]:
    """account -> value for one declared node field, refusing the wrong fold."""
    frame = table.rows_for(fold)
    if field not in frame.columns:
        raise GraphScopeError(
            f"the sealed table for fold {fold.fold_id!r} does not carry field {field!r}"
        )
    keys = frame[ACCOUNT].cast(pl.String).to_list()
    return dict(zip(keys, frame[field].to_list(), strict=True))


def rule_matrix(table: FoldScopedTable, fold: Fold, registry: FeatureRegistry) -> pl.DataFrame:
    """Pivot sealed hits into one column per declared rule id, plus the roll-ups.

    A missing (account, rule) pair stays absent rather than becoming 0.0: the registry's
    null policy says a rule that did not fire inside the fold has no severity to report,
    and a zero would tell a reader the rule was checked and came back clean.

    A rule that fired on *nobody* in this fold still gets its column. The registry declares
    every rule entry with ``null_policy: null_when_no_rule_hit`` and a ``null_reason``, which
    is a promise that the column exists and is null — not that the column is absent, which
    is what a pivot of the hits alone produces and what made the fold join fail the moment
    a real provider supplied a fold where only three of twelve rules had fired. Publishing
    the declared set with nulls keeps the two states this seam distinguishes — "checked and
    nothing fired" and "nobody computed this" — apart, and keeps the join's column list a
    function of the registry rather than of the corpus.
    """
    frame = table.rows_for(fold)
    declared = list(dict.fromkeys(registry.rule_ids))
    if frame.height == 0:
        empty: dict[str, pl.Series] = {ACCOUNT: pl.Series(ACCOUNT, [], pl.String)}
        empty.update(
            {
                rule: pl.Series(rule, [], pl.Float64)
                for rule in declared
            }
        )
        return pl.DataFrame(empty)
    rollups = frame.group_by(ACCOUNT).agg(
        pl.col(RULE_COLUMN).n_unique().alias(ROLLUP_COUNT),
        pl.col(SEVERITY_COLUMN).max().alias(ROLLUP_MAX),
    )
    wide = frame.pivot(
        on=RULE_COLUMN,
        index=ACCOUNT,
        values=SEVERITY_COLUMN,
        aggregate_function="max",
        sort_columns=True,
    )
    joined = rollups.join(wide, on=ACCOUNT, how="left", maintain_order="left")
    missing = [rule for rule in declared if rule not in joined.columns]
    if missing:
        joined = joined.with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(rule) for rule in missing]
        )
    return joined.select([ACCOUNT, *declared])


def require_graph_provider(candidate: object) -> GraphFeatureProvider:
    """Reject anything that is not a provider — including a bare DataFrame.

    This is where a caller reaching for a whole-graph parquet gets stopped: a DataFrame
    is not a provider, so the mistake is a type error at the boundary rather than a model
    trained on the future.
    """
    if isinstance(candidate, pl.DataFrame):
        raise GraphScopeError(
            "a bare DataFrame was handed to the feature builder as the graph source. Graph "
            "features must arrive fold-sealed through a GraphFeatureProvider, because a "
            "whole-graph table cannot be checked for what it has already read (plan §8)."
        )
    if not isinstance(candidate, GraphFeatureProvider):
        raise GraphScopeError(
            f"the graph source {type(candidate).__name__} does not satisfy GraphFeatureProvider"
        )
    return candidate


def require_rule_provider(candidate: object) -> RuleHitProvider:
    """The same boundary for the rules layer."""
    if isinstance(candidate, pl.DataFrame):
        raise GraphScopeError(
            "a bare DataFrame was handed to the feature builder as the rule-hit source; rule "
            "severities must be fold-sealed too (plan §8)"
        )
    if not isinstance(candidate, RuleHitProvider):
        raise GraphScopeError(
            f"the rule source {type(candidate).__name__} does not satisfy RuleHitProvider"
        )
    return candidate


__all__ = [
    "ACCOUNT",
    "CONTRIBUTING_TS_COLUMN",
    "GraphFeatureProvider",
    "GraphScopeError",
    "FoldScopedTable",
    "ROLLUP_COUNT",
    "ROLLUP_MAX",
    "RuleHitProvider",
    "edge_timestamps",
    "graph_provider_from_callable",
    "node_lookup",
    "require_graph_provider",
    "require_rule_provider",
    "rule_matrix",
    "seal_node_table",
    "seal_rule_hit_table",
    "table_rows",
]
