"""OXBOW graph layer: a directed, time-stamped multigraph over canonical events.

Built before the feature layer, because a degree cannot be computed from a graph
that does not exist yet (DEV-008). Every tunable this layer obeys lives in
``config/pipeline.yaml`` under ``graph:``; see :mod:`oxbow.graph.settings`.

The public surface, and what each piece is for:

``build_graph(events, cfg)``
    Canonical event v1 in, :class:`~oxbow.graph.model.AccountGraph` out. Parallel
    edges retained, rails and externals typed, singletons excluded from aggregates
    and counted, self-transfers retained as events and excluded from every adjacency
    structure and counted (by row and by per-currency value), communities found with
    a fixed seed, cycles enumerated inside a hard budget.
``degree_measurement(events, cfg)``
    The same degree and typing maths without traversal structures — the only path
    that runs over a full corpus, and the one the day-3 gate prints from.
``neighbourhood(graph, account, hops=...)``
    The explorer's query: bounded hops, rails as stoppers, capped nodes, collapsed
    communities labelled with their true size and expandable.
``write_graph`` / ``load_graph``
    The Parquet artifact the API reads instead of rebuilding, verified against its
    own manifest.

One design stance is worth stating where an importer will read it. This layer is
corpus-agnostic and it is honest, and DEV-011 is why both matter: PaySim measured a
median account degree of 1.0 with zero surviving time-respecting cycles, so on that
corpus the correct output is a graph that reports empty aggregates, named reasons,
and no manufactured structure. On IBM-AML, which does have multi-account topology,
the same code reports real communities and real loops. Neither result is cooked, and
every place the code could have invented structure instead declares why it did not.
"""

from __future__ import annotations

from oxbow.graph.build import (
    TOP_DEGREES_REPORTED,
    DegreeMeasurement,
    build_graph,
    degree_measurement,
    fingerprint,
    top_degrees,
)
from oxbow.graph.communities import CommunityResult, detect_communities
from oxbow.graph.cycles import enumerate_cycles
from oxbow.graph.errors import (
    EventContractError,
    GraphArtifactError,
    GraphConfigError,
    GraphError,
    GraphTooLargeError,
    MixedCurrencyError,
    UnknownAccountError,
)
from oxbow.graph.events import (
    CANONICAL_EVENT_V1_COLUMNS,
    REQUIRED_EVENT_COLUMNS,
    OrderedEvents,
    require_events,
    require_single_currency,
)
from oxbow.graph.features import betweenness_centrality
from oxbow.graph.model import (
    FEATURE_NULL_POLICY,
    FLOW_NULL_POLICY,
    NODE_TYPE_EXTERNAL,
    NODE_TYPE_MEMBER,
    NODE_TYPE_RAIL,
    SELF_TRANSFER_EXCLUDED_REASON,
    SINGLETON_DEGREE,
    AccountGraph,
    Cycle,
    CycleSearch,
    DegreeSummary,
    EdgeLeg,
    GraphStats,
)
from oxbow.graph.persist import GraphArtifacts, load_graph, write_graph
from oxbow.graph.settings import CycleSettings, GraphSettings, load_graph_settings
from oxbow.graph.subgraph import MetaNode, Subgraph, neighbourhood

__all__ = [
    "CANONICAL_EVENT_V1_COLUMNS",
    "FEATURE_NULL_POLICY",
    "FLOW_NULL_POLICY",
    "NODE_TYPE_EXTERNAL",
    "NODE_TYPE_MEMBER",
    "NODE_TYPE_RAIL",
    "REQUIRED_EVENT_COLUMNS",
    "SELF_TRANSFER_EXCLUDED_REASON",
    "SINGLETON_DEGREE",
    "TOP_DEGREES_REPORTED",
    "AccountGraph",
    "CommunityResult",
    "Cycle",
    "CycleSearch",
    "CycleSettings",
    "DegreeMeasurement",
    "DegreeSummary",
    "EdgeLeg",
    "EventContractError",
    "GraphArtifactError",
    "GraphArtifacts",
    "GraphConfigError",
    "GraphError",
    "GraphSettings",
    "GraphStats",
    "GraphTooLargeError",
    "MetaNode",
    "MixedCurrencyError",
    "OrderedEvents",
    "Subgraph",
    "UnknownAccountError",
    "betweenness_centrality",
    "build_graph",
    "degree_measurement",
    "detect_communities",
    "enumerate_cycles",
    "fingerprint",
    "load_graph",
    "load_graph_settings",
    "neighbourhood",
    "require_events",
    "require_single_currency",
    "top_degrees",
    "write_graph",
]
