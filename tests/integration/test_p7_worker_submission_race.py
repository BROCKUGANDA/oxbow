"""P7 — the two interleavings between a submission and the worker that serves it.

Two of the three findings against ``apps/api/jobs.py`` and ``apps/api/worker.py`` are gated
here, and both are *interleaving* claims:

* **a submission must be visible before it is promised.** ``submit_job`` used to ``flush()``
  the ``run`` row, enqueue, and only then commit. A worker that ``brpop``s the job inside that
  window reads through its own transaction, so the run it was told to work on does not exist
  for it: it tries to open the run itself and takes a unique violation on the INSERT still in
  flight behind the submission, and its ``job_run`` lookup misses, so every state it books is
  dropped and the row sits ``queued`` behind a run that finished.
* **the orphan sweep must measure quiet from a heartbeat.** Age from ``created_at`` puts a job
  that is merely *long* into the candidate set while it works — a 500k-row score did not
  finish in 1 h 47 m on this host — and the queue still answers ``started`` for a job whose
  worker was killed, so a verdict that waits on RQ filing the job as failed waits on a
  maintenance cycle behind a lock a dead worker can hold for 899 s.

Both are driven deterministically: the queue hands its own id to a callback that runs while the
enqueue is in flight, which *is* the race, and the sweep is handed a fake clock and a fake
heartbeat. **Nothing in this file sleeps**; a test that sleeps is not a gate, it is a coin toss.
The database is the real Compose Postgres, because ``job_run``'s primary key, the ``run``
foreign key and the visibility of a committed row across transactions are properties of that
database and of no substitute.

The third finding — a drain appointment lost to a failing pass, and the queue it was lost in —
is in ``tests/unit/test_p7_worker_drain_calendar.py``.

Secrets are explicit test values injected through the environment; the repository's
``RUN_SALT`` is never read, and no secret value is printed.
"""

from __future__ import annotations

import secrets
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Imported before ``api.jobs`` (which reaches pyarrow through ``oxbow.adapters.io``) for the
# same reason ``apps/api/worker.py`` and ``tests/conftest.py`` do it: osqp's native algebra
# probe faults the process when pyarrow is already resident.
import osqp  # noqa: F401 -- imported for the native load it performs, not for the name
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]

# Served as ``python apps/api/worker.py`` and enqueued as ``api.worker.*``, so the package name
# has to resolve from ``apps`` -- the same bootstrap the worker's own suite performs, and the
# reason the imports below it are not at the top of the file. They must stay *after* the loop:
# the order is what keeps osqp's native probe ahead of pyarrow, and an import sorter that joins
# the two blocks silently un-does it.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api import jobs as api_jobs  # noqa: E402
from api import worker as api_worker  # noqa: E402
from api.schemas.health import JobStatus  # noqa: E402

# The P7 worker environment is reused rather than restated so a second, looser copy of "what a
# P7 test environment means" cannot exist.
from oxbow.adapters.warehouse.models import JobRun, StageEvent  # noqa: E402
from oxbow.ports.warehouse import RunState  # noqa: E402
from tests.integration.test_p7_worker import (  # noqa: E402
    _abandoned_run,
    _job_row,
    _ok_runner,
    _run_state_raw,
    _submit,
)
from tests.integration.test_p7_worker import (  # noqa: E402
    warehouse as _scratch_warehouse,
)

#: The scratch Postgres the worker's own suite provisions and migrates, asked for by the name
#: this module's tests take it under. Bound rather than imported under that name because a
#: module-level import of a fixture name collides with the parameter every test has to take.
warehouse = _scratch_warehouse

#: Every row this module's sweep tests open is keyed with this prefix, which is what lets the
#: autouse clean-up below tell them from the finding-1 tests' own rows.
ROW_PREFIX = "p7r-"


