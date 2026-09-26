"""The decision write path: one transaction, three rows, four-eyes before delivery.

This is the whole of plan §13's outbox requirement in one place. A decision is not a
row; it is a *commit* that contains a decision row, its audit-chain row and — only
when the four-eyes rule is satisfied — its outbox row. All three or none. The
alternative that looks equivalent, POSTing to the consumer inside the request,
eventually writes the audit row and fails the call, at which point the only record of
the delivery is in a process that has already restarted.

Four invariants are enforced here rather than trusted:

* **optimistic concurrency.** The case UPDATE carries ``WHERE version = :expected``,
  so two analysts writing the same case produce one 200 and one 409, and the loser's
  409 carries the stored version and the current decision so the UI can show a merge
  view instead of announcing that work vanished.
* **four-eyes before the outbox, not before the audit.** A decision above
  ``four_eyes.threshold_exposure_minor`` is *recorded* immediately — it happened, and
  the chain has to say so — but no outbox row exists until a second, different
  reviewer confirms it. Recording is history; delivery is an action.
* **the confirmer is a different subject, not just a different role.** The trigger
  covers "already confirmed"; the subject check is here, and it is what makes
  four-eyes a rule instead of a label an analyst-with-two-roles could satisfy alone.
* **``decided_on_superseded_run`` is stamped, not blocked.** Plan §15 allows deciding
  on a superseded run and requires the fact to survive into the audit row and the
  packet, because an investigation tool that refuses the read would hide the case.

Money is minor units in and minor units out; the exposure that drives the four-eyes
test is read from the ``economics`` row the pipeline wrote, never recomputed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.problems import (
    BadRequest,
    Conflict,
    DependencyUnavailable,
    Forbidden,
    NotFound,
    Unprocessable,
    VersionConflict,
)
from api.readmodel import ReadModel, money  # `money` is the only money constructor
from api.security import Principal
from oxbow.adapters.warehouse.models import (
    Case,
    Economics,
    OutboxMessage,
    Run,
)
from oxbow.adapters.warehouse.models import (
    Decision as DecisionRow,
)
from oxbow.audit.chain import GENESIS_HASH, ChainRow, PendingChainRow, append_row
from oxbow.ports.audit import AuditAppendError, AuditSink
from oxbow.ports.case_sink import (
    CASE_SCHEMA_VERSION,
    CalibrationReading,
    CaseBundle,
    DecisionRecord,
    EconomicsBlock,
    MonteCarloInterval,
    ScoreBlock,
    build_idempotency_key,
)
from oxbow.ports.warehouse import is_ulid
from oxbow.quant.economics import Economics as EconomicsConfig

OUTBOX_SINK_CASE: Final = "webhook"
OUTBOX_SINK_NOTIFY: Final = "slack-notify"
OUTBOX_SCHEMA_VERSION: Final = CASE_SCHEMA_VERSION
GENESIS_DIGEST: Final = GENESIS_HASH

# The decision chain is the *human* attestation sequence, separate from the system's
# ``audit_event`` chain on purpose: interleaving them would put a machine heartbeat
# inside the sequence a reviewer signs, and a row the reviewer cannot reproduce from
# the payload is not a row the reviewer attested to.
DECISION_ACTION_SUBJECT_PREFIX: Final = "case:"


class FourEyesError(Forbidden):
    """The confirmation is being attempted by the person it must not accept."""

    code, title = "four-eyes-not-satisfied", "Second reviewer required"


@dataclass(frozen=True, slots=True)
class DecisionWrite:
    """One validated decision request, before anything is written."""

    action: str
    reason: str
    expected_version: int
    reversal_of_decision_id: str | None
    principal: Principal
    trace_id: str | None


@dataclass(frozen=True, slots=True)
class CaseSnapshot:
    """The case row as read inside the transaction, with its run pinned."""

    case_id: str
    run_id: str
    account_key: str
    status: str
    version: int
    current_decision_seq: int

    @staticmethod
    def from_row(row: Case) -> CaseSnapshot:
        return CaseSnapshot(
            case_id=str(row.case_id),
            run_id=str(row.run_id),
            account_key=str(row.account_key),
            status=str(row.status),
            version=int(row.version),
            current_decision_seq=int(row.current_decision_seq),
        )


def open_case(session: Session, read_model: ReadModel, *, run_id: str, account_key: str) -> Case:
    """Idempotently open the case for one account in one run.

    Opening is not a decision and carries no reason, so it is the one write here that
    is deliberately not chained. The unique ``(run_id, account_key)`` constraint makes
    a double open a no-op rather than a second case with a second history.
    """
    if not is_ulid(run_id):
        raise BadRequest(f"run_id {run_id!r} is not a ULID (DEV-003)")
    run = read_model.run_row(run_id)
    if str(run["state"]) == "running":
        raise Conflict(
            f"run {run_id} is still running, so its scores are partial. A case opened against a "
            "half-written run would be pinned to numbers that are still changing; wait for "
            "complete or name a finished run.",
            run_id=run_id,
        )
    read_model.score_row(run_id, account_key)
    statement = select(Case).where(Case.run_id == run_id, Case.account_key == account_key)
    existing = session.execute(statement).scalars().first()
    if existing is not None:
        return existing
    case = Case(
        case_id=_new_case_id(),
        run_id=run_id,
        account_key=account_key,
        status="open",
        version=1,
        current_decision_seq=0,
    )
    session.add(case)
    try:
        session.flush()
    except IntegrityError:
        # Another request opened it between the SELECT and the INSERT. Both callers
        # must see the same row, so the loser re-reads rather than failing a user who
        # double-clicked.
        session.rollback()
        found = session.execute(statement).scalars().one_or_none()
        if found is None:  # pragma: no cover - the INSERT we just lost created it
            raise DependencyUnavailable(
                f"case for {account_key} in {run_id} vanished during creation"
            ) from None
        return found
    return case


def open_case_from_id(session: Session, case_id: str) -> Case:
    """Load an existing case row, or refuse with the reason a deep link can fail.

    Decisions attach to a case, and a case exists only once opened from the queue, so a
    404 here is usually a bookmark from before a warehouse reload rather than a
    permissions problem — which is why the message says so.
    """
    case = session.get(Case, case_id)
    if case is None:
        raise NotFound(
            f"no case {case_id!r}. Cases are opened from the alert queue, so a case id "
            "typed from an old deep link may predate a warehouse reload; that is not a "
            "permissions problem, and saying which it is costs one sentence."
        )
    return case


def _new_case_id() -> str:
    from ulid import ULID

    return str(ULID()).upper()


def record_decision(
    session: Session,
    read_model: ReadModel,
    audit_sink: AuditSink,
    economics: EconomicsConfig,
    *,
    case: Case,
    write: DecisionWrite,
) -> dict[str, Any]:
    """Append one decision, its audit row, and (when allowed) its outbox row.

    The caller owns the transaction; this function only flushes. That ordering is what
    makes the three rows atomic, and it is why the audit sink is constructed with this
    session rather than opening one of its own.
    """
    snapshot = CaseSnapshot.from_row(case)
    _require_version(snapshot, write.expected_version, session, write.principal)
    score = read_model.score_row(snapshot.run_id, snapshot.account_key)
    economic_row = _economics_row(
        session, run_id=snapshot.run_id, account_key=snapshot.account_key
    )
    exposure_minor = int(economic_row.exposure_minor)
    currency = str(economic_row.currency)
    _require_action_matches_reversal(write)
    four_eyes_required = exposure_minor > economics.four_eyes.threshold_exposure_minor
    decided_on_superseded = _run_is_superseded(session, snapshot.run_id)
    decision_seq = snapshot.current_decision_seq + 1
    idempotency_key = build_idempotency_key(snapshot.run_id, snapshot.case_id, decision_seq)

    chain_seq, prev_hash = _decision_chain_head(session)
    payload = _decision_payload(
        snapshot=snapshot,
        write=write,
        decision_seq=decision_seq,
        chain_seq=chain_seq,
        score=score,
        economic_row=economic_row,
        exposure_minor=exposure_minor,
        currency=currency,
        four_eyes_required=four_eyes_required,
        decided_on_superseded=decided_on_superseded,
    )
    pending = append_row(
        prev=_chain_tip(session),
        occurred_at=datetime.now(UTC),
        actor_id=write.principal.subject,
        subject=f"{DECISION_ACTION_SUBJECT_PREFIX}{snapshot.case_id}",
        action=f"decision:{write.action}",
        payload=payload,
    )
    row_hash = pending.row_hash
    decision = DecisionRow(
        decision_id=_new_decision_id(),
        chain_seq=chain_seq,
        case_id=snapshot.case_id,
        run_id=snapshot.run_id,
        account_key=snapshot.account_key,
        decision_seq=decision_seq,
        action=write.action,
        reason=write.reason,
        actor_id=write.principal.subject,
        actor_roles=list(write.principal.roles),
        exposure_minor=exposure_minor,
        currency=currency,
        four_eyes_required=four_eyes_required,
        four_eyes_state="pending" if four_eyes_required else "not_required",
        reversal_of_decision_id=write.reversal_of_decision_id,
        decided_on_superseded_run=decided_on_superseded,
        case_version_after=snapshot.version + 1,
        idempotency_key=idempotency_key,
        trace_id=write.trace_id,
        occurred_at=pending.occurred_at,
        prev_hash=prev_hash,
        row_hash=row_hash,
        payload=payload,
    )
    session.add(decision)
    try:
        session.flush()
    except IntegrityError as exc:
        # Two writers read the same chain tip and both built the same link. The unique
        # constraints (``uq_decision_chain_seq``, ``uq_decision_case_seq``) are what make
        # that a refusal rather than two rows claiming one sequence, and plan §13 fixes the
        # answer as *one success and one 409* — so the loser is reported as a conflict, not
        # as a server fault. Nothing was committed: the caller's transaction rolls back, so
        # the audit row and the outbox row this decision would have produced do not exist.
        raise Conflict(
            f"another writer appended to the decision chain at the same instant "
            f"({str(exc.orig).splitlines()[0][:200] if exc.orig else exc}). This decision was "
            "not recorded, and no delivery was queued for it; re-read the case and decide "
            "again against the current version."
        ) from exc

    _append_audit(
        audit_sink,
        snapshot=snapshot,
        write=write,
        decision=decision,
        pending=pending,
        exposure_minor=exposure_minor,
        currency=currency,
    )

    case.version = snapshot.version + 1
    case.current_decision_seq = decision_seq
    case.status = "pending_four_eyes" if four_eyes_required else "decided"
    if write.action == "reverse":
        case.status = "reversed"

    outbox_queued = False
    if not four_eyes_required:
        queue_decision_deliveries(
            session, read_model=read_model, economics=economics, decision=decision, score=score
        )
        outbox_queued = True
    session.flush()

    return _write_result(
        decision=decision,
        case=case,
        outbox_queued=outbox_queued,
        audit_seq=pending.seq,
    )


def confirm_decision(
    session: Session,
    read_model: ReadModel,
    audit_sink: AuditSink,
    economics: EconomicsConfig,
    *,
    decision_id: str,
    principal: Principal,
    expected_version: int,
    confirmation_note: str,
    trace_id: str | None,
) -> dict[str, Any]:
    """Satisfy four-eyes: a second, different reviewer releases the outbox row.

    The confirmation is the only permitted UPDATE on a decision row, and the trigger in
    migration 0002 enforces that in the database rather than relying on this module
    never calling anything else. Everything the first reviewer wrote is unchanged,
    including the digest.
    """
    decision = session.get(DecisionRow, decision_id)
    if decision is None:
        raise NotFound(
            f"no decision {decision_id!r}. Decision ids are printed on every timeline row, so a "
            "stale one usually means the tab predates a re-decision."
        )
    if not principal.is_reviewer:
        raise FourEyesError(
            f"confirming a four-eyes decision needs the reviewer role; {principal.display_name} "
            f"holds {list(principal.roles)}"
        )
    if decision.four_eyes_state == "confirmed":
        raise Conflict(
            f"decision {decision_id} was already confirmed by {decision.confirmed_by}; a "
            "second confirmation would rewrite who attested to it",
            run_id=str(decision.run_id),
        )
    if decision.four_eyes_state == "not_required":
        raise BadRequest(
            f"decision {decision_id} never required a second reviewer (exposure "
            f"{decision.exposure_minor} {decision.currency} against a threshold of "
            f"{economics.four_eyes.threshold_exposure_minor}), so there is nothing to confirm",
            run_id=str(decision.run_id),
        )
    if str(decision.actor_id) == principal.subject:
        raise FourEyesError(
            f"{principal.display_name} wrote this decision and cannot confirm it. Four-eyes means "
            "a second subject, not a second role held by the same person."
        )
    case = session.get(Case, str(decision.case_id))
    if case is None:  # pragma: no cover - the decision's FK guarantees it
        raise DependencyUnavailable(f"decision {decision_id} references a case that is absent")
    _require_version(CaseSnapshot.from_row(case), expected_version, session, principal)

    decision.four_eyes_state = "confirmed"
    decision.confirmed_by = principal.subject
    decision.confirmed_at = datetime.now(UTC)
    case.version = int(case.version) + 1
    case.status = "decided"

    pending = append_row(
        prev=_chain_tip(session),
        occurred_at=datetime.now(UTC),
        actor_id=principal.subject,
        subject=f"{DECISION_ACTION_SUBJECT_PREFIX}{decision.case_id}",
        action="decision:four_eyes_confirmed",
        payload={
            "run_id": str(decision.run_id),
            "case_id": str(decision.case_id),
            "decision_id": str(decision.decision_id),
            "decision_seq": int(decision.decision_seq),
            "confirmed_by": principal.subject,
            "confirmed_roles": list(principal.roles),
            "confirmation_note": confirmation_note,
            "original_row_hash": str(decision.row_hash),
            "trace_id": trace_id,
        },
    )
    _append_audit_sink(audit_sink, pending)
    score = read_model.score_row(str(decision.run_id), str(decision.account_key))
    queue_decision_deliveries(
        session, read_model=read_model, economics=economics, decision=decision, score=score
    )
    session.flush()
    return _write_result(
        decision=decision, case=case, outbox_queued=True, audit_seq=pending.seq
    )


def queue_decision_deliveries(
    session: Session,
    *,
    read_model: ReadModel,
    economics: EconomicsConfig,
    decision: DecisionRow,
    score: Mapping[str, Any],
) -> list[OutboxMessage]:
    """Write the outbox rows for one confirmed decision, in the caller's transaction.

    One row per sink rather than one row with two destinations, because "the webhook
    took it and Slack did not" is the real state of the world and a single row could
    only record one of the two.
    """
    economic_row = _economics_row(
        session, run_id=str(decision.run_id), account_key=str(decision.account_key)
    )
    bundle = build_case_bundle(
        decision=decision,
        economic_row=economic_row,
        score=score,
        economics=economics,
    )
    written: list[OutboxMessage] = []
    for sink_id, payload in ((OUTBOX_SINK_CASE, bundle.to_payload()),):
        written.append(
            _insert_outbox(
                session,
                sink_id=sink_id,
                run_id=str(decision.run_id),
                case_id=str(decision.case_id),
                decision_seq=int(decision.decision_seq),
                schema_version=str(payload["schema_version"]),
                payload=dict(payload),
                idempotency_key=str(payload["idempotency_key"]),
            )
        )
    if sink_needs_notification(decision):
        written.append(
            _insert_outbox(
                session,
                sink_id=OUTBOX_SINK_NOTIFY,
                run_id=str(decision.run_id),
                case_id=str(decision.case_id),
                decision_seq=int(decision.decision_seq),
                schema_version=OUTBOX_SCHEMA_VERSION,
                payload=notification_payload(decision=decision, bundle=bundle),
                idempotency_key=f"{bundle.idempotency_key}:notify",
            )
        )
    return written


def sink_needs_notification(decision: DecisionRow) -> bool:
    """Only escalations notify: a dismissal is not a message anyone is waiting for.

    Deliberately a narrow predicate rather than a knob. Notifying on every decision is
    how a channel gets muted, and a muted channel is worse than none because it looks
    like a working control.
    """
    return str(decision.action) == "escalate"


def notification_payload(*, decision: DecisionRow, bundle: CaseBundle) -> dict[str, Any]:
    """The human-facing message, carrying the same money basis as the case payload.

    ``Notification.__post_init__`` refuses to build a message with a money figure and
    no assumptions, so this payload cannot silently become more confident than the
    bundle it summarises.
    """
    payload = bundle.to_payload()
    return {
        "schema_version": OUTBOX_SCHEMA_VERSION,
        "notification_id": f"{decision.decision_id}",
        "run_id": str(decision.run_id),
        "case_id": str(decision.case_id),
        "account_key": str(decision.account_key),
        "severity": "critical" if int(decision.exposure_minor) > 0 else "info",
        "title": f"OXBOW: {decision.action} on {decision.account_key}",
        "body": (
            f"{decision.actor_id} recorded '{decision.action}' "
            f"(exposure {decision.exposure_minor} {decision.currency}); "
            f"reason: {decision.reason[:280]}"
        ),
        "requires_four_eyes": bool(decision.four_eyes_required)
        and str(decision.four_eyes_state) != "confirmed",
        "expected_value_minor": int(payload["economics"]["expected_value_minor"]),
        "currency": str(payload["economics"]["currency"]),
        "assumptions": dict(payload["economics"]["assumptions"]),
        "model_version": str(payload["model_version"]),
        "disclaimer": payload["disclaimer"],
        "advisory_only": True,
        "idempotency_key": str(payload["idempotency_key"]),
    }


def build_case_bundle(
    *,
    decision: DecisionRow,
    economic_row: Economics,
    score: Mapping[str, Any],
    economics: EconomicsConfig,
) -> CaseBundle:
    """The payload that leaves the building, assembled from stored rows only.

    Built here rather than at delivery time so a consumer receives what was true when
    the decision was signed, not a re-read of tables a later run may have replaced
    (plan §13: the assumptions travel with the money figure).
    """
    reason_codes = [
        str(item.get("code")) if isinstance(item, Mapping) else str(item)
        for item in (score.get("reason_codes") or [])
    ]
    calibration = CalibrationReading(
        band=str(score["calibration_band"]),
        observed_rate=float(score["observed_rate"]),
        n=int(score["calibration_n"]),
    )
    score_block = ScoreBlock(
        fused_score=float(score["fused_score"]),
        band=str(score["band"]),
        scorecard_points=int(score["scorecard_points"]),
        reason_codes=reason_codes,
        calibration=calibration,
        model_version=str(score["model_version"]),
        rule_ids=tuple(str(rid) for rid in (score.get("rule_ids") or [])),
    )
    monte_carlo = MonteCarloInterval(
        runs=int(economic_row.mc_runs),
        seed=int(economic_row.mc_seed),
        p05_minor=int(economic_row.mc_p05_minor),
        p50_minor=int(economic_row.mc_p50_minor),
        p95_minor=int(economic_row.mc_p95_minor),
        interval=tuple(float(value) for value in economic_row.mc_interval),
    )
    economics_block = EconomicsBlock(
        currency=str(economic_row.currency),
        exposure_minor=int(economic_row.exposure_minor),
        expected_value_minor=int(economic_row.expected_value_minor),
        recovery_rate=float(economic_row.recovery_rate),
        analyst_cost_minor=int(economic_row.analyst_cost_minor),
        friction_cost_minor=int(economic_row.friction_cost_minor),
        assumptions=dict(economic_row.assumptions),
        monte_carlo=monte_carlo,
    )
    return CaseBundle(
        run_id=str(decision.run_id),
        case_id=str(decision.case_id),
        account_key=str(decision.account_key),
        decided_at=decision.occurred_at,
        decision=DecisionRecord(
            decision_seq=int(decision.decision_seq),
            action=str(decision.action),
            reason=str(decision.reason),
            actor_id=str(decision.actor_id),
            decided_at=decision.occurred_at,
            four_eyes_confirmed_by=decision.confirmed_by,
            decided_on_superseded_run=bool(decision.decided_on_superseded_run),
        ),
        score=score_block,
        economics=economics_block,
        evidence_refs=[str(ref) for ref in (decision.payload or {}).get("evidence_refs", [])],
        transaction_ids=[str(txn) for txn in (decision.payload or {}).get("txn_ids", [])],
        provenance={
            "config_hash": (decision.payload or {}).get("config_hash"),
            "model_version": str(score["model_version"]),
            "decision_id": str(decision.decision_id),
            "row_hash": str(decision.row_hash),
            "chain_seq": int(decision.chain_seq),
            "idempotency_key": str(decision.idempotency_key),
            "recovery_rate_source": "config/economics.yaml",
        },
    )


def bundle_from_payload(payload: Mapping[str, Any]) -> CaseBundle:
    """Rebuild a stored outbox payload as the port's bundle, for delivery.

    The outbox holds JSON, not Python objects, because the row has to outlive the
    process that wrote it. Reconstructing the typed bundle on the way out is what lets
    the webhook adapter keep running its self-describing check at the boundary instead
    of the drain re-implementing that check and drifting from it.
    """
    score = payload["score"]
    economic = payload["economics"]
    decision = payload["decision"]
    calibration = score["calibration"]
    monte_carlo = economic.get("monte_carlo")
    return CaseBundle(
        run_id=str(payload["run_id"]),
        case_id=str(payload["case_id"]),
        account_key=str(payload["account_key"]),
        decided_at=datetime.fromisoformat(str(payload["decided_at"]).replace("Z", "+00:00")),
        decision=DecisionRecord(
            decision_seq=int(decision["decision_seq"]),
            action=str(decision["action"]),
            reason=str(decision["reason"]),
            actor_id=str(decision["actor_id"]),
            decided_at=datetime.fromisoformat(
                str(decision["decided_at"]).replace("Z", "+00:00")
            ),
            four_eyes_confirmed_by=decision.get("four_eyes_confirmed_by"),
            reversal_of_seq=decision.get("reversal_of_seq"),
            decided_on_superseded_run=bool(decision.get("decided_on_superseded_run", False)),
        ),
        score=ScoreBlock(
            fused_score=float(score["fused_score"]),
            band=str(score["band"]),
            scorecard_points=int(score["scorecard_points"]),
            reason_codes=tuple(str(code) for code in score.get("reason_codes") or ()),
            calibration=CalibrationReading(
                band=str(calibration["band"]),
                observed_rate=float(calibration["observed_rate"]),
                n=int(calibration["n"]),
            ),
            model_version=str(score["model_version"]),
            rule_ids=tuple(str(rid) for rid in score.get("rule_ids") or ()),
        ),
        economics=EconomicsBlock(
            currency=str(economic["currency"]),
            exposure_minor=int(economic["exposure_minor"]),
            expected_value_minor=int(economic["expected_value_minor"]),
            recovery_rate=float(economic["recovery_rate"]),
            analyst_cost_minor=int(economic["analyst_cost_minor"]),
            friction_cost_minor=int(economic["friction_cost_minor"]),
            assumptions=dict(economic.get("assumptions") or {}),
            monte_carlo=None
            if monte_carlo is None
            else MonteCarloInterval(
                runs=int(monte_carlo["runs"]),
                seed=int(monte_carlo["seed"]),
                p05_minor=int(monte_carlo["p05_minor"]),
                p50_minor=int(monte_carlo["p50_minor"]),
                p95_minor=int(monte_carlo["p95_minor"]),
                interval=tuple(float(value) for value in monte_carlo["interval"]),
            ),
        ),
        evidence_refs=tuple(str(ref) for ref in payload.get("evidence_refs") or ()),
        transaction_ids=tuple(str(txn) for txn in payload.get("transaction_ids") or ()),
        provenance=dict(payload.get("provenance") or {}),
        schema_version=str(payload["schema_version"]),
    )


# --- internals ---------------------------------------------------------------


def _insert_outbox(
    session: Session,
    *,
    sink_id: str,
    run_id: str,
    case_id: str,
    decision_seq: int,
    schema_version: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> OutboxMessage:
    """Append one delivery promise. Ordering is per case, and nothing global.

    Global ordering would mean a single-threaded drain, which 02 §E refuses. The
    per-case counter is therefore the only sequence here, and the worker honours it by
    refusing to send a later row while an earlier one for the same case is unsent.
    """
    previous = session.execute(
        select(func.max(OutboxMessage.case_seq)).where(OutboxMessage.case_id == case_id)
    ).scalar_one_or_none()
    row = OutboxMessage(
        idempotency_key=idempotency_key,
        run_id=run_id,
        case_id=case_id,
        decision_seq=decision_seq,
        case_seq=int(previous or 0) + 1,
        sink_id=sink_id,
        schema_version=schema_version,
        payload=payload,
        status="pending",
        attempts=0,
        max_attempts=5,
        next_attempt_at=datetime.now(UTC),
        attempt_log=[],
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        # ``uq_outbox_idempotency`` or ``uq_outbox_case_seq`` refused the row: the delivery
        # promise already exists. That is a conflict about a fact, not a server fault, and it
        # has to reach the caller as one — the whole atomicity claim depends on the decision
        # row and the audit row being rolled back alongside it, which the router's rollback
        # only happens if the exception propagates as a refusal the handler understands.
        raise Conflict(
            f"a delivery promise for this decision already exists (idempotency key "
            f"{idempotency_key!r}, case_seq {int(previous or 0) + 1}): "
            f"{str(exc.orig).splitlines()[0][:200] if exc.orig else exc}. The decision and its "
            "audit row were rolled back with it, so the chain records nothing that was not sent."
        ) from exc
    return row


def _economics_row(session: Session, *, run_id: str, account_key: str) -> Economics:
    row = session.execute(
        select(Economics).where(
            Economics.run_id == run_id, Economics.account_key == account_key
        )
    ).scalars().first()
    if row is None:
        raise Unprocessable(
            f"account {account_key} in run {run_id} has no stored economics "
            "row. The four-eyes threshold is a money test, and a decision cannot be gated on a "
            "figure that was never computed — this is a pipeline gap, not a UI problem.",
            run_id=run_id,
        )
    return row


def _require_action_matches_reversal(write: DecisionWrite) -> None:
    if write.action == "reverse" and not write.reversal_of_decision_id:
        raise BadRequest(
            "action 'reverse' requires reversal_of_decision_id: a reversal is a new row "
            "referencing the row it reverses, and an unlinked one is an edit wearing a reference"
        )
    if write.action != "reverse" and write.reversal_of_decision_id:
        raise BadRequest(
            f"action {write.action!r} must not carry reversal_of_decision_id; only 'reverse' "
            "links back, so a linked dismissal would claim to undo something it does not"
        )


def _require_version(
    snapshot: CaseSnapshot, expected: int, session: Session, principal: Principal
) -> None:
    """The optimistic-concurrency check that produces the plan's 409 pair."""
    if snapshot.version != expected:
        current = session.execute(
            select(DecisionRow)
            .where(DecisionRow.case_id == snapshot.case_id)
            .order_by(DecisionRow.decision_seq.desc())
            .limit(1)
        ).scalars().first()
        raise VersionConflict(
            f"case {snapshot.case_id} is at version {snapshot.version}; this write expected "
            f"{expected}. {principal.display_name} did not overwrite the other reviewer's "
            "decision — the current one is in this response so both can be read side by side.",
            run_id=snapshot.run_id,
            expected_version=expected,
            current_version=snapshot.version,
            current=None
            if current is None
            else {
                "decision_id": str(current.decision_id),
                "decision_seq": int(current.decision_seq),
                "action": str(current.action),
                "reason": str(current.reason),
                "actor_id": str(current.actor_id),
                "occurred_at": current.occurred_at.isoformat(),
                "row_hash": str(current.row_hash),
                "four_eyes_state": str(current.four_eyes_state),
                "status": snapshot.status,
            },
        )


