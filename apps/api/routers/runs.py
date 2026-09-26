"""Run routes: the ledger of completed runs, and the immutable detail of one.

A run is the unit of everything OXBOW claims, so this router has one job beyond
listing rows: make the *immutability* visible. Plan §13 states it as a rule — "a run
is immutable once complete; rescoring creates a new run" — and the reason a user
should be able to see it from an API response is that it is what lets a packet pin a
run and stay truthful forever.

Three response fields carry that claim:

* ``state`` — ``running`` runs are explicitly labelled, and their counts are partial;
* ``superseded_by`` — a *derived* field, not a stored one. Migration 0002 freezes a
  complete run's identity columns and its state, so "this run was later replaced"
  cannot be written back into it; it is read off the ordering of ULIDs (DEV-003),
  which is exactly why run ids sort by creation time;
* ``quarantine_count`` — never defaulted to zero in a response. Rows dropped at
  ingest are a blind spot, and the plan requires them counted (02 §D).

``POST /api/runs/{run_id}/rescore`` queues a job and returns a **new** run id. It
never touches the old one, and it answers 409 if the old one is still running, because
two concurrent runs over the same dataset produce two partial read models.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, Path, Query

from api.deps import Container, analyst_or_higher, get_container, reviewer_or_higher
from api.jobs import submit_job
from api.problems import (
    COMMON_ERROR_STATUSES,
    BadRequest,
    Conflict,
    problem_responses,
)
from api.readmodel import SORTABLE_RUN_COLUMNS, ReadModel
from api.routers.common import Pagination, build_meta, build_page_meta, page_params, run_label
from api.schemas.catalog import RunDetail, RunSummary
from api.schemas.common import Envelope, PageEnvelope, envelope
from api.schemas.events import RunProgress
from api.schemas.health import JobStatus
from api.security import Principal

router = APIRouter(prefix="/api/runs", tags=["runs"])

run_page_params = page_params("run_id", SORTABLE_RUN_COLUMNS)

# A rescore over a corpus this large is not a request; it is a job. The number is a
# guard against a client hammering the queue rather than a tuning knob, and it lives
# here because it is a property of this route.
RESCORE_REQUIRES_STATE: Final = ("complete", "failed")


@router.get(
    "",
    response_model=PageEnvelope[RunSummary],
    summary="List runs, newest first, with their row counts and quarantine counts",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def list_runs(
    page: Pagination = Depends(run_page_params),
    state: str | None = Query(default=None, pattern="^(running|complete|failed|superseded)$"),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    rows, total = read_model.list_runs(
        state=state, limit=page.limit, offset=page.offset, sort=page.sort, order=page.order
    )
    newest = _newest_complete(read_model)
    return envelope(
        [
            RunSummary.model_validate({**row, "superseded_by": _superseded_by(row, newest)})
            for row in rows
        ],
        **build_page_meta(container, page, total).model_dump(),
    )


@router.get(
    "/{run_id}",
    response_model=Envelope[RunDetail],
    summary="One immutable run, with its stage ledger and artifact hashes",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def run_detail(
    run_id: str = Path(min_length=26, max_length=26),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    row = read_model.run_row(run_id)
    summary = read_model.run_summary(row)
    summary["superseded_by"] = _superseded_by(summary, _newest_complete(read_model))
    events = read_model.stage_events(run_id)
    detail = RunDetail.model_validate(
        {**summary, "events": [dict(event) for event in events]}
    )
    return envelope(
        detail,
        **build_meta(
            container, run_id=run_id, **run_label(row), assumptions=[]
        ).model_dump(),
    )


@router.get(
    "/{run_id}/progress",
    response_model=Envelope[RunProgress],
    summary="The stage ledger alone, for a client that only needs the rows",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def run_progress(
    run_id: str = Path(min_length=26, max_length=26),
    after_id: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    row = read_model.run_row(run_id)
    events = read_model.stage_events(run_id, after_id=after_id)
    progress = RunProgress(
        run_id=run_id,
        state=str(row["state"]),
        events=[dict(event) for event in events],
    )
    return envelope(progress, **build_meta(container, run_id=run_id).model_dump())


@router.post(
    "/{run_id}/rescore",
    response_model=Envelope[JobStatus],
    summary="Queue a rescore, which creates a NEW run and never edits this one",
    description=(
        "The new run id is returned immediately and its stage events stream from "
        "``/api/runs/{new_run_id}/events/stream``. The pinned run stays byte-for-byte "
        "unchanged, which is what keeps an already-exported packet truthful."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def rescore(
    run_id: str = Path(min_length=26, max_length=26),
    note: str | None = Query(default=None, max_length=500),
    container: Container = Depends(get_container),
    principal: Principal = Depends(reviewer_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    source_run = read_model.run_row(run_id)
    if str(source_run["state"]) not in RESCORE_REQUIRES_STATE:
        raise Conflict(
            f"run {run_id} is {source_run['state']}, so a rescore would produce two runs "
            "writing the same tables at once. Wait for it to finish.",
            run_id=run_id,
        )
    if str(source_run["state"]) == "running":  # pragma: no cover - guarded by the check above
        raise BadRequest("unreachable: a running run cannot pass the state guard")
    submission = submit_job(
        container,
        kind="pipeline",
        stages=("ingest", "graph", "score"),
        origin_run_id=run_id,
        requested_by=principal.subject,
        note=note,
        timezone=str(source_run["timezone"]),
        provenance=str(source_run["provenance"]),
    )
    return envelope(
        submission,
        **build_meta(container, run_id=submission.run_id).model_dump(),
    )


def _newest_complete(read_model: ReadModel) -> str | None:
    rows, _ = read_model.source.select(
        "run", where={"state": "complete"}, order="run_id", descending=True, limit=1
    )
    return None if not rows else str(rows[0]["run_id"])


def _superseded_by(row: dict[str, Any], newest: str | None) -> str | None:
    """The newer complete run that replaced this one, or ``None``.

    Derived rather than stored because migration 0002's trigger freezes a complete
    run's state and identity columns — writing ``superseded_by`` into a finished run
    later is refused by the database, correctly, since that row is what a packet
    pinned. Sorting ULIDs lexicographically is what makes "newest" mean "latest
    created" (DEV-003).
    """
    if newest is None or row.get("state") not in {"complete", "failed", "superseded"}:
        return None
    stored = row.get("superseded_by")
    if stored:
        return str(stored)
    current = str(row["run_id"])
    return newest if newest != current and newest > current else None


__all__ = ["router"]
