"""Baselines: the two references every number here has to beat.

Plan §10 names them, and they answer different questions:

* **Rules-only severity sum.** "Does the ML earn its complexity?" If the sum of P3b's
  normalised severities ranks as well as the trained models, the trained models are
  ceremony. This is consumed through the injected rules provider: models/ never imports
  rules/, so the baseline is defined by the hits the rules layer actually emits and not
  by a re-implementation that could quietly diverge from it.
* **The scorecard.** The transparent reference. Beating it is the GBM's burden, and
  losing to it is a result worth publishing, because a scorecard a human can audit is
  worth more per unit of ranking than a black box that ranks slightly better -- which is
  exactly why DEV-001 ships both and shows the disagreement between them.

Both baselines are scored on the same rows as the models and enter the same PR-AUC and
bootstrap-CI machinery, so the comparison in the gate report is arithmetic on identical
inputs rather than three numbers from three code paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import polars as pl

from oxbow.models.config import BaselinesConfig
from oxbow.models.errors import ModelLayerError
from oxbow.models.inputs import (
    RULE_COLUMN_SEVERITY_SUM,
)
from oxbow.scoring.frame import COL_LABEL

SCORECARD_PROBABILITY_COLUMN: Final = "p_scorecard"
RULES_ONLY_COLUMN: Final = "baseline_rules_only"


@dataclass(frozen=True, slots=True)
class BaselineScores:
    """The two reference rankings, with the definition each one used."""

    rules_only: np.ndarray
    scorecard: np.ndarray | None
    rules_only_definition: str
    scorecard_definition: str

    def to_dict(self) -> dict[str, object]:
        return {
            "rules_only": {
                "definition": self.rules_only_definition,
                "rows": int(self.rules_only.size),
                "distinct_values": int(np.unique(np.round(self.rules_only, 9)).size),
                "ties": int(self.rules_only.size - int(np.unique(self.rules_only).size)),
            },
            "scorecard": (
                None
                if self.scorecard is None
                else {
                    "definition": self.scorecard_definition,
                    "rows": int(self.scorecard.size),
                }
            ),
        }


def rules_only_severity_sum(frame: pl.DataFrame, cfg: BaselinesConfig) -> np.ndarray:
    """``sum_of_rule_severity`` over the structured hits, as config declares.

    The tie count matters and is reported: a baseline that assigns the same score to
    half the population is a constant, not a ranking, and PR-AUC on it is a statement
    about tie-breaking. Summing the per-rule severities rather than taking the maximum
    keeps accounts rankable within one rule, which plan §9 requires.
    """
    if not cfg.rules_only_enabled:
        raise ModelLayerError(
            "model.yaml baselines.rules_only.enabled is false, but the day-7 gate is defined "
            "as beating the rules-only baseline; the gate cannot be evaluated without it"
        )
    if cfg.rules_only_score != "sum_of_rule_severity":
        raise ModelLayerError(
            f"baselines.rules_only.score={cfg.rules_only_score!r} is not implemented; the "
            "plan defines the baseline as the severity sum"
        )
    if RULE_COLUMN_SEVERITY_SUM in frame.columns:
        return (
            frame.get_column(RULE_COLUMN_SEVERITY_SUM)
            .cast(pl.Float64)
            .fill_null(0.0)
            .to_numpy(zero_copy_only=False)
            .astype(np.float64)
        )
    severity_columns = [
        name for name in frame.columns if name.startswith("rule_r") and name.endswith("severity")
    ]
    if not severity_columns:
        raise ModelLayerError(
            "no per-rule severity columns and no rule_severity_sum on the frame: the "
            "rules-only baseline needs the structured hits P3b emits (plan §9)"
        )
    matrix = np.column_stack(
        [
            frame.get_column(name)
            .cast(pl.Float64)
            .fill_null(0.0)
            .to_numpy(zero_copy_only=False)
            .astype(np.float64)
            for name in sorted(severity_columns)
        ]
    )
    return matrix.sum(axis=1)


def scorecard_reference(frame: pl.DataFrame, cfg: BaselinesConfig) -> np.ndarray | None:
    """The scorecard's own calibrated probability as the transparent ranking.

    ``None`` is returned when calibration was refused, and the caller must not substitute
    the raw score for it: an uncalibrated probability has no meaning as a ranking
    baseline, and pretending otherwise is how a fabricated comparison reaches a report.
    """
    if not cfg.scorecard_only_enabled:
        raise ModelLayerError(
            "model.yaml baselines.scorecard_only.enabled is false; the scorecard is the "
            "declared transparent reference (DEV-001) and the gate compares against it"
        )
    if SCORECARD_PROBABILITY_COLUMN not in frame.columns:
        return None
    column = frame.get_column(SCORECARD_PROBABILITY_COLUMN)
    if column.null_count():
        # A partially-null probability column means some rows were scored without a
        # calibrated figure, which is a build fault rather than a data condition.
        raise ModelLayerError(
            f"{SCORECARD_PROBABILITY_COLUMN} has {column.null_count()} null(s) of "
            f"{column.len()} row(s): the run mixed calibrated and uncalibrated rows, so the "
            "scorecard baseline would rank against a column that is sometimes absent"
        )
    return column.cast(pl.Float64).to_numpy(zero_copy_only=False).astype(np.float64)


def compute_baselines(
    frame: pl.DataFrame, baselines_cfg: BaselinesConfig, calibration_refused: bool
) -> BaselineScores:
    """Both baselines on one frame, in the frame's row order."""
    scorecard = None if calibration_refused else scorecard_reference(frame, baselines_cfg)
    return BaselineScores(
        rules_only=rules_only_severity_sum(frame, baselines_cfg),
        scorecard=scorecard,
        rules_only_definition="sum of per-rule normalised severities (config: sum_of_rule_severity)",
        scorecard_definition=(
            "scorecard calibrated probability p_scorecard"
            if scorecard is not None
            else "unavailable: calibration was refused, so no scorecard probability is published"
        ),
    )


def rules_only_labels_check(frame: pl.DataFrame) -> int:
    """Positives on the slice the baseline is scored on, for the skip-with-reason path."""
    return int(frame.get_column(COL_LABEL).sum())


__all__ = [
    "RULES_ONLY_COLUMN",
    "SCORECARD_PROBABILITY_COLUMN",
    "BaselineScores",
    "compute_baselines",
    "rules_only_labels_check",
    "rules_only_severity_sum",
    "scorecard_reference",
]
