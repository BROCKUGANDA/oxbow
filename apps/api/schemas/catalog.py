"""Catalog read models: the dataset card, runs, the alert queue, the subgraph.

Each shape exists because a screen needs it and the API must be the only source of the
number on that screen (plan §19 rule 4: every rendered figure traces to a response
field). Two of them carry a deliberate extra:

* the dataset card carries its licence, obligations, citation, retrieval date and file
  hashes on the same object as the numbers, because 01 §A rule 7 makes attribution a
  property of the data rather than a line in a README;
* the alert queue carries the *active policy's* cutoff and rank with every row, because
  the capacity cutoff line is drawn from server state — if the client computed the
  cutoff it could disagree with the allocation that produced the ranking, and that
  disagreement is the failure 02 §B seam 5 exists to prevent.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.schemas.common import CalibrationKind, Money
from api.schemas.events import StageEvent

Band = Literal["A", "B", "C", "D", "E"]
RunStateName = Literal["running", "complete", "failed", "superseded"]
CaseStatus = Literal["open", "pending_four_eyes", "decided", "reversed"]


class DatasetFileCard(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    file_name: str
    sha256: str
    size_bytes: int | None = None
    row_count: int | None = None
    verified_at: datetime | None = None


class DatasetSourceCard(BaseModel):
    """One corpus, with everything 01 §A rule 7 requires beside its numbers."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    source_id: str
    name: str
    role: str
    module: str | None = None
    source_url: str
    retrieval: str
    license: str
    license_obligation: str
    citation: str
    description: str
    label_caveat: str
    known_biases: list[str]
    synthetic_fields: list[str]
    ingest_allowed: bool
    retrieved_at: date | None = None
    files: list[DatasetFileCard] = Field(default_factory=list)


class MeasurementCard(BaseModel):
    """A measured number about the corpus and the command that produced it."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    scope: str
    name: str
    value: float
    unit: str | None = None
    command: str
    measured_at: datetime


class DatasetMeta(BaseModel):
    """The object ``/api/meta/dataset`` serves: the dataset card, as the UI reads it."""

    model_config = ConfigDict(extra="forbid")

    sources: list[DatasetSourceCard]
    refused_sources: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Declared-but-refused sources, with the reason. A UI that shows only "
        "what was used hides the constraint that made the choice.",
    )
    measurements: list[MeasurementCard] = Field(default_factory=list)
    sampling: dict[str, Any] = Field(default_factory=dict)
    deidentification: dict[str, Any] = Field(default_factory=dict)
    disclaimer: str


class RunSummary(BaseModel):
    """The run record as both ``GET /api/runs`` and ``GET /api/runs/{run_id}`` serve it.

    The field set is exactly what :meth:`api.readmodel.ReadModel.run_summary` builds, and
    it has to be: that one accessor is the only place the per-run counts are computed, so
    a summary route and a detail route cannot disagree about what ``scored_count`` meant.
    ``extra="forbid"`` is what makes a drift between the two a 500 in a test rather than a
    silently dropped column, which is why ``notes`` and ``artifact_hashes`` are declared
    here instead of being stripped by whichever route asks for less.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    run_id: str
    created_at: datetime
    finished_at: datetime | None = None
    state: RunStateName
    seed: int
    timezone: str
    provenance: str
    model_version: str
    config_hash: str
    dataset_ref: str | None = None
    error: str | None = None
    superseded_by: str | None = None
    notes: str | None = None
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    account_count: int = 0
    scored_count: int = 0
    alert_count: int = 0
    quarantine_count: int = Field(
        default=0,
        description="Rows dropped at ingest. Never zero by default in a response: "
        "a silent drop is the blind spot 02 §D names.",
    )


class RunDetail(RunSummary):
    events: list[StageEvent] = Field(default_factory=list)
    notes: str | None = None


class AlertReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    label: str
    points: int | None = Field(
        default=None,
        description=(
            "Scorecard points this reason contributed, or null when the pipeline recorded "
            "the reason without a points value. Null is not zero: zero is a claim about the "
            "scorecard (the reason fired and cost nothing) and null is a claim about the "
            "record (the reason fired and no points were stored for it). The reasons reader "
            "in routers/alerts.py::_reasons has always emitted null in that second case, so "
            "typing this non-nullable made /api/alerts a 500 on every row carrying a plain "
            "reason code."
        ),
    )


