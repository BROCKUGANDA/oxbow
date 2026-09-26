"""Fusion: a constrained non-negative logistic meta-learner, fitted on validation.

Plan §10 is specific, and the specificity is the point. **No hand-tuned weights.** A
blend whose coefficients someone typed into a config file is a taste statement wearing
a number; it cannot be defended when the corpus shifts, and it silently decides that the
anomaly channel matters more than a confirmed rule hit. So the weights are *fitted* on
the validation slice, under one constraint -- every channel weight is non-negative.

That constraint is not cosmetic. Each input is a risk signal in the same direction
(higher means more suspicious), so a negative fitted weight would mean the meta-learner
learned "the stronger the pass-through evidence, the safer this account looks". On a
thin validation slice that is a noise artefact, and it would be printed on the
validation page as if it were a finding. Bounding the weights at zero costs some fit; it
buys a fused score that is explicable in one line, which is the deal the plan struck.

The printed coefficients carry a second column, ``standardised_weight = weight x
observed input spread``, because the inputs live on different scales: a rule-hit count
and a probability in [0, 1] are not comparable at the same coefficient, and a reader
given only the raw weights would rank the channels wrongly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import polars as pl
from scipy.optimize import minimize

from oxbow.models.config import FusionConfig
from oxbow.models.errors import FusionInputsError
from oxbow.models.inputs import FUSION_INPUTS, fusion_matrix
from oxbow.scoring.frame import COL_LABEL

PROBABILITY_FLOOR: Final = 1e-6


@dataclass(frozen=True, slots=True)
class FusionCoefficient:
    """One channel's fitted weight, with the scale context a reader needs."""

    input_name: str
    weight: float
    standardised_weight: float
    observed_mean: float
    observed_spread: float

    def to_dict(self) -> dict[str, object]:
        return {
            "input": self.input_name,
            "weight": round(self.weight, 8),
            "standardised_weight": round(self.standardised_weight, 8),
            "observed_mean": round(self.observed_mean, 8),
            "observed_spread": round(self.observed_spread, 8),
        }


@dataclass(frozen=True, slots=True)
class FusionModel:
    """The fitted meta-learner: intercept, coefficients, and how it was fitted."""

    intercept: float
    coefficients: tuple[FusionCoefficient, ...]
    inputs: tuple[str, ...]
    regularisation_strength: float
    fitted_on: str
    rows: int
    positives: int
    converged: bool
    solver_iterations: int
    validation_brier: float
    validation_pr_auc: float

    @property
    def weights(self) -> np.ndarray:
        return np.array([entry.weight for entry in self.coefficients], dtype=np.float64)

    def predict_matrix(self, matrix: np.ndarray) -> np.ndarray:
        values = self.intercept + np.asarray(matrix, dtype=np.float64) @ self.weights
        return 1.0 / (1.0 + np.exp(-np.clip(values, -50.0, 50.0)))

    def predict_frame(self, frame: pl.DataFrame) -> np.ndarray:
        return self.predict_matrix(fusion_matrix(frame, self.inputs))

    def one_line(self) -> str:
        """The fused score in one line, exactly as the plan asks it to be printable."""
        terms = " + ".join(
            f"{entry.weight:.4f}*{entry.input_name}" for entry in self.coefficients
        )
        return f"p_fused = sigmoid({self.intercept:.4f} + {terms})"

    def to_dict(self) -> dict[str, object]:
        return {
            "method": "constrained_logistic_non_negative",
            "intercept": round(self.intercept, 8),
            "coefficients": [entry.to_dict() for entry in self.coefficients],
            "inputs": list(self.inputs),
            "regularisation_strength": self.regularisation_strength,
            "fitted_on": self.fitted_on,
            "rows": self.rows,
            "positives": self.positives,
            "converged": self.converged,
            "solver_iterations": self.solver_iterations,
            "validation_brier": round(self.validation_brier, 8),
            "validation_pr_auc": round(self.validation_pr_auc, 8),
            "one_line": self.one_line(),
            "note": (
                "weights are fitted on validation under a non-negativity constraint; no "
                "coefficient in this object was chosen by hand, and standardised_weight is "
                "given because the inputs are not on a common scale"
            ),
        }


