"""Typed, fail-loud load of every ``config/`` value the backtest consumes.

00 §G: every tunable lives in config/, not code. The harness must not carry its own
copy of the bootstrap resample count, the tail alphas or the recovery rate — plan §12
and the ``economics.yaml`` header both insist a second record of one fact that can
drift is the defect. So one loader reads the four files and this is the only place
their backtest-facing subset is named.

The loader mirrors ``oxbow.config``'s discipline: a missing key raises at the
boundary, it never defaults. A backtest that quietly filled in ``bootstrap=100``
because the key was mistyped would report a confidence interval nobody configured.

MONEY (DEV-005): ``friction_cost_minor``, ``capacity`` and the analyst cost are read
as integers; ``recovery_rate`` is a genuine ratio and stays a float. The economic
figures themselves are still produced through ``oxbow.quant.money`` integer
arithmetic — this module only says *which* recovery rate, capacity and confidence
levels the harness is allowed to apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from oxbow.config import ConfigError, find_repo_root, load_yaml

CONFIG_DIR: Final = "config"
SEED: Final = 1337


def _require(mapping: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Walk a nested mapping, naming the missing key instead of returning None."""
    node: Any = mapping
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise ConfigError(f"config is missing required key: {'.'.join(path)}")
        node = node[key]
    return node


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer, got {type(value).__name__}")
    return value


def _as_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{name} must be a number, got {type(value).__name__}")
    return float(value)


