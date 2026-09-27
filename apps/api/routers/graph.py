"""Network explorer: a subgraph, capped on the server, with the overflow named.

Plan §14 sets the cap at 1,500 nodes and requires that what falls off the graph does
not vanish: it arrives as community meta-nodes labelled with their true member counts.
That requirement is the whole design of this router — a client-side cap would show a
different graph to two analysts on two machines, and a silent server-side cap would
understate the structure the page exists to show.

Every node and edge here comes from stored ``graph_edge``, ``account_membership`` and
``community`` rows. No edge is inferred from a shared counterparty or a close timestamp:
an edge the pipeline did not write is a claim about the corpus that nothing measured.

Two more rules hold this together, because the route was written before either was tested.

**The reads are as bounded as the response.** A page of at most ``subgraph_node_cap`` nodes
must not read every account the run scored, so each overlay table is filtered to the keys
this response names and projected to the columns it renders. The traversal itself is the
exception and says so: hop 2 is not computable from hop 1's rows, so the per-hop edge read
stays whole until the read model can bound it, which is not this file's to change.

**The cap is applied, not captioned.** ``truncated`` may not be true alongside more nodes
than ``node_cap``. A community that will not fit becomes a meta-node carrying the size the
run stored for it; the seed's own community is never collapsed, and when even that leaves
the view too wide the excess is cut by hop distance and the number cut is named.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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

# The largest ``IN`` list this router will build. The node cap is a config policy and
# a deployment may raise it, so the key set is read in bounded statements rather than
# in one whose parameter count grows with the corpus.
KEY_CHUNK: Final = 500

# A community of one is one node for one node: collapsing it costs the reader the
# account key and buys nothing. Same rule, and the same reason, as the graph layer's
# own ``MIN_MEMBERS_TO_COLLAPSE`` in ``oxbow.graph.subgraph``.
MIN_MEMBERS_TO_COLLAPSE: Final = 2

# The dense-community overlay's threshold. It is a display threshold over a *stored*
# density, not a re-clustering: the graph layer's own community policy lives in
# config/pipeline.yaml, and a second definition of "dense" in the renderer is how two
# screens disagree about the same picture.
DENSE_COMMUNITY_MIN: Final = 0.3

# The columns this route reads, named per table so a projection is auditable. The read
# model returns whole rows; ``score`` and ``economics`` in particular carry JSONB
# (reason codes, the assumption snapshot) that no field of ``NetworkNode`` renders.
MEMBERSHIP_COLUMNS: Final = ("account_key", "community_id", "degree")
SCORE_COLUMNS: Final = ("account_key", "band")
ECONOMICS_COLUMNS: Final = ("account_key", "exposure_minor", "currency")
COMMUNITY_COLUMNS: Final = ("canonical_index", "size", "density", "total_minor", "currency")


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

    # The traversal needs every stored edge that touches the frontier at each hop, and
    # that is what ``ReadModel.subgraph`` asks for: hop 2 cannot be computed from hop 1's
    # rows, so no limit can be pushed into it from here. What this route does control is
    # the four overlay tables, and it now asks each for exactly the keys it will render.
    edges = read_model.subgraph(rid, account_key, hops=depth, min_amount_minor=min_amount_minor)
    if window_start is not None or window_end is not None:
        if window_start is not None and window_end is not None and window_end < window_start:
            raise BadRequest("window_end precedes window_start; an inverted window matches nothing")
        edges = [edge for edge in edges if _in_window(edge, window_start, window_end)]

    keys = {account_key}
    for edge in edges:
        keys.add(str(edge["src_account_key"]))
        keys.add(str(edge["dst_account_key"]))
    depths = _hop_depths(account_key, edges)

    memberships = _select_keys(
        read_model,
        "account_membership",
        rid,
        "account_key",
        keys,
        columns=MEMBERSHIP_COLUMNS,
    )
    by_key = {str(row["account_key"]): row for row in memberships}

    truncated = len(keys) > cap
    collapsed_specs: list[tuple[int, list[str]]] = []
    cut_note: str | None = None
    if truncated:
        # Below the cap every reachable account is a node. Above it, whole communities
        # become meta-nodes, and the cap is a ceiling rather than a caption.
        included, collapsed_specs, dropped = _collapse(
            keys=keys,
            by_key=by_key,
            cap=cap,
            seed=account_key,
            depths=depths,
        )
        if dropped:
            cut_note = (
                f"{dropped} reachable accounts sit in communities that are under the cap "
                "individually but together exceed it, so the farthest ones by hop count were "
                "not rendered; the seed is always rendered."
            )
    else:
        included = set(keys)

    # Only the communities this response names: the collapsed ones, whose meta-nodes
    # carry their stored size, and the communities of the kept nodes, for the density
    # overlay. ``community`` is one row per community in the whole run, which is tens of
    # thousands of rows on the IBM corpus and irrelevant to a 1,500-node picture.
    needed_communities: set[int] = {index for index, _ in collapsed_specs}
    needed_communities.update(int(by_key[key]["community_id"]) for key in included if key in by_key)
    communities = _select_keys(
        read_model,
        "community",
        rid,
        "canonical_index",
        needed_communities,
        columns=COMMUNITY_COLUMNS,
        coerce=int,
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

    # ``read_model.money`` is the only money renderer used here on purpose. Config's
    # ``minor_units_per_major`` is a BASE (100) and ``Money.decimals`` is the EXPONENT
    # whose base is ten; the router that passed the former into the latter divided every
    # figure it served by 10**100. Going through the read model means this file never
    # gets to state a scale at all.
    score_by_key = {
        str(row["account_key"]): row
        for row in _select_keys(
            read_model, "score", rid, "account_key", included, columns=SCORE_COLUMNS
        )
    }
    money_by_key = {
        str(row["account_key"]): row
        for row in _select_keys(
            read_model, "economics", rid, "account_key", included, columns=ECONOMICS_COLUMNS
        )
    }
    nodes = [
        NetworkNode(
            id=key,
            label=key,
            band=None if key not in score_by_key else str(score_by_key[key]["band"]),
            exposure=None
            if key not in money_by_key
            else read_model.money(
                int(money_by_key[key]["exposure_minor"]), money_by_key[key]["currency"]
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
            total=read_model.money(int(edge["total_minor"]), edge["currency"]),
            txn_count=int(edge["txn_count"]),
            first_ts=edge["first_ts"],
            last_ts=edge["last_ts"],
            flags=list(edge.get("flags") or []),
        )
        for edge in sorted(
            # The traversal hands back what ``edges_for`` ordered by ``total_minor``, which
            # is not a total order the moment two edges move the same amount — and on
            # Postgres that tie is resolved by the plan, not by the data. ``(source,
            # target)`` is unique per run by ``uq_graph_edge``, so this list has one order.
            (
                edge
                for edge in edges
                if edge["src_account_key"] in included and edge["dst_account_key"] in included
            ),
            key=lambda edge: (str(edge["src_account_key"]), str(edge["dst_account_key"])),
        )
    ]
    collapsed = [
        _meta_node(read_model, index, members, community_by_index)
        for index, members in collapsed_specs
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
        else f"{len(keys)} nodes within {depth} hops exceeded the configured cap of {cap}: "
        f"{len(collapsed)} communities became meta-nodes carrying their true member counts, "
        f"{len(nodes)} account nodes are rendered"
        + (f"; {cut_note}" if cut_note is not None else ""),
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
        # ``flags`` is a set, because the same overlay name can arrive twice from two
        # edges; ``append`` on a set raised AttributeError for every node with a D/E band,
        # which made `GET /api/graph/subgraph` a 500 for exactly the accounts an analyst
        # opens first -- the flagged ones.
        flags.add("flagged")
    return sorted(flags)


def _select_keys(
    read_model: Any,
    table: str,
    run_id: str,
    column: str,
    values: Any,
    *,
    columns: Sequence[str],
    coerce: Callable[[Any], Any] = str,
) -> list[dict[str, Any]]:
    """One table, filtered to ``values``, projected to the columns this route renders.

    The whole-run form of this read — ``where={"run_id": rid}`` and nothing else — is
    what made a 1,500-node page materialise every account the run scored. Filtering and
    projecting are pushed into the query instead, and the key set is chunked so a raised
    ``subgraph_node_cap`` cannot turn one predicate into ten thousand bind parameters.
    """
    wanted = sorted({coerce(value) for value in values})
    if not wanted:
        return []
    rows: list[dict[str, Any]] = []
    for start in range(0, len(wanted), KEY_CHUNK):
        part, _total = read_model.source.select(
            table,
            where={"run_id": run_id, column: tuple(wanted[start : start + KEY_CHUNK])},
            columns=list(columns),
            allow_missing=True,
        )
        rows.extend(part)
    return rows


def _hop_depths(seed: str, edges: list[dict[str, Any]]) -> dict[str, int]:
    """Shortest stored-edge distance from the seed, for the order the cap cuts in.

    Computed here rather than read: the read model returns a flat edge list, and which
    accounts to drop when the graph is too wide is a decision that should prefer the
    near ones. A node with no path left in the window sorts as farthest.
    """
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        src = str(edge["src_account_key"])
        dst = str(edge["dst_account_key"])
        adjacency.setdefault(src, set()).add(dst)
        adjacency.setdefault(dst, set()).add(src)
    depths: dict[str, int] = {seed: 0}
    frontier = [seed]
    for hop in range(1, MAX_HOPS + 1):
        nxt: list[str] = []
        for node in frontier:
            for other in sorted(adjacency.get(node, ())):
                if other not in depths:
                    depths[other] = hop
                    nxt.append(other)
        frontier = sorted(nxt)
    return depths


def _meta_node(
    read_model: Any,
    index: int,
    members: list[str],
    community_by_index: dict[int, dict[str, Any]],
) -> CommunityMetaNode:
    """One collapsed community, labelled with the size the pipeline stored for it.

    Both numbers a meta-node asserts are stored ones. ``member_count`` used to fall back
    to the count of members *this traversal reached*, which is a smaller number than the
    community's size and is presented as "the true member count"; and the currency used
    to default to ``UGX``, so a run priced in another currency rendered a plausible wrong
    one. A community row that is absent or unpriced is a broken handoff, so it is a
    labelled 503 naming the column rather than a rendered guess.
    """
    row = community_by_index.get(index)
    if row is None or row.get("size") is None:
        raise DependencyUnavailable(
            f"community {index} has account members in account_membership but no row (or no "
            f"size) in community for this run, so its meta-node cannot state a true member "
            f"count. The collapse reports {len(members)} reachable members as nothing rather "
            "than as the community's size."
        )
    total_minor = row.get("total_minor")
    return CommunityMetaNode(
        community_id=index,
        member_count=int(row["size"]),
        total=None if total_minor is None else read_model.money(total_minor, row.get("currency")),
        representative_account_key=members[0],
    )


def _collapse(
    *,
    keys: set[str],
    by_key: dict[str, dict[str, Any]],
    cap: int,
    seed: str,
    depths: Mapping[str, int],
) -> tuple[set[str], list[tuple[int, list[str]]], int]:
    """Fit the view under ``cap`` by collapsing whole communities, largest first.

    Returns the accounts to render, the collapsed ``(community index, members)`` pairs in
    render order, and how many accounts the cut dropped. The rule that used to live here
    seeded the inclusion set with the seed's entire community *without testing the cap*,
    which made ``truncated=True`` and ``len(nodes) > node_cap`` reachable at once: the
    response reported a ceiling it had not applied.

    So the cap is checked against the projected node count after every collapse, and the
    seed's own community — which the graph layer's policy never collapses, because it *is*
    the answer to the question asked — is excluded from collapsing like it. When that still
    leaves the view over the cap, the excess is cut deterministically (farthest hops first,
    then account key) and the number cut is returned for the reason string. Nothing about
    the cut is silent, and the cap holds either way.
    """
    by_community: dict[int, list[str]] = {}
    for key in keys:
        membership = by_key.get(key)
        if membership is None:
            continue
        by_community.setdefault(int(membership["community_id"]), []).append(key)

    seed_community = (by_key.get(seed) or {}).get("community_id")
    ordered = sorted(
        (
            (index, sorted(members))
            for index, members in by_community.items()
            if index != seed_community and len(members) >= MIN_MEMBERS_TO_COLLAPSE
        ),
        key=lambda item: (-len(item[1]), item[0]),
    )
    collapsed: list[tuple[int, list[str]]] = []
    projected = len(keys)
    for index, members in ordered:
        if projected <= cap:
            break
        collapsed.append((index, members))
        projected -= len(members) - 1

    collapsed_indexes = {index for index, _ in collapsed}
    included = {
        key
        for key in keys
        if key == seed
        or key not in by_key
        or int(by_key[key]["community_id"]) not in collapsed_indexes
    }
    room = max(cap - len(collapsed), 1)
    dropped = 0
    if len(included) > room:
        # Deterministic and reported: nearest to the seed survives, ties broken by key.
        kept = sorted(included, key=lambda key: (depths.get(key, MAX_HOPS + 1), key))[:room]
        dropped = len(included) - len(kept)
        included = set(kept)
    return included, collapsed, dropped


__all__ = ["MAX_HOPS", "router"]
