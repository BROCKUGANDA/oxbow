"""Port conformance for the audit hash-chain port, asserted against every adapter.

Plan §13 requires seven ports, each with a Protocol, a ``Null*`` implementation *and a
contract test both must pass*, and ``scripts/verify.py`` names this directory's gate "port
conformance across every adapter". ``AuditSink`` is the port the integrity claim rests on:
the submission says an investigator can prove the trail was not rewritten, and the only
evidence behind that sentence is a chain that verifies for reasons a test has checked.

So this is a *conformance* suite, not a unit test of one adapter. The same assertions run
against every ``AuditSink`` in the tree, and a new implementation that does not satisfy the
port fails here rather than in production. Adding a class is one line in ``ADAPTERS`` - that
is the intended way this file grows. (A sink alone is not enough to test a *storage* port: a
conformance test for a hash chain has to be able to reach behind the adapter and break the
stored rows. So the registry builds a :class:`Store` - a sink plus the three handles a test
needs on the storage behind it: wipe it, edit a row in it, remove a row from it.)

What the port actually promises, and what each assertion protects against:

* ``append`` numbers every row from 1 with no gaps, and each row's digest covers the
  previous row's digest. That is what makes a stored digest non-portable: an editor who
  supplies a different history cannot reuse the digests from the old one, and rows cannot be
  reordered or renumbered for free, because ``seq`` and ``prev_hash`` are both inside the
  digest. The expected digests below are hand-computed from the formula spelled out in
  :mod:`oxbow.audit.chain`'s own docstring, not read back out of the code, so an
  implementation that quietly changed its digest material would disagree with this file.
* ``tip()`` is ``None`` on an empty chain and the highest row afterwards. Callers build their
  next link from the tip, so a tip that lies re-chains the trail - and both halves still
  verify, which is the one outcome nobody would ever notice.
* ``load(from_seq=...)`` resumes at the cursor without repeating or skipping a row. This is
  the same cursor contract the SSE reconnect is judged on (01 P7: "resumes without duplicate
  stage rows"), and the failure it prevents is an investigator's timeline with a row served
  twice or a row that is simply gone - neither of which changes a single digest.
* ``verify()`` reports success on an untouched chain and names the offending ``chain_seq``
  when a row *in storage* has been changed - plan §13 and §15 both state it, in those words.
  ``rows_checked`` is pinned alongside it, because a verifier that counted the rows it walked
  past as rows it checked would report "4 rows verified" about a chain it abandoned at row 2.
* An append that does not link to the tip is refused with :class:`AuditAppendError` rather
  than silently re-chained, and the refusal leaves no hole behind.
* There is no update and no delete in the interface. A reversal is a new row referencing the
  original (plan §15), which is only a record of a reversal while the original is there.

The digest arithmetic itself - the canonical JSON array material, the ``"|"`` field-shift
collision it closed, the naive-timestamp refusal - is pinned once, where it is pure, in
``tests/unit/test_audit_chain.py``. What is pinned here is what only a sink can get wrong:
that the *stored* rows chain, that the round trip through the store preserves the payload the
digest was computed over, and that both stores reach the same answer. That last one is
asserted across the family deliberately: ``scripts/verify_audit.py`` walks a file chain and a
database chain and prints one head digest per store, and a packet export carries the file
one, so "verified" can only mean one thing if the two hash the same rows identically.

One divergence between the two implementations is asserted rather than papered over: the file
sink refuses a row whose ``prev_hash`` is not its tip, while the Postgres sink checks the
*sequence* only, so a foreign link that happens to carry the right sequence is stored and
surfaces later, at ``verify()``. The port's observable - a chain that verifies was not
tampered with - holds either way, and
:meth:`test_a_link_from_another_store_never_verifies_here` accepts exactly those two outcomes
and nothing else. The gap is named in the assertion message rather than fixed here, because
this file's job is to report what the adapters do.

Postgres is the docker-compose server on host port **5433**, provisioned as a scratch database
and dropped at the end the way ``tests/integration/test_readmodel_page_shape.py`` does it. The
schema comes from ``create_all`` rather than alembic: migration 0002's
``trg_audit_append_only`` refuses UPDATE, and three tests here depend on committing an UPDATE
that a superuser *could* make anyway - the claim being tested is that the chain catches it, not
that the table protects itself. When the server is genuinely unreachable the Postgres cases
skip with the port and the command that starts it in the reason; the guard at the bottom of
this file then fails rather than passing quietly, because half a conformance suite that reads
green is how a doctrine ends up existing only in a docstring.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import create_engine, delete, update
from sqlalchemy.orm import Session

from oxbow.adapters.audit.postgres import PostgresAuditSink
from oxbow.adapters.null.audit import NullAuditSink
from oxbow.adapters.warehouse.models import AuditEvent, Base, Run
from oxbow.audit.chain import ChainRow, ChainVerification, PendingChainRow, append_row
from oxbow.ports.audit import AuditAppendError, AuditSink

REPO_ROOT: Final = Path(__file__).resolve().parents[2]


def _setting(name: str, default: str) -> str:
    """A compose setting: the caller's environment first, then the ``.env`` compose itself reads.

    ``make`` sources ``.env`` before it runs anything and no conftest in this repository loads
    it, so a test that looked only at ``os.environ`` would find no Postgres password, provision
    nothing, and skip the one adapter the deployment actually uses. No value read here is ever
    echoed: the skip reason names host, port and command, never a credential.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    dotenv = REPO_ROOT / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            key, separator, raw = line.partition("=")
            if separator and key.strip() == name:
                return raw.strip() or default
    return default


