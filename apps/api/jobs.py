"""Job submission: RQ for the queue, ``job_run`` for the record.

Plan §13 wants pipeline and backtest runs as jobs with live progress, and there is a
specific reason both a queue and a table are involved: RQ keeps its state in Redis,
which does not survive a flush and cannot be joined to a run. ``job_run`` is what
makes "what did we run, when, and what came out" answerable from the database alone —
the same argument the outbox makes about deliveries.

The submission therefore writes the row *before* enqueuing, and refuses if Redis is
down rather than running the work inline. A synchronous pipeline run inside an HTTP
request is the failure plan §13 lists: the request times out, the worker restarts, and
nobody can say which of the two produced the numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Depends, Path
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from ulid import ULID

from api.deps import Container, analyst_or_higher, get_container, reviewer_or_higher
from api.observability import get_logger
from api.problems import (
    COMMON_ERROR_STATUSES,
    Conflict,
    DependencyUnavailable,
    NotFound,
    problem_responses,
)
from api.readmodel import RunNotFound
from api.schemas.common import Envelope, envelope
from api.schemas.events import JobSubmission
from api.schemas.health import JobStatus
from api.security import Principal
from api.settings import get_settings
from oxbow.adapters.warehouse.models import JobRun, Run
from oxbow.ports.warehouse import RunState

logger = get_logger("oxbow.jobs")

router = APIRouter(tags=["jobs"])

# The stages a queued pipeline job runs. `warehouse` is one of them and is not one of the
# CLI's four verbs: 01 §D fixes `oxbow ingest graph score backtest` and the P0 gate asserts
# that tuple, while the stage-event ledger carries a CHECK over
# `ingest, graph, score, backtest, warehouse` — so landing a run is a stage by the ledger's
# own vocabulary, dispatched by the worker and never a fifth command.
PIPELINE_STAGES: Final = ("ingest", "graph", "score", "warehouse")
BACKTEST_STAGES: Final = ("backtest",)
QUEUE_NAME: Final = "oxbow"

# A run the queue is about to write must be registered first: every stage event row
# carries a foreign key to it, and a job that opens its own run mid-flight would make
# the submission response name a run id that does not exist yet if the enqueue failed.
JOB_KINDS: Final = {"pipeline": PIPELINE_STAGES, "backtest": BACKTEST_STAGES}

#: The ``job_run`` primary key before the queue answers with its own id. The row is written
#: first because the alternative — writing it after the enqueue, which is what this file used
#: to do — leaves a window in which a worker that ``brpop``s the job immediately sees neither
#: row: it then tries to open the ``run`` row itself and takes a unique violation on the
#: in-flight insert, while its own ``job_run`` lookup misses and every state it books is
#: dropped, so the row sits ``queued`` after the run finished. RQ mints the job id, so the
#: key is renamed to that id the moment the enqueue returns.
PENDING_JOB_ID_PREFIX: Final = "pending:"

#: ``run.error`` and ``job_run.error`` are unbounded ``Text``, but the reason a submission
#: died is quoted from an exception that may name a connection string, so it is bounded and
#: redacted rather than pasted whole.
ERROR_LIMIT: Final = 240


class QueueUnavailable(DependencyUnavailable):
    """Redis is down, so no job can be promised. The request says so rather than running it."""

    code, title = "queue-unavailable", "Job queue unavailable"


def submit_job(
    container: Container,
    *,
    kind: str,
    stages: tuple[str, ...],
    origin_run_id: str | None,
    requested_by: str,
    note: str | None,
    timezone: str = "UTC",
    provenance: str = "pipeline",
) -> JobSubmission:
    """Register a new run, enqueue the work, and return the client's handle.

    The new run id is minted here rather than by the worker so the response can name
    the stream to subscribe to before the job has even started. It is a real row
    because the stage events the worker writes will reference it.

    Both rows are committed *before* the enqueue and the queue's own id is written into the
    ``job_run`` key as soon as the enqueue answers, so there is no moment in which the job is
    runnable and the record of it is not. :data:`PENDING_JOB_ID_PREFIX` says why the key has to
    be renamed rather than minted here.
    """
    if kind not in JOB_KINDS:
        raise Conflict(
            f"unknown job kind {kind!r}; this API queues {sorted(JOB_KINDS)} and nothing else"
        )
    if not container.write_path_enabled:
        raise QueueUnavailable(
            "jobs need the Postgres write path: a job whose run row, stage events and result "
            "live only in Redis cannot be audited after a flush. This deployment is null-file."
        )
    run_id = str(ULID()).upper()
    pending_job_id = f"{PENDING_JOB_ID_PREFIX}{run_id}"
    session = container.new_session()
    try:
        session.add(
            Run(
                run_id=run_id,
                state=RunState.RUNNING.value,
                seed=container.settings.seed,
                timezone=timezone,
                provenance=provenance,
                config_hash=container.economics.source_path.name,
                model_version=f"pending:{kind}",
                dataset_ref=None if origin_run_id is None else f"rescore-of:{origin_run_id}",
                notes=note,
            )
        )
        # Flushed before the job row is added: the two tables have a foreign key between them
        # and no ORM relationship, so nothing else orders the two INSERTs. The defect this
        # function used to carry was the *commit* landing after the enqueue, not the flush.
        session.flush()
        session.add(
            JobRun(
                job_id=pending_job_id,
                kind=kind,
                run_id=run_id,
                queue=QUEUE_NAME,
                state="queued",
                trace_id=None,
            )
        )
        session.commit()
    except BaseException:
        # Nothing was promised yet, but the commit above may or may not have landed. The
        # clean-up is written to survive either answer; see its docstring.
        session.rollback()
        _mark_submission_failed(
            session, run_id=run_id, job_id=pending_job_id, kind=kind, cause=None
        )
        raise
    try:
        queue = _queue(container)
        job = queue.enqueue(
            "api.worker.run_stages",
            run_id=run_id,
            stages=list(stages),
            kind=kind,
            job_timeout="2h",
        )
        job_id = str(job.id)
    except BaseException as exc:
        # The run row is committed and visible now, so the refusal has to close it: a run in
        # ``running`` with no job behind it is the one thing this table must not contain.
        session.rollback()
        _mark_submission_failed(session, run_id=run_id, job_id=pending_job_id, kind=kind, cause=exc)
        raise
    try:
        # Re-key to the id the caller was handed, in its own transaction. The job is already
        # queued, so a failure here is a named bookkeeping loss rather than a reason to
        # condemn a run that is about to be worked on.
        session.execute(
            update(JobRun)
            .where(JobRun.job_id == pending_job_id)
            .values(job_id=job_id)
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except SQLAlchemyError as exc:  # pragma: no cover - the job runs whatever this row says
        session.rollback()
        logger.warning(
            "job_run could not be re-keyed to the queue's job id",
            run_id=run_id,
            pending_job_id=pending_job_id,
            job_id=job_id,
            error=f"{type(exc).__name__}: {str(exc)[:ERROR_LIMIT]}",
            detail="GET /api/jobs will report no row for the id the client was given; the run "
            "itself is queued and will produce its own ledger",
        )
    finally:
        session.close()
    from api.observability import trace_id_var

    return JobSubmission(
        job_id=job_id,
        run_id=run_id,
        queue=QUEUE_NAME,
        position=_position(container, job_id),
        stages=list(stages),
        stream_path=f"/api/runs/{run_id}/events/stream",
        submitted_at=datetime.now(UTC),
        trace_id=trace_id_var.get(),
    )


def _mark_submission_failed(
    session: Any, *, run_id: str, job_id: str, kind: str, cause: BaseException | None
) -> None:
    """Close out a submission that never reached the queue, with the reason stored.

    Reached from an ``except`` path, so the rows being closed are either committed (the
    enqueue failed after the commit) or absent (the commit itself failed) — one ``UPDATE``
    handles both, issued on a fresh transaction after the rollback, and guarded on the states
    only an unclosed submission has. It can therefore never overwrite a verdict a worker that
    did claim the job already booked, which is what keeps this clean-up from becoming the
    race it is here to close.
    """
    reason = f"job submission for {kind} failed: {_reason(cause)}"[:ERROR_LIMIT]
    try:
        session.execute(
            update(Run)
            .where(Run.run_id == run_id, Run.state == RunState.RUNNING.value)
            .values(state=RunState.FAILED.value, error=reason, finished_at=datetime.now(UTC))
        )
        session.execute(
            update(JobRun)
            .where(
                JobRun.job_id == job_id,
                JobRun.state.in_(("queued", "started")),
                JobRun.finished_at.is_(None),
            )
            .values(state="failed", error=reason, finished_at=datetime.now(UTC))
        )
        session.commit()
    except SQLAlchemyError:  # pragma: no cover - the submission failure is the reportable one
        session.rollback()
        logger.error(
            "an abandoned job submission could not be closed out",
            run_id=run_id,
            job_id=job_id,
            detail="the run row is still open; the orphan sweep closes it, and the stream for "
            "this run holds to its budget until it does",
        )


def _reason(cause: BaseException | None) -> str:
    """A one-line, bounded, credential-free account of why a submission died."""
    if cause is None:
        return "the run and job rows could not be committed before the enqueue"
    text = " ".join(str(cause).split()) or type(cause).__name__
    url = str(get_settings().redis_url or "")
    if url:
        text = text.replace(url, "***redis-url***")
    return f"{type(cause).__name__}: {text[:ERROR_LIMIT]}"


def _queue(container: Container) -> Any:
    """The RQ queue, or a named refusal.

    Imported lazily so the API can start and serve reads with Redis down: a missing
    queue degrades the job endpoints, not the product.
    """
    try:
        from redis import Redis
        from rq import Queue
    except ImportError as exc:  # pragma: no cover - rq is a pinned dependency
        raise QueueUnavailable(f"rq is not importable: {exc}") from exc
    url = container.settings.redis_url
    try:
        client = Redis.from_url(url)
        client.ping()
    except Exception as exc:
        raise QueueUnavailable(
            f"Redis at {url} did not answer PING, so no job can be promised. Jobs are not run "
            "inline in the request: a request that doubles as a worker is how a rescore gets "
            "run twice by a retrying proxy."
        ) from exc
    return Queue(QUEUE_NAME, connection=client)


def _position(container: Container, job_id: str) -> int | None:
    try:
        queue = _queue(container)
    except QueueUnavailable:
        return None
    ids = [str(item) for item in queue.get_job_ids()]
    return ids.index(job_id) + 1 if job_id in ids else None


@router.get(
    "/api/jobs/{job_id}",
    response_model=Envelope[JobStatus],
    summary="One job's recorded state, from the database rather than from Redis",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def job_status(
    job_id: str = Path(min_length=1, max_length=64),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    session = container.new_session()
    try:
        row = session.get(JobRun, job_id)
        if row is None:
            raise NotFound(
                f"no job_run row for job {job_id!r}. RQ may still know about it, but this API "
                "reports jobs from the database, because Redis state does not survive a flush"
            )
        payload = JobStatus.model_validate(_as_dict(row))
    finally:
        session.close()
    return envelope(
        payload,
        **build_meta_for(container, payload.run_id).model_dump(),
    )


def build_meta_for(container: Container, run_id: str | None) -> Any:
    from api.routers.common import build_meta

    if run_id is None:
        return build_meta(container)
    try:
        run = container.read_model.run_row(run_id)
    except RunNotFound:
        return build_meta(container)
    return build_meta(
        container,
        run_id=run_id,
        model_version=str(run["model_version"]),
        provenance=str(run["provenance"]),
    )


@router.get(
    "/api/jobs",
    response_model=Envelope[list[JobStatus]],
    summary="Recent jobs, newest first",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def list_jobs(
    kind: str | None = None,
    limit: int = 25,
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 200))
    session = container.new_session()
    try:
        statement = select(JobRun).order_by(JobRun.created_at.desc()).limit(bounded)
        if kind:
            statement = statement.where(JobRun.kind == kind)
        rows = [_as_dict(row) for row in session.execute(statement).scalars()]
    finally:
        session.close()
    return envelope(
        [JobStatus.model_validate(row) for row in rows],
        **build_meta_for(container, None).model_dump(),
    )


@router.post(
    "/api/jobs/pipeline",
    response_model=Envelope[JobSubmission],
    summary="Queue a full pipeline run (ingest, graph, score, warehouse)",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def queue_pipeline(
    container: Container = Depends(get_container),
    principal: Principal = Depends(reviewer_or_higher),
) -> dict[str, Any]:
    submission = submit_job(
        container,
        kind="pipeline",
        stages=PIPELINE_STAGES,
        origin_run_id=None,
        requested_by=principal.subject,
        note=None,
        timezone=container.settings.pipeline_config().deployment_timezone,
        provenance="pipeline",
    )
    return envelope(submission, **build_meta_for(container, submission.run_id).model_dump())


@router.post(
    "/api/jobs/backtest",
    response_model=Envelope[JobSubmission],
    summary="Queue a walk-forward backtest over the newest complete run",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def queue_backtest(
    container: Container = Depends(get_container),
    principal: Principal = Depends(reviewer_or_higher),
) -> dict[str, Any]:
    run = container.read_model.resolve_run(None, state="complete")
    submission = submit_job(
        container,
        kind="backtest",
        stages=BACKTEST_STAGES,
        origin_run_id=str(run["run_id"]),
        requested_by=principal.subject,
        note=None,
        timezone=str(run["timezone"]),
        provenance=str(run["provenance"]),
    )
    return envelope(submission, **build_meta_for(container, submission.run_id).model_dump())


def _as_dict(row: JobRun) -> dict[str, Any]:
    return {
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


__all__ = [
    "BACKTEST_STAGES",
    "JOB_KINDS",
    "PENDING_JOB_ID_PREFIX",
    "PIPELINE_STAGES",
    "QUEUE_NAME",
    "QueueUnavailable",
    "router",
    "submit_job",
]
