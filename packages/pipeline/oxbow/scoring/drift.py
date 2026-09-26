"""PSI, CSI and rating migration: drift as an operational decision, not a chart.

PSI is the plan's formula on the bin shares --

    PSI = sum_b (a_b - e_b) * ln(a_b / e_b)

-- with the *fit* distribution as the expected reference (``drift.expected_reference``)
and each later period as the actual. CSI is the same quantity decomposed per bin, so
a drifted score can be traced to the bin that moved rather than left as a single
number on a dashboard; that localisation is the difference between "the model aged"
and "this feature's top band lost its population".

The action is the load-bearing part: at ``drift.psi_action`` (0.25) the run degrades
to rules-plus-scorecard and says so (03 H:
``test_psi_action_degrades_scoring``). The reasoning is asymmetric on purpose. The
GBM and the fused meta-learner were both fitted on the population that has now
moved, and calibration sits on top of them, so drift invalidates the learned
probabilities down to the last decimal. The scorecard is a table of whole numbers a
human signed off: under drift it is *less* accurate, but it is still the number an
analyst can defend, and a queue still has to be ordered. So the scorecard stays and
the models step out -- with a banner, not a silent substitution.

Zero-share bins are floored, and the floor is recorded in the result: PSI on a bin
that emptied is genuinely large, and silently clipping it to zero would understate
exactly the drift someone needs to see.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

import numpy as np
import polars as pl

from oxbow.scoring.binning import FeatureBinning
from oxbow.scoring.config import DriftConfig
from oxbow.scoring.frame import COL_ACCOUNT_KEY, COL_FOLD, COL_LABEL

PSI_ZERO_FLOOR: Final = 1e-4
ACTION_DEGRADED: Final = "rules_plus_scorecard"
ACTION_NORMAL: Final = "full_model_stack"
STATUS_STABLE: Final = "stable"
STATUS_WATCH: Final = "watch"
STATUS_ACTION: Final = "action"


@dataclass(frozen=True, slots=True)
class BinShift:
    """One bin's share movement: the CSI atom that localises the drift."""

    bin_label: str
    expected_share: float
    actual_share: float
    contribution: float

    def to_dict(self) -> dict[str, object]:
        return {
            "bin_label": self.bin_label,
            "expected_share": round(self.expected_share, 8),
            "actual_share": round(self.actual_share, 8),
            "contribution": round(self.contribution, 8),
        }


