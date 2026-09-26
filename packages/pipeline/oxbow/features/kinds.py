"""As-of-correct Polars kernels, one per declared feature kind.

WHY THE KERNELS ARE GENERIC AND THE FEATURE LIST IS NOT. Plan §8 requires the code to
dispatch on declared kinds rather than hardcode features, so this module knows about
rolling windows, distinct-count identities, integer quotients and fold-scoped joins — and
about no feature by name. config/features.yaml says which of those a column uses and with
what parameters; the two vocabularies are cross-checked at import.

THE AS-OF RULE, IN ONE SENTENCE. Every value published here is computed from rows whose
``event_ts_utc`` is at or before the scored row's own timestamp, within the declared
window: ``(cutoff - window, cutoff]`` with ``cutoff = event_ts_utc``.
``tests/test_leakage.py`` proves it by deleting every row after a cutoff, recomputing, and
asserting byte-identical values.

TWO AGGREGATION PATHS, CHOSEN BY WHAT IS EXACT AT EVERY ANCHOR.

* Additive aggregations (count, sum, integer mean, standard deviation, and the float
  scores built from them) run as a **difference of two running totals**. This is the only
  formulation that answers exactly for a row the ``where`` predicate excluded: at an
  outflow, "money received in the last month" is still a defined quantity. Evaluating a
  grouped rolling window at the last *included* row instead answers for that row's own
  window, which starts earlier than the anchor's — a superset, and the wrong number. Two
  backward as-of joins, one at the cutoff and one at ``cutoff - window``, bound the
  anchor's window precisely.
* Order statistics (min, max, median, quantile) run on grouped rolling windows, which are
  exact only where the anchor row is part of the measured population. The registry loader
  therefore refuses a filtered order statistic, and direction-partitioned entries stand in
  for what would otherwise have been filtered medians.

WHY SINGLETON PARTITIONS ARE COMPUTED SEPARATELY on the rolling path. The cost of a
grouped rolling window in Polars is dominated by partitions, not rows: measured on this
host with a synthetic 1.4 M-row frame in 1.23 M partitions, a 24-expression rolling pass
took 68 s over all of it and 1.3 s over the 175 k rows belonging to a multi-row partition.
A one-member partition has one window, which is the row itself, so its aggregates are
known in closed form. That is an optimisation that changes no value, and the leakage gate
exercises both paths against the same expectation.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

import polars as pl

from oxbow.features.registry import (
    FeatureRegistry,
    FeatureSpec,
    parse_window,
    sorted_categories,
)

# --- frame vocabulary the builder guarantees ------------------------------
ENTITY: Final = "entity"
EVENT_TS: Final = "event_ts_utc"
TXN_ID: Final = "txn_id"
ROW_UNIT: Final = "_row_unit"
POSITION: Final = "_pos"
VALUE: Final = "__value__"
PREDICATE_PREFIX: Final = "_pred_"
ROW_VALUE_PREFIX: Final = "_rv_"
GROUP_ROWS: Final = "_group_rows"
FLAG: Final = "__flag__"
PREVIOUS_TS: Final = "__previous_ts__"
NEXT_TS: Final = "__next_ts__"
DELTA: Final = "__delta__"
SQUARES: Final = "_squares"
START: Final = "_start"
ROWS: Final = "__rows__"

SECONDS_PER_DAY: Final = 86_400
ONE_MICROSECOND: Final = timedelta(microseconds=1)

# A window wide enough to be "everything so far", for the lifetime kinds.
LIFETIME: Final = timedelta(days=36_500)

# Additive aggregations, answerable for every anchor as a difference of running totals.
CUMULATIVE_WINDOW_AGGREGATIONS: Final[frozenset[str]] = frozenset(
    {"count", "sum", "mean_int", "std", "mean_float"}
)

REGISTRY_KIND_NAMES: Final[frozenset[str]] = frozenset(
    {
        "window_agg",
        "forward_window",
        "distinct_in_window",
        "cumulative_distinct",
        "first_seen_flag",
        "quotient_int",
        "diff_int",
        "row_flag",
        "row_value",
        "event_field",
        "float_stat",
        "recency",
        "cumulative",
        "graph_node",
        "rule_field",
    }
)


class KernelError(RuntimeError):
    """Raised when a validated registry entry cannot be executed.

    That is a contract break between config and code, so it is a hard failure rather than
    a column of nulls: a null column that should have been a number is the exact defect
    this layer exists to make impossible.
    """


def group_column(name: str) -> str:
    """Map a declared `group_by` key onto its column in the work frame.

    `direction` is declared as a partition key because that is what a reviewer reads;
    the frame carries `direction_sign`, which is the value the partition actually needs.
    Every other key is already a column name.
    """
    return "direction_sign" if name == "direction" else name


def partition_columns(names: Sequence[str]) -> list[str]:
    """The frame columns a declared partition resolves to, in declared order."""
    return [group_column(name) for name in names]


def source_column(name: str) -> str:
    """Map a declared `source` name onto its column in the work frame.

    The registry writes `row_unit`, the frame carries `_row_unit`: the declared name is
    what a reviewer reads in YAML, the prefixed one is what cannot collide with a feature
    id. Every other source is a canonical column and passes through unchanged.
    """
    return ROW_UNIT if name == "row_unit" else name


def predicate_column(name: str) -> str:
    """The materialised boolean column for a declared predicate name."""
    return f"{PREDICATE_PREFIX}{name}"


def row_value_column(name: str) -> str:
    """The materialised int64 column for a declared row-derived value."""
    return f"{ROW_VALUE_PREFIX}{name}"


def fold_column(feature_id: str) -> str:
    """The pre-joined fold-scoped column a graph or rule feature reads."""
    return f"_fold_{feature_id}"


@dataclass(slots=True)
class KernelCache:
    """Memo for partition row counts, which several features share.

    Only the *membership* of a filtered partition is memoised, never a sub-frame: the work
    frame gains a column after every feature, so a cached frame would be missing the
    source column of whichever feature runs next. Row counts depend solely on which rows a
    predicate keeps, and predicates are materialised before the evaluation loop — so this
    cache stays valid where a frame cache could not.
    """

    sizes: dict[tuple[str, tuple[str, ...], int, str], pl.DataFrame] = field(default_factory=dict)

    def group_sizes(
        self,
        subset: pl.DataFrame,
        groups: Sequence[str],
        where: str | None,
        namespace: str = "",
    ) -> pl.DataFrame:
        """How many rows each partition holds within this filtered population."""
        key = (where or "__all__", tuple(groups), subset.height, namespace)
        cached = self.sizes.get(key)
        if cached is None:
            cached = (
                subset.group_by(list(groups), maintain_order=True).len().rename({"len": GROUP_ROWS})
            )
            self.sizes[key] = cached
        return cached


@dataclass(frozen=True, slots=True)
class KernelContext:
    """What a kernel needs besides the frame: the registry and the shared memo."""

    registry: FeatureRegistry
    cache: KernelCache


def _filtered(work: pl.DataFrame, where: str | None) -> pl.DataFrame:
    """The rows a declared predicate keeps, derived fresh from the current work frame."""
    return work if where is None else work.filter(pl.col(where))


def _resolve_where(entry: FeatureSpec, work: pl.DataFrame) -> str | None:
    """Turn a declared `where` into a materialised boolean column, or None for no filter."""
    where = entry.where
    materialised = predicate_column(where or "")
    if materialised in work.columns:
        # Checked first, so a sealed build's late-guarded alias wins over the bare column
        # it shadows: `_pred_is_zero_value` is "zero-valued *and* admitted by this seal",
        # which is the population a trailing window is entitled to read.
        return materialised
    if where is None or where == "always":
        return None
    if where in work.columns:
        return where
    raise KernelError(f"feature {entry.id}: where {where!r} was never materialised")


def _is_group_start(column: str) -> pl.Expr:
    """True on the first row of each run of `column`, the frame being run-sorted.

    ``fill_null`` is load-bearing: the comparison against a shifted value is null on row
    zero, and a null condition leaves the first partition without a start value, which
    silently nulls every cumulative quantity in it.
    """
    return (pl.col(column) != pl.col(column).shift(1)).fill_null(True)


def _scatter(work: pl.DataFrame, computed: pl.DataFrame) -> pl.Series:
    """Put computed values back into the work frame's row order.

    Sorting on the materialised position index is vectorised; scattering row by row in
    Python over a million rows is not.
    """
    ordered = computed.sort(POSITION)
    if ordered.height != work.height:
        raise KernelError(f"alignment lost rows: {ordered.height} of {work.height}")
    return ordered[VALUE]


def _align_by_position(work: pl.DataFrame, computed: pl.DataFrame, default: object) -> pl.Series:
    """Scatter per-row values back by position, filling rows outside the computation.

    Used by quantities that are a property of the row itself: a first-seen flag must not be
    carried forward from the previous matching row, or every row after the first would
    claim to be a first sighting.
    """
    aligned = work.select(POSITION).join(
        computed.select([POSITION, VALUE]), on=POSITION, how="left", maintain_order="left"
    )
    return aligned[VALUE].fill_null(default)


def _carry_backward(
    work: pl.DataFrame, computed: pl.DataFrame, groups: Sequence[str], tolerance: timedelta | None
) -> pl.Series:
    """Join computed values onto every anchor row, strictly backwards in time.

    ``tolerance=None`` is reserved for lifetime quantities; a bounded window must pass its
    own duration, or a stale aggregate would be presented as a current one.
    """
    key = list(groups)
    left = work.select([*key, EVENT_TS, POSITION]).sort([*key, EVENT_TS])
    joined = left.join_asof(
        computed.sort([*key, EVENT_TS]),
        left_on=EVENT_TS,
        right_on=EVENT_TS,
        by=key,
        strategy="backward",
        tolerance=tolerance,
    )
    if joined.height != left.height:
        raise KernelError(
            f"as-of join changed the row count ({left.height} -> {joined.height}); a partition "
            "key is not unique per row"
        )
    return _scatter(work, joined.select([POSITION, VALUE]))


def _running_totals(
    population: pl.DataFrame, groups: Sequence[str], sources: Sequence[str]
) -> pl.DataFrame:
    """Per-row running totals inside the contributing population, restarted per partition.

    ``population`` is sorted here, so callers need not pre-sort. ``__rows__`` counts
    contributing rows from the start of the partition, which is the denominator every mean
    needs and the count a filtered window still owes the rows it excluded.

    The totals are accumulated *within* the partition rather than across the frame. A
    frame-wide cumulative sum minus the frame-wide prefix at the partition's first row is
    algebraically the same quantity, but in floating point both prefixes carry every row
    that sorted earlier — including rows of other partitions that lie after the anchor.
    Deleting one of those rows then changes the rounding of a value for a row it has
    nothing to do with, which is exactly what plan 8's truncation probe measures as a
    future read. Accumulating per partition keeps every total a function of its own
    partition's rows alone, and the sums stay small enough to subtract without the
    cancellation that lost the digits.
    """
    ordered = population.sort([*groups, EVENT_TS, TXN_ID])
    # A partition restarts when *any* declared key changes, not only the first: with a
    # (entity, counterparty) window, restarting on entity alone makes a row's running total
    # absorb the rows of sibling counterparties that sort before it, which reports a
    # per-pair count as a per-account count.
    boundary = pl.any_horizontal(*[_is_group_start(name) for name in groups])
    started = ordered.with_columns(boundary.alias(START))
    index = pl.int_range(0, started.height, dtype=pl.Int64)
    run_start_index = pl.when(pl.col(START)).then(index).forward_fill()
    expressions: list[pl.Expr] = [pl.col(name) for name in (*groups, EVENT_TS, POSITION)]
    expressions.append((index - run_start_index + 1).cast(pl.Int64).alias(ROWS))
    for name in sources:
        total = pl.col(name).cum_sum().over([*groups])
        # The total keeps its source's dtype. Money is integer minor units (DEV-005) and a
        # money column is Int64 on the way in, so it needs no cast; what the unconditional
        # `.cast(pl.Int64)` was actually doing to the float sources is twofold and both bad
        # -- it truncated a fractional running total toward zero, and it refused the whole
        # stage on the first corpus big enough to hit it. `_squares` is the case: a single
        # PaySim transfer of 3.5e9 minor units squares to 1.2e19, past `i64::MAX`, and the
        # sum of squares for one account's 30-day window died with
        # `InvalidOperationError: conversion from f64 to i64 failed in column '_squares'`.
        # A moment is not an amount; it does not get an integer type imposed on it.
        expressions.append(total.alias(f"c_{name}"))
    return started.select(expressions)


def _assert_source_is_totalled(population: pl.DataFrame, sources: Sequence[str]) -> None:
    """Refuse a null-bearing source before it reaches a running total.

    A cumulative sum over a column with nulls stops answering after the first null, which
    would publish a truncated total as a real one. The fix is always a predicate that
    excludes those rows, never a zero fill: zero-filling money invents evidence
    (03 A rule 2).
    """
    for name in sources:
        nulls = int(population[name].null_count())
        if nulls:
            raise KernelError(
                f"the contributing population carries {nulls} null(s) in {name!r}. Declare a "
                "`where` predicate that excludes them; the alternative is a zero that reads "
                "like a measurement."
            )


def _window_totals(
    anchors: pl.DataFrame,
    population: pl.DataFrame,
    *,
    groups: Sequence[str],
    sources: Sequence[str],
    window: timedelta,
) -> Mapping[str, pl.Series]:
    """Exact ``(cutoff - window, cutoff]`` totals, answered for every anchor row.

    The window total is the running total at the last contributing row at or before the
    cutoff, minus the running total at the last contributing row at or before
    ``cutoff - window``. Both are backward as-of joins against the population, so an anchor
    the predicate excluded still gets its own answer rather than a neighbour's.
    """
    keys = list(groups)
    names = list(sources)
    if population.height == 0:
        return {**{name: _zeros(anchors.height) for name in names}, ROWS: _zeros(anchors.height)}
    _assert_source_is_totalled(population, names)
    keyed = _running_totals(population, keys, names)
    probes = anchors.select([*keys, EVENT_TS, POSITION]).sort([*keys, EVENT_TS])
    up_to = probes.join_asof(
        keyed.sort([*keys, EVENT_TS]),
        left_on=EVENT_TS,
        right_on=EVENT_TS,
        by=keys,
        strategy="backward",
    )
    boundary = keyed.rename(
        {EVENT_TS: "_b_ts", ROWS: "_b_rows", **{f"c_{name}": f"b_{name}" for name in names}}
    ).select([*keys, "_b_ts", "_b_rows", *[f"b_{name}" for name in names]])
    # The window is left-open at cutoff - window, so the tail is the running total at the
    # last contributing row *strictly before* that instant: probing one microsecond short
    # is what keeps a row sitting exactly on the boundary inside the window rather than
    # silently subtracted out of it.
    before = probes.with_columns(
        (pl.col(EVENT_TS) - window - ONE_MICROSECOND).alias("_probe_ts")
    ).join_asof(
        boundary.sort([*keys, "_b_ts"]),
        left_on="_probe_ts",
        right_on="_b_ts",
        by=keys,
        strategy="backward",
    )
    head = up_to.sort(POSITION)
    tail = before.sort(POSITION)
    out: dict[str, pl.Series] = {
        ROWS: (head[ROWS].fill_null(0) - tail["_b_rows"].fill_null(0)).cast(pl.Int64)
    }
    for name in names:
        out[name] = (head[f"c_{name}"].fill_null(0) - tail[f"b_{name}"].fill_null(0)).cast(pl.Int64)
    return out


def _zeros(height: int) -> pl.Series:
    return pl.Series("__zero__", [0] * height, dtype=pl.Int64)


def _sample_std(total: pl.Series, squares: pl.Series, counts: pl.Series) -> pl.Series:
    """Sample standard deviation from two running totals, in one pass.

    ``sqrt((Sxx - Sx*Sx/n) / (n - 1))`` is the variance algebraically, so no second window
    is needed. A negative residue inside the bracket is clamped to zero, and fewer than two
    observations is null rather than 0.0: a dispersion measured from one sample does not
    exist.
    """
    frame = pl.DataFrame(
        {
            "s": total.cast(pl.Float64),
            "q": squares.cast(pl.Float64),
            "n": counts.cast(pl.Float64),
        }
    )
    return frame.select(
        pl.when(pl.col("n") > 1)
        .then(
            ((pl.col("q") - (pl.col("s") * pl.col("s")) / pl.col("n")) / (pl.col("n") - 1))
            .clip(0.0, None)
            .sqrt()
        )
        .otherwise(None)
        .alias(VALUE)
    )[VALUE]


def _windowed(
    work: pl.DataFrame,
    context: KernelContext,
    *,
    group_by: Sequence[str],
    source: str,
    agg: str,
    window: timedelta,
    quantile: float | None = None,
    where: str | None = None,
    cache_ns: str = "",
) -> pl.Series:
    """Aggregate `source` over (cutoff - window, cutoff] inside each partition.

    Returns one value per row of `work`, in `work`'s row order.
    """
    groups = partition_columns(group_by)
    if agg in CUMULATIVE_WINDOW_AGGREGATIONS:
        return _cumulative_aggregate(
            work,
            group_by=groups,
            source=source,
            agg=agg,
            window=window,
            where=where,
        )
    subset = _filtered(work, where)
    if subset.height == 0:
        return pl.Series(VALUE, [None] * work.height, dtype=_agg_dtype(agg))
    source = source_column(source)
    sized = subset.join(
        context.cache.group_sizes(subset, groups, where, cache_ns),
        on=groups,
        how="left",
        maintain_order="left",
    ).with_columns(pl.col(GROUP_ROWS).cast(pl.Int64))
    big = sized.filter(pl.col(GROUP_ROWS) > 1)
    small = sized.filter(pl.col(GROUP_ROWS) == 1)
    frames: list[pl.DataFrame] = []
    if big.height:
        frames.append(_rolling_frame(big, groups, source, agg, window, quantile))
    if small.height:
        frames.append(_closed_form_frame(small, source, agg))
    computed = pl.concat(frames, how="vertical_relaxed")
    if computed.height == work.height:
        return _scatter(work, computed)
    return _carry_backward(work, computed, groups, window)


def _agg_dtype(agg: str) -> pl.DataType:
    return pl.Float64 if agg in {"std", "mean_float"} else pl.Int64


def _cumulative_aggregate(
    work: pl.DataFrame,
    *,
    group_by: Sequence[str],
    source: str,
    agg: str,
    window: timedelta,
    where: str | None,
    population: pl.DataFrame | None = None,
) -> pl.Series:
    """count / sum / integer mean / float mean / standard deviation, from running totals."""
    source = source_column(source)
    subset = _filtered(work, where) if population is None else population
    if source == ROW_UNIT:
        totals = _window_totals(work, subset, groups=group_by, sources=[], window=window)
        return totals[ROWS].rename(VALUE)
    names = [source]
    if agg == "std":
        subset = subset.with_columns((pl.col(source).cast(pl.Float64) ** 2).alias(SQUARES))
        names = [source, SQUARES]
    totals = _window_totals(work, subset, groups=group_by, sources=names, window=window)
    counts = totals[ROWS]
    if agg == "count":
        return counts.rename(VALUE)
    total = totals[source]
    if agg == "sum":
        return total.rename(VALUE)
    if agg in {"mean_int", "mean_float"}:
        pair = pl.DataFrame({"t": total, "n": counts})
        if agg == "mean_int":
            # An integer mean is an exact floor of the quotient: no float ever sees the
            # money, and a zero-count window is null rather than a fabricated zero.
            formula = pl.when(pl.col("n") > 0).then(pl.col("t") // pl.col("n")).otherwise(None)
            dtype = pl.Int64
        else:
            formula = (
                pl.when(pl.col("n") > 0)
                .then(pl.col("t").cast(pl.Float64) / pl.col("n").cast(pl.Float64))
                .otherwise(None)
            )
            dtype = pl.Float64
        return pair.select(formula.cast(dtype).alias(VALUE))[VALUE]
    if agg == "std":
        return _sample_std(total, totals[SQUARES], counts).rename(VALUE)
    raise KernelError(f"cumulative aggregation {agg!r} is not implemented")


def _rolling_frame(
    frame: pl.DataFrame,
    groups: Sequence[str],
    source: str,
    agg: str,
    window: timedelta,
    quantile: float | None,
) -> pl.DataFrame:
    """Grouped rolling aggregation, for partitions holding more than one row."""
    key = list(groups)
    try:
        if agg == "min":
            expr = pl.col(source).rolling_min_by(EVENT_TS, window).over(key).alias(VALUE)
        elif agg == "max":
            expr = pl.col(source).rolling_max_by(EVENT_TS, window).over(key).alias(VALUE)
        elif agg == "median":
            expr = (
                pl.col(source)
                .rolling_quantile_by(EVENT_TS, window, quantile=0.5, interpolation="lower")
                .over(key)
                .alias(VALUE)
            )
        elif agg == "quantile":
            if quantile is None:
                raise KernelError(f"agg quantile needs a quantile (source {source})")
            expr = (
                pl.col(source)
                .rolling_quantile_by(EVENT_TS, window, quantile=quantile, interpolation="lower")
                .over(key)
                .alias(VALUE)
            )
        else:
            raise KernelError(f"aggregation {agg!r} does not use the rolling kernel")
        return frame.with_columns(expr)
    except pl.exceptions.InvalidOperationError as exc:
        nulls = int(frame.get_column(source).null_count()) if source in frame.columns else -1
        raise KernelError(
            f"rolling {agg!r} over {source!r} failed: {exc}. The column carries {nulls} null(s) "
            "inside this population; exclude them with a `where` predicate rather than "
            "zero-filling money."
        ) from exc


def _closed_form_frame(frame: pl.DataFrame, source: str, agg: str) -> pl.DataFrame:
    """The exact answer where the window can only contain the row itself."""
    if agg in {"min", "max", "median", "quantile"}:
        return frame.with_columns(pl.col(source).cast(pl.Int64).alias(VALUE))
    raise KernelError(f"aggregation {agg!r} has no single-row closed form on this path")


def _previous_pair_ts(frame: pl.DataFrame, groups: Sequence[str], subject: str) -> pl.DataFrame:
    """Timestamp of the previous row sharing (groups..., subject), or null.

    The frame is sorted by the full pair key, so a global shift lands on the previous
    occurrence — and the equality guard is what stops that shift crossing into the
    neighbouring pair, which would report a false repeat.
    """
    ordered = frame.sort([*groups, subject, EVENT_TS, TXN_ID])
    same_pair = pl.all_horizontal(
        [pl.col(name) == pl.col(name).shift(1) for name in [*groups, subject]]
    )
    return ordered.with_columns(
        pl.when(same_pair).then(pl.col(EVENT_TS).shift(1)).otherwise(None).alias(PREVIOUS_TS)
    )


def _next_pair_ts(frame: pl.DataFrame, groups: Sequence[str], subject: str) -> pl.DataFrame:
    """Timestamp of the next row sharing (groups..., subject), or null.

    The mirror of :func:`_previous_pair_ts`, and the same equality guard: without it the
    shift crosses into the neighbouring key and reports a next occurrence that belongs to a
    different counterparty or a different day.
    """
    ordered = frame.sort([*groups, subject, EVENT_TS, TXN_ID])
    same_pair = pl.all_horizontal(
        [pl.col(name) == pl.col(name).shift(-1) for name in [*groups, subject]]
    )
    return ordered.with_columns(
        pl.when(same_pair).then(pl.col(EVENT_TS).shift(-1)).otherwise(None).alias(NEXT_TS)
    )


def _coverage_running_total(
    population: pl.DataFrame, groups: Sequence[str], subject: str, window: timedelta
) -> pl.DataFrame:
    """Sweep events for "the key is present in the anchor's window", as a running total.

    Each contributing row emits ``+1`` where its coverage begins — at the row itself — and
    ``-1`` where it ends, at the earlier of the key's next occurrence and ``row + window``.
    Collapsing to one row per instant before the running total is what makes ties at a
    boundary unambiguous: an anchor sitting exactly on an instant where one key expires and
    another begins reads the total after both, which is the count for a window closed at its
    right end and open at its left.
    """
    spans = _next_pair_ts(population, groups, subject).with_columns(
        (pl.col(EVENT_TS) + window).alias("_expires")
    )
    ended = (
        pl.when(pl.col(NEXT_TS).is_null())
        .then(pl.col("_expires"))
        .otherwise(pl.min_horizontal(pl.col(NEXT_TS), pl.col("_expires")))
        .alias("_end_ts")
    )
    opened = spans.select([*groups, EVENT_TS, pl.lit(1, dtype=pl.Int64).alias(DELTA)])
    closed = spans.select([*groups, ended.alias(EVENT_TS), pl.lit(-1, dtype=pl.Int64).alias(DELTA)])
    events = (
        pl.concat([opened, closed], how="vertical_relaxed")
        .group_by([*groups, EVENT_TS])
        .agg(pl.col(DELTA).sum().alias(DELTA))
        .sort([*groups, EVENT_TS])
    )
    # The partition-restart running total is written out here rather than borrowed from
    # :func:`_running_totals`, which keys its partitions on ``(event_ts_utc, txn_id)`` and
    # would need a transaction id invented for an instant that several transactions map to.
    boundary = pl.any_horizontal(*[_is_group_start(name) for name in groups])
    total = pl.col(DELTA).cum_sum()
    base = pl.when(boundary).then(total - pl.col(DELTA)).forward_fill()
    return events.select([*groups, EVENT_TS, (total - base).cast(pl.Int64).alias(VALUE)])


# --- kernels, one per declared kind ---------------------------------------


def _kind_window_agg(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    if entry.agg is None or entry.source is None:
        raise KernelError(f"feature {entry.id}: window_agg needs agg and source")
    window = parse_window(entry.window)
    if window is None:
        raise KernelError(f"feature {entry.id}: window_agg needs a rolling window")
    values = _windowed(
        work,
        context,
        group_by=entry.group_by,
        source=entry.source,
        agg=entry.agg,
        window=window,
        quantile=entry.quantile,
        where=_resolve_where(entry, work),
        cache_ns=entry.id,
    )
    # A count or sum over an empty window is a measured zero; an order statistic over one
    # is unknown. The registry says which, through null_policy.
    filled = values.fill_null(0) if entry.null_policy == "never_null" else values
    return filled.cast(entry.polars_dtype, strict=True).rename(entry.id)


def _kind_forward_window(
    work: pl.DataFrame, entry: FeatureSpec, context: KernelContext
) -> pl.Series:
    """Aggregate over [cutoff, cutoff + window): the outcome side, never a model input.

    Exposure E_i is defined by plan §11 as the value still interceptable *after* the
    triggering event, so the quant layer has to compute forward windows; and because a
    forward window is exactly what a leakage gate must catch, the registry refuses to
    publish one as a matrix column. Implemented as the backward cumulative kernel over a
    time-reversed frame — one aggregation path, so the two directions cannot drift apart,
    and the materialised position index restores the published order.
    """
    if entry.agg is None or entry.source is None:
        raise KernelError(f"feature {entry.id}: forward_window needs agg and source")
    window = parse_window(entry.window)
    if window is None:
        raise KernelError(f"feature {entry.id}: forward_window needs a duration")
    flipped = work.sort([ENTITY, EVENT_TS, TXN_ID], descending=[False, True, True])[::-1]
    values = _cumulative_aggregate(
        flipped,
        group_by=entry.group_by,
        source=entry.source,
        agg=entry.agg,
        window=window,
        where=_resolve_where(entry, flipped),
    )
    # `_scatter` puts the reversed-frame answers back into the published frame's row order
    # through the materialised position index. A Series has no `sort_by` in this Polars
    # version, and even if it did, sorting by the index is the vectorised form of the same
    # thing that a row-wise Python reindex would do.
    paired = pl.DataFrame({POSITION: flipped[POSITION], VALUE: values})
    return _scatter(work, paired).rename(entry.id)


def _kind_distinct_in_window(
    work: pl.DataFrame, entry: FeatureSpec, context: KernelContext
) -> pl.Series:
    """Exact distinct-key count per trailing window, by counting key *coverages*.

    WHY THE OBVIOUS SHORTCUT IS WRONG. The usual vectorised trick flags each row whose
    previous occurrence of the key lies before *that row's own* window start, then sums the
    flags over the window. It under-counts, and does so at exactly the boundary a reviewer
    would never notice: a key first seen before the window and re-seen inside it has no
    flagged row in the window at all, so it contributes nothing. The test depends on the
    anchor's window start, not the row's own, and no per-row flag can know the anchor's.
    ``tests/unit/test_p2_distinct_counts.py`` plants that case (local day 1 activity at
    01:00 and 20:00, anchor thirty-one and a half days later) where the shortcut answers 1
    and the true count is 2.

    WHAT IS EXACT. A row ``r`` of key ``k`` makes ``k`` present for every anchor at or after
    ``r`` and before the earlier of ``k``'s next occurrence and ``r + window``: that half-open
    interval is exactly the set of anchors whose window ends at a moment when ``k``'s most
    recent visit is ``r``. Key-present is then "some interval covers the anchor", which is a
    sweep — ``+1`` at each start, ``-1`` at each end, running total, backward as-of onto the
    anchors. Every event at or before an anchor is derived from rows at or before that same
    anchor (a start is its row, an end is either its row plus a duration or a next occurrence
    no later than the end), so the result is truncation-invariant, and a rolling ``n_unique``
    — which Polars does not have — is not needed.
    """
    if entry.subject is None:
        raise KernelError(f"feature {entry.id}: distinct_in_window needs a subject")
    window = parse_window(entry.window)
    if window is None:
        raise KernelError(f"feature {entry.id}: distinct_in_window needs a rolling window")
    groups = partition_columns(entry.group_by)
    subset = _filtered(work, _resolve_where(entry, work))
    if subset.height == 0:
        return pl.Series(entry.id, [0] * work.height, dtype=pl.Int64)
    spans = _coverage_running_total(subset, groups, entry.subject, window)
    covers = _carry_backward(work, spans, groups, None).cast(pl.Int64).fill_null(0)
    return covers.rename(entry.id)


def _kind_cumulative_distinct(
    work: pl.DataFrame, entry: FeatureSpec, context: KernelContext
) -> pl.Series:
    """How many distinct subjects this partition has ever met, as of each row.

    Exact because it counts *first-ever* occurrences: a key contributes on the one row that
    introduced it, and the running total of that flag is the distinct count, which needs no
    knowledge of what comes later. A bounded-window version of the same question is not
    expressible this way, which is why the registry restricts the kind to lifetime windows.
    """
    if entry.subject is None:
        raise KernelError(f"feature {entry.id}: cumulative_distinct needs a subject")
    where = _resolve_where(entry, work)
    subset = _filtered(work, where)
    if subset.height == 0:
        return pl.Series(entry.id, [0] * work.height, dtype=pl.Int64)
    groups = partition_columns(entry.group_by)
    paired = _previous_pair_ts(subset, groups, entry.subject)
    flagged = paired.with_columns(pl.col(PREVIOUS_TS).is_null().cast(pl.Int64).alias(FLAG))
    totals = _cumulative_aggregate(
        work,
        group_by=groups,
        source=FLAG,
        agg="sum",
        window=LIFETIME,
        where=None,
        population=flagged,
    )
    return totals.rename(entry.id)


def _kind_first_seen_flag(
    work: pl.DataFrame, entry: FeatureSpec, context: KernelContext
) -> pl.Series:
    """1 on the row that first ever pairs this partition with the subject."""
    if entry.subject is None:
        raise KernelError(f"feature {entry.id}: first_seen_flag needs a subject")
    where = _resolve_where(entry, work)
    subset = _filtered(work, where)
    if subset.height == 0:
        return pl.Series(entry.id, [0] * work.height, dtype=pl.Int64)
    paired = _previous_pair_ts(subset, partition_columns(entry.group_by), entry.subject)
    first = paired.select([POSITION, pl.col(PREVIOUS_TS).is_null().cast(pl.Int64).alias(VALUE)])
    return _align_by_position(work, first, 0).rename(entry.id)


def _kind_quotient_int(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    """Exact integer ratio, scaled then truncated toward zero. No float touches money.

    Truncation is explicit rather than relying on ``//``: floor division on a negative
    numerator rounds away from zero and would make a negative net flow read as a *larger*
    kept share than it is.
    """
    if entry.numerator is None or entry.denominator is None or not entry.scale:
        raise KernelError(
            f"feature {entry.id}: quotient_int needs numerator, denominator and scale"
        )
    power = entry.denominator_power or 1
    numerator = pl.col(entry.numerator).cast(pl.Int64)
    denominator = pl.col(entry.denominator).cast(pl.Int64)
    if power == 2:
        denominator = denominator * denominator
    elif power != 1:
        raise KernelError(f"feature {entry.id}: denominator_power {power} is not supported")
    scale = pl.lit(entry.scale, dtype=pl.Int64)
    magnitude = (numerator.abs() * scale) // denominator
    value = pl.when(denominator > 0).then(magnitude * numerator.sign()).otherwise(None)
    return work.select(value.cast(pl.Int64).alias(entry.id))[entry.id]


def _kind_diff_int(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    if entry.a is None or entry.b is None:
        raise KernelError(f"feature {entry.id}: diff_int needs a and b")
    return work.select((pl.col(entry.a) - pl.col(entry.b)).cast(pl.Int64).alias(entry.id))[entry.id]


def _kind_row_flag(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    if entry.predicate is None:
        raise KernelError(f"feature {entry.id}: row_flag needs a predicate")
    column = predicate_column(entry.predicate)
    if column not in work.columns:
        raise KernelError(f"feature {entry.id}: predicate {entry.predicate!r} was not materialised")
    return work.select(pl.col(column).cast(pl.Boolean).alias(entry.id))[entry.id]


def _kind_row_value(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    if entry.value is None:
        raise KernelError(f"feature {entry.id}: row_value needs a value")
    column = row_value_column(entry.value)
    if column not in work.columns:
        raise KernelError(f"feature {entry.id}: row value {entry.value!r} was not materialised")
    return work.select(pl.col(column).cast(pl.Int64).alias(entry.id))[entry.id]


def _kind_event_field(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    """Code a category from the declared, sorted list. An unknown value fails, naming it.

    A category outside the registry's list is contract drift — a transaction type or rail
    nobody has reasoned about. Mapping it to a silent -1 would let a model learn an
    undocumented bucket, so the run stops and a human decides.
    """
    if entry.source is None or not entry.categories:
        raise KernelError(f"feature {entry.id}: event_field needs source and categories")
    ordered = sorted_categories(entry.categories)
    lookup = pl.DataFrame({"_category": ordered, entry.id: list(range(len(ordered)))})
    source = work.select(pl.col(entry.source).cast(pl.String).alias("_category"))
    coded = source.join(lookup, on="_category", how="left", maintain_order="left")
    unknown = coded.filter(pl.col(entry.id).is_null() & pl.col("_category").is_not_null())
    if unknown.height:
        sample = unknown["_category"].unique(maintain_order=True).head(5).to_list()
        raise KernelError(
            f"feature {entry.id}: source {entry.source!r} carries values outside the declared "
            f"categories: {sample}. Add them to config/features.yaml deliberately."
        )
    return coded[entry.id].cast(pl.Int32, strict=True).rename(entry.id)


def _kind_float_stat(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    """Declared float scores. Each is a statistic, never money, and says so in the registry.

    Undefined cases — one observed value, zero dispersion — are null rather than 0.0,
    because 0.0 reads as "this amount is typical" when the truth is "there is nothing to
    compare against" (03 A rule 2).
    """
    formula = entry.formula
    if formula is None or entry.source is None:
        raise KernelError(f"feature {entry.id}: float_stat needs formula and source")
    window = parse_window(entry.window)
    if window is None:
        raise KernelError(f"feature {entry.id}: float_stat needs a trailing reference window")
    population = _filtered(work, _resolve_where(entry, work))
    stat_source = source_column(entry.source or "")
    population = (
        population.with_columns(pl.col(stat_source).alias(stat_source)) if False else population
    )
    if formula == "benford_dev":
        # Benford's law makes log10 of the mantissa uniform on [0,1), so its mean is 0.5.
        # One running mean answers the question; a chi-square over a digit histogram would
        # need ten filtered windows per row.
        base = population.with_columns(
            (pl.col(entry.source).cast(pl.Float64).log10() % 1.0).abs().alias("_mantissa_log")
        )
        totals = _window_totals(
            work, base, groups=entry.group_by, sources=["_mantissa_log"], window=window
        )
        pair = pl.DataFrame({"s": totals["_mantissa_log"], "n": totals[ROWS]})
        scored = pair.select(
            (
                (
                    (
                        pl.when(pl.col("n") > 0)
                        .then(pl.col("s").cast(pl.Float64) / pl.col("n"))
                        .otherwise(None)
                    )
                    - 0.5
                ).abs()
                * 2.0
            )
            .clip(0.0, 1.0)
            .alias(entry.id)
        )[entry.id]
        return scored
    with_squares = population.with_columns(
        (pl.col(stat_source).cast(pl.Float64) ** 2).alias(SQUARES)
    )
    totals = _window_totals(
        work, with_squares, groups=entry.group_by, sources=[stat_source, SQUARES], window=window
    )
    counts = totals[ROWS]
    triple = pl.DataFrame(
        {
            "own": work[stat_source].cast(pl.Float64),
            "total": totals[stat_source].cast(pl.Float64),
            "squares": totals[SQUARES].cast(pl.Float64),
            "n": counts.cast(pl.Float64),
        }
    )
    if formula == "robust_z":
        shape = "z"
    elif formula == "coefficient_of_variation":
        shape = "cv"
    elif formula == "burstiness":
        shape = "burst"
    else:
        raise KernelError(f"feature {entry.id}: formula {formula!r} is not implemented")
    values = triple.select(_score_expression(shape).alias(entry.id))[entry.id]
    return pl.Series(
        entry.id,
        [None if _not_finite(value) else value for value in values.to_list()],
        dtype=pl.Float64,
    )


def _score_expression(shape: str) -> pl.Expr:
    """The three declared float shapes, over the columns `_kind_float_stat` assembles.

    Mean and dispersion come from the running totals rather than a second window, and a
    zero-spread or single-observation history returns null: a z of 0.0 would read as
    "perfectly typical" when the truth is "nothing to compare against".
    """
    mean = pl.when(pl.col("n") > 0).then(pl.col("total") / pl.col("n")).otherwise(None)
    variance = (pl.col("squares") - (pl.col("total") * pl.col("total")) / pl.col("n")) / (
        pl.col("n") - 1
    )
    std = pl.when(pl.col("n") > 1).then(variance.clip(0.0, None).sqrt()).otherwise(None)
    if shape == "z":
        combined = (pl.col("own") - mean) / std
    elif shape == "cv":
        combined = std / mean
    else:
        combined = (std - mean) / (std + mean)
    return pl.when(std.is_not_null() & (std > 0.0)).then(combined).otherwise(None).cast(pl.Float64)


def _not_finite(value: float | None) -> bool:
    """True for null, NaN and the infinities a degenerate division can produce."""
    return value is None or math.isnan(value) or math.isinf(value)


def _kind_recency(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    """Whole units between the scored row and the last row satisfying the predicate.

    A backward as-of join with no tolerance: recency is a lifetime question, and every
    candidate is at or before the anchor's own timestamp. When this row is itself the match
    the gap is zero, which is the honest reading of "hours since the last inflow" measured
    on an inflow.
    """
    if entry.where is None:
        raise KernelError(f"feature {entry.id}: recency needs a where predicate")
    unit = _unit_seconds(entry.id)
    subset = _filtered(work, _resolve_where(entry, work))
    if subset.height == 0:
        return pl.Series(entry.id, [None] * work.height, dtype=pl.Int64)
    groups = partition_columns(entry.group_by)
    matched = subset.select([*groups, EVENT_TS]).rename({EVENT_TS: "_matched_ts"})
    left = work.select([*groups, EVENT_TS, POSITION]).sort([*groups, EVENT_TS])
    joined = left.join_asof(
        matched.sort([*groups, "_matched_ts"]),
        left_on=EVENT_TS,
        right_on="_matched_ts",
        by=groups,
        strategy="backward",
    )
    gaps = (joined[EVENT_TS] - joined["_matched_ts"]).dt.total_seconds().cast(pl.Float64)
    values = (gaps / unit).floor().cast(pl.Int64)
    aligned = _scatter(work, joined.select([POSITION, values.alias(VALUE)]))
    return aligned.rename(entry.id)


def _unit_seconds(feature_id: str) -> float:
    """Seconds per published unit, read from the id's declared suffix."""
    if feature_id.endswith("_s"):
        return 1.0
    if feature_id.endswith("_hours"):
        return 3_600.0
    if feature_id.endswith("_days"):
        return 86_400.0
    raise KernelError(f"feature {feature_id}: the recency unit is not named in the id")


