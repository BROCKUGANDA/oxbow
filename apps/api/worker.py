"""The RQ worker: the process that actually runs the jobs ``api.jobs`` promises.

Before this file, ``POST /api/jobs/pipeline`` wrote a ``run`` row in ``running``, pushed a
job onto Redis and answered with a stream to subscribe to — and nothing consumed it. The
run stayed ``running`` forever, the SSE stream polled a ledger with one row in it until its
900-second budget expired, and ``GET /api/jobs/{id}`` reported a job that could never
finish. The whole API was written for a worker that was never built. This is that worker,
and it is the consumer half of plan §13's "pipeline and backtest runs as jobs with live
progress".

Two rules shape every line here.

* **The worker does not implement a stage.** The stage bodies live in :mod:`oxbow.cli`,
  the pipeline's declared composition root, and :mod:`oxbow.stage_events` owns the ledger
  contract. What the worker adds is the sink: ``oxbow`` the CLI writes its ledger to
  ``out/warehouse/stage_events.jsonl``, while the API's read model reads the ``stage_event``
  table. That choice is the whole difference between a run anyone can see and a run nobody
  can, so the worker builds the same context the CLI builds, against
  ``container.warehouse_sink_factory()``, and calls the same helpers.
* **The worker may not remember anything that matters.** ``api/outbox.py`` says this about
  the drain and it holds here too: every transition is read from and written to Postgres, so
  a re-delivered job resumes the run row it already opened instead of opening a second one,
  and a worker killed mid-stage leaves a named failure rather than a ``running`` row — the
  exact shape ``api/jobs.py:120-130`` refuses to create on the submission side.

**Vocabulary, imported from whoever owns it and never restated.** ``run.state`` is
:class:`oxbow.ports.warehouse.RunState` (``running/complete/failed/superseded``);
``stage_event.status`` is the ledger's own ``running/complete/failed/unavailable``,
CHECK-enforced by migration 0001; ``job_run.state`` uses RQ's own job lifecycle
(:class:`rq.job.JobStatus`: ``queued -> started -> finished|failed``) so the row says exactly
what Redis would have said and ``api/jobs.py``'s ``_as_dict`` renders it without a new case.

**One declared stage has no implementation anywhere in the repository.**
``api/jobs.PIPELINE_STAGES`` names ``warehouse`` and ``STAGE_NAMES`` accepts it, but
``oxbow.cli.STAGES`` is ``ingest/graph/score/backtest`` and no code loads artifacts into the
warehouse tables under a run id. That stage is emitted ``unavailable`` with the missing seam
named — the ledger's own discipline, and the difference between an honest ledger and a
progress bar that lies.

Runs as ``python apps/api/worker.py`` (the Compose ``worker`` command, ``make worker``) and
is importable as ``api.worker`` for RQ's dotted-name resolution of the exact name
``api/jobs.py:103`` enqueues.
"""

# ruff: noqa: E402
# The sys.path bootstrap below has to run before the ``api.*`` imports it makes possible,
# which puts every one of them "not at top of file". Same reason pyproject.toml grants
# ``apps/api/main.py`` the exemption: a worker started by ``docker compose`` has the same
# constraint as the app.

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

# ``python apps/api/worker.py`` puts *this directory* on sys.path, which would leave the
# ``api`` package unimportable and make RQ's dotted-name lookup of ``api.worker`` fail in
# the very process that consumed the job. Same bootstrap, same argument as
# ``api/main.py:32-39``: one import style (``api.*``) in the app, the worker, the tests.
_APPS_DIR = str(Path(__file__).resolve().parents[1])
if _APPS_DIR not in sys.path:
    sys.path.insert(0, _APPS_DIR)

# ---------------------------------------------------------------------------
# NATIVE IMPORT-ORDER GUARD. Do not move this line and do not tidy it into a function: its
# whole value is that it runs here, before ``api.deps`` reaches ``oxbow.adapters.io`` and
# therefore pyarrow. ``oxbow.cli`` refuses to import when pyarrow was loaded first
# (``cli.py:41-112``, and ``tests/conftest.py`` for the test session) because resolving
# osqp's algebra backend with pyarrow resident segfaults the process — exit 139, no
# traceback. The worker imports ``oxbow.cli`` lazily, so the load that has to happen first
# has to happen at this point instead.
import osqp  # noqa: F401 -- imported for the native load it performs, not for the name
from redis import Redis
from rq import Queue, Worker, get_current_job
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from structlog.contextvars import clear_contextvars

from api.deps import Container, build_container
from api.jobs import JOB_KINDS, QUEUE_NAME
from api.observability import configure_logging, get_logger, new_trace_id, run_id_var, trace_id_var
from api.settings import get_settings
from oxbow.adapters.warehouse.models import JobRun, Run
from oxbow.ports.warehouse import (
    IMMUTABLE_RUN_STATES,
    RunState,
    WarehouseSink,
    WarehouseTableError,
    assert_run_id,
)
from oxbow.stage_events import StageEventEmitter, StageEventError, stage_vocabulary

if TYPE_CHECKING:  # pragma: no cover - annotations only, so the quant stack stays lazy
    from oxbow.cli import StageContext
    from oxbow.stage_events import StageHandle

logger = get_logger("oxbow.worker")

# --- the state vocabulary, borrowed from its owners --------------------------

# ``job_run.state`` mirrors RQ's own job states rather than inventing a parallel set: the
# row's whole purpose is to answer "what did we run" after Redis forgets, and a state the
# queue does not use would be a second, unverifiable account of the same job.
JOB_QUEUED: Final = JobStatus.QUEUED.value  # written by api/jobs.py at submission
JOB_STARTED: Final = JobStatus.STARTED.value
JOB_FINISHED: Final = JobStatus.FINISHED.value
JOB_FAILED: Final = JobStatus.FAILED.value
OPEN_JOB_STATES: Final = (JOB_QUEUED, JOB_STARTED)
CLOSED_JOB_STATES: Final = (JOB_FINISHED, JOB_FAILED)
#: RQ answers that mean "this job will never write again". ``""`` is the answer "there is
#: no such job", which is evidence of the same kind.
DEAD_RQ_STATES: Final = frozenset(
    {JOB_FINISHED, JOB_FAILED, JobStatus.STOPPED.value, JobStatus.CANCELED.value, ""}
)

