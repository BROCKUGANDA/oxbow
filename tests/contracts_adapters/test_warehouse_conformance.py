"""Port conformance for the warehouse port, asserted against every adapter.

``packages/pipeline/oxbow/ports/warehouse.py`` is 02 §B seam 1: the only handoff between the
half of the system that computes and the half that serves. P7's gate is
``pytest -q tests/contracts_adapters`` and :func:`scripts.verify` labels it "port conformance
across every adapter", but the directory held one test file against a declared set of eight
ports, so the gate's name was writing a cheque the directory could not cash for seven more of
them. This is the second file, for the port the whole product reads through.

So this is a *conformance* suite, not a unit test of one adapter: every assertion below runs
against every ``WarehouseSink`` in the tree (``ADAPTERS``), and a new sink that does not
satisfy the port fails here rather than inside a rendered packet. Adding a class is one line in
``ADAPTERS`` — that is the intended way this file grows.

What the port actually promises, and what each test pins:

* ``open_run`` happens before any row references the run, and ``run_state`` *raises* for a run
  nobody opened. A sink that answered ``running`` instead would let
  ``StageEventEmitter.ensure_run_open`` skip the open, and the run's seed, config hash and
  model version would never exist to be quoted later;
* a run id is a 26-character ULID, never a native uuid (DEV-003/C3). Nothing crashes when this
  is violated: the stage-event cursor stops sorting by time on its own, and an SSE client
  skips or repeats frames in a way that looks like a flaky network (03 §J);
* ``write`` returns the count it *stored* rather than the count it was handed, and refuses a
  table outside ``WAREHOUSE_TABLES`` even when that table exists — ``run`` and ``stage_event``
  both do, and neither is writable through the port;
* every ``*_minor`` column holds an integer (DEV-005/C5) and ``txn_id`` is corpus-namespaced
  (DEV-004/C4), both checked on the *name*, so a new money column is covered the moment it is
  named without anybody editing a list;
* a run in ``IMMUTABLE_RUN_STATES`` takes no further writes — the only reason a packet can pin
  a run and stay truthful forever (plan §15) — while a ``failed`` run stays writable, because
  resuming one is the documented behaviour of ``--run-id``;
* rows written under one run are invisible under another, and the ``after_id`` cursor resumes a
  stream without repeating, without skipping, and without serving a different run's events.

The Postgres sink is exercised against a real scratch database rather than a fake, because half
these promises are enforced by column types, CHECK constraints and a sequence a mock does not
have. The schema comes from ``Base.metadata.create_all`` and *not* from ``apps/api/alembic``:
migration 0002 installs database triggers that enforce run immutability a second time, and a
sink that had forgotten the port-level refusal would then pass for the wrong reason. Where that
choice leaves the two adapters disagreeing, the notes at the foot of this module say so instead
of asserting the comfortable version.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from oxbow.adapters.null.warehouse import NullWarehouse
from oxbow.adapters.warehouse.models import Base
from oxbow.adapters.warehouse.postgres import PostgresWarehouseSink
from oxbow.ports.warehouse import (
    IMMUTABLE_RUN_STATES,
    WAREHOUSE_TABLES,
    RunState,
    WarehouseSink,
    WarehouseTableError,
    is_ulid,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_DB_NAME = f"oxbow_warehouse_conformance_{os.getpid()}_{secrets.token_hex(3)}"

SinkFactory = Callable[["Workspace"], WarehouseSink]

# The two WarehouseSink implementations in the tree. `apps/api/deps.py::_warehouse_sink_factory`
# hands out exactly this pair, so this register and the container cannot drift apart unnoticed:
# a third sink is one line here, and every assertion below then applies to it.
EXPECTED_SINK_ID = {"null": "null", "postgres": "postgres"}

# Canonical 26-character ULIDs, written by hand rather than minted at runtime: a conformance
# fixture has to be able to name the run whose rows it expects (DEV-003).
RUN_A = "01J6ZXN4T8V3WKQM5RPSGHYBED"
RUN_B = "01J6ZXN4T9MNPQRSTVWXY01234"
RUN_NEVER_OPENED = "01J6ZXN4TAABCDEFGHJKMNPQRS"
# A native uuid, which is the form DEV-003/C3 exists to refuse.
NOT_A_RUN_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"

T_MORNING = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
T_NOON = datetime(2026, 1, 17, 12, 0, tzinfo=UTC)
T_EVENING = datetime(2026, 1, 19, 21, 5, tzinfo=UTC)

# Three accounts, every value hand-written so no expected number is derived from the code under
# test. The instants and the counts differ per row, so a sink that mixed two rows over cannot
# pass; `funding_minor` sums to 1_483_964 (14,839.64 UGX at the economics card's 100 minor units
# per unit), which is the total the round-trip test re-adds.
ACCOUNT_ROWS: tuple[dict[str, Any], ...] = (
    {
        "account_key": "A00000000001",
        "source_dataset": "paysim",
        "first_seen_at": T_MORNING,
        "last_seen_at": T_EVENING,
        "txn_count": 7,
        "n_outbound": 4,
        "n_inbound": 3,
        "n_counterparties": 5,
        "funding_minor": 1_400_000,
        "currency": "UGX",
        "age_days": 4,
    },
    {
        "account_key": "A00000000002",
        "source_dataset": "paysim",
        "first_seen_at": T_NOON,
        "last_seen_at": T_NOON,
        "txn_count": 2,
        "n_outbound": 2,
        "n_inbound": 0,
        "n_counterparties": 1,
        "funding_minor": 0,
        "currency": "UGX",
        "age_days": 0,
    },
    {
        "account_key": "A00000000003",
        "source_dataset": "ibmaml",
        "first_seen_at": T_MORNING,
        "last_seen_at": T_NOON,
        "txn_count": 3,
        "n_outbound": 0,
        "n_inbound": 3,
        "n_counterparties": 2,
        "funding_minor": 83_964,
        "currency": "UGX",
        "age_days": 2,
    },
)
EXPECTED_ACCOUNT_KEYS = ("A00000000001", "A00000000002", "A00000000003")
EXPECTED_FUNDING_MINOR = 1_483_964

# One transfer, written so the balance columns close: 1_000_000 - 983_964 = 16_036.
TRANSACTION_ROW: dict[str, Any] = {
    "txn_id": "paysim:41",
    "event_ts_utc": T_MORNING,
    "event_date_local": date(2026, 1, 15),
    "local_hour": 9,
    "src_account_key": "A00000000001",
    "dst_account_key": "A00000000002",
    "amount_minor": 983_964,
    "currency": "UGX",
    "txn_type": "TRANSFER",
    "src_balance_before": 1_000_000,
    "src_balance_after": 16_036,
    "dst_balance_before": 0,
    "dst_balance_after": 983_964,
    "label_fraud": False,
    "label_typology": "funnel",
    "source_dataset": "paysim",
}

# A stage ledger that fails halfway through, which is the case the cursor actually has to serve:
# a client that reconnects mid-run must see the failure and nothing it already rendered.
STAGE_LEDGER: tuple[dict[str, Any], ...] = (
    {"stage": "ingest", "status": "running", "rows": 0, "elapsed_ms": 0, "detail": None},
    {"stage": "ingest", "status": "complete", "rows": 41_902, "elapsed_ms": 1_840, "detail": None},
    {"stage": "graph", "status": "running", "rows": 0, "elapsed_ms": 0, "detail": None},
    {"stage": "graph", "status": "complete", "rows": 12_884, "elapsed_ms": 9_233, "detail": None},
    {
        "stage": "score",
        "status": "failed",
        "rows": 0,
        "elapsed_ms": 412,
        "detail": "calibration refused: band_n is 0",
    },
)
# The row `apps/api/schemas/events.py::StageEvent` renders and `oxbow.stage_events.sse_frame`
# serialises. Stated here as a literal, because the frame is what the browser consumes.
STAGE_EVENT_COLUMNS = frozenset(
    {"id", "run_id", "stage", "status", "rows", "elapsed_ms", "detail", "emitted_at"}
)


@dataclass(frozen=True)
class Workspace:
    """One test's private warehouse, provisioned both ways.

    Each sink uses one half: the null adapter the directory, the Postgres adapter the session.
    Provisioning both keeps ``ADAPTERS`` a plain registry of sinks-of-a-workspace, which is the
    shape ``tests/contracts_adapters/test_watchlist_conformance.py`` uses, and it means neither
    adapter is privileged by the fixture.
    """

    root: Path
    session: Session


def _postgres_sink(workspace: Workspace) -> WarehouseSink:
    return PostgresWarehouseSink(workspace.session)


def _null_sink(workspace: Workspace) -> WarehouseSink:
    return NullWarehouse(workspace.root)


# Every WarehouseSink in the tree. A new implementation is added here.
ADAPTERS: dict[str, SinkFactory] = {
    "postgres": _postgres_sink,
    "null": _null_sink,
}


def _ids() -> list[str]:
    return list(ADAPTERS)


@pytest.fixture(params=sorted(ADAPTERS))
def sink(request: pytest.FixtureRequest, workspace: Workspace) -> WarehouseSink:
    return ADAPTERS[request.param](workspace)


# --- the scratch Postgres -------------------------------------------------------


def _setting(name: str, default: str) -> str:
    """A compose setting: the caller's environment first, then the ``.env`` compose itself reads.

    ``make`` sources ``.env`` before running anything and no conftest in this repository loads
    it, so a test that looked only at ``os.environ`` would find no Postgres password and
    provision nothing. The value is never echoed; ``_redact`` keeps it out of any message.
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


