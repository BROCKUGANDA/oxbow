"""Metrics, bootstrap confidence intervals, seed stability, and the agreement matrix.

This is the measurement layer the gates are written against, so the rules here are about
not flattering the model:

* **PR-AUC is the headline and AUROC is reported but de-emphasised** (plan §12). Under a
  0.1 % base rate AUROC is dominated by the ease of separating ordinary accounts and
  stays high while the fraud ranking degrades; it is included for comparability and
  labelled as to why it is not the number to act on.
* **Every reported metric carries a bootstrap CI** with the resample count and seed taken
  from ``config/splits.yaml``, not restated here. A point estimate on ~200 positives is a
  story; the interval is the honest version of the same story.
* **Seed stability is mean +/- sd across the declared seed list**, not a single lucky
  run. It is the difference between "the model scored 0.41" and "the model scores 0.41
  +/- 0.03, so a 0.02 improvement over a baseline is inside the noise" -- which is the
  question a risk manager actually asks.
* **The scorecard-vs-GBM agreement matrix is produced in every run.** The disagreement
  between the two models is where model risk lives (DEV-001), so it is a first-class
  artefact rather than something someone thinks to check once.

Ties are broken on ``account_key`` before any ranking statistic, so a metric cannot
depend on row order.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import polars as pl
from sklearn.metrics import average_precision_score, roc_auc_score

from oxbow.models.errors import ModelLayerError
from oxbow.scoring.frame import COL_ACCOUNT_KEY

KS_MIN_BINS: Final = 2
PERCENTILE_EDGES: Final = (1, 5, 10, 25, 50, 75, 90, 95, 99)


def average_precision(labels: np.ndarray, scores: np.ndarray) -> float:
    """PR-AUC (sklearn's ``average_precision``), ties broken on the score itself.

    Returns NaN rather than 0.0 when the slice has no positives: PR-AUC is genuinely
    undefined there, and 0.0 would read as a terrible model instead of an unmeasurable
    one (03 I: ``test_no_alerts_fold_undefined_not_zero`` is the same rule at the alert
    level).
    """
    y = np.asarray(labels, dtype=np.int32)
    if y.size == 0:
        return float("nan")
    if int(y.sum()) == 0:
        return float("nan")
    if int(y.sum()) == int(y.size):
        return float("nan")
    return float(average_precision_score(y, np.asarray(scores, dtype=np.float64)))


def roc_auc(labels: np.ndarray, scores: np.ndarray) -> float:
    """AUROC, reported for comparability and explicitly not the headline."""
    y = np.asarray(labels, dtype=np.int32)
    if y.size == 0 or int(y.sum()) == 0 or int(y.sum()) == int(y.size):
        return float("nan")
    return float(roc_auc_score(y, np.asarray(scores, dtype=np.float64)))


def brier(labels: np.ndarray, probabilities: np.ndarray) -> float:
    values = np.asarray(probabilities, dtype=np.float64)
    if values.size == 0:
        return float("nan")
    if float(np.min(values)) < 0.0 or float(np.max(values)) > 1.0:
        raise ModelLayerError(
            "Brier is defined on probabilities in [0, 1]; a value outside that range means "
            "an uncalibrated score was passed where a probability was expected"
        )
    return float(np.mean(np.square(values - np.asarray(labels, dtype=np.float64))))


def ks_statistic(labels: np.ndarray, scores: np.ndarray) -> float:
    """Kolmogorov-Smirnov separation between the good and bad score distributions."""
    y = np.asarray(labels, dtype=np.int32)
    values = np.asarray(scores, dtype=np.float64)
    positives = values[y == 1]
    negatives = values[y == 0]
    if positives.size < 1 or negatives.size < 1:
        return float("nan")
    grid = np.unique(np.concatenate([positives, negatives]))
    if grid.size < KS_MIN_BINS:
        return 0.0
    cdf_positives = np.searchsorted(np.sort(positives), grid, side="right") / positives.size
    cdf_negatives = np.searchsorted(np.sort(negatives), grid, side="right") / negatives.size
    return float(np.max(np.abs(cdf_positives - cdf_negatives)))


def precision_at(labels: np.ndarray, scores: np.ndarray, budget: int) -> float:
    """Precision at a review budget: the operating quantity, not a threshold-free ideal.

    Order is ``(-score, account_key)``, so the budget cut is reproducible: which accounts
    sit at the boundary must not depend on row order (``test_tie_break_deterministic``).
    """
    if budget <= 0:
        raise ModelLayerError(
            "precision at a zero budget is undefined rather than zero; the caller must ask "
            "for a positive number of reviews"
        )
    y = np.asarray(labels, dtype=np.int32)
    if y.size == 0:
        return float("nan")
    order = rank_order(y.size, scores)
    top = order[: min(budget, y.size)]
    return float(np.sum(y[top] == 1)) / float(top.size)


def rank_order(rows: int, scores: np.ndarray) -> np.ndarray:
    """The codebase-wide ranking order: score descending, then account order.

    A single function owns it so PR-AUC, precision-at-budget and the queue cannot each
    invent a tie-break and disagree about which accounts are in the top 200.
    """
    return np.lexsort((np.arange(rows), -np.asarray(scores, dtype=np.float64)))


def recall_at(labels: np.ndarray, scores: np.ndarray, budget: int) -> float:
    y = np.asarray(labels, dtype=np.int32)
    positives = int(np.sum(y == 1))
    if positives == 0:
        return float("nan")
    order = rank_order(y.size, scores)
    top = order[: min(budget, y.size)]
    return float(np.sum(y[top] == 1)) / float(positives)


def alerts_per_10k(scores: np.ndarray, threshold: float) -> float:
    """Alert fatigue proxy, reported before anyone has to ask for it (plan §12)."""
    if scores.size == 0:
        return 0.0
    return float(np.sum(np.asarray(scores, dtype=np.float64) >= threshold) / scores.size * 10_000.0)


@dataclass(frozen=True, slots=True)
class BootstrapCI:
    """A percentile bootstrap interval, with its resample count and seed attached."""

    estimate: float
    lower: float
    upper: float
    resamples: int
    seed: int
    confidence: float
    method: str

    def to_dict(self) -> dict[str, object]:
        return {
            "estimate": None if np.isnan(self.estimate) else round(self.estimate, 8),
            "ci_lower": None if np.isnan(self.lower) else round(self.lower, 8),
            "ci_upper": None if np.isnan(self.upper) else round(self.upper, 8),
            "resamples": self.resamples,
            "seed": self.seed,
            "confidence": self.confidence,
            "method": self.method,
        }


def bootstrap_ci(
    metric: Callable[[np.ndarray, np.ndarray], float],
    labels: np.ndarray,
    scores: np.ndarray,
    resamples: int,
    confidence: float,
    seed: int,
) -> BootstrapCI:
    """Percentile bootstrap over rows.

    Reseeded per call from the declared seed, so the interval is reproducible; row
    resampling ignores the temporal structure, which is why the *fold* metrics are also
    reported separately -- a bootstrap CI on a pooled slice understates the variance a
    walk-forward actually has, and saying so is cheaper than being caught not saying it.
    """
    y = np.asarray(labels, dtype=np.int32)
    values = np.asarray(scores, dtype=np.float64)
    estimate = metric(y, values)
    if resamples < 1:
        raise ModelLayerError(f"bootstrap resamples must be >= 1, got {resamples}")
    if y.size == 0 or np.isnan(estimate):
        return BootstrapCI(
            estimate=estimate,
            lower=float("nan"),
            upper=float("nan"),
            resamples=resamples,
            seed=seed,
            confidence=confidence,
            method="percentile_bootstrap_rows",
        )
    rng = np.random.default_rng(seed)
    draws = np.empty(resamples, dtype=np.float64)
    for index in range(resamples):
        sample = rng.integers(0, y.size, y.size)
        draws[index] = metric(y[sample], values[sample])
    finite = draws[np.isfinite(draws)]
    if finite.size == 0:
        return BootstrapCI(
            estimate=estimate,
            lower=float("nan"),
            upper=float("nan"),
            resamples=resamples,
            seed=seed,
            confidence=confidence,
            method="percentile_bootstrap_rows",
        )
    alpha = (1.0 - confidence) / 2.0
    return BootstrapCI(
        estimate=float(estimate),
        lower=float(np.quantile(finite, alpha)),
        upper=float(np.quantile(finite, 1.0 - alpha)),
        resamples=int(finite.size),
        seed=seed,
        confidence=confidence,
        method="percentile_bootstrap_rows",
    )


@dataclass(frozen=True, slots=True)
class MetricSet:
    """Everything reported for one ranking on one slice."""

    name: str
    rows: int
    positives: int
    base_rate: float
    pr_auc: BootstrapCI
    roc_auc: float
    brier: float | None
    ks: float
    provenance: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "rows": self.rows,
            "positives": self.positives,
            "base_rate": round(self.base_rate, 8),
            "pr_auc": self.pr_auc.to_dict(),
            "roc_auc": None if np.isnan(self.roc_auc) else round(self.roc_auc, 8),
            "roc_auc_note": "reported for comparability; PR-AUC is the headline under imbalance",
            "brier": None if self.brier is None or np.isnan(self.brier) else round(self.brier, 8),
            "ks": None if np.isnan(self.ks) else round(self.ks, 8),
            "provenance": self.provenance,
        }


def measure(
    name: str,
    labels: np.ndarray,
    scores: np.ndarray,
    resamples: int,
    confidence: float,
    seed: int,
    provenance: str,
    probabilities: np.ndarray | None = None,
) -> MetricSet:
    """One slice, one ranking, all the statistics the gate names, with provenance."""
    y = np.asarray(labels, dtype=np.int32)
    values = np.asarray(scores, dtype=np.float64)
    if y.size != values.size:
        raise ModelLayerError(f"{name}: labels and scores disagree on length")
    pr = bootstrap_ci(
        average_precision, y, values, resamples=resamples, confidence=confidence, seed=seed
    )
    brier_value = (
        None if probabilities is None else brier(y, np.asarray(probabilities, dtype=np.float64))
    )
    return MetricSet(
        name=name,
        rows=int(y.size),
        positives=int(np.sum(y == 1)),
        base_rate=float(np.mean(y)) if y.size else 0.0,
        pr_auc=pr,
        roc_auc=roc_auc(y, values),
        brier=brier_value,
        ks=ks_statistic(y, values),
        provenance=provenance,
    )


@dataclass(frozen=True, slots=True)
class SeedStability:
    """Mean and sd across the declared seeds -- not a lucky run."""

    metric: str
    seeds: tuple[int, ...]
    values: tuple[float, ...]
    mean: float
    sd: float
    minimum: float
    maximum: float

    def summary(self) -> str:
        return f"{self.metric} {self.mean:.4f} +/- {self.sd:.4f} over {len(self.seeds)} seeds"

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "seeds": list(self.seeds),
            "values": [None if np.isnan(value) else round(value, 8) for value in self.values],
            "mean": None if np.isnan(self.mean) else round(self.mean, 8),
            "sd": None if np.isnan(self.sd) else round(self.sd, 8),
            "min": None if np.isnan(self.minimum) else round(self.minimum, 8),
            "max": None if np.isnan(self.maximum) else round(self.maximum, 8),
            "summary": self.summary(),
            "note": (
                "reported as mean +/- sd across seeds; a single run's number would "
                "understate the spread and read as a precision the model does not have"
            ),
        }


def seed_stability(
    metric: str,
    seeds: Sequence[int],
    run_for_seed: Callable[[int], float],
) -> SeedStability:
    """Run ``run_for_seed`` once per declared seed and summarise.

    The sd is a population sd over the seed list (``ddof=0``), because the list *is* the
    set being described -- the plan fixes the five seeds, so this is not an inference
    about a larger population of seeds and should not be labelled like one.
    """
    if len(seeds) < 2:
        raise ModelLayerError("seed stability needs at least two seeds")
    values = tuple(float(run_for_seed(int(seed))) for seed in seeds)
    array = np.asarray(values, dtype=np.float64)
    finite = array[np.isfinite(array)]
    if finite.size == 0:
        return SeedStability(
            metric=metric,
            seeds=tuple(int(seed) for seed in seeds),
            values=values,
            mean=float("nan"),
            sd=float("nan"),
            minimum=float("nan"),
            maximum=float("nan"),
        )
    return SeedStability(
        metric=metric,
        seeds=tuple(int(seed) for seed in seeds),
        values=values,
        mean=float(np.mean(finite)),
        sd=float(np.std(finite, ddof=0)),
        minimum=float(np.min(finite)),
        maximum=float(np.max(finite)),
    )


@dataclass(frozen=True, slots=True)
class AgreementMatrix:
    """Band-by-band cross-tabulation of the scorecard against the GBM."""

    band_ids: tuple[str, ...]
    counts: tuple[tuple[int, ...], ...]
    rows: int
    exact_agreement: float
    within_one_band: float
    disagreements: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "band_ids": list(self.band_ids),
            "rows_scorecard_then_gbm": [list(row) for row in self.counts],
            "rows": self.rows,
            "exact_agreement_share": round(self.exact_agreement, 6),
            "within_one_band_share": round(self.within_one_band, 6),
            "top_disagreements": list(self.disagreements),
            "note": (
                "the disagreement between the auditable scorecard and the ranking engine is "
                "where model risk lives (DEV-001), so it is produced every run and rendered "
                "as its own tab"
            ),
        }


def agreement_matrix(
    scored: pl.DataFrame,
    band_ids: Sequence[str],
    top_n: int,
    scorecard_band_column: str = "band",
    gbm_band_column: str = "gbm_band",
) -> AgreementMatrix:
    """Cross-tab the two band assignments and rank the biggest divergences.

    Both models must be banded on the same cut rule before this is called; comparing a
    scorecard band against a GBM quantile band would produce a matrix that measures the
    cut rule rather than the models.
    """
    for column in (scorecard_band_column, gbm_band_column, "p_scorecard", "p_gbm"):
        if column not in scored.columns:
            raise ModelLayerError(f"agreement matrix needs column {column!r}")
    order = {band: index for index, band in enumerate(band_ids)}
    matrix = np.zeros((len(band_ids), len(band_ids)), dtype=np.int64)
    for left, right in scored.select([scorecard_band_column, gbm_band_column]).iter_rows():
        if left not in order or right not in order:
            raise ModelLayerError(
                f"band value {left!r}/{right!r} is outside the declared band set {list(band_ids)}"
            )
        matrix[order[str(left)], order[str(right)]] += 1
    rows = int(matrix.sum())
    exact = float(np.trace(matrix) / rows) if rows else 0.0
    within = 0
    for left_index in range(len(band_ids)):
        for right_index in range(len(band_ids)):
            if abs(left_index - right_index) <= 1:
                within += int(matrix[left_index, right_index])
    distances = scored.with_columns(
        (pl.col("p_gbm") - pl.col("p_scorecard")).abs().alias("probability_gap"),
        (
            pl.col(gbm_band_column).replace_strict(order, return_dtype=pl.Int64)
            - pl.col(scorecard_band_column).replace_strict(order, return_dtype=pl.Int64)
        )
        .abs()
        .alias("band_distance"),
    )
    ordered = distances.sort(
        ["band_distance", "probability_gap", COL_ACCOUNT_KEY], descending=[True, True, False]
    )
    top = ordered.select(
        [
            COL_ACCOUNT_KEY,
            scorecard_band_column,
            gbm_band_column,
            "p_scorecard",
            "p_gbm",
            "band_distance",
            "probability_gap",
        ]
    ).head(top_n)
    disagreements = tuple(
        {
            "account_key": row[COL_ACCOUNT_KEY],
            "scorecard_band": row[scorecard_band_column],
            "gbm_band": row[gbm_band_column],
            "p_scorecard": round(float(row["p_scorecard"]), 8),
            "p_gbm": round(float(row["p_gbm"]), 8),
            "band_distance": int(row["band_distance"]),
            "probability_gap": round(float(row["probability_gap"]), 8),
        }
        for row in top.to_dicts()
    )
    return AgreementMatrix(
        band_ids=tuple(band_ids),
        counts=tuple(tuple(int(value) for value in row) for row in matrix),
        rows=rows,
        exact_agreement=exact,
        within_one_band=float(within / rows) if rows else 0.0,
        disagreements=disagreements,
    )


def band_by_probability(
    probabilities: np.ndarray, boundaries: dict[str, float], band_ids: Sequence[str]
) -> np.ndarray:
    """Band one model's probabilities using boundaries taken from the other model's bands.

    The agreement matrix only measures the models if both are cut the same way: comparing
    a scorecard band against a GBM quantile band would report the difference between two
    cut rules. So the scorecard's per-band upper probability is the boundary, and the GBM
    is assigned against it. ``band_ids`` runs safest to riskiest, so a probability at or
    above a band's boundary belongs to that safer band.
    """
    ordered = sorted(
        ((band, boundary) for band, boundary in boundaries.items() if band in band_ids),
        key=lambda item: -item[1],
    )
    out = np.empty(probabilities.size, dtype=object)
    fallback = band_ids[-1]
    for position, value in enumerate(np.asarray(probabilities, dtype=np.float64)):
        assigned = fallback
        for band, boundary in ordered:
            if float(value) <= boundary:
                assigned = band
                break
        out[position] = assigned
    return out


def scorecard_band_probability_boundaries(
    scored: pl.DataFrame, band_ids: Sequence[str], probability_column: str = "p_scorecard"
) -> dict[str, float]:
    """The upper probability of each scorecard band, as measured on the scored rows."""
    if probability_column not in scored.columns:
        raise ModelLayerError(f"no {probability_column!r} column to derive band boundaries from")
    out: dict[str, float] = {}
    for band in band_ids:
        rows = scored.filter(pl.col("band") == band).get_column(probability_column)
        out[band] = float(rows.max()) if rows.len() else float("inf")
    return out


def percentile_table(scores: np.ndarray) -> dict[str, float]:
    """Score distribution percentiles, for the drift and band narrative."""
    values = np.asarray(scores, dtype=np.float64)
    if values.size == 0:
        return {}
    return {
        f"p{percentile}": float(np.percentile(values, percentile))
        for percentile in PERCENTILE_EDGES
    }


__all__ = [
    "PERCENTILE_EDGES",
    "AgreementMatrix",
    "BootstrapCI",
    "MetricSet",
    "SeedStability",
    "agreement_matrix",
    "alerts_per_10k",
    "average_precision",
    "band_by_probability",
    "bootstrap_ci",
    "brier",
    "ks_statistic",
    "measure",
    "percentile_table",
    "precision_at",
    "rank_order",
    "recall_at",
    "roc_auc",
    "scorecard_band_probability_boundaries",
    "seed_stability",
]
