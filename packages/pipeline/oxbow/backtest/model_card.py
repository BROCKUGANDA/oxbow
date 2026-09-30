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

    DEV-033 keeps that promise on a corpus whose folds degrade. A row whose fitted channel no
    fold produced still gets a row here, with ``pr_auc: null`` and its ``channel_status`` /
    ``channel_availability`` naming the folds that refused and why. Dropping the row would
    narrow a never-cut table quietly; filling it with another channel's number is the
    substitution DEV-027 removed. A consumer that needs a number must read the status first.
    """
    rows: list[dict[str, Any]] = []
    for variant in run.variants:
        primary = variant.policies.get("ev_greedy") or variant.policies.get("score_threshold")
        availability = dict(variant.availability)
        rows.append(
            {
                "row_id": variant.row_id,
                "label": variant.label,
                "question": variant.question,
                "corpus": variant.corpus,
                "provenance": variant.provenance,
                "is_control": variant.is_control,
                "control_note": variant.control_note,
                "pr_auc": None if primary is None else primary.pr_auc,
                "pr_auc_ci_low": None if primary is None else primary.pr_auc_ci_low,
                "pr_auc_ci_high": None if primary is None else primary.pr_auc_ci_high,
                "auroc_comparability_only": None if primary is None else primary.auroc,
                "brier": None if primary is None else primary.brier,
                "net_benefit_total_minor": (
                    None if primary is None else primary.net_benefit_total_minor
                ),
                "currency": run.config.currency,
                # The count beside the number: a partial row's PR-AUC is pooled over fewer folds
                # than the plan, and the card says so on the row itself rather than only in the
                # fold detail a reader has to open.
                "folds_reported": 0 if primary is None else len(primary.folds),
                "folds_expected": variant.fold_count,
                "channel_status": availability.get("status", "measured"),
                "channel_availability": availability,
            }
        )
    return rows


def _availability_summary(run: BacktestRun) -> dict[str, Any]:
    """Which rows were measured, which were partial, which measured nothing — with the reasons.

    Generated from the variants, never typed: the card is the surface a reviewer reads, and a
    table whose unavailable cell looks like a zero-performing model is the failure DEV-033 was
    written to stop.
    """
    flagged = [
        variant
        for variant in run.variants
        if variant.availability.get("status") in ("partial", "unavailable")
    ]
    return {
        "rows_expected": len(run.variants),
        "rows_measured_on_every_fold": len(run.variants) - len(flagged),
        "rows_partially_measured": [
            str(variant.row_id)
            for variant in flagged
            if variant.availability.get("status") == "partial"
        ],
        "rows_unavailable": [
            str(variant.row_id)
            for variant in flagged
            if variant.availability.get("status") == "unavailable"
        ],
        "detail": [variant.availability for variant in flagged],
        "note": (
            "A row listed as partially measured or unavailable published no metric for the folds "
            "its producer refused. Those cells are absent, not zero: no fold's refusal was "
            "answered with another channel's number."
        ),
    }


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
        # DEV-033: the card that prints the table also prints which of its rows are measurements.
        # A reader who scrolls to the numbers gets the caveat with the numbers, not as a footnote
        # in a JSON file nobody opens.
        "ablation_row_availability": _availability_summary(run),
        "ablation_availability_note": (
            "Rows with channel_status 'partial' or 'unavailable' have null metric cells for the "
            "folds their producer refused; the count and the reason are on the row. An "
            "unavailable cell is never another channel's number, and never a zero."
        ),
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
    """The arm the card headlines: the declared configuration that actually measured something.

    DEV-033's fold-level refusals can leave a row with no policy aggregate at all. Such a row is
    still in the table — that is the point — but it cannot be the headline, because the headline
    block reads ``policies[…]`` positionally and an empty aggregate would either crash the card
    or publish a null as the run's one figure. The next declared configuration that did measure
    takes the slot, and the unavailable row stays visible beside it with its reason.
    """
    honest = [v for v in run.variants if not v.is_control]
    for want_id in ("full_calibrated", "gbm_with_graph", "threshold_vs_ev"):
        for variant in honest:
            if variant.row_id == want_id and variant.policies:
                return variant
    measurable = [v for v in honest if v.policies]
    if measurable:
        return measurable[0]
    return None


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
