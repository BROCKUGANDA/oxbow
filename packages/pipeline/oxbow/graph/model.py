"""The graph object and the shapes it reports.

One sentence on why this is a hand-built structure rather than "just a NetworkX
graph": the layer has to answer three different consumers at once — the feature
table wants columnar frames, the explorer wants a traversable structure, and the
audit trail wants a stats record whose every number is reproducible from the
frames. A bare ``MultiDiGraph`` serves none of those without a rebuild, and a
rebuild is precisely what the persisted artifact exists to avoid.

Node typing is the load-bearing decision in this file, because it is where the
honesty rule bites (DEV-011: PaySim's median account degree is 1.0, so most of
that corpus is *not* a network):

``member``
    An account that appears as an originator. It has both sides observable.
``external``
    An account that appears only ever as a counterparty (``account_to``). The
    corpus cannot see its outgoing behaviour at all, so origin-side features are
    **null, not zero**. A zero would claim "we watched this account send nothing",
    which is the exact error 03 A rule 2 names: never let an unknown become a zero.
``rail``
    An account whose degree sits above ``graph.rail_degree_percentile``. A
    merchant, agent or aggregator: its fan-in is the size of the economy, so it is
    excluded from fan scoring and it stops traversal — walking *through* a rail
    connects two strangers who never met.

A node can be external and rail at once; ``node_type`` records the stronger
statement (the one the explorer draws differently and the rules must obey) and
``is_external`` keeps the weaker one true.

Singletons — an account with exactly one non-self incident edge — are excluded
from every *aggregate* (communities, PageRank, local density, cycle search, the
degree summary) because a leaf carries no network information. They stay in the
node table, stay queryable in a neighbourhood, and stay scoreable by the tabular
module. Excluding them from aggregates is not deleting them.

Self-transfers — ``account_from == account_to``, a movement between two balances of
one customer — are the same kind of decision taken one level down, and they are not
singletons: an account can hold forty of them and still have no counterparty at all.
They stay events, stay in the pair table as a flagged self-pair, and are counted per
account and per currency; they contribute nothing to any traversal structure. See
:data:`SELF_TRANSFER_EXCLUDED_REASON` for the sentence the reports quote.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Final, NamedTuple

import networkx as nx
import polars as pl

from oxbow.graph.errors import EventContractError, UnknownAccountError
from oxbow.graph.events import DERIVED_SELF_COLUMN, OrderedEvents
from oxbow.graph.settings import GraphSettings

NODE_TYPE_MEMBER: Final = "member"
NODE_TYPE_EXTERNAL: Final = "external"
NODE_TYPE_RAIL: Final = "rail"

# Which columns are structurally null for each node type, and where they live.
# Written into the persisted manifest so a downstream consumer reads the policy with
# the data rather than inferring it from a schema. Split by table because the node
# table and the flow table are different grains: an account has one row in the first
# and one per currency in the second, and a consumer must be able to check the
# contract against the columns it is actually holding.
FEATURE_NULL_POLICY: Final[Mapping[str, tuple[str, ...]]] = {
    NODE_TYPE_EXTERNAL: (
        "out_degree",
        "fan_out",
        "fan_out_effective",
    ),
    NODE_TYPE_RAIL: ("fan_in", "fan_out", "fan_in_effective", "fan_out_effective"),
    NODE_TYPE_MEMBER: (),
}

# The same policy for the flow table, which is keyed by (account, currency).
FLOW_NULL_POLICY: Final[Mapping[str, tuple[str, ...]]] = {
    NODE_TYPE_EXTERNAL: (
        "outflow_total_minor",
        "outflow_count",
        "max_single_outflow_minor",
    ),
    NODE_TYPE_RAIL: (),
    NODE_TYPE_MEMBER: (),
}

# The singleton rule, stated once: one incident non-self edge is a leaf.
SINGLETON_DEGREE: Final[int] = 1

# The self-transfer rule, stated once beside it. An event whose originator and
# beneficiary are the same account moves value between two balances of one customer:
# it is real money and a real event, and it names no counterparty. So it is kept and
# counted — in the event frame, as a flagged self-pair in the aggregate table, per
# account in ``self_transfer_count``, and per currency in ``GraphStats`` — while
# contributing nothing to ``out_edges`` / ``in_edges`` / the neighbour map, and
# therefore nothing to degree, cycles, communities, PageRank, local density,
# betweenness or neighbourhood hops. Rule 01 P3 asks for exactly that split: excluded
# from cycle and fan detection *while kept as a feature*.
SELF_TRANSFER_EXCLUDED_REASON: Final[str] = (
    "self-transfers (account_from == account_to) are retained as events, aggregated as "
    "a flagged self-pair and counted per account and per currency, but excluded from "
    "degree, cycles, communities, PageRank, local density, betweenness and "
    "neighbourhood hops: A -> A moves value between two balances of one account and "
    "names no counterparty (01 P3, DEV-013)"
)


class EdgeLeg(NamedTuple):
    """One event, as the adjacency index sees it.

    Field order is the canonical total order first, so sorting a sequence of legs
    is sorting by ``(ts_us, txn_id)`` without a key function anywhere — which is
    how "every sort uses the total order" stays true by construction.
    """

    ts_us: int
    txn_id: str
    dst: str
    amount_minor: int
    currency: str
    txn_type: str
    edge_id: int


class Cycle(NamedTuple):
    """A time-respecting, value-retaining directed cycle.

    ``path`` lists the node keys in traversal (therefore time) order, starting at
    the earliest leg; the closing leg runs from ``path[-1]`` back to ``path[0]``.
    ``canonical_key`` is the same loop rotated to start at its smallest node key,
    which is the identity used to deduplicate and to sort. Rotation matters: a
    cycle has no natural first node, so without a canonical rotation two runs
    that start their search in a different order would report "different" cycles.
    """

    path: tuple[str, ...]
    canonical_key: tuple[str, ...]
    amounts_minor: tuple[int, ...]
    ts_us: tuple[int, ...]
    txn_ids: tuple[str, ...]
    currency: str
    first_amount_minor: int
    last_amount_minor: int
    value_retention: float

    @property
    def length(self) -> int:
        """Number of nodes, which for a directed cycle equals the number of legs."""
        return len(self.path)


@dataclass(frozen=True, slots=True)
class CycleSearch:
    """What the bounded cycle search came back with — including coming back early.

    ``truncated`` is a *result*, not an error. A corpus where the search hit its
    budget still has to produce a graph, and the explanation of that graph has to
    say the count is a lower bound. The alternative — no flag and a number — is
    how a pipeline manufactures confidence out of a timeout.
    """

    cycles: tuple[Cycle, ...]
    truncated: bool
    truncation_reason: str | None
    visits: int
    components_searched: int
    rails_excluded: int
    zero_value_loops_rejected: int

    @property
    def count(self) -> int:
        return len(self.cycles)

    @property
    def participating_nodes(self) -> frozenset[str]:
        return frozenset(node for cycle in self.cycles for node in cycle.path)


@dataclass(frozen=True, slots=True)
class DegreeSummary:
    """The 00 §D day-3 gate's degree distribution, over one node population."""

    accounts: int
    median: float
    p90: float
    p99: float
    max: int


