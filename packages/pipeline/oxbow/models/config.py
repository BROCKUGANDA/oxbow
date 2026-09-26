"""Strict readers for ``config/model.yaml`` and ``config/splits.yaml``.

The same contract as ``oxbow.config``: a missing or mistyped key is a load-time
error naming its path, never a silent default. This matters more here than
anywhere else in the phase, because the numbers these keys control decide which
branch runs -- isotonic above 500 validation positives, Platt above the floor,
**refusal below it**. A defaulted floor would quietly turn a refusal into a
probability, and Module C multiplies probabilities by money.

``config/splits.yaml`` is read, never written: P2 owns the splits module and P6 owns
the harness. P4 needs three declared facts from it -- the fold count, the
validation fraction and the reporting budgets (bootstrap resamples, seed list,
zero-positive policy) -- and reading them keeps a second copy of those numbers from
existing here and drifting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from oxbow.config import ConfigError, find_repo_root, load_yaml
from oxbow.scoring.config import (
    require_bool,
    require_float,
    require_int,
    require_optional_float,
    require_str,
    require_str_list,
)

MODEL_FILENAME: Final = "model.yaml"
SPLITS_FILENAME: Final = "splits.yaml"
CONFIG_DIRNAME: Final = "config"
EXPECTED_SEED: Final = 1337


def _node(mapping: Mapping[str, object], path: str) -> object:
    node: object = mapping
    walked: list[str] = []
    for part in path.split("."):
        walked.append(part)
        if not isinstance(node, Mapping) or part not in node:
            raise ConfigError(f"config key '{'.'.join(walked)}' is missing")
        node = node[part]
    return node


def require_int_list(mapping: Mapping[str, object], path: str) -> tuple[int, ...]:
    """Read a list of ints (the seed list, the search-space bounds)."""
    value = _node(mapping, path)
    if not isinstance(value, list):
        raise ConfigError(f"config key '{path}' must be a list, got {type(value).__name__}")
    out: list[int] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"config key '{path}[{index}]' must be an int")
        out.append(item)
    return tuple(out)


# The parameter names LightGBM declares as integers. Not a convenience list: LightGBM
# raises on a float where it wants an int, and YAML reads every one of these as a float
# unless the code says otherwise.
LIGHTGBM_INTEGER_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "num_leaves",
        "max_depth",
        "min_child_samples",
        "min_child_weight",
        "min_data_in_bin",
        "min_sum_hessian_in_leaf",
        "subsample_freq",
        "max_bin",
        "seed",
        "num_threads",
        "num_iterations",
        "feature_fraction_seed",
        "bagging_seed",
        "data_random_seed",
        "thread_seed",
        "extra_seed",
        "random_state",
        "verbose",
    }
)


def _as_declared_type(name: str, value: object) -> object:
    """Coerce a config value to the type LightGBM declares for that parameter."""
    if name not in LIGHTGBM_INTEGER_PARAMS or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ConfigError(
                f"gbm.params.{name}={value!r} is not a whole number, and LightGBM types "
                "this parameter as an integer. Refusing to truncate it into a different model."
            )
        return int(value)
    raise ConfigError(f"gbm.params.{name}={value!r} is not a number, and LightGBM wants one here")


@dataclass(frozen=True, slots=True)
class GbmConfig:
    """LightGBM settings, including the determinism pins."""

    library: str
    objective: str
    deterministic: bool
    force_row_wise: bool
    scale_pos_weight: str
    early_stopping_rounds: int
    eval_metric: str
    n_estimators: int
    num_threads: int
    params: Mapping[str, float]
    min_data_in_leaf_note: str

    @property
    def lgb_params(self) -> dict[str, object]:
        """The parameter dict handed to ``lightgbm.train``.

        ``num_threads`` is pinned to 1 rather than left to the machine: LightGBM's
        own documented condition for ``deterministic=true`` is single-threaded
        histogram building, otherwise two runs on this box would differ and
        ``make verify-determinism`` becomes a coin toss.

        YAML parses ``max_depth: -1`` and ``num_leaves: 31`` as floats, and LightGBM
        rejects a float where it declares an int ("Parameter max_depth should be of type
        int, got -1.0"). So the keys LightGBM types as integers are coerced here, at the
        only boundary where config becomes a training call -- and a non-integral value
        raises rather than being silently truncated, because ``num_leaves: 31.5`` is a
        typo someone should hear about, not a tree shape decided by truncation.
        """
        built: dict[str, object] = {
            "objective": self.objective,
            "deterministic": self.deterministic,
            "force_row_wise": self.force_row_wise,
            "num_threads": self.num_threads,
            "metric": self.eval_metric,
            "verbose": -1,
        }
        built.update({key: _as_declared_type(key, value) for key, value in self.params.items()})
        return built


@dataclass(frozen=True, slots=True)
class OptunaConfig:
    """The declared tuning budget. Every trial is logged because the count of
    configurations tried is itself a reported number: selection on validation with
    N trials biases the best result optimistically."""

    n_trials: int
    sampler: str
    seed: int
    log_every_trial: bool
    study_name: str
    direction: str
    search_space: Mapping[str, Mapping[str, object]]
    timeout_seconds: float | None


@dataclass(frozen=True, slots=True)
class AnomalyConfig:
    """Isolation Forest settings and its normalisation rule."""

    library: str
    model: str
    n_estimators: int
    contamination: str
    max_samples: str
    random_state: int
    normalisation: str
    fit_on: str


@dataclass(frozen=True, slots=True)
class FusionConfig:
    """The constrained non-negative meta-learner."""

    method: str
    fitted_on: str
    non_negative: bool
    inputs: tuple[str, ...]
    regularisation_strength: float
    show_coefficients: bool
    solver: str
    max_iter: int
    intercept: str
    tolerance: float


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    """Calibration, its floor, and the prior-correction choice."""

    method: str
    isotonic_min_positives: int
    fallback_method: str
    below_floor_action: str
    min_positives_for_calibration: int
    oversampling_correction: str
    base_rate_preserving_holdout: bool
    population_base_rate: float | None
    reliability_bins: int
    report: tuple[str, ...]
    oversampling_tolerance_multiple: float


@dataclass(frozen=True, slots=True)
class ShapConfig:
    """TreeExplainer persistence and the degenerate-tree fallback."""

    explainer: str
    precompute: bool
    persist_per_row: bool
    on_degenerate_tree: str
    degenerate_max_abs_contribution: float
    expected_value_column: str
    persist_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DriftGuardConfig:
    """The drift thresholds P4b enforces, and the feature-hash guard."""

    psi_watch: float
    psi_action: float
    on_action: str
    enforce_feature_hash_match: bool


@dataclass(frozen=True, slots=True)
class MlflowConfig:
    """Tracking settings, with the offline path spelled out.

    ``tracking_uri`` points at the Compose server; this prototype must also run with
    nothing up (02 §0: null adapters are the demo default), so ``local_store_dir`` is
    the file-store fallback and the payload records which one was used. Silently
    pretending the server was reached is the failure mode avoided here.
    """

    tracking_uri: str
    experiment: str
    log_models: bool
    log_shap: bool
    log_fold_touch_timestamps: bool
    local_store_dir: str
    registered_model_prefix: str
    offline_fallback_allowed: bool


@dataclass(frozen=True, slots=True)
class BaselinesConfig:
    """The two references the models must beat."""

    rules_only_enabled: bool
    rules_only_score: str
    scorecard_only_enabled: bool
    agreement_top_n_disagreements: int
    agreement_matrix_bands: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportingConfig:
    """Where artefacts go and how they are hashed."""

    artifact_dir: str
    hash_artifacts: bool
    scored_output_filename: str
    tie_break_score_column: str


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Everything P4b reads from model.yaml, validated."""

    root: Path
    seed: int
    gbm: GbmConfig
    optuna: OptunaConfig
    anomaly: AnomalyConfig
    fusion: FusionConfig
    calibration: CalibrationConfig
    shap: ShapConfig
    drift: DriftGuardConfig
    mlflow: MlflowConfig
    baselines: BaselinesConfig
    reporting: ReportingConfig
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class SplitConfig:
    """The declared split and reporting facts P4b must honour rather than restate."""

    root: Path
    seed: int
    n_folds: int
    scheme: str
    embargo_days: int
    purge: bool
    shuffle: bool
    on_zero_positive_fold: str
    validation_fraction_of_train: float
    touch_test_fold_once: bool
    bootstrap_resamples: int
    bootstrap_confidence: float
    seed_stability_seeds: tuple[int, ...]
    seed_stability_report: tuple[str, ...]
    entity_disjoint_enabled: bool
    entity_disjoint_report_as: str


def load_model_config(root: Path | None = None) -> ModelConfig:
    """Load and validate ``config/model.yaml``."""
    repo_root = (root or find_repo_root()).resolve()
    raw = load_yaml(repo_root / CONFIG_DIRNAME / MODEL_FILENAME)

    seed = require_int(raw, "seed")
    if seed != EXPECTED_SEED:
        raise ConfigError(
            f"model.yaml seed is {seed}; the project contract fixes 1337 (01 A rule 4)"
        )

    gbm_raw = _node(raw, "gbm")
    if not isinstance(gbm_raw, Mapping):
        raise ConfigError("gbm must be a mapping")
    params_raw = _node(gbm_raw, "params")
    if not isinstance(params_raw, Mapping):
        raise ConfigError("gbm.params must be a mapping")
    params: dict[str, float] = {}
    for key, value in params_raw.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"gbm.params.{key} must be numeric")
        params[str(key)] = float(value)
    gbm = GbmConfig(
        library=require_str(gbm_raw, "library"),
        objective=require_str(gbm_raw, "objective"),
        deterministic=require_bool(gbm_raw, "deterministic"),
        force_row_wise=require_bool(gbm_raw, "force_row_wise"),
        scale_pos_weight=require_str(gbm_raw, "scale_pos_weight"),
        early_stopping_rounds=require_int(gbm_raw, "early_stopping_rounds"),
        eval_metric=require_str(gbm_raw, "eval_metric"),
        n_estimators=require_int(gbm_raw, "n_estimators"),
        num_threads=require_int(gbm_raw, "num_threads"),
        params=params,
        min_data_in_leaf_note=require_str(gbm_raw, "determinism_note"),
    )
    if not gbm.deterministic:
        raise ConfigError(
            "gbm.deterministic is false; two runs must produce identical model artefacts "
            "and the phase requires that they be hashed and compared"
        )
    if gbm.num_threads != 1:
        raise ConfigError(
            "gbm.num_threads must be 1: lightgbm's deterministic=true only guarantees "
            "bitwise equality under single-threaded histogram building"
        )
    if gbm.scale_pos_weight != "from_train_prior":
        raise ConfigError("gbm.scale_pos_weight must be 'from_train_prior', never hand-set")

    optuna_raw = _node(raw, "optuna")
    if not isinstance(optuna_raw, Mapping):
        raise ConfigError("optuna must be a mapping")
    space_raw = _node(optuna_raw, "search_space")
    if not isinstance(space_raw, Mapping):
        raise ConfigError("optuna.search_space must be a mapping")
    space: dict[str, dict[str, object]] = {}
    for key, value in space_raw.items():
        if not isinstance(value, Mapping):
            raise ConfigError(f"optuna.search_space.{key} must be a mapping")
        space[str(key)] = dict(value)
    optuna = OptunaConfig(
        n_trials=require_int(optuna_raw, "n_trials"),
        sampler=require_str(optuna_raw, "sampler"),
        seed=require_int(optuna_raw, "seed"),
        log_every_trial=require_bool(optuna_raw, "log_every_trial"),
        study_name=require_str(optuna_raw, "study_name"),
        direction=require_str(optuna_raw, "direction"),
        search_space=space,
        timeout_seconds=require_optional_float(optuna_raw, "timeout_seconds"),
    )
    if optuna.seed != seed:
        raise ConfigError("optuna.seed must equal the project seed")
    if optuna.direction != "maximize":
        raise ConfigError("optuna.direction must be 'maximize' (validation PR-AUC)")

    anomaly_raw = _node(raw, "anomaly")
    if not isinstance(anomaly_raw, Mapping):
        raise ConfigError("anomaly must be a mapping")
    anomaly = AnomalyConfig(
        library=require_str(anomaly_raw, "library"),
        model=require_str(anomaly_raw, "model"),
        n_estimators=require_int(anomaly_raw, "n_estimators"),
        contamination=require_str(anomaly_raw, "contamination"),
        max_samples=require_str(anomaly_raw, "max_samples"),
        random_state=require_int(anomaly_raw, "random_state"),
        normalisation=require_str(anomaly_raw, "normalisation"),
        fit_on=require_str(anomaly_raw, "fit_on"),
    )
    if anomaly.normalisation != "validation_percentile_rank":
        raise ConfigError(
            "anomaly.normalisation must be 'validation_percentile_rank': a min-max fitted on "
            "the data being scored leaks the scored population into the score"
        )
    if anomaly.random_state != seed:
        raise ConfigError("anomaly.random_state must equal the project seed")

    fusion_raw = _node(raw, "fusion")
    if not isinstance(fusion_raw, Mapping):
        raise ConfigError("fusion must be a mapping")
    fusion = FusionConfig(
        method=require_str(fusion_raw, "method"),
        fitted_on=require_str(fusion_raw, "fitted_on"),
        non_negative=require_bool(fusion_raw, "non_negative"),
        inputs=require_str_list(fusion_raw, "inputs"),
        regularisation_strength=require_float(fusion_raw, "regularisation_strength"),
        show_coefficients=require_bool(fusion_raw, "show_coefficients"),
        solver=require_str(fusion_raw, "solver"),
        max_iter=require_int(fusion_raw, "max_iter"),
        intercept=require_str(fusion_raw, "intercept"),
        tolerance=require_float(fusion_raw, "tolerance"),
    )
    if fusion.fitted_on != "validation":
        raise ConfigError("fusion.fitted_on must be 'validation'")
    if not fusion.non_negative:
        raise ConfigError(
            "fusion.non_negative is false: a negative weight on a risk channel would make a "
            "stronger signal lower the fused score, which is not explicable in one line"
        )
    if fusion.method != "constrained_logistic":
        raise ConfigError("fusion.method must be 'constrained_logistic'")

    calibration_raw = _node(raw, "calibration")
    if not isinstance(calibration_raw, Mapping):
        raise ConfigError("calibration must be a mapping")
    calibration = CalibrationConfig(
        method=require_str(calibration_raw, "method"),
        isotonic_min_positives=require_int(calibration_raw, "isotonic_min_positives"),
        fallback_method=require_str(calibration_raw, "fallback_method"),
        below_floor_action=require_str(calibration_raw, "below_floor_action"),
        min_positives_for_calibration=require_int(calibration_raw, "min_positives_for_calibration"),
        oversampling_correction=require_str(calibration_raw, "oversampling_correction"),
        base_rate_preserving_holdout=require_bool(calibration_raw, "base_rate_preserving_holdout"),
        population_base_rate=require_optional_float(calibration_raw, "population_base_rate"),
        reliability_bins=require_int(calibration_raw, "reliability_bins"),
        report=require_str_list(calibration_raw, "report"),
        oversampling_tolerance_multiple=require_float(
            calibration_raw, "oversampling_tolerance_multiple"
        ),
    )
    if calibration.min_positives_for_calibration > calibration.isotonic_min_positives:
        raise ConfigError(
            "calibration.min_positives_for_calibration must be <= isotonic_min_positives; "
            "otherwise the Platt fallback has no population to exist in"
        )
    if calibration.below_floor_action != "refuse_and_label_uncalibrated":
        raise ConfigError(
            "calibration.below_floor_action must be 'refuse_and_label_uncalibrated': an "
            "uncalibrated probability multiplied by money is a wrong currency figure"
        )
    if calibration.oversampling_correction not in {
        "king_zeng_intercept",
        "base_rate_preserving_holdout",
    }:
        raise ConfigError(
            "calibration.oversampling_correction must name exactly one of "
            "king_zeng_intercept / base_rate_preserving_holdout"
        )

    shap_raw = _node(raw, "shap")
    if not isinstance(shap_raw, Mapping):
        raise ConfigError("shap must be a mapping")
    shap = ShapConfig(
        explainer=require_str(shap_raw, "explainer"),
        precompute=require_bool(shap_raw, "precompute"),
        persist_per_row=require_bool(shap_raw, "persist_per_row"),
        on_degenerate_tree=require_str(shap_raw, "on_degenerate_tree"),
        degenerate_max_abs_contribution=require_float(shap_raw, "degenerate_max_abs_contribution"),
        expected_value_column=require_str(shap_raw, "expected_value_column"),
        persist_columns=require_str_list(shap_raw, "persist_columns"),
    )
    if not shap.precompute or not shap.persist_per_row:
        raise ConfigError(
            "shap.precompute and shap.persist_per_row must be true: the UI reads, it never "
            "computes SHAP at request time"
        )

    drift_raw = _node(raw, "drift")
    if not isinstance(drift_raw, Mapping):
        raise ConfigError("drift must be a mapping")
    drift = DriftGuardConfig(
        psi_watch=require_float(drift_raw, "psi_watch"),
        psi_action=require_float(drift_raw, "psi_action"),
        on_action=require_str(drift_raw, "on_action"),
        enforce_feature_hash_match=require_bool(drift_raw, "enforce_feature_hash_match"),
    )
    if not drift.enforce_feature_hash_match:
        raise ConfigError(
            "drift.enforce_feature_hash_match cannot be false: version skew means scoring "
            "with columns the model never saw"
        )

    mlflow_raw = _node(raw, "mlflow")
    if not isinstance(mlflow_raw, Mapping):
        raise ConfigError("mlflow must be a mapping")
    mlflow = MlflowConfig(
        tracking_uri=require_str(mlflow_raw, "tracking_uri"),
        experiment=require_str(mlflow_raw, "experiment"),
        log_models=require_bool(mlflow_raw, "log_models"),
        log_shap=require_bool(mlflow_raw, "log_shap"),
        log_fold_touch_timestamps=require_bool(mlflow_raw, "log_fold_touch_timestamps"),
        local_store_dir=require_str(mlflow_raw, "local_store_dir"),
        registered_model_prefix=require_str(mlflow_raw, "registered_model_prefix"),
        offline_fallback_allowed=require_bool(mlflow_raw, "offline_fallback_allowed"),
    )

    baselines_raw = _node(raw, "baselines")
    if not isinstance(baselines_raw, Mapping):
        raise ConfigError("baselines must be a mapping")
    baselines = BaselinesConfig(
        rules_only_enabled=require_bool(baselines_raw, "rules_only.enabled"),
        rules_only_score=require_str(baselines_raw, "rules_only.score"),
        scorecard_only_enabled=require_bool(baselines_raw, "scorecard_only.enabled"),
        agreement_top_n_disagreements=require_int(baselines_raw, "agreement_top_n_disagreements"),
        agreement_matrix_bands=require_str_list(baselines_raw, "agreement_matrix_bands"),
    )

    reporting_raw = _node(raw, "reporting")
    if not isinstance(reporting_raw, Mapping):
        raise ConfigError("reporting must be a mapping")
    reporting = ReportingConfig(
        artifact_dir=require_str(reporting_raw, "artifact_dir"),
        hash_artifacts=require_bool(reporting_raw, "hash_artifacts"),
        scored_output_filename=require_str(reporting_raw, "scored_output_filename"),
        tie_break_score_column=require_str(reporting_raw, "tie_break_score_column"),
    )

    return ModelConfig(
        root=repo_root,
        seed=seed,
        gbm=gbm,
        optuna=optuna,
        anomaly=anomaly,
        fusion=fusion,
        calibration=calibration,
        shap=shap,
        drift=drift,
        mlflow=mlflow,
        baselines=baselines,
        reporting=reporting,
        raw=dict(raw),
    )


