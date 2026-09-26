"""P7 — the worker side of the job seam: someone must consume what the API enqueues.

``apps/api/jobs.py`` enqueues ``"api.worker.run_stages"`` by dotted name, and until
``apps/api/worker.py`` existed nothing in the repository had ever resolved that name. No test
could have caught that by importing the module, because the module was the missing thing; so
these tests pin the seam from both ends and then drive it.

Three properties are under test, in that order:

* **the name and the kwargs match, on both sides.** The dotted name is read out of
  ``jobs.py``'s own AST rather than restated here, so a rename on either side turns this red
  instead of letting two files agree with each other and nothing else. The fake queue in
  ``test_submit_job_pushes_that_name_with_those_kwargs`` is legitimate for the same reason:
  what is asserted is a contract between two modules in this repository, not Redis.
* **the states are the ones the read model can render**, written to the tables the read model
  reads. Ledger rows are checked through ``container.read_model`` -- the same object the SSE
  route polls -- and ``job_run.state`` is asserted to be RQ's own vocabulary, not a string
  invented here.
* **failure is a state, never a silence.** A stage that raises must leave a named ``failed``
  in three places and no stage left open at ``running``; that is the guarantee
  ``jobs.py:120-130`` makes on the submission side and the one the stream depends on.

Two environments, mirroring ``test_p7_api.py``: the state machine, the ledger and the
re-delivery rules are properties of the real Postgres tables with their real CHECK constraints
and their real ``stage_event`` sequence, so they run against a scratch database this module
provisions and migrates. The null-file container gets tests of its own because "the demo runs
with nothing else up" (plan §13) is a claim about this code path too; it is checked against
real files in a temporary directory.

The scratch database is named per process and is deliberately *not* shared with
``test_p7_api.py``: that module's ``_provision_database`` opens with ``DROP DATABASE ... WITH
(FORCE)``, so one name between the two would let this one's setup terminate that one's live
backends mid-session -- the failure its own comment records.

Secrets are explicit test values injected through the environment; the repository's
``RUN_SALT`` is never read, and no secret value is printed.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import secrets
import shutil
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

# Imported before ``api.jobs`` (which reaches pyarrow through ``oxbow.adapters.io``) for the
# same reason ``apps/api/worker.py`` and ``tests/conftest.py`` do it: osqp's native algebra
# probe faults the process when pyarrow is already resident. conftest keeps the pytest session
# safe; this line keeps the module importable on its own.
import osqp  # noqa: F401 -- imported for the native load it performs, not for the name
import pytest
from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[2]

# Served as ``python apps/api/worker.py`` and enqueued as ``api.worker.*``, so the package name
# has to resolve from ``apps`` -- the same bootstrap ``test_p7_api.py`` performs.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api import jobs as api_jobs  # noqa: E402
from api import outbox as api_outbox  # noqa: E402
from api import worker as api_worker  # noqa: E402
from api.deps import build_container  # noqa: E402
from api.readmodel import RunNotFound  # noqa: E402
from api.settings import reset_settings_cache  # noqa: E402

from oxbow import cli as oxbow_cli  # noqa: E402
from oxbow.adapters.warehouse.models import JobRun, Run, StageEvent  # noqa: E402
from oxbow.adapters.warehouse.postgres import new_run_id  # noqa: E402
from oxbow.ports.warehouse import RunState  # noqa: E402

# The P7 environment plumbing is reused rather than restated, so a second looser copy of "what
# a P7 test environment means" cannot exist.
from tests.integration.test_p7_api import (  # noqa: E402
    _candidate_admin_urls,
    _configure_env,
    _migrate,
    _redact,
)

TEST_DB_NAME = f"oxbow_p7w_test_{os.getpid()}_{secrets.token_hex(3)}"

#: The dotted name ``jobs.py`` enqueues. Restated here only as the value the AST test compares
#: the source against; the assertion below derives the same string from the file it reads.
ENQUEUED_NAME = "api.worker.run_stages"

STAGE_COLUMNS = ("stage", "status", "rows", "elapsed_ms", "detail")


# --- the scratch Postgres this module owns ----------------------------------


def _provision_database() -> str:
    """Create this module's own scratch database and return its SQLAlchemy URL."""
    import psycopg

    tried: list[str] = []
    last_error = "no candidate URL"
    for admin_url in _candidate_admin_urls():
        tried.append(_redact(admin_url))
        try:
            with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
                conn.execute(f"CREATE DATABASE {TEST_DB_NAME}")
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:140]}"
            continue
        parsed = urlparse(admin_url)
        return (
            urlunparse(parsed._replace(path=f"/{TEST_DB_NAME}"))
            .replace("postgresql://", "postgresql+psycopg://", 1)
            .replace("postgres://", "postgresql+psycopg://", 1)
        )
    pytest.fail(
        "the worker's state machine needs a real Postgres and none was reachable. Tried "
        f"{tried}; last failure {last_error}. Start it with `docker compose up postgres`. "
        "job_run.state, the stage_event CHECK constraints and the run-row immutability guard "
        "are properties of the real tables; they are not tested against a substitute."
    )
    raise AssertionError("unreachable")


def _drop_database(database_url: str) -> None:
    import psycopg

    parsed = urlparse(database_url)
    for url in (urlunparse(parsed._replace(path="/postgres")), database_url):
        try:
            with psycopg.connect(url, autocommit=True, connect_timeout=3) as conn:
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
                conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
            return
        except Exception:
            continue


