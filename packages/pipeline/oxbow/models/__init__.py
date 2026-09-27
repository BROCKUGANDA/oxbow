"""OXBOW models layer: the ranking stack, its calibration, its explanation, its lineage.

Plan §10 / §11 (phase P4b). The stack is deliberately two models that disagree in public
(DEV-001): an auditable WOE scorecard, built in ``oxbow.scoring`` (P4a), and a LightGBM
ranker, fused by a constrained non-negative logistic meta-learner whose coefficients are
printed rather than typed into config. IsolationForest keeps the product honest about
label scarcity -- real financial crime is under-reported, so a purely supervised model
only learns what a labeller already found.

What this layer promises, and where each promise lives:

* **Nothing is fitted on the corpus it is scored against.** :mod:`oxbow.models.run`
  trains one fold at a time through an injected fold provider and refuses a fit
  population containing the rows it will evaluate. That is a rejection trigger in plan
  §8/§18, so it is a guard with a recorded digest, not a convention.
* **A probability is either calibrated or it says it is not.**
  :mod:`oxbow.models.calibration` refuses below the declared positive floor and the
  output carries ``calibrated: false`` with the reason. The confidence label shown per
  row is the calibration bin's observed rate and sample size, never an adjective,
  because Module C (P5) multiplies these numbers by money.
* **Every score is explainable in one line or is labelled otherwise.** Fusion's
  coefficients print; SHAP TreeExplainer values are persisted per row during the run so
  the API only reads; a degenerate tree falls back to the scorecard's points and labels
  the row ``scorecard-explained``.
* **Every scored row names the model that made it** (02 §B seam 4), through
  :mod:`oxbow.models.registry`, on a local file store so the demo runs with nothing else
  up -- and degrades with a named reason rather than pretending a server was reached.

Boundaries enforced by import-linter, not by discipline: this package may not import
``oxbow.adapters``, any HTTP client, or ``oxbow.rules``. The rules layer arrives through
:data:`oxbow.models.inputs.RuleHitProvider`, injected. Money stays integer minor units
everywhere outside this layer; what leaves it is a float probability and a score.
"""

from __future__ import annotations

from oxbow.models.anomaly import AnomalyBundle, apply_anomaly, fit_anomaly
from oxbow.models.baselines import (
    RULES_ONLY_COLUMN,
    BaselineScores,
    compute_baselines,
    rules_only_severity_sum,
    scorecard_reference,
)
from oxbow.models.calibration import (
    CalibrationOutcome,
    ConfidenceBin,
    PriorCorrection,
    brier_score,
    calibrate,
    choose_method,
    correct_prior,
    expected_calibration_error,
    reliability_curve,
)
from oxbow.models.config import ModelConfig, SplitConfig, load_model_config, load_split_config
from oxbow.models.errors import (
    CalibrationRefusedError,
    DegenerateTreeError,
    FrameContractViolationError,
    FusionInputsError,
    ModelLayerError,
    OptunaBudgetError,
    TrackingUnavailableError,
)
from oxbow.models.evaluate import (
    BootstrapCI,
    MetricSet,
    SeedStability,
    agreement_matrix,
    average_precision,
    bootstrap_ci,
    brier,
    ks_statistic,
    measure,
    precision_at,
    recall_at,
    roc_auc,
    seed_stability,
)
from oxbow.models.explain import (
    SOURCE_SCORECARD,
    SOURCE_SHAP,
    ExplanationOutcome,
    explain_and_report,
    explain_rows,
)
from oxbow.models.folds import (
    SKIP_DEGRADED_RUN,
    SKIP_EMPTY_SLICE,
    SKIP_ZERO_POSITIVES,
    FoldPlan,
    FoldPlanSet,
    fold_frame,
    fold_plan_from_frame,
)
from oxbow.models.folds import (
    pooled_slice as pooled_fold_slice,
)
from oxbow.models.fusion import FusionCoefficient, FusionModel, fit_fusion
from oxbow.models.gbm import GbmBundle, fit_gbm, scale_pos_weight_from_prior
from oxbow.models.inputs import FUSION_INPUTS, RuleHitProvider, attach_rule_hits
from oxbow.models.registry import (
    ModelLineage,
    ModelRegistry,
    TrackedRun,
    TrackingStatus,
    resolve_tracking_uri,
)
from oxbow.models.run import (
    ANOMALY_COLUMN,
    MODE_DEGRADED,
    MODE_FULL,
    MODE_SCORECARD_ONLY,
    P_FUSED_COLUMN,
    P_GBM_COLUMN,
    P_SCORECARD_COLUMN,
    SCORED_ROW_COLUMNS,
    UNCALIBRATED_UI_TEXT,
    AccountProbability,
    DriftGate,
    FoldMetrics,
    FoldModelRunner,
    FoldProvider,
    FoldRun,
    FoldScore,
    GateOutcome,
    LeakageEvidence,
    P4bScorer,
    RoleColumnFoldProvider,
    aggregate_runs,
    assert_no_leakage,
    day7_gate,
    drift_gate,
    ordered_queue,
    pooled_gate,
    seed_stability_over_folds,
    stack_scored_frames,
    write_run_artifacts,
)
from oxbow.models.tuning import TuningResult, TuningTrial, fit_gbm_with_best_params, tune_gbm

