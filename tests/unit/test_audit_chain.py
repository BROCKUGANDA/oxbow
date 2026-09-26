"""Audit-chain digest tests: tamper evidence, and the delimiter collision it closed.

Two properties are pinned here, and both are arithmetic rather than opinion.

**(a) Tamper evidence.** A row edited after it was written, a row deleted, and a chain
re-linked at a row are three different failures, and each has to surface at a named
sequence number (plan §15, 03 §D). Deleting a row is *not* caught by the digest -- the
sequence counter catches it -- so it needs its own test rather than riding along on the
digest one.

**(b) The ``"|"`` join collision.** ``compute_row_hash`` used to concatenate its eight
fields with ``"|"``, but three of them can contain a pipe -- ``subject``, ``action``, and
any payload string -- so two rows with different field *splits* produced byte-identical
material and therefore the same digest. The material is now a canonical JSON array, whose
element boundaries are quoted and so cannot be imitated by a field value.

The collision is proven by reconstructing the retired join *inside* the test and showing
it collides on the same two rows that the current digest separates. Without that, a test
asserting only "these two hashes differ" would also pass against a digest that never had
the bug, and would document nothing about what changed or why.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final

import pytest

from oxbow.audit.chain import (
    GENESIS_HASH,
    HASH_VERSION,
    ChainRow,
    append_row,
    canonical_json,
    compute_row_hash,
    verify_chain,
)

T0: Final = datetime(2026, 9, 26, 10, 0, 0, tzinfo=UTC)
PREV: Final = "f" * 64


def _iso_z(value: datetime) -> str:
    """The timestamp encoding the digest covers: UTC ISO-8601 with a trailing Z."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dumps(value: Any) -> str:
    """JSON with the digest's own encoding settings: compact, non-ASCII preserved."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _unsigned(
    *,
    seq: int = 1,
    occurred_at: datetime = T0,
    actor_id: str = "U-ANALYST-1",
    subject: str = "CASE-4411",
    action: str = "freeze.account",
    payload: dict[str, Any] | None = None,
    prev_hash: str = PREV,
) -> ChainRow:
    """A complete row with no claimed digest yet -- for digesting by hand in a test."""
    return ChainRow(
        seq=seq,
        occurred_at=occurred_at,
        actor_id=actor_id,
        subject=subject,
        action=action,
        payload={"reason": "sanctions hit", "confidence": 0.91} if payload is None else payload,
        prev_hash=prev_hash,
        row_hash="",
    )


def _row(**fields: Any) -> ChainRow:
    """An unsigned row plus the digest the current code claims for it."""
    row = _unsigned(**fields)
    return replace(row, row_hash=compute_row_hash(row))


# ----------------------------------------------------------------- (b) the collision


def _legacy_join_hash(row: ChainRow) -> str:
    """The retired digest: the same eight fields joined with ``"|"``.

    Reconstructed rather than imported, because it deliberately no longer exists in the
    product code -- and the point of keeping it here is to show the ambiguity was real.
    """
    material = "|".join(
        (
            HASH_VERSION,
            str(row.seq),
            _iso_z(row.occurred_at),
            row.actor_id,
            row.subject,
            row.action,
            canonical_json(row.payload),
            row.prev_hash,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def test_two_field_splits_that_collided_under_the_join_now_hash_differently() -> None:
    """STATE.md §14: a pipe crossing the subject/action boundary used to be free."""
    # Same eight field values in the same order, split differently between two fields.
    left = _row(subject="CASE-1|freeze.account", action="x")
    right = _row(subject="CASE-1", action="freeze.account|x")

    assert (left.subject, left.action) != (right.subject, right.action)
    assert _legacy_join_hash(left) == _legacy_join_hash(right), (
        "the retired join is supposed to collide on this pair; if it no longer does, "
        "this test documents a bug that never existed and should be rewritten"
    )
    assert compute_row_hash(left) != compute_row_hash(right)
    # The two rows still share their legacy digest with themselves, i.e. the new digest
    # is the only thing that separates them -- not a side effect of a changed payload.
    assert canonical_json(left.payload) == canonical_json(right.payload)


def test_adversarial_delimiters_in_any_field_still_separate_the_digest() -> None:
    """Every field that can carry a pipe is exercised, not just the pair above."""
    cases = [
        {"subject": "a|b", "action": "c"},
        {"subject": "a", "action": "b|c"},
        {"subject": "a", "action": "b", "payload": {"k": "c"}},
        {"subject": 'a",', "action": '"b"', "payload": {"c": 1}},
        {"subject": "a", "action": "b|c", "payload": {"k": 'v",|x'}},
        {"subject": "a", "action": "b", "payload": {"k": "c"}, "prev_hash": "0" * 64},
    ]
    digests = {compute_row_hash(_unsigned(**case)) for case in cases}
    assert len(digests) == len(cases), "two distinct field splits produced one digest"


def test_a_resplit_forgery_carrying_the_stored_digest_is_now_rejected() -> None:
    """The security statement behind the collision, not just "the hashes differ".

    An attacker who keeps a stored ``row_hash`` and ``prev_hash`` intact but rewrites
    ``subject``/``action`` across the pipe boundary used to produce a row that verified
    cleanly, because the digest material was identical. Here the forged row deliberately
    inherits the genuine row's digest, and verification must still stop at it.
    """
    first = _row(seq=1, prev_hash=GENESIS_HASH)
    genuine = _row(seq=2, prev_hash=first.row_hash, subject="CASE-1|freeze.account", action="x")
    forged = replace(genuine, subject="CASE-1", action="freeze.account|x")

    assert forged.row_hash == genuine.row_hash  # the digest was not touched
    assert _legacy_join_hash(forged) == _legacy_join_hash(genuine)  # it was valid under v1
    result = verify_chain([first, forged])
    assert not result.ok
    broken = result.first_broken
    assert broken is not None
    assert broken.seq == 2
    assert "digest mismatch" in broken.reason


def test_hash_version_is_still_the_first_element_of_the_digest_material() -> None:
    """The version tag is retained, so a row's scheme stays identifiable from its digest.

    The material is rebuilt here independently, in the documented field order; if the tag
    were dropped or moved the two would disagree.
    """
    row = _row()
    material = _dumps(
        [
            HASH_VERSION,
            str(row.seq),
            _iso_z(row.occurred_at),
            row.actor_id,
            row.subject,
            row.action,
            canonical_json(row.payload),
            row.prev_hash,
        ]
    )
    assert material.startswith(f'["{HASH_VERSION}",')
    assert compute_row_hash(row) == hashlib.sha256(material.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- (a) tamper evidence


def test_an_untouched_chain_verifies_and_names_its_head() -> None:
    first = append_row(
        prev=None,
        occurred_at=T0,
        actor_id="U-ANALYST-1",
        subject="CASE-4411",
        action="freeze.account",
        payload={"reason": "sanctions hit"},
    ).stored()
    second = append_row(
        prev=first,
        occurred_at=T0,
        actor_id="U-ANALYST-1",
        subject="CASE-4411",
        action="release.account",
        payload={"reason": "false positive"},
    ).stored()

    result = verify_chain([first, second])
    assert result.ok
    assert result.rows_checked == 2
    assert result.head_hash == second.row_hash
    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.row_hash


def test_an_edited_row_fails_as_a_digest_mismatch_naming_its_sequence() -> None:
    """The core tamper claim: rewrite a stored row and verification stops right there."""
    first = _row(seq=1, prev_hash=GENESIS_HASH)
    second = _row(seq=2, prev_hash=first.row_hash)
    tampered = replace(second, payload={"reason": "withdrawn by supervisor"})

    result = verify_chain([first, tampered])
    assert not result.ok
    broken = result.first_broken
    assert broken is not None
    assert broken.seq == 2
    assert "digest mismatch" in broken.reason
    assert broken.actual == tampered.row_hash
    assert broken.expected != broken.actual


def test_a_nudged_timestamp_fails_though_no_string_field_changed() -> None:
    """``occurred_at`` is inside the digest precisely so that this cannot be free."""
    row = _row(seq=1, prev_hash=GENESIS_HASH)
    result = verify_chain([replace(row, occurred_at=T0.replace(minute=1))])
    assert not result.ok
    broken = result.first_broken
    assert broken is not None
    assert broken.seq == 1
    assert "digest mismatch" in broken.reason


def test_a_deleted_row_fails_as_a_sequence_gap_not_a_digest_mismatch() -> None:
    """A missing row is a different operator problem from an edited one, and says so."""
    first = _row(seq=1, prev_hash=GENESIS_HASH)
    third = _row(seq=3, prev_hash=first.row_hash)
    result = verify_chain([first, third])
    assert not result.ok
    broken = result.first_broken
    assert broken is not None
    assert broken.seq == 3
    assert "sequence gap" in broken.reason


def test_a_relinked_chain_fails_at_the_row_that_changed_its_predecessor() -> None:
    """Truncating and re-appending must not pass as an unbroken trail."""
    first = _row(seq=1, prev_hash=GENESIS_HASH)
    second = _row(seq=2, prev_hash="a" * 64)
    result = verify_chain([first, second])
    assert not result.ok
    broken = result.first_broken
    assert broken is not None
    assert broken.seq == 2
    assert "predecessor mismatch" in broken.reason
    assert broken.expected == first.row_hash


def test_naive_timestamps_are_refused_rather_than_assumed() -> None:
    row = _unsigned(seq=1, occurred_at=datetime(2026, 9, 26, 10, 0, 0))
    with pytest.raises(ValueError, match="tz-aware"):
        compute_row_hash(row)


def test_an_unrepresentable_payload_value_is_refused_not_stringified() -> None:
    """A repr containing a memory address would make the digest machine-dependent."""
    row = _unsigned(seq=1, payload={"why": object()})
    with pytest.raises(TypeError, match="not digestable"):
        compute_row_hash(row)