@pytest.fixture(scope="module")
def warehouse() -> Iterator[dict[str, Any]]:
    """A migrated Postgres, empty of runs: every row below is written by a test."""
    monkeypatch = pytest.MonkeyPatch()
    url = _provision_database()
    _configure_env(monkeypatch, DATABASE_URL=url, OXBOW_WAREHOUSE="postgres")
    _migrate(url)
    container = build_container()
    assert container.backend == "postgres" and container.write_path_enabled, (
        "the worker's bookkeeping needs the Postgres write path; the fixture asked for it and "
        f"got backend={container.backend} write_path={container.write_path_enabled}"
    )
    yield {"url": url, "container": container}
    container.close()
    _drop_database(url)
    monkeypatch.undo()
    reset_settings_cache()


class _FakeQueue:
    """Records ``enqueue`` and hands back a distinct job id per submission.

    The id is what ``submit_job`` keys the ``job_run`` row on, so one id per fake would make
    a test that submits twice collide on the primary key rather than exercise two jobs.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.job_ids: list[str] = []

    @property
    def job_id(self) -> str:
        """The most recent submission's job -- the one a test is following."""
        return self.job_ids[-1]

    def enqueue(self, *args: Any, **kwargs: Any) -> Any:
        job_id = f"p7w-fake-{secrets.token_hex(6)}"
        self.job_ids.append(job_id)
        self.calls.append({"args": args, "kwargs": kwargs})
        return _FakeJob(job_id)

    def get_job_ids(self) -> list[str]:
        return list(self.job_ids)


class _FakeJob:
    def __init__(self, job_id: str) -> None:
        self.id = job_id


@pytest.fixture()
def queue(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    """The queue ``submit_job`` would have built, replaced with a recorder.

    ``api/jobs.py`` builds ``Queue(QUEUE_NAME, connection=Redis.from_url(...))`` inside
    ``_queue``; patching that one function keeps the PING, the named refusal and the row
    writes that surround the enqueue intact, and swaps only the Redis handle.
    """
    fake = _FakeQueue()
    monkeypatch.setattr(api_jobs, "_queue", lambda container: fake)
    return fake


@pytest.fixture()
def null_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A null-file container over a temporary ``out/``, with the real ``config/`` copied in.

    Both ``OXBOW_REPO_ROOT`` and ``OXBOW_OUT_ROOT`` point inside the temporary directory on
    purpose: ``build_container`` resolves the read source from the repo root while the null
    warehouse sink resolves its own root from the environment, so leaving either behind would
    have this test appending its ledger into the repository's live ``out/warehouse``.
    """
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    (tmp_path / "out").mkdir()
    _configure_env(
        monkeypatch,
        OXBOW_REPO_ROOT=str(tmp_path),
        OXBOW_OUT_ROOT=str(tmp_path / "out"),
        DATABASE_URL="",
        OXBOW_WAREHOUSE="null",
    )
    container = build_container()
    assert container.backend == "null-file", (
        f"the fixture asked for a null-file deployment and got {container.backend}; the "
        "refusal this test is about would then be the write path's instead"
    )
    yield container
    container.close()
    monkeypatch.undo()
    reset_settings_cache()


# --- helpers ----------------------------------------------------------------


def _submit(
    container: Any, queue: _FakeQueue, *, stages: tuple[str, ...], kind: str
) -> api_jobs.JobSubmission:
    """Register a run through the real submission path, with only the queue faked."""
    return api_jobs.submit_job(
        container,
        kind=kind,
        stages=stages,
        origin_run_id=None,
        requested_by="p7w-test-analyst",
        note=None,
    )


def _ledger(container: Any, run_id: str) -> list[tuple[str, str, int]]:
    """This run's stage events as the *read model* returns them, in stored id order."""
    return [
        (str(event["stage"]), str(event["status"]), int(event["rows"]))
        for event in sorted(
            container.read_model.stage_events(run_id), key=lambda item: int(item["id"])
        )
    ]


def _ledger_details(container: Any, run_id: str) -> list[dict[str, Any]]:
    """The same rows straight from the table, with the columns the ledger renders."""
    session = container.new_session()
    try:
        rows = session.execute(
            select(StageEvent).where(StageEvent.run_id == run_id).order_by(StageEvent.id)
        ).scalars()
        return [{column: getattr(row, column) for column in STAGE_COLUMNS} for row in rows]
    finally:
        session.close()


def _run_row(container: Any, run_id: str) -> dict[str, Any]:
    return dict(container.read_model.run_row(run_id))


def _job_row(warehouse: dict[str, Any], job_id: str) -> JobRun:
    session = warehouse["container"].new_session()
    try:
        row = session.get(JobRun, job_id)
        assert row is not None, f"no job_run row for {job_id}"
        return row
    finally:
        session.close()


def _run_state_raw(warehouse: dict[str, Any], run_id: str) -> Run:
    session = warehouse["container"].new_session()
    try:
        row = session.get(Run, run_id)
        assert row is not None, f"no run row for {run_id}"
        return row
    finally:
        session.close()


def _ok_runner(rows: int = 3) -> Any:
    """A stage body that measures something and reports it, like the real ones do."""

    def body(handle: Any) -> int:
        handle.add_rows(rows)
        handle.detail = "p7w synthetic stage body"
        return oxbow_cli.EXIT_OK

    return body


def _raising_runner(message: str) -> Any:
    def body(handle: Any) -> int:
        handle.add_rows(1)
        raise RuntimeError(message)

    return body


def _exiting_runner(code: int) -> Any:
    def body(_handle: Any) -> int:
        return code

    return body


def _null_ledger(container: Any, run_id: str) -> list[tuple[str, str, int]]:
    """The stage ledger on the null-file backend, read where the null sink writes it.

    ``NullWarehouse.record_stage_event`` appends to ``out/warehouse/stage_events.jsonl`` while
    ``FileWarehouseSource._table_rows`` reads ``out/warehouse/stage_event/run=<id>.jsonl`` --
    two spellings of one table, so the API's read model returns no stage rows for a run on
    files (``run_row`` works, because ``runs.jsonl`` is special-cased). That mismatch is in
    ``apps/api/readmodel.py`` and ``packages/pipeline/oxbow/adapters/null/warehouse.py``, both
    outside this change; it is reported, and this helper reads the file that is actually
    written rather than restating the claim the read model cannot currently answer.
    """
    import json

    path = container.settings.repo_root / "out" / "warehouse" / "stage_events.jsonl"
    if not path.is_file():
        return []
    return [
        (str(event["stage"]), str(event["status"]), int(event["rows"]))
        for event in (
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        )
        if event.get("run_id") == run_id
    ]


def _null_run_record(container: Any, run_id: str) -> dict[str, Any]:
    import json

    path = container.settings.repo_root / "out" / "warehouse" / "runs.jsonl"
    records = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    return next(record for record in records if record.get("run_id") == run_id)


def _no_floats(payload: dict[str, Any]) -> bool:
    """Money is integer minor units (01 §B); a job result must not smuggle a float in.

    Checked structurally rather than by name because the payload lands in a JSONB column:
    ``1.0`` and ``1`` are different facts to a reader, and only one of them is a count this
    worker could have measured.
    """
    stack: list[Any] = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, float):
            return False
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list | tuple):
            stack.extend(value)
    return True