def _as_int_list(value: Any, name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must be a non-empty list")
    return tuple(_as_int(item, f"{name}[]") for item in value)


@dataclass(frozen=True, slots=True)
class BenefitRatioConfig:
    """The label, formula and non-Sharpe flag for the risk-adjusted benefit ratio.

    Carried from ``config/splits.yaml`` rather than hardcoded so the three surfaces
    plan §12 names — the output JSON, the model-card payload and the demo script — all
    read the *same* label and formula string, which is what makes
    ``test_label_not_sharpe`` assert one source of truth instead of three copies.
    """

    label: str
    formula: str
    is_sharpe_ratio: bool
    show_formula: bool


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Everything the harness is allowed to vary, resolved from config/."""

    root: Path
    seed: int
    n_folds: int
    embargo_days: int
    max_lookback_days: int
    validation_fraction: float

    # statistical reporting
    bootstrap_resamples: int
    bootstrap_confidence: float
    stability_seeds: tuple[int, ...]
    reliability_bins: int

    # tail-risk Monte Carlo
    mc_draws: int
    mc_seed: int
    var_alpha: float
    es_alpha: float

    # economics (money in integer minor units; rate and capacity are config)
    currency: str
    minor_units_per_major: int
    recovery_rate: float
    friction_cost_minor: int
    capacity_minutes: int
    default_alerts_reviewed: int
    analyst_hours_per_period: int
    analyst_cost_per_minute_minor: int

    # labelling / refusal policies plan §12 names explicitly
    benefit_ratio: BenefitRatioConfig
    zero_positive_fold_policy: str
    undefined_precision_policy: str
    zero_drawdown_policy: str
    primary_comparison: str

    # leakage control arm
    leakage_control_enabled: bool
    leakage_expect_outperforms: bool
    leakage_label: str
    leakage_show_in_ui: bool

    # entity-disjoint robustness check
    entity_disjoint_enabled: bool
    entity_disjoint_holdout_fraction: float
    entity_disjoint_report_as: str
    optimised_split: str


def load_backtest_config(root: Path | None = None) -> BacktestConfig:
    """Load and cross-validate the backtest's configuration from config/.

    Raises if the embargo disagrees with the longest feature lookback: plan §8 makes
    the split module assert that the two agree, and the harness re-checks it here
    because running a backtest whose embargo is shorter than its features is the
    leakage the whole phase exists to catch.
    """
    repo = (root or find_repo_root()).resolve()
    splits = load_yaml(repo / CONFIG_DIR / "splits.yaml")
    economics = load_yaml(repo / CONFIG_DIR / "economics.yaml")
    features = load_yaml(repo / CONFIG_DIR / "features.yaml")

    seed = _as_int(_require(splits, ("seed",)), "splits.seed")
    if seed != SEED:
        raise ConfigError(
            f"splits.seed is {seed}, but the project fixes the global seed at {SEED} (01 §A rule 4)"
        )

    wf = dict(_require(splits, ("walk_forward",)))
    report = dict(_require(splits, ("report",)))
    benefit = dict(_require(splits, ("report", "benefit_ratio")))
    bootstrap = dict(_require(splits, ("report", "bootstrap")))
    stability = dict(_require(splits, ("report", "seed_stability")))
    mc = dict(_require(splits, ("report", "monte_carlo")))
    tail = dict(_require(splits, ("report", "tail_risk")))
    leakage = dict(_require(splits, ("leakage_control",)))
    entity = dict(_require(splits, ("entity_disjoint",)))
    validation = dict(_require(splits, ("validation",)))

    n_folds = _as_int(_require(wf, ("n_folds",)), "walk_forward.n_folds")
    embargo_days = _as_int(_require(wf, ("embargo_days",)), "walk_forward.embargo_days")
    if _require(wf, ("shuffle",)) is not False:
        # A random split on transaction data is the classic hackathon tell (plan §12):
        # the config is not allowed to opt into it and the harness refuses it.
        raise ConfigError("walk_forward.shuffle must be false: temporal only, never random")

    max_lookback_days = _as_int(_require(features, ("max_lookback_days",)), "max_lookback_days")
    if embargo_days != max_lookback_days:
        raise ConfigError(
            f"walk_forward.embargo_days ({embargo_days}) must equal max_lookback_days "
            f"({max_lookback_days}): an embargo shorter than the longest feature window "
            "lets a training row read test-period data (plan §8)."
        )

    return BacktestConfig(
        root=repo,
        seed=seed,
        n_folds=n_folds,
        embargo_days=embargo_days,
        max_lookback_days=max_lookback_days,
        validation_fraction=_as_float(
            _require(validation, ("fraction_of_train",)), "validation.fraction_of_train"
        ),
        bootstrap_resamples=_as_int(
            _require(bootstrap, ("resamples",)), "report.bootstrap.resamples"
        ),
        bootstrap_confidence=_as_float(
            _require(bootstrap, ("confidence",)), "report.bootstrap.confidence"
        ),
        stability_seeds=_as_int_list(_require(stability, ("seeds",)), "report.seed_stability.seeds"),
        reliability_bins=5,
        mc_draws=_as_int(_require(mc, ("runs",)), "report.monte_carlo.runs"),
        mc_seed=_as_int(_require(mc, ("seed",)), "report.monte_carlo.seed"),
        var_alpha=_as_float(_require(tail, ("var_alpha",)), "report.tail_risk.var_alpha"),
        es_alpha=_as_float(_require(tail, ("es_alpha",)), "report.tail_risk.es_alpha"),
        currency=str(_require(economics, ("currency",))),
        minor_units_per_major=_as_int(
            _require(economics, ("minor_units_per_major",)), "minor_units_per_major"
        ),
        recovery_rate=_as_float(_require(economics, ("recovery", "rate")), "recovery.rate"),
        friction_cost_minor=_as_int(
            _require(economics, ("friction_cost_minor",)), "friction_cost_minor"
        ),
        capacity_minutes=_as_int(
            _require(economics, ("capacity", "review_minutes_per_period")),
            "capacity.review_minutes_per_period",
        ),
        default_alerts_reviewed=_as_int(
            _require(economics, ("capacity", "default_alerts_reviewed")),
            "capacity.default_alerts_reviewed",
        ),
        analyst_hours_per_period=_as_int(
            _require(economics, ("analyst", "hours_per_period")), "analyst.hours_per_period"
        ),
        analyst_cost_per_minute_minor=_as_int(
            _require(economics, ("analyst", "cost_per_minute_minor")),
            "analyst.cost_per_minute_minor",
        ),
        benefit_ratio=BenefitRatioConfig(
            label=str(_require(benefit, ("label",))),
            formula=str(_require(benefit, ("formula",))),
            is_sharpe_ratio=_require(benefit, ("is_sharpe_ratio",)) is True,
            show_formula=_require(benefit, ("show_formula",)) is True,
        ),
        zero_positive_fold_policy=str(_require(wf, ("on_zero_positive_fold",))),
        undefined_precision_policy=str(_require(report, ("undefined_precision_policy",))),
        zero_drawdown_policy=str(_require(report, ("zero_drawdown_policy",))),
        primary_comparison=str(_require(tail, ("primary_comparison",))),
        leakage_control_enabled=_require(leakage, ("enabled",)) is True,
        leakage_expect_outperforms=_require(leakage, ("expect_outperforms",)) is True,
        leakage_label=str(_require(leakage, ("label",))),
        leakage_show_in_ui=_require(leakage, ("show_in_validation_ui",)) is True,
        entity_disjoint_enabled=_require(entity, ("enabled",)) is True,
        entity_disjoint_holdout_fraction=_as_float(
            _require(entity, ("holdout_fraction",)), "entity_disjoint.holdout_fraction"
        ),
        entity_disjoint_report_as=str(_require(entity, ("report_as",))),
        # Plan §7/§8: state plainly which split was optimised on. Temporal is primary;
        # the entity-disjoint split is the robustness check, never the headline.
        optimised_split="temporal_walk_forward",
    )


__all__ = ["BacktestConfig", "BenefitRatioConfig", "load_backtest_config"]
