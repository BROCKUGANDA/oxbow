"""Postgres audit sink: the concurrent path for the hash chain (02 §F).

The file sink is single-writer; this one has to survive two analysts and a worker
appending at the same instant. The mechanism is the unique ``chain_seq`` column plus a
savepoint: the loser of the race gets an :class:`~oxbow.ports.audit.AuditAppendError`
naming the sequence that collided, and the caller retries the transaction. It does not
get a silently re-ordered audit trail, which is what a blind retry-with-next-seq would
produce.

The rows this writes are frozen by ``trg_audit_append_only`` from migration 0002: the
database itself refuses UPDATE and DELETE. A sink whose immutability depended on this
class never calling ``update`` would be an immutability claim with a hole in it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ulid import ULID

from oxbow.adapters.warehouse.models import AuditEvent
from oxbow.audit.chain import ChainRow, ChainVerification, PendingChainRow, verify_chain
from oxbow.audit.serialise import AUDIT_CHAIN_LOCK_KEY, chain_append_lock
from oxbow.ports.audit import AuditAppendError


class PostgresAuditSink:
    """The ``audit_event`` chain, written through the caller's session."""

    def __init__(self, session: Session, *, sink_id: str = "postgres") -> None:
        self._session = session
        self._sink_id = sink_id

    @property
    def sink_id(self) -> str:
        return self._sink_id

    def tip(self) -> ChainRow | None:
        row = self._session.scalars(
            select(AuditEvent).order_by(AuditEvent.chain_seq.desc()).limit(1)
        ).first()
        return _to_chain_row(row) if row is not None else None

    def append(self, row: PendingChainRow) -> ChainRow:
        """Insert one link, or refuse loudly when the tip has moved.

        The caller reads the tip and builds its link from it, so an unlocked read means two
        concurrent appends compute the same sequence and one is refused. Take the chain lock
        first; the seq check and the unique constraint stay, because they are what makes a
        genuine ordering bug visible rather than silently re-ordered. Callers that write both
        chains lock in the same order this project does — decision chain, then audit — which
        is what keeps the pair deadlock-free.
        """
        chain_append_lock(self._session, key=AUDIT_CHAIN_LOCK_KEY)
        statement = select(AuditEvent.chain_seq).order_by(AuditEvent.chain_seq.desc()).limit(1)
        current_seq = self._session.execute(statement).scalar_one_or_none()
        expected_seq = 1 if current_seq is None else int(current_seq) + 1
        if row.seq != expected_seq:
            raise AuditAppendError(
                f"audit: caller built seq {row.seq} but the stored tip is seq {current_seq}. "
                "Read the tip and the previous digest inside the same transaction."
            )
        event = AuditEvent(
            audit_id=str(ULID()),
            chain_seq=row.seq,
            occurred_at=row.occurred_at,
            actor_id=row.actor_id,
            subject=row.subject,
            action=row.action,
            run_id=str(row.payload.get("run_id")) if row.payload.get("run_id") else None,
            trace_id=str(row.payload.get("trace_id")) if row.payload.get("trace_id") else None,
            payload=dict(row.payload),
            prev_hash=row.prev_hash,
            row_hash=row.row_hash,
        )
        try:
            with self._session.begin_nested():
                self._session.add(event)
                self._session.flush()
        except IntegrityError as exc:
            raise AuditAppendError(
                f"audit: seq {row.seq} already exists -- a concurrent append won the link. "
                f"Retry the transaction. ({exc.orig})"
            ) from exc
        return _to_chain_row(event)

    def load(self, *, from_seq: int = 1) -> Sequence[ChainRow]:
        rows = self._session.scalars(
            select(AuditEvent)
            .where(AuditEvent.chain_seq >= from_seq)
            .order_by(AuditEvent.chain_seq)
        ).all()
        return [_to_chain_row(row) for row in rows]

    def verify(self) -> ChainVerification:
        return verify_chain(self.load())


def _to_chain_row(event: AuditEvent) -> ChainRow:
    payload: dict[str, Any] = dict(event.payload or {})
    return ChainRow(
        seq=int(event.chain_seq),
        occurred_at=event.occurred_at,
        actor_id=event.actor_id,
        subject=event.subject,
        action=event.action,
        payload=payload,
        prev_hash=event.prev_hash,
        row_hash=event.row_hash,
    )


__all__ = ["PostgresAuditSink"]
