"""The hash chain itself: one row in, one digest out, one walk to verify.

Kept deliberately free of database and framework imports. The chain has to be
verifiable from a packet export, from a Postgres cursor and from a test fixture,
and it must give the same answer in all three. The moment the arithmetic lives
behind a session, the audit claim becomes "we trust our own ORM".

What a row's digest covers, and why each part is there:

    sha256( canonical_json_array([ "oxbow-audit-v1", seq, occurred_at, actor,
                                   subject, action, canonical(payload), prev_hash ]) )

* the material is a JSON **array**, not a delimiter-joined string. A ``"|"`` join is
  ambiguous whenever a field may itself contain ``"|"`` -- ``subject``, ``action`` and
  the payload's canonical JSON all can -- so two different field splits could produce
  byte-identical digest material and therefore the same digest. JSON element boundaries
  are quoted and escaped, so they cannot be shifted between fields (STATE.md §14).
* ``HASH_VERSION`` is the array's first element so the scheme a row was written under
  stays readable from its own material; a future v2 can be told apart from v1 rather
  than silently re-hashed.
* ``prev_hash`` is what makes it a chain rather than a list of signatures: an
  editor who rewrites row 41 cannot produce row 42 without also rewriting 42, and
  the break surfaces at a named sequence number.
* ``seq`` is inside the digest so rows cannot be reordered, and ``occurred_at``
  so a timestamp cannot be nudged.
* the payload is canonical JSON (sorted keys, no insignificant whitespace),
  because two encodings of the same mapping must not produce two digests.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final

# The digest that precedes sequence 1. A real constant, not ``None``: genesis has
# to be comparable, or the first row silently skips its link check and the chain
# starts with an unverified link.
GENESIS_HASH: Final = "0" * 64

HASH_VERSION: Final = "oxbow-audit-v1"


class AuditIntegrityError(RuntimeError):
    """A chain operation was asked to do something that would break integrity.

    Raised when appending with a predecessor that is not the current tip. 03 §A
    rule 1: fail loud at the boundary; a silently mis-linked audit row is worse
    than a crashed write, because it still looks like an audit trail.
    """


def _encode_nonjson(value: Any) -> str:
    """Fallback encoder for values JSON does not know (datetimes, bytes...).

    Refuses anything unrepresentable rather than calling ``str`` on an object
    whose repr includes a memory address: that would make the digest
    machine-dependent, which is the exact failure the chain exists to catch.
    """
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, bytes | bytearray):
        return hashlib.sha256(bytes(value)).hexdigest()
    raise TypeError(
        f"payload value of type {type(value).__name__} is not digestable; serialise it "
        "at the call site so the audit encoding is explicit"
    )


def canonical_json(value: Mapping[str, Any]) -> str:
    """Deterministic JSON for a mapping: sorted keys, no space, non-ASCII kept.

    ``ensure_ascii=False`` matters: with it on, the same payload written by a
    locale-varying client would hash differently across machines.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_encode_nonjson
    )


def _canonical_array(values: Sequence[Any]) -> str:
    """Canonical JSON for an ordered sequence -- the digest material's own encoding.

    Same rules as :func:`canonical_json` (no insignificant whitespace, non-ASCII kept,
    the strict fallback encoder), minus key sorting, which an array has no use for.
    Element boundaries here are quoted, so no field value can imitate one and shift the
    parse; that is precisely what the ``"|"`` join this replaced could not guarantee.
    """
    return json.dumps(values, separators=(",", ":"), ensure_ascii=False, default=_encode_nonjson)


def _timestamp(value: datetime) -> str:
    """UTC ISO-8601 with a trailing Z. Naive datetimes are refused, not assumed."""
    if value.tzinfo is None:
        raise ValueError(
            f"audit timestamps must be tz-aware, got naive {value}. A naive timestamp is "
            "an unlogged assumption about who wrote it and where."
        )
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ChainRow:
    """One link, plus the digest claimed for it when it was written.

    ``subject`` is a pseudonymous key (``ACC-...``) or a case id, never a raw
    account identifier: 03 §D puts the PII boundary at ingest, and the audit log
    is downstream of it.
    """

    seq: int
    occurred_at: datetime
    actor_id: str
    subject: str
    action: str
    payload: Mapping[str, Any]
    prev_hash: str
    row_hash: str


@dataclass(frozen=True, slots=True)
class PendingChainRow:
    """A row whose digest has been computed but which has no claimed hash yet.

    Returned by :func:`append_row` so the caller can put ``row_hash`` in the same
    INSERT as everything else. Keeping the two types separate is what stops a
    half-built row being mistaken for a stored one.
    """

    seq: int
    occurred_at: datetime
    actor_id: str
    subject: str
    action: str
    payload: Mapping[str, Any]
    prev_hash: str
    row_hash: str

    def stored(self) -> ChainRow:
        """Lift into the verified shape without changing any field."""
        return ChainRow(
            seq=self.seq,
            occurred_at=self.occurred_at,
            actor_id=self.actor_id,
            subject=self.subject,
            action=self.action,
            payload=self.payload,
            prev_hash=self.prev_hash,
            row_hash=self.row_hash,
        )


