"""Monotonic optimal binning, WOE and IV, with the guards the plan names.

optbinning supplies the **split points**; this module owns the **bin table**. That
split is deliberate and it is why the guards are testable: a bin with zero bads in
optbinning's own output yields an infinite WOE that the caller cannot see inside,
and the auditable artefact needs every bin's population, good/bad counts, the
merge rule that created it and the smoothing that bounded it. So the table is
built here, from the counts and plan §10's formulas:

    WOE_b = ln((g_b / G) / (b_b / B))
    IV    = sum_b (g_b / G - b_b / B) * WOE_b

Laplace smoothing (``binning.laplace_alpha``) is added to every populated bin's
good and bad counts before shares are taken, so shares sum to one and -- with
alpha 0 -- the expression above is exactly the plan's. What alpha buys is
finiteness: no bin can produce ln(0) or ln(x/0). Bins where alpha is load-bearing
(a class count of zero) are flagged, because the day-6 gate clause is "no bin has
zero bads without a recorded merge or smoothing rule" (03 H:
``test_no_infinite_woe``).

Three special bin kinds are structural, not incidental:

* ``__missing__`` -- a null is evidence, not a zero. DEV-011 makes this the
  dominant case: on PaySim the graph-derived groups are null for almost every
  account, so the missing bin holds most of the population on those features. It
  gets its own WOE from its own counts, is never merged into a value bin and is
  never imputed with a mean (03 H: ``test_missing_is_a_bin_not_a_mean``).
* ``__zero__`` -- a structural zero ("no cash-out events in the window") is a
  different statement from a small number, so an exact 0 is its own bin.
* ``__unseen__`` -- a category that had no group at fit time inherits the WOE of
  the rarest observed value bin, the training tail, and that source bin is named
  in the artefact (03 H: ``test_unseen_category_handled``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Final

import numpy as np
from optbinning import OptimalBinning

from oxbow.scoring.config import BinningConfig
from oxbow.scoring.errors import DegenerateBinningError, WoeNotFiniteError

KIND_RANGE: Final = "range"
KIND_MISSING: Final = "missing"
KIND_ZERO: Final = "zero"
KIND_UNSEEN: Final = "unseen"
KIND_CATEGORY_GROUP: Final = "category_group"
VALUE_KINDS: Final = (KIND_RANGE, KIND_CATEGORY_GROUP)

BOUNDARY_SOURCE_BINNING: Final = "optbinning-mip"
BOUNDARY_SOURCE_QUANTILE_FALLBACK: Final = "quantile-fallback"
BOUNDARY_SOURCE_SINGLE: Final = "single-bin"
BOUNDARY_SOURCE_CATEGORICAL: Final = "optbinning-categorical"


@dataclass(frozen=True, slots=True)
class BinRow:
    """One bin, with everything a human needs in order to audit it."""

    label: str
    kind: str
    population: int
    n_good: int
    n_bad: int
    population_share: float
    bad_rate: float
    woe: float
    iv_contribution: float
    lower: float | None
    upper: float | None
    categories: tuple[str, ...]
    merge_applied: tuple[str, ...]
    smoothing_applied: bool
    smoothing_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "kind": self.kind,
            "population": self.population,
            "n_good": self.n_good,
            "n_bad": self.n_bad,
            "population_share": round(self.population_share, 6),
            "bad_rate": round(self.bad_rate, 8),
            "woe": round(self.woe, 8),
            "iv_contribution": round(self.iv_contribution, 8),
            "lower": self.lower,
            "upper": self.upper,
            "categories": list(self.categories),
            "merge_applied": list(self.merge_applied),
            "smoothing_applied": self.smoothing_applied,
            "smoothing_reason": self.smoothing_reason,
        }


@dataclass(frozen=True, slots=True)
class FeatureBinning:
    """The fitted bin table for one feature."""

    feature: str
    dtype: str
    rows: tuple[BinRow, ...]
    iv: float
    boundary_source: str
    monotonic_direction: str
    missing_share: float
    structural_zero_share: float
    unseen_tail_source: str | None
    merges_recorded: int
    bins_with_zero_bads: int
    bins_with_zero_goods: int
    notes: tuple[str, ...]

    def row_for_label(self, label: str) -> BinRow:
        for row in self.rows:
            if row.label == label:
                return row
        raise DegenerateBinningError(
            f"bin {label!r} is not in {self.feature}'s table: the bin table and the "
            "scoring path disagree, so points would be looked up from the wrong row"
        )

    @property
    def value_rows(self) -> tuple[BinRow, ...]:
        """Bins carrying value evidence, specials excluded."""
        return tuple(row for row in self.rows if row.kind in VALUE_KINDS)

    @property
    def populated_rows(self) -> tuple[BinRow, ...]:
        return tuple(row for row in self.rows if row.population > 0)

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "dtype": self.dtype,
            "iv": round(self.iv, 6),
            "boundary_source": self.boundary_source,
            "monotonic_direction": self.monotonic_direction,
            "missing_share": round(self.missing_share, 6),
            "structural_zero_share": round(self.structural_zero_share, 6),
            "unseen_tail_source": self.unseen_tail_source,
            "merges_recorded": self.merges_recorded,
            "bins_with_zero_bads": self.bins_with_zero_bads,
            "bins_with_zero_goods": self.bins_with_zero_goods,
            "notes": list(self.notes),
            "bins": [row.to_dict() for row in self.rows],
        }


def _smoothed_woe_iv(rows: list[BinRow], alpha: float) -> tuple[list[BinRow], float]:
    """Laplace-smoothed WOE and IV over the populated bins, exactly as plan §10.

    Shares are taken over the smoothed totals, so they sum to one and the
    expression reduces to the plan's formula at alpha = 0.
    """
    n_bins = len(rows)
    denom_good = float(sum(row.n_good for row in rows)) + alpha * n_bins
    denom_bad = float(sum(row.n_bad for row in rows)) + alpha * n_bins
    if denom_good <= 0.0 or denom_bad <= 0.0:
        raise DegenerateBinningError(
            "WOE denominators collapsed: the fit sample has neither goods nor bads once "
            "smoothing is applied, so this feature carries no information value."
        )
    iv = 0.0
    updated: list[BinRow] = []
    for row in rows:
        share_good = (float(row.n_good) + alpha) / denom_good
        share_bad = (float(row.n_bad) + alpha) / denom_bad
        woe = float(np.log(share_good / share_bad))
        if not np.isfinite(woe):
            raise WoeNotFiniteError(
                f"bin {row.label!r} produced a non-finite WOE ({woe}) even after Laplace "
                f"alpha={alpha}. Smoothing is not a formality here: it is what keeps the "
                "points table finite."
            )
        contribution = (share_good - share_bad) * woe
        iv += contribution
        load_bearing = row.n_good == 0 or row.n_bad == 0
        reason = f"laplace_alpha={alpha} added to both class counts; " + (
            "this bin had "
            + ("zero bads" if row.n_bad == 0 else "zero goods")
            + ", so its raw WOE was infinite and alpha is what bounds it"
            if load_bearing
            else "raw WOE was finite; alpha applied uniformly so shares stay comparable"
        )
        updated.append(
            replace(
                row,
                woe=woe,
                iv_contribution=contribution,
                smoothing_applied=load_bearing,
                smoothing_reason=reason,
            )
        )
    return updated, float(iv)


def _solver_time_limit_ms(cfg: BinningConfig) -> int | None:
    """`time_limit_seconds` as the integer ortools wants, or None for no limit.

    optbinning hands `time_limit` straight to `pywraplp.Solver.SetTimeLimit`, whose
    signature is `int64_t` and whose unit is **milliseconds**. The config key is in
    seconds because that is what a human tunes, so the conversion lives here. Passing
    the float from the config raises `TypeError: in method 'Solver_SetTimeLimit',
    argument 2 of type 'int64_t'` inside `_numeric_edges`, and until this existed that
    TypeError was swallowed by its bare `except Exception`, so every numeric feature
    silently took the quantile fallback and `enforce_monotonic_trend` never ran.

    This bounds `Solve()` and nothing else. The MIP solver's model *build* is not under
    it; `_effective_max_n_prebins` is what bounds that.
    """
    if cfg.time_limit_seconds <= 0:
        return None
    return int(round(cfg.time_limit_seconds * 1000))


def _effective_max_n_prebins(cfg: BinningConfig) -> int:
    """The candidate cap the build budget actually allows, at or below the declared one.

    `time_limit_seconds` reads like a bound on the whole feature fit and is not one.
    Measured on this host with `solver: mip`, 1200 rows, build time against the
    candidate count optbinning produced:

        candidates=20 ->  0.60 s      candidates=48 ->  7.35 s
        candidates=32 ->  2.28 s      candidates=64 -> 13.85 s

    which is quadratic -- `add_constraint_monotonic_descending` is a double loop issuing
    one IPC call per candidate pair -- at 1.5-3.4 ms per pair. `SetTimeLimit` bounds only
    `Solve()`, so before this the build was unbounded by any config value: the 300-row
    scorecard unit test spent 13.7 s building against 9.9 s solving, and a wider frame ran
    until pytest's own 300 s timeout with the stack parked in `mip.py`. A stage budget
    that a config key claims to enforce and does not is worse than no key at all, because
    it is read as the protection it is not.

    The cap is solved for rather than measured at runtime, and `measured_ms_per_candidate_pair`
    is the *worst* rate observed, so the projection is conservative. That matters more than
    it looks: a wall-clock abort mid-build would make the same feature produce different
    bin tables on a loaded machine, and the bin table is written into the artefact that
    the audit chain hashes. A deterministic cap keeps the fit reproducible; a feature that
    genuinely needs more candidates than the budget allows falls back to the quantile
    table and says so in `boundary_source`, which is a recorded outcome rather than a hang.
    """
    if cfg.build_budget_seconds <= 0 or cfg.measured_ms_per_candidate_pair <= 0:
        return cfg.max_n_prebins
    affordable = math.sqrt(
        (cfg.build_budget_seconds * 1000.0) / cfg.measured_ms_per_candidate_pair
    )
    # A cap below 2 admits no split at all, which would turn every numeric feature into a
    # single bin. 4 is the floor: enough for optbinning to find a boundary.
    return max(4, min(cfg.max_n_prebins, int(affordable)))


def _splits_to_list(raw: object) -> list[float]:
    """optbinning's split array as a sorted float list, without asking numpy for a truth value.

    `if binner.splits:` -- the form this module used before -- raises `ValueError: the
    truth value of an array with more than one element is ambiguous` for exactly the
    features that found several boundaries, which is the same silent-quantile-fallback
    path as the time-limit bug above.
    """
    if raw is None:
        return []
    values = np.atleast_1d(raw)
    if values.size == 0:
        return []
    return sorted({float(value) for value in values})


def _numeric_edges(
    x: np.ndarray,
    y: np.ndarray,
    ordinary_share: float,
    cfg: BinningConfig,
) -> tuple[list[float], str, str | None]:
    """Split points from optbinning, or a deterministic quantile fallback.

    ``min_bin_size`` is rescaled by the ordinary share because optbinning measures
    it against the subset handed to it, while the declared floor is 5 % of the
    *whole* population. The merge pass enforces the declared rule afterwards either
    way; this only stops the solver proposing boundaries the merge must undo.

    The third element is the fallback's cause, or ``None`` when the solver was used.
    A fallback that explains itself is the difference between a quantile bin table
    -- which is a legitimate, recorded outcome, since ``boundary_source`` is written
    into the artifact -- and a monotonic-trend promise that quietly stopped being
    kept, which is the defect this function carried until it was measured.
    """
    if np.unique(x).size < 2:
        return [], BOUNDARY_SOURCE_SINGLE, None
    min_bin_size = float(np.clip(cfg.min_bin_pct / max(ordinary_share, 1e-6), 1e-4, 0.5))
    candidate_cap = _effective_max_n_prebins(cfg)
    try:
        binner = OptimalBinning(
            dtype="numerical",
            solver=cfg.solver,
            monotonic_trend="auto_asc_desc" if cfg.enforce_monotonic_trend else "auto",
            max_n_bins=cfg.max_bins,
            min_bin_size=min_bin_size,
            max_n_prebins=candidate_cap,
            min_prebin_size=cfg.min_prebin_size,
            prebinning_method=cfg.prebinning_method,
            split_digits=cfg.split_digits,
            time_limit=_solver_time_limit_ms(cfg),
        )
        binner.fit(x, y)
        splits = _splits_to_list(binner.splits)
        if binner.status in {"OPTIMAL", "FEASIBLE"} and splits:
            return splits, BOUNDARY_SOURCE_BINNING, None
        if binner.status in {"OPTIMAL", "FEASIBLE"}:
            # A valid solution with no split is a single-bin feature: the IV bound
            # will reject it, and that rejection is the honest outcome.
            return [], BOUNDARY_SOURCE_SINGLE, None
        raise DegenerateBinningError(f"optbinning reported status {binner.status!r}")
    except DegenerateBinningError:
        raise
    except Exception as exc:
        # The cause is returned, not printed and not re-raised: a solver that cannot
        # solve is a legitimate outcome on a degenerate feature, and the artifact
        # records which bin table it got. What it may not do is look like the solver ran.
        probabilities = np.linspace(0.0, 1.0, cfg.max_bins + 1)[1:-1]
        splits = sorted(
            {float(np.quantile(x, p)) for p in probabilities if np.isfinite(np.quantile(x, p))}
        )
        unique_x = np.unique(x)
        splits = [s for s in splits if unique_x.min() <= s <= unique_x.max()]
        return (
            splits,
            BOUNDARY_SOURCE_QUANTILE_FALLBACK,
            f"optbinning ({cfg.solver} solver) did not run: {type(exc).__name__}: {exc}",
        )


def _categorical_groups(
    values: np.ndarray, y: np.ndarray, cfg: BinningConfig
) -> tuple[list[tuple[str, ...]], str]:
    """Category groups from optbinning, ordered as the solver returned them."""
    if values.size == 0:
        return [], BOUNDARY_SOURCE_SINGLE
    distinct = np.unique(values)
    if distinct.size < 2:
        return [(str(distinct[0]),)], BOUNDARY_SOURCE_CATEGORICAL
    try:
        binner = OptimalBinning(
            dtype="categorical",
            solver=cfg.solver,
            monotonic_trend="auto_asc_desc" if cfg.enforce_monotonic_trend else "auto",
            max_n_bins=cfg.max_bins,
            min_bin_size=cfg.min_bin_pct,
        )
        binner.fit(values, y)
        groups = [
            tuple(sorted(str(item) for item in np.atleast_1d(group))) for group in binner.splits
        ]
        if groups:
            return groups, BOUNDARY_SOURCE_CATEGORICAL
        # No groups returned (single distinct category, or a solver that admitted none):
        # one group holding every observed category is the honest degenerate answer, and
        # the IV bound rejects the feature afterwards rather than the binning lying.
        return (tuple(sorted(str(item) for item in distinct)),), BOUNDARY_SOURCE_CATEGORICAL
    except Exception:
        return [], BOUNDARY_SOURCE_SINGLE


def _range_label(lower: float | None, upper: float | None, digits: int) -> str:
    left = "-inf" if lower is None else f"{lower:.{digits}f}"
    right = "inf" if upper is None else f"{upper:.{digits}f}"
    return f"[{left}, {right})"


def _category_label(categories: tuple[str, ...]) -> str:
    return "[" + ", ".join(categories) + "]"


def _merge_value_bins(
    rows: list[BinRow], cfg: BinningConfig, total_population: int
) -> list[BinRow]:
    """Enforce the population floors by merging into a neighbour, with the rule recorded.

    Special bins are never merged away: the missing bin exists precisely so an
    account's null evidence stays visible, and folding it into a value range would
    hide it behind someone else's WOE. A value bin breaching a floor merges into
    whichever neighbour is smaller, so the merge disturbs the least informative
    bin, and the reason string lands in ``merge_applied``.
    """
    rows = list(rows)
    while True:
        value_indices = [i for i, row in enumerate(rows) if row.kind in VALUE_KINDS]
        if len(value_indices) < 2:
            break
        breaching = [
            i
            for i in value_indices
            if rows[i].population < cfg.min_bin_count
            or rows[i].population / total_population < cfg.min_bin_pct
        ]
        if not breaching:
            break
        index = min(breaching, key=lambda i: (rows[i].population, rows[i].label))
        row = rows[index]
        neighbours = [j for j in (index - 1, index + 1) if j in value_indices]
        if not neighbours:
            # Only one value bin sits between the special bins: it cannot absorb into
            # a special bin without losing its identity, so the breach is recorded as
            # unreachable rather than quietly ignored.
            rows[index] = replace(
                row, merge_applied=(*row.merge_applied, "floor_unreachable:single_value_bin")
            )
            break
        target = min(neighbours, key=lambda j: (rows[j].population, rows[j].label))
        kept, dropped = rows[target], rows[index]
        reason = (
            f"floor_merge:population={dropped.population}<"
            f"max({cfg.min_bin_count},{cfg.min_bin_pct:.0%}of{total_population})"
            f"->merged_into[{kept.label}]"
        )
        lower = kept.lower if target < index else dropped.lower
        upper = kept.upper if target > index else dropped.upper
        merged = BinRow(
            label=_category_label(tuple(sorted(set(kept.categories) | set(dropped.categories))))
            if kept.kind == KIND_CATEGORY_GROUP
            else _range_label(lower, upper, cfg.split_digits),
            kind=kept.kind,
            population=kept.population + dropped.population,
            n_good=kept.n_good + dropped.n_good,
            n_bad=kept.n_bad + dropped.n_bad,
            population_share=0.0,
            bad_rate=0.0,
            woe=0.0,
            iv_contribution=0.0,
            lower=lower,
            upper=upper,
            categories=tuple(sorted(set(kept.categories) | set(dropped.categories))),
            merge_applied=(*kept.merge_applied, reason),
            smoothing_applied=False,
            smoothing_reason="",
        )
        rows[target] = merged
        rows.pop(index)
    return [
        replace(
            row,
            population_share=(row.population / total_population) if total_population else 0.0,
            bad_rate=(row.n_bad / row.population) if row.population else 0.0,
        )
        for row in rows
    ]


def _value_rows_sorted(rows: list[BinRow]) -> list[BinRow]:
    return [row for row in rows if row.kind == KIND_RANGE]


def _monotonic_direction(rows: list[BinRow]) -> str:
    """Describe the fitted bad-rate trend across the value bins."""
    value_rows = [row for row in rows if row.kind in VALUE_KINDS and row.population > 0]
    if len(value_rows) < 2:
        return "single-bin"
    rates = [row.bad_rate for row in value_rows]
    rises = sum(1 for low, high in pairwise(rates) if high > low)
    falls = sum(1 for low, high in pairwise(rates) if high < low)
    if rises and not falls:
        return "ascending"
    if falls and not rises:
        return "descending"
    return "non-monotone-in-observed-rates"


def _training_tail_row(rows: list[BinRow]) -> BinRow:
    """The rarest populated value bin: the evidence an empty special bin inherits."""
    value_rows = [row for row in rows if row.kind in VALUE_KINDS and row.population > 0]
    if not value_rows:
        raise DegenerateBinningError("no populated value bin available to source the tail WOE")
    return min(value_rows, key=lambda row: (row.population, row.label))


def _special_row(label: str, kind: str, pop: int, n_bad: int, total: int) -> BinRow:
    return BinRow(
        label=label,
        kind=kind,
        population=pop,
        n_good=pop - n_bad,
        n_bad=n_bad,
        population_share=pop / total if total else 0.0,
        bad_rate=(n_bad / pop) if pop else 0.0,
        woe=0.0,
        iv_contribution=0.0,
        lower=None,
        upper=None,
        categories=(),
        merge_applied=(),
        smoothing_applied=False,
        smoothing_reason="",
    )


def fit_feature_binning(
    feature: str,
    values: np.ndarray,
    labels: np.ndarray,
    cfg: BinningConfig,
    dtype: str,
) -> FeatureBinning:
    """Fit one feature's bin table.

    ``values`` is float64 with NaN for nulls when ``dtype == 'numerical'``, and an
    object array of ``str | None`` when ``dtype == 'categorical'``. Nulls are the
    only missing representation accepted: the frame contract rejects float NaN, so
    a null reaching here is a real absence of evidence rather than an arithmetic
    accident, which is exactly the distinction the missing bin encodes.
    """
    if dtype == "numerical":
        return _fit_numeric(feature, values.astype(np.float64), labels, cfg)
    if dtype == "categorical":
        return _fit_categorical(feature, values, labels, cfg)
    raise DegenerateBinningError(f"unknown binning dtype {dtype!r} for feature {feature!r}")


def _fit_numeric(
    feature: str, values: np.ndarray, labels: np.ndarray, cfg: BinningConfig
) -> FeatureBinning:
    total = values.size
    if total == 0:
        raise DegenerateBinningError(f"feature {feature!r} has no rows to bin")
    missing_mask = np.isnan(values)
    zero_mask = (~missing_mask) & (values == 0.0)
    ordinary_mask = ~(missing_mask | zero_mask)
    ordinary_count = int(ordinary_mask.sum())
    notes: list[str] = []

    if ordinary_count == 0:
        splits, source = [], BOUNDARY_SOURCE_SINGLE
        notes.append(
            f"no non-null non-zero rows for {feature}: the whole population is missing or "
            "structural zero, so the value bins are empty by construction"
        )
    else:
        splits, source, fallback_cause = _numeric_edges(
            values[ordinary_mask], labels[ordinary_mask], ordinary_count / total, cfg
        )
        if fallback_cause is not None:
            promise = (
                "the declared monotonic trend could NOT be enforced on this feature"
                if cfg.enforce_monotonic_trend
                else "no monotonic trend was declared"
            )
            notes.append(
                f"{fallback_cause}; boundaries came from the deterministic quantile "
                f"fallback, so {promise}"
            )

    bounds: list[tuple[float | None, float | None]] = (
        list(zip([None, *splits, None][:-1], [None, *splits, None][1:], strict=True))
        if splits
        else [(None, None)]
    )
    if not splits and source == BOUNDARY_SOURCE_QUANTILE_FALLBACK:
        source = BOUNDARY_SOURCE_SINGLE
        notes.append("quantile fallback produced no distinct split; feature is one value bin")

    rows: list[BinRow] = []
    for lower, upper in bounds:
        if lower is None and upper is None:
            mask = ordinary_mask
        elif lower is None:
            mask = ordinary_mask & (values < upper)
        elif upper is None:
            mask = ordinary_mask & (values >= lower)
        else:
            mask = ordinary_mask & (values >= lower) & (values < upper)
        pop = int(mask.sum())
        n_bad = int(labels[mask].sum()) if pop else 0
        rows.append(
            BinRow(
                label=_range_label(lower, upper, cfg.split_digits),
                kind=KIND_RANGE,
                population=pop,
                n_good=pop - n_bad,
                n_bad=n_bad,
                population_share=pop / total,
                bad_rate=(n_bad / pop) if pop else 0.0,
                woe=0.0,
                iv_contribution=0.0,
                lower=lower,
                upper=upper,
                categories=(),
                merge_applied=(),
                smoothing_applied=False,
                smoothing_reason="",
            )
        )
    rows = [row for row in rows if row.population > 0]
    if not rows:
        raise DegenerateBinningError(f"feature {feature!r} produced no populated value bin")

    missing_pop = int(missing_mask.sum())
    zero_pop = int(zero_mask.sum())
    missing_row = _special_row(
        cfg.missing_bin,
        KIND_MISSING,
        missing_pop,
        int(labels[missing_mask].sum()) if missing_pop else 0,
        total,
    )
    zero_row = _special_row(
        cfg.structural_zero_bin,
        KIND_ZERO,
        zero_pop,
        int(labels[zero_mask].sum()) if zero_pop else 0,
        total,
    )
    if missing_pop:
        notes.append(
            f"missing is a bin, not a mean: {missing_pop / total:.1%} of rows are null and "
            "carry their own WOE (DEV-011: on a star-shaped corpus this is the normal case "
            "for graph-derived features)"
        )
    if zero_pop:
        notes.append(
            f"structural zero kept separate from the smallest range: {zero_pop / total:.1%} "
            "of rows are exactly 0"
        )

    rows = _merge_value_bins(rows, cfg, total)
    populated_specials = [row for row in (missing_row, zero_row) if row.population > 0]
    final_rows, iv = _smoothed_woe_iv([*rows, *populated_specials], cfg.laplace_alpha)
    empty_specials = [row for row in (missing_row, zero_row) if row.population == 0]
    if empty_specials:
        tail = _training_tail_row(final_rows)
        final_rows = [
            *final_rows,
            *[
                replace(
                    row,
                    woe=tail.woe,
                    merge_applied=(f"tail_source:{tail.label}",),
                    smoothing_applied=True,
                    smoothing_reason=(
                        f"{row.label} had no rows at fit time and inherits the training tail "
                        f"{tail.label}'s WOE; it scores from that row if a later frame has "
                        "values there"
                    ),
                )
                for row in empty_specials
            ],
        ]

    direction = _monotonic_direction(final_rows)
    final_rows = sorted(final_rows, key=lambda row: _row_sort_key(row))
    return FeatureBinning(
        feature=feature,
        dtype="numerical",
        rows=tuple(final_rows),
        iv=iv,
        boundary_source=source,
        monotonic_direction=direction,
        missing_share=missing_pop / total,
        structural_zero_share=zero_pop / total,
        unseen_tail_source=None,
        merges_recorded=sum(len(row.merge_applied) for row in final_rows),
        bins_with_zero_bads=sum(1 for row in final_rows if row.n_bad == 0),
        bins_with_zero_goods=sum(1 for row in final_rows if row.n_good == 0),
        notes=tuple(notes),
    )


def _row_sort_key(row: BinRow) -> tuple[int, float, str]:
    order = {KIND_RANGE: 0, KIND_CATEGORY_GROUP: 0, KIND_ZERO: 1, KIND_MISSING: 2, KIND_UNSEEN: 3}
    lower = row.lower if row.lower is not None else float("-inf")
    return (order[row.kind], lower, row.label)


def _fit_categorical(
    feature: str, values: np.ndarray, labels: np.ndarray, cfg: BinningConfig
) -> FeatureBinning:
    total = values.size
    if total == 0:
        raise DegenerateBinningError(f"feature {feature!r} has no rows to bin")
    missing_mask = np.array([value is None for value in values], dtype=bool)
    present = ~missing_mask
    text = np.array(["|" if value is None else str(value) for value in values], dtype=object)
    notes: list[str] = []

    groups, source = _categorical_groups(text[present], labels[present], cfg)
    rows: list[BinRow] = []
    assigned = np.zeros(total, dtype=bool)
    for group in groups:
        mask = present & np.isin(text, list(group))
        assigned |= mask
        pop = int(mask.sum())
        n_bad = int(labels[mask].sum()) if pop else 0
        rows.append(
            BinRow(
                label=_category_label(group),
                kind=KIND_CATEGORY_GROUP,
                population=pop,
                n_good=pop - n_bad,
                n_bad=n_bad,
                population_share=pop / total,
                bad_rate=(n_bad / pop) if pop else 0.0,
                woe=0.0,
                iv_contribution=0.0,
                lower=None,
                upper=None,
                categories=group,
                merge_applied=(),
                smoothing_applied=False,
                smoothing_reason="",
            )
        )
    rows = [row for row in rows if row.population > 0]
    if not rows:
        raise DegenerateBinningError(f"feature {feature!r} produced no populated category group")

    leftover = present & ~assigned
    leftover_pop = int(leftover.sum())
    if leftover_pop:
        notes.append(
            f"{leftover_pop} rows carried a category the solver admitted no group for; they "
            f"land in {cfg.unseen_bin} instead of being mapped to the most common category"
        )
    unseen_row = _special_row(
        cfg.unseen_bin,
        KIND_UNSEEN,
        leftover_pop,
        int(labels[leftover].sum()) if leftover_pop else 0,
        total,
    )
    missing_pop = int(missing_mask.sum())
    missing_row = _special_row(
        cfg.missing_bin,
        KIND_MISSING,
        missing_pop,
        int(labels[missing_mask].sum()) if missing_pop else 0,
        total,
    )

    rows = _merge_value_bins(rows, cfg, total)
    populated_specials = [row for row in (unseen_row, missing_row) if row.population > 0]
    final_rows, iv = _smoothed_woe_iv([*rows, *populated_specials], cfg.laplace_alpha)
    empty_specials = [row for row in (unseen_row, missing_row) if row.population == 0]
    tail = _training_tail_row(final_rows)
    if empty_specials:
        final_rows = [
            *final_rows,
            *[
                replace(
                    row,
                    woe=tail.woe,
                    merge_applied=(f"unseen_tail_source:{tail.label}",),
                    smoothing_applied=True,
                    smoothing_reason=(
                        f"{row.label} had no rows at fit time and inherits the training tail "
                        f"{tail.label}'s WOE (population={tail.population}, bad_rate="
                        f"{tail.bad_rate:.6f})"
                    ),
                )
                for row in empty_specials
            ],
        ]
    final_rows = sorted(final_rows, key=lambda row: _row_sort_key(row))
    return FeatureBinning(
        feature=feature,
        dtype="categorical",
        rows=tuple(final_rows),
        iv=iv,
        boundary_source=source,
        monotonic_direction=_monotonic_direction(final_rows),
        missing_share=missing_pop / total,
        structural_zero_share=0.0,
        unseen_tail_source=tail.label,
        merges_recorded=sum(len(row.merge_applied) for row in final_rows),
        bins_with_zero_bads=sum(1 for row in final_rows if row.n_bad == 0),
        bins_with_zero_goods=sum(1 for row in final_rows if row.n_good == 0),
        notes=tuple(notes),
    )


def assign_numeric_bin(
    values: np.ndarray, binning: FeatureBinning, cfg: BinningConfig
) -> list[str]:
    """Map scored values to bin labels.

    Edges are half-open ``[lower, upper)`` with the outer bins open at infinity, so
    every finite value lands somewhere and no out-of-range rule is needed. A null
    goes to the missing bin even if that bin was empty at fit time, because the row
    exists in the table with an inherited tail WOE.
    """
    value_rows = _value_rows_sorted(binning.rows)
    lookup: dict[str, BinRow] = {row.label: row for row in binning.rows}
    labels: list[str] = []
    for value in values:
        number = float(value)
        if np.isnan(number):
            if cfg.missing_bin not in lookup:
                raise DegenerateBinningError(
                    f"feature {binning.feature!r}: a null arrived but the table has no "
                    "missing bin; missing must always be a bin"
                )
            labels.append(cfg.missing_bin)
            continue
        if number == 0.0 and cfg.structural_zero_bin in lookup:
            labels.append(cfg.structural_zero_bin)
            continue
        for row in value_rows:
            if (row.lower is None or number >= row.lower) and (
                row.upper is None or number < row.upper
            ):
                labels.append(row.label)
                break
        else:
            raise DegenerateBinningError(
                f"feature {binning.feature!r}: value {number} fell outside every bin. The "
                "bin table is not exhaustive, so its points would be wrong."
            )
    return labels


def assign_categorical_bin(
    values: np.ndarray, binning: FeatureBinning, cfg: BinningConfig
) -> list[str]:
    """Map scored categories to bin labels; categories unseen at fit go to ``__unseen__``."""
    lookup: dict[str, str] = {}
    by_label: dict[str, BinRow] = {row.label: row for row in binning.rows}
    for row in binning.rows:
        for category in row.categories:
            lookup[category] = row.label
    labels: list[str] = []
    for value in values:
        if value is None:
            if cfg.missing_bin not in by_label:
                raise DegenerateBinningError(
                    f"feature {binning.feature!r}: null arrived with no missing bin in the table"
                )
            labels.append(cfg.missing_bin)
            continue
        text = str(value)
        if text in lookup:
            labels.append(lookup[text])
        elif cfg.unseen_bin in by_label:
            labels.append(cfg.unseen_bin)
        else:
            raise DegenerateBinningError(
                f"feature {binning.feature!r}: category {text!r} was unseen at fit time and "
                "the table has no unseen bin. Mapping it to the most common category would "
                "invent evidence (03 H: test_unseen_category_handled)."
            )
    return labels


def assign_bins(values: np.ndarray, binning: FeatureBinning, cfg: BinningConfig) -> list[str]:
    """Bin a scored column into labels, dispatching on the fitted dtype."""
    if binning.dtype == "numerical":
        return assign_numeric_bin(values, binning, cfg)
    return assign_categorical_bin(values, binning, cfg)


def woe_of_labels(binning: FeatureBinning, labels: list[str]) -> np.ndarray:
    """WOE per row, looked up from the fitted table and never recomputed at score time.

    Recomputing WOE at score time is how a model drifts into using the scored
    population's class balance: the table is fit-time evidence, frozen.
    """
    table = {row.label: row.woe for row in binning.rows}
    missing = [label for label in labels if label not in table]
    if missing:
        raise DegenerateBinningError(
            f"feature {binning.feature!r}: bins {sorted(set(missing))[:3]} have no WOE row"
        )
    return np.array([table[label] for label in labels], dtype=np.float64)


__all__ = [
    "BOUNDARY_SOURCE_BINNING",
    "BOUNDARY_SOURCE_CATEGORICAL",
    "BOUNDARY_SOURCE_QUANTILE_FALLBACK",
    "BOUNDARY_SOURCE_SINGLE",
    "KIND_CATEGORY_GROUP",
    "KIND_MISSING",
    "KIND_RANGE",
    "KIND_UNSEEN",
    "KIND_ZERO",
    "VALUE_KINDS",
    "BinRow",
    "FeatureBinning",
    "assign_bins",
    "assign_categorical_bin",
    "assign_numeric_bin",
    "fit_feature_binning",
    "woe_of_labels",
]
