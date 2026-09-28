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

THE SIX NAMES THE CLIENT USED TO DECODE. ``apps/web`` once demanded ``pr_curve``, ``brier``,
``calibration_floor``, ``shap_waterfall``, ``time`` and ``policy_id`` from this bundle, was
refused by a route that had never sent them, and had those decoders removed. Four are now
served because a row exists to serve them from; two are absent on purpose:

* ``brier`` (:class:`BrierMeasurement`) — measured, per fold, in ``backtest_fold.brier``;
* ``calibration_floor`` (:class:`CalibrationFloor`) — measured, per row, in
  ``score.calibration_kind`` and ``score.calibration_note``, against the configured floor;
* ``time`` (:class:`RunTime`) — recorded, in the ``run`` row's two stamps and the
  ``stage_event`` ledger's per-stage ``elapsed_ms``;
* ``policy_id`` — recorded in ``policy_allocation.policy_id`` when a run lands allocation
  rows, and null with ``policy_id_note`` when it does not.

Absent, and why. ``pr_curve`` has no measurement on this host: the route already forwards any
``curve_point`` family it is given — including ``pr_curve`` — but nothing in the pipeline
writes that table (it is not even in ``oxbow.ports.warehouse.WAREHOUSE_TABLES``), and the
backtest artifact records one PR-AUC and one precision/recall pair per fold, never a sweep
over thresholds. ``shap_waterfall`` is measured *per account* — ``scored_rows.parquet``
carries ``shap_json`` (75 feature/value contributions) and ``shap_base_value`` on all 43,720
scored rows — but a waterfall belongs to a named account, which is the case rail's question
and not this page's, and the ``shap_contribution`` table the case route reads has no
producer. Publishing one account's attribution as this page's answer, or an empty list of
nobody's, would be the fabrication this file's whole design exists to prevent.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.common import AssumptionLine, CalibrationKind, Money

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


