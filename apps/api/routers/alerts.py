"""The alert queue: filtered, sorted and paged on the server, ranked by the policy.

Plan §14's queue is virtualised and deep-linkable, which forces three things this
router is responsible for:

* **paging is server-side.** A client that fetched every alert and sliced it could not
  show a stable ``total`` while a run wrote, and the URL would not be sendable to a
  judge with the same rows on it.
* **the capacity cutoff is data, not geometry.** ``rank``, ``selected``,
  ``beyond_capacity`` and ``cutoff_rank`` come from the active policy's stored
  allocation. When a run wrote none, the queue re-allocates through the P5 layer and
  says so in ``allocation_source`` — a line drawn from a different computation than
  the ranking is the disagreement 02 §B seam 5 exists to prevent.
* **confidence never appears alone.** Each row carries the calibrated probability with
  its observed rate and the population behind it, because "high confidence" without an
  ``n`` is an adjective (plan §10), and the queue card is where a reviewer decides
  whether to open the case.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, Query

from api.deps import Container, analyst_or_higher, get_container
from api.policy_engine import live_rank_map, stored_priced_rows
from api.problems import (
    COMMON_ERROR_STATUSES,
    BadRequest,
    DependencyUnavailable,
    problem_responses,
)
from api.readmodel import ReadModel, money
from api.routers.common import Pagination, alert_page_params, build_meta, build_page_meta
from api.schemas.catalog import AlertFacets, AlertQueue, AlertRow
from api.schemas.common import Envelope, PageEnvelope, envelope
from api.security import Principal
from oxbow.quant.allocate import AllocatorId

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

DEFAULT_RUN_FROM: Final = "complete"


@router.get(
    "",
    response_model=PageEnvelope[AlertQueue],
    summary="The queue: server-filtered, server-sorted, ranked under the active policy",
    description=(
        "One row per scored account in one run. ``band`` accepts a repeated query "
        "parameter; ``side_of_cutoff=above`` returns only what the capacity budget "
        "reaches, which is the set the desk actually reviews this period."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def list_alerts(
    page: Pagination = Depends(alert_page_params),
    run_id: str | None = Query(default=None, min_length=26, max_length=26),
    band: list[str] | None = Query(default=None, description="Repeatable: A, B, C, D, E."),
    typology: str | None = Query(default=None, max_length=64),
    rule_id: str | None = Query(default=None, max_length=8, pattern="^R([1-9]|1[0-2])$"),
    account_key: str | None = Query(default=None, min_length=3, max_length=12),
    min_exposure_minor: int | None = Query(default=None, ge=0),
    max_exposure_minor: int | None = Query(default=None, ge=0),
    side_of_cutoff: str | None = Query(default=None, pattern="^(above|below)$"),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    run = read_model.resolve_run(run_id, state=DEFAULT_RUN_FROM if run_id is None else None)
    rid = str(run["run_id"])
    bands = _parse_bands(band)
    if bands is not None and not bands:
        raise BadRequest("band= was given but nothing valid in it; bands are A, B, C, D, E")

    active = _active_policy(container, rid)
    allocations, source_label, cutoff_rank, unpriced, capacity = _rankings(
        container, read_model, rid, active
    )
    rows, total = read_model.alert_page(
        run_id=rid,
        bands=bands,
        typology=typology,
        rule_id=rule_id,
        account_key=account_key,
        min_exposure_minor=min_exposure_minor,
        max_exposure_minor=max_exposure_minor,
        side_of_cutoff=side_of_cutoff,
        sort=page.sort,
        order=page.order,
        limit=page.limit,
        offset=page.offset,
        policy_id=None if active is None else str(active["policy_id"]),
        allocations=allocations,
    )
    decimals = container.economics.minor_units_per_major
    queue = AlertQueue(
        run_id=rid,
        policy_id=None if active is None else str(active["policy_id"]),
        capacity_minutes=capacity,
        cutoff_rank=cutoff_rank,
        allocation_source=source_label,
        unpriced_accounts=unpriced,
        rows=[
            AlertRow.model_validate(
                {
                    **row,
                    "reasons": _reasons(row.get("reason_codes")),
                    "exposure": money(
                        row.get("exposure_minor"), row.get("currency"), decimals=decimals
                    ),
                    "expected_value": money(
                        row.get("expected_value_minor"),
                        row.get("currency"),
                        decimals=decimals,
                    ),
                    "capacity_minutes": capacity,
                    "cutoff_rank": cutoff_rank,
                }
            )
            for row in rows
        ],
    )
    meta = build_page_meta(
        container,
        page,
        total,
        run_id=rid,
        model_version=str(run["model_version"]),
        provenance=str(run["provenance"]),
    )
    return envelope(queue, **meta.model_dump())


@router.get(
    "/facets",
    response_model=Envelope[AlertFacets],
    summary="The values each filter actually holds, so the UI never offers an empty one",
    description=(
        "The narrowest-predicate empty state (plan §14) needs to know what removing one "
        "filter would return, and it cannot know that from a page of rows. This is that "
        "count: per band, per typology, per rule, over the *current* filter set."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def facets(
    run_id: str | None = Query(default=None, min_length=26, max_length=26),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    run = read_model.resolve_run(run_id, state=DEFAULT_RUN_FROM if run_id is None else None)
    rid = str(run["run_id"])
    scores, total = read_model.source.select("score", where={"run_id": rid}, allow_missing=True)
    bands: dict[str, int] = {}
    typologies: dict[str, int] = {}
    rules: dict[str, int] = {}
    for row in scores:
        band_value = str(row.get("band"))
        bands[band_value] = bands.get(band_value, 0) + 1
        typology = row.get("predicted_typology")
        if typology:
            key = str(typology)
            typologies[key] = typologies.get(key, 0) + 1
        for hit in row.get("rule_ids") or []:
            rules[str(hit)] = rules.get(str(hit), 0) + 1
    body = {
        "run_id": rid,
        "total": total,
        "bands": bands,
        "typologies": typologies,
        "rules": rules,
        "sortable": sorted(
            [
                "rank",
                "fused_score",
                "calibrated_probability",
                "exposure_minor",
                "expected_value_minor",
                "last_seen_at",
                "first_seen_at",
                "txn_count",
                "band",
                "account_key",
            ]
        ),
    }
    from api.schemas.common import Meta

    meta: Meta = build_meta(container, run_id=rid)
    return envelope(AlertFacets.model_validate(body), **meta.model_dump())


def _active_policy(container: Container, run_id: str) -> dict[str, Any] | None:
    if not container.write_path_enabled:
        return None
    from sqlalchemy import select

    from oxbow.adapters.warehouse.models import Policy

    session = container.new_session()
    try:
        row = (
            session.execute(select(Policy).where(Policy.active.is_(True)).limit(1))
            .scalars()
            .first()
        )
        from api.policy_engine import _policy_dict

        return None if row is None else _policy_dict(row)
    finally:
        session.close()


def _rankings(
    container: Container,
    read_model: ReadModel,
    run_id: str,
    active: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]] | None, str, int | None, int, int]:
    """Stored ranks when the run wrote them, otherwise a live re-allocation.

    ``allocations=None`` tells the Postgres source to use the stored
    ``policy_allocation`` join; a mapping means the ranks are being produced now,
    which the response reports in ``allocation_source`` rather than hiding.
    """
    capacity = (
        int(active["capacity_minutes"])
        if active is not None
        else container.economics.capacity.review_minutes_per_period
    )
    if active is not None:
        stored = _stored_allocations(read_model, run_id, str(active["policy_id"]))
        if stored:
            cutoff = max(
                (int(row["rank"]) for row in stored.values() if row["selected"]), default=None
            )
            return stored, "stored", cutoff, 0, capacity
    rows, skipped = stored_priced_rows(read_model, run_id, container.economics)
    if not rows:
        raise DependencyUnavailable(
            f"run {run_id} has no priced accounts, so the queue has no ranking to show. The "
            "cutoff line cannot be drawn over an empty set, and an empty list here would read "
            "as 'no alerts' rather than 'nothing scored'."
        )
    allocation = _allocate(rows, capacity, container)
    ranks = live_rank_map(allocation)
    cutoff = max((entry["rank"] for entry in ranks.values() if entry["selected"]), default=None)
    # An unpriced account (scored, no stored economics row) is a missing row in the run's
    # own output. It is counted into the response rather than quietly absent from the
    # total, which is the difference between a short queue and a lying one.
    return ranks, "reallocated", cutoff, len(skipped), capacity


def _allocate(rows: Any, capacity: int, container: Container) -> Any:
    from oxbow.quant.allocate import allocate

    return allocate(rows, capacity, container.economics, allocator=AllocatorId.GREEDY)


def _stored_allocations(
    read_model: ReadModel, run_id: str, policy_id: str
) -> dict[str, dict[str, Any]]:
    rows, _ = read_model.source.select(
        "policy_allocation", where={"run_id": run_id, "policy_id": policy_id}, allow_missing=True
    )
    return {
        str(row["account_key"]): {
            "rank": int(row["rank"]),
            "selected": bool(row["selected"]),
            "beyond_capacity": bool(row["beyond_capacity"]),
        }
        for row in rows
    }


def _reasons(raw: Any) -> list[dict[str, Any]]:
    """The top reason codes, as objects, so the card renders label plus points.

    Stored in two shapes across pipeline versions — a list of strings and a list of
    ``{code,label,points}`` maps. Both are read; a reason with no points is reported
    with ``points`` null rather than zero, because zero points is a claim about the
    scorecard and null is a claim about the record.
    """
    if not isinstance(raw, list):
        return []
    reasons: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            points = item.get("points")
            reasons.append(
                {
                    "code": str(item.get("code") or item.get("reason_code") or "unspecified"),
                    "label": str(item.get("label") or item.get("description") or ""),
                    "points": None if points is None else int(points),
                }
            )
        else:
            reasons.append({"code": str(item), "label": "", "points": None})
    return reasons[:3]


def _parse_bands(values: list[str] | None) -> list[str] | None:
    """Validate the repeated ``band=`` filter against the five band letters.

    An unknown band is refused rather than dropped: a filter that silently matches
    nothing is how a reviewer concludes a queue is empty.
    """
    if values is None:
        return None
    allowed = {"A", "B", "C", "D", "E"}
    cleaned = [value.strip().upper() for value in values if value and value.strip()]
    unknown = [value for value in cleaned if value not in allowed]
    if unknown:
        raise BadRequest(
            f"band={unknown!r} is not one of {sorted(allowed)}; bands are the five scorecard "
            "classes and nothing else"
        )
    return cleaned or []


__all__ = ["router"]