def _candidate_admin_urls() -> list[str]:
    """Host port 5433, and only 5433.

    ``.env.example`` records why the usual fallback is wrong here: compose publishes the
    container on 5433 because something else on this machine already owns 5432 — a native
    Postgres that answers ``role "oxbow" does not exist``, which reads as a broken password
    rather than as the wrong server. Provisioning a contract test's scratch database there
    would conform the port against a warehouse that is not this project's.
    """
    override = os.environ.get("OXBOW_P7_PG_ADMIN_URL", "").strip()
    if override:
        return [override]
    user = _setting("POSTGRES_USER", "oxbow")
    port = _setting("POSTGRES_PORT", "5433")
    dbname = _setting("POSTGRES_DB", "oxbow")
    password = _setting("POSTGRES_PASSWORD", "")
    urls = [f"postgresql://{user}@127.0.0.1:{port}/{dbname}"]
    if password:
        urls.insert(0, f"postgresql://{user}:{password}@127.0.0.1:{port}/{dbname}")
    return urls


def _redact(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.password:
        return url
    netloc = f"{parsed.username}:***@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


@pytest.fixture(scope="module")
def warehouse_engine() -> Iterator[Engine]:
    """A scratch database holding the pipeline's own metadata, provisioned once per module.

    A dedicated *database* and not a schema inside the deployment's: this fixture DROPs, and it
    must never be able to do that to a warehouse somebody is investigating. The rows each test
    writes are never committed (see ``workspace``), so one module-scoped schema serves every
    test without a TRUNCATE and without two tests seeing each other's rows.
    """
    import psycopg

    tried: list[str] = []
    last_error = "no candidate URL"
    admin_url = ""
    for candidate in _candidate_admin_urls():
        tried.append(_redact(candidate))
        try:
            with psycopg.connect(candidate, autocommit=True, connect_timeout=3) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
                conn.execute(f"CREATE DATABASE {TEST_DB_NAME}")
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:140]}"
            continue
        admin_url = candidate
        break
    else:
        pytest.skip(
            f"no Postgres reachable on the compose host port (POSTGRES_PORT, default 5433), so "
            f"the real sink could not be exercised; tried {tried}, last failure {last_error}. "
            "Start it with `docker compose up -d postgres` (or `make up`) and re-run. The null "
            "adapter still runs, but a warehouse port conformed to by one adapter is not "
            "conformance, which is the thing this directory's gate claims."
        )

    parsed = urlparse(admin_url)
    # `postgresql+psycopg`, not the bare scheme: SQLAlchemy maps `postgresql://` to psycopg2,
    # which is not installed (02 §F pins psycopg3). Same normalisation the app applies.
    url = urlunparse(parsed._replace(path=f"/{TEST_DB_NAME}")).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (TEST_DB_NAME,),
        )
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")


