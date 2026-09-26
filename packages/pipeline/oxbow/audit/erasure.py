"""Right to erasure against an immutable audit chain (03 §L, plan §13).

The two obligations genuinely collide: an audit trail you can edit is not an
audit trail, and a record you cannot delete cannot be erased. The resolution is
not a compromise on either side — it is a change of what is stored.

The chain stores salted hashes and decision metadata only. The one-way mapping
from a real-world subject to an ``account_key`` lives in a separate table, and it
is the only place the salt exists. So erasure destroys the *mapping*, which makes
every downstream reference unrecoverable, and leaves every digest in place. After
erasure the audit still proves "someone decided X about subject ACC-9F2C on this
date, and the decision was signed" while being unable to say who ACC-9F2C was —
even to us, because we no longer hold the salt.

This module is the policy: what is destroyed, what is retained, and the audit row
that proves the erasure happened. Execution against Postgres lives in
``oxbow.adapters.audit.postgres``, which is where the transaction boundary is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from oxbow.audit.chain import PendingChainRow, append_row

# The action name written to both chains. Fixed here so the audit query, the
# packet renderer and the erasure test cannot drift into three spellings.
ERASURE_ACTION: Final = "subject_erased"

# Payload keys that must NOT appear in an erasure audit row. The erasure event
# itself is permanent, so anything identifying inside it would survive the very
# deletion it records.
FORBIDDEN_ERASURE_KEYS: Final = ("raw_account_id", "account_id", "salt", "email", "phone")


@dataclass(frozen=True, slots=True)
class PseudonymMapping:
    """One row of the mapping table: the only place the salt lives.

    ``salt`` is read from the environment-issued per-run salt at ingest and never
    re-derived, which is why destroying this row is what erasure actually means.
    """

    account_key: str
    subject_ref: str
    salt_version: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ErasureResult:
    """What an erasure destroyed, what it retained, and the proof it ran.

    ``chain_intact`` is asserted by ``test_erasure_preserves_chain``: the walk
    over the retained rows must still verify after the mapping is gone, because
    if it did not, the "immutable" claim would be conditional on nobody ever
    exercising a data-protection right.
    """

    account_key: str
    mapping_rows_deleted: int
    audit_rows_retained: int
    decision_rows_retained: int
    chain_intact: bool
    erased_at: datetime

    @property
    def subject_recoverable(self) -> bool:
        """Always ``False`` when a mapping was deleted — the whole point."""
        return self.mapping_rows_deleted == 0


def build_erasure_audit_row(
    *,
    account_key: str,
    tip: PendingChainRow | None,
    reason: str,
    actor_id: str,
    salt_version: str,
    mapping_rows_deleted: int,
    occurred_at: datetime | None = None,
) -> PendingChainRow:
    """The permanent record that a deletion happened, without the deleted data.

    The row says *that* an account key was erased, by whom, under which salt
    version and how many mappings went. It says nothing that would let anyone
    rebuild the mapping, which is why :func:`assert_erasure_payload_is_safe` runs
    before it is written.
    """
    payload: Mapping[str, Any] = {
        "account_key": account_key,
        "reason": reason,
        "salt_version": salt_version,
        "mapping_rows_deleted": mapping_rows_deleted,
    }
    assert_erasure_payload_is_safe(payload)
    return append_row(
        prev=tip,
        occurred_at=occurred_at or datetime.now(UTC),
        actor_id=actor_id,
        subject=account_key,
        action=ERASURE_ACTION,
        payload=payload,
    )


def assert_erasure_payload_is_safe(payload: Mapping[str, Any]) -> None:
    """Refuse to persist an identifying field inside an erasure record.

    A boundary check, not a style rule: an erasure event containing the raw
    identifier is a backup copy of exactly what was deleted, and it can never be
    removed without breaking the chain. Fails loud at write time instead of
    quietly in a subject-access request two years later.
    """
    offending = sorted(key for key in payload if key in FORBIDDEN_ERASURE_KEYS)
    if offending:
        raise ValueError(
            f"erasure audit payload carries identifying fields {offending}; the erasure "
            "record is permanent, so nothing that identifies the subject may be written into it"
        )


def select_mappings_for_subject(
    mappings: Sequence[PseudonymMapping], subject_ref: str
) -> tuple[PseudonymMapping, ...]:
    """Every mapping row for one subject, across every salt version.

    A subject can appear under several runs with different salts and therefore
    different account keys. Erasure that only deletes the newest one leaves the
    older keys recoverable, which is not erasure.
    """
    return tuple(mapping for mapping in mappings if mapping.subject_ref == subject_ref)


__all__ = [
    "ERASURE_ACTION",
    "FORBIDDEN_ERASURE_KEYS",
    "ErasureResult",
    "PseudonymMapping",
    "assert_erasure_payload_is_safe",
    "build_erasure_audit_row",
    "select_mappings_for_subject",
]