#: The compose server's host port. Not 5432: a native ``postgres.exe`` is bound there on this
#: host and answers ``FATAL: role "oxbow" does not exist``, which reads like a bad password.
#: See docker-compose.yml's own comment on the mapping.
PG_PORT: Final = _setting("POSTGRES_PORT", "5433")
PG_START_COMMAND: Final = "docker compose up -d postgres"
TEST_DB_NAME: Final = f"oxbow_audit_conformance_{os.getpid()}_{secrets.token_hex(3)}"

#: The digest that precedes sequence 1, spelled out rather than imported: a sink that started
#: its chain from anything else would disagree with this literal, and importing the constant
#: the implementation uses would let each adapter agree with itself.
GENESIS: Final = "0000000000000000000000000000000000000000000000000000000000000000"


@dataclass(frozen=True, slots=True)
class RowSpec:
    """One audit event as a caller would write it: an instant, who, to what, and why."""

    occurred_at: datetime
    actor_id: str
    subject: str
    action: str
    payload: Mapping[str, Any]


#: The four rows every test writes, hand-written so that the digests below are checkable by
#: hand. Fixed instants, fixed actors, money in integer minor units, one pipe inside a reason
#: string and one non-ASCII value, so the store's own encoding is exercised by the fixture
#: rather than by a special case.
ROWS: Final[tuple[RowSpec, ...]] = (
    RowSpec(
        datetime(2026, 3, 4, 5, 6, 7, 890000, tzinfo=UTC),
        "analyst-1",
        "ACC-0123456789AB",
        "review",
        {"reason": "reviewed", "amount_minor": 12345, "currency": "UGX"},
    ),
    RowSpec(
        datetime(2026, 3, 4, 5, 7, 7, 890000, tzinfo=UTC),
        "reviewer-2",
        "ACC-0123456789AB",
        "escalate",
        {"reason": "escalated | confirmed by phone", "amount_minor": 480000, "currency": "UGX"},
    ),
    RowSpec(
        datetime(2026, 3, 4, 5, 8, 7, 890000, tzinfo=UTC),
        "worker-1",
        "CASE-01HZY0K2N8P3Q5R7S9T",
        "outbox_queued",
        {"attempt": 1, "counterparty": "José Müller", "sink_id": "webhook"},
    ),
    RowSpec(
        datetime(2026, 3, 4, 5, 9, 7, 890000, tzinfo=UTC),
        "analyst-1",
        "ACC-0123456789AB",
        "reverse",
        {"reason": "marked up in error", "supersedes_seq": 2},
    ),
)

#: The eight-field digest of each row above, in order, computed by hand from the formula the
#: chain module documents:
#:
#:     sha256(canonical_json_array([HASH_VERSION, seq, occurred_at_utc_z, actor_id,
#:                                  subject, action, canonical_json(payload), prev_hash]))
#:
#: For row 1 that material is, verbatim:
#:
#:     ["oxbow-audit-v1","1","2026-03-04T05:06:07.890000Z","analyst-1",
#:      "ACC-0123456789AB","review",
#:      "{\"amount_minor\":12345,\"currency\":\"UGX\",\"reason\":\"reviewed\"}",
#:      "0000000000000000000000000000000000000000000000000000000000000000"]
#:
#: and rows 2-4 chain: each row's eighth element is the previous row's digest. These are what
#: ``make verify-audit`` prints for this fixture chain, and the assertion that they are what a
#: store actually holds is the difference between "we hash something" and "we hash this".
ROW_HASHES: Final[tuple[str, ...]] = (
    "8fbf81b833564c0dfdcbd78309283ad44db750de0728e6ab3e7f67bc5974aa34",
    "eee753c46835270f5589bf7f7e2f892b926cfd2fbb45f7ed1c33515e442f77fe",
    "857fd68a10f908507fbb864ba08742dd099938d84b421f9317ebfcb73d725e4f",
    "4876210eb34784f833e2a9f714162ee0dcf245a4413caf121caf2038ae370408",
)
HEAD_HASH: Final = ROW_HASHES[-1]
CHAIN_LENGTH: Final = len(ROWS)

#: A chain of the same shape as ``ROWS`` with different bytes in every row. Used wherever a
#: test needs a genuinely *foreign* history: same length, so its next link carries the right
#: sequence number, different content, so its digests are somebody else's. Nothing here
#: depends on their values, which is why deriving them from ``ROWS`` is honest - unlike the
#: expected digests above, these are input, not assertion.
OTHER_ROWS: Final[tuple[RowSpec, ...]] = tuple(
    replace(spec, actor_id=f"{spec.actor_id}-abroad", payload={**spec.payload, "store": "other"})
    for spec in ROWS
)


# --- the scratch stores --------------------------------------------------------


@dataclass(slots=True)
class Store:
    """One sink, plus the handles a storage-backed port needs to be attacked through."""

    sink: AuditSink
    wipe: Callable[[], None]
    tamper: Callable[[int, Mapping[str, Any]], None]
    forget: Callable[[int], None]
    close: Callable[[], None]


StoreFactory = Callable[[Path], Store]
_SCRATCH: dict[str, Any] = {}
_APPENDS: dict[str, int] = {}

#: Which adapters wrote rows, and in which tests. Read by the guard at the bottom of the file:
#: it is what makes "we skipped the real adapter" a reported fact rather than a green bar.
_EXERCISED: dict[str, set[str]] = {}


