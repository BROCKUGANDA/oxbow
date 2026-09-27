"""LightGBM: the ranking engine, with the determinism pins that make it auditable.

Plan §10: binary objective, ``scale_pos_weight`` from the train prior, early stopping on
validation PR-AUC, ``deterministic=true``, fixed seed, time-aware split. Every one of
those is a decision with a consequence, and the two that cost the most are stated here
rather than discovered later:

* **Single-threaded.** LightGBM's ``deterministic`` flag only guarantees bitwise-identical
  histograms when the split is row-wise *and* single-threaded. Twelve cores sit idle on
  purpose: a run whose model artefact hash moved between executions could not be
  compared to anything, and the phase requires hashing the artefacts and saying so.
* **Early stopping on PR-AUC, not logloss.** Under a 0.1 % base rate logloss is dominated
  by the negatives and keeps training trees that sharpen the mass of legitimate accounts
  while the fraud ranking degrades. PR-AUC is the metric the gate is reported on, so it
  is the metric the fit stops on.

``scale_pos_weight`` is derived from the train prior and never hand-set: the prior differs
across folds and across corpora, and a constant copied from a notebook is the sort of
tuning that quietly becomes load-bearing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import lightgbm as lgb
import numpy as np
import polars as pl

from oxbow.models.config import GbmConfig
from oxbow.models.errors import ModelLayerError
from oxbow.models.inputs import categorical_for_lightgbm
from oxbow.scoring.frame import COL_LABEL

MIN_POSITIVES_FOR_FIT: Final = 2
MODEL_ARTIFACT_FILENAME: Final = "gbm.txt"


def scale_pos_weight_from_prior(train_labels: np.ndarray) -> float:
    """``n_negatives / n_positives`` on the training slice, which is what the name means.

    Fails loud on a zero-positive training slice rather than defaulting to 1.0: with no
    positives the reweighting is undefined and a silent 1.0 would train a model that
    reports a plausible-looking near-zero probability for everything.
    """
    positives = int(train_labels.sum())
    negatives = int(train_labels.size - positives)
    if positives < MIN_POSITIVES_FOR_FIT:
        raise ModelLayerError(
            f"the training slice has {positives} positive row(s); scale_pos_weight is "
            f"n_negatives/n_positives and is undefined below {MIN_POSITIVES_FOR_FIT}. "
            "Refusing to default it to 1.0 (03 A rule 2)."
        )
    return negatives / positives


@dataclass(frozen=True, slots=True)
class GbmBundle:
    """The fitted booster plus everything needed to explain how it was fitted."""

    booster: lgb.Booster
    feature_names: tuple[str, ...]
    categorical_features: tuple[str, ...]
    params: Mapping[str, object]
    scale_pos_weight: float
    train_prior: float
    best_iteration: int
    best_valid_pr_auc: float
    n_estimators_requested: int
    seed: int
    history: tuple[dict[str, float], ...]
    degenerate: bool
    #: The subset of ``categorical_features`` actually named to ``categorical_feature``. A
    #: declared categorical whose codes are not admissible category indices (a string column,
    #: which arrives FNV-hashed) is coded numerically and is absent here; the gap is recorded
    #: rather than silent, because "which features the tree may split categorically" is a fact
    #: about the fitted model a reader is owed.
    lightgbm_categorical_features: tuple[str, ...] = ()

    def predict_matrix(self, matrix: np.ndarray) -> np.ndarray:
        """P(bad) for a feature matrix, at the early-stopped iteration.

        ``num_iteration`` is pinned to ``best_iteration`` rather than left at -1: a
        booster kept to 2000 trees would score with all of them and the early-stopping
        decision would exist only in the logs.
        """
        if matrix.ndim != 2 or matrix.shape[1] != len(self.feature_names):
            raise ModelLayerError(
                f"gbm expects {len(self.feature_names)} columns, got shape {matrix.shape}"
            )
        iteration = self.best_iteration if self.best_iteration > 0 else self.n_estimators_requested
        return np.asarray(self.booster.predict(matrix, num_iteration=iteration), dtype=np.float64)

    def training_curve(self) -> tuple[dict[str, float], ...]:
        """The validation PR-AUC per boosting round, for the validation page."""
        return self.history

    def fingerprint(self) -> str:
        """A hash over the model's substance: parameters, tree count, split structure.

        Reported so ``make verify-determinism`` can compare two runs' models without
        comparing a wall clock. Booster text dumps carry no timestamps, which is exactly
        why this is a stable identity.
        """
        dumped = self.booster.dump_model(num_iteration=self.best_iteration or -1)
        # LightGBM 4.x returns the parsed dict; 3.x returned a JSON string. Accepting only
        # one would make the artefact hash -- the thing determinism is checked with --
        # depend on which minor version happens to be installed.
        dump = dumped if isinstance(dumped, dict) else json.loads(str(dumped))
        payload = {
            "params": dict(self.params),
            "feature_names": list(self.feature_names),
            "scale_pos_weight": round(self.scale_pos_weight, 10),
            "best_iteration": self.best_iteration,
            "tree_count": len(dump.get("tree_info", [])),
            "structure_digest": hashlib.sha256(
                json.dumps(dump.get("tree_info", []), sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
        }
        encoded = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def split_feature_count(self) -> int:
        """How many features the ensemble actually split on.

        Zero means the trees are all leaves: the model carries no information, and SHAP
        on it returns zeros for every feature. That is the condition the scorecard-points
        fallback exists for, so it is measured rather than assumed.
        """
        gains = self.booster.feature_importance(importance_type="split")
        return int(np.count_nonzero(gains))


def _pr_auc_repo(preds: np.ndarray, dataset: lgb.Dataset) -> tuple[str, float, bool]:
    """PR-AUC as a LightGBM feval, computed by this repository's own function.

    The signature is ``(predictions, Dataset)`` because that is what LightGBM calls a
    feval with -- a labels-first signature raises TypeError on the first boosting round,
    which is the kind of bug that only exists until the tuning path is actually run.
    ``higher_is_better=True`` is what makes early stopping maximise it. Its value is
    cross-checked against LightGBM's native ``average_precision`` after the fit: the
    number reported as the gate's headline must be the same quantity the fit stopped on.
    """
    from oxbow.models.evaluate import average_precision

    return "pr_auc_repo", float(average_precision(dataset.get_label(), preds)), True


def _validation_metric(booster: lgb.Booster, metric: str) -> float:
    """One metric out of ``best_score``, whose shape is ``{dataset: {metric: value}}``."""
    scores = getattr(booster, "best_score", None)
    if not scores:
        return float("nan")
    try:
        block = scores["validation"]
    except KeyError:
        return float("nan")
    if isinstance(block, Mapping):
        value = block.get(metric, float("nan"))
        return float(value) if isinstance(value, int | float) else float("nan")
    return float(block)


def fit_gbm(
    train_frame: pl.DataFrame,
    valid_frame: pl.DataFrame,
    feature_names: tuple[str, ...],
    categorical_features: tuple[str, ...],
    cfg: GbmConfig,
    seed: int,
    params_override: Mapping[str, float] | None = None,
) -> GbmBundle:
    """Fit the booster with early stopping on validation PR-AUC.

    The validation slice is used *only* as the stopping set here; it is also the
    calibration set, which is the correct order of operations: the model stops where it
    is best at ranking, and the calibrator then maps scores onto probabilities.
    """
    if not feature_names:
        raise ModelLayerError("no features to fit the gbm on")
    x_train = _matrix(train_frame, feature_names, categorical_features)
    y_train = train_frame.get_column(COL_LABEL).cast(pl.Int32).to_numpy().astype(np.int32)
    x_valid = _matrix(valid_frame, feature_names, categorical_features)
    y_valid = valid_frame.get_column(COL_LABEL).cast(pl.Int32).to_numpy().astype(np.int32)
    if int(y_train.sum()) < MIN_POSITIVES_FOR_FIT:
        raise ModelLayerError(
            f"training slice carries {int(y_train.sum())} positives; a binary booster "
            "cannot be fitted on that and the run should say so rather than emit noise"
        )
    if int(y_valid.sum()) == 0:
        raise ModelLayerError(
            "the early-stopping slice has zero positives, so validation PR-AUC is undefined "
            "and early stopping would run to the full estimator count by default"
        )

    weight = scale_pos_weight_from_prior(y_train)
    params: dict[str, object] = dict(cfg.lgb_params)
    params["seed"] = seed
    params["data_random_seed"] = seed
    params["bagging_seed"] = seed
    params["feature_fraction_seed"] = seed
    params["thread_seed"] = seed
    params["scale_pos_weight"] = weight
    if params_override:
        params.update(dict(params_override))

    # Only the declared categoricals whose codes are admissible category INDICES are named
    # to lightgbm. A declared categorical whose column is string-coded arrives here as an
    # FNV-1a hash (2.4e9-magnitude), and lightgbm sizes its per-category structures by the
    # largest index, not by the category count: measured 2026-09-27, that coding took a
    # 29 MB fold-1 frame from 468 MB to 7,339 MB of RSS before `bad allocation` killed folds
    # 1-4 of run 01M3FZ2GC3J71AYT1QEKPWEDKJ. The filtered list is the guard, and what it
    # dropped is published on the bundle as ``lightgbm_categorical_features`` beside the
    # declared list rather than left as a silent narrowing of the model.
    admissible_categoricals = categorical_for_lightgbm(
        train_frame, feature_names, categorical_features
    )
    train_data = lgb.Dataset(
        x_train,
        label=y_train,
        feature_name=list(feature_names),
        categorical_feature=list(admissible_categoricals) if admissible_categoricals else "auto",
        free_raw_data=False,
    )
    valid_data = lgb.Dataset(
        x_valid,
        label=y_valid,
        reference=train_data,
        feature_name=list(feature_names),
        categorical_feature=list(admissible_categoricals) if admissible_categoricals else "auto",
        free_raw_data=False,
    )
    # Per-round validation metrics are captured by a callback: ``booster.evaluation_result``
    # is empty after ``train`` returns, so reading it would publish an empty training curve
    # and the validation page would show a chart with no line in it.
    history: list[dict[str, float]] = []

    def _field(env: object, name: str) -> object:
        """Read a callback field by attribute or key.

        LightGBM's callback environment is a ``CallbackEnv`` named tuple in 4.x and was a
        plain dict in older 3.x releases; the validation page needs the curve from either,
        and a version-dependent AttributeError inside a training loop is a miserable thing
        to debug at 2000 boosting rounds.
        """
        if hasattr(env, name):
            return getattr(env, name)
        try:
            return env[name]  # type: ignore[index]
        except (TypeError, KeyError) as exc:
            raise ModelLayerError(f"lightgbm callback environment has no {name!r} field") from exc

    def _record_round(env: object) -> None:
        row: dict[str, float] = {"iteration": float(_field(env, "iteration"))}
        for name, metric, value, _higher in _field(env, "evaluation_result_list"):  # type: ignore[misc]
            row[f"{name}_{metric}"] = float(value)
        history.append(row)

    callbacks = [
        _record_round,
        lgb.early_stopping(stopping_rounds=cfg.early_stopping_rounds, verbose=False),
        lgb.log_evaluation(period=0),
    ]
    booster = lgb.train(
        params,
        train_data,
        num_boost_round=cfg.n_estimators,
        valid_sets=[valid_data],
        valid_names=["validation"],
        feval=_pr_auc_repo,
        callbacks=callbacks,
    )
    best_iteration = int(getattr(booster, "best_iteration", 0) or 0)
    native_pr_auc = _validation_metric(booster, cfg.eval_metric)
    repo_pr_auc = _validation_metric(booster, "pr_auc_repo")
    if (
        np.isfinite(native_pr_auc)
        and np.isfinite(repo_pr_auc)
        and abs(native_pr_auc - repo_pr_auc) > 1e-6
    ):
        raise ModelLayerError(
            f"lightgbm's native {cfg.eval_metric} ({native_pr_auc:.8f}) disagrees with this "
            f"repository's average_precision ({repo_pr_auc:.8f}) on the same validation "
            "slice. The gate is reported as one number, so the two definitions must not be "
            "two different quantities."
        )
    return GbmBundle(
        booster=booster,
        feature_names=feature_names,
        categorical_features=tuple(categorical_features),
        params=params,
        scale_pos_weight=weight,
        train_prior=float(np.mean(y_train)),
        best_iteration=best_iteration,
        best_valid_pr_auc=native_pr_auc if np.isfinite(native_pr_auc) else repo_pr_auc,
        n_estimators_requested=cfg.n_estimators,
        seed=seed,
        history=tuple(history),
        degenerate=best_iteration == 0 or _split_count(booster) == 0,
        lightgbm_categorical_features=tuple(admissible_categoricals),
    )


def _split_count(booster: lgb.Booster) -> int:
    gains = booster.feature_importance(importance_type="split")
    return int(np.count_nonzero(gains))


def _matrix(
    frame: pl.DataFrame, feature_names: tuple[str, ...], categorical_features: tuple[str, ...]
) -> np.ndarray:
    """The feature matrix, via the one encoder this layer shares with the scorecard frame."""
    from oxbow.models.inputs import feature_matrix

    return feature_matrix(frame, feature_names, categorical_features)


__all__ = [
    "MODEL_ARTIFACT_FILENAME",
    "GbmBundle",
    "fit_gbm",
    "scale_pos_weight_from_prior",
]
