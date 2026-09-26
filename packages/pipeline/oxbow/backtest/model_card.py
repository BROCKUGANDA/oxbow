"""The model-card payload the validation page and ``MODEL_CARD.md`` are generated from.

WHY GENERATED, NOT WRITTEN (plan §15): "Docs rendered from ``make eval`` output, not
written from intent." A model card typed by hand restates what the author meant; this one
restates what the harness measured, so the two cannot drift. Every figure here is pulled
off a :class:`~oxbow.backtest.harness.BacktestRun`, and every money figure is emitted as
an integer ``*_minor`` field beside its currency and its recovery-band assumption, never as
a lone rendered number (plan §11/§18).

THE THREE PLAN §12 LABELS THIS MODULE MUST CARRY:
  * the risk-adjusted benefit ratio with its ``is_sharpe_ratio: false`` flag and its
    formula shown — ``test_label_not_sharpe`` reads the label, the flag and the formula
    from the exact payload written here, so this is the third of the three surfaces
    (output JSON, this model card, the demo script) that must all agree;
  * the multiple-testing caveat, derived from the number of configurations actually
    evaluated rather than asserted in prose;
  * the disclaimer, verbatim, so the model card cannot be shown without it.
"""

from __future__ import annotations

from typing import Any

from oxbow.backtest.ablation import ROW_IDS
from oxbow.backtest.harness import BacktestRun
from oxbow.backtest.serialize import DISCLAIMER


def _ablation_table(run: BacktestRun) -> list[dict[str, Any]]:
    """One row per variant: PR-AUC with its CI, net benefit in money, and the label.

    The table is rendered from ``run.variants`` so it always has exactly the eight rows
    (plus any control arm) that actually ran — the ablation and the model card cannot
    disagree because there is a single source.
    """
    rows: list[dict[str, Any]] = []
    for variant in run.variants:
        primary = variant.policies.get("ev_greedy") or variant.policies.get("score_threshold")
        if primary is None:
            continue
        rows.append(
            {
                "row_id": variant.row_id,
                "label": variant.label,
                "question": variant.question,
                "corpus": variant.corpus,
                "provenance": variant.provenance,
                "is_control": variant.is_control,
                "control_note": variant.control_note,
                "pr_auc": primary.pr_auc,
                "pr_auc_ci_low": primary.pr_auc_ci_low,
                "pr_auc_ci_high": primary.pr_auc_ci_high,
                "auroc_comparability_only": primary.auroc,
                "brier": primary.brier,
                "net_benefit_total_minor": primary.net_benefit_total_minor,
                "currency": run.config.currency,
            }
        )
    return rows


def build_model_card_payload(run: BacktestRun) -> dict[str, Any]:
    """Assemble the model-card payload from a completed backtest run."""
    headline = _headline_variant(run)
    config = run.config
    benefit_ratio = config.benefit_ratio
    payload: dict[str, Any] = {
        "artifact": "oxbow-model-card-v1",
        "disclaimer": DISCLAIMER,
        "corpus": run.corpus,
        "metrics_per_corpus_never_averaged": True,
        "metrics_note": (
            "All figures are per corpus. PaySim (tabular/volume, Module A) and IBM-AML "
            "(network/typology, Module B) are reported separately and never averaged into "
            "one headline (DEV-011/DEV-013, plan §6)."
        ),
        "seed": config.seed,
        "embargo_days": config.embargo_days,
        "max_lookback_days": config.max_lookback_days,
        "which_split_was_optimised_on": run.split_report_line,
        "walk_forward": {
            "scheme": "expanding_window_temporal",
            "shuffle": False,
            "corpus_feasibility": run.feasibility,
        },
        "headline": _headline_block(headline, config),
        "benefit_ratio": {
            "label": benefit_ratio.label,
            "formula": benefit_ratio.formula,
            "is_sharpe_ratio": benefit_ratio.is_sharpe_ratio,
            "explicitly_not_sharpe_because": (
                "no risk-free rate is subtracted and there is no annualisation; it is a "
                "mean-over-std ratio of per-fold net benefit, shown with its formula"
            ),
            "value": headline.policies[_primary_policy_name(headline)].risk_adjusted_ratio
            if headline
            else None,
        },
        "benefit_per_analyst_hour_minor": (
            headline.policies[_primary_policy_name(headline)].benefit_per_analyst_hour_minor
            if headline
            else None
        ),
        "currency": config.currency,
        "economics_assumption_line": _assumption_line(config),
        "ablation_table": _ablation_table(run),
        "per_typology_recall": _typology_block(headline),
        "fairness": headline.fairness if headline else {},
        "perturbations": headline.perturbations if headline else {},
        "overfitting_controls": {
            "configs_evaluated": run.configs_evaluated,
            "selection_on": "validation",
            "test_fold_touched_once": run.test_fold_touched_once,
            "test_fold_touched_at": run.test_fold_touched_at,
            "multiple_testing_caveat": (
                f"{run.configs_evaluated} configurations were evaluated against this corpus; "
                "with N configurations tried the best validation result is optimistically "
                "biased, which is exactly why the headline figure is taken from the untouched "
                "test fold and not from the validation peak."
            ),
        },
        "mlflow": run.mlflow,
        "expected_ablation_row_ids": list(ROW_IDS),
    }
    return payload