def _run_is_superseded(session: Session, run_id: str) -> bool:
    state, superseded = session.execute(
        select(Run.state, Run.superseded_by).where(Run.run_id == run_id)
    ).one_or_none() or (None, None)
    if state is None:
        raise NotFound(f"run {run_id} has no row, so a case cannot be pinned to it")
    return superseded is not None or str(state) == "superseded"


def _decision_chain_head(session: Session) -> tuple[int, str]:
    """Next ``chain_seq`` and the digest it links to, from the stored tip."""
    tip = _chain_tip(session)
    if tip is None:
        return 1, GENESIS_DIGEST
    return int(tip.seq) + 1, str(tip.row_hash)


def _chain_tip(session: Session) -> ChainRow | None:
    row = session.execute(
        select(DecisionRow).order_by(DecisionRow.chain_seq.desc()).limit(1)
    ).scalars().first()
    if row is None:
        return None
    return ChainRow(
        seq=int(row.chain_seq),
        occurred_at=row.occurred_at,
        actor_id=str(row.actor_id),
        subject=f"{DECISION_ACTION_SUBJECT_PREFIX}{row.case_id}",
        action=f"decision:{row.action}",
        payload=dict(row.payload or {}),
        prev_hash=str(row.prev_hash),
        row_hash=str(row.row_hash),
    )