__all__ = [
    "ANOMALY_COLUMN",
    "FUSION_INPUTS",
    "MODE_DEGRADED",
    "MODE_FULL",
    "MODE_SCORECARD_ONLY",
    "P_FUSED_COLUMN",
    "P_GBM_COLUMN",
    "P_SCORECARD_COLUMN",
    "RULES_ONLY_COLUMN",
    "SCORED_ROW_COLUMNS",
    "SKIP_DEGRADED_RUN",
    "SKIP_EMPTY_SLICE",
    "SKIP_ZERO_POSITIVES",
    "SOURCE_SCORECARD",
    "SOURCE_SHAP",
    "UNCALIBRATED_UI_TEXT",
    "AccountProbability",
    "AnomalyBundle",
    "BaselineScores",
    "BootstrapCI",
    "CalibrationOutcome",
    "CalibrationRefusedError",
    "ConfidenceBin",
    "DegenerateTreeError",
    "DriftGate",
    "ExplanationOutcome",
    "FoldMetrics",
    "FoldModelRunner",
    "FoldPlan",
    "FoldPlanSet",
    "FoldProvider",
    "FoldRun",
    "FoldScore",
    "FrameContractViolationError",
    "FusionCoefficient",
    "FusionInputsError",
    "FusionModel",
    "GateOutcome",
    "GbmBundle",
    "LeakageEvidence",
    "MetricSet",
    "ModelConfig",
    "ModelLayerError",
    "ModelLineage",
    "ModelRegistry",
    "OptunaBudgetError",
    "P4bScorer",
    "PriorCorrection",
    "RoleColumnFoldProvider",
    "RuleHitProvider",
    "SeedStability",
    "SplitConfig",
    "TrackedRun",
    "TrackingStatus",
    "TrackingUnavailableError",
    "TuningResult",
    "TuningTrial",
    "aggregate_runs",
    "agreement_matrix",
    "apply_anomaly",
    "assert_no_leakage",
    "attach_rule_hits",
    "average_precision",
    "bootstrap_ci",
    "brier",
    "brier_score",
    "calibrate",
    "choose_method",
    "compute_baselines",
    "correct_prior",
    "day7_gate",
    "drift_gate",
    "expected_calibration_error",
    "explain_and_report",
    "explain_rows",
    "fit_anomaly",
    "fit_fusion",
    "fit_gbm",
    "fit_gbm_with_best_params",
    "fold_frame",
    "fold_plan_from_frame",
    "ks_statistic",
    "load_model_config",
    "load_split_config",
    "measure",
    "ordered_queue",
    "pooled_fold_slice",
    "pooled_gate",
    "precision_at",
    "recall_at",
    "reliability_curve",
    "resolve_tracking_uri",
    "roc_auc",
    "rules_only_severity_sum",
    "scale_pos_weight_from_prior",
    "scorecard_reference",
    "seed_stability",
    "seed_stability_over_folds",
    "stack_scored_frames",
    "tune_gbm",
    "write_run_artifacts",
]
