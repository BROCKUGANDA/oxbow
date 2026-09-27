"""Serialising appends to an append-only hash chain.

Two chains in this project are append-only and globally ordered: the decision chain
(``apps/api/decisions.py``) and the audit-event chain
(``oxbow.adapters.audit.postgres.PostgresAuditSink``). Both used to read the tip with an
unlocked ``SELECT ... ORDER BY chain_seq DESC LIMIT 1``, compute ``tip + 1``, and insert.
Under two concurrent writers that is a guaranteed collision: both read the same tip, both
build the same link, the unique ``chain_seq`` refuses one, and the loser was told another
writer appended "at the same instant" and to decide again.

That refusal was being justified by plan §13's four-eyes rule — "one success and one 409" —
but that rule is about **two analysts deciding the same case**, and it is enforced elsewhere
(the case row is locked with ``SELECT ... FOR UPDATE`` and the write carries an
``expected_version``). The chain is global, so before this file existed two decisions on
*unrelated* cases could not be recorded concurrently either. A per-case integrity rule was
being enforced as a per-database throughput limit, and every writer paid for it.

Why an advisory lock and not ``SELECT ... FOR UPDATE`` on the tip row:

* the first link has no row to lock — ``FOR UPDATE`` over an empty result blocks nothing, so
  the very first two appends are the ones that still collide;
* locking the tip row serialises only writers that read *that* row, and a reader that
  happens to see an older tip (another transaction's insert not yet visible) is not blocked
  by it at all;
* ``pg_advisory_xact_lock`` is transaction-scoped, so it is released on commit *and* on
  rollback with no cleanup path to forget, and it can be taken before there is anything to
  point at.

Two distinct keys, one per chain, so the chains do not contend with each other. A writer
that appends to both takes them in the order declared here — decision chain first, then
audit — and every call site follows that order, which is what makes the pair deadlock-free
rather than merely unlikely to deadlock.

On a non-Postgres backend (the read-only null-file warehouse used for browser checks and
offline demos) there is no server to ask, and that warehouse is single-process and
read-only, so the lock is a no-op and the unique constraint remains the backstop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Stable identifiers, not hashes of names that might be renamed. If either is ever changed,
# every process holding the old key must be restarted, because two keys mean two locks mean
# no mutual exclusion at all.
DECISION_CHAIN_LOCK_KEY: int = 0x0BC0_0001
AUDIT_CHAIN_LOCK_KEY: int = 0x0BC0_0002


def chain_append_lock(session: Session, *, key: int) -> bool:
    """Take the transaction-scoped append lock for one chain.

    Returns True when a real lock was taken, False when the backend has no advisory-lock
    primitive. Callers must not treat False as "unlocked, so skip the write": the unique
    ``chain_seq`` constraint still refuses a collision, and the caller still has to handle
    it. What the lock changes is that a collision stops being the normal outcome of two
    people using the tool at once.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(key)})
    return True
