"""Case detail and decision shapes — the payload the packet, the rail and the timeline read.

The case response is the one object in this API that has to be defensible line by line,
because it is what an analyst signs. Three structural rules follow from that:

* **every figure names its source object.** Score comes from ``score``, points from
  ``scorecard_point``, money from ``economics`` with its ``assumptions``, the interval
  from the stored Monte Carlo draw with its ``runs`` and ``seed``. Nothing is composed
  in the client, and nothing here is recomputed on the way out (02 §B seam 5).
* **the run is pinned and the pin is visible.** ``pinned_run_id`` plus ``superseded``
  means a case decided under run A and reopened after run B still shows the evidence as
  it was at decision time (plan §15).
* **a reversal is an entry, not an edit.** ``decision_history`` is append-only and
  carries both the original and the reversing row, with each row's hash chip.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.schemas.catalog import Band, CaseStatus
from api.schemas.common import AssumptionLine, CalibrationKind, Money

DecisionAction = Literal["escalate", "dismiss", "review", "reverse"]

# Server-side cap on a free-text reason. The UI blocks a longer one before submission;
# this is the boundary that makes the block real (03 §G: long text is truncated and
# *said to be* truncated, never silently cut).
REASON_MAX_CHARS: int = 4000
REASON_MIN_CHARS: int = 3


class ScorecardPointRow(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    attribute: str
    bin_label: str
    points: int
    woe: float
    reason_code: str
    population_share: float
    bad_rate: float


class CalibrationBand(BaseModel):
    """Confidence with its observed rate and its population, never either alone.

    Two shapes, and the one that arrives is decided by ``kind``:

    * ``calibrated_band`` — a rate measured over a named band, with the ``n`` that makes
      the rate a measurement. ``note`` is absent: there is nothing to apologise for.
    * ``uncalibrated`` — no rate, no band and no ``n``, and a ``note`` in the fold's own
      words saying why calibration was refused. The score on the same card is real; the
      confidence figure was never measured, and plan 03 §H requires the case to say so
      rather than print a gap the reader has to interpret.

    A half-populated object is refused here for the same reason
    ``ck_score_calibration_pairing`` refuses the half-populated row in the table: the
    reader of a signed case cannot tell "0.00 over n=0" from "not measured", and those
    are different claims about the account.
    """

    model_config = ConfigDict(extra="forbid")

    kind: CalibrationKind
    band: str | None = None
    observed_rate: float | None = Field(
        default=None,
        description=(
            "Observed rate over the calibration population, not this account's outcome. "
            "Absent when the fold refused calibration."
        ),
    )
    n: int | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _calibration_shape(self) -> CalibrationBand:
        if self.kind is CalibrationKind.calibrated_band:
            missing = [
                name
                for name, value in (
                    ("band", self.band),
                    ("observed_rate", self.observed_rate),
                    ("n", self.n),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"kind='calibrated_band' but {missing} is absent; a confidence figure "
                    "without its population is an adjective, and a signed case refuses one"
                )
            if self.n is not None and self.n <= 0:
                raise ValueError(
                    f"n={self.n} is not a population: a calibrated reading states a rate "
                    "measured over something, and n=0 measures nothing"
                )
            if self.note is not None:
                raise ValueError("a calibrated reading must not carry a refusal note")
            return self
        if self.note is None:
            raise ValueError(
                "kind='uncalibrated' requires note: the analyst signing this case is "
                "entitled to know why no rate was measured"
            )
        present = [
            name
            for name, value in (
                ("band", self.band),
                ("observed_rate", self.observed_rate),
                ("n", self.n),
            )
            if value is not None
        ]
        if present:
            raise ValueError(
                f"an uncalibrated reading may not carry {present}; the fold refused to "
                "measure them and the case cannot present a refusal as a reading"
            )
        return self


class EconomicsBlock(BaseModel):
    """Money, tail and the assumptions -- one object, because they are one claim.

    A recovery rate that is an assumption is labelled as one, and the sensitivity band
    is rendered next to the point estimate: "never present a single money number
    without its r band" (config/economics.yaml, plan §12).

    ``monte_carlo`` is optional because the interval is a result, not a term of the
    price: a fold whose edge list was never propagated has no distribution to show,
    and migration 0004 stores that as three nulls so the account can still be priced.
    The route then serves ``None`` rather than a block with a made-up number in it --
    see :class:`MonteCarlo` for why the floor stays where it is. This matches
    ``oxbow.ports.case_sink.EconomicsBlock`` and ``oxbow.packet.loaders``, which
    already treat the whole block as absent rather than zero, and the case page's
    decoder already accepts null here.
    """

    model_config = ConfigDict(extra="forbid")

    exposure: Money
    expected_value: Money
    loss_avoided: Money
    analyst_minutes: float
    analyst_cost: Money
    friction_cost: Money
    recovery_rate: float
    recovery_sensitivity_band: list[float]
    assumptions: list[AssumptionLine]
    monte_carlo: MonteCarlo | None = None

    @field_validator("recovery_rate")
    @classmethod
    def _rate_is_a_rate(cls, value: float) -> float:
        if not 0.0 < value < 1.0:
            raise ValueError(
                f"recovery_rate {value} is outside the open interval (0,1); a degenerate "
                "rate makes every figure zero or the whole exposure (03 §I)"
            )
        return value


class MonteCarlo(BaseModel):
    """A simulated exposure distribution, present only when one was actually sampled.

    ``runs`` keeps its ``ge=1`` floor, and that is the reason this block is optional
    rather than zeroable: there is no representable draw count that means "none". A
    served ``MonteCarlo`` therefore always states at least one draw somebody took, and
    a run that simulated nothing serves ``None`` -- the alternative would be a case page
    whose "90 % interval" is a pair of nulls rendered as money (DEV-031, migration 0004).
    """

    model_config = ConfigDict(extra="forbid")

    runs: int = Field(ge=1)
    seed: int
    p05: Money
    p50: Money
    p95: Money
    interval: list[float]


class EvidenceRow(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    occurred_at: datetime
    kind: str
    label: str
    txn_id: str | None = None
    rule_id: str | None = None
    object_key: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class TransactionRow(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    txn_id: str
    event_ts_utc: datetime
    local_hour: int
    event_date_local: Any
    src_account_key: str | None
    dst_account_key: str | None
    amount: Money
    txn_type: str
    src_balance_before: int | None
    src_balance_after: int | None
    dst_balance_before: int | None
    dst_balance_after: int | None
    label_fraud: bool | None
    label_typology: str | None


class RuleHitRow(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    rule_id: str
    rule_name: str
    typology: str
    fired: bool
    observed: float | None
    threshold: float | None
    detail: dict[str, Any] = Field(default_factory=dict)


class ShapRow(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    feature: str
    shap: float
    feature_value: float | None
    rank: int
    evidence_txn_ids: list[str] = Field(default_factory=list)


class WatchlistEnrichment(BaseModel):
    """Screening results, structurally incapable of being a decision.

    ``advisory_only`` is on every hit and the block itself repeats it, because the only
    acceptable reading of this object is "a human should look" (02 §F).
    """

    model_config = ConfigDict(extra="forbid")

    list_name: str
    list_version: str
    record_count: int
    hits: list[dict[str, Any]] = Field(default_factory=list)
    advisory_only: bool = True
    note: str


class DecisionRecordRow(BaseModel):
    """One row of the append-only decision history, hash chip included."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    decision_id: str
    decision_seq: int
    chain_seq: int
    action: DecisionAction
    reason: str
    actor_id: str
    actor_roles: list[str]
    occurred_at: datetime
    exposure: Money
    four_eyes_required: bool
    four_eyes_state: Literal["not_required", "pending", "confirmed"]
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    reversal_of_decision_id: str | None = None
    decided_on_superseded_run: bool
    prev_hash: str
    row_hash: str


