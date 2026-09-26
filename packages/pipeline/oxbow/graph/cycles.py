"""Time-respecting, value-retaining cycle enumeration, inside a hard budget.

This is the module the P3a gate is really about, and the two adjectives are doing
all the work. An order-free directed cycle is worth nothing as evidence: money
that "went round" as A→B→C→A only means something if it moved in that order.
``measure_graph.py`` proved the point by measuring zero surviving
time-respecting 3-6 cycles on a 20k PaySim sample (DEV-011) — a corpus that
looks network-shaped to a naive enumerator and is not.

The three properties, each with a fixture that can distinguish it:

**time-respecting**
    Walking the loop forward, each leg's instant is no earlier than the leg before
    it. A loop with one timestamp reversed returns nothing.

**value-retaining**
    All legs share one currency (a loop that converts is an FX trade, not a
    round-robin), each leg carries no more than the leg before it — a hop that
    forwards more than it received is funded from outside the loop — and the
    smallest leg is at least ``graph.cycles.value_retention_floor`` of the
    largest. A loop that sheds 95 % of its value on the way round is not moving
    money, it is laundering a receipt.

**bounded**
    Depth is capped by ``max_length``, exploration by ``max_visits_per_component``
    and wall-clock by ``timeout_seconds``. A naive enumeration over a dense
    component does not finish; the budget is what keeps the pipeline able to
    complete, and hitting it sets ``cycle_search_truncated`` so the count is
    reported as a lower bound instead of a total. No silent default, no hang.

Determinism: components are visited in ascending order of their smallest node key,
start nodes and legs in the canonical ``(event_ts_utc, txn_id)`` order, and every
cycle is deduplicated on its rotation-to-minimum-node key. A directed cycle has no
natural first node, so without that rotation two runs that entered the loop at
different points would report "different" cycles.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping
from time import monotonic
from typing import Final

from oxbow.graph.model import Cycle, CycleSearch, EdgeLeg
from oxbow.graph.settings import CycleSettings

# Check the deadline every this-many-th leg rather than on every leg: a
# monotonic() call is cheap, but not inside a loop that runs millions of times.
DEADLINE_CHECK_STRIDE: Final[int] = 4096

BUDGET_REASON: Final = "max_visits_per_component"
REPORT_CAP_REASON: Final = "max_cycles_reported"
TIMEOUT_REASON: Final = "timeout_seconds"


def _first_leg_at_or_after(legs: tuple[EdgeLeg, ...], ts_us: int) -> int:
    """Index of the first leg no earlier than ``ts_us``.

    Adjacency lists are sorted on the canonical total order, whose first field is
    the timestamp, so the time-respecting filter is a binary search instead of a
    scan over every older edge. That is the difference between a search that
    finishes on a 500 k-edge corpus and one that does not.
    """
    return bisect_left(legs, (ts_us,))


def connected_components(
    nodes: Iterable[str], adjacency: Mapping[str, Iterable[str]]
) -> list[list[str]]:
    """Undirected components, each sorted, the list ordered by smallest node key.

    Undirected because a strongly-connected component would be the wrong question:
    the walk can enter a loop through any bridge, and a one-way chain that feeds a
    cycle is part of that cycle's neighbourhood.
    """
    seen: set[str] = set()
    components: list[list[str]] = []
    for node in sorted(nodes):
        if node in seen:
            continue
        seen.add(node)
        stack = [node]
        member: list[str] = []
        while stack:
            current = stack.pop()
            member.append(current)
            for nxt in sorted(adjacency.get(current, ())):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        member.sort()
        components.append(member)
    return components


def enumerate_cycles(
    *,
    out_edges: Mapping[str, tuple[EdgeLeg, ...]],
    candidates: Iterable[str],
    undirected_adjacency: Mapping[str, Iterable[str]],
    settings: CycleSettings,
    rails_excluded: int,
) -> CycleSearch:
    """Enumerate bounded cycles over ``candidates``.

    ``candidates`` arrives already filtered by the caller: rails when
    ``graph.cycles.ignore_rails`` (a loop through a supernode is the whole
    economy, not a conspiracy) and singletons, which cannot close a loop. Reversal
    legs are dropped per leg below, because a refund is not a hop of a
    value-retaining loop.
    """
    candidate_set = frozenset(candidates)
    components = connected_components(candidate_set, undirected_adjacency)
    min_length = settings.min_length
    max_length = settings.max_length
    excluded_types = frozenset(settings.excluded_txn_types)
    retention_floor = settings.value_retention_floor
    require_non_increasing = settings.require_non_increasing

    found: dict[tuple[tuple[str, ...], tuple[str, ...]], Cycle] = {}
    state = _SearchState(deadline=monotonic() + settings.timeout_seconds)

    def record(path_nodes: tuple[str, ...], legs: tuple[EdgeLeg, ...]) -> None:
        amounts = tuple(leg.amount_minor for leg in legs)
        peak = max(amounts)
        if peak == 0:
            # Retention is a ratio against the value that moved. An all-zero loop
            # has no denominator, and treating 0/0 as a perfect round-robin would
            # invent structure out of four bookkeeping entries (03 A rule 2).
            state.zero_value_rejected += 1
            return
        retention = min(amounts) / peak
        if retention < retention_floor:
            return
        shift = min(range(len(path_nodes)), key=lambda i: path_nodes[i])
        nodes_key = path_nodes[shift:] + path_nodes[:shift]
        txn_ids = tuple(leg.txn_id for leg in legs)
        identity = (nodes_key, txn_ids[shift:] + txn_ids[:shift])
        if identity in found:
            return
        found[identity] = Cycle(
            path=path_nodes,
            canonical_key=nodes_key,
            amounts_minor=amounts,
            ts_us=tuple(leg.ts_us for leg in legs),
            txn_ids=txn_ids,
            currency=legs[0].currency,
            first_amount_minor=amounts[0],
            last_amount_minor=amounts[-1],
            value_retention=retention,
        )
        if len(found) >= settings.max_cycles_reported:
            state.stop(REPORT_CAP_REASON)

    def extend(start: str, path_nodes: tuple[str, ...], legs: tuple[EdgeLeg, ...]) -> None:
        if state.stopped or len(legs) >= max_length:
            return
        head = legs[-1]
        visited = frozenset(path_nodes)
        source_legs = out_edges.get(head.dst, ())
        for leg in source_legs[_first_leg_at_or_after(source_legs, head.ts_us) :]:
            if state.stopped:
                # The report cap can trip inside `record`, and one more leg examined
                # after that is one more cycle counted than the cap promised.
                return
            state.visits += 1
            if state.hits_deadline():
                state.stop(TIMEOUT_REASON)
                return
            if leg.txn_type in excluded_types:
                continue
            if leg.currency != legs[0].currency:
                # Comparing amounts across currencies is the implicit-FX error
                # 01 B forbids, so a mixed loop is not a candidate at all.
                continue
            if require_non_increasing and leg.amount_minor > head.amount_minor:
                continue
            if leg.dst == start:
                if len(legs) + 1 >= min_length:
                    record(path_nodes, (*legs, leg))
                continue
            if leg.dst in visited:
                continue
            extend(start, (*path_nodes, leg.dst), (*legs, leg))
            if state.stopped:
                return

    for component in components:
        component_nodes = frozenset(component)
        visits_at_start = state.visits
        for start in component:
            for leg in out_edges.get(start, ()):
                if leg.dst not in component_nodes or leg.dst == start:
                    continue
                state.visits += 1
                extend(start, (start, leg.dst), (leg,))
                if state.stopped:
                    break
            if state.stopped:
                break
            if state.visits - visits_at_start > settings.max_visits_per_component:
                state.stop(BUDGET_REASON)
                break

    return CycleSearch(
        cycles=tuple(sorted(found.values(), key=_cycle_sort_key)),
        truncated=state.stopped,
        truncation_reason=state.reason,
        visits=state.visits,
        components_searched=len(components),
        rails_excluded=rails_excluded,
        zero_value_loops_rejected=state.zero_value_rejected,
    )


def _cycle_sort_key(cycle: Cycle) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Report order: canonical node rotation, then leg ids. Fully determined."""
    return (cycle.canonical_key, cycle.txn_ids)


class _SearchState:
    """Mutable counters for one search, kept out of the closures' signature noise.

    The deadline is the only wall-clock value in this module and it is never
    written to disk: it bounds the work, it does not describe the data, so two
    runs on the same input produce the same artifact bytes even if one of them
    was slower.
    """

    __slots__ = ("deadline", "reason", "stopped", "visits", "zero_value_rejected")

    def __init__(self, *, deadline: float) -> None:
        self.deadline = deadline
        self.visits = 0
        self.zero_value_rejected = 0
        self.stopped = False
        self.reason: str | None = None

    def stop(self, reason: str) -> None:
        self.stopped = True
        if self.reason is None:
            self.reason = reason

    def hits_deadline(self) -> bool:
        if self.visits % DEADLINE_CHECK_STRIDE:
            return False
        return monotonic() > self.deadline


__all__ = [
    "BUDGET_REASON",
    "DEADLINE_CHECK_STRIDE",
    "REPORT_CAP_REASON",
    "TIMEOUT_REASON",
    "connected_components",
    "enumerate_cycles",
]
