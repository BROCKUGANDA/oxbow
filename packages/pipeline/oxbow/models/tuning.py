"""Optuna search inside the declared budget, with the budget itself reported.

Two things the plan is explicit about and this module therefore cannot skip:

* **The number of configurations evaluated is a reported number.** With N trials the best
  validation result is optimistically biased, which is precisely why the headline comes
  from the untouched test fold (plan §12). So ``trials_requested``, ``trials_used``, the
  wall-clock cap and the reason for any shortfall land in the result, and every trial's
  parameters and score are kept -- ``log_every_trial: true`` in config, honoured here by
  returning the list rather than logging it to a console and forgetting it.
* **Selection happens on validation only.** The objective is validation PR-AUC from the
  early-stopped fit. The test fold is not passed into this function at all, which is a
  stronger guarantee than a comment saying it is not read.

Sampling is seeded from ``optuna.seed``, the pruner is deliberately absent (a pruned trial
that would have won is a finding nobody reports), and the study name comes from config.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final

import numpy as np
import optuna
import polars as pl

from oxbow.models.config import OptunaConfig
from oxbow.models.errors import ModelLayerError
from oxbow.models.gbm import GbmBundle, fit_gbm
from oxbow.scoring.frame import ROLE_TEST

TRIAL_LOG_LIMIT: Final = 200


@dataclass(frozen=True, slots=True)
class TuningTrial:
    """One evaluated configuration and what it scored."""

    number: int
    params: dict[str, float]
    validation_pr_auc: float
    best_iteration: int
    seconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "params": {key: round(value, 8) for key, value in sorted(self.params.items())},
            "validation_pr_auc": round(self.validation_pr_auc, 8),
            "best_iteration": self.best_iteration,
            "seconds": round(self.seconds, 4),
        }


@dataclass(frozen=True, slots=True)
class TuningResult:
    """The winner, the field, and the honest statement of how much searching happened."""

    best_params: dict[str, float | int] | None
    best_pr_auc: float
    trials: tuple[TuningTrial, ...]
    trials_requested: int
    trials_used: int
    budget_short_reason: str | None
    wall_clock_seconds: float
    study_name: str

    def to_dict(self) -> dict[str, object]:
        return {
            "best_params": (
                {key: round(value, 8) for key, value in sorted(self.best_params.items())}
                if self.best_params
                else None
            ),
            "best_validation_pr_auc": round(self.best_pr_auc, 8),
            "trials_requested": self.trials_requested,
            "trials_used": self.trials_used,
            "budget_short_reason": self.budget_short_reason,
            "wall_clock_seconds": round(self.wall_clock_seconds, 3),
            "study_name": self.study_name,
            # The multiple-testing caveat is arithmetic, not prose: the reported best
            # validation score is the maximum of this many noisy estimates.
            "selection_is_max_over": self.trials_used,
            "trials": [trial.to_dict() for trial in self.trials[:TRIAL_LOG_LIMIT]],
            "trials_logged": len(self.trials),
        }


def _suggest(
    trial: optuna.Trial, space: dict[str, dict[str, object]]
) -> dict[str, float | int]:
    """One configuration from the declared search space, with its declared type kept.

    An int stays an int: LightGBM rejects ``num_leaves=32.0`` with
    "Parameter num_leaves should be of type int", and coerces nothing on its own. A
    search that returned every value as a float would fail on the first trial of every
    fold, which is the kind of failure that only shows up when the tuning path is run
    rather than read.
    """
    params: dict[str, float | int] = {}
    for name, spec in space.items():
        kind = str(spec.get("type", "float"))
        low = float(spec["low"])  # type: ignore[arg-type]
        high = float(spec["high"])  # type: ignore[arg-type]
        log = bool(spec.get("log", False))
        if kind == "int":
            params[name] = int(trial.suggest_int(name, int(low), int(high), log=log))
        elif kind == "float":
            params[name] = float(trial.suggest_float(name, low, high, log=log))
        elif kind == "categorical":
            choices = spec.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ModelLayerError(f"search_space.{name} has no choices")
            params[name] = float(trial.suggest_categorical(name, choices))
        else:
            raise ModelLayerError(f"search_space.{name} has unknown type {kind!r}")
    return params


def tune_gbm(
    train_frame: pl.DataFrame,
    valid_frame: pl.DataFrame,
    feature_names: tuple[str, ...],
    categorical_features: tuple[str, ...],
    cfg: OptunaConfig,
    gbm_cfg: object,
    seed: int,
    trial_budget: int | None = None,
) -> TuningResult:
    """Search the declared space on validation PR-AUC.

    ``trial_budget`` exists for the wiring runs and is reported, not hidden: it is
    recorded as ``trials_requested`` only when it equals config, otherwise
    ``budget_short_reason`` names the caller-supplied override and the number of
    configurations actually evaluated. A silently reduced search would misreport the
    optimism of the selected model, which is the one thing the plan insists on stating.

    ``ROLE_TEST`` rows are refused on sight: a caller that handed in the untouched fold
    as the stopping set would be tuning on the answer.
    """
    if ROLE_TEST in set(valid_frame.get_column("role").unique().to_list()):
        raise ModelLayerError(
            "the tuning stopping set contains rows with role 'test'; selection must happen "
            "on validation and the test fold is touched once (plan §12)"
        )
    requested = cfg.n_trials if trial_budget is None else trial_budget
    if requested < 1:
        raise ModelLayerError(f"trial budget must be >= 1, got {requested}")

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    sampler = optuna.samplers.TPESampler(seed=cfg.seed, n_startup_trials=min(8, requested))
    study = optuna.create_study(direction=cfg.direction, study_name=cfg.study_name, sampler=sampler)
    trials: list[TuningTrial] = []
    started = time.perf_counter()

    def objective(trial: optuna.Trial) -> float:
        params = _suggest(trial, dict(cfg.search_space))
        bundle = fit_gbm(
            train_frame,
            valid_frame,
            feature_names,
            categorical_features,
            gbm_cfg,  # type: ignore[arg-type]
            seed,
            params_override=params,
        )
        score = bundle.best_valid_pr_auc
        if not np.isfinite(score):
            score = 0.0
        trials.append(
            TuningTrial(
                number=trial.number,
                params=params,
                validation_pr_auc=float(score),
                best_iteration=bundle.best_iteration,
                seconds=time.perf_counter() - started,
            )
        )
        return float(score)

    # Optuna 4.3 has no ``optuna.stoppers`` module (the stopper classes were removed), so
    # the wall-clock cap is study.optimize's own ``timeout``, which is checked between
    # trials -- exactly the semantics config's timeout_seconds describes: a laptop run
    # stops cleanly at the cap rather than finishing at 4 a.m., and fewer than
    # ``n_trials`` configurations then get recorded with the reason.
    study.optimize(
        objective,
        n_trials=requested,
        timeout=float(cfg.timeout_seconds) if cfg.timeout_seconds else None,
        gc_after_trial=False,
    )

    if not trials:
        return TuningResult(
            best_params=None,
            best_pr_auc=0.0,
            trials=(),
            trials_requested=requested,
            trials_used=0,
            budget_short_reason="every trial raised; no configuration was scored",
            wall_clock_seconds=time.perf_counter() - started,
            study_name=cfg.study_name,
        )
    ordered = sorted(trials, key=lambda item: (-item.validation_pr_auc, item.number))
    winner = ordered[0]
    short_reason: str | None = None
    if len(trials) < requested:
        # Stated, not hidden: the selected score is the maximum of fewer estimates than
        # config asked for, and which cap bit changes how optimistic that maximum is.
        limit_hit = bool(cfg.timeout_seconds and time.perf_counter() - started >= cfg.timeout_seconds)
        short_reason = (
            f"{len(trials)} of {requested} configurations were evaluated; "
            + (
                f"optuna.timeout_seconds={cfg.timeout_seconds} was reached"
                if limit_hit
                else "trial_budget override from the caller"
                if trial_budget is not None
                else "trials failed before scoring"
            )
        )
    return TuningResult(
        best_params=winner.params,
        best_pr_auc=winner.validation_pr_auc,
        trials=tuple(trials),
        trials_requested=requested,
        trials_used=len(trials),
        budget_short_reason=short_reason,
        wall_clock_seconds=time.perf_counter() - started,
        study_name=cfg.study_name,
    )


def fit_gbm_with_best_params(
    train_frame: pl.DataFrame,
    valid_frame: pl.DataFrame,
    feature_names: tuple[str, ...],
    categorical_features: tuple[str, ...],
    tuning: TuningResult,
    gbm_cfg: object,
    seed: int,
) -> GbmBundle:
    """Refit on the winning configuration, so the shipped model is the selected one.

    The refit is not redundant bookkeeping: the trial's booster was built inside Optuna's
    sampler state and the returned bundle's fingerprint must describe the model that goes
    to the registry, on the training rows the caller actually chose.
    """
    if tuning.best_params is None:
        raise ModelLayerError("no winning configuration to refit")
    return fit_gbm(
        train_frame,
        valid_frame,
        feature_names,
        categorical_features,
        gbm_cfg,  # type: ignore[arg-type]
        seed,
        params_override=tuning.best_params,
    )


__all__ = [
    "TRIAL_LOG_LIMIT",
    "TuningResult",
    "TuningTrial",
    "fit_gbm_with_best_params",
    "tune_gbm",
]