@dataclass(frozen=True, slots=True)
class GraphStats:
    """Every count the build produced, including the ones that are awkward.

    The ``*_skipped_reason`` fields exist because a null community column with no
    stated cause is indistinguishable from a bug: on a star-shaped corpus the
    honest output is "no community was computed, because after singletons are
    excluded there are no edges left", and that sentence belongs in the artifact.

    ``self_transfer_count`` and ``self_transfer_value_minor`` are the same idea
    applied to the rows this layer excludes from traversal rather than refuses: a
    report has to be able to say "11.6 % of this corpus's rows are self-transfers,
    they were kept, counted and left out of the cycles and the degrees, and here is
    how many and how much". On a corpus with none, both read as zero and empty.
    """

    event_count: int
    self_transfer_count: int
    self_transfer_value_minor: tuple[tuple[str, int], ...]
    edge_count: int
    pair_count: int
    node_count: int
    member_count: int
    external_count: int
    rail_count: int
    singleton_count: int
    in_graph_node_count: int
    rail_degree_threshold: int
    rail_degree_percentile: float
    currencies: tuple[str, ...]
    cycle_count: int
    cycle_search_truncated: bool
    cycle_search_reason: str | None
    cycle_visits: int
    cycle_nodes: int
    community_count: int
    community_skipped_reason: str | None
    pagerank_skipped_reason: str | None
    degree_all_nodes: DegreeSummary
    degree_in_graph: DegreeSummary
    top_degrees: tuple[tuple[str, int], ...] = ()
    settings_fingerprint: Mapping[str, object] = field(default_factory=dict)

    def as_json_dict(self) -> dict[str, object]:
        """Plain, sorted-key-ready data for the persisted manifest.

        No wall-clock anywhere in here, by construction: every field is a
        function of the events and the config, which is what lets
        ``make verify-determinism`` compare two runs' bytes.
        """
        payload = asdict(self)
        payload["currencies"] = list(self.currencies)
        payload["cycle_nodes"] = self.cycle_nodes
        payload["self_transfer_value_minor"] = [
            [str(currency), int(value)] for currency, value in self.self_transfer_value_minor
        ]
        payload["top_degrees"] = [[str(key), int(count)] for key, count in self.top_degrees]
        payload["degree_all_nodes"] = asdict(self.degree_all_nodes)
        payload["degree_in_graph"] = asdict(self.degree_in_graph)
        payload["settings_fingerprint"] = dict(self.settings_fingerprint)
        return payload

    @property
    def self_transfer_exclusion_reason(self) -> str | None:
        """Why the self-transfers are counted rather than used, or None if there
        were none to exclude.

        A property, not a field: the sentence is a rule of this layer and identical
        on every corpus, so writing it into every manifest would change artifact
        bytes for a corpus that gained no self-transfer rows. A report quotes it
        when :attr:`self_transfer_count` is non-zero, which is the only condition
        under which the exclusion did anything.
        """
        return SELF_TRANSFER_EXCLUDED_REASON if self.self_transfer_count else None