def _ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


# --- the seam: name, kwargs, and the call site that produces them -----------


def test_the_dotted_name_jobs_enqueue_resolves_to_this_builds_worker() -> None:
    """``jobs.py``'s literal must import to this module's ``run_stages``.

    The name is read out of the source with ``ast`` rather than written here, so the test
    fails when either side drifts. Two files agreeing with each other while the queue keeps
    enqueueing a name nobody implements is the exact defect this module closes.
    """
    tree = ast.parse(Path(api_jobs.__file__).read_text(encoding="utf-8"))
    names = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "enqueue"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert names == [ENQUEUED_NAME], f"jobs.py enqueues {names}, expected [{ENQUEUED_NAME}]"

    module_name, _, attribute = ENQUEUED_NAME.rpartition(".")
    resolved = getattr(importlib.import_module(module_name), attribute)
    assert (
        resolved is api_worker.run_stages
    ), f"{ENQUEUED_NAME} resolved to {resolved!r}, not the worker in this build"
    assert callable(resolved)


def test_run_stages_accepts_exactly_the_kwargs_the_queue_passes() -> None:
    """RQ calls the function with ``run_id``, ``stages`` and ``kind`` as keywords."""
    signature = inspect.signature(api_worker.run_stages)
    required = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    assert required == {"run_id", "stages", "kind"}, required
    for name in sorted(required):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
    # ``from __future__ import annotations`` means each annotation is its source spelling;
    # compared as strings so the assertion is about the declared type, not about whether
    # ``get_type_hints`` can resolve a TYPE_CHECKING-only import.
    assert {name: signature.parameters[name].annotation for name in sorted(required)} == {
        "kind": "str",
        "run_id": "str",
        "stages": "list[str]",
    }
    # Anything the queue passes that is RQ's own must not collide with the worker's signature.
    assert "job_timeout" not in signature.parameters, (
        "job_timeout is an RQ scheduling argument; accepting it here would mean two owners "
        "of one number"
    )


def test_submit_job_pushes_that_name_with_those_kwargs(
    warehouse: dict[str, Any], queue: _FakeQueue
) -> None:
    """The call site, executed: the row is written before the job is promised."""
    container = warehouse["container"]
    submission = _submit(container, queue, stages=api_jobs.PIPELINE_STAGES, kind="pipeline")

    assert len(queue.calls) == 1, queue.calls
    call = queue.calls[0]
    assert call["args"] == (ENQUEUED_NAME,), call["args"]
    assert call["kwargs"] == {
        "run_id": submission.run_id,
        "stages": list(api_jobs.PIPELINE_STAGES),
        "kind": "pipeline",
        "job_timeout": "2h",
    }, call["kwargs"]
    assert isinstance(call["kwargs"]["stages"], list), "the stage tuple is passed as a list"

    job = _job_row(warehouse, queue.job_id)
    assert job.state == api_worker.JOB_QUEUED, "submission opens the row in RQ's queued state"
    assert job.run_id == submission.run_id
    assert job.queue == api_jobs.QUEUE_NAME
    assert job.trace_id is None, "the job's own trace begins in the worker, not the request"

    run = _run_state_raw(warehouse, submission.run_id)
    assert run.state == RunState.RUNNING.value
    assert str(run.model_version).startswith(api_worker.PENDING_VERSION_PREFIX)


# --- the state machine, against the real tables -----------------------------


def test_run_stages_drives_its_stages_and_lands_the_transitions(
    warehouse: dict[str, Any], queue: _FakeQueue
) -> None:
    """``queued -> started -> finished``, one ledger pair per stage, readable by the API."""
    container = warehouse["container"]
    submission = _submit(container, queue, stages=("ingest", "graph"), kind="pipeline")
    assert _ledger(container, submission.run_id) == [], "submission alone writes no stage rows"

    summary = api_worker.run_stages(
        run_id=submission.run_id,
        stages=["ingest", "graph"],
        kind="pipeline",
        container=container,
        runners={"ingest": _ok_runner(11), "graph": _ok_runner(5)},
    )

    assert _ledger(container, submission.run_id) == [
        ("ingest", "running", 0),
        ("ingest", "complete", 11),
        ("graph", "running", 0),
        ("graph", "complete", 5),
    ]
    run = _run_row(container, submission.run_id)
    assert run["state"] == RunState.COMPLETE.value
    assert run["finished_at"] is not None
    assert not str(run["model_version"]).startswith(api_worker.PENDING_VERSION_PREFIX), (
        "the placeholder has to be replaced while the run is still open, or every response "
        "this run produces names a queue state as its model version"
    )

    job = _job_row(warehouse, queue.job_id)
    assert job.state == api_worker.JOB_FINISHED
    assert job.finished_at is not None
    assert job.error is None
    assert job.trace_id is not None and len(str(job.trace_id)) == 32, "a minted trace id"

    assert summary["run_state"] == RunState.COMPLETE.value
    assert summary["stage_status"] == {"ingest": "complete", "graph": "complete"}
    assert summary["events_emitted"] == 4
    assert summary["ledger"] == "resumed", "submit_job opened the run; this job resumed it"
    assert isinstance(summary["elapsed_ms"], int)
    assert summary["exit_codes"] == {"ingest": 0, "graph": 0}
    assert _no_floats(summary)