def _kind_cumulative(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    """A lifetime as-of quantity: everything in the partition up to and including the row."""
    if entry.agg is None or entry.source is None:
        raise KernelError(f"feature {entry.id}: cumulative needs agg and source")
    if entry.window != "lifetime":
        raise KernelError(f"feature {entry.id}: cumulative needs window 'lifetime'")
    groups = partition_columns(entry.group_by)
    where = _resolve_where(entry, work)
    subset = _filtered(work, where)
    if subset.height == 0:
        return pl.Series(entry.id, [None] * work.height, dtype=pl.Int64)
    if entry.agg == "age_days":
        ordered = subset.sort([*groups, EVENT_TS, TXN_ID])
        started = ordered.with_columns(_is_group_start(groups[0]).alias(START))
        first_ts = pl.when(pl.col(START)).then(pl.col(EVENT_TS)).forward_fill()
        computed = started.select(
            [
                *groups,
                EVENT_TS,
                POSITION,
                ((pl.col(EVENT_TS) - first_ts).dt.total_seconds() / SECONDS_PER_DAY)
                .floor()
                .cast(pl.Int64)
                .alias(VALUE),
            ]
        )
        return _align(work, computed, groups, where).rename(entry.id)
    if entry.agg not in {"count", "sum"}:
        raise KernelError(f"feature {entry.id}: cumulative agg {entry.agg!r} is not implemented")
    values = _cumulative_aggregate(
        work,
        group_by=groups,
        source=ROW_UNIT if entry.agg == "count" else entry.source,
        agg=entry.agg,
        window=LIFETIME,
        where=where,
        population=subset,
    )
    return values.rename(entry.id)


def _align(
    work: pl.DataFrame, computed: pl.DataFrame, groups: Sequence[str], where: str | None
) -> pl.Series:
    """Return a lifetime computation to the work frame's row order."""
    if where is None and list(groups) == [ENTITY]:
        return _scatter(work, computed)
    return _carry_backward(work, computed, groups, None)


def _kind_graph_node(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    """Fold-scoped graph values arrive pre-joined; see oxbow.features.fold_scope."""
    return _prejoined(work, entry)


def _kind_rule_field(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series:
    return _prejoined(work, entry)


def _prejoined(work: pl.DataFrame, entry: FeatureSpec) -> pl.Series:
    column = fold_column(entry.id)
    if column not in work.columns:
        raise KernelError(
            f"feature {entry.id}: the builder supplied no fold-scoped column {column}. Graph "
            "and rule features are read from a fold-sealed table or not at all."
        )
    return work.select(pl.col(column).cast(entry.polars_dtype, strict=False).alias(entry.id))[
        entry.id
    ]


Kernel = Callable[[pl.DataFrame, FeatureSpec, KernelContext], pl.Series]

KIND_IMPLEMENTATIONS: Final[dict[str, Kernel]] = {
    "window_agg": _kind_window_agg,
    "forward_window": _kind_forward_window,
    "distinct_in_window": _kind_distinct_in_window,
    "cumulative_distinct": _kind_cumulative_distinct,
    "first_seen_flag": _kind_first_seen_flag,
    "quotient_int": _kind_quotient_int,
    "diff_int": _kind_diff_int,
    "row_flag": _kind_row_flag,
    "row_value": _kind_row_value,
    "event_field": _kind_event_field,
    "float_stat": _kind_float_stat,
    "recency": _kind_recency,
    "cumulative": _kind_cumulative,
    "graph_node": _kind_graph_node,
    "rule_field": _kind_rule_field,
}


def dispatch(kind: str) -> Kernel:
    """Look up the kernel for a declared kind.

    The registry loader refuses a kind outside its vocabulary, so reaching here without an
    implementation means the two lists diverged — a programming error, raised at import by
    the guard below rather than in the middle of a run.
    """
    if kind not in KIND_IMPLEMENTATIONS:
        raise KernelError(f"kind {kind!r} has no kernel implementation")
    return KIND_IMPLEMENTATIONS[kind]


_DIVERGED = REGISTRY_KIND_NAMES ^ frozenset(KIND_IMPLEMENTATIONS)
if _DIVERGED:  # pragma: no cover - an import-time failure, not a test failure
    raise KernelError(
        f"the dispatch table and the registry vocabulary disagree: {sorted(_DIVERGED)}"
    )


def window_totals(
    anchors: pl.DataFrame,
    population: pl.DataFrame,
    *,
    group_by: Sequence[str],
    sources: Sequence[str],
    window: timedelta,
) -> Mapping[str, pl.Series]:
    """Public entry to the exact window kernel, used by the deliberate-leak fixture.

    The gate has to be able to build a column that really does read forward, and the way to
    show it bites is to compute that column through the same kernel the honest ones use.
    """
    return _window_totals(
        anchors, population, groups=list(group_by), sources=list(sources), window=window
    )


__all__ = [
    "CUMULATIVE_WINDOW_AGGREGATIONS",
    "ENTITY",
    "EVENT_TS",
    "KIND_IMPLEMENTATIONS",
    "POSITION",
    "PREDICATE_PREFIX",
    "ROW_UNIT",
    "ROW_VALUE_PREFIX",
    "TXN_ID",
    "KernelContext",
    "KernelError",
    "dispatch",
    "fold_column",
    "group_column",
    "partition_columns",
    "predicate_column",
    "row_value_column",
    "source_column",
    "window_totals",
]