@dataclass(frozen=True, slots=True)
class FeatureDrift:
    """PSI for one feature in one period, plus its per-bin CSI decomposition."""

    feature: str
    period: int
    psi: float
    csi: tuple[BinShift, ...]
    status: str
    population: int

    @property
    def max_csi_bin(self) -> BinShift | None:
        if not self.csi:
            return None
        return max(self.csi, key=lambda shift: abs(shift.contribution))

    def to_dict(self) -> dict[str, object]:
        top = self.max_csi_bin
        return {
            "feature": self.feature,
            "period": self.period,
            "psi": round(self.psi, 6),
            "status": self.status,
            "population": self.population,
            "csi_total": round(sum(shift.contribution for shift in self.csi), 6),
            "worst_bin": None if top is None else top.to_dict(),
            "bins": [shift.to_dict() for shift in self.csi],
        }


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Everything drift produces for a run, including the decision it forces."""

    features: tuple[FeatureDrift, ...]
    score_psi: Mapping[int, float]
    score_psi_worst: float
    action: str
    mode: str
    banner: str
    thresholds: Mapping[str, float]
    psi_zero_floor: float
    expected_reference: str

    def breached(self) -> tuple[FeatureDrift, ...]:
        return tuple(drift for drift in self.features if drift.status == STATUS_ACTION)

    def to_dict(self) -> dict[str, object]:
        return {
            "features": [drift.to_dict() for drift in self.features],
            "score_psi_by_period": {
                str(period): round(value, 6) for period, value in sorted(self.score_psi.items())
            },
            "score_psi_worst": round(self.score_psi_worst, 6),
            "action": self.action,
            "mode": self.mode,
            "banner": self.banner,
            "thresholds": dict(self.thresholds),
            "psi_zero_floor": self.psi_zero_floor,
            "expected_reference": self.expected_reference,
            "features_at_action": sorted(drift.feature for drift in self.breached()),
        }


def _shares(labels: Sequence[str], universe: Sequence[str]) -> dict[str, float]:
    total = len(labels)
    if total == 0:
        return {label: 0.0 for label in universe}
    counts: dict[str, int] = dict.fromkeys(universe, 0)
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return {label: counts[label] / total for label in universe}


def psi_from_shares(expected: Mapping[str, float], actual: Mapping[str, float]) -> tuple[float, list[BinShift]]:
    """PSI and its per-bin contributions, with empty bins floored and recorded."""
    universe = sorted(set(expected) | set(actual))
    total = 0.0
    shifts: list[BinShift] = []
    for label in universe:
        e = float(expected.get(label, 0.0))
        a = float(actual.get(label, 0.0))
        if e <= 0.0 and a <= 0.0:
            continue
        floored_e = e if e > 0.0 else PSI_ZERO_FLOOR
        floored_a = a if a > 0.0 else PSI_ZERO_FLOOR
        contribution = (floored_a - floored_e) * math.log(floored_a / floored_e)
        total += contribution
        shifts.append(BinShift(label, e, a, contribution))
    return float(total), shifts


def fit_reference_shares(binning: FeatureBinning) -> dict[str, float]:
    """The expected distribution: bin shares as they were at fit time."""
    total = sum(row.population for row in binning.rows)
    if total == 0:
        return {row.label: 0.0 for row in binning.rows}
    return {row.label: row.population / total for row in binning.rows}


def per_feature_drift(
    binnings: Mapping[str, FeatureBinning],
    expected_shares: Mapping[str, Mapping[str, float]],
    labels_by_feature: Mapping[str, Mapping[int, Sequence[str]]],
    cfg: DriftConfig,
) -> tuple[FeatureDrift, ...]:
    """PSI and CSI for each feature in each later period.

    ``labels_by_feature[feature][period]`` is the sequence of bin labels that period's
    rows landed in. Periods come from ``drift.period_column`` (``fold`` here, because
    that is the walk-forward unit the splits module emits).
    """
    out: list[FeatureDrift] = []
    for feature in sorted(binnings):
        reference = expected_shares[feature]
        for period in sorted(labels_by_feature.get(feature, {})):
            observed = labels_by_feature[feature][period]
            actual = _shares(observed, list(reference))
            psi, shifts = psi_from_shares(reference, actual)
            status = (
                STATUS_ACTION
                if psi >= cfg.psi_action
                else (STATUS_WATCH if psi >= cfg.psi_watch else STATUS_STABLE)
            )
            out.append(
                FeatureDrift(
                    feature=feature,
                    period=period,
                    psi=psi,
                    csi=tuple(shifts),
                    status=status,
                    population=len(observed),
                )
            )
    return tuple(out)


def score_psi(expected_scores: np.ndarray, actual_scores: np.ndarray, n_bins: int) -> float:
    """PSI of the score distribution itself, on fixed quantile bins.

    The bins come from the *expected* (fit) distribution, never from the scored one:
    re-binning on the current period would hide a shift in where the mass sits, which
    is the entire thing PSI is meant to catch.
    """
    if expected_scores.size == 0 or actual_scores.size == 0:
        return 0.0
    edges = np.quantile(expected_scores.astype(np.float64), np.linspace(0.0, 1.0, n_bins + 1))
    edges = np.unique(edges)
    if edges.size < 2:
        # A constant score at fit time: any non-constant actual distribution is
        # unbounded drift, so report the floor rather than dividing by zero.
        return float("inf")
    edges[0] = -np.inf
    edges[-1] = np.inf
    expected = _histogram_share(expected_scores.astype(np.float64), edges)
    actual = _histogram_share(actual_scores.astype(np.float64), edges)
    value, _ = psi_from_shares(expected, actual)
    return value


def _histogram_share(values: np.ndarray, edges: np.ndarray) -> dict[str, float]:
    indices = np.searchsorted(edges[1:-1], values, side="right")
    counts = np.bincount(indices, minlength=edges.size - 1).astype(np.float64)
    total = counts.sum()
    return {f"bin{index}": float(counts[index] / total) for index in range(counts.size)}


def drift_decision(score_psi_worst: float, feature_drift: Sequence[FeatureDrift], cfg: DriftConfig) -> DriftReport:
    """Turn drift numbers into the run's mode, with the banner text that explains it.

    Action triggers on *either* the score distribution or a single feature crossing
    ``psi_action``: a stable aggregate over a feature whose top band emptied is
    precisely the drift a total PSI averages away.
    """
    worst_feature = max((drift.psi for drift in feature_drift), default=0.0)
    triggered = score_psi_worst >= cfg.psi_action or worst_feature >= cfg.psi_action
    action = "degrade_to_rules_plus_scorecard" if triggered else "keep_full_model_stack"
    mode = ACTION_DEGRADED if triggered else ACTION_NORMAL
    if triggered:
        banner = (
            f"DEGRADED SCORING: PSI {score_psi_worst:.3f} (score) / {worst_feature:.3f} "
            f"(worst feature) crossed drift.psi_action={cfg.psi_action}. The GBM, the fused "
            "meta-learner and the calibration were all fitted on the population that has "
            "moved, so their probabilities are not defensible. Scores are produced by the "
            "rules layer plus the audited scorecard points table, which is less accurate "
            "and still explainable."
        )
    elif score_psi_worst >= cfg.psi_watch or worst_feature >= cfg.psi_watch:
        banner = (
            f"DRIFT WATCH: PSI {score_psi_worst:.3f} (score) / {worst_feature:.3f} (worst "
            f"feature) is at or above drift.psi_watch={cfg.psi_watch} but below "
            f"drift.psi_action={cfg.psi_action}. Full model stack remains in use."
        )
    else:
        banner = (
            f"No drift action: PSI {score_psi_worst:.3f} is below drift.psi_watch="
            f"{cfg.psi_watch}."
        )
    return DriftReport(
        features=tuple(feature_drift),
        score_psi={},
        score_psi_worst=score_psi_worst,
        action=action,
        mode=mode,
        banner=banner,
        thresholds={
            "psi_watch": cfg.psi_watch,
            "psi_action": cfg.psi_action,
            "csi_watch": cfg.csi_watch,
            "csi_action": cfg.csi_action,
        },
        psi_zero_floor=PSI_ZERO_FLOOR,
        expected_reference=cfg.expected_reference,
    )


@dataclass(frozen=True, slots=True)
class MigrationMatrix:
    """Band-to-band movement between two consecutive periods."""

    from_period: int
    to_period: int
    band_ids: tuple[str, ...]
    counts: Mapping[str, Mapping[str, int]]
    accounts_in_both: int
    downgrade_rate: float
    upgrade_rate: float
    alert: bool
    alert_threshold: float

    def to_dict(self) -> dict[str, object]:
        return {
            "from_period": self.from_period,
            "to_period": self.to_period,
            "band_ids": list(self.band_ids),
            "counts": {key: dict(row) for key, row in sorted(self.counts.items())},
            "accounts_in_both": self.accounts_in_both,
            "downgrade_rate": round(self.downgrade_rate, 6),
            "upgrade_rate": round(self.upgrade_rate, 6),
            "alert": self.alert,
            "alert_threshold": self.alert_threshold,
            "note": (
                "downgrade means moving toward band E, i.e. toward risk: points fall as "
                "risk rises because the scorecard scale is the PDO scale (higher = safer)"
            ),
        }


def rating_migration(
    scored_periods: pl.DataFrame,
    band_ids: Sequence[str],
    cfg: DriftConfig,
) -> tuple[MigrationMatrix, ...]:
    """Period-over-period band movement for accounts present in both periods.

    ``scored_periods`` needs ``fold``, ``account_key`` and ``band``. Consecutive folds
    are consecutive periods in the walk-forward, which is why the migration is computed
    on the fold column rather than on a calendar assumption.
    """
    if not cfg.migration_matrix:
        return ()
    missing = [name for name in (COL_FOLD, COL_ACCOUNT_KEY, "band") if name not in scored_periods.columns]
    if missing:
        raise ValueError(f"rating migration needs {missing}; the scored frame lacks them")
    periods = sorted(int(value) for value in scored_periods.get_column(COL_FOLD).unique().to_list())
    if len(periods) < 2:
        return ()
    band_position = {band: index for index, band in enumerate(band_ids)}
    out: list[MigrationMatrix] = []
    for previous, current in pairwise(periods):
        left = scored_periods.filter(pl.col(COL_FOLD) == previous).select(
            [pl.col(COL_ACCOUNT_KEY), pl.col("band").alias("band_from")]
        )
        right = scored_periods.filter(pl.col(COL_FOLD) == current).select(
            [pl.col(COL_ACCOUNT_KEY), pl.col("band").alias("band_to")]
        )
        joined = left.join(right, on=COL_ACCOUNT_KEY, how="inner")
        counts: dict[str, dict[str, int]] = {
            band: {other: 0 for other in band_ids} for band in band_ids
        }
        downgrades = 0
        upgrades = 0
        for band_from, band_to in joined.select(["band_from", "band_to"]).iter_rows():
            counts[str(band_from)][str(band_to)] += 1
            distance = band_position.get(str(band_to), 0) - band_position.get(str(band_from), 0)
            if distance > 0:
                downgrades += 1
            elif distance < 0:
                upgrades += 1
        total = joined.height
        downgrade_rate = downgrades / total if total else 0.0
        out.append(
            MigrationMatrix(
                from_period=previous,
                to_period=current,
                band_ids=tuple(band_ids),
                counts=counts,
                accounts_in_both=total,
                downgrade_rate=downgrade_rate,
                upgrade_rate=(upgrades / total if total else 0.0),
                alert=downgrade_rate > cfg.downgrade_rate_alert,
                alert_threshold=cfg.downgrade_rate_alert,
            )
        )
    return tuple(out)


def positive_rate_by_period(frame: pl.DataFrame) -> dict[int, float]:
    """Observed bad rate per fold: the label-side companion to feature drift.

    A population that shifts while the base rate holds is a scoring problem; a base
    rate that shifts is a labelling or typology problem, and the two need different
    responses. Kept beside PSI so the run cannot read one without the other.
    """
    out: dict[int, float] = {}
    for fold, group in frame.partition_by([COL_FOLD], maintain_order=True):
        del fold
        key = int(group.get_column(COL_FOLD)[0])
        total = group.height
        out[key] = float(group.get_column(COL_LABEL).sum()) / total if total else 0.0
    return dict(sorted(out.items()))


__all__ = [
    "ACTION_DEGRADED",
    "ACTION_NORMAL",
    "PSI_ZERO_FLOOR",
    "STATUS_ACTION",
    "STATUS_STABLE",
    "STATUS_WATCH",
    "BinShift",
    "DriftReport",
    "FeatureDrift",
    "MigrationMatrix",
    "drift_decision",
    "fit_reference_shares",
    "per_feature_drift",
    "positive_rate_by_period",
    "psi_from_shares",
    "rating_migration",
    "score_psi",
]
