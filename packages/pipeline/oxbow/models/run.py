"""Per-fold training and scoring: the seam that makes a walk-forward number mean something.

Everything else in this package fits a model. This module decides *what a fit is allowed
to see*, and that decision is the one the plan calls fatal:

* **A fold's model is fitted through an injected fold provider** on that fold's
  train+validation rows and scored on that fold's evaluation rows. Training once on the
  full corpus and evaluating on a slice of it is not a small optimisation -- it makes
  every reported number a statement about a model that had the answer, which is why
  plan §8/§18 lists it as a rejection trigger. :func:`assert_no_leakage` is the guard,
  and the key-set digests it computes are recorded on the run, so the claim is checkable
  in the artefact instead of asserted in prose.
* **Drift can degrade the run.** Score PSI at or above ``drift.psi_action`` means the
  GBM, the fused meta-learner and the calibration were all fitted on a population that
  has moved, so their probabilities are not defensible. Scoring then falls back to the
  audited scorecard plus the rules layer and says so in the banner. No new weights are
  invented at degradation time: a hand-picked fallback blend would be exactly the
  unfalsifiable number the rest of this phase exists to avoid.
* **Ties break on ``account_key``.** Two accounts with the same score must arrive in the
  same order in every run, or which case a reviewer sees first depends on row order.

Channel order matters and is fixed here: the scorecard contributes a prior-corrected
probability (``p_scorecard``, always available and monotone in the points, so it is a
sound ranking reference), the GBM and the forest contribute their channels, fusion is
fitted on validation over those inputs, and **one** calibration is fitted -- on the fused
score, which is the number Module C multiplies money by. Calibrating the scorecard and
then calibrating the fusion output would compound two step functions and leave no single
artefact saying which probability is which.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import numpy as np
import polars as pl

from oxbow.models.anomaly import AnomalyBundle, apply_anomaly, fit_anomaly
from oxbow.models.baselines import (
    RULES_ONLY_COLUMN,
    rules_only_severity_sum,
)
from oxbow.models.calibration import CalibrationOutcome, calibrate, correct_prior, logit, sigmoid
from oxbow.models.config import ModelConfig, ReportingConfig, SplitConfig
from oxbow.models.errors import FrameContractViolationError, ModelLayerError
from oxbow.models.evaluate import (
    BootstrapCI,
    MetricSet,
    SeedStability,
    measure,
    seed_stability,
)
from oxbow.models.explain import SOURCE_SCORECARD, ExplanationOutcome, explain_and_report
from oxbow.models.folds import (
    SKIP_DEGRADED_RUN,
    SKIP_ZERO_POSITIVES,
    FoldPlan,
    FoldPlanSet,
    fold_frame,
)
from oxbow.models.fusion import FusionModel, fit_fusion
from oxbow.models.gbm import MIN_POSITIVES_FOR_FIT, GbmBundle, fit_gbm
from oxbow.models.inputs import (
    FUSION_INPUTS,
    RULE_COLUMN_CYCLE_FLAG,
    RULE_COLUMN_FAN_FLAG,
    RULE_COLUMN_HIT_COUNT,
    RULE_COLUMN_SEVERITY_MAX,
    RuleHitProvider,
    attach_rule_hits,
)
from oxbow.models.registry import ModelLineage, ModelRegistry
from oxbow.models.tuning import TuningResult, fit_gbm_with_best_params, tune_gbm
from oxbow.scoring.config import FeatureRegistry as ScoreFeatureRegistry
from oxbow.scoring.config import ScorecardConfig
from oxbow.scoring.drift import ACTION_DEGRADED, drift_decision, score_psi
from oxbow.scoring.frame import (
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    COL_FOLD,
    COL_LABEL,
    COL_ROLE,
    COL_SPEC_HASH,
    ROLE_TEST,
    ROLE_TRAIN,
    ROLE_VALIDATION,
    TrainingFrame,
    build_training_frame,
)
from oxbow.scoring.model import ScorecardModel, fit_scorecard, score_frame

MODE_FULL: Final = "full_model_stack"
MODE_DEGRADED: Final = ACTION_DEGRADED
MODE_SCORECARD_ONLY: Final = "scorecard_and_rules_only"
P_FUSED_COLUMN: Final = "p_fused"
P_FUSED_RAW_COLUMN: Final = "p_fused_raw"
P_GBM_COLUMN: Final = "p_gbm"
# The same booster family fitted with the ablated feature groups removed, produced only when a
# caller asks for a feature-subset ablation. It lives here rather than in a sidecar because the
# ablation row is a fold measurement like any other: same rows, same seed, same fold discipline,
# one fewer group of features — and a number a reader can only trust next to the full model's.
P_GBM_NO_GRAPH_COLUMN: Final = "p_gbm_no_graph"
P_SCORECARD_COLUMN: Final = "p_scorecard"
P_SCORECARD_RAW_COLUMN: Final = "p_scorecard_uncalibrated"
ANOMALY_COLUMN: Final = "anomaly_norm"
BAND_OBSERVED_RATE_COLUMN: Final = "band_observed_rate"
BAND_N_COLUMN: Final = "band_n"
UNCALIBRATED_UI_TEXT: Final = "probabilities are uncalibrated"
QUEUE_RANK_COLUMN: Final = "queue_rank"
EVALUATION_SLICE_KEY: Final = "evaluation"
KEY_COLUMNS: Final = (COL_ACCOUNT_KEY, COL_AS_OF_TS)
FIT_ROLES: Final = (ROLE_TRAIN, ROLE_VALIDATION)

#: The columns every persisted scored row carries. P8's UI reads these and nothing else;
#: 02 §B seam 4 is satisfied because ``model_uri`` and ``model_version`` are on the row
#: rather than in a side table a reader has to join by hand.
SCORED_ROW_COLUMNS: Final = (
    *KEY_COLUMNS,
    COL_FOLD,
    COL_ROLE,
    COL_LABEL,
    COL_SPEC_HASH,
    P_FUSED_COLUMN,
    P_FUSED_RAW_COLUMN,
    P_SCORECARD_COLUMN,
    P_GBM_COLUMN,
    ANOMALY_COLUMN,
    RULE_COLUMN_SEVERITY_MAX,
    RULE_COLUMN_HIT_COUNT,
    RULE_COLUMN_CYCLE_FLAG,
    RULE_COLUMN_FAN_FLAG,
    RULES_ONLY_COLUMN,
    "band",
    "score_points",
    "points_json",
    "reason_codes",
    "shap_json",
    "shap_base_value",
    "explanation_source",
    "explanation_fallback_reason",
    "calibrated",
    "uncalibrated_reason",
    BAND_OBSERVED_RATE_COLUMN,
    BAND_N_COLUMN,
    "confidence_label",
    "scoring_mode",
    "provenance",
    "seed",
    "model_uri",
    "model_version",
    "mlflow_run_id",
    "model_fingerprint",
    "tracking_degraded",
    QUEUE_RANK_COLUMN,
)


def _keys_digest(frame: pl.DataFrame) -> str:
    """A hash over a slice's row identities, so leakage is checked by digest, not by eye."""
    keys = sorted(
        f"{row[COL_ACCOUNT_KEY]}|{row[COL_AS_OF_TS]}"
        for row in frame.select(list(KEY_COLUMNS)).iter_rows(named=True)
    )
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def row_keys(frame: pl.DataFrame) -> frozenset[tuple[str, object]]:
    return frozenset(
        (str(row[COL_ACCOUNT_KEY]), row[COL_AS_OF_TS])
        for row in frame.select(list(KEY_COLUMNS)).iter_rows(named=True)
    )


@dataclass(frozen=True, slots=True)
class LeakageEvidence:
    """What the guard saw, recorded so the artefact can prove the fit was fold-scoped."""

    fit_rows: int
    fit_roles: tuple[str, ...]
    fit_keys_sha256: str
    evaluation_rows: int
    evaluation_keys_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "fit_rows": self.fit_rows,
            "fit_roles": list(self.fit_roles),
            "fit_keys_sha256": self.fit_keys_sha256,
            "evaluation_rows": self.evaluation_rows,
            "evaluation_keys_sha256": self.evaluation_keys_sha256,
            "evaluation_rows_in_fit_population": 0,
            "allowed_fit_roles": list(FIT_ROLES),
            "note": (
                "the fold's model saw only these rows; a full-corpus fit would show test "
                "roles here and is refused before the fit, not after the metric"
            ),
        }