@dataclass(frozen=True, slots=True)
class AccountGraph:
    """The built graph: frames for the bulk path, structures for traversal.

    ``out_edges`` / ``in_edges`` hold every parallel edge per ordered *counterparty*
    pair, in the canonical total order — 40 transfers between one pair are 40 legs,
    never one (P3a: a burst of 40 transfers is not one transfer). Self-transfers are
    not in them: an ``A -> A`` event is retained in ``events``, ``pairs`` and the
    stats, and it enters no adjacency structure, so nothing that walks can mistake it
    for a relationship (see :data:`SELF_TRANSFER_EXCLUDED_REASON`).
    ``simple_neighbours`` is the undirected distinct-counterparty view, which is what
    degree, density and community use, because "who do they deal with" is a question
    about people, not about message counts.
    """

    settings: GraphSettings
    events: OrderedEvents
    nodes: pl.DataFrame
    pairs: pl.DataFrame
    flows: pl.DataFrame
    search: CycleSearch
    multigraph: nx.MultiDiGraph
    stats: GraphStats
    out_edges: Mapping[str, tuple[EdgeLeg, ...]]
    in_edges: Mapping[str, tuple[EdgeLeg, ...]]
    simple_neighbours: Mapping[str, frozenset[str]]
    node_types: Mapping[str, str]
    graph_nodes: frozenset[str]

    # --- lookups ---------------------------------------------------------
    def node_row(self, account: str) -> pl.DataFrame:
        if account not in self.node_types:
            raise UnknownAccountError(
                f"account {account!r} has no events in this graph. Refusing to answer "
                "with an empty row: 'no data' and 'no network' are different statements "
                "and the explorer must not be able to confuse them."
            )
        return self.nodes.filter(pl.col("account") == account)

    def degree_of(self, account: str) -> int:
        rows = self.node_row(account)["total_degree"]
        return int(rows[0]) if rows.len() else 0

    def neighbours(self, account: str) -> tuple[str, ...]:
        """Distinct counterparties, sorted. Self-transfers are not counterparties."""
        self.node_row(account)
        return tuple(sorted(self.simple_neighbours.get(account, frozenset())))

    def is_rail(self, account: str) -> bool:
        return self.node_types.get(account) == NODE_TYPE_RAIL

    def pair_totals(self, account_from: str, account_to: str) -> dict[str, int]:
        """Value moved between a pair, keyed by currency. Never one summed number."""
        rows = self.pairs.filter(
            (pl.col("account_from") == account_from) & (pl.col("account_to") == account_to)
        )
        return {
            str(currency): int(total)
            for currency, total in rows.select(["currency", "total_value_minor"]).iter_rows()
        }

    def self_transfer_count(self, account: str) -> int:
        self.node_row(account)
        counts = self.events.frame.filter(
            pl.col(DERIVED_SELF_COLUMN) & (pl.col("account_from") == account)
        ).height
        return int(counts)

    # --- time-windowed flow, the exposure primitive ----------------------
    def flow_window(
        self,
        accounts: Iterable[str] | None = None,
        *,
        window_start_us: int,
        window_end_us: int,
    ) -> pl.DataFrame:
        """Inbound and outbound value per account per currency inside a window.

        This is the primitive behind exposure E_i (spec 3.2): what left an account
        inside the recovery window, against what arrived in the same window. The
        window is half-open on the left and closed on the right, and the caller
        supplies the trigger instant — this layer deliberately does not decide what
        counts as a triggering event, because that is a rule, not a structure.
        """
        wanted = list(self.node_types) if accounts is None else sorted(set(accounts))
        for account in wanted:
            if account not in self.node_types:
                raise UnknownAccountError(f"account {account!r} is not in this graph")
        legs = self.events.frame.filter(
            (pl.col("ts_us") >= window_start_us)
            & (pl.col("ts_us") <= window_end_us)
            & ~pl.col(DERIVED_SELF_COLUMN)
        )
        outbound = (
            legs.filter(pl.col("account_from").is_in(wanted))
            .group_by("account_from", "currency")
            .agg(
                pl.col("amount_minor").sum().alias("outflow_minor"),
                pl.len().alias("outflow_count"),
            )
            .rename({"account_from": "account"})
        )
        inbound = (
            legs.filter(pl.col("account_to").is_in(wanted))
            .group_by("account_to", "currency")
            .agg(
                pl.col("amount_minor").sum().alias("inflow_minor"),
                pl.len().alias("inflow_count"),
            )
            .rename({"account_to": "account"})
        )
        joined = inbound.join(outbound, on=["account", "currency"], how="full")
        return joined.sort(["account", "currency"]) if joined.height else _empty_flow()

    def downstream(self, account: str, hops: int) -> tuple[str, ...]:
        """Outward-reachable accounts within ``hops``, rails stopping the walk.

        ``exposure.downstream_hops`` is read through this: a rail in the middle of
        a chain is not a hop towards a person, so expanding through it would
        inflate an exposure estimate into the whole customer base of a merchant.
        The walk reads ``out_edges``, which carries no self-transfer legs, so an
        account is never its own downstream — ``A -> A`` reaches nobody but A.
        """
        self.node_row(account)
        reached: set[str] = set()
        frontier = [account]
        for _ in range(max(hops, 0)):
            nxt: list[str] = []
            for node in frontier:
                if node != account and self.is_rail(node):
                    continue  # a rail is a destination in an exposure path, not a corridor
                for leg in self.out_edges.get(node, ()):
                    if leg.dst not in reached:
                        reached.add(leg.dst)
                        nxt.append(leg.dst)
            frontier = nxt
        return tuple(sorted(reached))

    # --- the day-3 gate numbers -----------------------------------------
    def top_degrees(self, n: int = 20) -> tuple[tuple[str, int], ...]:
        """Highest-degree accounts, tie-broken by node key so the list is stable.

        Printed by the P3a gate: 00 §D asks for the top-20 degrees of a real
        corpus, and the shape of that list is the evidence for or against the
        network thesis.
        """
        ordered = self.nodes.sort(["total_degree", "account"], descending=[True, False])
        return tuple(
            (str(account), int(degree))
            for account, degree in ordered.select(["account", "total_degree"]).head(n).iter_rows()
        )


