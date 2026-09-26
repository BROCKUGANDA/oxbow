"""Backtest, validation and scorecard-studio shapes — the rigor score, on the wire.

Plan §14's rule for this area is that **every chart is served from the API and
nothing is a screenshot**, which makes the response models load-bearing: a series
the client has to invent points for is a series nobody can check. So each curve
here carries its axis meaning, its population counts, and the operating point that
was actually chosen.

Three fields exist because the plan names a specific failure they prevent:

* ``precision_undefined`` with ``alerts`` — a fold with no alerts above the cutoff
  reports precision as undefined *with its alert count*, never as 0 and never as 1
  (``test_no_alerts_fold_undefined_not_zero``). Encoding "undefined" as null and
  forcing the caller to look at ``alerts`` is what keeps a zero from being read as
  a measurement.
* ``not_sharpe_note`` on the risk-adjusted ratio, carried from the stored row — plan
  §12 requires the label "explicitly NOT a Sharpe ratio" with the formula shown
  (``test_label_not_sharpe``), so it travels with the number instead of living in
  a component's copy.
* ``test_fold_touched_at`` on each fold — "the test fold is touched once and that
  fact is timestamped" (§12). A null means it has not been touched, which is the
  claim the headline number rests on.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.common import AssumptionLine, Money

CurveFamily = Literal[
    "reliability",
    "pr_curve",
    "shap_global",
    "typology_recall",
    "per_typology_precision",
]


class FoldRow(BaseModel):
    """One expanding-window walk-forward fold, with its embargo drawn as data.

    ``embargo_end`` and ``test_start`` are both returned so the walk-forward diagram
    shows the real gap rather than a decorative one: the embargo is the reason the
    result is not leakage, and a picture that omits it is the picture that invites
    the question.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    fold_index: int
    corpus: str
    train_start: date
    train_end: date
    embargo_days: int
    embargo_end: date
    test_start: date
    test_end: date
    n_train: int
    n_test: int
    pr_auc: float
    auroc: float = Field(
        description="Reported for comparability and explicitly de-emphasised (plan §12); "
        "the primary statistic on this page is pr_auc."
    )
    brier: float
    precision_at_budget: float | None = None
    recall_at_budget: float | None = None
    precision_undefined: bool = False
    precision_note: str | None = Field(
        default=None,
        description="Why precision is undefined when it is: the alert count, in words, "
        "so the page says it rather than leaving a dash.",
    )
    alerts: int
    captured_value: Money
    cost: Money
    net_benefit: Money
    max_drawdown: Money
    zero_drawdown_note: str | None = None
    var95: Money
    es975: Money
    monte_carlo_runs: int
    monte_carlo_seed: int
    entity_disjoint: bool
    test_fold_touched_at: datetime | None = None


class AblationRowView(BaseModel):
    """One row of the ablation table, with the question it answers and its CI.

    ``question`` is stored and returned rather than written in the component because
    the row's meaning is the question: "LightGBM with graph features" is a
    configuration, "how much does the graph add?" is the result.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    variant: str
    question: str
    corpus: str
    pr_auc: float
    net_benefit: Money
    ci_low: float
    ci_high: float
    ci_method: str
    n_resamples: int
    seed: int
    is_leakage_control: bool = Field(
        default=False,
        description="True for the deliberately lookahead-leaking config included as a "
        "control. It must visibly outperform — that is what proves the harness detects "
        "leakage rather than the model being lucky (plan §12's gate).",
    )
    is_graph_thesis: bool = Field(
        default=False, description="Marks the row plan §12 calls 'the thesis in one row'."
    )
    is_pricing_thesis: bool = Field(
        default=False,
        description="Marks threshold-vs-EV, the row that prices the queue decision itself.",
    )


class CurveDatum(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    point_index: int
    x: float
    y: float
    n: int | None = None
    label: str | None = None
    operating_point: bool = False


class CurveSeries(BaseModel):
    """One stored curve, with the axis meanings the page prints next to it.

    The minimum-series rule (``test_single_point_series``) is served here rather
    than in the client: ``note`` explains a one-point or empty series so the UI
    renders an honest sentence instead of an absurd chart.
    """

    model_config = ConfigDict(extra="forbid")

    family: CurveFamily
    x_label: str
    y_label: str
    points: list[CurveDatum] = Field(default_factory=list)
    currency: str | None = None
    operating_threshold: float | None = None
    note: str | None = None


class ConfusionCellView(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    label: str
    prediction: str
    n: int


class ConfusionMatrixView(BaseModel):
    """Confusion at the review budget, not over the whole ranking."""

    model_config = ConfigDict(extra="forbid")

    cells: list[ConfusionCellView] = Field(default_factory=list)
    budget: int | None = Field(
        default=None,
        description=(
            "The review budget the cells were counted at, or null when the run recorded "
            "no review_budget metric. A confusion matrix drawn at an unnamed budget is not "
            "a matrix at a budget, and 0 would be a claim that the desk reviewed nothing. "
            "The reader in routers/validation.py has always passed null in this case, so "
            "typing this non-nullable made every /api/validation response a 500 for a run "
            "whose confusion cells were present but whose budget was not."
        ),
    )
    basis: str


class FairnessRowView(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    bucket: str
    fp_rate: float
    fn_rate: float | None
    n: int


class FairnessAxisView(BaseModel):
    """One proxy axis, why it exists, and its buckets.

    No protected attributes exist in either corpus, so the axis name and its
    rationale are returned together: the honest move is to say that and then check
    anyway along amount decile, age, volume and community size (plan §12).
    """

    model_config = ConfigDict(extra="forbid")

    axis: str
    axis_rationale: str
    rows: list[FairnessRowView] = Field(default_factory=list)


class PerturbationRowView(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    kind: str
    magnitude: float
    result: float
    unit: str
    note: str
    seed: int


class ValidationMetricView(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    name: str
    value: float
    unit: str | None = None
    corpus: str
    note: str | None = None
    n: int | None = None


class ValidationBundle(BaseModel):
    """Everything the validation screen renders, from one request.

    ``overfitting`` carries the configuration count and the multiple-testing caveat
    because plan §12 requires the headline to come from the untouched fold, and a
    reader cannot judge a headline without knowing how many tries produced it.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    corpora: list[str] = Field(default_factory=list)
    folds: list[FoldRow] = Field(default_factory=list)
    ablation: list[AblationRowView] = Field(default_factory=list)
    curves: list[CurveSeries] = Field(default_factory=list)
    confusion: ConfusionMatrixView | None = None
    fairness: list[FairnessAxisView] = Field(default_factory=list)
    perturbations: list[PerturbationRowView] = Field(default_factory=list)
    metrics: list[ValidationMetricView] = Field(default_factory=list)
    typology_recall: list[ValidationMetricView] = Field(default_factory=list)
    overfitting: dict[str, Any] = Field(default_factory=dict)
    label_quality: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    assumptions: list[AssumptionLine] = Field(default_factory=list)