def _negative_log_likelihood(
    parameters: np.ndarray,
    matrix: np.ndarray,
    labels: np.ndarray,
    regularisation_strength: float,
) -> float:
    intercept = float(parameters[0])
    weights = parameters[1:]
    values = intercept + matrix @ weights
    probabilities = np.clip(1.0 / (1.0 + np.exp(-np.clip(values, -50.0, 50.0))), PROBABILITY_FLOOR, 1.0 - PROBABILITY_FLOOR)
    nll = -np.mean(labels * np.log(probabilities) + (1.0 - labels) * np.log(1.0 - probabilities))
    # L2 on the weights only: the intercept is the population prior's home and shrinking
    # it would fight the King-Zeng correction that already lives there.
    penalty = regularisation_strength * float(np.sum(weights * weights)) / (2.0 * matrix.shape[0])
    return float(nll + penalty)


def fit_fusion(
    frame: pl.DataFrame,
    cfg: FusionConfig,
    inputs: tuple[str, ...] = FUSION_INPUTS,
) -> FusionModel:
    """Fit the meta-learner on the validation rows of ``frame``.

    ``frame`` must be the pooled validation slice. The determinism claim rests on the
    start point (zeros), the fixed solver tolerance, the fixed iteration cap and the
    absence of any sampling: the same validation rows always produce the same
    coefficients, which is what lets them be printed and argued with.
    """
    from oxbow.models.evaluate import average_precision, brier

    if tuple(inputs) != tuple(cfg.inputs):
        raise FusionInputsError(
            f"fusion inputs {list(inputs)} do not match model.yaml {list(cfg.inputs)}: "
            "the printed coefficients would be labelled with the wrong channel names"
        )
    matrix = fusion_matrix(frame, inputs)
    labels = frame.get_column(COL_LABEL).cast(pl.Float64).to_numpy().astype(np.float64)
    if matrix.shape[0] != labels.size:
        raise FusionInputsError("fusion matrix and label vector disagree on row count")
    positives = int(labels.sum())
    if positives == 0:
        raise FusionInputsError(
            "the validation slice handed to fusion has no positives, so no meta-learner can "
            "be fitted on it; the fold should have been skipped with a named reason"
        )
    if matrix.shape[0] <= len(inputs) + 1:
        raise FusionInputsError(
            f"{matrix.shape[0]} validation rows cannot fit {len(inputs)} weights honestly"
        )

    bounds: list[tuple[float | None, float | None]] = [(None, None)]
    bounds += [(0.0, None)] * matrix.shape[1] if cfg.non_negative else [(None, None)] * matrix.shape[1]
    start = np.zeros(1 + matrix.shape[1], dtype=np.float64)
    result = minimize(
        _negative_log_likelihood,
        start,
        args=(matrix, labels, cfg.regularisation_strength),
        method=cfg.solver,
        bounds=bounds,
        options={"maxiter": cfg.max_iter, "ftol": cfg.tolerance, "disp": False},
    )
    if not result.success and not bool(np.all(np.isfinite(result.x))):
        raise FusionInputsError(
            f"fusion solver failed: {result.message!r} with non-finite parameters"
        )
    intercept = float(result.x[0])
    weights = np.asarray(result.x[1:], dtype=np.float64)
    if cfg.non_negative and float(np.min(weights)) < -1e-9:
        raise FusionInputsError(
            "the solver returned a negative channel weight despite the bounds; the "
            "non-negativity constraint that makes the fused score explicable did not apply"
        )
    fused = np.clip(1.0 / (1.0 + np.exp(-np.clip(intercept + matrix @ weights, -50.0, 50.0))), PROBABILITY_FLOOR, 1.0 - PROBABILITY_FLOOR)
    means = matrix.mean(axis=0)
    spreads = matrix.std(axis=0, ddof=1) if matrix.shape[0] > 1 else np.zeros(matrix.shape[1])
    coefficients = tuple(
        FusionCoefficient(
            input_name=name,
            weight=float(weight),
            standardised_weight=float(weight * spread),
            observed_mean=float(mean),
            observed_spread=float(spread),
        )
        for name, weight, mean, spread in zip(inputs, weights, means, spreads, strict=True)
    )
    return FusionModel(
        intercept=intercept,
        coefficients=coefficients,
        inputs=tuple(inputs),
        regularisation_strength=cfg.regularisation_strength,
        fitted_on=cfg.fitted_on,
        rows=int(matrix.shape[0]),
        positives=positives,
        converged=bool(result.success),
        solver_iterations=int(getattr(result, "nit", 0)),
        validation_brier=brier(labels.astype(np.int32), fused),
        validation_pr_auc=average_precision(labels.astype(np.int32), fused),
    )


__all__ = ["FusionCoefficient", "FusionModel", "fit_fusion"]