def percentile_nearest_rank(values: pl.Series, quantile: float) -> int:
    """The value at ``quantile`` by nearest rank, over an unsorted population.

    Nearest rank rather than interpolation, in exactly two places in this layer —
    here and at the rail threshold — because both answers must be an *account
    count*, and an interpolated degree of 1.5 accounts is not a threshold anyone
    can compare a node against. Sorting is O(n log n) and vectorised, so this
    stays usable on the full corpus.
    """
    if values.len() == 0:
        return 0
    ordered = values.cast(pl.Int64).sort()
    index = int(quantile * ordered.len())
    clamped = min(max(index, 0), ordered.len() - 1)
    value = ordered[clamped]
    if value is None:
        raise EventContractError(
            f"percentile {quantile} resolved to a null degree; the population is corrupt"
        )
    return int(value)


def summarize_degrees(degrees: pl.Series | Sequence[int]) -> DegreeSummary:
    """Median / p90 / p99 / max of a population of degrees.

    Same nearest-rank rule as the rail threshold (see
    :func:`percentile_nearest_rank`); the median of an even population is the mean
    of its two middle degrees, which is the ordinary median and can legitimately
    end in .5.
    """
    series = degrees if isinstance(degrees, pl.Series) else pl.Series(list(degrees))
    if series.len() == 0:
        return DegreeSummary(accounts=0, median=0.0, p90=0.0, p99=0.0, max=0)
    ordered = series.cast(pl.Int64).sort()
    size = ordered.len()
    if size % 2:
        median = float(ordered[size // 2] or 0)
    else:
        median = (float(ordered[size // 2 - 1] or 0) + float(ordered[size // 2] or 0)) / 2.0
    return DegreeSummary(
        accounts=size,
        median=median,
        p90=float(percentile_nearest_rank(ordered, 0.90)),
        p99=float(percentile_nearest_rank(ordered, 0.99)),
        max=int(ordered[-1] or 0),
    )


def _empty_flow() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "account": pl.Utf8,
            "currency": pl.Utf8,
            "inflow_minor": pl.Int64,
            "inflow_count": pl.Int64,
            "outflow_minor": pl.Int64,
            "outflow_count": pl.Int64,
        }
    )


__all__ = [
    "FEATURE_NULL_POLICY",
    "NODE_TYPE_EXTERNAL",
    "NODE_TYPE_MEMBER",
    "NODE_TYPE_RAIL",
    "SELF_TRANSFER_EXCLUDED_REASON",
    "SINGLETON_DEGREE",
    "AccountGraph",
    "Cycle",
    "CycleSearch",
    "DegreeSummary",
    "EdgeLeg",
    "GraphStats",
    "summarize_degrees",
]