@pytest.fixture()
def workspace(warehouse_engine: Engine, tmp_path: Path) -> Iterator[Workspace]:
    """A fresh output directory and a fresh session that is rolled back, never committed."""
    session = Session(warehouse_engine)
    try:
        yield Workspace(root=tmp_path, session=session)
    finally:
        session.close()


def _open_run(sink: WarehouseSink, run_id: str = RUN_A, *, provenance: str = "pipeline") -> str:
    """Register a run with the identity the port asks for, and return its id."""
    sink.open_run(
        run_id,
        seed=1337,
        timezone="Africa/Kampala",
        provenance=provenance,
        config_hash="0" * 64,
        model_version="p4b-1",
        dataset_ref="paysim",
    )
    return run_id


def _record(sink: WarehouseSink, run_id: str, entry: Mapping[str, Any]) -> int:
    return sink.record_stage_event(
        run_id,
        stage=str(entry["stage"]),
        status=str(entry["status"]),
        rows=int(entry["rows"]),
        elapsed_ms=int(entry["elapsed_ms"]),
        detail=entry["detail"],
    )


def _ids_of(events: Sequence[Mapping[str, Any]]) -> list[int]:
    return [int(event["id"]) for event in events]


# --- the port's structural promises ---------------------------------------------


@pytest.mark.parametrize("name", _ids())
def test_satisfies_the_runtime_checkable_protocol(name: str, workspace: Workspace) -> None:
    """A duck that does not satisfy the Protocol is not an implementation of the port.

    Both sinks are handed to ``oxbow.stage_events.StageEventEmitter`` and built by
    ``apps/api/deps.py``, both typed against the Protocol, so a method missing here is an
    AttributeError raised mid-stream, after some of the frames have already been rendered.
    """
    assert isinstance(ADAPTERS[name](workspace), WarehouseSink), (
        f"{name} does not satisfy WarehouseSink; it will fail at the call site in production, "
        "not at the seam that is supposed to make it substitutable"
    )