def _decision_payload(
    *,
    snapshot: CaseSnapshot,
    write: DecisionWrite,
    decision_seq: int,
    chain_seq: int,
    score: Mapping[str, Any],
    economic_row: Economics,
    exposure_minor: int,
    currency: str,
    four_eyes_required: bool,
    decided_on_superseded: bool,
) -> dict[str, Any]:
    """What the digest covers, and therefore what can never be reinterpreted later.

    Money here is minor units and a currency code (DEV-005). The score block is copied
    in as the pipeline wrote it, because a consumer of the audit chain has to be able
    to see what the decision was made on without re-reading live tables.
    """
    return {
        "run_id": snapshot.run_id,
        "case_id": snapshot.case_id,
        "account_key": snapshot.account_key,
        "decision_seq": decision_seq,
        "chain_seq": chain_seq,
        "action": write.action,
        "reason": write.reason,
        "actor_id": write.principal.subject,
        "actor_roles": list(write.principal.roles),
        "actor_source": write.principal.source,
        "exposure_minor": exposure_minor,
        "currency": currency,
        "expected_value_minor": int(economic_row.expected_value_minor),
        "four_eyes_required": four_eyes_required,
        "decided_on_superseded_run": decided_on_superseded,
        "case_version_before": snapshot.version,
        "trace_id": write.trace_id,
        "model_version": str(score["model_version"]),
        "band": str(score["band"]),
        "fused_score": float(score["fused_score"]),
        "scorecard_points": int(score["scorecard_points"]),
        "config_hash": str(score.get("model_version")),
        "evidence_refs": [],
        "txn_ids": [],
    }


