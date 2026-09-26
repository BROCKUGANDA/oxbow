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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.schemas.catalog import Band, CaseStatus
from api.schemas.common import AssumptionLine, Money

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
    """Confidence with its observed rate and its population, never either alone."""

    model_config = ConfigDict(extra="forbid")

    band: str
    observed_rate: float
    n: int
    note: str | None = None


class EconomicsBlock(BaseModel):
    """Money, tail and the assumptions -- one object, because they are one claim.

    A recovery rate that is an assumption is labelled as one, and the sensitivity band
    is rendered next to the point estimate: "never present a single money number
    without its r band" (config/economics.yaml, plan §12).
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