def _record(sink: AuditSink) -> None:
    _APPENDS[sink.sink_id] = _APPENDS.get(sink.sink_id, 0) + 1


def _pending(prev: ChainRow | PendingChainRow | None, spec: RowSpec) -> PendingChainRow:
    """The next link, built the way every caller in this project builds it."""
    return append_row(
        prev=prev,
        occurred_at=spec.occurred_at,
        actor_id=spec.actor_id,
        subject=spec.subject,
        action=spec.action,
        payload=dict(spec.payload),
    )


def _append(sink: AuditSink, row: PendingChainRow) -> ChainRow:
    _record(sink)
    return sink.append(row)


def _write(sink: AuditSink, specs: Sequence[RowSpec] = ROWS) -> list[ChainRow]:
    """Write a chain through the sink under test, tip by tip, like a caller does."""
    rows: list[ChainRow] = []
    prev: ChainRow | None = None
    for spec in specs:
        prev = _append(sink, _pending(prev, spec))
        rows.append(prev)
    return rows


def _chain_of(name: str, directory: Path) -> Store:
    """An independent chain, written through another store and left open for the caller."""
    other = ADAPTERS[name](directory)
    other.wipe()
    _write(other.sink, OTHER_ROWS)
    return other


def _sink_dir(directory: Path, *, name: str = "sink") -> Path:
    """A private output root, so no two stores share a chain.

    ``NullAuditSink`` appends ``/audit`` to whatever root it is handed, and the real
    repository root is not somewhere a test gets to write audit rows into.
    """
    root = directory / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def _tamper_jsonl(path: Path, seq: int, fields: Mapping[str, Any]) -> None:
    """Rewrite one stored line the way an editor with write access to the file would."""
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    touched = [record for record in records if int(record["seq"]) == seq]
    assert len(touched) == 1, f"tried to edit stored seq={seq} and found {len(touched)} rows"
    for record in touched:
        record.update(fields)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _forget_jsonl(path: Path, seq: int) -> None:
    """Remove one stored line: the ``DELETE`` a superuser can always run."""
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    kept = [record for record in records if int(record["seq"]) != seq]
    assert len(kept) == len(records) - 1, f"tried to delete stored seq={seq} and removed nothing"
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in kept), encoding="utf-8"
    )


def _null_store(directory: Path) -> Store:
    sink = NullAuditSink(_sink_dir(directory))
    path = sink.path
    return Store(
        sink=sink,
        wipe=lambda: path.unlink(missing_ok=True),
        tamper=lambda seq, fields: _tamper_jsonl(path, seq, fields),
        forget=lambda seq: _forget_jsonl(path, seq),
        close=lambda: None,
    )


def _admin_candidates() -> list[str]:
    """Where to ask for a scratch database. Port 5433 unless the environment says otherwise."""
    user = _setting("POSTGRES_USER", "oxbow")
    password = _setting("POSTGRES_PASSWORD", "")
    dbname = _setting("POSTGRES_DB", "oxbow")
    urls: list[str] = []
    override = os.environ.get("OXBOW_P7_PG_ADMIN_URL", "").strip()
    if override:
        urls.append(override)
    urls.append(f"postgresql://{user}@127.0.0.1:{PG_PORT}/{dbname}")
    if password:
        urls.insert(0, f"postgresql://{user}:{password}@127.0.0.1:{PG_PORT}/{dbname}")
    return urls


def _sqlalchemy_url(admin_url: str) -> str:
    parsed = urlparse(admin_url)
    return urlunparse(parsed._replace(path=f"/{TEST_DB_NAME}")).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )


def _engine() -> Any:
    """The scratch engine once it exists; an equivalent lazy one before that.

    Building a Postgres sink must not need a server: ``create_engine`` does not connect, and
    the protocol-satisfaction test asks only whether the object satisfies the Protocol.
    """
    engine = _SCRATCH.get("engine")
    if engine is not None:
        return engine
    unprovisioned = _SCRATCH.get("unprovisioned_engine")
    if unprovisioned is None:
        unprovisioned = create_engine(_sqlalchemy_url(_admin_candidates()[-1]))
        _SCRATCH["unprovisioned_engine"] = unprovisioned
    return unprovisioned


def _postgres_store(_directory: Path) -> Store:
    """A sink over its own session and transaction.

    One chain per table by construction: ``audit_event``'s ``chain_seq`` is global, so a
    second independent Postgres chain would need a second database. That is why the foreign
    history in ``test_a_link_from_another_store_never_verifies_here`` comes from the *other*
    adapter rather than from a sibling table.
    """
    session = Session(_engine())
    sink = PostgresAuditSink(session)

    def wipe() -> None:
        session.execute(delete(AuditEvent))
        session.commit()

    def tamper(seq: int, fields: Mapping[str, Any]) -> None:
        # Committed, so the sink reads the row back out of the table rather than out of this
        # transaction's own buffer. ``expire_all`` because the identity map still holds the
        # pre-edit instance, and a test that read a stale object would report a tamper that
        # never reached storage.
        session.execute(update(AuditEvent).where(AuditEvent.chain_seq == seq).values(**fields))
        session.commit()
        session.expire_all()

    def forget(seq: int) -> None:
        session.execute(delete(AuditEvent).where(AuditEvent.chain_seq == seq))
        session.commit()
        session.expire_all()

    def close() -> None:
        session.rollback()
        session.close()

    return Store(
        sink=sink,
        wipe=wipe,
        tamper=tamper,
        forget=forget,
        close=close,
    )