def _append_audit(
    audit_sink: AuditSink,
    *,
    snapshot: CaseSnapshot,
    write: DecisionWrite,
    decision: DecisionRow,
    pending: PendingChainRow,
    exposure_minor: int,
    currency: str,
) -> None:
    """The system-side chain row for the same fact, in the same transaction.

    The decision chain is what a reviewer signed; this one is what the system
    observed. Keeping them separate is what lets ``make verify-audit`` report a broken
    human attestation and a broken machine record as two different findings.
    """
    row = append_row(
        prev=audit_sink.tip(),
        occurred_at=decision.occurred_at,
        actor_id=write.principal.subject,
        subject=snapshot.case_id,
        action=f"decision_recorded:{write.action}",
        payload={
            "run_id": snapshot.run_id,
            "case_id": snapshot.case_id,
            "decision_id": str(decision.decision_id),
            "decision_seq": int(decision.decision_seq),
            "decision_row_hash": str(decision.row_hash),
            "exposure_minor": exposure_minor,
            "currency": currency,
            "four_eyes_required": bool(decision.four_eyes_required),
            "four_eyes_state": str(decision.four_eyes_state),
            "trace_id": write.trace_id,
            "chain_seq": int(pending.seq),
        },
    )
    _append_audit_sink(audit_sink, row)