def test_job_run_states_are_rqs_own_vocabulary(
    warehouse: dict[str, Any], queue: _FakeQueue
) -> None:
    """No status string the read model cannot render is written by this worker."""
    from rq.job import JobStatus

    assert (
        JobStatus.QUEUED.value,
        JobStatus.STARTED.value,
        JobStatus.FINISHED.value,
        JobStatus.FAILED.value,
    ) == (
        api_worker.JOB_QUEUED,
        api_worker.JOB_STARTED,
        api_worker.JOB_FINISHED,
        api_worker.JOB_FAILED,
    )
    assert api_worker.OPEN_JOB_STATES == ("queued", "started")
    assert api_worker.CLOSED_JOB_STATES == ("finished", "failed")

    container = warehouse["container"]
    submission = _submit(container, queue, stages=("ingest",), kind="pipeline")
    seen: list[str] = []

    def watching(handle: Any) -> int:
        session = container.new_session()
        try:
            seen.append(str(session.get(JobRun, queue.job_id).state))
        finally:
            session.close()
        handle.add_rows(1)
        return oxbow_cli.EXIT_OK

    api_worker.run_stages(
        run_id=submission.run_id,
        stages=["ingest"],
        kind="pipeline",
        container=container,
        runners={"ingest": watching},
    )
    assert seen == [
        api_worker.JOB_STARTED
    ], f"the row was {seen} while the stage ran, not 'started': the transition the UI polls"

    session = container.new_session()
    try:
        states = {str(row) for row in session.execute(select(JobRun.state)).scalars()}
    finally:
        session.close()
    assert states <= set(api_worker.OPEN_JOB_STATES) | set(api_worker.CLOSED_JOB_STATES), states


def test_a_stage_that_raises_lands_a_named_failure_not_a_hanging_running_row(
    warehouse: dict[str, Any], queue: _FakeQueue
) -> None:
    """The guarantee the stream depends on: a red stage is a closed run with a reason."""
    container = warehouse["container"]
    submission = _submit(container, queue, stages=("ingest", "graph"), kind="pipeline")

    with pytest.raises(RuntimeError, match="the graph build lost its corpus"):
        api_worker.run_stages(
            run_id=submission.run_id,
            stages=["ingest", "graph"],
            kind="pipeline",
            container=container,
            runners={
                "ingest": _ok_runner(9),
                "graph": _raising_runner("the graph build lost its corpus"),
            },
        )

    events = _ledger_details(container, submission.run_id)
    keyed = {(str(row["stage"]), str(row["status"])) for row in events}
    assert keyed == {
        ("ingest", "running"),
        ("ingest", "complete"),
        ("graph", "running"),
        ("graph", "failed"),
    }, keyed
    failed = [row for row in events if row["status"] == "failed"][-1]
    assert "RuntimeError" in str(failed["detail"])
    assert "the graph build lost its corpus" in str(failed["detail"])
    assert int(failed["elapsed_ms"]) >= 0
    assert int(failed["rows"]) == 1, "the rows a stage counted survive its failure"

    last_by_stage: dict[str, str] = {}
    for row in events:
        last_by_stage[str(row["stage"])] = str(row["status"])
    assert all(
        status != "running" for status in last_by_stage.values()
    ), f"a stage left open at 'running' would spin the progress UI forever: {last_by_stage}"

    run = _run_row(container, submission.run_id)
    assert run["state"] == RunState.FAILED.value
    assert "the graph build lost its corpus" in str(run["error"])

    job = _job_row(warehouse, queue.job_id)
    assert job.state == api_worker.JOB_FAILED
    assert job.finished_at is not None
    assert "RuntimeError" in str(job.error)


def test_a_nonzero_stage_exit_stops_the_chain_and_goes_red(
    warehouse: dict[str, Any], queue: _FakeQueue
) -> None:
    """A stage that returns a red code without raising is still a named failure.

    This is the branch ``cli._run_stage`` guards with "exited N without recording what
    failed", and the reason a red run cannot be reported as a clean job: a failed pipeline is
    a failed job, the way a red ``oxbow pipeline`` is a non-zero process.
    """
    container = warehouse["container"]
    submission = _submit(container, queue, stages=("ingest", "graph", "score"), kind="pipeline")

    with pytest.raises(api_worker.StageChainFailedError, match="graph exited 2"):
        api_worker.run_stages(
            run_id=submission.run_id,
            stages=["ingest", "graph", "score"],
            kind="pipeline",
            container=container,
            runners={
                "ingest": _ok_runner(2),
                "graph": _exiting_runner(2),
                "score": _ok_runner(1),
            },
        )

    ledger = _ledger(container, submission.run_id)
    assert [stage for stage, _, _ in ledger] == ["ingest", "ingest", "graph", "graph"]
    assert ledger[-1][1] == "failed"
    assert "score" not in [stage for stage, _, _ in ledger], (
        "the first non-zero exit stops the chain: a score over a corpus that failed to land "
        "is arithmetic on a fiction"
    )
    assert ledger[-2][1] == "running" and ledger[-2][0] == "graph"
    assert _run_row(container, submission.run_id)["state"] == RunState.FAILED.value
    assert _job_row(warehouse, queue.job_id).state == api_worker.JOB_FAILED