# Every AuditSink in the tree. A new implementation is added here.
ADAPTERS: dict[str, StoreFactory] = {
    "null": _null_store,
    "postgres": _postgres_store,
}

#: Where each adapter's outsider comes from. ``audit_event`` carries one global chain, so a
#: second Postgres chain would need a second database, and the two stores are each other's
#: foreign history - a row from the demo path must not pass as a row from the production one,
#: or the reverse. If the server is down the Postgres case takes a second file chain instead,
#: because the property under test is independence rather than technology.
FOREIGN_STORE: dict[str, str] = {"null": "postgres", "postgres": "null"}


def _foreign_of(name: str) -> str:
    other = FOREIGN_STORE[name]
    if other == "postgres" and _SCRATCH.get("engine") is None:
        return "null"
    return other


@pytest.fixture(scope="module")
def pg_database() -> Any:
    """A scratch Postgres holding the pipeline's own ``audit_event`` table.

    Ported from ``tests/integration/test_readmodel_page_shape.py``: provision, ``create_all``,
    yield, terminate, drop. A database that outlives the module is a chain a later run would
    inherit, and a chain inherited from an earlier run is a tip that is not empty.
    """
    import psycopg

    tried: list[str] = []
    last_error = "no candidate URL"
    admin_url = ""
    for candidate in _admin_candidates():
        tried.append(urlparse(candidate).netloc.split("@")[-1])
        try:
            with psycopg.connect(candidate, autocommit=True, connect_timeout=3) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
                conn.execute(f"CREATE DATABASE {TEST_DB_NAME}")
            admin_url = candidate
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:140]}"
    else:
        pytest.skip(
            f"no Postgres reachable on 127.0.0.1:{PG_PORT} (docker-compose publishes the "
            f"container's 5432 there; 5432 is a different server on this host), so the real "
            f"audit adapter went unexercised: {last_error}. Start it with "
            f"`{PG_START_COMMAND}`, or export OXBOW_P7_PG_ADMIN_URL; tried {tried}"
        )
    engine = create_engine(_sqlalchemy_url(admin_url), future=True)
    # audit_event.run_id references run, so the parent has to exist even though no audit row
    # written here carries one.
    Base.metadata.create_all(engine, tables=[Run.__table__, AuditEvent.__table__])
    _SCRATCH["url"] = _sqlalchemy_url(admin_url)
    _SCRATCH["engine"] = engine
    yield engine
    engine.dispose()
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
    _SCRATCH.pop("engine", None)
    _SCRATCH.pop("url", None)


def _ids() -> list[str]:
    return list(ADAPTERS)


@pytest.fixture(params=sorted(ADAPTERS))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    """One empty sink per adapter per test, ready to be written into and then attacked."""
    name = str(request.param)
    if name == "postgres":
        # Resolved lazily: the null cases have to keep working with nothing else up.
        request.getfixturevalue("pg_database")
    built = ADAPTERS[name](tmp_path)
    before = _APPENDS.get(name, 0)
    built.wipe()
    yield built
    if _APPENDS.get(name, 0) > before:
        _EXERCISED.setdefault(name, set()).add(str(request.node.originalname))
    built.close()


# --- the port's structural promises -------------------------------------------


@pytest.mark.parametrize("name", _ids())
def test_satisfies_the_runtime_checkable_protocol(name: str, tmp_path: Path) -> None:
    """A duck that does not satisfy the Protocol is not an implementation of the port."""
    assert isinstance(ADAPTERS[name](tmp_path).sink, AuditSink), (
        f"{name} does not satisfy AuditSink; a decision transaction wired through a protocol "
        "the adapter does not implement fails at the audit write, after the decision has "
        "already been committed"
    )


def test_the_registry_key_is_the_name_the_sink_gives_itself(store: Store) -> None:
    """``sink_id`` is how ``make verify-audit`` labels a line of its report.

    If it disagreed with the registry, the skip guard at the bottom of this file would be
    counting a different adapter from the one the report names - and a twin store of the same
    kind must report the same name, or "which store was this read from" has no answer.
    """
    name = store.sink.sink_id
    assert name in ADAPTERS, f"{name!r} is not a key of this file's registry"
    twin = ADAPTERS[name](_sink_dir(Path(tempfile.mkdtemp()), name="twin"))
    try:
        assert twin.sink.sink_id == name, (
            f"{name} reports a different store name per instance; two chains from one "
            "technology would be indistinguishable in the verify report"
        )
    finally:
        twin.close()


def test_the_interface_offers_no_way_to_rewrite_history(store: Store) -> None:
    """Append-only is a promise about the interface, not about the author's intentions.

    A reversal is a new row referencing the original (plan §15), and that is only a record of
    a reversal while the original row is still there to point at. A method named ``update``,
    ``delete``, ``truncate`` or ``clear`` on either side of this port is the integrity claim
    with a back door in it.
    """
    for forbidden in ("update", "delete", "remove", "truncate", "clear", "rollback", "purge"):
        assert not hasattr(store.sink, forbidden), (
            f"{store.sink.sink_id} exposes .{forbidden}(); an audit trail that can be edited "
            "after the fact is a log, and the submission's claim is about a chain"
        )
    declared = {attribute for attribute in dir(AuditSink) if not attribute.startswith("_")}
    assert declared == {"append", "load", "sink_id", "tip", "verify"}, (
        f"the port itself now declares {sorted(declared)}; this assertion and every caller "
        "that relies on append-only need re-reading, not this line updating"
    )