class AlertRow(BaseModel):
    """One card in the alert queue, with everything the card renders.

    ``rank`` and ``beyond_capacity`` come from the active policy's stored allocation,
    and ``cutoff_rank`` repeats the policy's line position on every row so a virtualised
    list can draw the line without a second request per scroll.

    ``calibration_kind`` is whether a stored probability was measured against a population.
    ``uncalibrated`` carries neither a rate nor an ``n`` but a reason instead: the fold was
    refused calibration because it held fewer validation positives than
    ``config/model.yaml``'s ``min_positives_for_calibration``, which plan 03 §H says the
    product must *state* rather than suppress. The vocabulary is the pipeline's own — the
    ``kind`` field of ``CalibrationResult.confidence_label``.
    """

    model_config = ConfigDict(extra="forbid")

    account_key: str
    run_id: str
    band: Band
    fused_score: float
    calibration_kind: CalibrationKind
    calibration_note: str | None = Field(
        default=None,
        description="Why calibration was refused, in the fold's own words. Present exactly when "
        "calibration_kind is 'uncalibrated' and absent exactly when it is not — the pairing the "
        "database enforces in ck_score_calibration_pairing is restated here so a client cannot "
        "receive an uncalibrated row that looks like a calibrated one with a missing number.",
    )
    calibrated_probability: float | None = None
    observed_rate: float | None = None
    calibration_n: int | None = None
    predicted_typology: str | None = None
    reasons: list[AlertReason]
    exposure: Money
    expected_value: Money
    rank: int
    selected: bool
    beyond_capacity: bool
    cutoff_rank: int | None = Field(
        default=None,
        description="The active policy's capacity line position, repeated on every row so a "
        "virtualised list can draw it without a second request. Null only when the policy "
        "selects nothing at all — which is a documented outcome, not an absent line.",
    )
    capacity_minutes: int
    case_id: str | None = None
    case_status: CaseStatus | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    txn_count: int

    @model_validator(mode="after")
    def _calibration_shape(self) -> AlertRow:
        if self.calibration_kind is CalibrationKind.calibrated_band:
            missing = [
                name
                for name, value in (
                    ("calibrated_probability", self.calibrated_probability),
                    ("observed_rate", self.observed_rate),
                    ("calibration_n", self.calibration_n),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"calibration_kind='calibrated_band' but {missing} is absent; a confidence "
                    "figure without its population is an adjective, and the queue refuses one"
                )
            if self.calibration_note is not None:
                raise ValueError("a calibrated row must not carry a refusal note")
        elif self.calibration_note is None:
            raise ValueError(
                "calibration_kind='uncalibrated' requires calibration_note: the reader is "
                "entitled to know why no rate was measured"
            )
        elif (
            self.calibrated_probability is not None
            or self.observed_rate is not None
            or self.calibration_n is not None
        ):
            raise ValueError(
                "an uncalibrated row may not carry a rate, an n or a calibrated probability; "
                "the fold refused to measure them"
            )
        return self


class AlertQueue(BaseModel):
    """The queue page plus the policy frame it is drawn inside.

    The rows alone are not enough: the capacity cutoff line is the single most
    persuasive element in the product (plan §17, never-cut), and a client cannot draw
    it from a page of rows. ``allocation_source`` is here for the same reason —
    ranks read from a run's committed allocation and ranks produced by a live
    re-allocation are different claims about the same picture.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    policy_id: str | None = None
    capacity_minutes: int
    cutoff_rank: int | None = None
    allocation_source: Literal["stored", "reallocated"]
    unpriced_accounts: int = Field(
        default=0,
        description="Scored accounts with no stored economics row, so absent from the ranking. "
        "A queue that is short by an unexplained number is the blind spot 02 §D names.",
    )
    rows: list[AlertRow] = Field(default_factory=list)


class AlertFacets(BaseModel):
    """What each queue filter actually holds for one run, so the UI never offers an empty one.

    Declared as a model rather than a bare ``dict`` because the generated client types this
    branch: an untyped facet bag means the filter chips are built from a shape nobody
    checked, which is the ``any`` plan §13 forbids. ``total`` is the number of scored rows
    the counts were taken over, so a band count of zero is distinguishable from "no rows
    were read".
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    total: int = Field(ge=0, description="Scored rows in this run, before any filter.")
    bands: dict[str, int] = Field(default_factory=dict)
    typologies: dict[str, int] = Field(default_factory=dict)
    rules: dict[str, int] = Field(default_factory=dict)
    sortable: list[str] = Field(default_factory=list)


class NetworkNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="An account_key, never a raw identifier (03 §D).")
    label: str
    band: Band | None = None
    exposure: Money | None = None
    degree: int
    community_id: int | None = None
    is_seed: bool = False
    is_rail: bool = False
    flags: list[str] = Field(default_factory=list)


class NetworkEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    total: Money
    txn_count: int
    first_ts: datetime
    last_ts: datetime
    flags: list[str] = Field(default_factory=list)


class CommunityMetaNode(BaseModel):
    """A collapsed community, labelled with its true size.

    The 1,500-node cap is server-side (plan §14), and what falls off the graph does not
    disappear: it arrives here with a real member count, because a network view that
    quietly drops members would understate the thing it is showing.
    """

    model_config = ConfigDict(extra="forbid")

    community_id: int
    member_count: int
    total: Money | None = None
    representative_account_key: str


NetworkFlag = Literal["cycle", "high_velocity_hops", "fan_in", "fan_out", "dense_community"]


class NetworkSubgraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    seed_account_key: str
    hops: int
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]
    collapsed_communities: list[CommunityMetaNode] = Field(default_factory=list)
    node_cap: int
    truncated: bool
    truncation_reason: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    counterparty_note: str | None = Field(
        default=None,
        description="Why an account has no counterparties in the window, when it has none: "
        "the empty state names the window and offers to widen it (plan §14).",
    )


__all__ = [
    "AlertQueue",
    "AlertReason",
    "AlertRow",
    "Band",
    "CaseStatus",
    "CommunityMetaNode",
    "DatasetFileCard",
    "DatasetMeta",
    "DatasetSourceCard",
    "MeasurementCard",
    "NetworkEdge",
    "NetworkFlag",
    "NetworkNode",
    "NetworkSubgraph",
    "RunDetail",
    "RunSummary",
]
