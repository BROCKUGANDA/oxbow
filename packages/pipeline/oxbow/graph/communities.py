"""Leiden communities, fixed seed, fixed iterations, canonically relabelled.

Three properties, and the third is the one that is easy to get wrong:

1. **igraph + leidenalg, not a NetworkX approximation.** NetworkX has no Leiden.
   The conversion is done once per build and thrown away, because the artifact is
   the community *label per account*, not the partition object.
2. **Seed and ``n_iterations`` are both pinned** (``graph.community``). Leiden is
   stochastic — repeated local moves — so an unpinned seed means the same account
   gets a different community between runs, which means its explanation changes
   for a reason nobody can state. Fixing the seed but not the iteration count has
   the same effect through a different door.
3. **Community ids are remapped to a canonical order: size descending, then the
   smallest node key ascending.** leidenalg's own indices depend on the order the
   algorithm happens to merge clusters, so "community 3" is not stable even with a
   fixed seed if the node population shifts by one row. Size-then-min-node-key is
   a function of the partition, not of the search path, and it is the config's
   declared order (``canonical_order: size_then_min_node_key``).

Nodes excluded from the graph — singletons — get no community, and the reason is
recorded in :class:`~oxbow.graph.model.CommunityResult` rather than left as a bare
null. On a star-shaped corpus (DEV-011) the honest result is *no communities at
all*, and a build that quietly reported zero instead of saying why is the failure
this module is written against.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import igraph
import leidenalg

from oxbow.graph.errors import GraphConfigError
from oxbow.graph.settings import CPM_OBJECTIVE, CommunitySettings

# Below these the partition is not interesting and leidenalg is not happy: a
# single node has no modularity to optimise.
MIN_NODES_FOR_COMMUNITIES: Final[int] = 2


@dataclass(frozen=True, slots=True)
class CommunityResult:
    """Community id per account, plus the reason when there is no id to give."""

    ids: Mapping[str, int]
    community_count: int
    skipped_reason: str | None

    @property
    def sizes(self) -> Mapping[int, int]:
        counts: dict[int, int] = {}
        for community in self.ids.values():
            counts[community] = counts.get(community, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    @property
    def members_by_community(self) -> Mapping[int, tuple[str, ...]]:
        grouped: dict[int, list[str]] = {}
        for account, community in self.ids.items():
            grouped.setdefault(community, []).append(account)
        return {key: tuple(sorted(grouped[key])) for key in sorted(grouped)}


def build_igraph(
    nodes: Sequence[str], weighted_pairs: Iterable[tuple[str, str, int]]
) -> igraph.Graph:
    """Undirected weighted igraph over ``nodes``, with a stable vertex order.

    Undirected on purpose: "who clusters with whom" is a question about
    connectivity, and directed modularity would make a merchant's inbound and
    outbound bookkeeping decide the community. Vertex names are assigned in sorted
    order so the same node population always produces the same igraph, which is
    what lets a fixed seed mean anything.

    A pair carrying zero edges is dropped rather than added as a zero-weight edge:
    leidenalg treats a zero weight as "no evidence", and stating it twice would be
    a rounding decision nobody signed off on.
    """
    index = {account: position for position, account in enumerate(sorted(nodes))}
    graph = igraph.Graph(n=len(index), directed=False)
    graph.vs["name"] = sorted(index)
    edges: list[tuple[int, int]] = []
    weights: list[float] = []
    for left, right, count in weighted_pairs:
        if left == right or count <= 0:
            continue
        if left not in index or right not in index:
            raise GraphConfigError(
                f"community input referenced {left!r}/{right!r}, which is outside the "
                "node population. A silent skip would change the partition without "
                "saying so."
            )
        edges.append((index[left], index[right]))
        weights.append(float(count))
    if edges:
        graph.add_edges(edges)
        graph.es["weight"] = weights
        # Merge any duplicate pair into one weighted edge: multiplicity belongs in
        # the weight, and leidenalg's modularity on a multi-edge igraph would count
        # the same relationship twice in a way the reader cannot see.
        graph.simplify(combine_edges={"weight": "sum"})
    return graph


def detect_communities(
    nodes: Sequence[str],
    weighted_pairs: Iterable[tuple[str, str, int]],
    settings: CommunitySettings,
) -> CommunityResult:
    """Run Leiden and return canonically ordered ids, or a reason it did not run."""
    population = sorted(set(nodes))
    allowed = frozenset(population)
    usable = [
        (left, right, count)
        for left, right, count in weighted_pairs
        if left != right and left in allowed and right in allowed
    ]
    if len(population) < MIN_NODES_FOR_COMMUNITIES:
        return CommunityResult(
            ids={},
            community_count=0,
            skipped_reason=(
                f"fewer than {MIN_NODES_FOR_COMMUNITIES} accounts remain after excluding "
                "singletons, so there is no partition to compute"
            ),
        )
    if not usable:
        return CommunityResult(
            ids={},
            community_count=0,
            skipped_reason=(
                "no edges remain between the non-singleton accounts. On a star-shaped "
                "corpus this is the expected result, and it is a measurement rather than "
                "a failure (DEV-011)."
            ),
        )

    graph = build_igraph(population, usable)
    if settings.objective_function == CPM_OBJECTIVE:
        partition = leidenalg.find_partition(
            graph,
            leidenalg.CPMVertexPartition,
            weights="weight",
            seed=settings.seed,
            n_iterations=settings.n_iterations,
            resolution_parameter=settings.resolution,
        )
    else:
        # Modularity ignores a resolution parameter, and settings.py refuses a
        # non-1.0 resolution under this objective rather than let it look effective.
        partition = leidenalg.find_partition(
            graph,
            leidenalg.ModularityVertexPartition,
            weights="weight",
            seed=settings.seed,
            n_iterations=settings.n_iterations,
        )

    labels = graph.vs["name"]
    grouped: dict[int, list[str]] = {}
    for position, raw_community in enumerate(partition.membership):
        community = int(raw_community)
        name = str(labels[position])
        grouped.setdefault(community, []).append(name)

    # Canonical order: bigger first, then the smallest node key inside the
    # community breaks a tie. Two communities of the same size are distinguished by
    # content, never by the order the algorithm found them.
    ordered = sorted(
        grouped.values(), key=lambda members: (-len(members), min(members) if members else "")
    )
    ids: dict[str, int] = {}
    for new_id, members in enumerate(ordered):
        for member in members:
            ids[member] = new_id
    return CommunityResult(ids=ids, community_count=len(ordered), skipped_reason=None)


__all__ = ["MIN_NODES_FOR_COMMUNITIES", "CommunityResult", "build_igraph", "detect_communities"]