# --- append: numbering, and what each digest covers ---------------------------


def test_append_numbers_every_row_from_one_with_no_gaps(store: Store) -> None:
    """A gap is a deleted row and a repeat is a fork; neither is possible through here."""
    rows = _write(store.sink)

    assert [row.seq for row in rows] == [1, 2, 3, 4], (
        f"the store numbered its rows {[row.seq for row in rows]}; verification checks the "
        "sequence as hard as it checks the digest, so a gap or a repeat is an audit trail "
        "with a hole in it that no reader will be told about"
    )
    tip = store.sink.tip()
    assert tip is not None and tip.seq == CHAIN_LENGTH


def test_each_digest_covers_the_row_before_it(store: Store) -> None:
    """The chaining rule, and the digests it produces.

    ``prev_hash`` inside the digest is what makes this a chain rather than four independent
    signatures: rewriting row 2 without rewriting row 3 is impossible, so the break surfaces
    at a named sequence number instead of quietly.
    """
    _write(store.sink)
    loaded = list(store.sink.load())

    assert [row.row_hash for row in loaded] == list(ROW_HASHES), (
        f"{store.sink.sink_id} holds {[row.row_hash[:12] for row in loaded]} for the fixture "
        "chain whose digests are computed by hand in this file; `make verify-audit` and a "
        "packet export would now be attesting to different bytes"
    )
    assert loaded[0].prev_hash == GENESIS, (
        "the first row must link to the genesis digest; linking it to nothing leaves the "
        "whole chain resting on an unverified first link"
    )
    for previous, current in pairwise(loaded):
        assert current.prev_hash == previous.row_hash, (
            f"seq {current.seq} links to {current.prev_hash[:12]} but seq {previous.seq} "
            f"digests to {previous.row_hash[:12]}; the trail is forked between them"
        )


def test_a_row_digest_binds_its_row_to_its_position_in_the_chain(store: Store) -> None:
    """The same row content under two different histories hashes to two different digests.

    This is the precise sense in which a stored hash cannot be reproduced after the fact: an
    editor who supplies an alternative first row and then copies the genuine second row field
    for field cannot carry the genuine digest into it. The content matches, the predecessor
    does not, and the substitution is visible at that row's own sequence number.
    """
    genuine = _write(store.sink)
    substitute = RowSpec(
        occurred_at=ROWS[0].occurred_at,
        actor_id="someone-else",
        subject=ROWS[0].subject,
        action=ROWS[0].action,
        payload=dict(ROWS[0].payload),
    )

    store.wipe()
    alternative = _write(store.sink, (substitute, ROWS[1]))

    assert alternative[1].payload == genuine[1].payload
    assert alternative[1].actor_id == genuine[1].actor_id
    assert alternative[1].seq == genuine[1].seq
    assert alternative[1].prev_hash != genuine[1].prev_hash, (
        "two chains with different first rows claim the same predecessor for row 2, so the "
        "digest is not covering the link it exists to cover"
    )
    assert alternative[1].row_hash != genuine[1].row_hash, (
        "an identical row digests identically across two different histories: every digest "
        "in the trail is portable into a fabricated chain"
    )
    assert store.sink.verify().ok, "the alternative chain must still be self-consistent"


# --- tip ---------------------------------------------------------------------


def test_tip_is_none_on_an_empty_store_and_the_head_afterwards(store: Store) -> None:
    """A caller builds its next link from the tip, so a lying tip re-chains the trail.

    ``None`` on an empty chain is load-bearing rather than a nicety: a tip that returned a
    zero-filled row it had made up would let the first append link to something that was
    never written, and nothing downstream could tell.
    """
    assert store.sink.tip() is None, (
        f"{store.sink.sink_id} reported a tip on an empty store; the first append would link "
        "to a row that does not exist and only the sequence check would notice"
    )
    rows = _write(store.sink)
    tip = store.sink.tip()
    assert tip is not None
    assert tip == rows[-1], "the tip is not the row the store holds at the head"
    assert tip.seq == CHAIN_LENGTH
    assert tip.row_hash == HEAD_HASH

    _append(store.sink, _pending(tip, ROWS[0]))
    moved = store.sink.tip()
    assert moved is not None and moved.seq == CHAIN_LENGTH + 1, (
        "the tip did not move after a fifth append, so the next caller reads a stale "
        "predecessor and two rows end up claiming the same link"
    )


# --- load: the cursor contract ------------------------------------------------


def test_load_resumes_at_the_cursor_without_repeating_or_skipping(store: Store) -> None:
    """``from_seq`` is a sequence number, not an offset, and its row is included.

    The port's words are "every row at or after ``from_seq``", which is what lets a caller
    hold the last row it saw and ask for the next one at ``cursor + 1`` - the same resume
    contract ``Last-Event-ID`` is judged on in 01 P7. The two halves have to reassemble the
    whole chain: no row in both, no row in neither, and identical digests either side of the
    cut. A row served twice or lost in the middle changes no hash anywhere, which is exactly
    why it has to be asserted here rather than noticed later.
    """
    _write(store.sink)
    whole = list(store.sink.load())
    assert [row.seq for row in whole] == [
        1,
        2,
        3,
        4,
    ], "the default cursor did not read the whole chain"

    for cursor in range(1, CHAIN_LENGTH + 3):
        resumed = list(store.sink.load(from_seq=cursor))
        assert [row.seq for row in resumed] == list(
            range(cursor, CHAIN_LENGTH + 1)
        ), f"from_seq={cursor} returned {[row.seq for row in resumed]}"
        before = [row for row in whole if row.seq < cursor]
        assert [row.row_hash for row in before] + [row.row_hash for row in resumed] == [
            row.row_hash for row in whole
        ], f"the chain does not reassemble across the cursor at from_seq={cursor}"

    assert list(store.sink.load(from_seq=CHAIN_LENGTH + 1)) == [], (
        "reading past the head returned rows; a consumer that replays what it already saw "
        "double-counts the audit trail"
    )