class CaseDetail(BaseModel):
    """Everything the case workspace renders, from one request."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    pinned_run_id: str
    run_state: str
    superseded: bool = Field(
        description="True when a later run has replaced the pinned one. Deciding anyway is "
        "allowed and is stamped into the audit row (plan §15)."
    )
    account_key: str
    band: Band
    fused_score: float
    scorecard_points_total: int
    scorecard_points: list[ScorecardPointRow]
    calibration: CalibrationBand
    predicted_typology: str | None
    model_version: str
    reason_codes: list[dict[str, Any]]
    rule_ids: list[str]
    economics: EconomicsBlock
    evidence: list[EvidenceRow]
    transactions: list[TransactionRow]
    transaction_total: int
    shap: list[ShapRow]
    rule_hits: list[RuleHitRow]
    watchlist: WatchlistEnrichment | None = None
    decision_history: list[DecisionRecordRow]
    case_version: int = Field(description="Optimistic-concurrency token for the next write.")
    status: CaseStatus
    rank_under_active_policy: int | None = None
    counterfactual: dict[str, Any] | None = Field(
        default=None,
        description="The computed 'what would have had to be true' from the stored "
        "scorecard, or null when the run has no scorecard for this account.",
    )


class DecisionCreate(BaseModel):
    """The write body. A blank reason cannot be expressed as a valid instance."""

    model_config = ConfigDict(extra="forbid")

    action: DecisionAction
    reason: str = Field(
        min_length=REASON_MIN_CHARS,
        max_length=REASON_MAX_CHARS,
        description="Required, non-blank, and preserved verbatim in the audit chain and "
        "the packet. The empty reason is refused server-side (plan §15).",
    )
    expected_version: int = Field(ge=1, description="The case version the UI last read.")
    reversal_of_decision_id: str | None = None

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "a decision reason cannot be blank or whitespace: it is the sentence that "
                "survives into the audit chain and the packet"
            )
        return value

    @field_validator("action")
    @classmethod
    def _reverse_needs_target(cls, value: DecisionAction) -> DecisionAction:
        return value


class DecisionWriteResult(BaseModel):
    """What a successful write returns, including whether it is *finished*.

    ``outbox_queued`` is the honest four-eyes answer: a decision above the threshold is
    stored, confirmed-by-nobody, and the outbox row does not exist yet. The client needs
    to be able to say that without inferring it from the absence of a field.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    decision_id: str
    decision_seq: int
    chain_seq: int
    row_hash: str
    four_eyes_required: bool
    four_eyes_state: Literal["not_required", "pending", "confirmed"]
    outbox_queued: bool
    case_version: int
    decided_on_superseded_run: bool
    audit_seq: int | None = None
    occurred_at: datetime


class FourEyesConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    confirmation_note: str = Field(min_length=REASON_MIN_CHARS, max_length=REASON_MAX_CHARS)


class OutboxRow(BaseModel):
    """One outbox entry as the ledger shows it, including dead letters."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    outbox_id: int
    idempotency_key: str
    run_id: str
    case_id: str
    decision_seq: int
    case_seq: int
    sink_id: str
    status: Literal["pending", "in_flight", "sent", "dead"]
    attempts: int
    max_attempts: int
    schema_version: str
    next_attempt_at: datetime
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None
    dead_at: datetime | None


__all__ = [
    "REASON_MAX_CHARS",
    "REASON_MIN_CHARS",
    "CalibrationBand",
    "CaseDetail",
    "DecisionAction",
    "DecisionCreate",
    "DecisionRecordRow",
    "DecisionWriteResult",
    "EconomicsBlock",
    "EvidenceRow",
    "FourEyesConfirm",
    "MonteCarlo",
    "OutboxRow",
    "RuleHitRow",
    "ScorecardPointRow",
    "ShapRow",
    "TransactionRow",
    "WatchlistEnrichment",
]
