"""Calibration: which branch ran, what it cost, and when the answer is "refused".

Plan §11 states why this layer is load-bearing rather than decorative: Module C
multiplies these probabilities by money, so a miscalibrated model produces a wrong
currency figure, and the wrongness is invisible -- the arithmetic downstream is
impeccable. Three behaviours follow, and all three are reported in the artefact rather
than assumed:

* **Isotonic on validation, falling back to Platt below ``isotonic_min_positives`` (500),
  and REFUSED below ``min_positives_for_calibration``.** Isotonic is a step function
  fitted to the observed rates; on a thin positive count it steps on noise and reports a
  confident 0.0 or 1.0. Platt is the honest weaker estimate. Below the floor neither is
  defensible, so the output says *probabilities are uncalibrated* and carries the positive
  count, the floor and the branch that would have been chosen (03 H:
  ``test_calibration_refused_below_floor``). The floor is config, not a number in code.
* **Oversampled positives get corrected before they get calibrated.** With King-Zeng
  intercept correction the logit shifts by ``ln(odds_population / odds_train)``, which
  pulls the mean predicted probability back onto the base rate the population actually
  has. Which method was used, both base rates and the shift value are stated in the model
  card payload (plan §10: "say which in the model card"; 03 H:
  ``test_prior_correction_applied``).
* **The confidence label the UI shows is a rate and a count, never an adjective.**
  ``"observed rate in this band: 7.1%, n=432"`` -- taken from the calibration bin the
  prediction lands in, together with its sample size. "High confidence" is a feeling;
  an observed rate with an n is a measurement, and the n is what tells a reader how much
  to trust the rate.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Final

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from oxbow.models.config import CalibrationConfig
from oxbow.models.errors import CalibrationRefusedError, ModelLayerError

PROBABILITY_CLIP: Final = 1e-7
MIN_LOGIT_DENOMINATOR: Final = 1e-12
# The stored isotonic step function is capped so an artefact does not carry tens of
# thousands of breakpoints. The cap is a stated limit, not a silent truncation: hitting
# it is reported in the artefact's knot count.
ISOTONIC_KNOT_LIMIT: Final = 512


def logit(probability: np.ndarray) -> np.ndarray:
    """Logit with the probability clipped inside (0, 1).

    Clipped rather than guarded per call: a raw model probability of exactly 0.0 or 1.0
    is common in boosting and an infinite logit would poison every downstream fit with
    no diagnostic. The clip magnitude is in ``PROBABILITY_CLIP``, one place, stated.
    """
    clipped = np.clip(
        np.asarray(probability, dtype=np.float64), PROBABILITY_CLIP, 1.0 - PROBABILITY_CLIP
    )
    return np.log(clipped / (1.0 - clipped))


def sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -50.0, 50.0)))


def odds(probability: float) -> float:
    bounded = min(max(probability, PROBABILITY_CLIP), 1.0 - PROBABILITY_CLIP)
    return bounded / (1.0 - bounded)


@dataclass(frozen=True, slots=True)
class PriorCorrection:
    """The King-Zeng intercept correction, applied or explicitly not needed."""

    method: str
    applied: bool
    train_positive_share: float
    population_positive_share: float
    tolerance_multiple: float
    logit_shift: float
    reason: str
    decision_rule: str

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "applied": self.applied,
            "train_positive_share": round(self.train_positive_share, 10),
            "population_positive_share": round(self.population_positive_share, 10),
            "tolerance_multiple": self.tolerance_multiple,
            "logit_shift": round(self.logit_shift, 8),
            "formula": "shift = ln(odds(population) / odds(train)), added to the raw logit",
            "reason": self.reason,
            "decision_rule": self.decision_rule,
        }


def correct_prior(
    train_positive_share: float,
    population_positive_share: float,
    cfg: CalibrationConfig,
) -> PriorCorrection:
    """Decide whether the training prior was oversampled, and shift the intercept.

    The comparison is against the population share, which is either declared in config
    (a number measured on the corpus and written into the dataset card) or measured on
    the validation slice, which oversampling never touches.
    """
    threshold = cfg.oversampling_tolerance_multiple * max(
        population_positive_share, PROBABILITY_CLIP
    )
    oversampled = train_positive_share > threshold
    decision_rule = (
        f"applied when train share > {cfg.oversampling_tolerance_multiple} x population share"
    )
    if cfg.oversampling_correction == "base_rate_preserving_holdout":
        return PriorCorrection(
            method=cfg.oversampling_correction,
            applied=False,
            train_positive_share=train_positive_share,
            population_positive_share=population_positive_share,
            tolerance_multiple=cfg.oversampling_tolerance_multiple,
            logit_shift=0.0,
            reason=(
                "calibration is fitted on a base-rate-preserving holdout, so the raw score "
                "already carries the population prior and no intercept shift is applied. "
                f"Train share {train_positive_share:.6f} vs population share "
                f"{population_positive_share:.6f}."
            ),
            decision_rule=decision_rule,
        )
    if cfg.oversampling_correction != "king_zeng_intercept":
        raise ModelLayerError(
            f"calibration.oversampling_correction={cfg.oversampling_correction!r} is not a "
            "method this build implements; the plan allows exactly two and config must name one"
        )
    if not oversampled:
        return PriorCorrection(
            method=cfg.oversampling_correction,
            applied=False,
            train_positive_share=train_positive_share,
            population_positive_share=population_positive_share,
            tolerance_multiple=cfg.oversampling_tolerance_multiple,
            logit_shift=0.0,
            reason=(
                f"train share {train_positive_share:.6f} is within "
                f"{cfg.oversampling_tolerance_multiple}x of the population share "
                f"{population_positive_share:.6f}, so there is no oversampling to correct. "
                "Not applied is stated rather than left blank."
            ),
            decision_rule=decision_rule,
        )
    shift = math.log(odds(population_positive_share) / odds(train_positive_share))
    return PriorCorrection(
        method=cfg.oversampling_correction,
        applied=True,
        train_positive_share=train_positive_share,
        population_positive_share=population_positive_share,
        tolerance_multiple=cfg.oversampling_tolerance_multiple,
        logit_shift=shift,
        reason=(
            f"train positives are oversampled: share {train_positive_share:.6f} against a "
            f"population share of {population_positive_share:.6f}. King-Zeng intercept "
            f"correction shifts the logit by {shift:.6f} before calibration, so the mean "
            "probability is anchored to the base rate Module C will be multiplying money by."
        ),
        decision_rule=decision_rule,
    )


@dataclass(frozen=True, slots=True)
class ConfidenceBin:
    """One reliability bin: the observed rate and the sample size behind it."""

    bin_index: int
    probability_lower: float
    probability_upper: float
    sample_size: int
    observed_rate: float
    mean_predicted: float

    @property
    def label_text(self) -> str:
        return (
            f"observed rate in this band: {self.observed_rate * 100:.2f}%, " f"n={self.sample_size}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "bin_index": self.bin_index,
            "probability_lower": round(self.probability_lower, 8),
            "probability_upper": round(self.probability_upper, 8),
            "sample_size": self.sample_size,
            "observed_rate": round(self.observed_rate, 8),
            "mean_predicted": round(self.mean_predicted, 8),
            "label_text": self.label_text,
        }


@dataclass(frozen=True, slots=True)
class CalibrationOutcome:
    """What calibration produced, refused, or both."""

    method: str
    refused: bool
    refusal_reason: str | None
    validation_positives: int
    validation_rows: int
    floor: int
    isotonic_min_positives: int
    branch_reason: str
    brier: float
    ece: float
    reliability: tuple[ConfidenceBin, ...]
    prior_correction: PriorCorrection
    platt_weight: float | None
    platt_intercept: float | None
    isotonic_x: tuple[float, ...]
    isotonic_y: tuple[float, ...]
    mean_raw_probability: float
    mean_calibrated_probability: float
    raw_brier: float

    @property
    def calibrated(self) -> bool:
        return not self.refused and self.method in {"isotonic", "platt"}

    def apply(self, raw_probabilities: np.ndarray, demand: bool = False) -> np.ndarray | None:
        """Map raw scores to probabilities, or refuse.

        Refusal returns ``None`` so a caller cannot accidentally multiply money by it;
        ``demand=True`` raises for the callers that have no degraded path and must not
        proceed (the model card builder uses it, because a card without probabilities is
        not a card).
        """
        values = np.asarray(raw_probabilities, dtype=np.float64)
        shifted = sigmoid(logit(np.clip(values, 0.0, 1.0)) + self.prior_correction.logit_shift)
        if self.refused:
            if demand:
                raise CalibrationRefusedError(self.refusal_reason or "calibration refused")
            return None
        if self.method == "platt":
            if self.platt_weight is None or self.platt_intercept is None:
                raise ModelLayerError(
                    "calibration reports method='platt' but carries no fitted parameters"
                )
            return sigmoid(self.platt_weight * logit(shifted) + self.platt_intercept)
        if not self.isotonic_x:
            raise ModelLayerError(
                "calibration reports method='isotonic' but carries no fitted step function"
            )
        regressor = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
        regressor.fit(np.asarray(self.isotonic_x), np.asarray(self.isotonic_y))
        return np.asarray(regressor.predict(shifted), dtype=np.float64)

    def confidence_label(self, calibrated_probability: float | None) -> dict[str, object]:
        """The label the UI shows: an observed rate and its sample size, or the refusal.

        Never an adjective. A probability without a measured rate behind it is a number
        the reader cannot weigh, and "high confidence" is the invention this function
        exists to prevent.
        """
        if self.refused or calibrated_probability is None:
            return {
                "kind": "uncalibrated",
                "text": (
                    "probabilities are uncalibrated: "
                    f"{self.refusal_reason or self.branch_reason}"
                ),
                "observed_rate": None,
                "sample_size": None,
                "band_index": None,
            }
        for entry in self.reliability:
            if entry.probability_lower <= calibrated_probability < entry.probability_upper:
                return {
                    "kind": "calibrated_band",
                    "text": entry.label_text,
                    "observed_rate": entry.observed_rate,
                    "sample_size": entry.sample_size,
                    "band_index": entry.bin_index,
                }
        edge = self.reliability[-1] if self.reliability else None
        if edge is None:
            return {
                "kind": "uncalibrated",
                "text": "no reliability bins were fitted",
                "observed_rate": None,
                "sample_size": None,
                "band_index": None,
            }
        return {
            "kind": "calibrated_band",
            "text": edge.label_text,
            "observed_rate": edge.observed_rate,
            "sample_size": edge.sample_size,
            "band_index": edge.bin_index,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "calibrated": self.calibrated,
            "validation_positives": self.validation_positives,
            "validation_rows": self.validation_rows,
            "floor": self.floor,
            "isotonic_min_positives": self.isotonic_min_positives,
            "branch_reason": self.branch_reason,
            "brier": round(self.brier, 8),
            "brier_raw_uncorrected": round(self.raw_brier, 8),
            "ece": round(self.ece, 8),
            "reliability_curve": [entry.to_dict() for entry in self.reliability],
            "prior_correction": self.prior_correction.to_dict(),
            "platt": (
                None
                if self.platt_weight is None
                else {
                    "weight": round(self.platt_weight, 8),
                    "intercept": round(self.platt_intercept or 0.0, 8),
                }
            ),
            "isotonic_boundaries": {
                "x": [round(value, 8) for value in self.isotonic_x],
                "y": [round(value, 8) for value in self.isotonic_y],
            },
            "mean_raw_probability": round(self.mean_raw_probability, 8),
            "mean_calibrated_probability": round(self.mean_calibrated_probability, 8),
            "confidence_label_examples": [entry.label_text for entry in self.reliability[:3]],
            "isotonic_knots_stored": len(self.isotonic_x),
            "isotonic_knot_limit": ISOTONIC_KNOT_LIMIT,
        }


def choose_method(validation_positives: int, cfg: CalibrationConfig) -> tuple[str, str]:
    """Which calibration branch runs, with the reason a reviewer can check."""
    if validation_positives < cfg.min_positives_for_calibration:
        return (
            "refused",
            f"{validation_positives} validation positives is below "
            f"calibration.min_positives_for_calibration={cfg.min_positives_for_calibration}; "
            "an observed rate measured on that few rows is noise, and Module C multiplies "
            "it by money",
        )
    if validation_positives >= cfg.isotonic_min_positives:
        return (
            cfg.method,
            f"{validation_positives} validation positives is at or above "
            f"calibration.isotonic_min_positives={cfg.isotonic_min_positives}, so "
            f"{cfg.method} is fitted",
        )
    return (
        cfg.fallback_method,
        f"{validation_positives} validation positives is below "
        f"calibration.isotonic_min_positives={cfg.isotonic_min_positives}, so isotonic would "
        f"step on noise and the declared fallback {cfg.fallback_method!r} runs instead",
    )


def brier_score(labels: np.ndarray, probabilities: np.ndarray) -> float:
    if labels.size == 0:
        raise ModelLayerError("Brier is undefined on an empty slice")
    delta = np.asarray(probabilities, dtype=np.float64) - np.asarray(labels, dtype=np.float64)
    return float(np.mean(np.square(delta)))


def reliability_curve(
    probabilities: np.ndarray, labels: np.ndarray, bins: int
) -> tuple[ConfidenceBin, ...]:
    """Equal-count bins of predicted probability against observed rate.

    Equal-count rather than equal-width: under a 0.1 % base rate an equal-width curve
    puts every account in the first two bins and leaves eight empty, which reads as a
    well-behaved model. The sample size per bin is reported with the rate for the same
    reason.
    """
    if probabilities.size != labels.size:
        raise ModelLayerError("reliability curve needs one label per probability")
    if probabilities.size == 0:
        return ()
    order = np.argsort(probabilities, kind="stable")
    sorted_prob = np.asarray(probabilities, dtype=np.float64)[order]
    sorted_label = np.asarray(labels, dtype=np.float64)[order]
    edges = np.linspace(0, sorted_prob.size, bins + 1, dtype=np.int64)
    out: list[ConfidenceBin] = []
    index = 0
    for lower, upper in itertools.pairwise(edges):
        if upper <= lower:
            continue
        block_prob = sorted_prob[lower:upper]
        block_label = sorted_label[lower:upper]
        out.append(
            ConfidenceBin(
                bin_index=index,
                probability_lower=float(block_prob[0]),
                probability_upper=float(block_prob[-1])
                if upper - lower > 1
                else float(block_prob[-1]),
                sample_size=int(upper - lower),
                observed_rate=float(np.mean(block_label)),
                mean_predicted=float(np.mean(block_prob)),
            )
        )
        index += 1
    return tuple(out)


def expected_calibration_error(curve: tuple[ConfidenceBin, ...], total_rows: int) -> float:
    """ECE over the reliability bins already measured -- no second fit to disagree with."""
    if not curve or total_rows <= 0:
        return 0.0
    return float(
        sum(
            (entry.sample_size / total_rows) * abs(entry.observed_rate - entry.mean_predicted)
            for entry in curve
        )
    )


def calibrate(
    raw_probabilities: np.ndarray,
    labels: np.ndarray,
    cfg: CalibrationConfig,
    train_positive_share: float,
    population_positive_share: float,
) -> CalibrationOutcome:
    """Correct the prior, choose the branch, fit, and measure the result.

    ``raw_probabilities`` and ``labels`` are the pooled **validation** slice: fitting a
    calibrator on the test fold would put the answer inside the thing that turns scores
    into money figures.
    """
    raw = np.asarray(raw_probabilities, dtype=np.float64)
    y = np.asarray(labels, dtype=np.int32)
    if raw.size != y.size:
        raise ModelLayerError("calibration needs one label per raw probability")
    if raw.size == 0:
        raise ModelLayerError("calibration was handed an empty validation slice")
    positives = int(y.sum())
    method, branch_reason = choose_method(positives, cfg)
    correction = correct_prior(train_positive_share, population_positive_share, cfg)
    corrected = sigmoid(logit(np.clip(raw, 0.0, 1.0)) + correction.logit_shift)

    if method == "refused":
        # The metrics of the *unfitted* corrected scores are still reported, because the
        # honest statement is "here is what we have and here is why we will not call it
        # a probability" -- not "here is nothing".
        curve = reliability_curve(corrected, y, cfg.reliability_bins)
        return CalibrationOutcome(
            method="refused",
            refused=True,
            refusal_reason=(
                f"{branch_reason}. below_floor_action="
                f"{cfg.below_floor_action!r}: probabilities are labelled uncalibrated and "
                "no calibrated figure is emitted"
            ),
            validation_positives=positives,
            validation_rows=int(y.size),
            floor=cfg.min_positives_for_calibration,
            isotonic_min_positives=cfg.isotonic_min_positives,
            branch_reason=branch_reason,
            brier=brier_score(y, corrected),
            ece=expected_calibration_error(curve, int(y.size)),
            reliability=curve,
            prior_correction=correction,
            platt_weight=None,
            platt_intercept=None,
            isotonic_x=(),
            isotonic_y=(),
            mean_raw_probability=float(np.mean(raw)),
            mean_calibrated_probability=float(np.mean(corrected)),
            raw_brier=brier_score(y, raw),
        )

    isotonic_x: tuple[float, ...] = ()
    isotonic_y: tuple[float, ...] = ()
    weight: float | None = None
    intercept: float | None = None
    if method == "isotonic":
        regressor = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
        regressor.fit(corrected, y.astype(np.float64))
        calibrated = np.asarray(regressor.predict(corrected), dtype=np.float64)
        # The fitted step function IS the model. sklearn keeps its thresholds, and storing
        # those (rather than a sample of the domain) is what lets a reviewer reproduce any
        # prediction without re-running the fit.
        knots_x_raw = np.asarray(getattr(regressor, "X_thresholds_", corrected), dtype=np.float64)
        knots_y_raw = np.asarray(getattr(regressor, "y_thresholds_", calibrated), dtype=np.float64)
        keep = min(knots_x_raw.size, ISOTONIC_KNOT_LIMIT)
        isotonic_x = tuple(float(value) for value in knots_x_raw[:keep])
        isotonic_y = tuple(float(value) for value in knots_y_raw[:keep])
    elif method == "platt":
        scaler = LogisticRegression(penalty=None, solver="lbfgs", max_iter=1000, tol=1e-10)
        scaler.fit(logit(corrected).reshape(-1, 1), y)
        weight = float(scaler.coef_[0][0])
        intercept = float(scaler.intercept_[0])
        calibrated = sigmoid(weight * logit(corrected) + intercept)
    else:
        raise ModelLayerError(f"unknown calibration method {method!r}")

    curve = reliability_curve(calibrated, y, cfg.reliability_bins)
    return CalibrationOutcome(
        method=method,
        refused=False,
        refusal_reason=None,
        validation_positives=positives,
        validation_rows=int(y.size),
        floor=cfg.min_positives_for_calibration,
        isotonic_min_positives=cfg.isotonic_min_positives,
        branch_reason=branch_reason,
        brier=brier_score(y, calibrated),
        ece=expected_calibration_error(curve, int(y.size)),
        reliability=curve,
        prior_correction=correction,
        platt_weight=weight,
        platt_intercept=intercept,
        isotonic_x=isotonic_x,
        isotonic_y=isotonic_y,
        mean_raw_probability=float(np.mean(raw)),
        mean_calibrated_probability=float(np.mean(calibrated)),
        raw_brier=brier_score(y, raw),
    )


def calibration_from_dict(payload: dict[str, object]) -> CalibrationOutcome:
    """Rebuild the outcome from the model card, so a scorer can apply it offline."""
    correction_raw = payload["prior_correction"]
    if not isinstance(correction_raw, dict):
        raise ModelLayerError("model card has no prior_correction block")
    correction = PriorCorrection(
        method=str(correction_raw["method"]),
        applied=bool(correction_raw["applied"]),
        train_positive_share=float(correction_raw["train_positive_share"]),
        population_positive_share=float(correction_raw["population_positive_share"]),
        tolerance_multiple=float(correction_raw["tolerance_multiple"]),
        logit_shift=float(correction_raw["logit_shift"]),
        reason=str(correction_raw["reason"]),
        decision_rule=str(correction_raw["decision_rule"]),
    )
    platt = payload.get("platt")
    weight = float(platt["weight"]) if isinstance(platt, dict) else None
    intercept = float(platt["intercept"]) if isinstance(platt, dict) else None
    bounds = payload.get("isotonic_boundaries")
    knots_x: tuple[float, ...] = ()
    knots_y: tuple[float, ...] = ()
    if isinstance(bounds, dict):
        knots_x = tuple(float(value) for value in bounds.get("x", []))
        knots_y = tuple(float(value) for value in bounds.get("y", []))
    curve_raw = payload.get("reliability_curve", [])
    curve = (
        tuple(
            ConfidenceBin(
                bin_index=int(entry["bin_index"]),
                probability_lower=float(entry["probability_lower"]),
                probability_upper=float(entry["probability_upper"]),
                sample_size=int(entry["sample_size"]),
                observed_rate=float(entry["observed_rate"]),
                mean_predicted=float(entry["mean_predicted"]),
            )
            for entry in curve_raw
            if isinstance(entry, dict)
        )
        if isinstance(curve_raw, list)
        else ()
    )
    return CalibrationOutcome(
        method=str(payload["method"]),
        refused=bool(payload["refused"]),
        refusal_reason=(
            None if payload.get("refusal_reason") is None else str(payload["refusal_reason"])
        ),
        validation_positives=int(payload["validation_positives"]),
        validation_rows=int(payload["validation_rows"]),
        floor=int(payload["floor"]),
        isotonic_min_positives=int(payload["isotonic_min_positives"]),
        branch_reason=str(payload["branch_reason"]),
        brier=float(payload["brier"]),
        ece=float(payload["ece"]),
        reliability=curve,
        prior_correction=correction,
        platt_weight=weight,
        platt_intercept=intercept,
        isotonic_x=knots_x,
        isotonic_y=knots_y,
        mean_raw_probability=float(payload["mean_raw_probability"]),
        mean_calibrated_probability=float(payload["mean_calibrated_probability"]),
        raw_brier=float(payload["brier_raw_uncorrected"]),
    )


__all__ = [
    "ISOTONIC_KNOT_LIMIT",
    "CalibrationOutcome",
    "ConfidenceBin",
    "PriorCorrection",
    "brier_score",
    "calibrate",
    "calibration_from_dict",
    "choose_method",
    "correct_prior",
    "expected_calibration_error",
    "logit",
    "odds",
    "reliability_curve",
    "sigmoid",
]