def _primary_policy_name(variant: Any) -> str:
    for name in ("ev_greedy", "score_threshold"):
        if name in variant.policies:
            return name
    return next(iter(variant.policies))


def _headline_variant(run: BacktestRun) -> Any:
    honest = [v for v in run.variants if not v.is_control]
    for want_id in ("full_calibrated", "gbm_with_graph", "threshold_vs_ev"):
        for variant in honest:
            if variant.row_id == want_id:
                return variant
    return honest[0] if honest else (run.variants[0] if run.variants else None)


def _headline_block(variant: Any, config: Any) -> dict[str, Any]:
    if variant is None:
        return {}
    primary = variant.policies[_primary_policy_name(variant)]
    return {
        "corpus": variant.corpus,
        "provenance": variant.provenance,
        "model_version": variant.model_version,
        "feature_spec_hash": variant.feature_spec_hash,
        "pr_auc": primary.pr_auc,
        "pr_auc_ci_low": primary.pr_auc_ci_low,
        "pr_auc_ci_high": primary.pr_auc_ci_high,
        "net_benefit_total_minor": primary.net_benefit_total_minor,
        "currency": config.currency,
        "band_note": (
            "Recovery-rate band r in "
            f"{config.recovery_rate} (sensitivity over r=0.20/0.35/0.50 per config/economics.yaml)"
        ),
    }


def _assumption_line(config: Any) -> str:
    return (
        "Every currency figure is a model estimate under the assumptions in "
        "config/economics.yaml: recovery rate r, per-minute analyst cost, friction cost f, "
        "and exposure-at-risk definition. Illustrative, not measured outcomes. "
        f"Currency {config.currency} in integer minor units."
    )


def _typology_block(variant: Any) -> dict[str, Any]:
    if variant is None:
        return {}
    primary = variant.policies[_primary_policy_name(variant)]
    per_fold = [fold.typology_recall for fold in primary.folds if fold.typology_recall]
    pooled: dict[str, list[float]] = {}
    for fold_map in per_fold:
        for typology, value in fold_map.items():
            pooled.setdefault(typology, []).append(value)
    mean_recall = {t: sum(v) / len(v) for t, v in sorted(pooled.items())}
    return {
        "provenance": variant.provenance,
        "source": "data/processed/ibm_typologies.parquet join (DEV-014)",
        "recall_by_typology_mean_over_folds": mean_recall,
        "negative_control_note": (
            "RANDOM-labelled attempts are the free negative control (DEV-014): high recall "
            "there next to low CYCLE recall would indicate point-anomaly detection rather "
            "than network detection."
        ),
    }


__all__ = ["build_model_card_payload"]
