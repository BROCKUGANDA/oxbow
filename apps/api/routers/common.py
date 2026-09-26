"""Shared router plumbing: envelope meta, paging, and the filters the queue offers.

Two rules live here so no router can forget them.

**Every success body is ``{data, meta}`` and nothing else.** ``status`` is the HTTP
code; there is no ``success`` field anywhere in this API (the doctrine test walks the
generated schema and fails if one appears). ``meta`` is where a response carries the
run id, the trace id, the model version, the provenance of its own numbers, the
assumptions its money depends on, and the disclaimer — because a screen that cannot
name those has nothing to be challenged on.

**Degraded is a field, not a silent substitution.** If this process could not reach
the solver, ``meta.degraded`` is true and ``degraded_reason`` names it, so the pane can
render the labelled banner plan §14 requires instead of showing a fallback as if it
were the answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from fastapi import Query

from api.deps import Container
from api.problems import BadRequest
from api.readmodel import SORTABLE_ALERT_COLUMNS, ReadModel
from api.schemas.common import AssumptionLine, Meta, PageMeta
from oxbow.quant.economics import Economics

DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 500

PageParams = tuple[int, int, str, str]


class Pagination:
    """``limit``/``offset``/``sort``/``order`` as one object, validated once."""

    def __init__(self, limit: int, offset: int, sort: str, order: str, allowed: frozenset[str]):
        self.limit = limit
        self.offset = offset
        self.sort = sort
        self.order = order
        self.allowed = allowed

    def checked(self) -> None:
        if self.sort not in self.allowed:
            raise BadRequest(
                f"sort={self.sort!r} is not a sortable column here; the API sorts server-side "
                f"only on {sorted(self.allowed)}, because a client-side sort of a paged list "
                "sorts one page and calls that the queue"
            )
        if self.order not in {"asc", "desc"}:
            raise BadRequest(f"order must be asc or desc, got {self.order!r}")


def page_params(default_sort: str, allowed: frozenset[str]) -> Any:
    """Build the four query dependencies every list route shares."""

    def dependency(
        limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
        sort: str = Query(default=default_sort),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> Pagination:
        page = Pagination(limit, offset, sort, order, allowed)
        page.checked()
        return page

    return dependency


alert_page_params = page_params("rank", SORTABLE_ALERT_COLUMNS)


def build_meta(
    container: Container,
    *,
    run_id: str | None = None,
    model_version: str | None = None,
    provenance: str | None = None,
    assumptions: list[AssumptionLine] | None = None,
    extra: dict[str, Any] | None = None,
) -> Meta:
    """The ``meta`` half of every envelope, assembled in one place.

    ``degraded`` comes from the live component probe rather than a caller's argument,
    because a router that could set it false is a router that will.
    """
    components = container.components()
    unavailable = [item.name for item in components if item.state != "available"]
    payload: dict[str, Any] = {
        "run_id": run_id,
        "trace_id": _trace_id(),
        "model_version": model_version,
        "provenance": provenance,
        "generated_at": datetime.now(UTC),
        "assumptions": assumptions or [],
        "degraded": bool(unavailable),
        "degraded_reason": None
        if not unavailable
        else ", ".join(f"{name}: {container.component(name).detail}" for name in unavailable),
        "disclaimer": ReadModel.disclaimer(),
    }
    payload.update(extra or {})
    return Meta.model_validate(payload)


def build_page_meta(
    container: Container,
    page: Pagination,
    total: int,
    *,
    run_id: str | None = None,
    model_version: str | None = None,
    provenance: str | None = None,
    assumptions: list[AssumptionLine] | None = None,
) -> PageMeta:
    next_offset = page.offset + page.limit if page.offset + page.limit < total else None
    return PageMeta.model_validate(
        {
            **build_meta(
                container,
                run_id=run_id,
                model_version=model_version,
                provenance=provenance,
                assumptions=assumptions,
            ).model_dump(),
            "limit": page.limit,
            "offset": page.offset,
            "total": total,
            "next_offset": next_offset,
            "sort": page.sort,
            "order": page.order,
        }
    )


def assumption_lines(economics: Economics) -> list[AssumptionLine]:
    """The four assumptions every money figure on this response depends on.

    Only the ones a money figure is actually a function of — the recovery rate, the
    band, the analyst minute price and the friction cost — because plan §16 asks for a
    *visible* assumption line naming its config values, and a wall of every key in the
    file is how a reader stops checking them.
    """
    return [
        AssumptionLine(
            key="recovery.rate",
            value=economics.recovery.rate,
            source="config/economics.yaml",
            note="r: the share of exposure a timely investigation prevents",
        ),
        AssumptionLine(
            key="recovery.sensitivity_band",
            value=", ".join(str(item) for item in economics.recovery.band),
            source="config/economics.yaml",
            note="every headline money figure is rendered over these three rates",
        ),
        AssumptionLine(
            key="analyst.cost_per_minute_minor",
            value=economics.analyst.cost_per_minute_minor,
            source="config/economics.yaml",
            note=f"minor units per analyst-minute, {economics.currency}",
        ),
        AssumptionLine(
            key="friction_cost_minor",
            value=economics.friction_cost.minor,
            source="config/economics.yaml",
            note="f: the priced harm of freezing a legitimate customer",
        ),
        AssumptionLine(
            key="four_eyes.threshold_exposure_minor",
            value=economics.four_eyes.threshold_exposure_minor,
            source="config/economics.yaml",
            note="exposure above which a decision needs a second reviewer before delivery",
        ),
    ]


def _trace_id() -> str | None:
    from api.observability import trace_id_var

    return trace_id_var.get()


def run_label(run: dict[str, Any]) -> dict[str, str]:
    """The two provenance fields every run-scoped response carries."""
    return {
        "model_version": str(run["model_version"]),
        "provenance": str(run["provenance"]),
    }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Pagination",
    "alert_page_params",
    "assumption_lines",
    "build_meta",
    "build_page_meta",
    "page_params",
    "run_label",
]