@pytest.fixture(autouse=True)
def _swept_rows_do_not_leak() -> Iterator[None]:
    """Delete this module's open rows after each test, so a sweep never reads another's.

    :func:`reclaim_orphaned` is a whole-table statement — it sweeps every open row it is asked
    about — and these tests hand it a fake queue that answers the same way for every job id.
    Without this, the third sweep test condemns the first one's still-live row and the failure
    says nothing about the code under test. Scoped to the prefix this module writes, on the
    scratch database this module provisions.
    """
    yield
    from sqlalchemy import delete

    container = _open_container()
    session = container.new_session()
    try:
        job_ids = [
            str(row)
            for row in session.execute(
                select(JobRun.job_id).where(JobRun.job_id.like(f"{ROW_PREFIX}%"))
            ).scalars()
        ]
        run_ids = [
            str(row)
            for row in session.execute(
                select(JobRun.run_id).where(JobRun.job_id.like(f"{ROW_PREFIX}%"))
            ).scalars()
            if row
        ]
        if job_ids:
            session.execute(delete(JobRun).where(JobRun.job_id.in_(job_ids)))
        if run_ids:
            session.execute(delete(StageEvent).where(StageEvent.run_id.in_(run_ids)))
            # The `run` rows are deliberately left. Deleting one raises
            # `psycopg.errors.CheckViolation: run … is complete: its lifecycle state is frozen
            # (plan §13)` — that guard is the point, not an obstacle, and a cleanup that
            # worked around it would be testing past the rule. It is also unnecessary:
            # `reclaim_orphaned` takes its candidates from `job_run`, so removing the job rows
            # above is what stops a finished test from being swept by the next one.
        session.commit()
    finally:
        session.close()


def _open_container() -> Any:
    """The container the scratch database this module is pointed at is on."""
    from api.deps import build_container

    return build_container()


KIND = "pipeline"
STAGES = ("ingest", "graph")


class _RacingQueue:
    """A queue that hands its job id to a callback *while the enqueue is in flight*.

    That is the whole device: a real ``Queue.enqueue`` returns only after the job is sitting in
    Redis, and a worker blocked in ``brpop`` can have claimed it before this call returns, so
    a test that wants the interleaving has to run the worker from in here. ``refuse`` covers
    the other half of the submission's failure story — Redis answering something other than a
    job — and ``enqueues`` counts the calls, because "the stages ran once" and "the stages ran
    twice, the second time by an unrelated submission" look the same in the ledger otherwise.
    """

    def __init__(self) -> None:
        self.on_enqueue: Any = None
        self.before_enqueue: BaseException | None = None
        self.refuse: BaseException | None = None
        self.enqueues = 0
        self.job_ids: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    @property
    def job_id(self) -> str:
        return self.job_ids[-1]

    def enqueue(self, _name: str, **kwargs: Any) -> Any:
        self.enqueues += 1
        if self.before_enqueue is not None:
            raise self.before_enqueue
        if self.refuse is not None:
            raise self.refuse
        job_id = f"p7r-{secrets.token_hex(8)}"
        self.job_ids.append(job_id)
        self.kwargs.append(dict(kwargs))
        if self.on_enqueue is not None:
            self.on_enqueue(job_id)
        return _Job(job_id)

    def get_job_ids(self) -> list[str]:
        return list(self.job_ids)


class _Job:
    def __init__(self, job_id: str) -> None:
        self.id = job_id


@pytest.fixture()
def racing(monkeypatch: pytest.MonkeyPatch) -> _RacingQueue:
    """``api.jobs._queue`` swapped for the recorder, as the worker suite does for its own."""
    fake = _RacingQueue()
    monkeypatch.setattr(api_jobs, "_queue", lambda container: fake)
    return fake