class FoldBrier(BaseModel):
    """One fold's Brier score, with the corpus and index that say which fold it is.

    Deliberately not a bare ``float``: a Brier without its fold is a number that cannot be
    argued with, and the folds are the unit the pipeline measured in.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    fold_index: int
    corpus: str
    brier: float


class BrierMeasurement(BaseModel):
    """``brier`` as the distribution that was actually measured — one stored value per fold.

    Why this is a list and not a scalar. The client asked for one ``brier``; the pipeline
    publishes Brier per fold (``backtest_fold.brier``, landed from the fold record the backtest
    wrote) and, separately, per ablation arm per policy ladder
    (``ablation_results.json#variants[i].policies[p].brier``, which is what
    ``model_card.json#/ablation_table[i].brier`` repeats). Those are different populations, no
    row in the warehouse holds the arm-level figure, and averaging five folds into one float in
    a router would be exactly the recomputation §B seam 5 forbids — the fold distribution would
    disappear behind a number nobody produced. So the served ``brier`` *is* the distribution,
    and ``aggregation`` says in words that no aggregate was published.

    The same five values also arrive on ``folds[].brier``; this field exists so a caller that
    wants the discrimination curve alone does not have to unpack the money fields to get it.
    """

    model_config = ConfigDict(extra="forbid")

    per_fold: list[FoldBrier] = Field(default_factory=list)
    aggregation: str = Field(
        description="What was combined to produce this field. Today it always says 'none', and "
        "that sentence is the contract: a mean here would be a statistic the API invented."
    )
    note: str | None = None


class CalibrationFloor(BaseModel):
    """The calibration floor the run's stored probabilities were measured against.

    This is the honest half of what ``/model`` used to render from a fixture
    (``{min_positives, refused, method}``). The floor is configuration —
    ``config/model.yaml :: calibration.min_positives_for_calibration`` — and the *refusal* is a
    published measurement: every fold of the scored run declined calibration because its
    validation-positive count was below that floor, and the scorer wrote that decision onto
    each row it landed (``score.calibration_kind`` = ``uncalibrated``, ``score.calibration_note``
    = the producer's own sentence naming the count and the floor). Both are echoed, never
    recomputed; ``note`` is the pipeline's wording, not this API's.

    What is *not* here is the reliability curve the old field implied. Calibration was refused
    in every fold, so no observed-rate-per-bin series was measured against a calibrated
    probability, and a curve drawn through the uncalibrated scores would be a picture of a thing
    the run declined to claim. The backtest artifact's own ``reliability_curve`` exists per arm
    and ladder but has no landing path into ``curve_point`` — see the module docstring — so it
    is absent from the wire, and says so, rather than being reconstructed here.
    """

    model_config = ConfigDict(extra="forbid")

    kind: CalibrationKind = Field(
        description="The stored label on the run's score rows — the pipeline's own vocabulary "
        "(``oxbow.models.calibration.CalibrationResult.confidence_label``), shared with AlertRow."
    )
    refused: bool = Field(
        description="True when the stored label is 'uncalibrated'. Decoded from the label; not a "
        "count of rows and not a judgement about the corpus."
    )
    note: str | None = Field(
        default=None,
        description="Why calibration was refused, verbatim from the stored row. It names the "
        "fold's validation-positive count and the configured floor, which is why no numeric "
        "floor field is duplicated here.",
    )
    basis: str = Field(
        description="Which rows this was read from, and the rule that let one answer be named: "
        "the served label is only published when the run's stored rows agree."
    )
    floor_source: str = Field(
        default="config/model.yaml#/calibration/min_positives_for_calibration",
        description="Where the floor value itself is declared. A pointer, not a copy: the number "
        "travels in `note`, in the producer's words.",
    )


class StageTiming(BaseModel):
    """One recorded stage event: what ran, what it counted, and how long it says it took."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    stage: str
    status: str
    rows: int
    elapsed_ms: int
    emitted_at: datetime


class RunTime(BaseModel):
    """``time`` — the run's recorded clock, from the run row and the stage ledger.

    Two stamps and one row per recorded stage, each carrying the ``elapsed_ms`` the stage itself
    emitted. There is no total duration: subtracting ``created_at`` from ``finished_at`` would
    publish a wall-clock figure that spans queue wait and any stage that never emitted, which is
    a number the pipeline did not measure. Where the store holds no stage rows for the run,
    ``stages`` is empty *and* ``stages_note`` says so — an empty list with no sentence beside it
    is the failure this bundle has been built to avoid.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: str
    created_at: datetime
    finished_at: datetime | None = Field(
        default=None, description="Null while the run has not recorded a finish; never an estimate."
    )
    stages: list[StageTiming] = Field(default_factory=list)
    stages_note: str | None = None


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
    brier: BrierMeasurement | None = Field(
        default=None,
        description="The Brier distribution over the served folds, or null when the run stored "
        "no fold rows at all — which the route already refuses with a named reason. Never a mean.",
    )
    calibration_floor: CalibrationFloor | None = Field(
        default=None,
        description="The floor the stored probabilities were measured against, read off the run's "
        "own score rows. Null when the run landed no score rows, or landed both labels and this "
        "API will not pick one.",
    )
    time: RunTime | None = Field(
        default=None,
        description="The run's recorded clock: its two stamps and every stage timing the ledger "
        "holds for it.",
    )
    policy_id: str | None = Field(
        default=None,
        description="The identifier of the allocation policy whose stored ranks this run produced, "
        "read from `policy_allocation`. Null is the honest answer for a run that landed no "
        "allocation rows; the fold rows' owning ladder is declared as a constant in the landing "
        "module and importing a constant into a response would put a second source of truth on "
        "the page (see `policy_id_note`).",
    )
    policy_id_note: str | None = Field(
        default=None,
        description="Present exactly when `policy_id` is null, and it names the reason and the "
        "artifact that would have to exist to remove it.",
    )
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
    "BrierMeasurement",
    "CalibrationFloor",
    "ConfusionCellView",
    "ConfusionMatrixView",
    "CurveDatum",
    "CurveFamily",
    "CurveSeries",
    "DisagreementRowView",
    "DriftRowView",
    "FairnessAxisView",
    "FairnessRowView",
    "FoldBrier",
    "FoldRow",
    "MigrationCellView",
    "PerturbationRowView",
    "RunTime",
    "ScorecardAttributeView",
    "ScorecardBinView",
    "ScorecardStudioBundle",
    "StageTiming",
    "ValidationBundle",
    "ValidationMetricView",
]
