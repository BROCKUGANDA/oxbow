"""Decision writes: the four-eyes gate, the 409, and the outbox ledger.

The gate for plan §13 is *"two concurrent decision writes produce one success and one
409"*, and the 409 this router returns is not a generic conflict: it carries the
stored version, the current decision's action, reason, actor, timestamp and row hash,
so the UI can render the merge view the plan asks for rather than telling someone
their work vanished.

The four-eyes rule is enforced **before the outbox row exists**, not before the audit
row. A decision above ``four_eyes.threshold_exposure_minor`` is recorded immediately —
it happened, the chain has to say so, and a chain with a gap where a pending decision
was is worse than a pending decision — but no delivery is promised until a second,
*different* reviewer confirms it. The write result reports that as
``outbox_queued: false`` with ``four_eyes_state: "pending"``, so the client never has
to infer a state from a missing field.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, Path, Query

from api.deps import Container, analyst_or_higher, get_container, reviewer_or_higher
from api.problems import COMMON_ERROR_STATUSES, NotFound, problem_responses
from api.routers.common import build_meta
from api.schemas.case import (
    DecisionCreate,
    DecisionWriteResult,
    FourEyesConfirm,
    OutboxRow,
)
from api.schemas.common import Envelope, envelope
from api.security import Principal

router = APIRouter(tags=["decisions"])

FOUR_EYES_ACTIONS: Final = ("escalate", "dismiss", "review", "reverse")


@router.post(
    "/api/cases/{case_id}/decisions",
    response_model=Envelope[DecisionWriteResult],
    summary="Record a decision (needs a written reason; 409 on a stale version)",
    description=(
        "Appends a decision row, its audit-chain row and — unless the exposure is above "
        "the four-eyes threshold — its outbox row, in one Postgres transaction. The "
        "reason is required and survives verbatim into the chain and the packet."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def create_decision(
    body: DecisionCreate,
    case_id: str = Path(min_length=26, max_length=26),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    from api.decisions import DecisionWrite, open_case_from_id, record_decision

    session = container.new_session()
    try:
        case = open_case_from_id(session, case_id)
        result = record_decision(
            session,
            container.read_model,
            container.audit_sink(session),
            container.economics,
            case=case,
            write=DecisionWrite(
                action=body.action,
                reason=body.reason,
                expected_version=body.expected_version,
                reversal_of_decision_id=body.reversal_of_decision_id,
                principal=principal,
                trace_id=_trace(),
            ),
        )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    run_id = _run_id_for(container, case_id)
    return envelope(
        DecisionWriteResult.model_validate(result),
        **build_meta(container, run_id=run_id).model_dump(),
    )


@router.post(
    "/api/decisions/{decision_id}/confirm",
    response_model=Envelope[DecisionWriteResult],
    summary="Second reviewer confirms a four-eyes decision, which queues the outbox row",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def confirm(
    body: FourEyesConfirm,
    decision_id: str = Path(min_length=1, max_length=64),
    container: Container = Depends(get_container),
    principal: Principal = Depends(reviewer_or_higher),
) -> dict[str, Any]:
    from api.decisions import confirm_decision

    session = container.new_session()
    try:
        result = confirm_decision(
            session,
            container.read_model,
            container.audit_sink(session),
            container.economics,
            decision_id=decision_id,
            principal=principal,
            expected_version=body.expected_version,
            confirmation_note=body.confirmation_note,
            trace_id=_trace(),
        )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    run_id = _run_id_for(container, str(result["case_id"]))
    return envelope(
        DecisionWriteResult.model_validate(result),
        **build_meta(container, run_id=run_id).model_dump(),
    )


@router.get(
    "/api/cases/{case_id}/decisions",
    response_model=Envelope[list[dict[str, Any]]],
    summary="The append-only decision history for one case, oldest first",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def case_decisions(
    case_id: str = Path(min_length=26, max_length=26),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    from api.readmodel import money

    rows, _ = container.read_model.source.select(
        "decision", where={"case_id": case_id}, order="decision_seq", allow_missing=True
    )
    if not rows:
        case_rows, _ = container.read_model.source.select(
            "review_case", where={"case_id": case_id}, limit=1
        )
        if not case_rows:
            raise NotFound(f"no case {case_id!r}")
    # The exponent, not the base: config declares minor_units_per_major (100) and
    # both this server and apps/web/src/lib/format/money.ts raise ten to whatever
    # arrives in a `decimals` field. Inherited from the read model, which converts
    # once and refuses a base that is not an exact power of ten.
    decimals = container.read_model.money_decimals

    body = [
        {
            "decision_id": row["decision_id"],
            "decision_seq": row["decision_seq"],
            "chain_seq": row["chain_seq"],
            "action": row["action"],
            "reason": row["reason"],
            "actor_id": row["actor_id"],
            "actor_roles": row["actor_roles"],
            "occurred_at": row["occurred_at"],
            "exposure": money(int(row["exposure_minor"]), str(row["currency"]), decimals=decimals),
            "four_eyes_required": bool(row["four_eyes_required"]),
            "four_eyes_state": row["four_eyes_state"],
            "confirmed_by": row.get("confirmed_by"),
            "confirmed_at": row.get("confirmed_at"),
            "reversal_of_decision_id": row.get("reversal_of_decision_id"),
            "decided_on_superseded_run": bool(row["decided_on_superseded_run"]),
            "prev_hash": row["prev_hash"],
            "row_hash": row["row_hash"],
        }
        for row in rows
    ]
    run_id = None if not rows else str(rows[0]["run_id"])
    return envelope(body, **build_meta(container, run_id=run_id).model_dump())


@router.get(
    "/api/outbox",
    response_model=Envelope[dict[str, Any]],
    summary="The delivery ledger: pending, retrying, sent and dead-lettered rows",
    description=(
        "The database's own answer to 'has this been sent?' (02 §E). Dead rows stay "
        "here rather than being deleted: a delivery that never happened is a fact a "
        "later reader needs."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def outbox_ledger(
    status: str | None = Query(default=None, pattern="^(pending|in_flight|sent|dead)$"),
    case_id: str | None = Query(default=None, min_length=26, max_length=26),
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    from api.outbox import ledger, queue_depth

    session = container.new_session()
    try:
        rows = ledger(session, status=status, case_id=case_id, limit=limit)
        depth = queue_depth(session)
        body = [OutboxRow.model_validate(_outbox_dict(row)) for row in rows]
    finally:
        session.close()
    return envelope(
        {"rows": [row.model_dump() for row in body], "depth": depth},
        **build_meta(container).model_dump(),
    )


@router.get(
    "/api/outbox/pending",
    response_model=Envelope[dict[str, Any]],
    summary="How many deliveries are waiting, and when the next one is due",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def outbox_pending(
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    from api.outbox import queue_depth

    session = container.new_session()
    try:
        depth = queue_depth(session)
    finally:
        session.close()
    return envelope(
        {
            **depth,
            "note": "at-least-once delivery with idempotent consumers; the "
            "idempotency key is sha256(run_id|case_id|decision_seq)",
        },
        **build_meta(container).model_dump(),
    )


def _outbox_dict(row: Any) -> dict[str, Any]:
    return {
        "outbox_id": int(row.outbox_id),
        "idempotency_key": str(row.idempotency_key),
        "run_id": str(row.run_id),
        "case_id": str(row.case_id),
        "decision_seq": int(row.decision_seq),
        "case_seq": int(row.case_seq),
        "sink_id": str(row.sink_id),
        "status": str(row.status),
        "attempts": int(row.attempts),
        "max_attempts": int(row.max_attempts),
        "schema_version": str(row.schema_version),
        "next_attempt_at": row.next_attempt_at,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "sent_at": row.sent_at,
        "dead_at": row.dead_at,
    }


def _run_id_for(container: Container, case_id: str) -> str | None:
    rows, _ = container.read_model.source.select(
        "review_case", where={"case_id": case_id}, columns=["run_id"], limit=1
    )
    return None if not rows else str(rows[0]["run_id"])


def _run_id_for_decision(container: Container, decision_id: str) -> str | None:
    rows, _ = container.read_model.source.select(
        "decision", where={"decision_id": decision_id}, columns=["run_id"], limit=1
    )
    return None if not rows else str(rows[0]["run_id"])


def _trace() -> str | None:
    from api.observability import trace_id_var

    return trace_id_var.get()


__all__ = ["FOUR_EYES_ACTIONS", "router"]