def test_load_returns_rows_in_sequence_order(store: Store) -> None:
    """Rows arrive oldest first. The verifier's arithmetic depends on it, in both senses."""
    _write(store.sink)
    loaded = list(store.sink.load(from_seq=2))

    assert [row.seq for row in loaded] == [2, 3, 4], f"a resumed read returned {loaded}"
    assert all(isinstance(row, ChainRow) for row in loaded)
    assert [row.seq for row in loaded] == sorted(row.seq for row in loaded), (
        "the store served its rows out of order; verify_chain reads a sequence gap in a "
        "perfectly good chain and tells the operator rows were deleted"
    )


# --- verify -------------------------------------------------------------------


def test_verify_passes_an_untouched_chain_and_counts_every_row(store: Store) -> None:
    """What ``make verify-audit`` reports, obtained from the sink rather than a fixture."""
    _write(store.sink)
    verification = store.sink.verify()

    assert isinstance(verification, ChainVerification)
    assert verification.ok
    assert verification.first_broken is None
    assert verification.rows_checked == CHAIN_LENGTH, (
        f"verify reported {verification.rows_checked} rows checked for a chain of "
        f"{CHAIN_LENGTH}; a count that is not the whole chain is a walk that stopped early"
    )
    assert verification.head_hash == HEAD_HASH
    assert verification.checked_at.tzinfo is not None, (
        "an unqualified check time cannot be compared with the rows it checked, every one of "
        "which is stored as an instant"
    )


def test_verify_on_an_empty_chain_reports_nothing_checked(store: Store) -> None:
    """An empty chain verifies. It is not a broken one, and it is not evidence either.

    ``rows_checked=0`` has to stay distinguishable from ``rows_checked=4``, because a demo host
    whose audit directory was never written and a deployment whose chain fully verifies would
    otherwise print the same OK. ``scripts/verify_audit.py`` prints "NOTHING VERIFIED" for this
    case and can only do that while the count is honest.
    """
    verification = store.sink.verify()

    assert verification.ok
    assert verification.rows_checked == 0
    assert verification.head_hash == GENESIS, (
        "an empty chain reported a head that is not the genesis digest, which is a head hash "
        "for rows this store does not have"
    )


def test_an_edited_row_fails_verification_naming_its_sequence(store: Store) -> None:
    """Plan §13 and §15: a tampered row fails verification *with the sequence number named*.

    The edit is made in the store - an actor renamed after the fact - not to an object in this
    test's memory. Every digest in the file or table is still syntactically intact and the row
    still claims the digest it was written with, so only recomputing the arithmetic catches it.
    """
    _write(store.sink)
    store.tamper(2, {"actor_id": "whoever-says-so"})
    verification = store.sink.verify()

    assert not verification.ok, (
        "a stored row whose actor was rewritten still verifies; this audit trail records who "
        "did what, and the store no longer proves it"
    )
    broken = verification.first_broken
    assert broken is not None
    assert broken.seq == 2, f"the break was named at seq {broken.seq}, not at the edited row"
    assert "digest mismatch" in broken.reason, (
        f"an edited row was reported as {broken.reason!r}; an operator has to be able to tell "
        "an edited row from a deleted one and from a re-linked chain (03 §D)"
    )
    assert broken.expected != broken.actual
    assert broken.actual == ROW_HASHES[1], (
        "the row is still claiming the digest it was written with, which is what makes this "
        "an edit rather than a re-hash"
    )
    assert verification.rows_checked == 1, (
        f"verify reported {verification.rows_checked} rows checked and then abandoned the "
        "chain at row 2; the rows it never looked at are not rows that verified"
    )


def test_a_rewritten_payload_breaks_the_chain_at_that_row(store: Store) -> None:
    """The money in the audit trail is part of the attested record.

    Same mechanism as the actor edit one row later, so the reported *position* is pinned rather
    than inferred: a verifier that always blamed the first row, or always the last, would pass
    the test above and send an investigator to the wrong place in the timeline.
    """
    _write(store.sink)
    store.tamper(3, {"payload": {"attempt": 99, "counterparty": "José Müller", "sink_id": "x"}})
    verification = store.sink.verify()

    assert not verification.ok
    broken = verification.first_broken
    assert broken is not None
    assert broken.seq == 3, f"the break was named as seq {broken.seq}, not the edited row 3"
    assert "digest mismatch" in broken.reason
    assert verification.rows_checked == 2, (
        f"{verification.rows_checked} rows were reported as checked when the walk stopped at "
        "row 3; the two rows before it are all this result attests to"
    )


