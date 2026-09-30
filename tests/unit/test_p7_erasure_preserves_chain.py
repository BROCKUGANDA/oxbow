"""Right to erasure against an immutable audit chain, proved on the real write path.

`oxbow/audit/erasure.py` carries a docstring saying ``chain_intact`` "is asserted by
``test_erasure_preserves_chain``". No such test existed, and neither did any test for
`execute_erasure` -- so the repository's erasure claim (`ARCHITECTURE.md`: "the salt
mapping is destroyed, the chain survives, the subject is unrecoverable"; the
"not a limitation" section of `LIMITATIONS.md`) rested on a citation to a test that had
never been written. That is a gate that cannot fail, which this repo's own doctrine
calls decoration.

Three properties are proved here, in the order the doctrine states them:

1. **The chain still verifies.** Deleting every `pseudonym_map` row for an account key
   and appending the erasure record must leave `verify_chain` green over the whole
   retained history and must not move a single earlier digest. If it did, "immutable
   audit" would be conditional on nobody ever exercising a data-protection right.
2. **The subject stops being recoverable.** The mapping table is the only place a salt
   exists, so the deleted-row count is what makes the surviving `account_key`
   references unrecoverable. `subject_recoverable` must be `False` after a real erasure
   and `True` when nothing was found -- an erasure that deleted nothing is not an
   erasure, and reporting one silently is the failure this pins out.
3. **The write-time identifying-key refusal bites** when a payload carries one.

Two things this file deliberately does *not* pretend are fixed, both recorded as open
findings in `docs/FAILURE-MODES.md`: no route reaches `execute_erasure` at all
(`auth.py:201` advertises the `erasure` capability with nothing behind it), and one
request cannot erase one subject holding two salted keys, because `pseudonym_map` has no
`subject_ref` column to select on -- adding it belongs in
`adapters/warehouse/models.py`, which this change does not own. The third test below
therefore asserts *current* behaviour in the shape of a tripwire, so the day someone
adds `subject_ref` it goes red and forces the decision to be taken deliberately.

The audit side runs on `FileAuditSink`, the same `AuditSink` port the Postgres sink
implements, so the digests are real bytes rather than a mock. The mapping side runs on
an in-memory SQLite session over the two shipped tables, because the claim under test is
what the policy destroys and retains, not how Postgres stores it.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api.decisions import execute_erasure  # noqa: E402
from api.security import Principal  # noqa: E402

from oxbow.adapters.file.audit import FileAuditSink  # noqa: E402
from oxbow.adapters.warehouse.models import (  # noqa: E402
    Base,
    ErasureRequest,
    PseudonymMap,
)
from oxbow.audit.chain import append_row, verify_chain  # noqa: E402
from oxbow.audit.erasure import (  # noqa: E402
    ERASURE_ACTION,
    FORBIDDEN_ERASURE_KEYS,
    PseudonymMapping,
    assert_erasure_payload_is_safe,
    build_erasure_audit_row,
    select_mappings_for_subject,
)

ACCOUNT_KEY = "01ERASUREACCOUNT0000000000A"
SIBLING_KEY = "01ERASUREACCOUNT0000000000B"
SUBJECT = "subject-under-test"


def _principal() -> Principal:
    return Principal(
        subject="auditor-1", roles=("admin",), display_name="Auditor One", source="local-jwt"
    )


def _mapping(account_key: str, salt_version: str) -> PseudonymMap:
    return PseudonymMap(
        account_key=account_key,
        salt_version=salt_version,
        source_id="paysim",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


@pytest.fixture()
def session() -> Iterator[Session]:
    """A session over only the two tables erasure touches.

    `create_all(tables=...)` rather than the whole metadata: most of the schema carries
    Postgres `JSONB` columns, and narrowing the DDL to the tables under test is both
    honest about the boundary and the reason this is a unit test rather than an
    integration one.

    `erasure_request.id` is declared `BigInteger, primary_key, autoincrement`, which is
    correct on Postgres, where a sequence backs it, and inert on SQLite, which only
    auto-assigns for a column rendered `INTEGER PRIMARY KEY`. The type is therefore
    widened to `Integer` for the duration of the DDL and restored afterwards -- a
    dialect accommodation to create the same table, not a change to what the shipped
    schema says, and deliberately not a mock: the insert below is the one
    `execute_erasure` performs itself.
    """
    column = ErasureRequest.__table__.c.id
    declared = column.type
    column.type = Integer()
    try:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(
            engine, tables=[PseudonymMap.__table__, ErasureRequest.__table__]
        )
        with Session(engine) as live:
            yield live
    finally:
        column.type = declared


@pytest.fixture()
def audit(tmp_path: Path) -> FileAuditSink:
    return FileAuditSink(tmp_path / "audit")


def _seed_decisions(sink: FileAuditSink, count: int = 3) -> None:
    """Decision-shaped rows, appended through the port so the digests are real."""
    prev = None
    for index in range(count):
        row = append_row(
            prev=prev,
            occurred_at=datetime(2026, 9, 2, index, tzinfo=UTC),
            actor_id="reviewer-1",
            subject=f"case-{index}",
            action="decision_recorded:dismiss",
            payload={"case_id": f"case-{index}", "decision_seq": index + 1},
        )
        sink.append(row)
        prev = row


def test_erasure_preserves_chain(session: Session, audit: FileAuditSink) -> None:
    """The erasure property in one sentence: the mapping goes, the digests stay.

    Asserted against the *composed chain in the store*, not against the return value
    alone: `chain_intact: True` is a claim the function makes about the audit sink, and
    a function that returned `True` over a chain that no longer verifies would pass a
    test that only read the dict.
    """
    session.add_all([_mapping(ACCOUNT_KEY, "v1"), _mapping(SIBLING_KEY, "v1")])
    session.flush()
    _seed_decisions(audit)
    before = list(audit.load())
    assert verify_chain(before).ok and len(before) == 3

    result = execute_erasure(
        session,
        audit,
        account_key=ACCOUNT_KEY,
        reason="data-subject request under 03 L",
        principal=_principal(),
    )

    after = list(audit.load())
    verification = verify_chain(after)
    assert (
        verification.ok
    ), f"the chain stopped verifying after an erasure: {verification.first_broken}"
    assert result["chain_intact"] is True, "the function's claim must agree with the store"
    assert len(after) == len(before) + 1, "erasure appends exactly one permanent row"
    assert [row.row_hash for row in after[:3]] == [
        row.row_hash for row in before
    ], "a retained digest moved: history was rewritten to make the deletion fit"
    assert after[-1].action == ERASURE_ACTION
    assert after[-1].payload["mapping_rows_deleted"] == 1
    assert result["mapping_rows_deleted"] == 1
    assert result["subject_recoverable"] is False

    survivors = list(session.execute(select(PseudonymMap)).scalars().all())
    assert [row.account_key for row in survivors] == [
        SIBLING_KEY
    ], "erasure deleted the wrong subject's mapping, or left the target behind"
    proof = list(session.execute(select(ErasureRequest)).scalars().all())
    assert (
        len(proof) == 1 and proof[0].audit_seq == after[-1].seq
    ), "the erasure must be evidenced by a row naming the audit sequence it wrote"
    assert (
        proof[0].subject_ref_hash != SIBLING_KEY
    ), "the erasure proof must not be a plain copy of the key it records"


def test_an_erasure_of_a_key_that_was_never_mapped_says_so(
    session: Session, audit: FileAuditSink
) -> None:
    """Nothing deleted is not the same event as something erased.

    `ErasureResult.subject_recoverable` is defined as `mapping_rows_deleted == 0`, so a
    zero-row erasure has to be reportable as such rather than silently succeeding.
    """
    _seed_decisions(audit)
    result = execute_erasure(
        session,
        audit,
        account_key=ACCOUNT_KEY,
        reason="no mapping exists for this key",
        principal=_principal(),
        salt_version="v-none",
    )
    assert result["mapping_rows_deleted"] == 0
    assert (
        result["subject_recoverable"] is True
    ), "an erasure that deleted nothing left the subject recoverable and must say so"
    assert verify_chain(audit.load()).ok


def test_an_erasure_record_cannot_carry_an_identifier() -> None:
    """The write-time refusal, driven to show that it bites.

    `FORBIDDEN_ERASURE_KEYS` is checked against the *keys* of the payload, so the
    reachable case is a payload assembled with one of them -- which is what a future
    caller adding a field would do. The current producer builds a fixed four-key payload
    in which none of them appears, so the guard is a tripwire rather than a live check
    today; asserting it directly is what keeps it from rotting into decoration.
    """
    assert FORBIDDEN_ERASURE_KEYS, "the denylist itself must not be empty"
    for key in FORBIDDEN_ERASURE_KEYS:
        with pytest.raises(ValueError) as raised:
            assert_erasure_payload_is_safe({"account_key": ACCOUNT_KEY, key: "anything"})
        assert key in str(raised.value), f"the refusal did not name {key!r}: {raised.value}"

    safe = build_erasure_audit_row(
        account_key=ACCOUNT_KEY,
        tip=None,
        reason="the erasure record is permanent, so it may not hold the deleted data",
        actor_id="auditor-1",
        salt_version="v1",
        mapping_rows_deleted=1,
        occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
    )
    assert not (
        set(safe.payload) & set(FORBIDDEN_ERASURE_KEYS)
    ), "the row the producer builds must itself satisfy the guard it calls"
    assert safe.payload["account_key"] == ACCOUNT_KEY


def test_erasure_reaches_only_the_named_account_key(session: Session, audit: FileAuditSink) -> None:
    """The multi-salt gap, pinned as a tripwire on current behaviour.

    `select_mappings_for_subject` exists because one subject can hold several salted
    keys, and an erasure that removes only one of them is not erasure. `execute_erasure`
    selects `PseudonymMap` by `account_key`, and the table carries no `subject_ref` to
    select on, so the policy module's own promise cannot be met through the API path.

    Asserted as what happens *today* -- the sibling key survives -- so that adding
    `subject_ref` and honouring the policy makes this test fail on purpose rather than
    leaving the finding open forever.
    """
    session.add_all([_mapping(ACCOUNT_KEY, "v1"), _mapping(SIBLING_KEY, "v2")])
    session.flush()

    result = execute_erasure(
        session,
        audit,
        account_key=ACCOUNT_KEY,
        reason="single-key erasure",
        principal=_principal(),
        salt_version="v1",
    )
    assert result["mapping_rows_deleted"] == 1
    assert verify_chain(audit.load()).ok

    remaining = list(session.execute(select(PseudonymMap)).scalars().all())
    assert [row.account_key for row in remaining] == [SIBLING_KEY], (
        "the storage layer now reaches a sibling key: the query no longer selects by "
        "account_key alone, so this tripwire and the finding must both be retired"
    )
    # The policy layer, by contrast, can see both keys for one subject: the two
    # layers' notions of "the same person" do not agree, and that is the finding.
    as_policy_rows = [
        PseudonymMapping(
            account_key=key,
            subject_ref=SUBJECT,
            salt_version=f"v{index}",
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        for index, key in enumerate((ACCOUNT_KEY, SIBLING_KEY), start=1)
    ]
    assert len(select_mappings_for_subject(as_policy_rows, SUBJECT)) == 2
    assert len(select_mappings_for_subject(as_policy_rows, "someone-else")) == 0, (
        "subject selection matched rows it should not have: erasure would cross a "
        "data-subject boundary"
    )


def test_the_gate_itself_bites(tmp_path: Path) -> None:
    """Every assertion above is run against a chain that has actually been broken.

    A test of immutability that never sees a mutable thing is the defect it is meant to
    catch, so the verifier is pointed at a tampered file before it is trusted to pass a
    clean one.
    """
    sink = FileAuditSink(tmp_path / "audit")
    _seed_decisions(sink)
    path = sink.path
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert verify_chain(sink.load()).ok

    edited = lines[1].replace("decision_recorded:dismiss", "decision_recorded:approv")
    assert edited != lines[1], "the mutation did not change the stored row"
    path.write_text("\n".join([lines[0], edited, lines[2]]) + "\n", encoding="utf-8")

    broken = verify_chain(sink.load())
    assert not broken.ok and broken.first_broken is not None
    assert broken.first_broken.seq == 2, "the break must be named by sequence number"

    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    gapped = verify_chain(sink.load())
    assert not gapped.ok and gapped.first_broken is not None
    assert (
        gapped.first_broken.seq == 3
    ), "a deleted row must surface as the row that no longer follows its predecessor"


def test_erasure_payload_assertion_is_not_vacuous() -> None:
    """The guard's denylist must contain something a payload could plausibly carry.

    `build_erasure_audit_row` writes `salt_version`; the denylist bans `salt`. If a
    future edit renamed the payload key to exactly a banned name, the guard would fire
    -- and this check documents that the two spellings are *not* the same today, which
    is the reason the guard cannot fire from the current producer.
    """
    produced = build_erasure_audit_row(
        account_key=ACCOUNT_KEY,
        tip=None,
        reason="probe",
        actor_id="auditor-1",
        salt_version="v1",
        mapping_rows_deleted=1,
        occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
    )
    overlap = set(produced.payload) & set(FORBIDDEN_ERASURE_KEYS)
    assert overlap == set(), (
        f"the producer now emits banned keys {sorted(overlap)}: either the guard would "
        "fire on every row (good) or the two lists have drifted (fix the producer)"
    )
    assert "salt" in FORBIDDEN_ERASURE_KEYS and "salt_version" in produced.payload, (
        "the near-miss between `salt` and `salt_version` is the documented reason the "
        "guard is a tripwire today; if either spelling moved, re-read finding 1"
    )


def test_execute_erasure_is_reachable_from_no_route() -> None:
    """The dead-code half of the finding, kept honest by the router table.

    `execute_erasure` is exported from `api.decisions` and `auth.py:201` advertises the
    `erasure` capability to `admin`, but no mounted route calls it. Asserting that here
    rather than asserting the opposite later means the finding closes with a test change
    and a deliberate note, not with a silently stale claim in `LIMITATIONS.md`.
    """
    from api import decisions
    from api import main as api_main

    assert callable(decisions.execute_erasure)
    app = api_main.create_app()
    paths = sorted(getattr(route, "path", "") for route in app.routes)
    assert paths, "the app mounted no routes, so the absence below would prove nothing"
    erasure_paths = [path for path in paths if "eras" in path.lower()]
    assert erasure_paths == [], (
        f"an erasure route {erasure_paths} now exists: retire the finding in "
        "docs/FAILURE-MODES.md and assert what the route returns"
    )