@pytest.mark.parametrize("name", _ids())
def test_names_the_warehouse_it_writes(name: str, workspace: Workspace) -> None:
    """``sink_id`` is the port's docstring made executable: ``null``, ``postgres``.

    ``apps/api/main.py`` reports ``warehouse_backend`` in every health body for the same
    reason — a row from a fixture warehouse quoted as a pipeline measurement is the failure
    00 §B rule 3 forbids, and it is only detectable if the sink can say which one it is.
    """
    sink = ADAPTERS[name](workspace)
    assert sink.sink_id == EXPECTED_SINK_ID[name], (
        f"{name} reports sink_id {sink.sink_id!r}, not {EXPECTED_SINK_ID[name]!r}: the API "
        "would tell an operator it is reading a warehouse it is not reading"
    )


def test_no_two_sinks_report_the_same_sink_id(workspace: Workspace) -> None:
    """Two adapters answering ``null`` is a backend report with no information in it."""
    reported = [ADAPTERS[name](workspace).sink_id for name in _ids()]
    assert len(set(reported)) == len(reported), f"sinks reported {reported} — indistinguishable"


# --- run lifecycle ---------------------------------------------------------------


def test_an_opened_run_is_running_until_something_says_otherwise(sink: WarehouseSink) -> None:
    """``open_run`` registers a run and ``run_state`` reports the state the port promises.

    ``running`` is the only state a fresh run may report: it is what makes the run resumable
    and writable, so a sink that opened a run already terminal would reject the first write
    the pipeline made against it.
    """
    run_id = _open_run(sink)
    assert sink.run_state(run_id) is RunState.RUNNING


def test_an_unknown_run_is_reported_as_unknown_and_not_as_a_state(sink: WarehouseSink) -> None:
    """``run_state`` "raises for an unknown run", and the raise is load-bearing.

    ``StageEventEmitter.ensure_run_open`` catches exactly ``WarehouseTableError`` to decide
    "open it" versus "resume it". Answering ``running`` for a run nobody opened makes that
    branch take the resume path, and the run row — seed, config hash, model version,
    provenance — is never written. Every later response then cites a run that does not exist.
    """
    with pytest.raises(WarehouseTableError):
        sink.run_state(RUN_NEVER_OPENED)


def test_a_run_id_that_is_not_a_ulid_is_refused_at_both_ends(sink: WarehouseSink) -> None:
    """DEV-003/C3. A UUID does not crash anything; it sorts wrongly, forever.

    Asserted on ``open_run`` and on ``write`` because those are the two entry points a caller
    reaches first. The consequence is downstream and silent: the stage-event cursor is keyed on
    this text, so a run id that does not sort by creation time makes an SSE client skip or
    repeat frames in a way that reads as a flaky network (03 §J).
    """
    assert is_ulid(RUN_A) and is_ulid(RUN_B) and is_ulid(RUN_NEVER_OPENED), (
        "the fixture's own ids must be canonical, or every refusal below fires on the literal "
        "instead of on the adapter"
    )
    with pytest.raises(WarehouseTableError):
        sink.open_run(
            NOT_A_RUN_ID,
            seed=1337,
            timezone="UTC",
            provenance="pipeline",
            config_hash="0" * 64,
            model_version="p4b-1",
        )
    _open_run(sink)
    with pytest.raises(WarehouseTableError):
        sink.write("account", NOT_A_RUN_ID, ACCOUNT_ROWS)


def test_reopening_a_run_that_already_exists_is_refused(sink: WarehouseSink) -> None:
    """A second ``open_run`` would reset the state of a run whose rows already exist.

    That is one of the two ways a completed run gets back to ``running`` and starts accepting
    writes again — the other is ``complete_run``, and the freeze is what the packet's
    truthfulness rests on (plan §15).
    """
    _open_run(sink)
    with pytest.raises(WarehouseTableError):
        _open_run(sink)


def test_a_row_cannot_reference_a_run_that_was_never_opened(sink: WarehouseSink) -> None:
    """The first sentence of ``open_run``: register a run *before* any row references it.

    The foreign key is the same rule inside the database, but an orphan row is worse than a
    rejected one: ``GET /api/runs/{id}`` answers 404 while the score row underneath it is still
    being ranked into somebody's queue.
    """
    with pytest.raises(WarehouseTableError):
        sink.write("account", RUN_NEVER_OPENED, ACCOUNT_ROWS)


# --- write and read round trip ---------------------------------------------------