#: How long a job may sit ``started`` before the startup sweep may call it dead. Deliberately
#: an order of magnitude above a full pipeline run: a false positive here fails a live job's
#: run underneath the worker holding it.
ABANDON_GRACE_SECONDS: Final = 7200

#: ``submit_job`` cannot know the model version before the model is imported, so it writes
#: ``pending:{kind}`` and the worker replaces it while the run is still open.
PENDING_VERSION_PREFIX: Final = "pending:"

#: How far ahead the next outbox drain is scheduled. The retry ladder's shortest rung is 1s
#: and every pass is stateless, so a minute is responsive without an empty queue dominating
#: Redis.
DRAIN_INTERVAL_SECONDS: Final = 60

#: A detail column is Text with a CHECK on nothing, but every producer in this repo truncates
#: to 900 (``stage_events.py``, ``pipeline_cmd``); one limit is one behaviour.
DETAIL_LIMIT: Final = 900


class UnknownJobKindError(ValueError):
    """A queued job names a kind this build has no stage set for."""


class UnknownStageError(ValueError):
    """A queued job names a stage the ledger's own vocabulary does not contain."""


class JobAlreadyRecordedError(RuntimeError):
    """A job arrived again after its run had already reached a verdict. Not a failure."""


class StageChainFailedError(RuntimeError):
    """The stages ran and the run they produced went red.

    Raised rather than returned so the job's own fate matches the run's: RQ files it in its
    failed registry, where an operator looks, and ``job_run`` records ``failed``. This is the
    worker's version of ``pipeline_cmd`` ending in ``raise typer.Exit(code=1)`` — a red
    pipeline is a non-zero process, not a clean exit that happens to mention a failure.
    """


# --- the sink the ledger writes through -------------------------------------


@dataclass(slots=True)
class DurableSink:
    """Wraps a :class:`WarehouseSink` so a ledger row is committed the moment it lands.

    ``PostgresWarehouseSink`` takes a caller-owned session and only ever ``flush()``es, so
    inside one transaction an event is invisible to every other connection. Correct for the
    CLI — one process, one transaction — and wrong for a worker: the SSE subscriber is a
    *different* connection in a different transaction, and a progress stream that cannot see
    progress until the job ends is precisely the lying progress bar plan §13 forbids. So
    every call that opens a transaction ends it again, reads included: an idle read
    transaction parked across a forty-second stage would pin the snapshot the stage runs
    inside and keep the next ``stage_events()`` from seeing the row the last stage wrote.

    A write that fails is retried once after a rollback, because the row a *failed* stage
    writes is the one row that must not be lost to the exception that is already in flight.
    """

    inner: WarehouseSink
    session: Session | None

    @property
    def sink_id(self) -> str:
        return self.inner.sink_id

    def _land(self, write: Callable[[], Any]) -> Any:
        """Run one sink write and commit it, retrying once on a poisoned session."""
        if self.session is None:
            return write()
        try:
            value = write()
            self.session.commit()
            return value
        except SQLAlchemyError:
            self.session.rollback()
            value = write()
            self.session.commit()
            return value

    def open_run(
        self,
        run_id: str,
        *,
        seed: int,
        timezone: str,
        provenance: str,
        config_hash: str,
        model_version: str,
        dataset_ref: str | None = None,
    ) -> None:
        self._land(
            lambda: self.inner.open_run(
                run_id,
                seed=seed,
                timezone=timezone,
                provenance=provenance,
                config_hash=config_hash,
                model_version=model_version,
                dataset_ref=dataset_ref,
            )
        )

    def run_state(self, run_id: str) -> RunState:
        return self._land(lambda: self.inner.run_state(run_id))

    def write(self, table: str, run_id: str, rows: Sequence[Mapping[str, Any]]) -> int:
        return self._land(lambda: self.inner.write(table, run_id, rows))

    def record_stage_event(
        self,
        run_id: str,
        *,
        stage: str,
        status: str,
        rows: int,
        elapsed_ms: int,
        detail: str | None = None,
    ) -> int:
        return self._land(
            lambda: self.inner.record_stage_event(
                run_id,
                stage=stage,
                status=status,
                rows=rows,
                elapsed_ms=elapsed_ms,
                detail=detail,
            )
        )

    def stage_events(self, run_id: str, *, after_id: int = 0) -> Sequence[Mapping[str, Any]]:
        return self._land(lambda: self.inner.stage_events(run_id, after_id=after_id))

    def complete_run(self, run_id: str, state: RunState, *, error: str | None = None) -> None:
        self._land(lambda: self.inner.complete_run(run_id, state, error=error))


# --- job_run bookkeeping ----------------------------------------------------