class ScorecardBinView(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    bin_index: int
    label: str
    woe: float
    points: int
    population_share: float
    bad_rate: float
    n: int


class ScorecardAttributeView(BaseModel):
    """One attribute with its IV and its bins — Scorecard Studio's list and detail.

    ``points`` are integers by construction (plan §10: the score is the sum of the
    points, which is what makes a "minus 48 points" reason code auditable), so a
    bin's points are shown rather than a continuous contribution.
    """

    model_config = ConfigDict(extra="forbid")

    attribute: str
    iv: float
    n_bins: int
    monotone: bool
    family: str
    bins: list[ScorecardBinView] = Field(default_factory=list)


class BandRowView(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    band: str
    lower_points: int
    upper_points: int | None
    observed_rate: float
    n: int
    action: str
    review_minutes: float


class DriftRowView(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    attribute: str
    period: str
    psi: float
    csi: float | None
    n: int
    bad_rate: float


class MigrationCellView(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    from_band: str
    to_band: str
    n: int


class DisagreementRowView(BaseModel):
    """Where the scorecard and the GBM part company — "where model risk lives"."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    account_key: str
    band_scorecard: str
    band_gbm: str
    p_scorecard: float
    p_gbm: float
    delta: float
    case_id: str | None = None


class ScorecardStudioBundle(BaseModel):
    """The scorecard as it was actually fitted, constants and formula included.

    ``points_formula`` is served from the stored row rather than restated in the
    client, so what the page renders as "the formula" is the formula that produced
    the points on the case rail next door.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    model_version: str
    pdo: int
    base_score: int
    base_odds: float
    points_to_double: float
    offset: float
    scale_factor: float
    points_formula: str
    n_total: int
    n_bad: int
    fitted_at: datetime | None = None
    attributes: list[ScorecardAttributeView] = Field(default_factory=list)
    bands: list[BandRowView] = Field(default_factory=list)
    drift: list[DriftRowView] = Field(default_factory=list)
    migration: list[MigrationCellView] = Field(default_factory=list)
    disagreements: list[DisagreementRowView] = Field(default_factory=list)
    disagreement_note: str | None = Field(
        default=None,
        description="Set when there are no disagreements above band C: plan §14 asks for "
        "that empty state framed as a finding, and the threshold that would surface "
        "near-misses is server-computed because the server has the distribution.",
    )
    disagreement_threshold: float | None = None


__all__ = [
    "AblationRowView",
    "BandRowView",
    "ConfusionCellView",
    "ConfusionMatrixView",
    "CurveDatum",
    "CurveFamily",
    "CurveSeries",
    "DisagreementRowView",
    "DriftRowView",
    "FairnessAxisView",
    "FairnessRowView",
    "FoldRow",
    "MigrationCellView",
    "PerturbationRowView",
    "ScorecardAttributeView",
    "ScorecardBinView",
    "ScorecardStudioBundle",
    "ValidationBundle",
    "ValidationMetricView",
]