def test_write_returns_the_count_a_later_read_finds(sink: WarehouseSink) -> None:
    """``write`` returns "the count actually committed", and ``read`` is the only witness.

    This is the assertion that catches a sink reporting the number it was *handed* instead of
    the number it *stored*. The pipeline prints what ``write`` returned into the stage-event
    ledger, and that ledger is what ``make verify`` and the packet quote: a run claiming 41,902
    rows while holding none is the most damaging artifact this repository can produce.
    """
    run_id = _open_run(sink)
    written = sink.write("account", run_id, ACCOUNT_ROWS)
    assert written == 3, f"the fixture holds three account rows; write reported {written}"
    stored = list(sink.read("account", run_id))
    assert written == len(stored), (
        f"write committed {written} rows and the warehouse holds {len(stored)}: the ledger's "
        "row count and the served table now disagree, and only one of them is auditable"
    )


def test_read_gives_back_every_column_that_was_written(sink: WarehouseSink) -> None:
    """Round trip, column by column, on a table the API serves directly.

    ``read`` is what resume and the contract tests use; ``apps/api/readmodel.py`` reads the same
    columns for the queue. A value that comes back *changed* — a minor-units integer widened to
    a float, a ``CHAR(12)`` key padded or folded — is a number an analyst quotes into a packet.
    """
    run_id = _open_run(sink)
    sink.write("account", run_id, ACCOUNT_ROWS)
    rows = list(sink.read("account", run_id))
    assert len(rows) == 3, f"the run holds {len(rows)} account rows; the fixture wrote three"
    stored = {str(row["account_key"]): row for row in rows}
    # A set rather than a sequence: `read`'s docstring promises "the table's declared order" and
    # the Postgres adapter issues no ORDER BY, so positional order is not something either sink
    # actually guarantees. *Which* keys came back is, and so is everything inside each of them.
    assert set(stored) == set(
        EXPECTED_ACCOUNT_KEYS
    ), f"read back the account keys {sorted(stored)}; the fixture wrote {EXPECTED_ACCOUNT_KEYS}"
    assert len(stored) == len(rows), "two accounts came back under one account_key"

    for expected in ACCOUNT_ROWS:
        key = str(expected["account_key"])
        row = stored[key]
        for column, value in expected.items():
            assert (
                row[column] == value
            ), f"{key}.{column} came back {row[column]!r}, stored {value!r}"
        money = row["funding_minor"]
        assert isinstance(money, int) and not isinstance(money, bool), (
            f"{key}.funding_minor came back as {type(money).__name__}: DEV-005 fixes minor "
            "units as integers, and a float does not crash on the way in, it drifts on the way "
            "out — which is exactly how a money column survives a review"
        )

    total = sum(int(row["funding_minor"]) for row in stored.values())
    assert total == EXPECTED_FUNDING_MINOR, (
        f"stored funding sums to {total} minor units, expected {EXPECTED_FUNDING_MINOR} "
        "(14,839.64 UGX): the money the queue ranks on is not the money that was landed"
    )


def test_rows_written_under_one_run_are_invisible_under_another(sink: WarehouseSink) -> None:
    """Rescoring is a new run, so a new run must not see the old one's rows.

    ``score`` is unique on (run_id, account_key) for this reason. A leak across runs would make
    the queue for run B a merge of two models, and no response body in the API says so.
    """
    first = _open_run(sink, RUN_A)
    second = _open_run(sink, RUN_B)
    sink.write("account", first, ACCOUNT_ROWS[:1])
    assert list(sink.read("account", second)) == []

    sink.write("account", second, ACCOUNT_ROWS[1:])
    assert sorted(str(row["account_key"]) for row in sink.read("account", first)) == [
        "A00000000001"
    ], "run B's rows appeared under run A"
    assert sorted(str(row["account_key"]) for row in sink.read("account", second)) == [
        "A00000000002",
        "A00000000003",
    ], "the two runs collapsed into one table"


def test_a_read_honours_the_limit_it_was_given(sink: WarehouseSink) -> None:
    """``limit`` is how resume bounds a backfill; an ignored limit re-reads the table.

    Only the count is asserted, not which rows came back: the port's docstring promises "the
    table's declared order" and ``PostgresWarehouseSink.read`` issues no ``ORDER BY``, so a
    positional assertion here would pin down nothing that the adapter actually guarantees.
    """
    run_id = _open_run(sink)
    sink.write("account", run_id, ACCOUNT_ROWS)
    page = list(sink.read("account", run_id, limit=2))
    assert len(page) == 2, f"limit=2 returned {len(page)} rows out of three"
    assert {str(row["account_key"]) for row in page} <= set(EXPECTED_ACCOUNT_KEYS)


