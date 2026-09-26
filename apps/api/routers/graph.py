"""Network explorer: a subgraph, capped on the server, with the overflow named.

Plan §14 sets the cap at 1,500 nodes and requires that what falls off the graph does
not vanish: it arrives as community meta-nodes labelled with their true member counts.
That requirement is the whole design of this router — a client-side cap would show a
different graph to two analysts on two machines, and a silent server-side cap would
understate the structure the page exists to show.

Every node and edge here comes from stored ``graph_edge``, ``account_membership`` and
``community`` rows. No edge is inferred from a shared counterparty or a close
timestamp: an edge the pipeline did not write is a claim about the corpus that nothing
measured.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from fastapi import APIRouter, Depends, Query

from api.deps import Container, analyst_or_higher, get_container
from api.problems import (
    COMMON_ERROR_STATUSES,
    BadRequest,
    DependencyUnavailable,
    problem_responses,
)
from api.readmodel import money
from api.routers.common import build_meta
from api.schemas.catalog import (
    CommunityMetaNode,
    NetworkEdge,
    NetworkNode,
    NetworkSubgraph,
)
from api.schemas.common import Envelope, envelope
from api.security import Principal

router = APIRouter(prefix="/api/graph", tags=["graph"])

MAX_HOPS: Final = 4
DEFAULT_MIN_AMOUNT_MINOR: Final = 0

# The dense-community overlay's threshold. It is a display threshold over a *stored*
# density, not a re-clustering: the graph layer's own community policy lives in
# config/pipeline.yaml, and a second definition of "dense" in the renderer is how two
# screens disagree about the same picture.
DENSE_COMMUNITY_MIN: Final = 0.3


@router.get(
    "/subgraph",
    response_model=Envelope[NetworkSubgraph],
    summary="A bounded subgraph around one account, with communities collapsed",
    description=(
        "Breadth-first over stored edges, ``hops`` deep. Beyond the configured node cap "
        "whole communities collapse into meta-nodes that carry their true size, and "
        "``truncated`` plus ``truncation_reason`` say that happened."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def subgraph(
    account_key: str = Query(min_length=3, max_length=12),
    run_id: str | None = Query(default=None, min_length=26, max_length=26),
    hops: int | None = Query(default=None, ge=1, le=MAX_HOPS),
    min_amount_minor: int = Query(default=DEFAULT_MIN_AMOUNT_MINOR, ge=0),
    window_start: datetime | None = Query(default=None),
    window_end: datetime | None = Query(default=None),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    run = read_model.resolve_run(run_id, state=None if run_id else "complete")
    rid = str(run["run_id"])
    settings = container.settings
    graph_settings = _graph_settings(settings)
    cap = int(graph_settings["subgraph_node_cap"])
    depth = int(graph_settings["default_hops"]) if hops is None else hops
    if depth > MAX_HOPS:
        raise BadRequest(
            f"hops={depth} exceeds the {MAX_HOPS}-hop server limit: a wider radius on this "
            "corpus reaches the rails, and a graph that includes a merchant aggregator "
            "shows the economy rather than a network"
        )

    edges = read_model.subgraph(rid, account_key, hops=depth, min_amount_minor=min_amount_minor)
    if window_start is not None or window_end is not None:
        if window_start is not None and window_end is not None and window_end < window_start:
            raise BadRequest("window_end precedes window_start; an inverted window matches nothing")
        edges = [edge for edge in edges if _in_window(edge, window_start, window_end)]

    keys = {account_key}
    for edge in edges:
        keys.add(str(edge["src_account_key"]))
        keys.add(str(edge["dst_account_key"]))

    memberships, _ = read_model.source.select(
        "account_membership", where={"run_id": rid}, allow_missing=True
    )
    by_key = {str(row["account_key"]): row for row in memberships}
    scores, _ = read_model.source.select("score", where={"run_id": rid}, allow_missing=True)
    score_by_key = {str(row["account_key"]): row for row in scores}
    economics, _ = read_model.source.select("economics", where={"run_id": rid}, allow_missing=True)
    money_by_key = {str(row["account_key"]): row for row in economics}
    communities, _ = read_model.source.select(
        "community", where={"run_id": rid}, allow_missing=True
    )
    community_by_index = {int(row["canonical_index"]): row for row in communities}

    # Node overlays are derived from stored columns only: ``graph_edge.is_rail`` and
    # ``graph_edge.flags`` for the edge-derived ones, ``community.density`` for the
    # dense-community overlay, and the score band for "flagged". The read model has no
    # per-account rail column, so a node inherits the flag from the edges that touch it
    # and the response says which overlay came from where.
    rail_keys: set[str] = set()
    flags_by_key: dict[str, set[str]] = {}
    for edge in edges:
        for side in ("src_account_key", "dst_account_key"):
            key = str(edge[side])
            flags_by_key.setdefault(key, set()).update(
                str(flag) for flag in (edge.get("flags") or [])
            )
            if bool(edge.get("is_rail")):
                rail_keys.add(key)
    dense_communities = {
        int(row["canonical_index"])
        for row in communities
        if row.get("density") is not None and float(row["density"]) >= DENSE_COMMUNITY_MIN
    }

    truncated = len(keys) > cap
    collapsed: list[CommunityMetaNode] = []
    included: set[str] = {account_key}
    if truncated:
        included, collapsed = _collapse(
            keys=keys,
            by_key=by_key,
            community_by_index=community_by_index,
            money_by_key=money_by_key,
            cap=cap,
            seed=account_key,
        )
    nodes = [
        NetworkNode(
            id=key,
            label=key,
            band=None if key not in score_by_key else str(score_by_key[key]["band"]),
            exposure=None
            if key not in money_by_key
            else money(
                int(money_by_key[key]["exposure_minor"]),
                str(money_by_key[key]["currency"]),
                decimals=container.economics.minor_units_per_major,
            ),
            degree=int((by_key.get(key) or {}).get("degree") or 0),
            community_id=None if key not in by_key else int(by_key[key]["community_id"]),
            is_seed=key == account_key,
            is_rail=key in rail_keys,
            flags=_node_flags(
                key,
                # The edge-derived flag set for this node. Passing `key` here instead of the
                # mapping made `_node_flags` call `.get()` on a string, which raised
                # AttributeError for every node — i.e. `GET /api/graph/subgraph` answered 500
                # for every account, on both warehouses, with or without edges.
                flags_by_key=flags_by_key,
                membership=by_key.get(key),
                score=score_by_key.get(key),
                dense_communities=dense_communities,
            ),
        )
        for key in sorted(included)
    ]
    kept_edges = [
        NetworkEdge(
            source=str(edge["src_account_key"]),
            target=str(edge["dst_account_key"]),
            total=money(
                int(edge["total_minor"]),
                str(edge["currency"]),
                decimals=container.economics.minor_units_per_major,
            ),
            txn_count=int(edge["txn_count"]),
            first_ts=edge["first_ts"],
            last_ts=edge["last_ts"],
            flags=list(edge.get("flags") or []),
        )
        for edge in edges
        if edge["src_account_key"] in included and edge["dst_account_key"] in included
    ]
    counterparty_note = None
    if not edges:
        counterparty_note = (
            f"{account_key} has no stored edge at {depth} hop(s)"
            + (" in this window" if window_start is not None or window_end is not None else "")
            + ". The graph is empty because of the window and the radius, not because the "
            "account is isolated: widen either and ask again."
        )
    body = NetworkSubgraph(
        run_id=rid,
        seed_account_key=account_key,
        hops=depth,
        nodes=nodes,
        edges=kept_edges,
        collapsed_communities=collapsed,
        node_cap=cap,
        truncated=truncated,
        truncation_reason=None
        if not truncated
        else f"{len(keys)} nodes within {depth} hops exceeded the configured cap of {cap}; "
        "whole communities were collapsed into meta-nodes carrying their true member counts",
        window_start=window_start,
        window_end=window_end,
        counterparty_note=counterparty_note,
    )
    return envelope(body, **build_meta(container, run_id=rid).model_dump())


def _graph_settings(settings: Any) -> dict[str, Any]:
    try:
        graph = settings.pipeline_config().graph
    except Exception as exc:  # ConfigError or OSError: the cap is not optional
        raise DependencyUnavailable(
            f"config/pipeline.yaml's graph section could not be read, so the node cap is "
            f"unknown and no subgraph can be safely sized: {exc}"
        ) from exc
    for key in ("subgraph_node_cap", "default_hops"):
        if key not in graph:
            raise DependencyUnavailable(
                f"config/pipeline.yaml graph.{key} is missing. The cap is a policy (plan §14), "
                "so it is refused rather than defaulted to a number invented in code."
            )
    return graph


def _in_window(edge: dict[str, Any], start: datetime | None, end: datetime | None) -> bool:
    first = _as_dt(edge["first_ts"])
    last = _as_dt(edge["last_ts"])
    if start is not None and last < start:
        return False
    if end is not None and first > end:
        return False
    return True


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _node_flags(
    key: str,
    *,
    flags_by_key: dict[str, set[str]],
    membership: dict[str, Any] | None,
    score: dict[str, Any] | None,
    dense_communities: set[int],
) -> list[str]:
    """Overlay flags, read off stored columns rather than recomputed here."""
    flags = set(flags_by_key.get(key) or set())
    if membership is not None and int(membership.get("community_id") or -1) in dense_communities:
        flags.add("dense_community")
    if score is not None and str(score.get("band")) in {"D", "E"}:
        flags.append("flagged")
    return sorted(flags)


def _collapse(
    *,
    keys: set[str],
    by_key: dict[str, dict[str, Any]],
    community_by_index: dict[int, dict[str, Any]],
    money_by_key: dict[str, dict[str, Any]],
    cap: int,
    seed: str,
) -> tuple[set[str], list[CommunityMetaNode]]:
    """Keep the seed's own community in full; represent the rest by meta-node.

    The order is deterministic (seed first, then by community size descending, then by
    smallest member key), which is what keeps the same query on the same run drawing the
    same picture for two people at two screens.
    """
    by_community: dict[int, list[str]] = {}
    unassigned: list[str] = []
    for key in keys:
        membership = by_key.get(key)
        if membership is None:
            unassigned.append(key)
            continue
        by_community.setdefault(int(membership["community_id"]), []).append(key)

    included: set[str] = {seed}
    seed_community = (by_key.get(seed) or {}).get("community_id")
    if seed_community is not None:
        for key in sorted(by_community.get(int(seed_community), [])):
            included.add(key)
    collapsed: list[CommunityMetaNode] = []
    ordered = sorted(
        (index for index in by_community if index != seed_community),
        key=lambda index: (-len(by_community[index]), index),
    )
    for index in ordered:
        members = sorted(by_community[index])
        row = community_by_index.get(index) or {}
        if len(included) + len(members) <= cap:
            included.update(members)
            continue
        total_minor = row.get("total_minor")
        collapsed.append(
            CommunityMetaNode(
                community_id=index,
                member_count=int(row.get("size") or len(members)),
                total=None
                if total_minor is None
                else money(
                    int(total_minor),
                    str(row.get("currency") or "UGX"),
                    decimals=2,
                ),
                representative_account_key=members[0],
            )
        )
    for key in sorted(unassigned):
        if len(included) < cap:
            included.add(key)
    return included, collapsed


__all__ = ["MAX_HOPS", "router"]