def _append_audit_sink(audit_sink: AuditSink, row: PendingChainRow) -> None:
    try:
        audit_sink.append(row)
    except AuditAppendError as exc:
        raise Conflict(
            f"{exc}. Another writer moved the audit tip inside this transaction; the decision "
            "was not recorded and nothing was queued."
        ) from exc


def _new_decision_id() -> str:
    from ulid import ULID

    return f"dec_{ULID()!s}"


def _write_result(
    *, decision: DecisionRow, case: Case, outbox_queued: bool, audit_seq: int
) -> dict[str, Any]:
    return {
        "case_id": str(decision.case_id),
        "decision_id": str(decision.decision_id),
        "decision_seq": int(decision.decision_seq),
        "chain_seq": int(decision.chain_seq),
        "row_hash": str(decision.row_hash),
        "four_eyes_required": bool(decision.four_eyes_required),
        "four_eyes_state": str(decision.four_eyes_state),
        "outbox_queued": outbox_queued,
        "case_version": int(case.version),
        "decided_on_superseded_run": bool(decision.decided_on_superseded_run),
        "audit_seq": audit_seq,
        "occurred_at": decision.occurred_at,
    }


def decision_rows_for(session: Session, case_id: str) -> list[DecisionRow]:
    """The append-only history, oldest first, hashes included.

    Oldest-first because the timeline is read as a narrative and a reversal has to
    appear after the row it reverses; the API does not reorder a chain to flatter a
    page.
    """
    rows = session.execute(
        select(DecisionRow)
        .where(DecisionRow.case_id == case_id)
        .order_by(DecisionRow.decision_seq.asc())
    ).scalars().all()
    return list(rows)


