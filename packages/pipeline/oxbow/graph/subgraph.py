"""Subgraph serving: the explorer's view, capped on the server, never on the client.

``graph.subgraph_node_cap`` is 1,500 and the reason is in the sentence above it in
the config: beyond that a rendered graph is a hairball and the browser dies. The
naive fix — ``head(1500)`` — is worse than the problem, because it silently deletes
accounts from a fraud exhibit and the viewer cannot tell absence from innocence.

So the collapse is structural instead: a community that will not fit becomes one
meta-node, **labelled with its true member count**, that the explorer can expand on
click. The reader sees "412 accounts" where the alternative was seeing 1,500 of them
and quietly not seeing the rest. Rules of the collapse, all of them visible on the
returned :class:`Subgraph`:

* the community containing the seed account is never collapsed — it is the answer
  to the question that was asked;
* whole communities only, never a partial one, largest first, so two calls return
  the same picture;
* edges that touched a collapsed member are re-pointed at the meta-node and
  re-aggregated **per currency**, because summing across a currency boundary is
  forbidden everywhere else in this layer and a rendering shortcut is not an
  exception;
* edges inside a collapsed community become a self-loop on the meta-node carrying
  their count, so the cluster's internal activity survives as a number rather than
  as four hundred dots;
* a meta-node's structural features stay null. An averaged degree is a number
  nobody observed, and 03 A rule 2 forbids turning an unknown into a figure.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import polars as pl

from oxbow.graph.errors import UnknownAccountError
from oxbow.graph.model import NODE_TYPE_RAIL, AccountGraph

META_NODE_PREFIX: Final = "community:"
NODE_KIND_ACCOUNT: Final = "account"
NODE_KIND_COMMUNITY: Final = "community"
# A community of one gains nothing by collapsing: one node for one node, and the
# reader loses the account key that was there.
MIN_MEMBERS_TO_COLLAPSE: Final = 2


@dataclass(frozen=True, slots=True)
class MetaNode:
    """A collapsed community, carrying the size it stands in for."""

    node_id: str
    community_id: int
    member_count: int
    representative_account: str
    expandable: bool
    collapse_reason: str

    @property
    def label(self) -> str:
        """The text the explorer renders. The true size is not optional."""
        return f"{self.node_id} ({self.member_count} accounts)"


@dataclass(frozen=True, slots=True)
class Subgraph:
    """A capped, deterministic, self-describing view of the graph."""

    seed: str
    hops: int
    cap: int
    nodes: pl.DataFrame
    edges: pl.DataFrame
    meta_nodes: tuple[MetaNode, ...]
    truncated: bool
    truncation_reason: str | None
    reachable_before_cap: int

    @property
    def node_count(self) -> int:
        return self.nodes.height

    @property
    def within_cap(self) -> bool:
        return self.node_count <= self.cap

    @property
    def collapsed_community_ids(self) -> tuple[int, ...]:
        return tuple(meta.community_id for meta in self.meta_nodes)

    @property
    def collapsed_member_count(self) -> int:
        return sum(meta.member_count for meta in self.meta_nodes)

    def expand(self, graph: AccountGraph, community_id: int) -> tuple[str, ...]:
        """Members of a collapsed community, for the click-through.

        Read from the authoritative node table rather than carried inside the view,
        so an expanded cluster is always exactly the set of accounts the graph
        recorded: the collapse is a rendering decision, never a data decision.
        """
        members = graph.nodes.filter(pl.col("community_id") == community_id)["account"].to_list()
        if not members:
            raise UnknownAccountError(
                f"community {community_id} has no members in this graph; a meta-node "
                "that expands to nothing is a stale artifact, not an empty cluster"
            )
        return tuple(sorted(str(account) for account in members))


def neighbourhood(
    graph: AccountGraph,
    account: str,
    *,
    hops: int | None = None,
    cap: int | None = None,
) -> Subgraph:
    """Bounded-radius neighbourhood of ``account``, capped to ``subgraph_node_cap``.

    ``hops`` defaults to ``graph.default_hops``. Rails are included as endpoints and
    never traversed *through*: a rail in the middle of a path connects two strangers
    who never met, and expanding through it turns a merchant into a bridge across an
    entire customer base.
    """
    if account not in graph.node_types:
        raise UnknownAccountError(
            f"account {account!r} has no events in this graph. An empty neighbourhood "
            "would render as 'this account is connected to nobody', which is a claim "
            "about the absence of evidence stated as evidence of absence."
        )
    depth = graph.settings.default_hops if hops is None else hops
    limit = graph.settings.subgraph_node_cap if cap is None else cap

    depths: dict[str, int] = {account: 0}
    frontier = [account]
    for hop in range(max(depth, 0)):
        nxt: list[str] = []
        for node in sorted(frontier):
            if node != account and graph.node_types.get(node) == NODE_TYPE_RAIL:
                continue
            for partner in sorted(graph.simple_neighbours.get(node, frozenset())):
                if partner not in depths:
                    depths[partner] = hop + 1
                    nxt.append(partner)
        frontier = nxt

    members = sorted(depths)
    edges = _edges_between(graph, members)
    return _serve(graph, members, depths, edges, account, limit, depth)


def _edges_between(graph: AccountGraph, members: list[str]) -> pl.DataFrame:
    """Every parallel edge with both endpoints inside the view, at event granularity.

    Kept per event because the point of the explorer is the burst: forty transfers
    between one pair must render as forty marks or as a count of forty, never as one
    anonymous line.
    """
    wanted = sorted(members)
    columns = list(graph.events.frame.columns)
    return graph.events.frame.select(columns).filter(
        pl.col("account_from").is_in(wanted) & pl.col("account_to").is_in(wanted)
    )


def _serve(
    graph: AccountGraph,
    members: list[str],
    depths: Mapping[str, int],
    edges: pl.DataFrame,
    seed: str,
    cap: int,
    hops: int,
) -> Subgraph:
    """Enforce the node cap by collapsing whole communities, or say why it cannot."""
    reachable = len(members)
    if reachable <= cap:
        return Subgraph(
            seed=seed,
            hops=hops,
            cap=cap,
            nodes=_node_table(graph, members, depths, {}),
            edges=edges,
            meta_nodes=(),
            truncated=False,
            truncation_reason=None,
            reachable_before_cap=reachable,
        )

    community_of = _community_of(graph)
    seed_community = community_of.get(seed)
    grouped: dict[int, list[str]] = {}
    for account in members:
        community = community_of.get(account)
        if community is None or community == seed_community:
            continue
        grouped.setdefault(community, []).append(account)

    # Largest first, then by community id: collapsing the biggest cluster buys the
    # most headroom per meta-node, and the order is a function of the partition
    # rather than of the iteration that found it.
    ordered = sorted(
        (
            (community, accounts)
            for community, accounts in grouped.items()
            if len(accounts) >= MIN_MEMBERS_TO_COLLAPSE
        ),
        key=lambda item: (-len(item[1]), item[0]),
    )
    collapsed: dict[int, list[str]] = {}
    for community, accounts in ordered:
        if _projected_nodes(members, collapsed) <= cap:
            break
        collapsed[community] = accounts

    reason: str | None = None
    kept = [
        account
        for account in members
        if account == seed or community_of.get(account) not in collapsed
    ]
    if len(kept) + len(collapsed) > cap:
        # Every eligible community is collapsed and the view is still too wide: a
        # deterministic cut, reported as truncated instead of passed off as whole.
        room = max(cap - len(collapsed), 1)
        kept = sorted(kept, key=lambda account: (depths.get(account, 0), account))[:room]
        reason = (
            f"deterministic head({room}) after collapsing {len(collapsed)} communities; "
            f"{reachable - room - sum(len(v) for v in collapsed.values())} reachable "
            "accounts are not rendered"
        )
    elif collapsed:
        reason = (
            f"{reachable} accounts reachable, cap {cap}: {len(collapsed)} communities "
            f"({sum(len(v) for v in collapsed.values())} accounts) collapsed into "
            "expandable meta-nodes labelled with their true size"
        )

    meta_nodes = tuple(
        MetaNode(
            node_id=f"{META_NODE_PREFIX}{community}",
            community_id=community,
            member_count=len(accounts),
            representative_account=min(accounts),
            expandable=True,
            collapse_reason=(
                "over graph.subgraph_node_cap; the community containing the seed "
                "account is never collapsed"
            ),
        )
        for community, accounts in sorted(collapsed.items())
        if community_of.get(seed) != community
    )

    return Subgraph(
        seed=seed,
        hops=hops,
        cap=cap,
        nodes=_node_table(graph, kept, depths, collapsed),
        edges=_repoint_edges(edges, community_of, collapsed, kept),
        meta_nodes=meta_nodes,
        truncated=True,
        truncation_reason=reason,
        reachable_before_cap=reachable,
    )


def _projected_nodes(members: list[str], collapsed: Mapping[int, list[str]]) -> int:
    """How many nodes the view would hold with the current set of collapses."""
    hidden = sum(len(accounts) for accounts in collapsed.values())
    return len(members) - hidden + len(collapsed)


def _community_of(graph: AccountGraph) -> dict[str, int | None]:
    return {
        str(account): (None if community is None else int(community))
        for account, community in zip(
            graph.nodes["account"].to_list(),
            graph.nodes["community_id"].to_list(),
            strict=True,
        )
    }


def _node_table(
    graph: AccountGraph,
    accounts: list[str],
    depths: Mapping[str, int],
    collapsed: Mapping[int, list[str]],
) -> pl.DataFrame:
    """The served account rows, plus one row per meta-node carrying its true size."""
    wanted = sorted(set(accounts))
    rows = (
        graph.nodes.filter(pl.col("account").is_in(wanted))
        .with_columns(
            pl.Series("hop", [int(depths.get(account, 0)) for account in wanted], pl.Int64),
            pl.lit(NODE_KIND_ACCOUNT).alias("node_kind"),
            pl.lit(None).cast(pl.Int64).alias("member_count"),
            pl.lit(False).alias("expandable"),
        )
        .sort("account")
    )
    if not collapsed:
        return rows
    communities = sorted(collapsed)
    meta = pl.DataFrame(
        {
            "account": [f"{META_NODE_PREFIX}{community}" for community in communities],
            "hop": [
                min(int(depths.get(account, 0)) for account in collapsed[community])
                for community in communities
            ],
            "member_count": [len(collapsed[community]) for community in communities],
            "expandable": [True] * len(communities),
            "node_kind": [NODE_KIND_COMMUNITY] * len(communities),
            "community_id": communities,
        }
    )
    missing = [name for name in rows.columns if name not in meta.columns]
    meta = meta.with_columns(
        pl.lit(None).cast(rows.schema[name]).alias(name) for name in missing
    ).select(rows.columns)
    return pl.concat([rows, meta], how="vertical_relaxed").sort(["node_kind", "account"])


def _repoint_edges(
    edges: pl.DataFrame,
    community_of: Mapping[str, int | None],
    collapsed: Mapping[int, list[str]],
    served_accounts: list[str],
) -> pl.DataFrame:
    """Fold edges that touched a collapsed member into edges that touch its meta-node.

    Re-aggregated per ``(from, to, currency)`` with a count and a per-currency total,
    so a meta-edge states how much moved in which currency and never one number
    across two.
    """
    served = sorted(set(served_accounts))
    if not collapsed:
        return edges.filter(
            pl.col("account_from").is_in(served) & pl.col("account_to").is_in(served)
        )
    endpoints: dict[str, str] = {}
    for community, accounts in collapsed.items():
        target = f"{META_NODE_PREFIX}{community}"
        for account in accounts:
            endpoints[account] = target
    del community_of  # the map is used by callers; folding needs only the endpoints
    folded = edges.with_columns(
        pl.col("account_from")
        .replace_strict(endpoints, return_dtype=pl.Utf8, default=pl.col("account_from"))
        .alias("account_from"),
        pl.col("account_to")
        .replace_strict(endpoints, return_dtype=pl.Utf8, default=pl.col("account_to"))
        .alias("account_to"),
    )
    visible = sorted(set(served) | set(endpoints.values()))
    return (
        folded.filter(pl.col("account_from").is_in(visible) & pl.col("account_to").is_in(visible))
        .group_by("account_from", "account_to", "currency")
        .agg(
            pl.len().alias("edge_count"),
            pl.col("amount_minor").sum().alias("total_value_minor"),
            pl.col("amount_minor").min().alias("min_amount_minor"),
            pl.col("amount_minor").max().alias("max_amount_minor"),
            pl.col("ts_us").min().alias("first_ts_us"),
            pl.col("ts_us").max().alias("last_ts_us"),
        )
        .sort(["account_from", "account_to", "currency"])
    )


__all__ = [
    "META_NODE_PREFIX",
    "MIN_MEMBERS_TO_COLLAPSE",
    "NODE_KIND_ACCOUNT",
    "NODE_KIND_COMMUNITY",
    "MetaNode",
    "Subgraph",
    "neighbourhood",
]