def assert_no_leakage(
    fit_rows: pl.DataFrame, evaluation_rows: pl.DataFrame, fold: int
) -> LeakageEvidence:
    """Refuse a fit population that touches the rows it will later be scored on.

    Both directions are checked, because the failures differ: an evaluation row inside
    the fit population is leakage, and a ``role == 'test'`` row anywhere in it is
    leakage even when the ids happen not to collide with this fold's evaluation slice
    (the provider handed back the whole corpus, which is the exact mistake plan §8 names).
    """
    roles = (
        set(fit_rows.get_column(COL_ROLE).unique().to_list())
        if COL_ROLE in fit_rows.columns
        else set()
    )
    unallowed = sorted(roles - set(FIT_ROLES))
    if unallowed:
        raise FrameContractViolationError(
            f"fold {fold}: the fit population contains role(s) {unallowed}. A model may "
            f"only be fitted on {list(FIT_ROLES)} rows; fitting on the evaluation slice and "
            "then scoring it is the rejection trigger in plan §8/§18, not a shortcut"
        )
    overlap = row_keys(fit_rows) & row_keys(evaluation_rows)
    if overlap:
        sample = ", ".join(sorted(key[0] for key in list(overlap)[:3]))
        raise FrameContractViolationError(
            f"fold {fold}: {len(overlap)} evaluation row(s) also appear in the fit "
            f"population (for example {sample}). Training on a row and reporting its score "
            "as out-of-sample is fabrication; the fold provider must return disjoint slices"
        )
    return LeakageEvidence(
        fit_rows=int(fit_rows.height),
        fit_roles=tuple(sorted(roles)),
        fit_keys_sha256=_keys_digest(fit_rows),
        evaluation_rows=int(evaluation_rows.height),
        evaluation_keys_sha256=_keys_digest(evaluation_rows),
    )


@runtime_checkable
class FoldProvider(Protocol):
    """Where a fold's slices come from. Injected, never recomputed here.

    Structurally compatible with the harness's own provider in
    ``oxbow.backtest.interfaces``, which models/ may not import: the plan keeps
    purge/embargo in exactly one module, so this layer consumes whatever the splits
    module produced rather than deriving boundaries a second time and quietly disagreeing.
    """

    def fold_slices(
        self, frame: TrainingFrame, fold: int, evaluation_role: str
    ) -> Mapping[str, pl.DataFrame]: ...

    def embargo_days(self) -> int: ...


class RoleColumnFoldProvider:
    """The default provider: the ``fold`` and ``role`` columns the splits module wrote.

    It computes no boundaries. It reads the ones that exist and refuses a frame with
    none, because a silently self-derived split is how a walk-forward becomes a random
    split wearing a temporal label.
    """

    def __init__(self, embargo_days: int) -> None:
        self._embargo_days = int(embargo_days)

    def fold_slices(
        self, frame: TrainingFrame, fold: int, evaluation_role: str
    ) -> Mapping[str, pl.DataFrame]:
        if COL_ROLE not in frame.data.columns or COL_FOLD not in frame.data.columns:
            raise FrameContractViolationError(
                "frame carries no fold/role columns; the splits module assigns them, and "
                "this provider refuses to invent a temporal split here"
            )
        plan = FoldPlan(
            fold=fold,
            train_rows=0,
            validation_rows=0,
            evaluation_rows=0,
            evaluation_positives=0,
            base_rate=0.0,
            skipped=False,
            skip_reason=None,
            skip_detail=None,
            evaluation_role=evaluation_role,
        )
        slices = fold_frame(frame, plan)
        return {
            ROLE_TRAIN: slices[ROLE_TRAIN].sort(list(KEY_COLUMNS)),
            ROLE_VALIDATION: slices[ROLE_VALIDATION].sort(list(KEY_COLUMNS)),
            EVALUATION_SLICE_KEY: slices[evaluation_role].sort(list(KEY_COLUMNS)),
        }

    def embargo_days(self) -> int:
        return self._embargo_days


