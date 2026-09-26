"""Fold iteration: the plan's skip-with-named-reason, not a silent drop.

P2 owns the splits module (expanding window, purge, 30-day embargo) and P6 owns the
harness. What P4b owns is narrower and still load-bearing: a fold whose evaluation
slice contains no positives has an **undefined** PR-AUC, and the two tempting wrong
answers -- report 0.0, or drop the fold from the average without saying so -- are both
in the plan's edge-case list (03 H: ``test_zero_positive_fold_reported``). Reporting 0.0
understates; dropping silently makes a five-fold run look like a four-fold run that
"just happened" to be easier.

So a fold is either evaluated or skipped with a reason that carries its population,
its positive count and the threshold it failed, and the skipped list is part of the
payload the validation page reads.

This module deliberately does *not* re-derive temporal boundaries. It reads the
``fold`` and ``role`` columns the splits module emits. The one fallback, in
``fold_plan_from_frame``, derives validation windows from the fractions P0 already
committed in config/splits.yaml, and it stamps ``role_source`` so a reader can tell
which path produced the split it is looking at.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import polars as pl

from oxbow.models.errors import ModelLayerError
from oxbow.scoring.frame import (
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    COL_FOLD,
    COL_LABEL,
    COL_ROLE,
    ROLE_TEST,
    ROLE_TRAIN,
    ROLE_VALIDATION,
    TrainingFrame,
)

SKIP_ZERO_POSITIVES: Final = "zero_positives_in_evaluation_slice"
SKIP_EMPTY_SLICE: Final = "empty_evaluation_slice"
SKIP_DEGRADED_RUN: Final = "skipped_by_drift_action"


@dataclass(frozen=True, slots=True)
class FoldPlan:
    """One fold's slices plus the decision about whether it can be evaluated."""

    fold: int
    train_rows: int
    validation_rows: int
    evaluation_rows: int
    evaluation_positives: int
    base_rate: float
    skipped: bool
    skip_reason: str | None
    skip_detail: str | None
    evaluation_role: str

    @property
    def key(self) -> str:
        return f"fold-{self.fold}"

    def to_dict(self) -> dict[str, object]:
        return {
            "fold": self.fold,
            "train_rows": self.train_rows,
            "validation_rows": self.validation_rows,
            "evaluation_rows": self.evaluation_rows,
            "evaluation_positives": self.evaluation_positives,
            "base_rate": round(self.base_rate, 8),
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "skip_detail": self.skip_detail,
            "evaluation_role": self.evaluation_role,
        }


@dataclass(frozen=True, slots=True)
class FoldPlanSet:
    """Every fold, the ones that ran, and the ones that did not and why."""

    plans: tuple[FoldPlan, ...]
    role_source: str
    embargo_days: int

    @property
    def evaluated(self) -> tuple[FoldPlan, ...]:
        return tuple(plan for plan in self.plans if not plan.skipped)

    @property
    def skipped(self) -> tuple[FoldPlan, ...]:
        return tuple(plan for plan in self.plans if plan.skipped)

    @property
    def total_positives(self) -> int:
        return int(sum(plan.evaluation_positives for plan in self.plans))

    def to_dict(self) -> dict[str, object]:
        return {
            "role_source": self.role_source,
            "embargo_days": self.embargo_days,
            "folds": len(self.plans),
            "evaluated_folds": len(self.evaluated),
            "skipped_folds": [plan.to_dict() for plan in self.skipped],
            "plans": [plan.to_dict() for plan in self.plans],
            # Reported even when empty: "no fold was skipped" is a fact a reader
            # needs, and an absent key is indistinguishable from an unimplemented one.
            "skipped_fold_count": len(self.skipped),
        }


def _slice(frame: pl.DataFrame, fold: int, role: str) -> pl.DataFrame:
    return frame.filter((pl.col(COL_FOLD) == fold) & (pl.col(COL_ROLE) == role))