def _states_of_job_rows(warehouse: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Every ``job_run`` row as ``job_id -> (state, run_id)``: the sweep's whole input set."""
    session = warehouse["container"].new_session()
    try:
        return {
            str(row.job_id): (str(row.state), str(row.run_id))
            for row in session.execute(select(JobRun)).scalars()
        }
    finally:
        session.close()


# --- finding 1: nothing may be enqueueable before the record of it is committed -----------


def test_a_worker_that_dequeues_immediately_sees_the_run_and_the_row_behind_it(
    warehouse: dict[str, Any], racing: _RacingQueue
) -> None:
    """The hand-off driven at its worst moment: the worker starts inside the enqueue.

    Before the fix this failed both ways the audit named: the worker's ``run_stages`` died on a
    unique violation for the ``run`` row the submission had flushed but not committed, and its
    ``job_run`` lookup missed, so the ``started``/``finished`` it booked were dropped and the
    row stayed ``queued`` behind a run that had already gone green.
    """
    container = warehouse["container"]
    captured: dict[str, str] = {}

    def claimed(_job_id: str) -> None:
        api_worker.run_stages(
            run_id=captured["run_id"],
            stages=list(STAGES),
            kind=KIND,
            container=container,
            runners={stage: _ok_runner(2) for stage in STAGES},
        )

    racing.on_enqueue = claimed
    # ``submit_job`` mints the run id before it enqueues, so the callback has to learn it from
    # the enqueue's own kwargs rather than from a return value that does not exist yet.
    racing.kwargs = []
    submission = _run_submission_with_kwargs_capture(container, racing, captured)

    assert submission.run_id == captured["run_id"], "the callback ran a different run"
    # (1) The delivery that happened *during* the submission finished its stages cleanly.
    assert _run_state_raw(warehouse, submission.run_id).state == RunState.COMPLETE.value
    assert racing.enqueues == 1, "the stage bodies must not have run behind a second enqueue"

    # (2) The key the queue answered with is the key the row carries, so the handle the client
    # was given resolves. A row left under the submission's placeholder is the same window.
    row = _job_row(warehouse, racing.job_id)
    assert row.run_id == submission.run_id
    assert str(row.job_id) == racing.job_id
    assert not str(row.job_id).startswith(api_jobs.PENDING_JOB_ID_PREFIX)
    assert _job_row(warehouse, racing.job_id).state == api_worker.JOB_FINISHED

    # (3) The row the client holds renders through the API's own schema — the read path that
    # 404s if the re-key did not happen, and 404s forever if it happened wrongly.
    rendered = JobStatus.model_validate(
        {
            "job_id": str(row.job_id),
            "kind": str(row.kind),
            "run_id": row.run_id,
            "queue": str(row.queue),
            "state": str(row.state),
            "created_at": row.created_at,
            "finished_at": row.finished_at,
            "result": row.result,
            "error": row.error,
            "trace_id": row.trace_id,
        }
    )
    assert rendered.state == api_worker.JOB_FINISHED
    assert rendered.run_id == submission.run_id


def _run_submission_with_kwargs_capture(
    container: Any, racing: _RacingQueue, captured: dict[str, str]
) -> Any:
    """Submit with the worker wired to the claim, once the run id is known to both sides.

    The run id is minted inside ``submit_job``, so the callback can only be installed after it
    is known: the enqueue kwargs carry it, and the callback runs from inside that same call.
    """
    racing.on_enqueue = None

    def claim_from_kwargs(_job_id: str, **kwargs: Any) -> None:
        captured["run_id"] = str(kwargs["run_id"])
        api_worker.run_stages(
            run_id=captured["run_id"],
            stages=list(STAGES),
            kind=KIND,
            container=container,
            runners={stage: _ok_runner(2) for stage in STAGES},
        )

    class _QueueThatClaimsItself(_RacingQueue):
        def enqueue(self, name: str, **kwargs: Any) -> Any:  # type: ignore[override]
            job_id = f"p7r-{secrets.token_hex(8)}"
            self.enqueues += 1
            self.job_ids.append(job_id)
            self.kwargs.append(dict(kwargs))
            claim_from_kwargs(job_id, **kwargs)
            return _Job(job_id)

    live = _QueueThatClaimsItself()
    live.__dict__.update({k: v for k, v in racing.__dict__.items() if k != "enqueues"})
    import api.jobs as _jobs

    original = _jobs._queue
    _jobs._queue = lambda _container: live
    try:
        return _submit(container, live, stages=STAGES, kind=KIND)  # type: ignore[arg-type]
    finally:
        _jobs._queue = original
        racing.enqueues = live.enqueues
        racing.job_ids = live.job_ids
        racing.kwargs = live.kwargs


@pytest.mark.parametrize("stage", ["commit", "enqueue"])
def test_a_refused_submission_leaves_no_row_anyone_can_mistake_for_live(
    warehouse: dict[str, Any], stage: str
) -> None:
    """Failure is a state, never a silence, on the submission side too.

    The two ways a submission can die once the run row exists: the commit itself, and the
    enqueue refusing (Redis down, which is ``QueueUnavailable`` and nothing else). Both must
    leave a ``failed`` run carrying the reason and a closed ``job_run``, because a run in
    ``running`` with no job behind it is the one row shape this stack refuses to contain — the
    SSE stream for it holds to its 900 s budget on the strength of that row.
    """
    container = warehouse["container"]
    before = _states_of_job_rows(warehouse)
    queue = _RacingQueue()
    boom = RuntimeError("the injected refusal")
    if stage == "commit":
        queue.before_enqueue = boom
    else:
        queue.refuse = boom
    import api.jobs as _jobs

    original = _jobs._queue
    _jobs._queue = lambda _container: queue
    try:
        with pytest.raises(RuntimeError, match="injected refusal"):
            _submit(container, queue, stages=STAGES, kind=KIND)  # type: ignore[arg-type]
    finally:
        _jobs._queue = original

    after = _states_of_job_rows(warehouse)
    added = {job_id: state for job_id, state in after.items() if job_id not in before}
    assert added, "the refused submission wrote no job_run row, so it stored no reason either"
    assert all(state[0] == api_worker.JOB_FAILED for state in added.values()), added
    for job_id, (state, run_id) in added.items():
        row = _job_row(warehouse, job_id)
        assert row.finished_at is not None, job_id
        assert "failed" in str(row.error).lower(), row.error
        assert (
            _run_state_raw(warehouse, run_id).state == RunState.FAILED.value
        ), f"run {run_id} was left {state!r} by a submission that was refused"


def test_a_refusal_that_quotes_the_queue_url_does_not_put_the_url_in_a_column(
    warehouse: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason is stored, and the credential inside it is not.

    ``QueueUnavailable`` names the Redis URL it could not ping, and deployments put the AUTH in
    that URL. The refusal text belongs on the row — that is the difference between a failure
    and a silence — so it is stored redacted rather than dropped.
    """
    secret_url = "redis://user:sup3rsecret-value@127.0.0.1:6399/0"
    monkeypatch.setenv("REDIS_URL", secret_url)
    api_jobs.get_settings.cache_clear()

    def refuse(_container: Any) -> Any:
        raise api_jobs.QueueUnavailable(f"Redis at {secret_url} did not answer PING")

    monkeypatch.setattr(api_jobs, "_queue", refuse)
    before = _states_of_job_rows(warehouse)
    try:
        with pytest.raises(api_jobs.QueueUnavailable):
            _submit(
                warehouse["container"],
                _RacingQueue(),
                stages=STAGES,
                kind=KIND,  # type: ignore[arg-type]
            )
        added = {job_id for job_id in _states_of_job_rows(warehouse) if job_id not in before}
        session = warehouse["container"].new_session()
        try:
            reasons = [
                str(row)
                for row in session.execute(
                    select(JobRun.error).where(JobRun.job_id.in_(added))
                ).scalars()
                if row
            ]
        finally:
            session.close()
    finally:
        api_jobs.get_settings.cache_clear()
    assert reasons, "the refusal stored no reason on the row it closed"
    assert all(secret_url not in reason for reason in reasons), reasons
    assert any("***redis-url***" in reason for reason in reasons), reasons


# --- finding 2: the sweep's clock is the heartbeat, its evidence is not RQ's verdict -------


def test_a_live_job_in_a_long_stage_is_never_a_candidate(warehouse: dict[str, Any]) -> None:
    """A job three hours old and three seconds from beating is not abandoned.

    The row is older than the *submission* grace, which was the only condition the sweep had
    for a job the queue still reports as ``started``. Here the queue says ``started`` **and**
    the heartbeat is fresh, which is what the 500k-row score that did not finish in 1 h 47 m on
    this host actually looks like to a sweep.
    """
    container = warehouse["container"]
    run_id = _abandoned_run(
        warehouse,
        job_id="p7r-clock-1",
        kind=KIND,
        created_at=_minutes_ago(180),
        state="started",
    )

    swept = api_worker.reclaim_orphaned(
        container,
        now=datetime.now(UTC),
        probe=lambda _job_id: api_worker.JOB_STARTED,
        beats=lambda _job_id: _minutes_ago(0),
    )

    assert [item.job_id for item in swept] == [], swept
    assert _job_row(warehouse, "p7r-clock-1").state == api_worker.JOB_STARTED
    assert (
        _run_state_raw(warehouse, run_id).state == RunState.RUNNING.value
    ), "the stream for this run is being fed by the worker that is still inside the stage"


def test_a_run_whose_worker_stopped_beating_is_closed_without_rqs_verdict(
    warehouse: dict[str, Any],
) -> None:
    """The container-kill case, and why the sweep cannot wait for RQ to notice it.

    Measured against the Compose Redis on this host: killing the worker freezes
    ``last_heartbeat`` while the job hash keeps ``status=started``, and it is only
    ``Worker.clean_registries`` — its own 600 s cadence, behind a maintenance lock a dead
    worker can hold for 899 s — that ever files the job ``failed``. An answer of ``started`` is
    therefore not a live answer once the heartbeat is ten missed intervals old, and the row
    that carries it is closed on that evidence with the evidence in its text.
    """
    container = warehouse["container"]
    silence = timedelta(seconds=api_worker.HEARTBEAT_GRACE_SECONDS + 120)
    stopped_beating = datetime.now(UTC) - silence
    run_id = _abandoned_run(
        warehouse,
        job_id="p7r-kill-1",
        kind=KIND,
        created_at=_minutes_ago(40),
        state="started",
    )

    swept = api_worker.reclaim_orphaned(
        container,
        now=datetime.now(UTC),
        probe=lambda _job_id: api_worker.JOB_STARTED,
        beats=lambda _job_id: stopped_beating,
    )

    assert [item.job_id for item in swept] == ["p7r-kill-1"], swept
    found = swept[0]
    assert found.prior_state == api_worker.JOB_STARTED
    assert found.rq_state == api_worker.JOB_STARTED, "the queue never said this job was dead"
    assert "heartbeat" in found.reason, found.reason
    job = _job_row(warehouse, "p7r-kill-1")
    assert job.state == api_worker.JOB_FAILED
    assert job.finished_at is not None
    assert "heartbeat" in str(job.error), job.error
    run = _run_state_raw(warehouse, run_id)
    assert run.state == RunState.FAILED.value
    assert run.finished_at is not None, "otherwise the stream for this run never closes"


def test_a_heartbeat_fresher_than_the_row_is_the_one_that_counts(
    warehouse: dict[str, Any],
) -> None:
    """The other side of the same rule: three hours since the row, two minutes since it beat.

    That is a delivery resumed onto a row an earlier submission opened, and it is alive. A
    sweep that read the submission clock fails it; a sweep that read only the queue's answer
    and not the heartbeat has nothing to say about it either way.
    """
    container = warehouse["container"]
    _abandoned_run(
        warehouse,
        job_id="p7r-clock-2",
        kind=KIND,
        created_at=_minutes_ago(180),
        state="started",
    )

    swept = api_worker.reclaim_orphaned(
        container,
        now=datetime.now(UTC),
        probe=lambda _job_id: api_worker.JOB_STARTED,
        beats=lambda _job_id: _minutes_ago(2),
    )
    assert [item.job_id for item in swept] == [], swept
    assert _job_row(warehouse, "p7r-clock-2").state == api_worker.JOB_STARTED


def test_a_short_silence_and_a_silent_queue_are_both_refused(
    warehouse: dict[str, Any],
) -> None:
    """What the sweep still refuses, after the change that let it do more.

    Three rows, all older than the submission grace, all answered ``started``: one beat a
    minute ago, one beat a minute *before* the grace (a worker that may only be swapped), and
    one the queue cannot be asked about at all. The first two are left alone, and the third
    aborts the whole sweep with nothing written: an unreachable Redis is not evidence that a
    job died, and that rule outlives this change on purpose.
    """
    container = warehouse["container"]
    fresh = _abandoned_run(
        warehouse, job_id="p7r-refuse-fresh", kind=KIND, created_at=_minutes_ago(180)
    )
    almost = _abandoned_run(
        warehouse, job_id="p7r-refuse-almost", kind=KIND, created_at=_minutes_ago(180)
    )
    mute = _abandoned_run(
        warehouse, job_id="p7r-refuse-mute", kind=KIND, created_at=_minutes_ago(180)
    )
    almost_stale = timedelta(seconds=api_worker.HEARTBEAT_GRACE_SECONDS - 60)

    swept = api_worker.reclaim_orphaned(
        container,
        now=datetime.now(UTC),
        probe=lambda _job_id: api_worker.JOB_STARTED,
        beats=lambda job_id: (
            datetime.now(UTC) - almost_stale if job_id == "p7r-refuse-almost" else _minutes_ago(1)
        ),
    )
    assert [item.job_id for item in swept] == [], swept

    aborted = api_worker.reclaim_orphaned(
        container,
        now=datetime.now(UTC),
        # Compared against the JOB id, which is what the probe is handed. `mute` is the run
        # id `_abandoned_run` returned, and a run id never equals a job id, so the predicate
        # as first written was never true: every row answered `started`, all three were
        # condemned, and the test that names the abort rule proved nothing about it.
        probe=lambda job_id: (None if job_id == "p7r-refuse-mute" else api_worker.JOB_STARTED),
        beats=lambda _job_id: _minutes_ago(999),
    )
    assert aborted == [], "an unanswered question stopped nothing, so rows were written"
    for run_id, job_id in ((fresh, "p7r-refuse-fresh"), (almost, "p7r-refuse-almost")):
        assert _run_state_raw(warehouse, run_id).state == RunState.RUNNING.value, job_id
        assert _job_row(warehouse, job_id).state == api_worker.JOB_STARTED
    assert _run_state_raw(warehouse, mute).state == RunState.RUNNING.value
    assert _job_row(warehouse, "p7r-refuse-mute").state == api_worker.JOB_STARTED


# --- the shape of the row the re-key leaves behind -----------------------------------------


def test_the_row_handed_to_the_worker_is_keyed_on_the_queue_id(
    warehouse: dict[str, Any], racing: _RacingQueue
) -> None:
    """A calm submission, checked end to end: no placeholder left, and the row resolves.

    The re-key is the half of the fix that can silently do nothing: an ``UPDATE`` that matches
    no row leaves the record under ``pending:<run id>`` while the client holds an id that
    answers 404 forever. So the placeholder key is asserted *absent*, not merely the real key
    present.
    """
    container = warehouse["container"]
    submission = _submit(container, racing, stages=STAGES, kind=KIND)  # type: ignore[arg-type]

    assert (
        submission.job_id == racing.job_id
    ), "the client was handed a different id than the queue's"
    session = container.new_session()
    try:
        left = [
            str(row)
            for row in session.execute(
                select(JobRun.job_id).where(
                    JobRun.job_id.like(f"{api_jobs.PENDING_JOB_ID_PREFIX}%"),
                    # Scoped to this submission. A submission the queue refused keeps its
                    # placeholder key by design — there is no queue id to re-key to — and the
                    # other tests in this module perform exactly that refusal, so an unscoped
                    # predicate failed on their rows rather than on this one's.
                    JobRun.run_id == submission.run_id,
                )
            ).scalars()
        ]
        row = session.get(JobRun, submission.job_id)
        assert row is not None, f"no job_run row for {submission.job_id}"
        assert row.state == api_worker.JOB_QUEUED
        assert row.queue == api_jobs.QUEUE_NAME
        assert row.run_id == submission.run_id
        assert row.trace_id is None
        assert row.error is None
        assert row.finished_at is None
    finally:
        session.close()
    assert left == [], f"the submission left a row under its placeholder key: {left}"


def test_two_submissions_do_not_collide_on_the_placeholder_key(
    warehouse: dict[str, Any], racing: _RacingQueue
) -> None:
    """Why a placeholder key is safe where the removed comment said it could not be.

    ``jobs.py`` justified writing the row after the enqueue by the risk of two concurrent
    submissions colliding on a minted key. The key carries the run id, minted per submission,
    so the collision it feared cannot happen — which is what this asserts, rather than what the
    comment claimed.
    """
    container = warehouse["container"]
    first = _submit(container, racing, stages=STAGES, kind=KIND)  # type: ignore[arg-type]
    second = _submit(container, racing, stages=STAGES, kind=KIND)  # type: ignore[arg-type]

    assert first.job_id != second.job_id
    assert first.run_id != second.run_id
    session = container.new_session()
    try:
        found = {
            str(row.run_id): str(row.job_id)
            for row in session.execute(
                select(JobRun).where(JobRun.run_id.in_([first.run_id, second.run_id]))
            ).scalars()
        }
    finally:
        session.close()
    assert found == {first.run_id: first.job_id, second.run_id: second.job_id}
    # And the key is still a key: a second row for the same job id is refused by the database.
    with pytest.raises(IntegrityError):
        other = container.new_session()
        try:
            other.add(
                JobRun(
                    job_id=first.job_id,
                    kind=KIND,
                    run_id=second.run_id,
                    queue=api_jobs.QUEUE_NAME,
                    state="queued",
                )
            )
            other.commit()
        finally:
            other.close()


def _minutes_ago(minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)