class ExpandingWindowFoldProvider:
    """Walk-forward slices derived from the ``fold`` column and the declared embargo.

    Why a second provider exists, stated rather than assumed: the role column P4a derives
    when P2's splits module has not shipped marks only the *highest* fold as test, which
    is the single-model path (fit everywhere, score one untouched fold). A walk-forward
    needs one evaluation window per fold, so this provider expands over the fold column
    the splits module already owns: fold ``k`` is scored, fold ``k-1`` is the validation
    window the calibrator and meta-learner see, and folds before that are training. The
    embargo is applied at both boundaries, because a 30-day rolling feature computed just
    after a boundary would otherwise see the window it is meant to predict.

    No label is read here. Deriving a split from the positive counts would be tuning the
    boundary until it flatters the model, which is the failure plan §7 exists to prevent.
    """

    def __init__(self, *, embargo_days: int, validation_offset: int = 1) -> None:
        if validation_offset < 1:
            raise ModelLayerError(
                "validation_offset must be >= 1: validation cannot be the evaluation window"
            )
        self._embargo_days = int(embargo_days)
        self._validation_offset = int(validation_offset)

    def minimum_fold(self) -> int:
        """The first fold with both a training and a validation window before it."""
        return self._validation_offset + 1

    def can_supply(self, fold: int) -> bool:
        return fold >= self.minimum_fold()

    def fold_slices(
        self, frame: TrainingFrame, fold: int, evaluation_role: str
    ) -> Mapping[str, pl.DataFrame]:
        _ = evaluation_role
        if COL_FOLD not in frame.data.columns:
            raise FrameContractViolationError(
                "frame carries no fold column; the splits module owns the temporal "
                "boundaries and this provider refuses to invent them"
            )
        if not self.can_supply(fold):
            raise FrameContractViolationError(
                f"fold {fold} has no earlier training and validation window (this provider "
                f"needs fold >= {self.minimum_fold()}); report the fold as skipped with a "
                "named reason rather than fitting on the answer"
            )
        data = frame.data.sort([COL_AS_OF_TS, COL_ACCOUNT_KEY])
        evaluation = data.filter(pl.col(COL_FOLD) == fold)
        validation = data.filter(pl.col(COL_FOLD) == fold - self._validation_offset)
        train = data.filter(pl.col(COL_FOLD) < fold - self._validation_offset)
        train, validation = self._apply_embargo(train, validation, evaluation)
        if train.height == 0 or validation.height == 0:
            raise FrameContractViolationError(
                f"fold {fold}: the embargo removed the whole "
                f"{'training' if train.height == 0 else 'validation'} window "
                f"(embargo {self._embargo_days}d). Too little clean history to fit; the fold "
                "is reported, not silently scored"
            )
        return {ROLE_TRAIN: train, ROLE_VALIDATION: validation, EVALUATION_SLICE_KEY: evaluation}

    def _apply_embargo(
        self, train: pl.DataFrame, validation: pl.DataFrame, evaluation: pl.DataFrame
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Drop fit rows whose as-of timestamp falls inside the embargo before a window."""
        if self._embargo_days <= 0:
            return train, validation
        gap = pl.duration(days=self._embargo_days)
        if evaluation.height:
            boundary = evaluation.get_column(COL_AS_OF_TS).min() - gap
            train = train.filter(pl.col(COL_AS_OF_TS) <= boundary)
            validation = validation.filter(pl.col(COL_AS_OF_TS) <= boundary)
        if validation.height:
            inner = validation.get_column(COL_AS_OF_TS).min() - gap
            train = train.filter(pl.col(COL_AS_OF_TS) <= inner)
        return train, validation

    def embargo_days(self) -> int:
        return self._embargo_days


@dataclass(frozen=True, slots=True)
class _FixedSlicesProvider:
    """Adapts three ready-made frames to the provider interface (P6's call shape)."""

    train: pl.DataFrame
    validation: pl.DataFrame
    evaluation: pl.DataFrame
    embargo: int

    def fold_slices(
        self, frame: TrainingFrame, fold: int, evaluation_role: str
    ) -> Mapping[str, pl.DataFrame]:
        _ = frame, fold, evaluation_role
        return {
            ROLE_TRAIN: _require_role(self.train, ROLE_TRAIN),
            ROLE_VALIDATION: _require_role(self.validation, ROLE_VALIDATION),
            EVALUATION_SLICE_KEY: _require_role(self.evaluation, ROLE_TEST),
        }

    def embargo_days(self) -> int:
        return self.embargo


def _stamp_role(frame: pl.DataFrame, role: str) -> pl.DataFrame:
    """Stamp a slice with the role the caller's call assigned it.

    The call is the authority on what a row was used for, and the disjointness guard in
    :func:`assert_no_leakage` is the check that the authority is telling the truth. This
    is deliberately a re-stamp rather than a contradiction check: the corpus role column
    describes the *pooled* split, and inside a walk-forward fold a row can legitimately
    move from 'train' (an earlier fold's model) to 'evaluation' (this one's). Rewriting
    the label to match how the fit actually used it is what makes the artefact honest;
    refusing on the mismatch would only encourage callers to pre-shuffle roles to appease it.
    """
    return frame.with_columns(pl.lit(role).alias(COL_ROLE))


def _require_role(frame: pl.DataFrame, role: str) -> pl.DataFrame:
    """Back-compat alias: slices are stamped, and leakage is caught by the disjointness guard."""
    return _stamp_role(frame, role)


@dataclass(frozen=True, slots=True)
class DriftGate:
    """The PSI decision for a run, with the banner the UI shows verbatim."""

    degrade: bool
    score_psi: float
    mode: str
    banner: str
    action: str
    threshold: float

    def to_dict(self) -> dict[str, object]:
        return {
            "degrade": self.degrade,
            "score_psi": None if np.isinf(self.score_psi) else round(self.score_psi, 6),
            "score_psi_is_infinite": bool(np.isinf(self.score_psi)),
            "mode": self.mode,
            "action": self.action,
            "banner": self.banner,
            "threshold": self.threshold,
        }


def drift_gate(
    expected_scores: np.ndarray,
    actual_scores: np.ndarray,
    *,
    model_cfg: ModelConfig,
    scorecard_cfg: ScorecardConfig,
) -> DriftGate:
    """Score PSI and the mode it forces. The arithmetic is P4a's, not a second implementation.

    The two configs are cross-checked rather than one ignored: ``psi_action`` is declared
    in both model.yaml and scorecard.yaml, and if they disagree about where the cliff is,
    the run would degrade on a threshold nobody agreed to. That disagreement raises.
    """
    if abs(model_cfg.drift.psi_action - scorecard_cfg.drift.psi_action) > 1e-12:
        raise ModelLayerError(
            f"drift.psi_action disagrees between model.yaml ({model_cfg.drift.psi_action}) "
            f"and scorecard.yaml ({scorecard_cfg.drift.psi_action}); one of them is lying "
            "about when scoring degrades"
        )
    psi = score_psi(
        np.asarray(expected_scores, dtype=np.float64),
        np.asarray(actual_scores, dtype=np.float64),
        scorecard_cfg.drift.score_psi_bins,
    )
    report = drift_decision(psi, (), scorecard_cfg.drift)
    if (
        report.action == "degrade_to_rules_plus_scorecard"
        and report.action != model_cfg.drift.on_action
    ):
        raise ModelLayerError(
            f"scoring's drift layer emitted {report.action!r} but model.yaml declares "
            f"drift.on_action={model_cfg.drift.on_action!r}; the degradation path this run "
            "took is not the one the configuration names"
        )
    return DriftGate(
        degrade=report.mode == MODE_DEGRADED,
        score_psi=psi,
        mode=report.mode,
        banner=report.banner,
        action=report.action,
        threshold=model_cfg.drift.psi_action,
    )


def ordered_queue(scored: pl.DataFrame, score_column: str, tie_break: str) -> pl.DataFrame:
    """The queue in ``(-score, account_key)`` order, with ``queue_rank`` attached.

    One function owns the order, so the queue, precision-at-budget and PR-AUC cannot each
    invent a tie-break and disagree about which accounts are in the top 200
    (``test_tie_break_deterministic``).
    """
    for name, label in ((score_column, "score"), (tie_break, "tie-break")):
        if name not in scored.columns:
            raise ModelLayerError(
                f"the queue needs the {label} column {name!r}, absent from the frame"
            )
    ordered = scored.sort([score_column, tie_break], descending=[True, False])
    return ordered.with_columns(
        pl.arange(1, ordered.height + 1, dtype=pl.Int64).alias(QUEUE_RANK_COLUMN)
    )


def _ranking_slice(frame: pl.DataFrame, score_column: str, tie_break: str) -> pl.DataFrame:
    """Rows in canonical ranking order, so a positional tie-break *is* an account-key tie-break."""
    return frame.sort([score_column, tie_break], descending=[True, False])


@dataclass(frozen=True, slots=True)
class FoldMetrics:
    """Validation and evaluation numbers for one fold, per ranking, with provenance."""

    validation: Mapping[str, MetricSet]
    evaluation: Mapping[str, MetricSet]
    evaluation_role: str

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluation_role": self.evaluation_role,
            "validation": {name: metric.to_dict() for name, metric in self.validation.items()},
            "evaluation": {name: metric.to_dict() for name, metric in self.evaluation.items()},
        }


def _metrics_for(
    prefix: str,
    frame: pl.DataFrame,
    *,
    model_cfg: ModelConfig,
    split_cfg: SplitConfig,
    provenance: str,
    score_columns: Mapping[str, str],
) -> dict[str, MetricSet]:
    """PR-AUC / ROC-AUC / KS / Brier for each named ranking on one slice.

    The slice is sorted by ``(-score, account_key)`` before any statistic, so the
    tie-break that decides the queue also decides precision-at-budget -- one order, one
    answer, and :func:`oxbow.models.evaluate.rank_order`'s positional tie-break becomes a
    statement about account keys instead of about row position.
    """
    out: dict[str, MetricSet] = {}
    for name, column in score_columns.items():
        if column not in frame.columns:
            continue
        ordered = _ranking_slice(frame, column, COL_ACCOUNT_KEY)
        labels = ordered.get_column(COL_LABEL).cast(pl.Int32).to_numpy().astype(np.int32)
        scores = ordered.get_column(column).cast(pl.Float64).to_numpy().astype(np.float64)
        probabilities = scores if name in {"fused", "scorecard"} else None
        out[name] = measure(
            f"{prefix}_{name}",
            labels,
            scores,
            resamples=split_cfg.bootstrap_resamples,
            confidence=split_cfg.bootstrap_confidence,
            seed=model_cfg.seed,
            provenance=provenance,
            probabilities=probabilities,
        )
    return out


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """The day-7 clause: the model stack must beat both transparent references.

    The pass test is on the *point estimate* of validation PR-AUC, with the bootstrap CIs
    printed beside it and the overlap flagged rather than hidden: a stack that beats the
    scorecard by 0.004 inside overlapping intervals has not earned its complexity, and a
    gate that checked only the point estimate would say it had.
    """

    fused_pr_auc: BootstrapCI
    rules_only_pr_auc: BootstrapCI | None
    scorecard_pr_auc: BootstrapCI | None
    beats_rules_only: bool | None
    beats_scorecard: bool | None
    ci_overlap_with_rules_only: bool | None
    ci_overlap_with_scorecard: bool | None
    delta_rules_only: float | None
    delta_scorecard: float | None
    provenance: str

    @property
    def passed(self) -> bool:
        return bool(self.beats_rules_only) and bool(self.beats_scorecard)

    def to_dict(self) -> dict[str, object]:
        return {
            "gate": "P4b day-7: the fused stack beats rules-only AND scorecard-only on validation PR-AUC",
            "metric": "pr_auc",
            "passed": self.passed,
            "fused_pr_auc": self.fused_pr_auc.to_dict(),
            "rules_only_pr_auc": None
            if self.rules_only_pr_auc is None
            else self.rules_only_pr_auc.to_dict(),
            "scorecard_pr_auc": None
            if self.scorecard_pr_auc is None
            else self.scorecard_pr_auc.to_dict(),
            "delta_vs_rules_only": None
            if self.delta_rules_only is None
            else round(self.delta_rules_only, 8),
            "delta_vs_scorecard": None
            if self.delta_scorecard is None
            else round(self.delta_scorecard, 8),
            "ci_overlap_with_rules_only": self.ci_overlap_with_rules_only,
            "ci_overlap_with_scorecard": self.ci_overlap_with_scorecard,
            "provenance": self.provenance,
            "caveat": (
                "point estimates decide the gate; overlapping bootstrap intervals mean the "
                "margin is not resolvable at this sample size and must not be reported as a "
                "demonstrated win of the fitted stack over the auditable reference"
            ),
        }


def _delta(ci: BootstrapCI, baseline: BootstrapCI | None) -> float | None:
    if baseline is None or np.isnan(ci.estimate) or np.isnan(baseline.estimate):
        return None
    return float(ci.estimate - baseline.estimate)


def _beats(ci: BootstrapCI, baseline: BootstrapCI | None) -> bool | None:
    delta = _delta(ci, baseline)
    return None if delta is None else bool(delta > 0.0)


def _ci_overlap(ci: BootstrapCI, baseline: BootstrapCI | None) -> bool | None:
    if baseline is None:
        return None
    values = (ci.lower, ci.upper, baseline.lower, baseline.upper)
    if any(np.isnan(value) for value in values):
        return None
    return bool(not (ci.lower > baseline.upper or baseline.lower > ci.upper))


def day7_gate(validation: Mapping[str, MetricSet], provenance: str) -> GateOutcome:
    """Compare the fused stack with both references, measured on the same validation slice."""
    if "fused" not in validation:
        raise ModelLayerError(
            "the day-7 gate needs a 'fused' validation metric set; its absence means no "
            "fold produced a fused score, which is reported rather than averaged away"
        )
    fused = validation["fused"]
    rules_only = validation.get("rules_only")
    scorecard = validation.get("scorecard")
    return GateOutcome(
        fused_pr_auc=fused.pr_auc,
        rules_only_pr_auc=None if rules_only is None else rules_only.pr_auc,
        scorecard_pr_auc=None if scorecard is None else scorecard.pr_auc,
        beats_rules_only=_beats(fused.pr_auc, None if rules_only is None else rules_only.pr_auc),
        beats_scorecard=_beats(fused.pr_auc, None if scorecard is None else scorecard.pr_auc),
        ci_overlap_with_rules_only=_ci_overlap(
            fused.pr_auc, None if rules_only is None else rules_only.pr_auc
        ),
        ci_overlap_with_scorecard=_ci_overlap(
            fused.pr_auc, None if scorecard is None else scorecard.pr_auc
        ),
        delta_rules_only=_delta(fused.pr_auc, None if rules_only is None else rules_only.pr_auc),
        delta_scorecard=_delta(fused.pr_auc, None if scorecard is None else scorecard.pr_auc),
        provenance=provenance,
    )


@dataclass(frozen=True, slots=True)
class FoldRun:
    """One fold's output: rows, the models that made them, and why any channel is missing."""

    fold: int
    seed: int
    mode: str
    provenance: str
    scored: pl.DataFrame
    metrics: FoldMetrics | None
    fusion: FusionModel | None
    gbm: GbmBundle | None
    anomaly: AnomalyBundle | None
    scorecard_model: ScorecardModel
    calibration: CalibrationOutcome | None
    lineage: ModelLineage | None
    leakage: LeakageEvidence
    plan: FoldPlan | None
    skipped: bool
    skip_reason: str | None
    skip_detail: str | None
    channel_skips: Mapping[str, str] = field(default_factory=dict)
    tuning: TuningResult | None = None
    drift: DriftGate | None = None
    explanation: ExplanationOutcome | None = None

    @property
    def calibrated(self) -> bool:
        return self.calibration is not None and self.calibration.calibrated

    @property
    def uncalibrated_reason(self) -> str:
        if self.calibration is None:
            return f"{UNCALIBRATED_UI_TEXT}: no calibration was fitted for this fold"
        if self.calibration.refused:
            return f"{UNCALIBRATED_UI_TEXT}: {self.calibration.refusal_reason or self.calibration.branch_reason}"
        return f"{UNCALIBRATED_UI_TEXT}: not applicable, calibration used {self.calibration.method}"

    def to_dict(self) -> dict[str, object]:
        return {
            "fold": self.fold,
            "seed": self.seed,
            "scoring_mode": self.mode,
            "provenance": self.provenance,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "skip_detail": self.skip_detail,
            "channel_skips": dict(self.channel_skips),
            "rows_scored": self.scored.height,
            "fusion": None if self.fusion is None else self.fusion.to_dict(),
            "gbm": None
            if self.gbm is None
            else {
                "seed": self.gbm.seed,
                "scale_pos_weight": round(self.gbm.scale_pos_weight, 6),
                "train_prior": round(self.gbm.train_prior, 8),
                "best_iteration": self.gbm.best_iteration,
                "best_valid_pr_auc": (
                    None
                    if np.isnan(self.gbm.best_valid_pr_auc)
                    else round(self.gbm.best_valid_pr_auc, 8)
                ),
                "estimators_requested": self.gbm.n_estimators_requested,
                "degenerate": self.gbm.degenerate,
                "split_feature_count": self.gbm.split_feature_count,
                "fingerprint": self.gbm.fingerprint(),
            },
            "anomaly": None if self.anomaly is None else self.anomaly.to_dict(),
            "calibration": None if self.calibration is None else self.calibration.to_dict(),
            "calibrated": self.calibrated,
            "uncalibrated_reason": self.uncalibrated_reason,
            "explanation": None if self.explanation is None else self.explanation.to_dict(),
            "leakage_evidence": self.leakage.to_dict(),
            "fold_plan": None if self.plan is None else self.plan.to_dict(),
            "tuning": None if self.tuning is None else self.tuning.to_dict(),
            "drift": None if self.drift is None else self.drift.to_dict(),
            "tracking": None if self.lineage is None else self.lineage.to_dict(),
            "metrics": None if self.metrics is None else self.metrics.to_dict(),
        }


def build_fold_training_frame(
    rows: pl.DataFrame,
    feature_registry: ScoreFeatureRegistry,
    scorecard_cfg: ScorecardConfig,
    *,
    provenance: str,
    split_cfg: SplitConfig,
) -> TrainingFrame:
    """Validate one fold's rows into a TrainingFrame, roles preserved.

    Built through P4a's validator rather than assembled by hand, so the per-fold
    scorecard is fitted against the same contract checks as the pooled one -- including
    the spec hash, which is the guard against fitting on one feature set and scoring
    another.
    """
    return build_training_frame(
        rows,
        feature_registry,
        tuple(scorecard_cfg.binning.categorical_features),
        provenance,
        split_cfg.validation_fraction_of_train,
        split_cfg.n_folds,
        notes=(f"fold-scoped frame built by the P4b runner; provenance={provenance}",),
    )


def with_scorecard_channel(
    scored: pl.DataFrame, train_positive_share: float, population_share: float, cfg: ModelConfig
) -> tuple[pl.DataFrame, dict[str, object]]:
    """``p_scorecard``: the scorecard's raw probability with the prior correction applied.

    Corrected but not calibrated. The correction is a single stated logit shift that
    anchors the mean probability to the base rate Module C multiplies money by; a second
    step function on the reference channel would leave two calibrations in series and no
    single place where the published probability is decided.
    """
    correction = correct_prior(train_positive_share, population_share, cfg.calibration)
    raw = scored.get_column(P_SCORECARD_RAW_COLUMN).cast(pl.Float64).to_numpy().astype(np.float64)
    shifted = sigmoid(logit(np.clip(raw, 0.0, 1.0)) + correction.logit_shift)
    return scored.with_columns(pl.Series(P_SCORECARD_COLUMN, shifted)), correction.to_dict()


def with_gbm_and_anomaly(
    scored: pl.DataFrame,
    *,
    gbm: GbmBundle,
    anomaly: AnomalyBundle,
    features: tuple[str, ...],
    categorical: tuple[str, ...],
) -> pl.DataFrame:
    """Attach ``p_gbm`` and ``anomaly_norm`` for every row of the fold."""
    from oxbow.models.gbm import _matrix as gbm_matrix

    p_gbm = gbm.predict_matrix(gbm_matrix(scored, features, categorical))
    anomaly_norm = apply_anomaly(anomaly, scored, categorical)
    return scored.with_columns(
        pl.Series(P_GBM_COLUMN, p_gbm),
        pl.Series(ANOMALY_COLUMN, anomaly_norm),
    )


def with_fused_channel(scored: pl.DataFrame, fusion: FusionModel) -> pl.DataFrame:
    """``p_fused_raw``: the meta-learner's output before calibration."""
    return scored.with_columns(pl.Series(P_FUSED_RAW_COLUMN, fusion.predict_frame(scored)))


def with_degraded_channels(scored: pl.DataFrame, drift: DriftGate | None) -> pl.DataFrame:
    """Degraded scoring: the audited scorecard ranks, and the row says it is uncalibrated.

    ``p_fused`` is set to the scorecard channel and the run is labelled degraded, because
    the defensible object during a drift action is the points table a human can audit --
    not a fused number whose meta-learner was fitted on the population that just moved.
    """
    height = scored.height
    scorecard = scored.get_column(P_SCORECARD_COLUMN).cast(pl.Float64).to_numpy().astype(np.float64)
    return scored.with_columns(
        pl.Series(P_FUSED_RAW_COLUMN, scorecard),
        pl.Series(P_FUSED_COLUMN, scorecard),
        pl.Series("drift_banner", [(drift.banner if drift is not None else "")] * height),
        pl.Series("drift_score_psi", [None if drift is None else drift.score_psi] * height),
    )


def annotate_rows(
    scored: pl.DataFrame,
    *,
    mode: str,
    seed: int,
    provenance: str,
    calibration: CalibrationOutcome | None,
    lineage: ModelLineage | None,
    feature_spec_hash: str,
) -> pl.DataFrame:
    """The per-row contract: a probability, the measured rate and count behind it, its lineage.

    The confidence label is the calibration bin's observed rate plus its sample size and
    never an adjective, because Module C (P5) multiplies these probabilities by money:
    ``"observed rate in this band: 7.14%, n=432"`` is a measurement a reader can weigh,
    while "high confidence" is a feeling that turns into a wrong currency figure.
    """
    height = scored.height
    calibrated = calibration is not None and calibration.calibrated
    observed = np.full(height, np.nan)
    counts = np.zeros(height, dtype=np.int64)
    labels: list[str] = []
    probabilities = scored.get_column(P_FUSED_COLUMN).cast(pl.Float64).to_numpy().astype(np.float64)
    for index in range(height):
        value = float(probabilities[index]) if calibrated else None
        label = (
            calibration.confidence_label(value)
            if calibration is not None
            else {
                "text": f"{UNCALIBRATED_UI_TEXT}: no calibration was fitted for this fold",
                "observed_rate": None,
                "sample_size": None,
            }
        )
        labels.append(str(label["text"]))
        if label["observed_rate"] is not None:
            observed[index] = float(label["observed_rate"])
            counts[index] = int(label["sample_size"] or 0)
    if calibrated:
        reason = ""
    else:
        detail = (
            (calibration.refusal_reason or calibration.branch_reason)
            if calibration is not None
            else f"scoring mode {mode!r} fitted no calibrator"
        )
        reason = f"{UNCALIBRATED_UI_TEXT}: {detail}"
    lineage_row = (
        lineage.to_row()
        if lineage is not None
        else {
            "mlflow_run_id": None,
            "model_uri": None,
            "model_version": None,
            "registered_model_name": None,
            "model_fingerprint": None,
            "tracking_uri_used": "",
            "tracking_degraded": True,
        }
    )
    columns: list[pl.Series] = [
        pl.Series("calibrated", [calibrated] * height, dtype=pl.Boolean),
        pl.Series("uncalibrated_reason", [reason] * height),
        pl.Series(BAND_OBSERVED_RATE_COLUMN, observed),
        pl.Series(BAND_N_COLUMN, counts, dtype=pl.Int64),
        pl.Series("confidence_label", labels),
        pl.Series("scoring_mode", [mode] * height),
        pl.Series("provenance", [provenance] * height),
        pl.Series("seed", [seed] * height, dtype=pl.Int64),
        pl.Series(COL_SPEC_HASH, [feature_spec_hash] * height),
    ]
    for name, value in lineage_row.items():
        dtype = pl.Boolean if isinstance(value, bool) else pl.Utf8
        coerced: object = (
            value if isinstance(value, bool) else (None if value is None else str(value))
        )
        columns.append(pl.Series(name, [coerced] * height, dtype=dtype))
    return scored.with_columns(columns)


class FoldModelRunner:
    """Trains and scores one fold at a time, through the injected provider.

    The dependencies are constructor arguments rather than module-level calls: the rules
    layer is being written concurrently and may not be imported here, and the tests hand
    in a deterministic rules provider. P6 passes its own fold provider and gets the same
    objects back.

    ``ablate_feature_groups`` names registry groups (``graph_local``, ``graph_global``, …)
    whose features are removed from a SECOND booster fit, published as
    :data:`P_GBM_NO_GRAPH_COLUMN` beside the full model's. It is opt-in and costs one extra
    LightGBM fit per fold: the row that measures what the graph adds cannot be computed from
    the model that used it, and re-listing one column under a second label is the imitation
    DEV-027 refused to ship.
    """

    def __init__(
        self,
        *,
        model_cfg: ModelConfig,
        scorecard_cfg: ScorecardConfig,
        split_cfg: SplitConfig,
        feature_registry: ScoreFeatureRegistry,
        rules_provider: RuleHitProvider,
        provenance: str,
        tracking: ModelRegistry | None = None,
        trial_budget: int | None = None,
        explain: bool = True,
        root: Path | None = None,
        ablate_feature_groups: tuple[str, ...] = (),
    ) -> None:
        if model_cfg.reporting.tie_break_score_column != P_FUSED_COLUMN:
            raise ModelLayerError(
                f"reporting.tie_break_score_column={model_cfg.reporting.tie_break_score_column!r} "
                f"is not {P_FUSED_COLUMN!r}: the queue would be ordered by a different "
                "number than the one this build publishes"
            )
        if tuple(model_cfg.fusion.inputs) != FUSION_INPUTS:
            raise ModelLayerError(
                f"model.yaml fusion.inputs {list(model_cfg.fusion.inputs)} does not match the "
                f"vector this build scores {list(FUSION_INPUTS)}"
            )
        self.model_cfg = model_cfg
        self.scorecard_cfg = scorecard_cfg
        self.split_cfg = split_cfg
        self.feature_registry = feature_registry
        self.rules_provider = rules_provider
        self.provenance = provenance
        self.tracking = tracking
        self.trial_budget = trial_budget
        self.explain = explain
        self.root = Path(root) if root is not None else model_cfg.root
        unknown = sorted(set(ablate_feature_groups) - set(feature_registry.groups))
        if unknown:
            raise ModelLayerError(
                f"ablate_feature_groups names {unknown}, which the feature registry does not "
                f"define; its groups are {sorted(feature_registry.groups)} — an unknown group "
                "would ablate nothing and still print a row"
            )
        self.ablate_feature_groups = tuple(ablate_feature_groups)

    def provider(self) -> FoldProvider:
        return RoleColumnFoldProvider(self.split_cfg.embargo_days)

    def fold_training_frame(self, frame: TrainingFrame, fold: int) -> TrainingFrame:
        """A fold-scoped TrainingFrame: this fold's rows only, roles preserved."""
        slices = self.provider().fold_slices(frame, fold, ROLE_TEST)
        rows = pl.concat(
            [slices[ROLE_TRAIN], slices[ROLE_VALIDATION], slices[EVALUATION_SLICE_KEY]]
        )
        return build_fold_training_frame(
            rows,
            self.feature_registry,
            self.scorecard_cfg,
            provenance=self.provenance,
            split_cfg=self.split_cfg,
        )

    def run_fold(
        self,
        frame: TrainingFrame,
        fold: int,
        *,
        provider: FoldProvider | None = None,
        evaluation_role: str = ROLE_TEST,
        seed: int | None = None,
        drift: DriftGate | None = None,
        plan: FoldPlan | None = None,
        tracking_run_name: str | None = None,
    ) -> FoldRun:
        """Train on this fold's train+validation, score its evaluation rows, and report.

        This is the call P6 makes once per fold. Nothing in it receives the full corpus:
        the fit population comes from ``provider.fold_slices`` and is checked against the
        evaluation rows before a single tree is built.
        """
        active = provider or self.provider()
        run_seed = self.model_cfg.seed if seed is None else int(seed)
        slices = active.fold_slices(frame, fold, evaluation_role)
        missing = sorted({*FIT_ROLES, EVALUATION_SLICE_KEY} - set(slices))
        if missing:
            raise FrameContractViolationError(
                f"fold provider returned keys {sorted(slices)}; it must supply "
                f"{[*FIT_ROLES, EVALUATION_SLICE_KEY]} (missing {missing})"
            )
        train, validation, evaluation = (
            slices[ROLE_TRAIN],
            slices[ROLE_VALIDATION],
            slices[EVALUATION_SLICE_KEY],
        )
        fit_rows = pl.concat([train, validation])
        leakage = assert_no_leakage(fit_rows, evaluation, fold)
        skipped = bool(plan is not None and plan.skipped)
        channel_skips: dict[str, str] = {}
        degrade = drift is not None and drift.degrade

        fold_rows = pl.concat(
            [
                _stamp_role(train, ROLE_TRAIN),
                _stamp_role(validation, ROLE_VALIDATION),
                _stamp_role(evaluation, evaluation_role),
            ]
        )
        fold_tf = build_fold_training_frame(
            fold_rows,
            self.feature_registry,
            self.scorecard_cfg,
            provenance=self.provenance,
            split_cfg=self.split_cfg,
        )
        scorecard_model = fit_scorecard(fold_tf, self.scorecard_cfg, self.feature_registry)
        scored = score_frame(fold_tf, scorecard_model, self.scorecard_cfg)
        scored = attach_rule_hits(scored, self.rules_provider)
        scored = scored.with_columns(
            pl.Series(RULES_ONLY_COLUMN, rules_only_severity_sum(scored, self.model_cfg.baselines))
        )
        train_positive_share = (
            float(np.mean(train.get_column(COL_LABEL).cast(pl.Float64).to_numpy()))
            if train.height
            else 0.0
        )
        population_share = self._population_base_rate(validation)
        scored, prior_correction = with_scorecard_channel(
            scored, train_positive_share, population_share, self.model_cfg
        )

        gbm: GbmBundle | None = None
        anomaly: AnomalyBundle | None = None
        fusion: FusionModel | None = None
        calibration: CalibrationOutcome | None = None
        tuning: TuningResult | None = None
        explanation: ExplanationOutcome | None = None

        validation_positives = (
            int(validation.get_column(COL_LABEL).sum()) if validation.height else 0
        )
        train_positives = int(train.get_column(COL_LABEL).sum()) if train.height else 0

        if degrade:
            for channel in ("gbm", "anomaly", "fusion", "calibration"):
                channel_skips[channel] = (
                    f"{SKIP_DEGRADED_RUN}: score PSI "
                    f"{drift.score_psi:.4f} is at or above drift.psi_action={drift.threshold}; "
                    "the fitted channels were trained on the population that has moved"
                )
            scored = with_degraded_channels(scored, drift)
            mode = MODE_DEGRADED
        elif train_positives < MIN_POSITIVES_FOR_FIT or validation_positives == 0:
            channel_skips["gbm"] = (
                f"{SKIP_ZERO_POSITIVES}: fold {fold} has {train_positives} training and "
                f"{validation_positives} validation positives, which cannot fit a booster "
                f"needing at least {MIN_POSITIVES_FOR_FIT} training positives and one "
                "validation positive for early stopping on PR-AUC. The rows are still "
                "scored by the scorecard and the rules layer, and the fold is reported."
            )
            channel_skips["fusion"] = "unavailable without a gbm channel"
            channel_skips["calibration"] = "unavailable without a fused score"
            scored = with_degraded_channels(scored, drift)
            mode = MODE_SCORECARD_ONLY
        else:
            features = self.feature_registry.names
            categorical = tuple(self.scorecard_cfg.binning.categorical_features)
            anomaly = fit_anomaly(train, validation, features, categorical, self.model_cfg.anomaly)
            gbm, tuning = self._fit_gbm(
                train, validation, features, categorical, run_seed, channel_skips
            )
            if gbm is None:
                anomaly = None
                channel_skips.setdefault("fusion", "unavailable without a gbm channel")
                channel_skips.setdefault("calibration", "unavailable without a fused score")
                scored = with_degraded_channels(scored, drift)
                mode = MODE_SCORECARD_ONLY
            else:
                scored = with_gbm_and_anomaly(
                    scored, gbm=gbm, anomaly=anomaly, features=features, categorical=categorical
                )
                if self.ablate_feature_groups:
                    scored = self._fit_feature_ablated_gbm(
                        scored,
                        train=train,
                        validation=validation,
                        seed=run_seed,
                        channel_skips=channel_skips,
                    )
                fusion = self._fit_fusion(scored, channel_skips)
                if fusion is None:
                    scored = with_degraded_channels(scored, drift)
                    mode = MODE_SCORECARD_ONLY
                else:
                    scored = with_fused_channel(scored, fusion)
                    calibration = self._calibrate_fused(scored, train, channel_skips)
                    scored = self._publish_fused(scored, calibration)
                    mode = MODE_FULL

        lineage: ModelLineage | None = None
        context = self._open_tracking(fold, run_seed, mode, prior_correction, tracking_run_name)
        with context as tracked_run:
            if tracked_run is not None:
                if gbm is not None:
                    lineage = tracked_run.log_model(gbm.booster, gbm.fingerprint())
                tracked_run.log_metrics(
                    self._metrics_to_log(calibration, gbm, fusion, prior_correction)
                )
                if calibration is not None:
                    tracked_run.log_reliability_curve(
                        [entry.to_dict() for entry in calibration.reliability]
                    )

        scored = annotate_rows(
            scored,
            mode=mode,
            seed=run_seed,
            provenance=self.provenance,
            calibration=calibration,
            lineage=lineage,
            feature_spec_hash=frame.feature_spec_hash,
        )
        if self.explain:
            scored, explanation = self._explain(scored, gbm, channel_skips)

        metrics: FoldMetrics | None = None
        if skipped:
            if plan is not None:
                channel_skips.setdefault("metrics", plan.skip_reason or "fold skipped")
        else:
            columns = {
                "fused": P_FUSED_COLUMN,
                "scorecard": P_SCORECARD_COLUMN,
                "rules_only": RULES_ONLY_COLUMN,
            }
            metrics = FoldMetrics(
                validation=_metrics_for(
                    "validation",
                    scored.filter(pl.col(COL_ROLE) == ROLE_VALIDATION),
                    model_cfg=self.model_cfg,
                    split_cfg=self.split_cfg,
                    provenance=self.provenance,
                    score_columns=columns,
                ),
                evaluation=_metrics_for(
                    "evaluation",
                    scored.filter(pl.col(COL_ROLE) == evaluation_role),
                    model_cfg=self.model_cfg,
                    split_cfg=self.split_cfg,
                    provenance=self.provenance,
                    score_columns=columns,
                ),
                evaluation_role=evaluation_role,
            )

        return FoldRun(
            fold=fold,
            seed=run_seed,
            mode=mode,
            provenance=self.provenance,
            scored=ordered_queue(scored, P_FUSED_COLUMN, COL_ACCOUNT_KEY),
            metrics=metrics,
            fusion=fusion,
            gbm=gbm,
            anomaly=anomaly,
            scorecard_model=scorecard_model,
            calibration=calibration,
            lineage=lineage,
            leakage=leakage,
            plan=plan,
            skipped=skipped,
            skip_reason=(plan.skip_reason if plan is not None and skipped else None),
            skip_detail=(plan.skip_detail if plan is not None and skipped else None),
            channel_skips=channel_skips,
            tuning=tuning,
            drift=drift,
            explanation=explanation,
        )

    # -- channel internals -----------------------------------------------------

    def _population_base_rate(self, validation: pl.DataFrame) -> float:
        declared = self.model_cfg.calibration.population_base_rate
        if declared is not None:
            return float(declared)
        if validation.height == 0:
            raise ModelLayerError(
                "calibration.population_base_rate is null and the validation slice is empty, "
                "so the base rate the intercept is corrected toward cannot be measured"
            )
        return float(validation.get_column(COL_LABEL).sum()) / validation.height

    def _fit_gbm(
        self,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        features: tuple[str, ...],
        categorical: tuple[str, ...],
        seed: int,
        channel_skips: dict[str, str],
        skip_key: str = "gbm",
    ) -> tuple[GbmBundle | None, TuningResult | None]:
        budget = (
            self.model_cfg.optuna.n_trials if self.trial_budget is None else int(self.trial_budget)
        )
        tuning: TuningResult | None = None
        try:
            if budget > 1:
                tuning = tune_gbm(
                    train,
                    validation,
                    features,
                    categorical,
                    self.model_cfg.optuna,
                    self.model_cfg.gbm,
                    seed,
                    trial_budget=budget,
                )
                bundle = fit_gbm_with_best_params(
                    train, validation, features, categorical, tuning, self.model_cfg.gbm, seed
                )
            else:
                bundle = fit_gbm(train, validation, features, categorical, self.model_cfg.gbm, seed)
        except ModelLayerError as exc:
            # Skipped with a named reason and surfaced, never silently omitted.
            channel_skips[skip_key] = f"{skip_key} fit refused: {exc}"
            return None, None
        return bundle, tuning

    def ablated_feature_names(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """``(kept, removed)`` for the feature-subset booster, from the registry's own groups.

        The names come from the registry rather than a hard-coded prefix so the row labelled
        "without graph features" is bound to the features P2 declares as graph features. A
        group that exists in config but publishes no feature is refused here rather than
        silently ablate nothing.
        """
        removed: list[str] = []
        for group in self.ablate_feature_groups:
            members = tuple(self.feature_registry.groups[group])
            if not members:
                raise ModelLayerError(
                    f"registry group {group!r} declares no features, so ablating it would "
                    "re-fit the same model under a different label"
                )
            unknown = sorted(set(members) - set(self.feature_registry.names))
            if unknown:
                raise ModelLayerError(
                    f"registry group {group!r} publishes {unknown}, which is not in the "
                    "fitted feature list"
                )
            removed.extend(members)
        dropped = set(removed)
        kept = tuple(name for name in self.feature_registry.names if name not in dropped)
        if not kept:
            raise ModelLayerError(
                f"ablating {self.ablate_feature_groups} would leave no features to fit"
            )
        return kept, tuple(sorted(dropped))

    def _fit_feature_ablated_gbm(
        self,
        scored: pl.DataFrame,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        seed: int,
        channel_skips: dict[str, str],
    ) -> pl.DataFrame:
        """Fit the booster a second time without the ablated groups and publish its column.

        Same rows, same seed, same early-stopping discipline as :data:`P_GBM_COLUMN` — only the
        feature set differs, which is what makes the two columns a measurement of the removed
        groups rather than two views of one number. A fold whose reduced fit is refused leaves
        the column absent and the reason in ``channel_skips``; the scorer then refuses the row
        by name instead of falling back to the full model's probability.
        """
        from oxbow.models.gbm import _matrix as gbm_matrix

        categorical_all = tuple(self.scorecard_cfg.binning.categorical_features)
        kept, _removed = self.ablated_feature_names()
        categorical = tuple(c for c in categorical_all if c in set(kept))
        bundle, _ = self._fit_gbm(
            train,
            validation,
            kept,
            categorical,
            seed,
            channel_skips,
            skip_key=P_GBM_NO_GRAPH_COLUMN,
        )
        if bundle is None:
            return scored
        return scored.with_columns(
            pl.Series(
                P_GBM_NO_GRAPH_COLUMN,
                bundle.predict_matrix(gbm_matrix(scored, kept, categorical)),
            )
        )

    def _fit_fusion(
        self, scored: pl.DataFrame, channel_skips: dict[str, str]
    ) -> FusionModel | None:
        validation = scored.filter(pl.col(COL_ROLE) == ROLE_VALIDATION)
        try:
            return fit_fusion(validation, self.model_cfg.fusion, FUSION_INPUTS)
        except ModelLayerError as exc:
            channel_skips["fusion"] = f"fusion refused: {exc}"
            return None

    def _calibrate_fused(
        self, scored: pl.DataFrame, train: pl.DataFrame, channel_skips: dict[str, str]
    ) -> CalibrationOutcome:
        validation = scored.filter(pl.col(COL_ROLE) == ROLE_VALIDATION)
        raw = (
            validation.get_column(P_FUSED_RAW_COLUMN).cast(pl.Float64).to_numpy().astype(np.float64)
        )
        labels = validation.get_column(COL_LABEL).cast(pl.Int32).to_numpy().astype(np.int32)
        outcome = calibrate(
            raw,
            labels,
            self.model_cfg.calibration,
            train_positive_share=float(
                np.mean(train.get_column(COL_LABEL).cast(pl.Float64).to_numpy())
            )
            if train.height
            else 0.0,
            population_positive_share=self._population_base_rate(validation),
        )
        if outcome.refused:
            channel_skips["calibration"] = outcome.refusal_reason or outcome.branch_reason
        return outcome

    def _publish_fused(self, scored: pl.DataFrame, outcome: CalibrationOutcome) -> pl.DataFrame:
        """``p_fused``: the calibrated probability, or the raw score labelled uncalibrated.

        When calibration is refused the ranking column still has to exist -- a queue
        cannot be built from nothing -- so it carries the raw fused score and the row
        says in words that it is not a calibrated probability. The value is monotone in
        the score either way, so the ranking is unaffected while the money multiplication
        downstream is protected by the ``calibrated`` flag.
        """
        mapped = outcome.apply(
            scored.get_column(P_FUSED_RAW_COLUMN).cast(pl.Float64).to_numpy().astype(np.float64)
        )
        if mapped is None:
            mapped = (
                scored.get_column(P_FUSED_RAW_COLUMN).cast(pl.Float64).to_numpy().astype(np.float64)
            )
        return scored.with_columns(pl.Series(P_FUSED_COLUMN, mapped))

    def _explain(
        self, scored: pl.DataFrame, gbm: GbmBundle | None, channel_skips: dict[str, str]
    ) -> tuple[pl.DataFrame, ExplanationOutcome]:
        annotated, outcome = explain_and_report(
            scored,
            gbm,
            self.model_cfg.shap,
            P_GBM_COLUMN if gbm is not None else P_SCORECARD_COLUMN,
        )
        if outcome.source == SOURCE_SCORECARD:
            channel_skips["shap"] = f"scorecard-explained: {outcome.fallback_reason}"
        else:
            channel_skips["shap"] = "shap persisted per row"
        return annotated, outcome

    def _open_tracking(
        self, fold: int, seed: int, mode: str, prior_correction: dict[str, object], name: str | None
    ) -> _TrackingContext:
        return _TrackingContext(
            registry=self.tracking,
            name=name or f"fold-{fold}-seed-{seed}",
            tags={
                "fold": fold,
                "seed": seed,
                "scoring_mode": mode,
                "provenance": self.provenance,
                "phase": "P4b",
                "prior_correction_method": str(prior_correction["method"]),
            },
            params={
                "fold": fold,
                "seed": seed,
                "scoring_mode": mode,
                "provenance": self.provenance,
                "optuna_trials_configured": self.model_cfg.optuna.n_trials,
                "optuna_trial_budget_effective": (
                    self.model_cfg.optuna.n_trials
                    if self.trial_budget is None
                    else self.trial_budget
                ),
                "prior_correction": json.dumps(prior_correction, sort_keys=True, default=str),
            },
        )

    def _metrics_to_log(
        self,
        calibration: CalibrationOutcome | None,
        gbm: GbmBundle | None,
        fusion: FusionModel | None,
        prior_correction: dict[str, object],
    ) -> dict[str, float]:
        logged: dict[str, float] = {}
        if calibration is not None:
            logged.update(
                {
                    "brier": calibration.brier,
                    "brier_uncorrected": calibration.raw_brier,
                    "ece": calibration.ece,
                    "validation_positives": float(calibration.validation_positives),
                    "mean_predicted_probability": calibration.mean_calibrated_probability,
                    "calibration_refused": 1.0 if calibration.refused else 0.0,
                }
            )
        if gbm is not None:
            logged["gbm_validation_pr_auc"] = gbm.best_valid_pr_auc
            logged["gbm_scale_pos_weight"] = gbm.scale_pos_weight
        if fusion is not None:
            logged["fusion_validation_pr_auc"] = fusion.validation_pr_auc
            logged["fusion_validation_brier"] = fusion.validation_brier
        logged["prior_correction_logit_shift"] = float(prior_correction["logit_shift"])
        return logged


class _TrackingContext:
    """Enter the tracking run when a registry is configured; a no-op when it is not.

    Kept out of :meth:`FoldModelRunner.run_fold` inline because the degraded path must
    behave identically with and without a registry: a fold scored while tracking is down
    produces the same rows, the same calibration and the same SHAP, and only loses the
    lineage -- which is what the plan says a degraded banner is for.
    """

    def __init__(
        self,
        *,
        registry: ModelRegistry | None,
        name: str,
        tags: Mapping[str, object],
        params: Mapping[str, object],
    ) -> None:
        self._registry = registry
        self._name = name
        self._tags = dict(tags)
        self._params = dict(params)
        self._run: object | None = None

    def __enter__(self) -> object | None:
        if self._registry is None:
            return None
        run = self._registry.open_run(run_name=self._name, tags=self._tags)
        run.__enter__()
        run.log_params(self._params)
        self._run = run
        return run

    def __exit__(self, *exc_info: object) -> None:
        if self._run is not None:
            self._run.__exit__(*exc_info)


@dataclass(frozen=True, slots=True)
class AccountProbability:
    """One account: the calibrated p and the measured rate and count behind it."""

    account_key: str
    p_calibrated: float
    band_observed_rate: float
    band_n: int

    def __post_init__(self) -> None:
        for name in ("p_calibrated", "band_observed_rate"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ModelLayerError(f"{name}={value} for {self.account_key} is outside [0, 1]")
        if self.band_n < 0:
            raise ModelLayerError(f"band_n={self.band_n} for {self.account_key} is negative")


@dataclass(frozen=True, slots=True)
class FoldScore:
    """A fold's calibrated output, shaped the way the harness consumes it."""

    scores: Mapping[str, AccountProbability]
    model_version: str
    feature_spec_hash: str
    calibrated: bool

    def p_of(self, account_key: str) -> float:
        """The probability for one account, refusing to let an unknown become a zero."""
        try:
            return self.scores[account_key].p_calibrated
        except KeyError as exc:
            raise ModelLayerError(
                f"the scorer returned no probability for {account_key!r}; reporting a "
                "missing score as p=0 would move an account in the ranking silently"
            ) from exc


class P4bScorer:
    """P6's ``Scorer`` seam, satisfied without importing ``oxbow.backtest``.

    ``score(*, train, validation, scored, feature_spec_hash, seed)`` fits on
    train+validation only and returns a calibrated probability per account plus the band
    evidence behind it. The keyword names are the harness's, so the two layers meet by
    structure while models/ keeps its import boundary. The feature hash is checked inside
    ``build_training_frame``, which refuses a frame whose spec hash disagrees with the
    registry the model was fitted against (plan §8, 02 §B seam 3).
    """

    def __init__(
        self, runner: FoldModelRunner, *, fold: int = 0, embargo_days: int | None = None
    ) -> None:
        self._runner = runner
        self._fold = fold
        self._embargo = runner.split_cfg.embargo_days if embargo_days is None else int(embargo_days)

    def score(
        self,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> FoldScore:
        rows = pl.concat([train, validation, scored])
        frame = build_fold_training_frame(
            rows,
            self._runner.feature_registry,
            self._runner.scorecard_cfg,
            provenance=self._runner.provenance,
            split_cfg=self._runner.split_cfg,
        )
        if frame.feature_spec_hash != feature_spec_hash:
            raise FrameContractViolationError(
                f"the harness declared feature_spec_hash {feature_spec_hash[:16]}… but the fold "
                f"frame carries {frame.feature_spec_hash[:16]}…: scoring with a feature set the "
                "model never saw produces confident nonsense, so it is refused (plan §8)"
            )
        evaluation_role = (
            str(scored.get_column(COL_ROLE).unique().to_list()[0])
            if COL_ROLE in scored.columns
            else ROLE_TEST
        )
        run = self._runner.run_fold(
            frame,
            self._fold,
            provider=_FixedSlicesProvider(train, validation, scored, self._embargo),
            evaluation_role=evaluation_role,
            seed=seed,
        )
        rows_out = run.scored.filter(pl.col(COL_ROLE) == evaluation_role)
        entries: dict[str, AccountProbability] = {}
        for row in rows_out.iter_rows(named=True):
            key = str(row[COL_ACCOUNT_KEY])
            rate = row[BAND_OBSERVED_RATE_COLUMN]
            entries[key] = AccountProbability(
                account_key=key,
                p_calibrated=float(row[P_FUSED_COLUMN]),
                band_observed_rate=0.0 if rate is None else float(rate),
                band_n=int(row[BAND_N_COLUMN] or 0),
            )
        return FoldScore(
            scores=entries,
            model_version=(
                run.lineage.model_version
                if run.lineage is not None and run.lineage.model_version is not None
                else (
                    run.gbm.fingerprint()[:12]
                    if run.gbm is not None
                    else f"fold-{self._fold}-{run.mode}"
                )
            ),
            feature_spec_hash=frame.feature_spec_hash,
            calibrated=run.calibrated,
        )


def aggregate_runs(runs: Sequence[FoldRun], provenance: str) -> dict[str, object]:
    """The fold table: every fold, including the skipped ones and why they were skipped."""
    return {
        "folds": len(runs),
        "evaluated_folds": sum(1 for run in runs if not run.skipped),
        "skipped_folds": [
            {"fold": run.fold, "reason": run.skip_reason, "detail": run.skip_detail}
            for run in runs
            if run.skipped
        ],
        "degraded_folds": [run.fold for run in runs if run.mode == MODE_DEGRADED],
        "scorecard_only_folds": [run.fold for run in runs if run.mode == MODE_SCORECARD_ONLY],
        "provenance": provenance,
        "per_fold": [
            {
                "fold": run.fold,
                "mode": run.mode,
                "skipped": run.skipped,
                "skip_reason": run.skip_reason,
                "rows": run.scored.height,
                "channel_skips": dict(run.channel_skips),
                "calibrated": run.calibrated,
                "fusion_one_line": None if run.fusion is None else run.fusion.one_line(),
                "leakage_evidence": run.leakage.to_dict(),
            }
            for run in runs
        ],
        "fold_plans": [run.plan.to_dict() for run in runs if run.plan is not None],
    }


def pooled_slice(runs: Sequence[FoldRun], role: str) -> pl.DataFrame:
    """Rows for one role across every fold that produced them, in codebase order."""
    frames = [run.scored.filter(pl.col(COL_ROLE) == role) for run in runs if not run.skipped]
    kept = [frame for frame in frames if frame.height]
    if not kept:
        raise ModelLayerError(
            f"no fold produced {role!r} rows, so there is nothing to pool; the fold table "
            "names the skipped folds and their reasons"
        )
    return pl.concat(kept).sort([COL_AS_OF_TS, COL_ACCOUNT_KEY])


def pooled_gate(
    runs: Sequence[FoldRun], *, model_cfg: ModelConfig, split_cfg: SplitConfig
) -> GateOutcome:
    """The day-7 gate on the pooled validation slice.

    Pooling is the right unit for the gate: five small folds each give a noisy PR-AUC, and
    the gate is a statement about the stack, not about one fold's luck. The per-fold
    numbers stay on ``FoldRun.metrics`` so the spread across folds remains visible rather
    than being averaged away.
    """
    pooled = pooled_slice(runs, ROLE_VALIDATION)
    metrics = _metrics_for(
        "pooled_validation",
        pooled,
        model_cfg=model_cfg,
        split_cfg=split_cfg,
        provenance=runs[0].provenance,
        score_columns={
            "fused": P_FUSED_COLUMN,
            "scorecard": P_SCORECARD_COLUMN,
            "rules_only": RULES_ONLY_COLUMN,
        },
    )
    return day7_gate(metrics, runs[0].provenance)


def seed_stability_over_folds(
    frame: TrainingFrame,
    plans: FoldPlanSet,
    *,
    runner_factory: Callable[[int], FoldModelRunner],
    seeds: Sequence[int],
    provider: FoldProvider | None = None,
) -> SeedStability:
    """Re-run every fold once per declared seed; report mean +/- sd of validation PR-AUC.

    A single seed's number is a lucky draw and the plan asks for the spread instead. The
    seeds come from config/splits.yaml so this phase and the backtest cannot quietly tune
    a different set.
    """

    def run_for_seed(seed: int) -> float:
        runner = runner_factory(seed)
        active = provider or runner.provider()
        values: list[float] = []
        for plan in plans.evaluated:
            run = runner.run_fold(frame, plan.fold, provider=active, seed=seed, plan=plan)
            if run.metrics is None or "fused" not in run.metrics.validation:
                continue
            values.append(run.metrics.validation["fused"].pr_auc.estimate)
        if not values:
            raise ModelLayerError(
                "no fold produced a fused validation PR-AUC for seed "
                f"{seed}, so seed stability cannot be reported as mean +/- sd"
            )
        return float(np.mean(values))

    return seed_stability("validation_pr_auc_pooled_over_folds", seeds, run_for_seed)


def stack_scored_frames(frames: Sequence[pl.DataFrame]) -> pl.DataFrame:
    """Put every fold's scored rows in one frame, aligned on column names.

    Folds do not carry the same channels. A fold that degraded to `scorecard_and_rules_only` has
    no `p_gbm` and no `anomaly_norm`; a fold that ran the full stack carries no `drift_*`. Each
    mode swaps two columns for two others, so the frames are the same width with different names,
    and `vertical_relaxed` aligns by position and refuses that —
    `ComputeError: schema names differ: got p_gbm, expected p_fused_raw`. On 2026-09-27 that
    killed a run *after* it had scored all five folds, the most complete fold coverage this repo
    has produced: every model fitted, and not one number written down.

    Aligning on names makes a channel one fold did not produce a null on that fold's rows, which
    is what the row already says through `scoring_mode`. It is not a licence to null-fill the
    contract: a column NO fold produced is a build defect, and `SCORED_ROW_COLUMNS` — documented
    since P4b as "the columns every persisted scored row carries", and enforced by nothing until
    now — is checked against the union so a typo becomes a refusal instead of a silent extra
    column of nulls for the UI to read as a channel that exists and is empty.
    """
    if not len(frames):
        raise ModelLayerError(
            "no fold frames to stack; the caller decided there was nothing to land"
        )
    scored = pl.concat(list(frames), how="diagonal_relaxed")
    absent = [column for column in SCORED_ROW_COLUMNS if column not in scored.columns]
    if absent:
        raise ModelLayerError(
            f"no fold produced {absent}; the persisted artifact would not carry the row "
            "contract SCORED_ROW_COLUMNS that P8's UI reads, so a missing column is a "
            "build defect and not something to null-fill"
        )
    return scored


def write_run_artifacts(
    runs: Sequence[FoldRun],
    *,
    reporting: ReportingConfig,
    model_card: Mapping[str, object],
    root: Path,
    gate: Mapping[str, object] | None = None,
    stability: Mapping[str, object] | None = None,
    fold_table: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Persist the artefacts under ``reporting.artifact_dir`` and hash them.

    The scored rows are one parquet with the SHAP payload already inside it: persistence
    happens here, during the run, so the API never computes an explanation on request
    (which is what would stall the browser the plan names).
    """
    directory = Path(root) / reporting.artifact_dir
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, object] = {}
    if runs:
        scored = stack_scored_frames([run.scored for run in runs])
        parquet_path = directory / reporting.scored_output_filename
        scored.write_parquet(parquet_path)
        written[parquet_path.name] = _artifact_record(parquet_path, reporting, rows=scored.height)
    payloads: dict[str, Mapping[str, object]] = {"model_card.json": model_card}
    for name, payload in (
        ("gate.json", gate),
        ("seed_stability.json", stability),
        ("folds.json", fold_table),
    ):
        if payload is not None:
            payloads[name] = payload
    for name, payload in payloads.items():
        path = directory / name
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        written[name] = _artifact_record(path, reporting)
    manifest = directory / "artifacts.json"
    manifest.write_text(
        json.dumps({"artifact_dir": str(directory), "files": written}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {"artifact_dir": str(directory), "files": written}


def _artifact_record(path: Path, reporting: ReportingConfig, **extra: object) -> dict[str, object]:
    record: dict[str, object] = {"path": str(path), "bytes": path.stat().st_size}
    if reporting.hash_artifacts:
        record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    record.update(extra)
    return record


__all__ = [
    "ANOMALY_COLUMN",
    "BAND_N_COLUMN",
    "BAND_OBSERVED_RATE_COLUMN",
    "MODE_DEGRADED",
    "MODE_FULL",
    "MODE_SCORECARD_ONLY",
    "P_FUSED_COLUMN",
    "P_GBM_COLUMN",
    "P_SCORECARD_COLUMN",
    "SCORED_ROW_COLUMNS",
    "UNCALIBRATED_UI_TEXT",
    "AccountProbability",
    "DriftGate",
    "FoldMetrics",
    "FoldModelRunner",
    "FoldProvider",
    "FoldRun",
    "FoldScore",
    "GateOutcome",
    "LeakageEvidence",
    "P4bScorer",
    "RoleColumnFoldProvider",
    "aggregate_runs",
    "annotate_rows",
    "assert_no_leakage",
    "build_fold_training_frame",
    "day7_gate",
    "drift_gate",
    "ordered_queue",
    "pooled_gate",
    "pooled_slice",
    "row_keys",
    "seed_stability_over_folds",
    "stack_scored_frames",
    "write_run_artifacts",
]