def test_a_deleted_row_is_reported_as_a_gap_not_a_digest_mismatch(store: Store) -> None:
    """Three failures, three names (03 §D): edited, deleted, re-linked.

    A deletion is invisible to every digest - the surviving rows still hash to what they were
    told to - so the sequence counter is the only thing that can see it. This is the same
    superuser ``DELETE`` as the tamper tests, and the append-only trigger is only a policy
    until something reports it.
    """
    _write(store.sink)
    store.forget(2)
    verification = store.sink.verify()

    assert [row.seq for row in store.sink.load()] == [
        1,
        3,
        4,
    ], "the fixture did not actually lose a row, so this test is not testing a deletion"
    assert not verification.ok
    broken = verification.first_broken
    assert broken is not None
    assert broken.seq == 3, f"a deleted row was reported at seq {broken.seq}"
    assert "sequence gap" in broken.reason, (
        f"a missing row was reported as {broken.reason!r}; 'a row was edited' and 'a row was "
        "removed' are different investigations and different incidents"
    )
    assert (
        verification.rows_checked == 1
    ), f"{verification.rows_checked} rows counted as checked across a hole in the trail"


# --- refusal: the append that would fork the chain ----------------------------


def test_a_stale_tip_append_is_refused_and_leaves_no_hole(store: Store) -> None:
    """The lost race, on purpose (02 §E; 03 §K: one success and one 409).

    A caller reads the tip, another writer appends, the first caller then sends the row it
    built. Re-chaining it silently would renumber an event somebody else already read, and
    both chains would verify - a fork nobody sees. What makes the refusal safe to raise is
    that nothing was written on the way to it.
    """
    rows = _write(store.sink)
    stale = _pending(rows[0], ROWS[3])
    assert stale.seq == 2, "the fixture built its stale link at the wrong sequence"

    with pytest.raises(AuditAppendError) as refused:
        store.sink.append(stale)

    message = str(refused.value)
    assert "seq" in message or "prev_hash" in message, (
        f"the refusal said {message!r}; a caller has to learn which link was wrong, because "
        "the recovery is to re-read the tip and rebuild, not to retry the same row"
    )
    after = list(store.sink.load())
    assert [row.seq for row in after] == [1, 2, 3, 4], (
        f"a refused append left {[row.seq for row in after]}; the decision row, its audit row "
        "and its outbox row commit in one transaction, and a half-written chain is not "
        "something the caller can roll back"
    )
    assert store.sink.verify().ok, "the refusal left the chain broken behind it"


def test_a_link_from_another_store_never_verifies_here(store: Store) -> None:
    """Two sinks written independently must not accept each other's rows.

    The foreign chain is the same length, so its next link carries the *right* sequence number
    and the *wrong* predecessor: the one case a sequence check cannot see. Either the append
    refuses it - which is what the port's docstring says - or it is stored and the chain that
    contains it fails verification at that row. Storing it and verifying clean is not an
    available outcome: that is a decision an investigator never made, in a trail that vouches
    for it.

    Named here rather than fixed, because this file reports what the adapters do: the file sink
    refuses on ``prev_hash`` at append time, and the Postgres sink does not check the
    predecessor there at all, so a foreign link of the right length reaches the table and is
    caught only by ``verify()``.
    """
    mine = _write(store.sink)
    foreign = _chain_of(_foreign_of(store.sink.sink_id), Path(tempfile.mkdtemp()))
    try:
        link = _pending(foreign.sink.tip(), ROWS[0])
        assert link.seq == CHAIN_LENGTH + 1, (
            "the fixture's two chains stopped being the same length, so this stopped testing "
            "the predecessor and started testing the sequence counter"
        )
        assert link.prev_hash != mine[-1].row_hash, (
            "the fixture's two chains ended at the same digest, so this link is not foreign "
            "and the test has stopped testing anything"
        )
        try:
            _append(store.sink, link)
        except AuditAppendError:
            assert (
                store.sink.verify().ok
            ), "the refusal was correct and the chain behind it is broken anyway"
        else:
            verification = store.sink.verify()
            assert not verification.ok, (
                f"{store.sink.sink_id} stored a link built against another store's tip and "
                "still reports OK. Its append path checks the sequence but not the "
                "predecessor, so verification is the only thing standing between a foreign row "
                "and a clean report - and it just missed one."
            )
            broken = verification.first_broken
            assert broken is not None
            assert broken.seq == CHAIN_LENGTH + 1, (
                f"the foreign link was reported at seq {broken.seq}, not at the row that "
                "changed its predecessor"
            )
            assert "predecessor mismatch" in broken.reason, (
                f"the foreign link was reported as {broken.reason!r}; an operator reading this "
                "needs to know the chain was re-linked, not that a row was edited"
            )
    finally:
        foreign.close()


def test_the_row_append_returns_is_the_row_stored(store: Store) -> None:
    """The caller's receipt and the investigator's read have to be the same bytes.

    ``append`` hands back a row and the packet export reads that row out of storage later. If
    the store changed anything on the way in - dropped a payload key, restamped an instant -
    the digest the caller attested to is no longer the digest of what is stored, and the chain
    fails its own ``verify()`` in front of whoever checks next.
    """
    written = _write(store.sink)
    loaded = list(store.sink.load())

    assert loaded == written, (
        f"{store.sink.sink_id} stored {[row.row_hash[:12] for row in loaded]} for what it "
        f"returned as {[row.row_hash[:12] for row in written]}"
    )
    for index, (spec, row) in enumerate(zip(ROWS, loaded, strict=True), start=1):
        assert row.seq == index
        assert row.actor_id == spec.actor_id
        assert row.subject == spec.subject
        assert row.action == spec.action
        assert row.occurred_at == spec.occurred_at, (
            f"seq {row.seq} came back at {row.occurred_at}; the digest covers the instant, so "
            "a store that restamps it is rewriting the record it is attesting to"
        )
        assert row.row_hash == ROW_HASHES[index - 1]