def load_split_config(root: Path | None = None) -> SplitConfig:
    """Read the declared split and reporting facts from ``config/splits.yaml``.

    Read-only by design. The bootstrap resample count and the seed list are P0/P6
    declarations that P4b must *use*, not re-declare: two copies of a number is one
    too many, and the report has to be able to say which config line produced it.
    """
    repo_root = (root or find_repo_root()).resolve()
    raw = load_yaml(repo_root / CONFIG_DIRNAME / SPLITS_FILENAME)

    walk = _node(raw, "walk_forward")
    if not isinstance(walk, Mapping):
        raise ConfigError("walk_forward must be a mapping")
    validation = _node(raw, "validation")
    if not isinstance(validation, Mapping):
        raise ConfigError("validation must be a mapping")
    report = _node(raw, "report")
    if not isinstance(report, Mapping):
        raise ConfigError("report must be a mapping")
    bootstrap = _node(report, "bootstrap")
    if not isinstance(bootstrap, Mapping):
        raise ConfigError("report.bootstrap must be a mapping")
    stability = _node(report, "seed_stability")
    if not isinstance(stability, Mapping):
        raise ConfigError("report.seed_stability must be a mapping")
    entity = _node(raw, "entity_disjoint")
    if not isinstance(entity, Mapping):
        raise ConfigError("entity_disjoint must be a mapping")

    config = SplitConfig(
        root=repo_root,
        seed=require_int(raw, "seed"),
        n_folds=require_int(walk, "n_folds"),
        scheme=require_str(walk, "scheme"),
        embargo_days=require_int(walk, "embargo_days"),
        purge=require_bool(walk, "purge"),
        shuffle=require_bool(walk, "shuffle"),
        on_zero_positive_fold=require_str(walk, "on_zero_positive_fold"),
        validation_fraction_of_train=require_float(validation, "fraction_of_train"),
        touch_test_fold_once=require_bool(validation, "touch_test_fold_once"),
        bootstrap_resamples=require_int(bootstrap, "resamples"),
        bootstrap_confidence=require_float(bootstrap, "confidence"),
        seed_stability_seeds=require_int_list(stability, "seeds"),
        seed_stability_report=require_str_list(stability, "report"),
        entity_disjoint_enabled=require_bool(entity, "enabled"),
        entity_disjoint_report_as=require_str(entity, "report_as"),
    )
    if config.shuffle:
        raise ConfigError(
            "walk_forward.shuffle must be false: a random split on temporal "
            "data is the classic tell and a leakage source"
        )
    if config.on_zero_positive_fold != "skip_with_named_reason":
        raise ConfigError(
            "walk_forward.on_zero_positive_fold must be 'skip_with_named_reason' (03 H: "
            "test_zero_positive_fold_reported)"
        )
    if len(config.seed_stability_seeds) < 2:
        raise ConfigError("report.seed_stability.seeds needs at least two seeds")
    return config


__all__ = [
    "AnomalyConfig",
    "BaselinesConfig",
    "CalibrationConfig",
    "DriftGuardConfig",
    "FusionConfig",
    "GbmConfig",
    "MlflowConfig",
    "ModelConfig",
    "OptunaConfig",
    "ReportingConfig",
    "ShapConfig",
    "SplitConfig",
    "load_model_config",
    "load_split_config",
    "require_int_list",
]