def compute_row_hash(row: PendingChainRow | ChainRow) -> str:
    """The v1 digest of one row, including its predecessor.

    The eight fields are digested as a canonical JSON **array** carrying
    ``HASH_VERSION`` first, so the material is unambiguous: a ``"|"`` in ``subject``,
    ``action`` or a payload string value cannot be mistaken for a field boundary, and
    two different field splits cannot hash identically.
    """
    material = _canonical_array(
        [
            HASH_VERSION,
            str(row.seq),
            _timestamp(row.occurred_at),
            row.actor_id,
            row.subject,
            row.action,
            canonical_json(row.payload),
            row.prev_hash,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append_row(
    *,
    prev: ChainRow | None,
    occurred_at: datetime,
    actor_id: str,
    subject: str,
    action: str,
    payload: Mapping[str, Any],
) -> PendingChainRow:
    """Produce the next row of a chain from the known tip.

    ``prev=None`` means the chain is empty and this row is sequence 1 linked to
    the genesis digest. Called inside the same transaction as the decision write,
    so a concurrent append that moved the tip shows up as a unique-violation on
    ``seq`` and the caller retries (03 §K: two concurrent writes, one success and
    one 409).
    """
    prev_hash = GENESIS_HASH if prev is None else prev.row_hash
    seq = 1 if prev is None else prev.seq + 1
    pending = PendingChainRow(
        seq=seq,
        occurred_at=occurred_at,
        actor_id=actor_id,
        subject=subject,
        action=action,
        payload=dict(payload),
        prev_hash=prev_hash,
        row_hash="",
    )
    return replace(pending, row_hash=compute_row_hash(pending))


@dataclass(frozen=True, slots=True)
class BrokenLink:
    """Where verification stopped and why, with the sequence number named."""

    seq: int
    reason: str
    expected: str
    actual: str


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The result of walking a chain. ``ok`` is only true when every link verified."""

    ok: bool
    rows_checked: int
    first_broken: BrokenLink | None
    checked_at: datetime
    head_hash: str


def verify_chain(rows: Sequence[ChainRow]) -> ChainVerification:
    """Walk an ordered chain and report the first broken link.

    Three distinct failures are separated because an operator has to tell them
    apart (03 §D): a digest mismatch is an edited row, a sequence gap is a deleted
    row, and a predecessor mismatch is a re-linked chain.
    """
    checked_at = datetime.now(UTC)
    if not rows:
        return ChainVerification(
            ok=True,
            rows_checked=0,
            first_broken=None,
            checked_at=checked_at,
            head_hash=GENESIS_HASH,
        )

    head = GENESIS_HASH
    for index, row in enumerate(rows):
        expected_seq = index + 1
        if row.seq != expected_seq:
            return ChainVerification(
                ok=False,
                rows_checked=index,
                first_broken=BrokenLink(
                    seq=row.seq,
                    reason="sequence gap: a row was deleted from the chain",
                    expected=f"seq {expected_seq}",
                    actual=f"seq {row.seq}",
                ),
                checked_at=checked_at,
                head_hash=head,
            )
        if row.prev_hash != head:
            return ChainVerification(
                ok=False,
                rows_checked=index,
                first_broken=BrokenLink(
                    seq=row.seq,
                    reason="predecessor mismatch: the chain was re-linked at this row",
                    expected=head,
                    actual=row.prev_hash,
                ),
                checked_at=checked_at,
                head_hash=head,
            )
        recomputed = compute_row_hash(row)
        if recomputed != row.row_hash:
            return ChainVerification(
                ok=False,
                rows_checked=index,
                first_broken=BrokenLink(
                    seq=row.seq,
                    reason="digest mismatch: this row was edited after it was written",
                    expected=recomputed,
                    actual=row.row_hash,
                ),
                checked_at=checked_at,
                head_hash=head,
            )
        head = recomputed

    return ChainVerification(
        ok=True, rows_checked=len(rows), first_broken=None, checked_at=checked_at, head_hash=head
    )


def render_verification(verification: ChainVerification, *, label: str) -> str:
    """The exact line ``make verify-audit`` prints.

    OK names the chain and the row count; a failure names the sequence number
    (plan §13: "prints OK or the first broken link naming the sequence number").
    One function so the script, the CLI and the test cannot disagree about what
    "verified" looks like on screen.
    """
    if verification.ok:
        return (
            f"OK   {label}: {verification.rows_checked} rows verified, head "
            f"{verification.head_hash}"
        )
    broken = verification.first_broken
    assert broken is not None  # verify_chain never returns ok=False without one
    return (
        f"FAIL {label}: first broken link at seq={broken.seq} -- {broken.reason}\n"
        f"     expected {broken.expected}\n"
        f"     actual   {broken.actual}"
    )


__all__ = [
    "GENESIS_HASH",
    "HASH_VERSION",
    "AuditIntegrityError",
    "BrokenLink",
    "ChainRow",
    "ChainVerification",
    "PendingChainRow",
    "append_row",
    "canonical_json",
    "compute_row_hash",
    "render_verification",
    "verify_chain",
]
