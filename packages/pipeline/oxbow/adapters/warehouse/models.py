"""The Postgres schema: one declarative source of truth for the handoff tables.

Why this lives in the warehouse adapter and not in the API: the pipeline writes
these rows and the API reads them, and two ORM declarations of the same table is
the slow way to discover that the two halves disagree about a column name. Alembic
autogenerates from here, the null warehouse mirrors these table names into
``out/warehouse/``, and the API's read queries are checked against the same
metadata (02 §B seam 1).

Type rules, each traceable to a logged decision rather than to taste:

* money is ``BIGINT`` minor units with a ``CHAR(3)`` currency next to it. Never
  ``NUMERIC``, never float (DEV-005 / C5). ``0.10`` has no exact binary
  representation, and a money column that cannot represent its own values cannot
  be summed in a packet.
* ``run_id`` and ``case_id`` are ``CHAR(26)`` ULIDs stored as text (DEV-003 / C3),
  never the native ``uuid`` type: the SSE cursor and "rescoring is a new run" both
  need a key that sorts by creation time on its own.
* ``txn_id`` is corpus-namespaced text (DEV-004 / C4): ``paysim:41`` and
  ``ibmaml:41`` are different transactions, and a bigint primary key would merge
  them into a row belonging to neither.
* probabilities, ratios and point statistics are ``DOUBLE PRECISION``; the only
  exactness requirement in this schema is on money.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

RUN_ID_LEN: Final = 26
ACCOUNT_KEY_LEN: Final = 12
CURRENCY_LEN: Final = 3

# A run stops accepting writes in these states, which is what lets a packet pin
# one and stay truthful forever (plan §15).
IMMUTABLE_STATES: Final = ("complete", "superseded")

RUN_STATES: Final = ("running", "complete", "failed", "superseded")
RUN_PROVENANCES: Final = ("pipeline", "fixture", "demo_snapshot")
STAGE_NAMES: Final = ("ingest", "graph", "score", "backtest", "warehouse")
STAGE_STATUSES: Final = ("running", "complete", "failed", "unavailable")
DECISION_ACTIONS: Final = ("escalate", "dismiss", "review", "reverse")
FOUR_EYES_STATES: Final = ("not_required", "pending", "confirmed")
OUTBOX_STATUSES: Final = ("pending", "in_flight", "sent", "dead")
CASE_STATUSES: Final = ("open", "pending_four_eyes", "decided", "reversed")


def _state_check(column: str, states: tuple[str, ...]) -> str:
    """A CHECK constraint over a closed enum, spelled once."""
    values = ", ".join(f"'{state}'" for state in states)
    return f"{column} IN ({values})"


class Base(DeclarativeBase):
    """The one MetaData for every OXBOW table."""


# --- run identity and lifecycle -------------------------------------------


class Run(Base):
    """One immutable execution of the pipeline, or of a labelled fixture of it.

    ``provenance`` is a column rather than a convention because the API has to be
    able to say "these numbers came from a fixture, not from a model" in the
    response body itself. A demo row presented as a measurement is the failure
    00 §B rule 3 and plan §19 both forbid, and this column is what makes the
    distinction survive any amount of downstream copy-paste.
    """

    __tablename__ = "run"
    __table_args__ = (CheckConstraint(_state_check("state", RUN_STATES), name="ck_run_state"),)

    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[str] = mapped_column(String(24), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_ref: Mapped[str | None] = mapped_column(String(128))
    source_revision: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[str | None] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"))
    notes: Mapped[str | None] = mapped_column(Text)
    artifact_hashes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class StageEvent(Base):
    """One row of the pipeline's ledger, and the source of every SSE frame.

    ``id`` is a global sequence, which is what makes it a usable ``Last-Event-ID``:
    a per-run counter would collide across runs and the client would re-read or
    skip rows on resume (plan §13). Resume reads from this table, so a reconnect
    backfills from durable state instead of hoping the publisher remembers.
    """

    __tablename__ = "stage_event"
    __table_args__ = (
        CheckConstraint(_state_check("status", STAGE_STATUSES), name="ck_stage_event_status"),
        CheckConstraint("rows >= 0", name="ck_stage_event_rows_nonnegative"),
        CheckConstraint("elapsed_ms >= 0", name="ck_stage_event_elapsed_nonnegative"),
        Index("ix_stage_event_run_id_id", "run_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    detail: Mapped[str | None] = mapped_column(Text)
    emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Batch(Base):
    """An inbound batch and its manifest verdict (03 §B).

    A batch is accepted whole or rejected whole; ``accepted=False`` rows exist so
    the UI can show what was refused. Half a day of transactions looks like a
    quiet day, which looks normal.
    """

    __tablename__ = "batch"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"))
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    declared_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_rows: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class QuarantineRow(Base):
    """A row that could not be mapped, kept with the constraint it failed (02 §D).

    Never dropped silently: the count is surfaced on the runs endpoint, because a
    fraud system that quietly loses rows has a blind spot nobody can see.
    """

    __tablename__ = "quarantine_row"
    __table_args__ = (Index("ix_quarantine_run_id", "run_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"))
    batch_id: Mapped[str | None] = mapped_column(String(64))
    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    failing_constraint: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    original_row: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    quarantined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- dataset card (01 §A rule 7: license and attribution on the surface) ----


class DatasetSource(Base):
    """One declared corpus: license, obligations, citation, retrieval date."""

    __tablename__ = "dataset_source"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    module: Mapped[str | None] = mapped_column(String(128))
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval: Mapped[str] = mapped_column(Text, nullable=False)
    license: Mapped[str] = mapped_column(String(64), nullable=False)
    license_obligation: Mapped[str] = mapped_column(Text, nullable=False)
    citation: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    label_caveat: Mapped[str] = mapped_column(Text, nullable=False)
    known_biases: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    synthetic_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    ingest_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    retrieved_at: Mapped[date | None] = mapped_column(Date)


class DatasetFile(Base):
    """One file behind a source, with the hash ``make data --verify`` rechecks."""

    __tablename__ = "dataset_file"

    source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dataset_source.source_id"), primary_key=True
    )
    file_name: Mapped[str] = mapped_column(String(256), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Measurement(Base):
    """A measured number about the corpus, with the command that produced it.

    ``command`` is a column on purpose: DEV-011's degree measurements are only
    credible because the exact command is recorded next to the value, and the UI
    prints it (plan §4: write the number and the command that produced it).
    """

    __tablename__ = "measurement"
    __table_args__ = (UniqueConstraint("scope", "name", name="uq_measurement_scope_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Double, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32))
    command: Mapped[str] = mapped_column(Text, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- pseudonymity and erasure ---------------------------------------------


class PseudonymMap(Base):
    """The only table holding a salt. Erasing a subject deletes from here.

    02 §F frames this as "right to erasure vs immutable audit", and the resolution
    is structural: the chain stores salted hashes and decision metadata, so
    destroying this row makes every historical reference unrecoverable while the
    digests still verify. The two obligations are satisfied by different tables,
    which is the only way both can be absolute.
    """

    __tablename__ = "pseudonym_map"

    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), primary_key=True)
    salt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ErasureRequest(Base):
    """Proof that an erasure ran, without anything identifying inside it.

    ``subject_ref_hash`` is a one-way digest of the request the data subject made;
    the request row itself must not carry the raw identifier, because a permanent
    audit record of a deletion that contains the deleted data is a backup copy of
    it (03 §L).
    """

    __tablename__ = "erasure_request"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subject_ref_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    mapping_rows_deleted: Mapped[int] = mapped_column(Integer, nullable=False)
    audit_seq: Mapped[int | None] = mapped_column(Integer)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- the read model the pipeline writes -----------------------------------


class Account(Base):
    """An account's observed shape for one run. Counts and money, no judgement.

    Everything here is a measurement over transactions. Band, score, exposure and
    rank live in their own tables because they come from different stages, and
    collapsing them into one row would make it unclear which stage is on the hook
    for which number (02 §B seam 5).
    """

    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint("run_id", "account_key", name="uq_account_run_key"),
        Index("ix_account_run_first_seen", "run_id", "first_seen_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    txn_count: Mapped[int] = mapped_column(Integer, nullable=False)
    n_outbound: Mapped[int] = mapped_column(Integer, nullable=False)
    n_inbound: Mapped[int] = mapped_column(Integer, nullable=False)
    n_counterparties: Mapped[int] = mapped_column(Integer, nullable=False)
    funding_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LEN), nullable=False)
    age_days: Mapped[int | None] = mapped_column(Integer)


class Score(Base):
    """One account's stored score in one run. The API reads this and never derives.

    ``calibration_n`` is the population behind ``observed_rate``. A confidence
    figure without its ``n`` is an adjective, and plan §14 asks for both on the
    case rail, so the column is not optional in the response either.
    """

    __tablename__ = "score"
    __table_args__ = (
        UniqueConstraint("run_id", "account_key", name="uq_score_run_key"),
        CheckConstraint("band IN ('A','B','C','D','E')", name="ck_score_band"),
        Index("ix_score_run_band", "run_id", "band"),
        Index("ix_score_run_fused", "run_id", text("fused_score DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    fused_score: Mapped[float] = mapped_column(Double, nullable=False)
    band: Mapped[str] = mapped_column(CHAR(1), nullable=False)
    scorecard_points: Mapped[int] = mapped_column(Integer, nullable=False)
    p_scorecard: Mapped[float | None] = mapped_column(Double)
    p_gbm: Mapped[float | None] = mapped_column(Double)
    anomaly_norm: Mapped[float | None] = mapped_column(Double)
    calibrated_probability: Mapped[float] = mapped_column(Double, nullable=False)
    calibration_band: Mapped[str] = mapped_column(String(8), nullable=False)
    observed_rate: Mapped[float] = mapped_column(Double, nullable=False)
    calibration_n: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_typology: Mapped[str | None] = mapped_column(String(64))
    reason_codes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    rule_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)


class ScorecardPoint(Base):
    """One attribute's contribution, with the reason code a human reads."""

    __tablename__ = "scorecard_point"
    __table_args__ = (
        UniqueConstraint("run_id", "account_key", "attribute", name="uq_scorecard_point"),
        Index("ix_scorecard_point_run_key", "run_id", "account_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    attribute: Mapped[str] = mapped_column(String(128), nullable=False)
    bin_label: Mapped[str] = mapped_column(String(128), nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    woe: Mapped[float] = mapped_column(Double, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    population_share: Mapped[float] = mapped_column(Double, nullable=False)
    bad_rate: Mapped[float] = mapped_column(Double, nullable=False)


class RuleHit(Base):
    """R1-R12 per account, with the value that was compared to the threshold."""

    __tablename__ = "rule_hit"
    __table_args__ = (
        UniqueConstraint("run_id", "account_key", "rule_id", name="uq_rule_hit"),
        Index("ix_rule_hit_run_fired", "run_id", "fired"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(128), nullable=False)
    typology: Mapped[str] = mapped_column(String(64), nullable=False)
    fired: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed: Mapped[float | None] = mapped_column(Double)
    threshold: Mapped[float | None] = mapped_column(Double)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ShapContribution(Base):
    """One feature's SHAP value for one account (plan §14, "why this was flagged").

    The UI's cross-filter needs the transaction ids behind a contribution, so
    ``evidence_txn_ids`` is stored with it rather than reconstructed in the
    browser — reconstructing it there would be the client computing an answer the
    pipeline already had (plan §19 rule 4).
    """

    __tablename__ = "shap_contribution"
    __table_args__ = (
        UniqueConstraint("run_id", "account_key", "feature", name="uq_shap_contribution"),
        Index("ix_shap_run_key_rank", "run_id", "account_key", "rank"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    feature: Mapped[str] = mapped_column(String(128), nullable=False)
    shap: Mapped[float] = mapped_column(Double, nullable=False)
    feature_value: Mapped[float | None] = mapped_column(Double)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_txn_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class Economics(Base):
    """The money for one account, together with the assumptions that produced it.

    ``assumptions`` is a column, not a join: the assumptions change with the
    configuration that produced the run, and a later edit of
    ``config/economics.yaml`` must not silently reinterpret an older run's money.
    This is the storage form of "the assumptions travel with the number"
    (plan §13, spec §6.1).
    """

    __tablename__ = "economics"
    __table_args__ = (
        UniqueConstraint("run_id", "account_key", name="uq_economics_run_key"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_economics_currency"),
        Index("ix_economics_run_exposure", "run_id", text("exposure_minor DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LEN), nullable=False)
    exposure_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_value_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    loss_avoided_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    analyst_cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    friction_cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    analyst_minutes: Mapped[float] = mapped_column(Double, nullable=False)
    recovery_rate: Mapped[float] = mapped_column(Double, nullable=False)
    ev_density: Mapped[float | None] = mapped_column(Double)
    mc_runs: Mapped[int] = mapped_column(Integer, nullable=False)
    mc_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    mc_p05_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mc_p50_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mc_p95_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mc_interval: Mapped[list[float]] = mapped_column(JSONB, nullable=False)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class EvidenceEvent(Base):
    """One row of a case's timeline: a transaction, a rule hit, a screening hit."""

    __tablename__ = "evidence_event"
    __table_args__ = (Index("ix_evidence_run_key_ts", "run_id", "account_key", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    txn_id: Mapped[str | None] = mapped_column(String(64))
    rule_id: Mapped[str | None] = mapped_column(String(16))
    object_key: Mapped[str | None] = mapped_column(String(512))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Transaction(Base):
    """A canonical transaction: the evidence the whole case rests on.

    Money is ``amount_minor`` plus an explicit ``currency`` (DEV-005). Balances are
    nullable because IBM-AML does not carry them; a zero-filled absent column would
    be a fabricated value (03 §A rule 2).
    """

    __tablename__ = "transaction"
    __table_args__ = (
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_transaction_currency"),
        CheckConstraint("txn_id ~ '^[a-z0-9_]+:.*'", name="ck_transaction_txn_namespaced"),
        Index("ix_transaction_src_ts", "src_account_key", "event_ts_utc"),
        Index("ix_transaction_dst_ts", "dst_account_key", "event_ts_utc"),
        Index("ix_transaction_run_ts", "run_id", "event_ts_utc"),
    )

    txn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    event_ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_date_local: Mapped[datetime] = mapped_column(Date, nullable=False)
    local_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    src_account_key: Mapped[str | None] = mapped_column(CHAR(ACCOUNT_KEY_LEN))
    dst_account_key: Mapped[str | None] = mapped_column(CHAR(ACCOUNT_KEY_LEN))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LEN), nullable=False)
    txn_type: Mapped[str] = mapped_column(String(32), nullable=False)
    src_balance_before: Mapped[int | None] = mapped_column(BigInteger)
    src_balance_after: Mapped[int | None] = mapped_column(BigInteger)
    dst_balance_before: Mapped[int | None] = mapped_column(BigInteger)
    dst_balance_after: Mapped[int | None] = mapped_column(BigInteger)
    label_fraud: Mapped[bool | None] = mapped_column(Boolean)
    label_typology: Mapped[str | None] = mapped_column(String(64))
    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False)


class GraphEdge(Base):
    """An aggregated directed edge for one run (plan §14, network explorer)."""

    __tablename__ = "graph_edge"
    __table_args__ = (
        UniqueConstraint("run_id", "src_account_key", "dst_account_key", name="uq_graph_edge"),
        Index("ix_graph_edge_src", "run_id", "src_account_key"),
        Index("ix_graph_edge_dst", "run_id", "dst_account_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    src_account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    dst_account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    txn_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LEN), nullable=False)
    first_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_rail: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class Community(Base):
    """A Leiden community, with the deterministic remap that makes ids stable.

    ``canonical_index`` is the size-then-min-key ordering from
    ``config/pipeline.yaml``. Without it, the same community is number 7 in one run
    and 19 in the next, and every screenshot, annotation and test that names it
    becomes noise.
    """

    __tablename__ = "community"
    __table_args__ = (UniqueConstraint("run_id", "canonical_index", name="uq_community_run_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    canonical_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_label: Mapped[str] = mapped_column(String(32), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    density: Mapped[float | None] = mapped_column(Double)
    total_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(CHAR(CURRENCY_LEN))
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)


class AccountMembership(Base):
    """Which community an account belongs to (node border colour in the UI)."""

    __tablename__ = "account_membership"
    __table_args__ = (UniqueConstraint("run_id", "account_key", name="uq_membership_run_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    degree: Mapped[int] = mapped_column(Integer, nullable=False)
    pagerank: Mapped[float | None] = mapped_column(Double)


# --- scorecard studio data -------------------------------------------------


class ScorecardSpec(Base):
    """The scaling constants and the points formula, verbatim, per run.

    plan §14 requires the studio to show PDO, base score, base odds and the
    formula. Storing the rendered formula string next to the constants means the UI
    displays the formula the scorecard actually used, not the one someone
    remembers (01 §A: the API never recomputes; the UI never restates).
    """

    __tablename__ = "scorecard_spec"
    __table_args__ = (UniqueConstraint("run_id", "model_version", name="uq_scorecard_spec"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    pdo: Mapped[int] = mapped_column(Integer, nullable=False)
    base_score: Mapped[int] = mapped_column(Integer, nullable=False)
    base_odds: Mapped[float] = mapped_column(Double, nullable=False)
    points_to_double: Mapped[float] = mapped_column(Double, nullable=False)
    offset: Mapped[float] = mapped_column(Double, nullable=False)
    scale_factor: Mapped[float] = mapped_column(Double, nullable=False)
    points_formula: Mapped[str] = mapped_column(Text, nullable=False)
    n_total: Mapped[int] = mapped_column(Integer, nullable=False)
    n_bad: Mapped[int] = mapped_column(Integer, nullable=False)
    fitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScorecardAttribute(Base):
    """One attribute with its information value and bin count."""

    __tablename__ = "scorecard_attribute"
    __table_args__ = (UniqueConstraint("run_id", "attribute", name="uq_scorecard_attribute"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    attribute: Mapped[str] = mapped_column(String(128), nullable=False)
    iv: Mapped[float] = mapped_column(Double, nullable=False)
    n_bins: Mapped[int] = mapped_column(Integer, nullable=False)
    monotone: Mapped[bool] = mapped_column(Boolean, nullable=False)
    family: Mapped[str] = mapped_column(String(32), nullable=False)


class ScorecardBin(Base):
    """One WOE bin: edges, weight, points, population share, bad rate."""

    __tablename__ = "scorecard_bin"
    __table_args__ = (
        UniqueConstraint("run_id", "attribute", "bin_index", name="uq_scorecard_bin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    attribute: Mapped[str] = mapped_column(String(128), nullable=False)
    bin_index: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    woe: Mapped[float] = mapped_column(Double, nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    population_share: Mapped[float] = mapped_column(Double, nullable=False)
    bad_rate: Mapped[float] = mapped_column(Double, nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)


class BandDefinition(Base):
    """Band letter, point range, observed rate, population, action."""

    __tablename__ = "band_definition"
    __table_args__ = (
        UniqueConstraint("run_id", "band", name="uq_band_definition"),
        CheckConstraint("band IN ('A','B','C','D','E')", name="ck_band_definition_band"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    band: Mapped[str] = mapped_column(CHAR(1), nullable=False)
    lower_points: Mapped[int] = mapped_column(Integer, nullable=False)
    upper_points: Mapped[int | None] = mapped_column(Integer)
    observed_rate: Mapped[float] = mapped_column(Double, nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    review_minutes: Mapped[float] = mapped_column(Double, nullable=False)


class DriftPeriod(Base):
    """PSI/CSI for one attribute over one period, plus the population it describes."""

    __tablename__ = "drift_period"
    __table_args__ = (UniqueConstraint("run_id", "attribute", "period", name="uq_drift_period"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    attribute: Mapped[str] = mapped_column(String(128), nullable=False)
    period: Mapped[str] = mapped_column(String(32), nullable=False)
    psi: Mapped[float] = mapped_column(Double, nullable=False)
    csi: Mapped[float | None] = mapped_column(Double)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    bad_rate: Mapped[float] = mapped_column(Double, nullable=False)


class RatingMigration(Base):
    """One cell of the migration matrix from a previous band to the current one."""

    __tablename__ = "rating_migration"
    __table_args__ = (
        UniqueConstraint("run_id", "from_band", "to_band", name="uq_rating_migration"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    from_band: Mapped[str] = mapped_column(CHAR(1), nullable=False)
    to_band: Mapped[str] = mapped_column(CHAR(1), nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)


class ModelDisagreement(Base):
    """Where the scorecard and the GBM part company — "where model risk lives".

    plan §14 gives this its own tab, so the pair difference is stored per account
    rather than computed in the browser from two other columns.
    """

    __tablename__ = "model_disagreement"
    __table_args__ = (UniqueConstraint("run_id", "account_key", name="uq_model_disagreement"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    band_scorecard: Mapped[str] = mapped_column(CHAR(1), nullable=False)
    band_gbm: Mapped[str] = mapped_column(CHAR(1), nullable=False)
    p_scorecard: Mapped[float] = mapped_column(Double, nullable=False)
    p_gbm: Mapped[float] = mapped_column(Double, nullable=False)
    delta: Mapped[float] = mapped_column(Double, nullable=False)


# --- policy and allocation -------------------------------------------------


class Policy(Base):
    """A review policy: capacity, economics, cutoff. Exactly one is active.

    ``solver`` records whether the allocation came from CP-SAT or the greedy
    fallback, and ``degraded_reason`` says why if it was the fallback. The UI is
    required to show a labelled degraded banner rather than present a greedy answer
    as an optimal one (plan §14, "degraded, not broken").
    """

    __tablename__ = "policy"
    __table_args__ = (
        CheckConstraint(
            "recovery_rate > 0 AND recovery_rate < 1", name="ck_policy_recovery_bounds"
        ),
        CheckConstraint(
            "min_review_minutes > 0",
            name="ck_policy_positive_review_minutes",
        ),
    )

    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    capacity_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    recovery_rate: Mapped[float] = mapped_column(Double, nullable=False)
    recovery_sensitivity_band: Mapped[list[float]] = mapped_column(JSONB, nullable=False)
    analyst_cost_per_hour_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    min_review_minutes: Mapped[float] = mapped_column(Double, nullable=False)
    friction_cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    four_eyes_threshold_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    review_minutes_by_band: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LEN), nullable=False)
    solver: Mapped[str] = mapped_column(String(16), nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    degraded_reason: Mapped[str | None] = mapped_column(Text)
    solve_ms: Mapped[int | None] = mapped_column(Integer)
    optimality_gap_minor: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PolicyAllocation(Base):
    """An account's rank and selection under one policy, including the cutoff.

    ``beyond_capacity`` is stored rather than inferred: the capacity cutoff line is
    the single most persuasive visual in the product (plan §14) and the queue needs
    it as data, on the same page, without a client-side comparison that could
    disagree with the server about which side of the line an account is on.
    """

    __tablename__ = "policy_allocation"
    __table_args__ = (
        UniqueConstraint("run_id", "policy_id", "account_key", name="uq_policy_allocation"),
        Index("ix_allocation_run_policy_rank", "run_id", "policy_id", "rank"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    policy_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("policy.policy_id"), nullable=False
    )
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    beyond_capacity: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expected_value_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    exposure_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    analyst_minutes: Mapped[float] = mapped_column(Double, nullable=False)
    ev_density: Mapped[float] = mapped_column(Double, nullable=False)


class PolicySummary(Base):
    """What one policy is worth over a period, with its tail.

    ``max_drawdown_minor`` and the VaR/ES pair are stored alongside the mean:
    "compare policies by tail reduction, not just mean benefit" (plan §12), and a
    response that carried only the mean would invite exactly the bad policy that
    lowers average loss while fattening the tail.
    """

    __tablename__ = "policy_summary"
    __table_args__ = (UniqueConstraint("run_id", "policy_id", name="uq_policy_summary"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    policy_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("policy.policy_id"), nullable=False
    )
    currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LEN), nullable=False)
    capacity_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    loss_avoided_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    analyst_cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    friction_cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_benefit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    benefit_per_analyst_hour_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_drawdown_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    zero_drawdown: Mapped[bool] = mapped_column(Boolean, nullable=False)
    var_alpha: Mapped[float] = mapped_column(Double, nullable=False)
    var95_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    es_alpha: Mapped[float] = mapped_column(Double, nullable=False)
    es975_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mc_runs: Mapped[int] = mapped_column(Integer, nullable=False)
    mc_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    alerts_per_10k_accounts: Mapped[float] = mapped_column(Double, nullable=False)
    risk_adjusted_benefit: Mapped[float] = mapped_column(Double, nullable=False)
    risk_adjusted_benefit_note: Mapped[str] = mapped_column(Text, nullable=False)
    cumulative_curve: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    frontier: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    baselines: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class PolicySimulation(Base):
    """One recorded re-allocation from the simulator, including what changed.

    The simulator re-allocates for real (plan §14), and the answer has to be
    revisit-able after the tab crashes and after the demo, so each run is stored
    with the request that produced it.
    """

    __tablename__ = "policy_simulation"

    simulation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    base_policy_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("policy.policy_id"), nullable=False
    )
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    entered: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    left: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    degraded_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- backtest, validation, ablation ---------------------------------------


class BacktestFold(Base):
    """One expanding-window fold with its embargo, stored as measured.

    ``embargo_end`` and ``test_start`` are both columns so the walk-forward diagram
    draws the real gap instead of a decorative one, and ``precision_undefined``
    records the honest answer for a fold with no alerts above the cutoff: undefined
    with its alert count, never 0 and never 1 (03 §I, test_no_alerts_fold_undefined_not_zero).
    """

    __tablename__ = "backtest_fold"
    __table_args__ = (UniqueConstraint("run_id", "fold_index", name="uq_backtest_fold"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    fold_index: Mapped[int] = mapped_column(Integer, nullable=False)
    corpus: Mapped[str] = mapped_column(String(64), nullable=False)
    train_start: Mapped[date] = mapped_column(Date, nullable=False)
    train_end: Mapped[date] = mapped_column(Date, nullable=False)
    embargo_days: Mapped[int] = mapped_column(Integer, nullable=False)
    embargo_end: Mapped[date] = mapped_column(Date, nullable=False)
    test_start: Mapped[date] = mapped_column(Date, nullable=False)
    test_end: Mapped[date] = mapped_column(Date, nullable=False)
    n_train: Mapped[int] = mapped_column(Integer, nullable=False)
    n_test: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_auc: Mapped[float] = mapped_column(Double, nullable=False)
    auroc: Mapped[float] = mapped_column(Double, nullable=False)
    brier: Mapped[float] = mapped_column(Double, nullable=False)
    precision_at_budget: Mapped[float | None] = mapped_column(Double)
    recall_at_budget: Mapped[float | None] = mapped_column(Double)
    precision_undefined: Mapped[bool] = mapped_column(Boolean, nullable=False)
    alerts: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_value_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_benefit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_drawdown_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    var95_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    es975_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mc_runs: Mapped[int] = mapped_column(Integer, nullable=False)
    mc_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LEN), nullable=False)
    entity_disjoint: Mapped[bool] = mapped_column(Boolean, nullable=False)
    test_fold_touched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AblationRow(Base):
    """One row of the ablation table — "the rigor score" (spec §7.6)."""

    __tablename__ = "ablation_row"
    __table_args__ = (UniqueConstraint("run_id", "variant", "corpus", name="uq_ablation_row"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    variant: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    corpus: Mapped[str] = mapped_column(String(64), nullable=False)
    pr_auc: Mapped[float] = mapped_column(Double, nullable=False)
    net_benefit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LEN), nullable=False)
    ci_low: Mapped[float] = mapped_column(Double, nullable=False)
    ci_high: Mapped[float] = mapped_column(Double, nullable=False)
    ci_method: Mapped[str] = mapped_column(String(64), nullable=False)
    n_resamples: Mapped[int] = mapped_column(Integer, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)


class ValidationMetric(Base):
    """A named scalar from the validation run, with its unit and its caveat."""

    __tablename__ = "validation_metric"
    __table_args__ = (UniqueConstraint("run_id", "name", "corpus", name="uq_validation_metric"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Double, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32))
    corpus: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    n: Mapped[int | None] = mapped_column(Integer)


class CurvePoint(Base):
    """One point on a reliability, PR, importance or typology-recall curve.

    One table for the curve families the validation page draws: they share shape
    (an index, two coordinates, a population count) and nothing else, and four
    near-identical tables would be four chances to get an axis label wrong.
    """

    __tablename__ = "curve_point"
    __table_args__ = (
        UniqueConstraint("run_id", "family", "point_index", name="uq_curve_point"),
        CheckConstraint(
            "family IN ('reliability','pr_curve','shap_global','typology_recall',"
            "'per_typology_precision')",
            name="ck_curve_point_family",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    family: Mapped[str] = mapped_column(String(32), nullable=False)
    point_index: Mapped[int] = mapped_column(Integer, nullable=False)
    x: Mapped[float] = mapped_column(Double, nullable=False)
    y: Mapped[float] = mapped_column(Double, nullable=False)
    n: Mapped[int | None] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(128))
    operating_point: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    currency: Mapped[str | None] = mapped_column(CHAR(CURRENCY_LEN))


class ConfusionCell(Base):
    """Confusion matrix at the budget, not over the whole ranking."""

    __tablename__ = "confusion_cell"
    __table_args__ = (UniqueConstraint("run_id", "label", "prediction", name="uq_confusion_cell"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    label: Mapped[str] = mapped_column(String(16), nullable=False)
    prediction: Mapped[str] = mapped_column(String(16), nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)


class FairnessRow(Base):
    """False-positive rate for one bucket of one unprotected proxy axis.

    plan §12: no protected attributes exist in either corpus, so the honest move is
    to say that and then check anyway along amount decile, account age, activity
    volume and community size. The axis and its name are stored together so the UI
    prints why the axis exists.
    """

    __tablename__ = "fairness_row"
    __table_args__ = (UniqueConstraint("run_id", "axis", "bucket", name="uq_fairness_row"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    axis: Mapped[str] = mapped_column(String(64), nullable=False)
    axis_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    fp_rate: Mapped[float] = mapped_column(Double, nullable=False)
    fn_rate: Mapped[float | None] = mapped_column(Double)
    n: Mapped[int] = mapped_column(Integer, nullable=False)


class PerturbationRow(Base):
    """A robustness check's result, with what was perturbed and by how much."""

    __tablename__ = "perturbation_row"
    __table_args__ = (UniqueConstraint("run_id", "kind", name="uq_perturbation_row"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    magnitude: Mapped[float] = mapped_column(Double, nullable=False)
    result: Mapped[float] = mapped_column(Double, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)


# --- decisions, audit chain, outbox (API-owned, transactional) -------------


class Case(Base):
    """The unit a decision is attached to: one account, pinned to one run.

    The table is ``review_case`` because ``case`` is reserved in Postgres and a
    quoted identifier leaks into every hand-written SQL string in the API.

    A case pins its run so a packet rendered later shows the evidence as it was at
    decision time (plan §15). ``version`` is the optimistic-concurrency token: the
    UPDATE carries ``WHERE version = :expected``, so two analysts writing at once
    produce one 200 and one 409 rather than a last-write-wins overwrite.
    """

    __tablename__ = "review_case"
    __table_args__ = (
        UniqueConstraint("run_id", "account_key", name="uq_case_run_key"),
        CheckConstraint(_state_check("status", CASE_STATUSES), name="ck_case_status"),
        CheckConstraint("version >= 1", name="ck_case_version_positive"),
    )

    case_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), primary_key=True)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'open'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    current_decision_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Decision(Base):
    """An append-only decision, chained, with four-eyes state.

    Invariants this table enforces in the database rather than in a route:

    * ``reason`` is not null and not blank — an empty justification is refused
      server-side (plan §15), because the sentence that survives is the one that
      says why.
    * a reversal points at the row it reverses and is itself a new row; nothing
      here is ever UPDATEd except the four-eyes confirmation columns and the
      outbox handoff state.
    * ``chain_seq`` is unique and contiguous with ``audit_seq``-style hashing, so
      ``make verify-audit`` can name the first broken link.
    """

    __tablename__ = "decision"
    __table_args__ = (
        UniqueConstraint("case_id", "decision_seq", name="uq_decision_case_seq"),
        UniqueConstraint("chain_seq", name="uq_decision_chain_seq"),
        CheckConstraint(_state_check("action", DECISION_ACTIONS), name="ck_decision_action"),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_decision_reason_not_blank"),
        CheckConstraint(
            _state_check("four_eyes_state", FOUR_EYES_STATES), name="ck_decision_four_eyes"
        ),
        CheckConstraint(
            "four_eyes_state <> 'confirmed' OR confirmed_by IS NOT NULL",
            name="ck_decision_confirmer_present",
        ),
        Index("ix_decision_run_key", "run_id", "account_key"),
    )

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chain_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    case_id: Mapped[str] = mapped_column(
        CHAR(RUN_ID_LEN), ForeignKey("review_case.case_id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    account_key: Mapped[str] = mapped_column(CHAR(ACCOUNT_KEY_LEN), nullable=False)
    decision_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    exposure_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LEN), nullable=False)
    four_eyes_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    four_eyes_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'not_required'")
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_of_decision_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("decision.decision_id")
    )
    decided_on_superseded_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    case_version_after: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class AuditEvent(Base):
    """The system-side chain: what happened, not what was decided.

    Kept in its own chain from ``decision`` because decisions are signed by people
    and audit events are emitted by the system; interleaving them would put a
    machine heartbeat inside the sequence an investigator attests to.
    """

    __tablename__ = "audit_event"
    __table_args__ = (
        UniqueConstraint("chain_seq", name="uq_audit_event_chain_seq"),
        Index("ix_audit_event_subject_seq", "subject", "chain_seq"),
    )

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chain_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class OutboxMessage(Base):
    """The outbox: what the database promises has been sent (02 §E, non-negotiable).

    The decision transaction writes the audit row and this row in the same
    Postgres transaction, and an RQ worker drains it afterwards. The alternative —
    POSTing inside the request — eventually writes the audit row and fails the
    call, at which point the only record of the delivery is in a process that has
    already restarted.

    ``case_seq`` orders messages within a case and nothing else. Global ordering
    would require a single-threaded drain, and 02 §E refuses that trade.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_idempotency"),
        UniqueConstraint("case_id", "case_seq", name="uq_outbox_case_seq"),
        CheckConstraint(_state_check("status", OUTBOX_STATUSES), name="ck_outbox_status"),
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
        CheckConstraint("max_attempts > 0", name="ck_outbox_max_attempts"),
        Index("ix_outbox_status_next", "status", "next_attempt_at"),
    )

    outbox_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"), nullable=False)
    case_id: Mapped[str] = mapped_column(
        CHAR(RUN_ID_LEN), ForeignKey("review_case.case_id"), nullable=False
    )
    decision_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    case_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    sink_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_url: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    attempt_log: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRun(Base):
    """A record of every queued job, so a job id maps to a run and a trace.

    RQ keeps its own state in Redis; that state does not survive a flush and it
    cannot be joined to a run. The row here is what makes "what did we run, when,
    and what came out" answerable from the database alone — the same argument the
    outbox makes about deliveries (02 §E: the database is the only source of truth
    about what has been sent).
    """

    __tablename__ = "job_run"
    __table_args__ = (Index("ix_job_run_kind_created", "kind", "created_at"),)

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[str | None] = mapped_column(CHAR(RUN_ID_LEN), ForeignKey("run.run_id"))
    queue: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(64))


__all__ = [
    "ACCOUNT_KEY_LEN",
    "CASE_STATUSES",
    "CURRENCY_LEN",
    "DECISION_ACTIONS",
    "IMMUTABLE_STATES",
    "RUN_ID_LEN",
    "RUN_PROVENANCES",
    "RUN_STATES",
    "STAGE_NAMES",
    "STAGE_STATUSES",
    "AblationRow",
    "Account",
    "AccountMembership",
    "AuditEvent",
    "BacktestFold",
    "BandDefinition",
    "Base",
    "Batch",
    "Case",
    "Community",
    "ConfusionCell",
    "CurvePoint",
    "DatasetFile",
    "DatasetSource",
    "DriftPeriod",
    "Economics",
    "ErasureRequest",
    "EvidenceEvent",
    "FairnessRow",
    "GraphEdge",
    "JobRun",
    "Measurement",
    "ModelDisagreement",
    "OutboxMessage",
    "PerturbationRow",
    "Policy",
    "PolicyAllocation",
    "PolicySimulation",
    "PseudonymMap",
    "QuarantineRow",
    "RatingMigration",
    "RuleHit",
    "Run",
    "Score",
    "ScorecardAttribute",
    "ScorecardBin",
    "ScorecardPoint",
    "ScorecardSpec",
    "ShapContribution",
    "StageEvent",
    "Transaction",
    "ValidationMetric",
]
