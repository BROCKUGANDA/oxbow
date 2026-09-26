"""The outbox drain: the only code that turns a stored promise into a delivery.

Plan §13's sentence — *the database is the only source of truth about what has been
sent* — has a consequence that is easy to lose: the worker may not remember anything
that matters. Every decision the drain makes reads from the row, so a worker that
restarts mid-flight resumes from ``status`` and ``next_attempt_at`` rather than from a
dict that died with the previous process.

The four behaviours that a reviewer of this file should be able to point at:

* **ordering per ``case_id``, never global.** A row is claimable only when no earlier
  ``case_seq`` for the same case is unsent. That is what lets several workers drain in
  parallel without a case's escalation being delivered before its dismissal.
* **the ladder is the adapter's, not a local copy.** Delays come from
  ``oxbow.adapters.retry.plan_delay`` — 1s/4s/16s/64s/256s, full jitter, five
  attempts — so the outbox and any other caller cannot drift about when attempt four
  happens. The worker *stores* the next attempt time rather than sleeping it, because
  blocking a queue slot for four minutes behind one dead endpoint is how one bad
  consumer stops every other case being delivered (03 §K).
* **a 4xx dead-letters immediately.** A consumer that rejected the payload on
  validation grounds will reject it again; five attempts is a hammer pointed at
  someone's schema (plan §18).
* **signing is not reimplemented here.** The payload is signed by
  ``oxbow.adapters.signing`` inside the sink, on the raw bytes actually sent — the
  copy this module used to keep is why two implementations disagreed once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from api import decisions as decision_module
from api.deps import Container
from api.problems import DependencyUnavailable
from oxbow.adapters.retry import (
    MAX_ATTEMPTS,
    NonRetryableDeliveryError,
    RetryPlan,
    plan_delay,
)
from oxbow.adapters.warehouse.models import OutboxMessage
from oxbow.ports.case_sink import CaseSink, assert_self_describing
from oxbow.ports.notify import Notification, NotifySink
from oxbow.ports.report import ReportSink

DRAIN_BATCH_DEFAULT: Final = 20
DRAIN_BATCH_MAX: Final = 200
CLAIMABLE_STATUSES: Final = ("pending", "in_flight")

# Rows left ``in_flight`` by a worker that died holding them become claimable again
# after this long. Without it, a crash between claim and delivery would strand the
# row: the status says someone else is sending it, and that process is gone.
IN_FLIGHT_STALE_SECONDS: Final = 300


class SinkConfigurationError(DependencyUnavailable):
    """A row names a sink this deployment has no destination for."""


@dataclass(slots=True)
class DeliveryOutcome:
    """What one attempt did, in the words the ledger and the response both show."""

    outbox_id: int
    case_id: str
    case_seq: int
    sink_id: str
    idempotency_key: str
    attempts: int
    status: str
    detail: str
    next_attempt_at: datetime | None = None
    jitter_delay_seconds: float | None = None


@dataclass(slots=True)
class DrainReport:
    """One drain pass, summarised. Returned to the caller and logged, never swallowed."""

    started_at: datetime
    claimed: int = 0
    sent: int = 0
    retrying: int = 0
    dead: int = 0
    rejected_permanently: int = 0
    outcomes: list[DeliveryOutcome] = field(default_factory=list)

    @property
    def pending(self) -> int:
        return self.claimed - self.sent - self.dead - self.rejected_permanently

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "claimed": self.claimed,
            "sent": self.sent,
            "retrying": self.retrying,
            "dead": self.dead,
            "rejected_permanently": self.rejected_permanently,
            "outcomes": [
                {
                    "outbox_id": outcome.outbox_id,
                    "case_id": outcome.case_id,
                    "case_seq": outcome.case_seq,
                    "sink_id": outcome.sink_id,
                    "idempotency_key": outcome.idempotency_key,
                    "attempts": outcome.attempts,
                    "status": outcome.status,
                    "detail": outcome.detail,
                    "next_attempt_at": None
                    if outcome.next_attempt_at is None
                    else outcome.next_attempt_at.isoformat(),
                    "jitter_delay_seconds": outcome.jitter_delay_seconds,
                }
                for outcome in self.outcomes
            ],
        }


def resolve_sink(container: Container, sink_id: str, session: Session) -> Any:
    """Bind a stored ``sink_id`` back to the adapter that should serve it.

    The outbox row records the sink it was written against, so a later change to the
    environment does not silently re-point an old delivery at a new consumer: the row
    says where it was *meant* to go, and an unknown name is an error rather than a
    re-route.
    """
    if sink_id == decision_module.OUTBOX_SINK_CASE:
        sink = container.case_sink_factory()
        if isinstance(sink, CaseSink) or hasattr(sink, "emit"):
            return sink
        raise SinkConfigurationError(f"sink_id {sink_id!r} resolved to a non-case sink")
    if sink_id == decision_module.OUTBOX_SINK_NOTIFY:
        sink = container.notify_sink_factory()
        if hasattr(sink, "notify"):
            return sink
        raise SinkConfigurationError(f"sink_id {sink_id!r} resolved to a non-notify sink")
    raise SinkConfigurationError(
        f"outbox row {sink_id!r} names a sink this deployment cannot build. Known sinks: "
        f"{decision_module.OUTBOX_SINK_CASE}, {decision_module.OUTBOX_SINK_NOTIFY}."
    )


def claim_due(
    session: Session, *, now: datetime, batch: int = DRAIN_BATCH_DEFAULT
) -> list[OutboxMessage]:
    """Rows whose time has come, honouring per-case order, claimed under a row lock.

    ``FOR UPDATE SKIP LOCKED`` is what makes this safe with several workers: each
    takes a different row instead of two workers POSTING the same case and one of them
    finding out at the consumer's dedupe table. The per-case guard is the ordering
    rule, and it is expressed as a NOT EXISTS rather than in Python so that two
    adjacent rows for one case cannot be claimed by two workers at once.
    """
    stale = now - timedelta(seconds=IN_FLIGHT_STALE_SECONDS)
    earlier = aliased(OutboxMessage)
    earlier_unsent = (
        select(func.count())
        .select_from(earlier)
        .where(
            earlier.case_id == OutboxMessage.case_id,
            earlier.case_seq < OutboxMessage.case_seq,
            earlier.status.notin_(("sent", "dead")),
        )
        .correlate(OutboxMessage)
        .exists()
    )
    statement = (
        select(OutboxMessage)
        .where(
            OutboxMessage.status.in_(CLAIMABLE_STATUSES),
            OutboxMessage.next_attempt_at <= now,
            ~earlier_unsent,
        )
        .order_by(OutboxMessage.case_id.asc(), OutboxMessage.case_seq.asc())
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    rows = list(session.execute(statement).scalars())
    # A row left in_flight by a dead worker is claimed on the next pass rather than
    # becoming a permanent ghost: the status is a lock, not a fact about the network.
    return [
        row
        for row in rows
        if row.status != "in_flight"
        or (row.next_attempt_at is not None and row.next_attempt_at <= stale)
    ]


def drain_once(
    container: Container,
    *,
    now: datetime | None = None,
    batch: int = DRAIN_BATCH_DEFAULT,
    session: Session | None = None,
) -> DrainReport:
    """Attempt every due row once, and return exactly what happened.

    The session is injectable because the integration suite drives one drain inside a
    transaction it can inspect and roll back; production passes nothing and gets its
    own commit, which is the only way a delivery that succeeded stays recorded.
    """
    started = now or datetime.now(UTC)
    report = DrainReport(started_at=started)
    own_session = session is None
    active = session if session is not None else container.new_session()
    try:
        rows = claim_due(active, now=started, batch=batch)
        report.claimed = len(rows)
        for row in rows:
            outcome = attempt_delivery(container, active, row, now=started)
            report.outcomes.append(outcome)
            if outcome.status == "sent":
                report.sent += 1
            elif outcome.status == "dead":
                report.dead += 1
            elif outcome.status == "rejected":
                report.rejected_permanently += 1
            else:
                report.retrying += 1
        if own_session:
            active.commit()
    except BaseException:
        if own_session:
            active.rollback()
        raise
    finally:
        if own_session:
            active.close()
    return report


def attempt_delivery(
    container: Container, session: Session, row: OutboxMessage, *, now: datetime
) -> DeliveryOutcome:
    """One attempt at one row. Every branch writes state; none of them swallows."""
    attempts = int(row.attempts) + 1
    payload = dict(row.payload or {})
    sink: CaseSink | NotifySink | ReportSink
    outcome = DeliveryOutcome(
        outbox_id=int(row.outbox_id),
        case_id=str(row.case_id),
        case_seq=int(row.case_seq),
        sink_id=str(row.sink_id),
        idempotency_key=str(row.idempotency_key),
        attempts=attempts,
        status="retrying",
        detail="",
    )
    try:
        sink = resolve_sink(container, str(row.sink_id), session)
    except SinkConfigurationError as exc:
        _mark_dead(session, row, attempts=attempts, error=str(exc), at=now)
        outcome.status = "dead"
        outcome.detail = str(exc)
        return outcome

    if sink.has_delivered(str(row.idempotency_key)):
        # The sink already holds this key: this is at-least-once delivery meeting its
        # idempotent consumer, and the honest outcome is "sent", not a second POST.
        _mark_sent(session, row, attempts=attempts, at=now, note="deduplicated by sink")
        outcome.status = "sent"
        outcome.detail = "sink already holds this idempotency key"
        return outcome

    try:
        if row.sink_id == decision_module.OUTBOX_SINK_CASE:
            bundle = decision_module.bundle_from_payload(payload)
            assert_self_describing(bundle.to_payload())
            receipt = sink.emit(bundle)  # type: ignore[union-attr]
        else:
            notification = _notification_from_payload(payload)
            receipt = sink.notify(notification)  # type: ignore[union-attr]
    except NonRetryableDeliveryError as exc:
        # A permanent refusal: the payload is wrong, and the consumer has said so.
        # Retrying it five times is hammering a validation error (plan §18), so the
        # row dead-letters now with the consumer's own words attached.
        _mark_dead(session, row, attempts=attempts, error=str(exc), at=now)
        outcome.status = "rejected"
        outcome.detail = str(exc)
        return outcome
    except Exception as exc:  # transient by contract: everything retryable lands here
        plan: RetryPlan = plan_delay(attempts)
        if attempts >= MAX_ATTEMPTS or plan.exhausted:
            _mark_dead(session, row, attempts=attempts, error=str(exc), at=now)
            outcome.status = "dead"
        else:
            next_at = now + timedelta(seconds=plan.delay_seconds)
            _mark_retry(session, row, attempts=attempts, at=now, error=str(exc), next_at=next_at)
            outcome.status = "retrying"
            outcome.next_attempt_at = next_at
            outcome.jitter_delay_seconds = plan.delay_seconds
        outcome.detail = str(exc)
        return outcome

    _mark_sent(session, row, attempts=attempts, at=now, note=receipt.consumer)
    outcome.status = "sent"
    outcome.detail = f"accepted by {receipt.consumer}"
    return outcome


def _notification_from_payload(payload: dict[str, Any]) -> Notification:
    """Rebuild the port's notification from stored JSON, with its money basis intact.

    ``Notification.__post_init__`` refuses a money figure with no currency or
    assumptions, so a row whose payload has drifted since it was written fails here,
    loudly, instead of arriving as a confident one-liner about a number nobody can
    account for.
    """
    return Notification(
        notification_id=str(payload["notification_id"]),
        run_id=str(payload["run_id"]),
        severity=str(payload["severity"]),
        title=str(payload["title"]),
        body=str(payload["body"]),
        occurred_at=datetime.now(UTC),
        case_id=payload.get("case_id"),
        account_key=payload.get("account_key"),
        requires_four_eyes=bool(payload.get("requires_four_eyes", False)),
        money_minor=None
        if payload.get("expected_value_minor") is None
        else int(payload["expected_value_minor"]),
        currency=payload.get("currency"),
        assumptions=dict(payload.get("assumptions") or {}) or None,
        model_version=payload.get("model_version"),
        schema_version=str(payload["schema_version"]),
    )


def _append_attempt(
    session: Session, row: OutboxMessage, *, at: datetime, status: str, error: str | None
) -> None:
    """Record the attempt on the row itself, so the retry history survives the worker.

    ``attempt_log`` is a JSONB array rather than a table because nothing queries it;
    it exists so that a delivery someone disputes can be answered with the five
    attempts and their messages, from the row in question.
    """
    log = list(row.attempt_log or [])
    log.append(
        {
            "attempt": int(row.attempts) + 1,
            "at": at.isoformat(),
            "status": status,
            "error": None if error is None else str(error)[:500],
        }
    )
    row.attempt_log = log


def _mark_sent(
    session: Session, row: OutboxMessage, *, attempts: int, at: datetime, note: str
) -> None:
    _append_attempt(session, row, at=at, status="sent", error=None)
    row.attempts = attempts
    row.status = "sent"
    row.sent_at = at
    row.last_error = None
    session.flush()


def _mark_retry(
    session: Session,
    row: OutboxMessage,
    *,
    attempts: int,
    at: datetime,
    error: str,
    next_at: datetime,
) -> None:
    _append_attempt(session, row, at=at, status="retrying", error=error)
    row.attempts = attempts
    row.status = "pending"
    row.next_attempt_at = next_at
    row.last_error = str(error)[:1000]
    session.flush()


def _mark_dead(
    session: Session, row: OutboxMessage, *, attempts: int, error: str, at: datetime
) -> None:
    """Dead-letter, with the consumer's refusal kept readable.

    The row is not deleted: a delivery that was never made is a fact a later reader
    needs, and the ledger endpoint lists dead rows precisely so the gap is visible in
    the product rather than reconstructed from logs.
    """
    _append_attempt(session, row, at=at, status="dead", error=error)
    row.attempts = attempts
    row.status = "dead"
    row.dead_at = at
    row.last_error = str(error)[:1000]
    session.flush()


def ledger(
    session: Session, *, status: str | None = None, case_id: str | None = None, limit: int = 50
) -> list[OutboxMessage]:
    """The delivery ledger: what is waiting, what went, what was refused."""
    statement = select(OutboxMessage).order_by(OutboxMessage.outbox_id.desc()).limit(limit)
    if status:
        statement = statement.where(OutboxMessage.status == status)
    if case_id:
        statement = statement.where(OutboxMessage.case_id == case_id)
    return list(session.execute(statement).scalars())


def queue_depth(session: Session) -> dict[str, int]:
    """Counts by status, for the degraded banner and the operator's sanity."""
    rows = session.execute(
        select(OutboxMessage.status, func.count()).group_by(OutboxMessage.status)
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    for status in ("pending", "in_flight", "sent", "dead"):
        counts.setdefault(status, 0)
    return counts


__all__ = [
    "CLAIMABLE_STATUSES",
    "DRAIN_BATCH_DEFAULT",
    "DRAIN_BATCH_MAX",
    "IN_FLIGHT_STALE_SECONDS",
    "DeliveryOutcome",
    "DrainReport",
    "SinkConfigurationError",
    "attempt_delivery",
    "claim_due",
    "drain_once",
    "ledger",
    "queue_depth",
    "resolve_sink",
]