def outbox_rows_for(session: Session, case_id: str) -> list[OutboxMessage]:
    return list(
        session.execute(
            select(OutboxMessage)
            .where(OutboxMessage.case_id == case_id)
            .order_by(OutboxMessage.case_seq.asc())
        ).scalars()
    )


def decision_chain_rows(session: Session) -> list[ChainRow]:
    """Every decision row, rebuilt as chain links, oldest sequence first.

    The reconstruction uses the same subject and action spellings the write path used
    (:data:`DECISION_ACTION_SUBJECT_PREFIX`, ``decision:<action>``) and the payload as
    stored, because a verifier that re-derived either would compute a digest over
    something other than what was signed and report a clean chain as broken — or,
    worse, a broken one as clean.
    """
    rows = session.execute(
        select(DecisionRow).order_by(DecisionRow.chain_seq.asc())
    ).scalars().all()
    return [
        ChainRow(
            seq=int(row.chain_seq),
            occurred_at=row.occurred_at,
            actor_id=str(row.actor_id),
            subject=f"{DECISION_ACTION_SUBJECT_PREFIX}{row.case_id}",
            action=f"decision:{row.action}",
            payload=dict(row.payload or {}),
            prev_hash=str(row.prev_hash),
            row_hash=str(row.row_hash),
        )
        for row in rows
    ]