@pytest.mark.parametrize("table", ["run", "stage_event", "not_a_table_at_all"])
def test_write_refuses_a_table_outside_the_declared_handoff(
    sink: WarehouseSink, table: str
) -> None:
    """``WAREHOUSE_TABLES`` is the handoff vocabulary, and two of these three names exist.

    ``run`` and ``stage_event`` are real tables in the same schema and are deliberately *not*
    writable through the port: a run row is created by ``open_run`` and frozen by
    ``complete_run``, so a ``write("run", ...)`` that landed would rewrite lifecycle state
    around the immutability rule. Refusing a table that exists is the part of this promise that
    a plain metadata lookup would quietly drop.
    """
    assert (
        table not in WAREHOUSE_TABLES
    ), f"{table} is now a declared handoff table, so this case no longer tests a refusal"
    _open_run(sink)
    with pytest.raises(WarehouseTableError):
        sink.write(table, RUN_A, ACCOUNT_ROWS)


def test_money_arrives_in_integer_minor_units(sink: WarehouseSink) -> None:
    """DEV-005/C5, checked on the column *name* so a new money column is covered unnamed.

    The consequence is the port's own: ``0.1 + 0.2`` summed over six million rows is a
    reconciliation failure that stays invisible until the column is quoted in a packet.
    ``scripts/no_float_money.py`` gates the source; this gates the data crossing the seam.
    ``True`` is included because Python's bool *is* an int, and ``funding_minor=True`` is a
    claim about one minor unit that nobody made.
    """
    run_id = _open_run(sink)
    with pytest.raises(WarehouseTableError):
        sink.write("account", run_id, [{**ACCOUNT_ROWS[0], "funding_minor": 14_000.00}])
    with pytest.raises(WarehouseTableError):
        sink.write("account", run_id, [{**ACCOUNT_ROWS[0], "funding_minor": True}])
    assert list(sink.read("account", run_id)) == [], (
        "a refused money row still landed: the refusal is decorative and the queue will rank on "
        "a figure that cannot be summed"
    )
    assert sink.write("account", run_id, ACCOUNT_ROWS) == 3, (
        "the refusals left the run unusable, so one bad row in a batch of six million takes the "
        "whole stage down instead of the row"
    )


def test_a_transaction_id_names_the_corpus_it_came_from(sink: WarehouseSink) -> None:
    """DEV-004/C4: two corpora share one table and row 41 of each is not one transaction.

    The namespaced form is written and read back too, because a convention that only refuses is
    a convention nobody can land data under. The balance arithmetic is checked on the stored
    row rather than on the input: a sink that widened ``BIGINT`` minor units on the way through
    would break the subtraction and nothing else here would notice.
    """
    run_id = _open_run(sink)
    with pytest.raises(WarehouseTableError):
        sink.write("transaction", run_id, [{**TRANSACTION_ROW, "txn_id": "41"}])
    assert list(sink.read("transaction", run_id)) == []

    assert sink.write("transaction", run_id, [TRANSACTION_ROW]) == 1
    stored = list(sink.read("transaction", run_id))
    assert [str(row["txn_id"]) for row in stored] == ["paysim:41"]
    row = stored[0]
    assert row["amount_minor"] == 983_964 and row["dst_balance_after"] == 983_964
    assert int(row["src_balance_before"]) - int(row["amount_minor"]) == int(
        row["src_balance_after"]
    ), (
        "the stored balances do not close on the stored amount (1_000_000 - 983_964 = 16_036): "
        "evidence that does not add up is evidence invented, and this row is what a case rests on"
    )


# --- the immutability of a finished run -----------------------------------------


@pytest.mark.parametrize(
    "terminal", sorted(IMMUTABLE_RUN_STATES, key=lambda state: state.value), ids=lambda s: s.value
)
def test_a_terminal_run_takes_no_further_writes(sink: WarehouseSink, terminal: RunState) -> None:
    """A stated invariant of this repository, asserted against both sinks and not weakened.

    ``write``'s own docstring gives the reason a packet depends on: a run is immutable once
    complete, "which is the only reason a packet can pin one and stay truthful forever"
    (plan §15). The refusal is asserted *after* a write that succeeded, so the only thing that
    changed between the two calls is the run's state — a sink that refused for a bad table name,
    or for a bad row, would pass a weaker version of this test and still be rewriting runs.
    """
    run_id = _open_run(sink)
    assert sink.write("account", run_id, ACCOUNT_ROWS[:1]) == 1
    sink.complete_run(run_id, terminal)
    assert sink.run_state(run_id) is terminal, (
        f"complete_run({terminal.value}) left the run reporting {sink.run_state(run_id).value}: "
        "nothing downstream can tell that this run is finished"
    )
    with pytest.raises(WarehouseTableError):
        sink.write("account", run_id, ACCOUNT_ROWS[1:])
    assert len(list(sink.read("account", run_id))) == 1, (
        f"a run already marked {terminal.value} accepted two more rows: every packet pinned to "
        "it now describes a table the run did not have when the packet was signed"
    )