@dataclass(slots=True)
class JobLedger:
    """This job's ``job_run`` row, moved through RQ's own states.

    ``api/jobs.py`` writes the row before the job exists anywhere else, keyed on the queue's
    own job id, so the worker *finds* it rather than creating one. Outside a worker — a
    test, or an operator running a stage by hand — there is no RQ job to name, so the row is
    located by ``run_id``, which ``submit_job`` mints fresh per submission.

    A missing row is not an error: the run ledger is the durable record and a hand-run job
    has no submission behind it. A null-file container has no ``job_run`` table at all and
    says so once instead of failing every stage on the way past.
    """

    container: Container
    run_id: str
    kind: str
    job_id: str | None = None
    session: Session | None = None
    row: JobRun | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def open(cls, container: Container, *, run_id: str, kind: str) -> JobLedger:
        ledger = cls(container=container, run_id=run_id, kind=kind)
        if not container.write_path_enabled:
            ledger.notes.append(
                "job_run not written: this deployment has no Postgres write path, so the run "
                "ledger under out/warehouse is the only record of this job"
            )
            return ledger
        session = container.new_session()
        ledger.session = session
        job_id = _current_rq_job_id()
        if job_id is not None:
            ledger.job_id = job_id
            ledger.row = session.get(JobRun, job_id)
        if ledger.row is None:
            ledger.row = (
                session.execute(
                    select(JobRun)
                    .where(JobRun.run_id == run_id, JobRun.kind == kind)
                    .order_by(JobRun.created_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if ledger.row is not None:
                ledger.job_id = str(ledger.row.job_id)
        if ledger.row is None:
            ledger.notes.append(
                f"no job_run row references run {run_id} (kind {kind!r}); submit_job normally "
                "writes one, so this job was started by hand rather than through the API"
            )
        return ledger

    def close(self) -> None:
        if self.session is not None:
            with suppress(SQLAlchemyError):
                self.session.rollback()
            self.session.close()
            self.session = None

    @property
    def closed_already(self) -> bool:
        """Whether the row already carries a terminal state (a re-delivered job)."""
        return self.row is not None and str(self.row.state) in CLOSED_JOB_STATES

    def recorded(self) -> dict[str, Any] | None:
        return None if self.row is None or self.row.result is None else dict(self.row.result)

    def mark_started(self, *, trace_id: str) -> None:
        """``queued -> started``, and the trace id ``submit_job`` left null.

        ``job_run.trace_id`` is written as ``None`` at submission because the row's key is
        the queue's job id and its trace is the request's; the job's own trace begins here,
        so filling it in is what lets ``/api/jobs/{id}`` name the log lines this job produced.
        """
        self._move(JOB_STARTED, trace_id=trace_id)

    def mark_finished(self, result: Mapping[str, Any]) -> None:
        self._move(JOB_FINISHED, result=dict(result), finished_at=datetime.now(UTC))

    def mark_failed(self, error: str) -> None:
        self._move(JOB_FAILED, error=error[:DETAIL_LIMIT], finished_at=datetime.now(UTC))

    def _move(self, state: str, **values: Any) -> None:
        if self.session is None or self.row is None:
            return
        self.row.state = state
        for key, value in values.items():
            setattr(self.row, key, value)
        try:
            self.session.commit()
        except SQLAlchemyError as exc:
            # Bookkeeping must not be the reason a run's verdict is unknown: the run row and
            # the stage ledger are already committed by their own paths, so this is logged as
            # the named loss it is rather than allowed to replace the real failure.
            self.session.rollback()
            self.notes.append(f"job_run write failed: {type(exc).__name__}: {str(exc)[:200]}")
            logger.error("job_run transition failed", run_id=self.run_id, error=str(exc)[:300])


def _current_rq_job_id() -> str | None:
    """The id of the job this process is executing, or ``None`` outside a worker.

    RQ sets the context var only inside a horse running a job. Outside one — a test, or an
    operator running a stage by hand — there is no job id to look the row up by, which is
    why :class:`JobLedger` also searches by ``run_id``.
    """
    job = get_current_job()
    return None if job is None else str(job.id)


# --- the run itself ---------------------------------------------------------


@dataclass(slots=True)
class StageRun:
    """One job's pipeline execution: the context its stages share, and its ledger."""

    context: StageContext
    emitter: StageEventEmitter
    session: Session | None
    opened: str

    @property
    def run_id(self) -> str:
        return self.emitter.run_id


@contextmanager
def open_stage_run(
    container: Container, *, kind: str, run_id: str, dry_run: bool
) -> Iterator[StageRun]:
    """Build the ``StageContext`` the stage bodies expect, then open or resume the run.

    Deliberately *not* ``cli._open_context``: that function chooses its own sink — a
    ``NullWarehouse`` under ``out/warehouse`` — and a run registered there is invisible to
    the API that just answered with its id. Everything else it does is reproduced by calling
    the same helpers it calls (resolve the root, load ``config/``, require ``RUN_SALT``,
    name the run, hash the config, choose the model version) so the two entry points cannot
    tell two stories about one run.
    """
    from oxbow import __version__, cli
    from oxbow.config import CONFIG_DIRNAME, load_pipeline_config, resolve_run_salt
    from oxbow.identity import require_ulid

    settings = container.settings
    root = settings.repo_root
    context_dir = root / CONFIG_DIRNAME
    started = time.monotonic()
    try:
        cfg = load_pipeline_config(root)
        salt = resolve_run_salt(root)
    except cli.ConfigError as exc:  # oxbow.config raises this; a refusal, never a default
        raise StageConfigError(str(exc)) from exc
    identity = require_ulid(run_id.strip().upper(), field="run_id")

    session: Session | None = None
    sink: WarehouseSink
    if container.write_path_enabled:
        from oxbow.adapters.warehouse.postgres import PostgresWarehouseSink

        session = container.new_session()
        sink = DurableSink(inner=PostgresWarehouseSink(session), session=session)
    else:
        sink = DurableSink(inner=container.warehouse_sink_factory(), session=None)

    emitter = StageEventEmitter(sink=sink, run_id=identity, stream=False, echo=_log_line)
    context = cli.StageContext(
        stage=kind,
        root=root,
        config_dir=context_dir,
        cfg=cfg,
        run_id=identity,
        salt=salt,
        emitter=emitter,
        dry_run=dry_run,
        started_monotonic=started,
    )
    opened = "dry-run"
    try:
        if not dry_run:
            _refuse_immutable_run(sink, identity)
            opened = emitter.ensure_run_open(
                seed=cfg.seed,
                timezone=cfg.deployment_timezone,
                config_hash=cli._config_hash(context_dir),
                model_version=f"{cli.MODEL_VERSION_PREFIX}/{__version__}",
                provenance="pipeline",
                dataset_ref=(root / cli.DATA_DIRNAME / cli.INTERIM_DIRNAME).as_posix(),
            )
            _stamp_model_version(container, session, identity)
        yield StageRun(context=context, emitter=emitter, session=session, opened=opened)
    finally:
        if session is not None:
            with suppress(SQLAlchemyError):
                session.rollback()
            session.close()


def _refuse_immutable_run(sink: WarehouseSink, run_id: str) -> None:
    """Stop before writing into a ledger that is already closed.

    ``ensure_run_open`` raises on a completed run, and letting that through would land a
    ``failed`` job over work an earlier delivery already finished — RQ re-delivers a job
    whose horse died, and by then the run may have completed. Asking the sink (not the
    API's read model, which caches its misses) turns the same fact into a named no-op.
    """
    try:
        state = sink.run_state(run_id)
    except WarehouseTableError:
        return  # nothing opened under this id yet; ``ensure_run_open`` will open it
    if state in IMMUTABLE_RUN_STATES:
        raise JobAlreadyRecordedError(
            f"run {run_id} is {state.value} and its ledger is immutable, so this delivery has "
            "nothing to do. Rescoring is a new run (plan §13)."
        )


class StageConfigError(RuntimeError):
    """``config/`` or ``RUN_SALT`` could not be resolved, so no stage can be honest."""


def _log_line(message: str) -> None:
    """Where the ledger's ``echo`` goes: structlog, not a stray stdout line.

    The CLI prints these for a human watching a terminal. A worker has no terminal, and a
    line nobody reads is worse than a log line an aggregator can index — ``run_id`` arrives
    on it from the context vars that ``run_stages`` sets.
    """
    logger.info(message)


def _stamp_model_version(container: Container, session: Session | None, run_id: str) -> None:
    """Replace ``pending:{kind}`` with the version actually producing rows.

    The run row freezes at ``complete``, so this is the only moment the placeholder can be
    replaced; leaving it means every ``meta.model_version`` this run answers with names a
    queue state instead of a model. Guarded on ``state = 'running'`` and on the placeholder
    prefix, so it can neither rewrite a finished run nor overwrite a real version.
    """
    if session is None or not container.write_path_enabled:
        return
    from oxbow import __version__, cli

    try:
        session.execute(
            update(Run)
            .where(
                Run.run_id == run_id,
                Run.state == RunState.RUNNING.value,
                Run.model_version.startswith(PENDING_VERSION_PREFIX),
            )
            .values(model_version=f"{cli.MODEL_VERSION_PREFIX}/{__version__}")
        )
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        logger.warning(
            "run.model_version could not be stamped", run_id=run_id, error=str(exc)[:200]
        )


# --- stage dispatch ---------------------------------------------------------


def default_runners(context: StageContext) -> dict[str, Callable[[StageHandle], int]]:
    """The four stage bodies :mod:`oxbow.cli` owns, at the same defaults it uses.

    ``pipeline_cmd`` composes exactly these calls in this order for a run started from a
    terminal; a run started from the queue must not differ in one argument, because two ways
    to run the same stage is two ways to get two different numbers out of one corpus. The
    fifth declared stage has no runner and becomes an explicit ``unavailable`` in
    :func:`runner_for`.
    """
    from oxbow import cli

    return {
        "ingest": lambda handle: cli.run_ingest_stage(
            context, handle, source=[], limit=None, out=None, batch_rows=None
        ),
        "graph": lambda handle: cli.run_graph_stage(context, handle, source=[], max_events=None),
        "score": lambda handle: cli.run_score_stage(context, handle, source=[], max_events=None),
        "backtest": lambda handle: cli.run_backtest_stage(
            context, handle, corpus=None, demo_fakes=False, baselines_only=False, out=None
        ),
    }


def runner_for(stage: str, context: StageContext) -> Callable[[StageHandle], int]:
    """The body for one stage: the pipeline's own, or a named refusal.

    A stage the composition root does not implement is reported ``unavailable`` with the
    missing seam named, and returns zero so the run's verdict stays about the stages that
    did run. Marking it ``failed`` would call a seam nobody has written a measurement that
    came out red; marking it ``complete`` would be the lie ``stage_events.py`` exists to
    prevent.
    """
    from oxbow import cli

    if stage in cli.STAGES:
        return default_runners(context)[stage]

    reason = (
        f"no runner is wired for stage {stage!r} in oxbow.cli: its declared stages are "
        f"{list(cli.STAGES)}, and nothing in this repository loads landed artifacts into the "
        f"warehouse tables under a run id, so {stage} has no work to perform"
    )

    def refuse(handle: StageHandle) -> int:
        handle.mark_unavailable(reason)
        return cli.EXIT_OK

    return refuse


def _validate(kind: str, stages: Sequence[str]) -> None:
    """Refuse a job whose kind or stages this build cannot honour, before running any.

    Cheaper than discovering it mid-chain: a stage outside the ledger's vocabulary would be
    rejected by ``assert_stage_status`` *after* the earlier stages had already written rows
    and burned minutes.
    """
    if kind not in JOB_KINDS:
        raise UnknownJobKindError(
            f"kind {kind!r} is not one of {sorted(JOB_KINDS)}; api/jobs.py queues only those, "
            "so a job naming another was not submitted through this API"
        )
    known, statuses = stage_vocabulary()
    if not stages:
        raise UnknownStageError(
            f"kind {kind!r} arrived with no stages; submission declares {list(JOB_KINDS[kind])}"
        )
    unknown = [stage for stage in stages if stage not in known]
    if unknown:
        raise UnknownStageError(
            f"stages {unknown} are outside the ledger's vocabulary {list(known)}, which the "
            f"sink's CHECK constraint would reject and the UI cannot render. Statuses the "
            f"ledger knows: {list(statuses)}"
        )


def _drive(
    container: Container,
    *,
    kind: str,
    run_id: str,
    stages: Sequence[str],
    runners: Mapping[str, Callable[[StageHandle], int]] | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Run each stage in order and close the run with the verdict they produced.

    The loop is ``pipeline_cmd``'s loop: one ``StageContext`` shared by every stage, the
    first non-zero exit stopping the chain, ``terminal=False`` so the run's own terminal
    state is written once at the end rather than by whichever stage finished first.
    """
    from oxbow import cli

    with open_stage_run(container, kind=kind, run_id=run_id, dry_run=dry_run) as run:
        outcomes: list[tuple[str, int]] = []
        for stage in stages:
            code = cli._execute(run.context, stage, _body_for(stage, run, runners), terminal=False)
            outcomes.append((stage, int(code)))
            if code != cli.EXIT_OK:
                # A backtest over a corpus that failed to land is arithmetic on a fiction.
                break
        statuses = _terminal_statuses(run)
        if dry_run:
            return _summary(run, kind=kind, stages=stages, outcomes=outcomes, statuses=statuses)
        failures = [f"{stage} exited {code}" for stage, code in outcomes if code]
        if failures:
            run.emitter.finish(RunState.FAILED, error="; ".join(failures)[:DETAIL_LIMIT])
        else:
            run.emitter.finish(RunState.COMPLETE)
        return _summary(
            run,
            kind=kind,
            stages=stages,
            outcomes=outcomes,
            statuses=statuses,
            failures=failures,
        )


def _body_for(
    stage: str,
    run: StageRun,
    runners: Mapping[str, Callable[[StageHandle], int]] | None,
) -> Callable[[StageHandle], int]:
    """The body for one stage: an injected one when the caller supplied it, else the real one.

    An injected runner that does not name this stage still falls through to the real
    dispatch, so a test that supplies one failing body and asks for two stages exercises
    both halves of the seam instead of dying on a ``KeyError`` nobody was testing for.
    """
    if runners is not None and stage in runners:
        return runners[stage]
    return runner_for(stage, run.context)


def _terminal_statuses(run: StageRun) -> dict[str, str]:
    """Each stage's last ledger status, read back from the sink and not remembered.

    The read model serves the same rows, so what the job reports and what the UI renders
    cannot disagree — which is the property ``stage_events.py``'s module docstring names as
    the reason there is one emitter at all.
    """
    statuses: dict[str, str] = {}
    for event in run.emitter.sink.stage_events(run.run_id):
        statuses[str(event["stage"])] = str(event["status"])
    return statuses


def _summary(
    run: StageRun,
    *,
    kind: str,
    stages: Sequence[str],
    outcomes: Sequence[tuple[str, int]],
    statuses: Mapping[str, str],
    failures: Sequence[str] = (),
) -> dict[str, Any]:
    """The job's result, stored in ``job_run.result`` and returned to RQ.

    Counts, states and timings only: no row-level data, and no float in a money-shaped name
    anywhere (01 §B). The rows themselves are already in ``stage_event``; duplicating them
    here would be a second truth about one event.
    """
    return {
        "run_id": run.run_id,
        "kind": kind,
        "queue": QUEUE_NAME,
        "ledger": run.opened,
        "stages_requested": [str(stage) for stage in stages],
        "stages_attempted": [stage for stage, _ in outcomes],
        "exit_codes": dict(outcomes),
        "stage_status": dict(statuses),
        "failures": list(failures),
        "events_emitted": len(run.emitter.events),
        "elapsed_ms": int(round((time.monotonic() - run.context.started_monotonic) * 1000)),
        "unavailable": sorted(name for name, state in statuses.items() if state == "unavailable"),
        "failed": sorted(name for name, state in statuses.items() if state == "failed"),
        "run_state": _run_state(run),
    }


def _run_state(run: StageRun) -> str:
    try:
        return str(run.emitter.sink.run_state(run.run_id).value)
    except (WarehouseTableError, StageEventError):
        # A dry run never opens a run, and a run whose row vanished is reported as what it
        # is rather than guessed at.
        return "unknown"


# --- the queued entrypoint --------------------------------------------------


def _sweep_orphans(
    container: Container, *, probe: Callable[[str], str | None] | None = None
) -> int:
    """Close abandoned rows at the start of every job, not only at process start.

    :func:`reclaim_orphaned` runs once in :func:`main`, which covers a worker that
    restarts. It does not cover the commoner failure on this stack: RQ forks a horse per
    job, the kernel OOM-kills *that* process, and the worker carries on serving. Nothing
    ever runs the boot sweep again, so a run the killed horse was holding stays ``running``
    indefinitely -- the exact row shape this module's whole failure story exists to
    prevent, arriving by a route no ``except`` block sees.

    Re-running the sweep per job closes that window without a new mechanism: the same
    grace period and the same "an unreachable Redis is not evidence" refusal apply, so a
    job another live worker is running is never condemned, and a job's own fresh row is
    younger than the grace by construction.

    It cannot fail the job it precedes. Bookkeeping about somebody else's abandoned row is
    not this stage's business, and a worker that dies of a good intention serves nobody.
    """
    try:
        observer = probe if probe is not None else _rq_state_probe(container)
        closed = reclaim_orphaned(container, probe=observer)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, and it only logs
        logger.warning(
            "the per-job orphan sweep was skipped", error=f"{type(exc).__name__}: {exc}"
        )
        return 0
    if closed:
        logger.warning(
            "the per-job orphan sweep closed rows",
            closed=len(closed),
            jobs=[str(row.job_id) for row in closed],
        )
    return len(closed)


def run_stages(
    *,
    run_id: str,
    stages: list[str],
    kind: str,
    container: Container | None = None,
    runners: Mapping[str, Callable[[StageHandle], int]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute the stages of one queued job and record every transition.

    The three keyword-only arguments before ``container`` are exactly what
    ``api/jobs.py:102`` enqueues under the dotted name ``api.worker.run_stages``; RQ passes
    them as kwargs, so those names are part of the wire contract and not free to rename.
    ``container``, ``runners`` and ``dry_run`` default for that call and exist so the seam is
    testable: a caller may supply its own container (a test's scratch warehouse) and its own
    stage bodies (one that raises, to prove the failure path).

    Failure is a state, never a silence. Anything that escapes :func:`_drive` is written to
    ``job_run.error`` and to the run row before it is re-raised, so RQ's failure registry and
    the database agree. A run left ``running`` by a process that no longer exists is the one
    row shape this worker must not produce.
    """
    owned = container is None
    resolved = build_container() if owned else container
    identity = assert_run_id(run_id.strip().upper())
    trace_token = trace_id_var.set(new_trace_id())
    run_token = run_id_var.set(identity)
    trace_id = trace_id_var.get()
    ledger = JobLedger.open(resolved, run_id=identity, kind=kind)
    logger.info(
        "job started",
        run_id=identity,
        kind=kind,
        job_id=ledger.job_id,
        stages=[str(stage) for stage in stages],
        warehouse_backend=resolved.backend,
    )
    try:
        replayed = _replayed_summary(ledger, identity)
        if replayed is not None:
            return replayed
        _validate(kind, stages)
        ledger.mark_started(trace_id=str(trace_id))
        _sweep_orphans(resolved)
        summary = _drive(
            resolved,
            kind=kind,
            run_id=identity,
            stages=[str(stage) for stage in stages],
            runners=runners,
            dry_run=dry_run,
        )
        if summary["run_state"] == RunState.FAILED.value:
            raise StageChainFailedError(
                f"run {identity} went red: "
                + ("; ".join(summary["failures"]) or "a stage recorded its own failure")
            )
        # Written here, not after the ``finally``: the finally clause closes the session, and
        # a verdict committed against a closed connection is not committed at all.
        ledger.mark_finished(summary)
    except BaseException as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, JobAlreadyRecordedError):
            # Not a failure: the ledger already holds this job's verdict from a delivery that
            # got here first. Recording ``failed`` over it would be a false accusation.
            logger.warning("job redelivered after its verdict", run_id=identity, detail=reason)
        else:
            _force_run_failed(resolved, identity, reason)
            ledger.mark_failed(reason)
            logger.error("job failed", run_id=identity, kind=kind, error=reason[:400])
        raise
    finally:
        ledger.close()
        if owned:
            resolved.close()
        trace_id_var.reset(trace_token)
        run_id_var.reset(run_token)
    logger.info(
        "job finished",
        run_id=identity,
        kind=kind,
        job_id=ledger.job_id,
        run_state=summary["run_state"],
        stages_attempted=summary["stages_attempted"],
    )
    return summary


def _replayed_summary(ledger: JobLedger, run_id: str) -> dict[str, Any] | None:
    """What to return when this delivery has nothing left to do, or ``None`` to carry on.

    Two shapes of re-delivery exist. RQ retrying a job whose horse died finds the ``job_run``
    row still open and the run still ``running``: that is the normal path, this returns
    ``None``, and :func:`open_stage_run` resumes the run row the first delivery opened. A job
    redelivered *after* its own row reached a verdict is the case below — the work is recorded,
    and re-running it would either duplicate the ledger or replace a finished job's row with a
    ``failed`` one.
    """
    if not ledger.closed_already:
        return None
    prior = None if ledger.row is None else str(ledger.row.state)
    recorded = ledger.recorded() or {}
    logger.warning(
        "job already recorded in job_run; its stages are not being run a second time",
        run_id=run_id,
        job_id=ledger.job_id,
        prior_state=prior,
    )
    # The first delivery's own summary is echoed rather than a fresh one, because nothing was
    # measured here and an invented zero would be a second, weaker account of the same job.
    # ``ledger`` says why the row count did not move.
    return {
        "run_id": run_id,
        "kind": ledger.kind,
        "queue": QUEUE_NAME,
        "ledger": "replayed",
        "job_state": prior,
        "stages_requested": list(recorded.get("stages_requested") or []),
        "stages_attempted": list(recorded.get("stages_attempted") or []),
        "exit_codes": dict(recorded.get("exit_codes") or {}),
        "stage_status": dict(recorded.get("stage_status") or {}),
        "failures": list(recorded.get("failures") or []),
        "events_emitted": 0,
        "elapsed_ms": 0,
        "unavailable": list(recorded.get("unavailable") or []),
        "failed": list(recorded.get("failed") or []),
        "run_state": str(recorded.get("run_state") or "unknown"),
    }


def _force_run_failed(container: Container, run_id: str, reason: str) -> None:
    """Last-resort close for a run still ``running`` after its job died.

    The normal path is ``StageEventEmitter.finish`` inside :func:`_drive`. This exists because
    that path can itself fail — the exception may *be* the sink's session blowing up — and the
    guarantee the run row owes the SSE stream is not conditional on the failure having a
    convenient cause. Guarded on ``state = 'running'`` so it can never overwrite a verdict a
    stage actually reached.
    """
    if not container.write_path_enabled:
        _force_run_failed_on_files(container, run_id, reason)
        return
    session = container.new_session()
    try:
        closed = session.execute(
            update(Run)
            .where(Run.run_id == run_id, Run.state == RunState.RUNNING.value)
            .values(
                state=RunState.FAILED.value,
                error=reason[:DETAIL_LIMIT],
                finished_at=datetime.now(UTC),
            )
        )
        session.commit()
        if int(closed.rowcount) and not _ledger_has_failure(container, run_id):
            logger.warning(
                "run forced to failed with no failed stage row",
                run_id=run_id,
                detail="the ledger could not record which stage died; the reason is on the run",
            )
    except SQLAlchemyError as exc:
        session.rollback()
        logger.error(
            "run could not be forced to a terminal state",
            run_id=run_id,
            error=str(exc)[:300],
            detail="the stream for this run closes on its budget instead of on a verdict",
        )
    finally:
        session.close()


def _force_run_failed_on_files(container: Container, run_id: str, reason: str) -> None:
    """The same refusal on the null-file backend, through the sink that owns the files."""
    try:
        sink = container.warehouse_sink_factory()
        if sink.run_state(run_id) is RunState.RUNNING:
            sink.complete_run(run_id, RunState.FAILED, error=reason[:DETAIL_LIMIT])
    except (WarehouseTableError, StageEventError, OSError) as exc:
        logger.warning("null-file run could not be closed", run_id=run_id, error=str(exc)[:200])


def _ledger_has_failure(container: Container, run_id: str) -> bool:
    from api.readmodel import RunNotFound

    try:
        events = container.read_model.stage_events(run_id)
    except RunNotFound:
        return False
    return any(str(event["status"]) == "failed" for event in events)


# --- the outbox drain -------------------------------------------------------


def drain_outbox(*, container: Container | None = None, reschedule: bool = True) -> dict[str, Any]:
    """One drain pass over the outbox, then the next appointment.

    Plan §13 says an RQ worker drains the outbox, and this process is the only one in the
    stack that qualifies as a worker. ``api/outbox.py`` already refuses to remember anything,
    so a pass is stateless by construction and two workers overlapping is the at-least-once
    contract the docs state, not a bug. ``rq-scheduler`` is not a dependency of this build, so
    the period is carried by the job itself: each pass books its successor. If a pass dies hard
    enough to skip that, the appointment dies with it and the next worker start re-seeds it —
    which is why :func:`main` books the first one.
    """
    from api.outbox import drain_once

    owned = container is None
    resolved = build_container() if owned else container
    try:
        if not resolved.write_path_enabled:
            logger.warning(
                "outbox not drained: this deployment has no Postgres write path, so it has "
                "no outbox rows"
            )
            return {"drained": False, "reason": "no write path"}
        report = drain_once(resolved)
        if reschedule:
            _schedule_next_drain(_queue_for(resolved))
        return {
            "drained": True,
            "claimed": report.claimed,
            "sent": report.sent,
            "retrying": report.retrying,
            "dead": report.dead,
            "rejected_permanently": report.rejected_permanently,
        }
    finally:
        if owned:
            resolved.close()


def _schedule_next_drain(queue: Queue | None) -> None:
    if queue is None:
        logger.warning("outbox drain not rescheduled: the queue is unreachable")
        return
    try:
        queue.enqueue_in(
            timedelta(seconds=DRAIN_INTERVAL_SECONDS),
            "api.worker.drain_outbox",
            job_timeout="5m",
        )
    except Exception as exc:  # a missed appointment must not fail the pass that made it
        logger.error("outbox drain could not be rescheduled", error=str(exc)[:200])


# --- crash recovery ---------------------------------------------------------


@dataclass(slots=True)
class AbandonedJob:
    """One job the database thinks is live and the queue knows is not."""

    job_id: str
    kind: str
    run_id: str | None
    prior_state: str
    rq_state: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "run_id": self.run_id,
            "prior_state": self.prior_state,
            "rq_state": self.rq_state,
            "reason": self.reason,
        }


def reclaim_orphaned(
    container: Container,
    *,
    now: datetime | None = None,
    grace_seconds: int = ABANDON_GRACE_SECONDS,
    probe: Callable[[str], str | None] | None = None,
) -> list[AbandonedJob]:
    """Close jobs a dead worker left ``started``, and the runs they opened.

    This is the enqueue-side guard of ``api/jobs.py:120-130`` seen from the other end:
    ``job_run`` must not hold a run that looks live and never advances, and a ``SIGKILL`` —
    an OOM, a container scaled away, a ``job_timeout`` that killed the horse — reaches no
    ``except`` block in :func:`run_stages`. So each worker start asks the queue, job by job,
    whether the job behind an open row still exists.

    Two refusals keep it from doing damage. It never acts on a job the queue cannot answer
    about — an unreachable Redis is not evidence that a job died, so the whole sweep aborts —
    and it never touches a row younger than ``grace_seconds``, because another worker may be
    inside that stage right now. Each row it does close carries the *evidence* in its error
    text, because "the worker died" and "the queue forgot" are different findings.
    """
    if not container.write_path_enabled:
        return []
    observer = probe if probe is not None else _rq_state_probe(container)
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=grace_seconds)
    session = container.new_session()
    abandoned: list[AbandonedJob] = []
    rows = []
    try:
        rows = list(
            session.execute(
                select(JobRun)
                .where(JobRun.state.in_(OPEN_JOB_STATES), JobRun.created_at < cutoff)
                .order_by(JobRun.created_at)
            ).scalars()
        )
    except SQLAlchemyError as exc:
        session.rollback()
        session.close()
        logger.error("orphan sweep could not read job_run", error=str(exc)[:300])
        return []

    try:
        for row in rows:
            state = observer(str(row.job_id))
            if state is None:
                logger.warning(
                    "orphan sweep stopped without sweeping: the queue did not answer about a "
                    "job",
                    job_id=str(row.job_id),
                    detail="an unanswered question is not evidence that a job died",
                )
                return []
            if state not in DEAD_RQ_STATES:
                continue
            prior = str(row.state)
            reason = (
                f"abandoned: job_run recorded {prior!r} but the queue reports "
                f"{state or 'no such job'!r}; the worker that claimed it is gone"
            )
            row.state = JOB_FAILED
            row.error = reason[:DETAIL_LIMIT]
            row.finished_at = now or datetime.now(UTC)
            if row.run_id is not None:
                session.execute(
                    update(Run)
                    .where(Run.run_id == str(row.run_id), Run.state == RunState.RUNNING.value)
                    .values(
                        state=RunState.FAILED.value,
                        error=reason[:DETAIL_LIMIT],
                        finished_at=now or datetime.now(UTC),
                    )
                )
            abandoned.append(
                AbandonedJob(
                    job_id=str(row.job_id),
                    kind=str(row.kind),
                    run_id=None if row.run_id is None else str(row.run_id),
                    prior_state=prior,
                    rq_state=state or "no such job",
                    reason=reason,
                )
            )
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        logger.error("orphan sweep rolled back", error=str(exc)[:300])
        return []
    finally:
        session.close()
    for item in abandoned:
        logger.warning("abandoned job closed", **item.as_dict())
    return abandoned


def _rq_state_probe(container: Container) -> Callable[[str], str | None]:
    """A probe reading one job's state from Redis, or ``None`` when Redis is mute.

    ``""`` is the answer "this job does not exist" — evidence. ``None`` is "I could not ask",
    which the sweep treats as a reason to stop, not as permission to close rows.
    """
    connection = _redis_connection(container)
    if connection is None:
        return lambda _job_id: None

    def probe(job_id: str) -> str | None:
        try:
            return str(Job.fetch(job_id, connection=connection).get_status())
        except NoSuchJobError:
            return ""
        except Exception as exc:
            logger.warning("job state unknown", job_id=job_id, error=str(exc)[:200])
            return None

    return probe


# --- process entry ----------------------------------------------------------


def _redis_connection(container: Container) -> Redis | None:
    """The client ``api/jobs.py:_queue`` builds, or ``None`` when Redis will not answer."""
    try:
        client = Redis.from_url(container.settings.redis_url)
        client.ping()
    except Exception as exc:
        logger.error("redis did not answer PING", detail=str(exc)[:200])
        return None
    return client


def _queue_for(container: Container) -> Queue | None:
    """The job queue the worker serves, built exactly the way the API enqueues into it.

    Mirroring ``api/jobs.py:162-183`` rather than inventing a second construction is the
    point: ``Queue(QUEUE_NAME, connection=Redis.from_url(...))`` is what guarantees the
    consumer is listening on the same list the producer pushed to.
    """
    connection = _redis_connection(container)
    if connection is None:
        return None
    return Queue(QUEUE_NAME, connection=connection)


def _install_logging() -> None:
    """The processor chain, at process start (``api/observability.py:186``).

    ``OXBOW_LOG_FORMAT=console`` is the same switch the API's lifespan reads, so one operator
    setting makes both processes human-readable instead of two that drift.
    """
    configure_logging(json_output=os.environ.get("OXBOW_LOG_FORMAT", "json") != "console")
    clear_contextvars()


def build_worker(container: Container) -> Worker | None:
    """The RQ worker for this deployment's queue, or ``None`` when the queue is unreachable."""
    queue = _queue_for(container)
    if queue is None:
        logger.error(
            "the worker cannot serve: Redis at the configured REDIS_URL did not answer PING. "
            "A worker with no queue holds a slot nothing can fill, which is why api/jobs.py "
            "refuses the same way on the submission side."
        )
        return None
    return Worker([queue])


def main(argv: Sequence[str] | None = None) -> int:
    """Serve the queue: sweep the wreckage, re-seed the drain, then block on ``work``."""
    parser = argparse.ArgumentParser(
        prog="oxbow-worker",
        description=(
            "RQ worker for the jobs api/jobs.py enqueues: pipeline and backtest stage runs, "
            "and the outbox drain."
        ),
    )
    parser.add_argument(
        "--burst",
        action="store_true",
        help="serve what is already queued and exit, instead of serving forever",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    _install_logging()
    container = build_container(get_settings())
    try:
        if not container.write_path_enabled:
            # api/jobs.py refuses to *submit* a job on the null-file backend, because a run
            # nobody can audit after a Redis flush is not a job. A worker that would execute
            # such jobs anyway is the same refusal one step later, so it says so at startup
            # rather than idling against a queue nothing can ever enqueue to.
            logger.error(
                "the worker will not start: this deployment is null-file, so jobs cannot be "
                "submitted and stage events have no table to land in. Set DATABASE_URL and "
                "start the Compose database."
            )
            return 2
        swept = reclaim_orphaned(container)
        queue = _queue_for(container)
        if queue is None:
            return 2
        _schedule_next_drain(queue)
        worker = build_worker(container)
        if worker is None:
            return 2
        logger.info(
            "worker serving",
            queue=QUEUE_NAME,
            warehouse_backend=container.backend,
            jobs_in_queue=len(list(queue.get_job_ids())),
            abandoned_closed=len(swept),
            degraded=container.degraded_components(),
        )
        # This container exists for the sweep and the startup report. Its engine is disposed
        # before the first job is claimed, because RQ forks a horse per job and a psycopg
        # connection inherited across a fork is one connection in two processes. Each job
        # builds and closes its own, which is also what keeps a crashed horse from taking a
        # pooled connection with it.
        container.close()
        worker.work(burst=args.burst)
        return 0
    finally:
        container.close()


__all__ = [
    "ABANDON_GRACE_SECONDS",
    "CLOSED_JOB_STATES",
    "DRAIN_INTERVAL_SECONDS",
    "JOB_FAILED",
    "JOB_FINISHED",
    "JOB_QUEUED",
    "JOB_STARTED",
    "OPEN_JOB_STATES",
    "JobAlreadyRecordedError",
    "StageChainFailedError",
    "StageConfigError",
    "UnknownJobKindError",
    "UnknownStageError",
    "build_worker",
    "default_runners",
    "drain_outbox",
    "main",
    "reclaim_orphaned",
    "run_stages",
    "runner_for",
]


if __name__ == "__main__":  # pragma: no cover - the compose entrypoint
    raise SystemExit(main())