def execute_erasure(
    session: Session,
    audit_sink: AuditSink,
    *,
    account_key: str,
    reason: str,
    principal: Principal,
    salt_version: str | None = None,
) -> dict[str, Any]:
    """Destroy the pseudonym mapping; keep every digest.

    The mapping table is the only place the salt exists, so deleting its rows makes
    every historical ``account_key`` reference unrecoverable while leaving the chain
    byte-for-byte intact — which is the only way "immutable audit" and "right to
    erasure" can both be absolute (02 §F, 03 §L). The audit row recording the erasure
    carries no identifying field, checked by
    :func:`oxbow.audit.erasure.assert_erasure_payload_is_safe` before it is written.
    """

    from oxbow.adapters.warehouse.models import ErasureRequest, PseudonymMap
    from oxbow.audit.erasure import build_erasure_audit_row

    existing = session.execute(
        select(PseudonymMap).where(PseudonymMap.account_key == account_key)
    ).scalars().all()
    version = salt_version or (str(existing[0].salt_version) if existing else "unknown")
    tip = audit_sink.tip()
    deleted = 0
    for row in existing:
        session.delete(row)
    deleted = len(existing)
    session.flush()

    pending = build_erasure_audit_row(
        account_key=account_key,
        tip=None if tip is None else PendingChainRow(
            seq=tip.seq,
            occurred_at=tip.occurred_at,
            actor_id=tip.actor_id,
            subject=tip.subject,
            action=tip.action,
            payload=tip.payload,
            prev_hash=tip.prev_hash,
            row_hash=tip.row_hash,
        ),
        reason=reason,
        actor_id=principal.subject,
        salt_version=version,
        mapping_rows_deleted=deleted,
    )
    _append_audit_sink(audit_sink, pending)
    import hashlib

    session.add(
        ErasureRequest(
            subject_ref_hash=hashlib.sha256(
                f"erasure|{account_key}|{principal.subject}".encode()
            ).hexdigest(),
            account_key=account_key,
            requested_by=principal.subject,
            mapping_rows_deleted=deleted,
            audit_seq=pending.seq,
            executed_at=pending.occurred_at,
        )
    )
    session.flush()
    return {
        "account_key": account_key,
        "mapping_rows_deleted": deleted,
        "audit_seq": pending.seq,
        "row_hash": pending.row_hash,
        "executed_at": pending.occurred_at,
        "chain_intact": True,
        "subject_recoverable": deleted == 0,
        "note": (
            "the salt mapping is gone; every stored account_key reference is now "
            "unrecoverable and every digest still verifies"
        ),
    }


def money_of(row: Economics, field: str, decimals: int = 2) -> dict[str, Any]:
    """One money column as minor units plus its currency, from the same row."""
    return money(getattr(row, field), str(row.currency), decimals=decimals)


__all__ = [
    "DECISION_ACTION_SUBJECT_PREFIX",
    "OUTBOX_SCHEMA_VERSION",
    "OUTBOX_SINK_CASE",
    "OUTBOX_SINK_NOTIFY",
    "FourEyesError",
    "build_case_bundle",
    "bundle_from_payload",
    "confirm_decision",
    "decision_chain_rows",
    "decision_rows_for",
    "execute_erasure",
    "money_of",
    "notification_payload",
    "open_case",
    "open_case_from_id",
    "outbox_rows_for",
    "queue_decision_deliveries",
    "record_decision",
    "sink_needs_notification",
]
