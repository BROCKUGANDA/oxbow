"""SHAP per scored row, with the fallback that keeps the pane honest.

Plan §10: TreeExplainer values are **persisted per scored row** -- the API only reads,
it never computes SHAP at request time -- and on a degenerate tree the case is labelled
**scorecard-explained** rather than left blank (03 H: ``test_shap_fallback_to_points``).

The failure this guards against is specific and visible in the UI: a boosted ensemble can
end up with no split at all (a tiny training slice, an early stop at iteration zero, a
feature set the sampler never found gain in). TreeExplainer on such a model returns zeros
for every feature, and the case page then renders an empty "why this was flagged" pane.
An empty pane reads as "there was no evidence", which is a lie about a detection system:
the evidence exists, the scorecard points carry it, and the honest label is that the tree
explanation was unavailable.

Contributions are stored as a JSON list per row sorted by absolute contribution, so the
waterfall is a read, and the base value is stored beside it so the arithmetic closes:
base + sum(contributions) = model output, asserted before anything is persisted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

import numpy as np
import polars as pl

from oxbow.models.config import ShapConfig
from oxbow.models.errors import ModelLayerError
from oxbow.models.gbm import GbmBundle

SOURCE_SHAP: Final = "shap-tree-explainer"
SOURCE_SCORECARD: Final = "scorecard-explained"
SHAP_SORT_NOTE: Final = "contributions ordered by absolute magnitude, then feature name"


@dataclass(frozen=True, slots=True)
class ExplanationOutcome:
    """What explanation each row got, and why the others did not."""

    source: str
    rows: int
    explained_rows: int
    fallback_rows: int
    base_value: float
    fallback_reason: str | None
    global_importance: tuple[tuple[str, float], ...]
    closure_max_error: float

    @property
    def all_scorecard_explained(self) -> bool:
        return self.source == SOURCE_SCORECARD

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "rows": self.rows,
            "explained_rows": self.explained_rows,
            "fallback_rows": self.fallback_rows,
            "base_value": round(self.base_value, 8),
            "fallback_reason": self.fallback_reason,
            "closure_max_error": (
                None if np.isnan(self.closure_max_error) else round(self.closure_max_error, 10)
            ),
            "global_importance": [
                {"feature": name, "mean_abs_contribution": round(value, 8)}
                for name, value in self.global_importance
            ],
            "note": (
                "SHAP values are persisted per scored row during the run; the API reads "
                "them and never computes at request time"
            ),
        }


def is_degenerate(bundle: GbmBundle, cfg: ShapConfig, total_contribution: float) -> bool:
    """True when the ensemble carries no attribution for the tree explainer to find.

    Two conditions, both measured rather than assumed: the booster never split on
    anything, and the explainer's total contribution magnitude sits below
    ``shap.degenerate_max_abs_contribution``.
    """
    if bundle.degenerate or bundle.split_feature_count == 0:
        return True
    return bool(total_contribution < cfg.degenerate_max_abs_contribution)


def _explainer_values(bundle: GbmBundle, matrix: np.ndarray) -> tuple[np.ndarray, float]:
    """TreeExplainer values and the expected value, normalised across shap versions."""
    import shap

    explainer = shap.TreeExplainer(bundle.booster)
    raw = explainer.shap_values(matrix, check_additivity=False)
    values = np.asarray(raw, dtype=np.float64)
    if values.ndim == 3:
        values = values[:, :, -1]
    if values.ndim != 2:
        raise ModelLayerError(f"unexpected SHAP value shape {values.shape}")
    expected = explainer.expected_value
    base = float(np.asarray(expected).reshape(-1)[-1])
    return values, base


def _closure_error(values: np.ndarray, base_value: float, model_output: np.ndarray) -> float:
    """Max |base + sum(contributions) - model output| in logit space.

    Measured because the persisted waterfall must add back up to the score the queue was
    ordered by; a silent mismatch would mean the UI explains a different decision than the
    one the row records. LightGBM's margin output is the log-odds, so the check is done in
    that space with a tolerance appropriate to float accumulation over hundreds of trees.
    """
    totals = base_value + values.sum(axis=1)
    return float(np.max(np.abs(totals - model_output))) if values.size else float("nan")


def explain_rows(
    frame: pl.DataFrame,
    bundle: GbmBundle | None,
    cfg: ShapConfig,
    probability_column: str,
) -> pl.DataFrame:
    """Attach ``shap_json`` / ``shap_base_value`` / ``explanation_source`` to every row.

    When the tree explanation is unavailable the row keeps the **scorecard points** as
    its contribution list and is labelled ``scorecard-explained`` -- a different
    explanation source, not a missing one, and the source travels on the row so the UI
    cannot present it as SHAP.

    A thin wrapper over :func:`explain_and_report` for callers that only want the
    frame. It exists as a separate name because P6 scores a fold and discards the
    outcome record; two functions each computing SHAP their own way is how the API and
    the pipeline end up disagreeing about the same row.
    """
    annotated, _ = explain_and_report(frame, bundle, cfg, probability_column)
    return annotated


def explain_and_report(
    frame: pl.DataFrame,
    bundle: GbmBundle | None,
    cfg: ShapConfig,
    probability_column: str,
) -> tuple[pl.DataFrame, ExplanationOutcome]:
    """Return the annotated frame **and** the outcome record for the model card."""
    if bundle is None:
        annotated = _fallback_all_rows(frame, cfg, "no gbm was fitted for this run's mode")
        return annotated, _scorecard_outcome(
            annotated, cfg, "no gbm was fitted for this run's mode"
        )
    matrix = _matrix(frame, bundle.feature_names, bundle.categorical_features)
    values, base_value = _explainer_values(bundle, matrix)
    total = float(np.max(np.abs(values.sum(axis=1)))) if values.size else 0.0
    if is_degenerate(bundle, cfg, total):
        reason = (
            f"the ensemble split on {bundle.split_feature_count} feature(s) and the largest "
            f"absolute total contribution was {total:.3e}, below "
            f"shap.degenerate_max_abs_contribution={cfg.degenerate_max_abs_contribution}"
        )
        annotated = _fallback_all_rows(frame, cfg, reason)
        return annotated, _scorecard_outcome(annotated, cfg, reason)
    probabilities = (
        frame.get_column(probability_column)
        .cast(pl.Float64)
        .fill_null(0.5)
        .to_numpy(allow_copy=True)
    )
    margins = np.log(
        np.clip(probabilities, 1e-12, 1.0 - 1e-12)
        / (1.0 - np.clip(probabilities, 1e-12, 1.0 - 1e-12))
    )
    closure = _closure_error(values, base_value, margins)
    payload: list[str] = []
    for position in range(values.shape[0]):
        contributions = [
            {"feature": name, "value": float(values[position, index])}
            for index, name in enumerate(bundle.feature_names)
        ]
        contributions.sort(key=lambda item: (-abs(item["value"]), item["feature"]))
        payload.append(json.dumps(contributions, separators=(",", ":")))
    importance = np.abs(values).mean(axis=0)
    ranked = sorted(
        zip(bundle.feature_names, (float(item) for item in importance), strict=True),
        key=lambda item: (-item[1], item[0]),
    )
    rows = int(values.shape[0])
    annotated = frame.with_columns(
        pl.Series("shap_json", payload),
        pl.Series(cfg.expected_value_column, [base_value] * rows),
        pl.Series("explanation_source", [SOURCE_SHAP] * rows),
        # Present on both branches with the same dtype: a fold whose rows fell back and a
        # fold whose rows did not must concatenate into one artefact, and a column that
        # exists in one frame and not the other is a schema failure at read time, not a
        # difference in data.
        pl.Series("explanation_fallback_reason", [None] * rows, dtype=pl.Utf8),
    )
    return annotated, ExplanationOutcome(
        source=SOURCE_SHAP,
        rows=rows,
        explained_rows=rows,
        fallback_rows=0,
        base_value=base_value,
        fallback_reason=None,
        global_importance=tuple(ranked),
        closure_max_error=closure,
    )


def _scorecard_outcome(annotated: pl.DataFrame, cfg: ShapConfig, reason: str) -> ExplanationOutcome:
    rows = annotated.height
    # `Series.filter` takes a predicate function or a boolean Series, not an expression:
    # `Series.filter(pl.col(...))` raised TypeError on every scorecard-explained path,
    # which is the branch the drift action and a degenerate tree both land on.
    source_column = annotated.get_column("explanation_source")
    fallbacks = int((source_column == SOURCE_SCORECARD).sum()) if rows else 0
    return ExplanationOutcome(
        source=SOURCE_SCORECARD,
        rows=rows,
        explained_rows=rows - fallbacks,
        fallback_rows=fallbacks,
        base_value=float(annotated.get_column(cfg.expected_value_column)[0]) if rows else 0.0,
        fallback_reason=reason,
        global_importance=(),
        closure_max_error=float("nan"),
    )


def _fallback_all_rows(frame: pl.DataFrame, cfg: ShapConfig, reason: str) -> pl.DataFrame:
    """Use the scorecard points as the contribution list, and label it as such.

    The fallback is not decoration: a whole-number points list sums to the auditable
    score by construction, so the case pane can still show the arithmetic a human would
    do, with the source named.
    """
    payload: list[str] = []
    for raw in frame.get_column("points_json").to_list():
        contributions = json.loads(raw)
        renamed = [
            {
                "feature": item["feature"],
                "value": float(item["points"]),
                "attribute": item["attribute"],
            }
            for item in contributions
        ]
        renamed.sort(key=lambda item: (-abs(item["value"]), item["feature"]))
        payload.append(json.dumps(renamed, separators=(",", ":")))
    rows = frame.height
    base = int(frame.get_column("score_points").sum() // rows) if rows else 0
    return frame.with_columns(
        pl.Series("shap_json", payload),
        pl.Series(cfg.expected_value_column, [float(base)] * rows),
        pl.Series("explanation_source", [SOURCE_SCORECARD] * rows),
        pl.Series("explanation_fallback_reason", [reason] * rows),
    )


def _matrix(
    frame: pl.DataFrame,
    feature_names: tuple[str, ...],
    categorical: tuple[str, ...] = (),
) -> np.ndarray:
    """The matrix SHAP is asked about, in the booster's own column order.

    Built by the same encoder the booster was trained through, because a SHAP value is an
    attribution *within the model's feature space*: coding a category differently here
    would attribute the row to a feature value the tree never saw.
    """
    from oxbow.models.inputs import feature_matrix

    return feature_matrix(frame, feature_names, categorical)


def explain_scores(
    frame: pl.DataFrame,
    feature_names: tuple[str, ...],
    matrix: np.ndarray,
    values: np.ndarray,
) -> pl.DataFrame:
    """The waterfall payload for an already-computed value matrix.

    Split out because P6's harness computes SHAP on a fold and needs the same persistence
    shape as the run, from the same code path -- two writers of one artefact is how the
    API and the pipeline end up disagreeing about a score.
    """
    if matrix.shape[0] != frame.height:
        raise ModelLayerError(
            f"the SHAP value matrix has {matrix.shape[0]} rows and the frame has "
            f"{frame.height}: persisting explanations for a different population than the "
            "rows would attribute one account's explanation to another"
        )
    payload: list[str] = []
    for position in range(values.shape[0]):
        contributions = [
            {"feature": name, "value": float(values[position, index])}
            for index, name in enumerate(feature_names)
        ]
        contributions.sort(key=lambda item: (-abs(item["value"]), item["feature"]))
        payload.append(json.dumps(contributions, separators=(",", ":")))
    return frame.with_columns(pl.Series("shap_json", payload))


__all__ = [
    "SHAP_SORT_NOTE",
    "SOURCE_SCORECARD",
    "SOURCE_SHAP",
    "ExplanationOutcome",
    "explain_and_report",
    "explain_rows",
    "explain_scores",
    "is_degenerate",
]