def test_the_store_preserves_the_payload_the_digest_was_computed_over(store: Store) -> None:
    """JSON is not one encoding, and money is integer minor units (DEV-005/C5).

    A store that round-trips ``480000`` through a float - which JSONB, Parquet and a
    hand-rolled encoder can each do - writes a payload that compares equal to the one it was
    given and hashes to something else. Dict equality would pass; the chain would break on the
    next read, in production, by someone who trusted this file.
    """
    odd = RowSpec(
        occurred_at=ROWS[0].occurred_at,
        actor_id="analyst-2",
        subject="ACC-0123456789AB",
        action="review",
        payload={
            "z_key_last": 1,
            "a_key_first": {"nested": [3, 2, 1]},
            "counterparty": "José Müller",
            "amount_minor": 480000,
            "ratio": 0.91,
            "flag": True,
            "note": None,
        },
    )
    _append(store.sink, _pending(None, odd))
    stored = store.sink.load()[0]

    assert stored.payload == dict(
        odd.payload
    ), f"the store wrote {stored.payload} for the payload {dict(odd.payload)} it was given"
    amount = stored.payload["amount_minor"]
    assert isinstance(amount, int) and not isinstance(amount, bool), (
        f"money came back as {type(amount).__name__}; the digest is over the encoded bytes, "
        "and a store that widens an integer to a float has changed the row it signed"
    )
    assert stored.payload["counterparty"] == "José Müller", (
        "a store that escapes non-ASCII on the way in and unescapes it differently on the way "
        "out hashes different bytes on each side of its own round trip"
    )
    assert store.sink.verify().ok, "the payload round trip broke the chain's own digest"


# --- across the family --------------------------------------------------------


@pytest.mark.parametrize("name", _ids())
def test_every_store_hashes_the_same_rows_to_the_same_head(
    name: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The null demo path and the Postgres production path agree byte for byte.

    ``scripts/verify_audit.py`` walks a file chain and a database chain and prints one head
    digest per store; a packet export carries the file one. If the two disagreed, "verified"
    would mean something different depending on who ran it - which is two checks, and neither
    of them trustworthy. The Postgres case needs a real server, and ``pg_database`` reports
    the port and the command that starts it when there is none; the guard at the bottom of
    this file is what stops that being quiet.
    """
    if name == "postgres":
        request.getfixturevalue("pg_database")
    built = ADAPTERS[name](tmp_path)
    try:
        built.wipe()
        rows = _write(built.sink)
        assert [row.row_hash for row in rows] == list(ROW_HASHES), (
            f"{name} hashed the fixture chain differently from the values computed by hand in "
            "this file, so the two stores no longer agree about what they attested to"
        )
        assert built.sink.verify().head_hash == HEAD_HASH
    finally:
        built.close()


# --- the guard ----------------------------------------------------------------


#: Every test above that writes rows through a sink. Hand-written on purpose: this is the
#: floor that keeps a skipped real adapter honest, and a set derived from the file's own
#: function list would drift whenever someone renamed a test and quietly stop meaning
#: anything. Adding a per-store test that writes means adding its name here.
NULL_CONTRACT_TESTS: Final[frozenset[str]] = frozenset(
    {
        "test_append_numbers_every_row_from_one_with_no_gaps",
        "test_each_digest_covers_the_row_before_it",
        "test_a_row_digest_binds_its_row_to_its_position_in_the_chain",
        "test_tip_is_none_on_an_empty_store_and_the_head_afterwards",
        "test_load_resumes_at_the_cursor_without_repeating_or_skipping",
        "test_load_returns_rows_in_sequence_order",
        "test_verify_passes_an_untouched_chain_and_counts_every_row",
        "test_an_edited_row_fails_verification_naming_its_sequence",
        "test_a_rewritten_payload_breaks_the_chain_at_that_row",
        "test_a_deleted_row_is_reported_as_a_gap_not_a_digest_mismatch",
        "test_a_stale_tip_append_is_refused_and_leaves_no_hole",
        "test_a_link_from_another_store_never_verifies_here",
        "test_the_row_append_returns_is_the_row_stored",
        "test_the_store_preserves_the_payload_the_digest_was_computed_over",
    }
)


def test_the_gate_is_not_green_because_everything_skipped() -> None:
    """Last test on purpose, and the reason this file cannot pass on nothing.

    A ``pytest.skip`` on the Postgres cases is legitimate when the server is genuinely down -
    that is a fact about the host, not about the port. It is not legitimate for the gate to
    read green afterwards, which is what happens if nothing checks that the *other* half still
    ran. So the null adapter's full contract is asserted here, as a failure rather than another
    skip, and the missing Postgres coverage is named out loud.
    """
    exercised = _EXERCISED.get("null", set())
    missing = NULL_CONTRACT_TESTS - exercised
    assert not missing, (
        f"the null sink never exercised {sorted(missing)}; the conformance floor moved, and "
        "this file's promise that some adapter was tested in full no longer holds"
    )
    if "postgres" not in _EXERCISED:
        pytest.fail(
            "not one row was written through PostgresAuditSink on this host, so half of this "
            f"port's conformance went dark. Start the Compose database with "
            f"`{PG_START_COMMAND}` (it publishes host port {PG_PORT}, not 5432) and re-run "
            "`pytest -q tests/contracts_adapters`: the P7 gate is 'port conformance across "
            "every adapter', and every adapter includes the one the deployment actually uses"
        )
