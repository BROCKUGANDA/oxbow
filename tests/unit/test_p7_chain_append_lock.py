"""The chain append lock: taken before the tip is read, and a no-op off Postgres.

C7 in the audit: the decision chain and the audit chain both read their tip with an
unlocked ``SELECT ... ORDER BY chain_seq DESC LIMIT 1``, computed ``tip + 1``, and inserted.
Two writers on *unrelated* cases therefore both built the same link and one was refused with
a 409 blaming a collision the server had caused. Plan §13's "one success and one 409" is the
four-eyes rule for two analysts on the SAME case — enforced by the case row lock and
``expected_version`` — and it was being paid for by every writer in the database.

These tests pin the wiring deterministically: the lock must be issued with the right key
before the tip read, on the backend that has the primitive, and must not be issued where it
does not exist. The end-to-end claim — two concurrent cross-case decisions at the production
isolation level both land, with contiguous ``chain_seq`` — is NOT asserted here; it needs a
second live session blocked on the advisory lock while the first commits, and
``test_p7_api.py::test_concurrent_append_gives_one_success_and_one_409`` deliberately forces
REPEATABLE READ to exercise the unique-constraint backstop instead. That gap is stated here,
as a named limitation of this file, rather than papered over by a test that sleeps.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from oxbow.audit.serialise import (  # noqa: E402
    AUDIT_CHAIN_LOCK_KEY,
    DECISION_CHAIN_LOCK_KEY,
    chain_append_lock,
)


class _RecordingResult:
    def scalar_one_or_none(self) -> None:
        return None


class FakeSession:
    """Records what was executed, in order, and reports whatever dialect is asked for."""

    def __init__(self, *, dialect: str = "postgresql") -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any]] = []
        self._dialect = dialect

    def get_bind(self) -> Any:
        session = self

        class _Bind:
            dialect = type("D", (), {"name": session._dialect})()

        return _Bind()

    def execute(self, statement: Any, params: Any = None) -> _RecordingResult:
        self.statements.append(str(statement))
        self.params.append(dict(params or {}))
        return _RecordingResult()


def test_the_lock_is_taken_on_postgres_with_the_key_it_was_given() -> None:
    session = FakeSession()
    assert chain_append_lock(session, key=DECISION_CHAIN_LOCK_KEY) is True
    assert len(session.statements) == 1
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert session.params[0] == {"key": DECISION_CHAIN_LOCK_KEY}


def test_the_lock_is_a_no_op_on_a_backend_without_advisory_locks() -> None:
    """The null-file warehouse has no server to ask. It must not raise, and it must say so.

    Returning False matters: a caller that read "no lock" as "nobody can collide" would drop
    the unique-constraint handling that is the actual backstop there.
    """
    for dialect in ("sqlite", "duckdb"):
        session = FakeSession(dialect=dialect)
        assert chain_append_lock(session, key=AUDIT_CHAIN_LOCK_KEY) is False
        assert session.statements == [], f"{dialect} was sent {session.statements}"


def test_the_two_chains_do_not_share_a_lock_key() -> None:
    """One key for both chains would serialise unrelated writes against each other, and a
    writer that took them in opposite orders would deadlock."""
    assert DECISION_CHAIN_LOCK_KEY != AUDIT_CHAIN_LOCK_KEY


def test_the_decision_write_locks_the_chain_before_reading_the_tip() -> None:
    """The ordering that makes the fix real.

    Locking after the read is the same race with an extra statement. Asserted against the
    source of the write path because the two calls sit in one function and a reorder is
    invisible to every other test in this file.
    """
    source = (REPO_ROOT / "apps" / "api" / "decisions.py").read_text(encoding="utf-8")
    lock_at = source.find("chain_append_lock(session, key=DECISION_CHAIN_LOCK_KEY)")
    tip_at = source.find("chain_seq, prev_hash = _decision_chain_head(session)")
    assert lock_at != -1, "the decision chain no longer takes the append lock"
    assert tip_at != -1, "the decision write path no longer reads the chain tip"
    assert lock_at < tip_at, "the append lock is taken AFTER the tip read, which is the race"


def test_the_audit_sink_locks_before_it_reads_its_tip() -> None:
    sink = (
        REPO_ROOT / "packages" / "pipeline" / "oxbow" / "adapters" / "audit" / "postgres.py"
    ).read_text(encoding="utf-8")
    lock_at = sink.find("chain_append_lock(self._session, key=AUDIT_CHAIN_LOCK_KEY)")
    tip_at = sink.find("statement = select(AuditEvent.chain_seq)")
    assert lock_at != -1, "the audit chain no longer takes the append lock"
    assert tip_at != -1
    assert lock_at < tip_at, "the audit sink locks after reading its tip"


def test_a_caller_that_skips_the_lock_still_gets_the_constraint_backstop() -> None:
    """The lock removes the ordinary collision; the unique constraint is what makes a
    collision impossible to *silently* win. Both must stay, so assert the refusal is still
    there rather than deleted along with the fix."""
    decisions = (REPO_ROOT / "apps" / "api" / "decisions.py").read_text(encoding="utf-8")
    assert "uq_decision_chain_seq" in decisions, (
        "the chain-sequence constraint stopped being named in the refusal path; the advisory "
        "lock is not a substitute for it under snapshot isolation"
    )
    sink = (
        REPO_ROOT / "packages" / "pipeline" / "oxbow" / "adapters" / "audit" / "postgres.py"
    ).read_text(encoding="utf-8")
    assert "AuditAppendError" in sink, "the audit sink stopped refusing a moved tip"


@pytest.mark.parametrize("key", [DECISION_CHAIN_LOCK_KEY, AUDIT_CHAIN_LOCK_KEY])
def test_the_lock_key_fits_a_bigint(key: int) -> None:
    """pg_advisory_xact_lock takes a bigint. A key that overflows is accepted by Python and
    rejected by the server at runtime, in production, under load."""
    assert 0 < key < 2**63 - 1
