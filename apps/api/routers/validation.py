"""Validation and scorecard-studio routes: every chart the page draws, from rows.

Plan §14's rule here is that **every chart is served from the API and nothing is a
screenshot**. That makes the shape of these responses the substance of the
credibility claim, so three conversions happen server-side and are named in the
payload rather than being left to the client:

* an undefined statistic stays undefined. A fold with no alerts above the cutoff
  reports ``precision_undefined: true`` with its ``alerts`` count, never 0 and never 1
  (plan §12, ``test_no_alerts_fold_undefined_not_zero``);
* zero drawdown is explained. The label is generated here — "zero because the policy
  never lost money in these folds" — because an empty chart reads as a missing
  feature and a stated zero reads as a measurement (``test_zero_drawdown_labelled``);
* the risk-adjusted ratio arrives with its own non-Sharpe disclaimer, copied from the
  stored row, since plan §12 requires the label and the formula on the page, the model
  card and the script.

Curves come from ``curve_point``: reliability, PR, global SHAP importance, and
per-typology recall, each with its axis meaning and its operating point marked.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, Query

from api.deps import Container, analyst_or_higher, get_container
from api.problems import (
    COMMON_ERROR_STATUSES,
    DependencyUnavailable,
    problem_responses,
)
from api.readmodel import ReadModel, money
from api.routers.common import build_meta
from api.schemas.common import Envelope, envelope
from api.schemas.validation import (
    AblationRowView,
    ConfusionCellView,
    ConfusionMatrixView,
    CurveSeries,
    DisagreementRowView,
    DriftRowView,
    FairnessAxisView,
    FairnessRowView,
    FoldRow,
    MigrationCellView,
    PerturbationRowView,
    ScorecardAttributeView,
    ScorecardStudioBundle,
    ValidationBundle,
    ValidationMetricView,
)
from api.security import Principal

router = APIRouter(prefix="/api/validation", tags=["validation"])
scorecard_router = APIRouter(prefix="/api/scorecard", tags=["validation"])

# The curve families this API renders, each with the axis labels the page prints. One
# table holds them all (they share a shape), so the labels are declared once and a
# chart cannot be drawn with an axis someone remembered wrongly.
CURVE_AXES: Final = {
    "reliability": ("predicted probability", "observed frequency"),
    "pr_curve": ("recall", "precision"),
    "shap_global": ("feature", "mean |SHAP|"),
    "typology_recall": ("typology", "recall"),
    "per_typology_precision": ("typology", "precision"),
}
LEAKAGE_CONTROL_MARKERS: Final = ("leak", "lookahead", "control")


@router.get(
    "",
    response_model=Envelope[ValidationBundle],
    summary="Folds, ablation table, PR and reliability curves, fairness, perturbation",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def validation(
    run_id: str | None = Query(default=None, min_length=26, max_length=26),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    run = read_model.resolve_run(run_id, state="complete" if run_id is None else None)
    rid = str(run["run_id"])
    decimals = container.economics.minor_units_per_major

    folds, fold_total = read_model.source.select(
        "backtest_fold", where={"run_id": rid}, order="fold_index", allow_missing=True
    )
    ablation, _ = read_model.source.select(
        "ablation_row", where={"run_id": rid}, order="variant", allow_missing=True
    )
    metrics, _ = read_model.source.select(
        "validation_metric", where={"run_id": rid}, order="name", allow_missing=True
    )
    fairness, _ = read_model.source.select(
        "fairness_row", where={"run_id": rid}, order="axis", allow_missing=True
    )
    perturbations, _ = read_model.source.select(
        "perturbation_row", where={"run_id": rid}, order="kind", allow_missing=True
    )
    confusion_rows, confusion_total = read_model.source.select(
        "confusion_cell", where={"run_id": rid}, order="label", allow_missing=True
    )

    curves = [
        _curve(read_model, rid, family)
        for family in CURVE_AXES
        if _has_points(read_model, rid, family)
    ]
    if fold_total == 0:
        raise DependencyUnavailable(
            f"run {rid} wrote no backtest folds, so there is no validation to render. The page "
            "would otherwise show empty axes, which reads as 'the model found nothing' rather "
            "than 'the backtest has not run' — run `make backtest`.",
        )
    budget_metric = next(
        (row for row in metrics if str(row["name"]) in {"review_budget", "budget"}), None
    )
    body = ValidationBundle(
        run_id=rid,
        corpora=sorted({str(row["corpus"]) for row in folds}),
        folds=[FoldRow.model_validate(_fold_dict(row)) for row in folds],
        ablation=[AblationRowView.model_validate(_ablation_dict(row)) for row in ablation],
        curves=curves,
        confusion=None
        if confusion_total == 0
        else ConfusionMatrixView(
            cells=[ConfusionCellView.model_validate(row) for row in confusion_rows],
            budget=None if budget_metric is None else int(float(budget_metric["value"])),
            basis="at the configured review budget, not over the whole ranking (plan §12)",
        ),
        fairness=_fairness_axes(fairness),
        perturbations=[PerturbationRowView.model_validate(row) for row in perturbations],
        metrics=[
            ValidationMetricView.model_validate(row)
            for row in metrics
            if str(row["name"]) not in {"review_budget", "budget"}
        ],
        typology_recall=[
            ValidationMetricView.model_validate(row)
            for row in metrics
            if str(row["name"]).startswith("typology_recall")
        ],
        overfitting=_overfitting(metrics),
        label_quality=_label_quality(read_model, metrics),
        limitations=_limitations(metrics),
        assumptions=[
            {
                "key": "config/economics.yaml",
                "value": container.economics.source_path.name,
                "source": "config/economics.yaml",
                "note": "every money figure on this page is a function of these assumptions",
            }
        ],
    )
    del decimals
    return envelope(
        body,
        **build_meta(
            container,
            run_id=rid,
            model_version=str(run["model_version"]),
            provenance=str(run["provenance"]),
        ).model_dump(),
    )


@scorecard_router.get(
    "",
    response_model=Envelope[ScorecardStudioBundle],
    summary="Scorecard Studio: constants and formula, attributes, bins, bands, drift",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def scorecard_studio(
    run_id: str | None = Query(default=None, min_length=26, max_length=26),
    attribute: str | None = Query(default=None, max_length=128),
    disagreement_limit: int = Query(default=25, ge=1, le=200),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    run = read_model.resolve_run(run_id, state="complete" if run_id is None else None)
    rid = str(run["run_id"])
    specs, spec_total = read_model.source.select(
        "scorecard_spec", where={"run_id": rid}, order="model_version", allow_missing=True
    )
    if spec_total == 0:
        raise DependencyUnavailable(
            f"run {rid} stored no scorecard spec, so the studio cannot show the scaling "
            "constants or the points formula the scores were built from. Showing points "
            "without the formula would leave the reader to guess the units.",
        )
    spec = specs[0]
    attributes, _ = read_model.source.select(
        "scorecard_attribute",
        where=None if attribute is None else {"run_id": rid, "attribute": attribute},
        order="iv",
        descending=True,
        allow_missing=True,
    )
    if attribute is None:
        attributes, _ = read_model.source.select(
            "scorecard_attribute", where={"run_id": rid}, order="iv", descending=True
        )
    bins, _ = read_model.source.select(
        "scorecard_bin", where={"run_id": rid}, order="attribute", allow_missing=True
    )
    bands, _ = read_model.source.select(
        "band_definition", where={"run_id": rid}, order="band", allow_missing=True
    )
    drift, _ = read_model.source.select(
        "drift_period", where={"run_id": rid}, order="period", allow_missing=True
    )
    migration, _ = read_model.source.select(
        "rating_migration", where={"run_id": rid}, order="from_band", allow_missing=True
    )
    disagreements, disagreement_total = read_model.source.select(
        "model_disagreement",
        where={"run_id": rid},
        order="delta",
        descending=True,
        limit=disagreement_limit,
        allow_missing=True,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in bins:
        grouped.setdefault(str(row["attribute"]), []).append(row)
    body = ScorecardStudioBundle(
        run_id=rid,
        model_version=str(spec["model_version"]),
        pdo=int(spec["pdo"]),
        base_score=int(spec["base_score"]),
        base_odds=float(spec["base_odds"]),
        points_to_double=float(spec["points_to_double"]),
        offset=float(spec["offset"]),
        scale_factor=float(spec["scale_factor"]),
        points_formula=str(spec["points_formula"]),
        n_total=int(spec["n_total"]),
        n_bad=int(spec["n_bad"]),
        fitted_at=spec.get("fitted_at"),
        attributes=[
            ScorecardAttributeView(
                attribute=str(row["attribute"]),
                iv=float(row["iv"]),
                n_bins=int(row["n_bins"]),
                monotone=bool(row["monotone"]),
                family=str(row["family"]),
                bins=grouped.get(str(row["attribute"]), []),
            )
            for row in attributes
        ],
        bands=list(bands),
        drift=[DriftRowView.model_validate(row) for row in drift],
        migration=[MigrationCellView.model_validate(row) for row in migration],
        disagreements=[DisagreementRowView.model_validate(row) for row in disagreements],
        disagreement_note=None
        if disagreement_total
        else (
            "the two models agree everywhere: no account's scorecard band differs from its GBM "
            "band in this run. That is a finding, not an empty tab — the threshold that would "
            "surface near-misses is in disagreement_threshold"
        ),
        disagreement_threshold=float(spec.get("scale_factor") or 0.0)
        * DISAGREEMENT_NEAR_MISS_FACTOR,
    )
    return envelope(
        body,
        **build_meta(
            container,
            run_id=rid,
            model_version=str(spec["model_version"]),
            provenance=str(run["provenance"]),
        ).model_dump(),
    )


# A near-miss is half a band-step of probability apart. Expressed through the stored
# scale factor rather than a hard number so the threshold means the same thing in
# every run, whatever the scorecard's own scaling is.
DISAGREEMENT_NEAR_MISS_FACTOR: Final = 0.5


def _has_points(read_model: ReadModel, run_id: str, family: str) -> bool:
    _, total = read_model.source.select(
        "curve_point", where={"run_id": run_id, "family": family}, limit=1, allow_missing=True
    )
    return total > 0


def _curve(read_model: ReadModel, run_id: str, family: str) -> CurveSeries:
    rows, total = read_model.source.select(
        "curve_point", where={"run_id": run_id, "family": family}, order="point_index"
    )
    x_label, y_label = CURVE_AXES[family]
    operating = next((row for row in rows if bool(row.get("operating_point"))), None)
    note = None
    if total < 2:
        note = (
            f"this series has {total} point(s). A curve cannot be drawn through it, so the page "
            "shows this note and the number instead of an absurd chart (plan §14 "
            "test_single_point_series)"
        )
    return CurveSeries(
        family=family,
        x_label=x_label,
        y_label=y_label,
        points=rows,
        currency=None if not rows or rows[0].get("currency") is None else str(rows[0]["currency"]),
        operating_threshold=None if operating is None else float(operating["x"]),
        note=note,
    )


def _fold_dict(row: dict[str, Any]) -> dict[str, Any]:
    currency = str(row["currency"])
    decimals = 2
    undefined = bool(row["precision_undefined"])
    return {
        "fold_index": int(row["fold_index"]),
        "corpus": str(row["corpus"]),
        "train_start": row["train_start"],
        "train_end": row["train_end"],
        "embargo_days": int(row["embargo_days"]),
        "embargo_end": row["embargo_end"],
        "test_start": row["test_start"],
        "test_end": row["test_end"],
        "n_train": int(row["n_train"]),
        "n_test": int(row["n_test"]),
        "pr_auc": float(row["pr_auc"]),
        "auroc": float(row["auroc"]),
        "brier": float(row["brier"]),
        "precision_at_budget": None if undefined else row.get("precision_at_budget"),
        "recall_at_budget": None if undefined else row.get("recall_at_budget"),
        "precision_undefined": undefined,
        "precision_note": None
        if not undefined
        else (
            f"precision is undefined: {int(row['alerts'])} alerts crossed the cutoff in this "
            "fold, so there is no denominator. It is reported as undefined rather than 0 or 1 "
            "(plan §12)."
        ),
        "alerts": int(row["alerts"]),
        "captured_value": money(int(row["captured_value_minor"]), currency, decimals=decimals),
        "cost": money(int(row["cost_minor"]), currency, decimals=decimals),
        "net_benefit": money(int(row["net_benefit_minor"]), currency, decimals=decimals),
        "max_drawdown": money(int(row["max_drawdown_minor"]), currency, decimals=decimals),
        "zero_drawdown_note": "drawdown is zero because this fold never lost money"
        if int(row["max_drawdown_minor"]) == 0
        else None,
        "var95": money(int(row["var95_minor"]), currency, decimals=decimals),
        "es975": money(int(row["es975_minor"]), currency, decimals=decimals),
        "monte_carlo_runs": int(row["mc_runs"]),
        "monte_carlo_seed": int(row["mc_seed"]),
        "entity_disjoint": bool(row["entity_disjoint"]),
        "test_fold_touched_at": row.get("test_fold_touched_at"),
    }


def _ablation_dict(row: dict[str, Any]) -> dict[str, Any]:
    variant = str(row["variant"]).lower()
    return {
        **row,
        "is_leakage_control": any(marker in variant for marker in LEAKAGE_CONTROL_MARKERS),
        "is_graph_thesis": "with_graph" in variant or "graph_features" in variant,
        "is_pricing_thesis": "threshold" in variant and "ev" in variant,
    }


def _fairness_axes(rows: list[dict[str, Any]]) -> list[FairnessAxisView]:
    axes: dict[str, FairnessAxisView] = {}
    for row in rows:
        axis = str(row["axis"])
        axes.setdefault(
            axis,
            FairnessAxisView(axis=axis, axis_rationale=str(row["axis_rationale"]), rows=[]),
        )
        axes[axis].rows.append(
            FairnessRowView(
                bucket=str(row["bucket"]),
                fp_rate=float(row["fp_rate"]),
                fn_rate=row.get("fn_rate"),
                n=int(row["n"]),
            )
        )
    return [axes[key] for key in sorted(axes)]


def _overfitting(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    wanted = {
        "configurations_evaluated": "configurations evaluated during selection",
        "test_fold_touched_once": "the test fold was touched exactly once",
        "selection_on_validation": "selection happened on the validation folds",
    }
    out = {key: float(row["value"]) for row in metrics for key in wanted if str(row["name"]) == key}
    out["caveat"] = (
        "with N configurations tried, the best validation result is optimistically biased by "
        "multiple testing; the headline comes from the untouched fold for that reason"
    )
    out["keys"] = wanted
    return out


def _label_quality(read_model: ReadModel, metrics: list[dict[str, Any]]) -> dict[str, Any]:
    named = {
        "label_prevalence": "positive rate of the learning label",
        "flagged_fraud_rows": "rows carrying the crude isFlaggedFraud threshold",
    }
    out = {
        str(row["name"]): {
            "value": float(row["value"]),
            "meaning": meaning,
            "corpus": str(row["corpus"]),
        }
        for row in metrics
        for name, meaning in named.items()
        if str(row["name"]) == name
    }
    out["note"] = (
        "PaySim's isFlaggedFraud is not a usable target; isFraud is the only viable label on "
        "that corpus (DEV-011). IBM-AML's typology labels come from its pattern file, not its "
        "label column (DEV-014)."
    )
    return out


def _limitations(metrics: list[dict[str, Any]]) -> list[str]:
    """Limitations stored by the run, echoed rather than re-authored here.

    Plan §15 requires them written in the first person and generated from measured
    output; a second copy in the API would be a second document to fall out of date.
    """
    return [
        str(row.get("note") or row["name"])
        for row in metrics
        if str(row["name"]).startswith("limitation:")
    ]


__all__ = ["router", "scorecard_router"]