def test_a_failed_run_is_still_writable(sink: WarehouseSink) -> None:
    """The other half of the same sentence, and the half an over-eager guard breaks.

    ``IMMUTABLE_RUN_STATES`` is exactly ``{complete, superseded}``: ``failed`` ends a run's
    lifecycle but does not freeze its rows, because ``StageEventEmitter.ensure_run_open``
    resumes a failed run on purpose. Refusing writes to one would turn every retried pipeline
    into a fresh run id whose earlier stage events describe a different run.
    """
    run_id = _open_run(sink)
    sink.complete_run(run_id, RunState.FAILED, error="calibration refused for lack of positives")
    assert sink.run_state(run_id) is RunState.FAILED
    assert sink.run_state(run_id) not in IMMUTABLE_RUN_STATES
    assert sink.write("account", run_id, ACCOUNT_ROWS) == 3, (
        "a failed run took no rows, so --run-id cannot resume one and a stage that died on the "
        "last of five has to be re-run from the first"
    )


# --- the stage-event ledger and the resume cursor --------------------------------


def test_a_recorded_event_comes_back_shaped_like_a_frame(sink: WarehouseSink) -> None:
    """``record_stage_event`` returns "its monotonic, stable id", and the row must be readable.

    The key set is the one ``apps/api/schemas/events.py::StageEvent`` renders and
    ``oxbow.stage_events.sse_frame`` serialises. A missing ``detail`` is a KeyError in the middle
    of an open stream; a missing ``id`` is a frame no client can resume from. This is also the
    premise of ``StageEventEmitter.emit``, which reads the row back through the sink rather than
    trusting its own copy.
    """
    run_id = _open_run(sink)
    event_id = _record(sink, run_id, STAGE_LEDGER[1])
    stored = [event for event in sink.stage_events(run_id) if int(event["id"]) == event_id]
    assert len(stored) == 1, (
        f"record_stage_event returned id {event_id} and the ledger holds {len(stored)} such rows: "
        "an id that names nothing is not a durable event"
    )
    event = stored[0]
    assert frozenset(event) == STAGE_EVENT_COLUMNS, (
        f"the stored ledger row carries {sorted(event)}, and a frame rendered from it either "
        "loses a field the UI reads or invents one it does not"
    )
    assert event["run_id"] == run_id
    assert (event["stage"], event["status"], int(event["rows"]), int(event["elapsed_ms"])) == (
        "ingest",
        "complete",
        41_902,
        1_840,
    ), f"the ledger row reads {event}, not the measurement that was handed to it"


def test_a_cursor_resumes_without_repeating_or_skipping(sink: WarehouseSink) -> None:
    """The SSE reconnect, end to end: ``Last-Event-ID`` backfills from durable state.

    Five events, a client that saw the first three, then a reconnect. It must receive exactly
    the last two in ascending id order, and asking twice must give the same answer. A cursor
    that repeats re-renders a stage the analyst already watched; one that skips loses the stage
    that failed, which is the only row in the ledger anybody needed. ``apps/api/events.py``
    reads through this method and nothing else, so both failures are invisible in the UI.
    """
    run_id = _open_run(sink)
    ids = [_record(sink, run_id, entry) for entry in STAGE_LEDGER]
    assert ids == sorted(ids) and len(set(ids)) == len(ids), (
        f"event ids came back {ids}: not strictly increasing, so a cursor comparison cannot "
        "name a position in the ledger"
    )

    seen = _ids_of(sink.stage_events(run_id))
    assert seen == ids, f"the ledger served {seen} for a run that emitted {ids}"

    cursor = seen[2]
    resumed = _ids_of(sink.stage_events(run_id, after_id=cursor))
    assert resumed == seen[3:], (
        f"resuming at {cursor} gave {resumed}, not the {seen[3:]} that were missed: the client "
        "either re-renders frames it already showed or loses the failure"
    )
    assert _ids_of(sink.stage_events(run_id, after_id=cursor)) == resumed, (
        "the same cursor twice gave two different answers, so resume is not idempotent and a "
        "client that retries a dropped connection duplicates the ledger"
    )
    assert set(resumed).isdisjoint(seen[:3])

    failed = [
        event for event in sink.stage_events(run_id, after_id=cursor) if event["stage"] == "score"
    ]
    assert len(failed) == 1 and failed[0]["status"] == "failed", (
        "the failed score stage is not in the backfill, so a reconnecting operator sees a run "
        "that stopped rather than a run that broke"
    )