def fold_plan_from_frame(
    frame: TrainingFrame,
    evaluation_role: str,
    embargo_days: int,
    skip_roles: Sequence[str] = (),
) -> FoldPlanSet:
    """Build the per-fold plan from the frame's own ``fold`` and ``role`` columns.

    ``skip_roles`` lets a caller name roles that must not be evaluated (the drift
    action uses it: a fold whose model does not run is reported as skipped with a
    reason, not quietly omitted from the fold table).
    """
    if evaluation_role not in (ROLE_VALIDATION, ROLE_TEST):
        raise ModelLayerError(
            f"evaluation role must be validation or test, got {evaluation_role!r}: fitting "
            "and evaluating on the same rows is the rejection trigger, not a mistake"
        )
    data = frame.data
    if COL_ROLE not in data.columns:
        raise ModelLayerError("frame carries no role column; role assignment happens upstream")
    role_source = (
        "splits_module"
        if frame.validation_fraction == 0.0
        else "derived_from_fold_and_as_of_ts"
    )
    plans: list[FoldPlan] = []
    for fold in frame.folds:
        train = _slice(data, fold, ROLE_TRAIN)
        validation = _slice(data, fold, ROLE_VALIDATION)
        evaluation = _slice(data, fold, evaluation_role)
        positives = int(evaluation.get_column(COL_LABEL).sum()) if evaluation.height else 0
        skipped = evaluation.height == 0 or positives == 0 or evaluation_role in skip_roles
        reason: str | None = None
        detail: str | None = None
        if evaluation_role in skip_roles:
            reason = SKIP_DEGRADED_RUN
            detail = (
                f"role {evaluation_role!r} excluded by the run's mode; fold {fold} is "
                "reported rather than hidden"
            )
        elif evaluation.height == 0:
            reason = SKIP_EMPTY_SLICE
            detail = f"fold {fold} has no {evaluation_role} rows at all"
        elif positives == 0:
            reason = SKIP_ZERO_POSITIVES
            detail = (
                f"fold {fold} has {evaluation.height} {evaluation_role} rows and 0 positives; "
                "PR-AUC is undefined on a zero-positive slice, so the fold is skipped with "
                "this reason and shown on the validation page (03 H)"
            )
        plans.append(
            FoldPlan(
                fold=fold,
                train_rows=train.height,
                validation_rows=validation.height,
                evaluation_rows=evaluation.height,
                evaluation_positives=positives,
                base_rate=(positives / evaluation.height) if evaluation.height else 0.0,
                skipped=skipped,
                skip_reason=reason,
                skip_detail=detail,
                evaluation_role=evaluation_role,
            )
        )
    return FoldPlanSet(
        plans=tuple(plans),
        role_source=role_source,
        embargo_days=embargo_days,
    )


def fold_frame(frame: TrainingFrame, plan: FoldPlan) -> dict[str, pl.DataFrame]:
    """The three slices of one fold, keyed by role."""
    return {
        ROLE_TRAIN: _slice(frame.data, plan.fold, ROLE_TRAIN),
        ROLE_VALIDATION: _slice(frame.data, plan.fold, ROLE_VALIDATION),
        plan.evaluation_role: _slice(frame.data, plan.fold, plan.evaluation_role),
    }


def pooled_slice(data: pl.DataFrame, roles: Sequence[str]) -> pl.DataFrame:
    """Rows across every fold for one role, in the codebase's total order.

    Used by the single-model wiring path: train on all training rows, calibrate on all
    validation rows. P6's harness calls :func:`fold_frame` per fold instead, which is
    why both exist here rather than one pretending to cover the other.
    """
    out = data.filter(pl.col(COL_ROLE).is_in(list(roles)))
    return out.sort([COL_AS_OF_TS, COL_ACCOUNT_KEY])


__all__ = [
    "SKIP_DEGRADED_RUN",
    "SKIP_EMPTY_SLICE",
    "SKIP_ZERO_POSITIVES",
    "FoldPlan",
    "FoldPlanSet",
    "fold_frame",
    "fold_plan_from_frame",
    "pooled_slice",
]