def test_a_real_stage_name_reaches_the_pipelines_own_body(
    warehouse: dict[str, Any], queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker calls ``oxbow.cli``'s stage function with the CLI's own defaults.

    Patching the pipeline's function does not fake the work away: what is asserted is that the
    worker reaches for *that* symbol with *those* arguments, which is the one thing a
    re-implementation inside the worker would get wrong and this test would catch.
    """
    seen: list[dict[str, Any]] = []

    def recorded(context: Any, handle: Any, **kwargs: Any) -> int:
        seen.append(dict(kwargs))
        assert context.run_id, "the stage body is handed the run this job owns"
        handle.add_rows(7)
        return oxbow_cli.EXIT_OK

    monkeypatch.setattr(oxbow_cli, "run_graph_stage", recorded)
    container = warehouse["container"]
    submission = _submit(container, queue, stages=("graph",), kind="pipeline")

    summary = api_worker.run_stages(
        run_id=submission.run_id,
        stages=["graph"],
        kind="pipeline",
        container=container,
    )

    assert seen == [{"source": [], "max_events": None}], (
        "these are pipeline_cmd's own defaults; restating them differently would be two ways "
        "to run one stage"
    )
    assert summary["stage_status"] == {"graph": "complete"}
    assert _ledger(container, submission.run_id)[-1] == ("graph", "complete", 7)


def test_a_declared_stage_with_no_runner_is_unavailable_not_complete(
    warehouse: dict[str, Any], queue: _FakeQueue
) -> None:
    """``warehouse`` is in ``PIPELINE_STAGES`` and implemented nowhere; the ledger says so.

    The honest row is ``unavailable`` naming the missing seam. ``complete`` with zero rows
    would be the lie ``stage_events.py`` exists to prevent, and ``failed`` would call an
    unwritten module a measurement that came out red.
    """
    container = warehouse["container"]
    submission = _submit(container, queue, stages=("warehouse",), kind="pipeline")

    summary = api_worker.run_stages(
        run_id=submission.run_id,
        stages=["warehouse"],
        kind="pipeline",
        container=container,
        runners={},  # nothing injected: this is the real dispatch
    )

    ledger = _ledger(container, submission.run_id)
    assert [status for _, status, _ in ledger] == ["running", "unavailable"], ledger
    detail = str(_ledger_details(container, submission.run_id)[-1]["detail"])
    assert "no runner is wired for stage 'warehouse'" in detail
    assert "oxbow.cli" in detail, "the row names the module the seam is missing from"
    assert summary["unavailable"] == ["warehouse"]
    assert summary["failed"] == []
    assert summary["run_state"] == RunState.COMPLETE.value


def test_unknown_kind_and_unknown_stage_are_refused_before_anything_runs(
    warehouse: dict[str, Any], queue: _FakeQueue
) -> None:
    """A job nobody could have submitted is named, not half-executed.

    Each refusal gets its own submission: a refused job closes its ``job_run`` row, and the
    next delivery of *that* job would correctly find the row closed and do nothing, which is a
    different assertion (and tested as one below).
    """
    container = warehouse["container"]

    empty = _submit(container, queue, stages=("ingest",), kind="pipeline")
    with pytest.raises(api_worker.UnknownStageError, match="no stages"):
        api_worker.run_stages(
            run_id=empty.run_id,
            stages=[],
            kind="pipeline",
            container=container,
            runners={"ingest": _ok_runner()},
        )
    assert _ledger(container, empty.run_id) == []
    assert _job_row(warehouse, queue.job_id).state == api_worker.JOB_FAILED
    assert (
        _run_row(container, empty.run_id)["state"] == RunState.FAILED.value
    ), "a refused job must not leave its run looking live to the stream"

    bad_kind = _submit(container, queue, stages=("ingest",), kind="pipeline")
    with pytest.raises(api_worker.UnknownJobKindError, match="kind 'nonsense'"):
        api_worker.run_stages(
            run_id=bad_kind.run_id,
            stages=["ingest"],
            kind="nonsense",
            container=container,
            runners={"ingest": _ok_runner()},
        )
    assert _ledger(container, bad_kind.run_id) == [], "refused before a row was written"

    bad_stage = _submit(container, queue, stages=("ingest",), kind="pipeline")
    with pytest.raises(api_worker.UnknownStageError, match="outside the ledger"):
        api_worker.run_stages(
            run_id=bad_stage.run_id,
            stages=["teleport"],
            kind="pipeline",
            container=container,
            runners={"ingest": _ok_runner()},
        )
    assert _ledger(container, bad_stage.run_id) == []


# --- re-delivery, and the crash the docs promise is survivable --------------


def test_a_job_redelivered_after_its_verdict_writes_nothing(
    warehouse: dict[str, Any], queue: _FakeQueue
) -> None:
    """The second delivery of a finished job is a no-op, not a second run."""
    container = warehouse["container"]
    submission = _submit(container, queue, stages=("ingest", "graph"), kind="pipeline")
    runners = {"ingest": _ok_runner(4), "graph": _ok_runner(6)}
    api_worker.run_stages(
        run_id=submission.run_id,
        stages=["ingest", "graph"],
        kind="pipeline",
        container=container,
        runners=runners,
    )
    first = _ledger(container, submission.run_id)
    assert first[-1] == ("graph", "complete", 6)

    replayed = api_worker.run_stages(
        run_id=submission.run_id,
        stages=["ingest", "graph"],
        kind="pipeline",
        container=container,
        runners=runners,
    )
    assert replayed["ledger"] == "replayed"
    assert replayed["run_state"] == RunState.COMPLETE.value
    assert replayed["stage_status"] == {
        "ingest": "complete",
        "graph": "complete",
    }, "the no-op echoes the recorded verdict rather than an invented one"
    assert (
        _ledger(container, submission.run_id) == first
    ), "a no-op delivery still wrote ledger rows"
    assert _job_row(warehouse, queue.job_id).state == api_worker.JOB_FINISHED


def _abort_after_first_stage(warehouse: dict[str, Any], run_id: str) -> None:
    """Leave the state a ``SIGKILL`` between two stages leaves: one stage recorded, no verdict.

    Built from the same pieces :func:`_drive` uses and stopped before its ``finish()`` call,
    rather than by rewinding a completed run -- migration 0002's ``trg_run_immutable`` refuses
    exactly that, and a test that had to fight the trigger to set up would be testing the
    fixture instead of the worker.
    """
    container = warehouse["container"]
    with api_worker.open_stage_run(container, kind="pipeline", run_id=run_id, dry_run=False) as run:
        code = oxbow_cli._execute(run.context, "ingest", _ok_runner(4), terminal=False)
    assert code == oxbow_cli.EXIT_OK


def test_a_delivery_resumes_the_run_an_aborted_delivery_left_open(
    warehouse: dict[str, Any], queue: _FakeQueue
) -> None:
    """A re-delivered job resumes the run row it already opened and skips the finished stage.

    This is the idempotency the run row exists to make possible: no second run, no re-emitted
    stage, and the stage that never ran still runs.
    """
    container = warehouse["container"]
    submission = _submit(container, queue, stages=("ingest", "graph"), kind="pipeline")
    _abort_after_first_stage(warehouse, submission.run_id)

    assert _ledger(container, submission.run_id) == [
        ("ingest", "running", 0),
        ("ingest", "complete", 4),
    ]
    assert _run_row(container, submission.run_id)["state"] == RunState.RUNNING.value

    summary = api_worker.run_stages(
        run_id=submission.run_id,
        stages=["ingest", "graph"],
        kind="pipeline",
        container=container,
        runners={"ingest": _ok_runner(99), "graph": _ok_runner(6)},
    )
    after = _ledger(container, submission.run_id)
    assert len([row for row in after if row[0] == "ingest"]) == 2, (
        "ingest already reported complete under this run; running it again is a second run "
        f"pretending to be one: {after}"
    )
    assert after[-1] == ("graph", "complete", 6), "the unfinished stage still has to run"
    assert summary["ledger"] == "resumed"
    assert _run_row(container, submission.run_id)["state"] == RunState.COMPLETE.value
    assert _job_row(warehouse, queue.job_id).state == api_worker.JOB_FINISHED


def test_a_completed_run_is_not_executed_by_a_later_delivery(
    warehouse: dict[str, Any], queue: _FakeQueue
) -> None:
    """A redelivered job whose run already froze is named, not failed over.

    ``ensure_run_open`` raises on an immutable run; letting that reach the failure path would
    write ``failed`` over work an earlier delivery finished, and the queue would report a red
    job that never was.
    """
    container = warehouse["container"]
    submission = _submit(container, queue, stages=("ingest",), kind="pipeline")
    api_worker.run_stages(
        run_id=submission.run_id,
        stages=["ingest"],
        kind="pipeline",
        container=container,
        runners={"ingest": _ok_runner(2)},
    )
    rows = _ledger(container, submission.run_id)

    # The job row is open again -- as it would be if Redis re-pushed a finished job -- while
    # the run itself is frozen. That delivery must not touch the ledger.
    session = container.new_session()
    try:
        job = session.get(JobRun, queue.job_id)
        job.state = api_worker.JOB_QUEUED
        job.result = None
        job.finished_at = None
        session.commit()
    finally:
        session.close()

    with pytest.raises(api_worker.JobAlreadyRecordedError, match="immutable"):
        api_worker.run_stages(
            run_id=submission.run_id,
            stages=["ingest"],
            kind="pipeline",
            container=container,
            runners={"ingest": _ok_runner(2)},
        )
    assert _ledger(container, submission.run_id) == rows
    refused = _job_row(warehouse, queue.job_id)
    assert refused.state != api_worker.JOB_FAILED and refused.error is None, (
        "a delivery that declined to touch an immutable ledger is not this job's failure: "
        f"state={refused.state!r} error={refused.error!r}"
    )
    assert (
        _run_row(container, submission.run_id)["state"] == RunState.COMPLETE.value
    ), "the first delivery's verdict survives the second one"


def test_the_orphan_sweep_closes_a_run_a_dead_worker_left_running(
    warehouse: dict[str, Any],
) -> None:
    """The SIGKILL case: no ``except`` block ran, so the next worker start asks the queue."""
    container = warehouse["container"]
    run_id = _abandoned_run(warehouse, job_id="p7w-orphan-1", kind="pipeline", created_at=_ago(5))

    # ``""`` is the probe's answer for "there is no such job": evidence, not a shrug.
    swept = api_worker.reclaim_orphaned(container, now=_ago(0), probe=lambda _job_id: "")

    assert "p7w-orphan-1" in [item.job_id for item in swept], swept
    found = next(item for item in swept if item.job_id == "p7w-orphan-1")
    assert found.run_id == run_id
    assert found.prior_state == api_worker.JOB_STARTED
    assert "abandoned" in found.reason and "no such job" in found.reason
    job = _job_row(warehouse, "p7w-orphan-1")
    assert job.state == api_worker.JOB_FAILED
    assert job.finished_at is not None
    assert "abandoned" in str(job.error)
    run = _run_state_raw(warehouse, run_id)
    assert run.state == RunState.FAILED.value
    assert "abandoned" in str(run.error), "otherwise the stream for this run never closes"


def test_the_orphan_sweep_refuses_to_guess_when_the_queue_cannot_answer(
    warehouse: dict[str, Any],
) -> None:
    """An unreachable Redis is not evidence that a job died, so nothing whatever is written."""
    container = warehouse["container"]
    run_id = _abandoned_run(warehouse, job_id="p7w-orphan-2", kind="backtest", created_at=_ago(5))

    # The row is old enough to be eligible; the queue simply cannot be asked. That is the
    # difference between this test and the one above, and it is the one that keeps the sweep
    # from failing live jobs during a Redis outage.
    swept = api_worker.reclaim_orphaned(container, now=_ago(0), probe=lambda _job_id: None)

    assert swept == []
    assert _job_row(warehouse, "p7w-orphan-2").state == api_worker.JOB_STARTED
    assert _run_state_raw(warehouse, run_id).state == RunState.RUNNING.value


def test_the_orphan_sweep_leaves_a_live_job_and_a_young_row_alone(
    warehouse: dict[str, Any],
) -> None:
    """Both guards, in one pass: another worker's job, and a row inside the grace window."""
    container = warehouse["container"]
    live = _abandoned_run(
        warehouse, job_id="p7w-live-1", kind="pipeline", created_at=_ago(3), state="started"
    )
    young = _abandoned_run(
        warehouse, job_id="p7w-young-1", kind="pipeline", created_at=_ago(0), state="started"
    )

    def probe(job_id: str) -> str | None:
        # The live row's job is genuinely executing; the young one's is gone, but it is too
        # recent to conclude anything from.
        return "started" if job_id == "p7w-live-1" else ""

    swept = api_worker.reclaim_orphaned(container, now=_ago(0), probe=probe)
    ids = [item.job_id for item in swept]

    assert (
        "p7w-live-1" not in ids
    ), f"a job the queue still reports as started was declared dead: {ids}"
    assert "p7w-young-1" not in ids, (
        f"a row younger than the grace window belongs to a worker that may still be inside "
        f"the stage: {ids}"
    )
    assert _run_state_raw(warehouse, live).state == RunState.RUNNING.value
    assert _run_state_raw(warehouse, young).state == RunState.RUNNING.value
    assert _job_row(warehouse, "p7w-young-1").state == api_worker.JOB_STARTED


def test_a_job_sweeps_the_rows_a_killed_horse_left_behind(
    warehouse: dict[str, Any], queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RQ forks a horse per job; the kernel can kill *it* while the worker carries on.

    The orphan sweep used to run only at process start, so the common failure measured on
    this stack -- two `OSError: [Errno 12] Cannot allocate memory` rows sitting in
    `job_run` -- left its run `running` until the next deploy, which is the one row shape
    this module exists to prevent. Every job now runs the sweep before it begins.

    Asserted both ways: an orphan older than the grace closes as a side effect of an
    unrelated job starting, and the job that happened to start is untouched by it.
    """
    container = warehouse["container"]
    orphan = _abandoned_run(
        warehouse, job_id="p7w-horse-killed", kind="pipeline", created_at=_ago(5)
    )
    # `""` is the probe's evidence for "there is no such job".
    monkeypatch.setattr(api_worker, "_rq_state_probe", lambda _c: (lambda _job_id: ""))

    submission = _submit(container, queue, stages=("ingest",), kind="pipeline")
    api_worker.run_stages(
        run_id=submission.run_id,
        stages=["ingest"],
        kind="pipeline",
        container=container,
        runners={"ingest": _ok_runner(3)},
    )

    dead = _run_row(container, orphan)
    assert dead["state"] == RunState.FAILED.value, (
        f"the horse's run is still {dead['state']!r} after another job ran the sweep"
    )
    assert "abandoned" in str(dead["error"])
    assert _job_row(warehouse, "p7w-horse-killed").state == api_worker.JOB_FAILED
    live = _run_row(container, submission.run_id)
    assert live["state"] == RunState.COMPLETE.value, (
        "the sweep condemned the job that ran it: its own row is younger than the grace, "
        "and that is the only thing standing between this and a worker that eats itself"
    )


def _abandoned_run(
    warehouse: dict[str, Any],
    *,
    job_id: str,
    kind: str,
    created_at: datetime,
    state: str = api_worker.JOB_STARTED,
) -> str:
    """A run left in ``running`` with an open ``job_run`` row behind it, aged on purpose."""
    container = warehouse["container"]
    run_id = new_run_id()
    session = container.new_session()
    try:
        session.add(
            Run(
                run_id=run_id,
                state=RunState.RUNNING.value,
                seed=1337,
                timezone="UTC",
                provenance="pipeline",
                config_hash="0" * 64,
                model_version=f"pending:{kind}",
            )
        )
        # Flushed before the job row, exactly as ``submit_job`` does: these two tables have a
        # foreign key and no ORM relationship between them, so nothing orders the inserts.
        session.flush()
        session.add(
            JobRun(
                job_id=job_id,
                kind=kind,
                run_id=run_id,
                queue=api_jobs.QUEUE_NAME,
                state=state,
                created_at=created_at,
            )
        )
        session.commit()
    finally:
        session.close()
    return run_id


# --- the null-file deployment: plan §13's "with nothing else up" -------------


def test_the_worker_runs_a_job_on_the_null_file_container_with_nothing_up(
    null_container: Any,
) -> None:
    """Files in, files out: the ledger and the run row land where that read model looks."""
    container = null_container
    run_id = new_run_id()

    summary = api_worker.run_stages(
        run_id=run_id,
        stages=["warehouse"],
        kind="pipeline",
        container=container,
        runners={},
    )

    assert summary["ledger"] == "opened", "no submit_job ran, so this delivery opens the run"
    assert [status for _, status, _ in _null_ledger(container, run_id)] == [
        "running",
        "unavailable",
    ]
    # The run row is visible to the deployment's own read model; the stage rows are checked
    # where the null sink writes them (see ``_null_ledger`` for why those two differ).
    assert _run_row(container, run_id)["state"] == RunState.COMPLETE.value
    assert _null_run_record(container, run_id)["state"] == RunState.COMPLETE.value
    ledger_file = container.settings.repo_root / "out" / "warehouse" / "stage_events.jsonl"
    assert ledger_file.is_file(), "the null warehouse is the record on this backend"
    assert run_id in ledger_file.read_text(encoding="utf-8")


def test_the_worker_records_nothing_for_a_dry_run(null_container: Any) -> None:
    """Nothing was measured, so nothing may be recorded -- ``cli._run_stage``'s rule."""
    container = null_container
    run_id = new_run_id()

    summary = api_worker.run_stages(
        run_id=run_id,
        stages=["ingest"],
        kind="pipeline",
        container=container,
        runners={"ingest": _ok_runner(1000)},
        dry_run=True,
    )

    assert summary["ledger"] == "dry-run"
    assert summary["stages_attempted"] == ["ingest"]
    assert summary["events_emitted"] == 0
    with pytest.raises(RunNotFound):
        container.read_model.run_row(run_id)
    ledger_file = container.settings.repo_root / "out" / "warehouse" / "stage_events.jsonl"
    assert not ledger_file.exists() or run_id not in ledger_file.read_text(encoding="utf-8")


def test_the_null_file_worker_closes_a_run_whose_stage_raised(null_container: Any) -> None:
    """The same crash discipline on the backend with no ``job_run`` table to write to."""
    container = null_container
    run_id = new_run_id()

    with pytest.raises(RuntimeError, match="the ingest batch vanished"):
        api_worker.run_stages(
            run_id=run_id,
            stages=["ingest"],
            kind="pipeline",
            container=container,
            runners={"ingest": _raising_runner("the ingest batch vanished")},
        )

    assert [status for _, status, _ in _null_ledger(container, run_id)] == ["running", "failed"]
    assert _run_row(container, run_id)["state"] == RunState.FAILED.value
    assert "the ingest batch vanished" in str(_null_run_record(container, run_id)["error"])


# --- the process entry and the drain seam -----------------------------------


def test_main_refuses_to_start_a_null_file_worker(
    null_container: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python apps/api/worker.py`` on a null-file deployment exits 2, naming the setting.

    ``apps/api/jobs.py`` refuses to *submit* a job without a write path, so a worker that
    would execute such jobs anyway is the same refusal one step later: it must say so at
    startup rather than idle against a queue nothing can ever push to. Output is read from the
    process's own stream because ``configure_logging`` caches structlog's processor chain on
    first use, which makes ``capture_logs`` blind to a logger this module bound at import.
    """
    assert null_container.backend == "null-file"
    code = api_worker.main([])
    printed = capsys.readouterr()
    text = printed.out + printed.err

    assert code == 2, (
        "a null-file worker started anyway; it has no job_run table and nothing can enqueue "
        "a job to run on it"
    )
    assert "will not start" in text, text[-2000:]
    assert "DATABASE_URL" in text, "the message must name the thing to fix"
    assert "null-file" in text
    assert (
        os.environ["RUN_SALT"] not in text
    ), "the run salt must never reach a log line, at any length or in any digest form"


def test_the_compose_command_names_this_module() -> None:
    """``docker-compose.yml`` runs ``python apps/api/worker.py``; that file *is* ``api.worker``.

    The gap this module closes started as a Compose command pointing at a file that did not
    exist, which no Python test could have noticed. The same is asserted for the image each of
    the three application services builds from.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "apps/api/worker.py" in compose
    assert Path(api_worker.__file__).resolve() == (REPO_ROOT / "apps" / "api" / "worker.py")

    for dockerfile in (
        REPO_ROOT / "apps" / "api" / "Dockerfile",
        REPO_ROOT / "apps" / "web" / "Dockerfile",
    ):
        assert dockerfile.is_file(), f"compose builds from {dockerfile}, which does not exist"
        text = dockerfile.read_text(encoding="utf-8")
        unpinned = [
            line
            for line in text.splitlines()
            if line.lower().startswith("from ") and "@sha256:" not in line
        ]
        assert not unpinned, f"FROM lines must be digest-pinned (02 §F): {unpinned}"


def test_drain_outbox_reports_nothing_to_drain_on_a_null_file_deployment(
    null_container: Any,
) -> None:
    """``api/outbox.py`` says a worker drains it; on this backend it says why it cannot."""
    assert api_worker.drain_outbox(container=null_container, reschedule=False) == {
        "drained": False,
        "reason": "no write path",
    }


def test_the_drain_calls_the_outbox_module_and_keeps_only_counters(
    warehouse: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker delegates: the ladder, the signing and the dead-lettering are ``outbox``'s."""

    def fake(container: Any, **kwargs: Any) -> Any:
        assert container is warehouse["container"], "the drain runs on this process's container"
        return api_outbox.DrainReport(
            started_at=datetime.now(UTC), claimed=3, sent=2, retrying=1, dead=0
        )

    monkeypatch.setattr(api_outbox, "drain_once", fake)
    report = api_worker.drain_outbox(container=warehouse["container"], reschedule=False)
    assert report == {
        "drained": True,
        "claimed": 3,
        "sent": 2,
        "retrying": 1,
        "dead": 0,
        "rejected_permanently": 0,
    }
    assert "outcomes" not in report, "per-row detail belongs to the outbox ledger, not job_run"
    assert _no_floats(report)