def test_one_runs_cursor_never_serves_another_runs_events(sink: WarehouseSink) -> None:
    """Two live runs share one warehouse, and one stream must not read the other's ledger.

    The id is global on purpose — a per-run counter would let two runs both hand out id 1 and
    ``Last-Event-ID: 1`` would be ambiguous (DEV-003/C3 is the same argument about run ids).
    That is exactly why the filter has to be on ``run_id`` and not on the cursor: the last
    assertion is the one an "optimise the backfill to a pure id range" change breaks.
    """
    first = _open_run(sink, RUN_A)
    second = _open_run(sink, RUN_B)
    _record(sink, first, STAGE_LEDGER[0])
    seen_by_first = _record(sink, first, STAGE_LEDGER[1])
    later = _record(sink, second, STAGE_LEDGER[2])

    assert [str(event["run_id"]) for event in sink.stage_events(first)] == [first, first]
    assert _ids_of(sink.stage_events(second)) == [later]
    assert later > seen_by_first, "the fixture's premise: the other run's id is the higher one"
    assert list(sink.stage_events(first, after_id=later)) == [], (
        "a cursor past another run's newest event reached back into this run's ledger: the "
        "stream for one run would be rendered with another run's stage names"
    )


# --- what this suite cannot assert, written down rather than smoothed over --------
#
# Three places where the two sinks do not hold the port to the same standard. None of them is
# reachable as a family assertion, so each is recorded here with the call that demonstrates it:
#
# 1. ``WAREHOUSE_TABLES`` names ``dataset_snapshot`` and ``Base.metadata`` has no such table.
#    ``PostgresWarehouseSink.write("dataset_snapshot", ...)`` gets past ``assert_writable_table``
#    — the name *is* declared — and dies on ``_TABLES[table]`` with a bare ``KeyError`` instead
#    of a ``WarehouseTableError``. The port calls the tuple "the handoff vocabulary"; one of its
#    twenty-two names is a name the adapter that owns the vocabulary cannot write.
# 2. ``complete_run`` out of a frozen state. ``NullWarehouse`` refuses any state change once a
#    run is complete or superseded; ``PostgresWarehouseSink.complete_run`` assigns ``run.state``
#    whatever it is handed, so through the port alone a complete run can be put back to
#    ``running`` and start taking writes again. Migration 0002's ``guard_run_immutable`` trigger
#    is the only thing that stops that in a deployment, and this suite deliberately does not
#    install it (see ``warehouse_engine``). Asserted per adapter below, because only one of the
#    two actually enforces it.
# 3. A ledger event for a run that was never opened. ``record_stage_event`` on the Postgres sink
#    raises the foreign-key violation as a bare ``sqlalchemy.exc.IntegrityError`` — unlike
#    ``write``, it catches nothing — while ``NullWarehouse`` appends the row and returns an id.
#    Two different answers to one call, and the null one is the silent one. The port does not
#    state which is correct, so neither is asserted here; the emitter that consumes this method
#    only ever calls it after ``ensure_run_open``.


def test_the_null_sink_freezes_a_completed_runs_own_state(tmp_path: Path) -> None:
    """Freezing has to hold both ways or it is not a freeze.

    Null-specific because only this adapter enforces it: see finding 2 above. The behaviour is
    asserted on the object the API's own worker reaches through
    (``apps/api/worker.py::_refuse_immutable_run`` checks the state, then completes the run), so
    a silent regression here would only surface on the null backend — which is the backend the
    demo and the offline boot run on.
    """
    warehouse = NullWarehouse(tmp_path / "freeze")
    run_id = _open_run(warehouse)
    warehouse.complete_run(run_id, RunState.COMPLETE)
    with pytest.raises(WarehouseTableError):
        warehouse.complete_run(run_id, RunState.RUNNING)
    assert warehouse.run_state(run_id) is RunState.COMPLETE, (
        "a completed run that could be moved back to running is a run whose rows can be "
        "rewritten by going through the state door instead of the write door"
    )


def test_the_null_adapters_ledger_ids_belong_to_the_file_not_to_the_object(tmp_path: Path) -> None:
    """``out/warehouse/stage_events.jsonl`` is the durable copy, so a fresh reader must agree.

    The cursor promise is worthless if the id is a counter in the writer's memory: the CLI emits
    through one ``NullWarehouse`` and the API's worker and read paths construct their own
    (``apps/api/deps.py`` builds a fresh sink per request), so a second instance that restarted
    the numbering would make every reconnect replay a whole run — and would report the run as
    running forever.
    """
    writer = NullWarehouse(tmp_path)
    run_id = _open_run(writer)
    ids = [_record(writer, run_id, entry) for entry in STAGE_LEDGER]

    reader = NullWarehouse(tmp_path)
    assert _ids_of(reader.stage_events(run_id)) == ids, (
        "a second sink instance over the same files renumbered the ledger, so a client holding "
        "Last-Event-ID from the first instance cannot resume from the second"
    )
    assert reader.run_state(run_id) is RunState.RUNNING
    assert (tmp_path / "warehouse" / "stage_events.jsonl").is_file(), (
        "the null warehouse kept its ledger in memory: out/warehouse is what the offline demo "
        "serves, and a run with no rows there is a run that never happened"
    )
