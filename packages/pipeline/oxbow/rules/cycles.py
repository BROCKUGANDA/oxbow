"""Bounded loop and chain search for R4 and R12, with a near-miss ledger.

Why this is not simply ``oxbow.graph.enumerate_cycles`` called with R4's numbers:
the graph engine reports cycles and *only* cycles. It prunes a cross-currency leg at the
walk, so a loop that the corpus labels as a cycle (DEV-015: 38 of its 54 labelled
cycles) never appears anywhere in its output — not as a hit, not as a rejection, not as
a number. A rule that cannot distinguish "there was nothing here" from "there was
something my policy refused" is definitionally blind and looks successful, which is the
failure DEV-015 was raised about. This module therefore separates two concerns the
graph engine fuses:

1. **Walk** — find time-respecting directed loops and paths, bounded by depth, visits
   and wall clock. No value policy in the walk, so a refused pattern is still seen.
2. **Classify** — apply §9's policy (length, retention, non-increasing, single
   currency, non-zero) and record either a loop or a :class:`NearMiss` naming every
   reason it was refused.

With the §9 defaults the walk and the classification agree with
:func:`oxbow.graph.cycles.enumerate_cycles` on the same input — which
``tests/unit/test_p3b_rules.py::test_r4_default_policy_matches_graph_engine`` asserts
rather than asserts-about. If they ever diverge, that test says which one moved.

The walk is time-respecting in the strict sense §9 requires: each leg's instant is
greater than the leg before it, so a loop whose timestamps run backwards from any
starting point returns nothing. That is the gate clause, and it is a property of the
walk rather than of the classifier — no amount of relaxation re-admits a reversed loop.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from time import monotonic
from typing import Final

from oxbow.rules.base import RuleContext
from oxbow.rules.events import RuleEvents, Window
from oxbow.rules.hits import NearMiss
from oxbow.rules.settings import CURRENCY_POLICY_SAME

# Near-miss reasons. Strings rather than an enum because they land in the run report and
# in an operator-facing panel verbatim, and a reason that has to be translated twice
# loses the specificity DEV-015 asked for.
REASON_LENGTH_BELOW_MIN: Final = "length_below_min_length"
REASON_LENGTH_ABOVE_MAX: Final = "length_above_max_length"
REASON_RETENTION_FLOOR: Final = "value_retention_below_floor"
REASON_AMOUNT_INCREASES: Final = "amount_increases_along_loop"
REASON_CROSS_CURRENCY: Final = "cross_currency_legs"
REASON_ZERO_VALUE: Final = "zero_value_loop"
REASON_TIME_REVERSAL: Final = "timestamps_not_increasing"

BUDGET_REASON: Final = "max_visits"
TIMEOUT_REASON: Final = "timeout_ms"
DEPTH_REASON: Final = "max_depth"

# Near-miss discovery looks a little past the reporting ceiling, because "a 7-node loop
# was excluded by max_length" is only sayable if the 7-node loop was found. The slack is
# small and fixed: a loop long enough to be past it is not a typology, it is a component.
NEAR_MISS_DEPTH_SLACK: Final[int] = 2

# Legs carrying a deadline check every this-many-th visit. ``monotonic()`` is cheap but
# not free inside a walk that runs millions of times.
DEADLINE_CHECK_STRIDE: Final[int] = 4096


@dataclass(frozen=True, slots=True)
class Leg:
    """One directed movement between accounts, as the walk needs it.

    Field order is the canonical total order first, so a sorted sequence of legs is
    sorted by ``(ts_us, txn_id)`` without a key function at each call site.
    """

    src: str
    dst: str
    ts_us: int
    txn_id: str
    amount_minor: int
    currency: str


@dataclass(frozen=True, slots=True)
class Loop:
    """A classified loop: the ring, and whatever policy still stood between it and a hit."""

    nodes: tuple[str, ...]
    legs: tuple[Leg, ...]
    currencies: tuple[str, ...]
    amounts_minor: tuple[int, ...]
    ts_us: tuple[int, ...]
    value_retention: float
    retention_comparable: bool
    reasons: tuple[str, ...]
    non_increasing_breaches: int
    timestamps_increasing: bool = True

    @property
    def length(self) -> int:
        return len(self.nodes)

    @property
    def txn_ids(self) -> tuple[str, ...]:
        return tuple(leg.txn_id for leg in self.legs)

    @property
    def is_hit(self) -> bool:
        return not self.reasons


@dataclass(frozen=True, slots=True)
class LoopSearch:
    loops: tuple[Loop, ...]
    truncated: bool
    reason: str | None
    visits: int
    nodes_searched: int
    horizon: int


@dataclass(frozen=True, slots=True)
class Chain:
    nodes: tuple[str, ...]
    legs: tuple[Leg, ...]

    @property
    def hops(self) -> int:
        return len(self.legs)

    @property
    def amounts_minor(self) -> tuple[int, ...]:
        return tuple(leg.amount_minor for leg in self.legs)

    @property
    def txn_ids(self) -> tuple[str, ...]:
        return tuple(leg.txn_id for leg in self.legs)

    @property
    def window(self) -> Window:
        return Window(
            start_us=self.legs[0].ts_us,
            end_us=self.legs[-1].ts_us,
            label="chain",
        )


@dataclass(frozen=True, slots=True)
class ChainSearch:
    chains: tuple[Chain, ...]
    truncated: bool
    reason: str | None
    visits: int


def adjacency(
    events: RuleEvents,
    *,
    drop_reversals: bool,
    drop_zero_value: bool,
    skip_nodes: Iterable[str] = (),
) -> tuple[dict[str, tuple[Leg, ...]], frozenset[str], dict[str, int]]:
    """Total-order-sorted outbound legs per account, and the node set the walk may use.

    Self-transfers are dropped here rather than at every call site: a self-loop is one
    node, and it trivially satisfies a cycle at full retention. They stay counted on
    :attr:`RuleEvents.self_transfer_count`, which is the "excluded but counted" half of
    the requirement.
    """
    skip = frozenset(skip_nodes)
    buckets: dict[str, list[Leg]] = {}
    dropped = {"reversal": 0, "zero_value": 0, "self_transfer": 0, "skipped_node": 0}
    for account, legs in events.originated.items():
        if account in skip:
            dropped["skipped_node"] += len(legs)
            continue
        for event in legs:
            if event.is_self_transfer:
                dropped["self_transfer"] += 1
                continue
            if drop_reversals and event.is_reversal:
                dropped["reversal"] += 1
                continue
            if drop_zero_value and event.is_zero_value:
                dropped["zero_value"] += 1
                continue
            if event.peer in skip:
                dropped["skipped_node"] += 1
                continue
            buckets.setdefault(account, []).append(
                Leg(
                    src=account,
                    dst=event.peer,
                    ts_us=event.ts_us,
                    txn_id=event.txn_id,
                    amount_minor=event.amount_minor,
                    currency=event.currency,
                )
            )
    ordered = {
        src: tuple(sorted(legs, key=lambda leg: (leg.ts_us, leg.txn_id)))
        for src, legs in buckets.items()
    }
    nodes = frozenset(ordered) | {leg.dst for legs in ordered.values() for leg in legs}
    return ordered, nodes, dropped


def build_loop(nodes: Sequence[str], legs: Sequence[Leg], *, reasons: Sequence[str] = ()) -> Loop:
    """Measure one explicit ring, without deciding anything about it.

    Public because the corpus's own labelled cycles are rings the *walk* cannot find
    (they convert currency, and five of them do not respect time), while DEV-015 asks
    what each setting excludes. Taking the same measurement the walk takes is the only
    way that answer can be comparable to a hit; a second arithmetic would be a second
    definition with a different name.
    """
    currencies = tuple(sorted({leg.currency for leg in legs}))
    amounts = tuple(leg.amount_minor for leg in legs)
    stamps = tuple(leg.ts_us for leg in legs)
    peak = max(amounts) if amounts else 0
    return Loop(
        nodes=tuple(nodes),
        legs=tuple(legs),
        currencies=currencies,
        amounts_minor=amounts,
        ts_us=stamps,
        value_retention=0.0 if peak == 0 else min(amounts) / peak,
        retention_comparable=len(currencies) == 1 and peak > 0,
        reasons=tuple(reasons),
        non_increasing_breaches=sum(
            1 for previous, following in pairwise(amounts) if following > previous
        ),
        timestamps_increasing=all(later > earlier for earlier, later in pairwise(stamps)),
    )


def loop_reasons(
    loop: Loop,
    *,
    min_length: int,
    max_length: int,
    retention_floor: float,
    require_non_increasing: bool,
    same_currency: bool,
    require_strict_time: bool,
) -> tuple[str, ...]:
    """Every §9 setting that excluded this ring, in config order.

    All of them, not the first: DEV-015 asked what each knob excludes, and short-circuiting
    would report one number per knob instead of the overlap between them — the 25 cycles the
    retention floor refuses are not the same 25 the currency rule refuses.
    """
    reasons: list[str] = []
    if loop.length < min_length:
        reasons.append(REASON_LENGTH_BELOW_MIN)
    if loop.length > max_length:
        reasons.append(REASON_LENGTH_ABOVE_MAX)
    if require_strict_time and not loop.timestamps_increasing:
        reasons.append(REASON_TIME_REVERSAL)
    if all(amount == 0 for amount in loop.amounts_minor):
        reasons.append(REASON_ZERO_VALUE)
    elif loop.value_retention < retention_floor:
        reasons.append(REASON_RETENTION_FLOOR)
    if same_currency and len(loop.currencies) > 1:
        reasons.append(REASON_CROSS_CURRENCY)
    if require_non_increasing and loop.non_increasing_breaches:
        reasons.append(REASON_AMOUNT_INCREASES)
    return tuple(reasons)


def walk_loops(
    out: Mapping[str, tuple[Leg, ...]],
    nodes: Iterable[str],
    *,
    min_length: int,
    horizon: int,
    max_visits: int,
    timeout_ms: int,
) -> LoopSearch:
    """Every simple time-respecting loop up to ``horizon`` nodes, deduplicated by rotation.

    The walk is policy-free on purpose (module docstring); ``horizon`` is where the
    budget lives instead, and it is the only value that stops an exhaustive search from
    being an exhaustive non-termination.
    """
    node_set = frozenset(nodes)
    deadline = monotonic() + timeout_ms / 1000.0
    visits = 0
    truncated = False
    reason: str | None = None
    # Keyed on rotation *and* leg ids, the same identity the graph engine uses: three
    # laps of one ring are three observations of one pattern, and collapsing them during
    # the walk would destroy the recurrence the periodicity down-weight needs to see.
    found: dict[tuple[tuple[str, ...], tuple[str, ...]], Loop] = {}

    def record(path: tuple[str, ...], legs: tuple[Leg, ...]) -> None:
        rotation = _canonical_rotation(path, legs)
        measured = build_loop(rotation.nodes, rotation.legs)
        found[(rotation.canonical_key, rotation.leg_ids)] = measured

    def extend(path: tuple[str, ...], legs: tuple[Leg, ...]) -> None:
        nonlocal visits, truncated, reason
        if truncated:
            return
        if len(legs) >= horizon:
            return
        head = legs[-1]
        candidates = out.get(head.dst, ())
        # Strictly greater: the bisect floor is the first leg after the head's own
        # instant, which is what makes a time-reversed loop return nothing rather than
        # merely score badly.
        for leg in candidates[bisect_right(candidates, head.ts_us, key=_leg_ts) :]:
            if truncated:
                return
            visits += 1
            if visits > max_visits:
                truncated, reason = True, BUDGET_REASON
                return
            if visits % DEADLINE_CHECK_STRIDE == 0 and monotonic() > deadline:
                truncated, reason = True, TIMEOUT_REASON
                return
            if leg.dst == path[0]:
                if len(legs) + 1 >= min_length:
                    record(path, (*legs, leg))
                continue
            if leg.dst in path:
                continue
            extend((*path, leg.dst), (*legs, leg))
            if truncated:
                return

    for start in sorted(node_set):
        if truncated:
            break
        for leg in out.get(start, ()):
            if leg.dst == start or leg.dst not in node_set:
                continue
            visits += 1
            if visits > max_visits:
                truncated, reason = True, BUDGET_REASON
                break
            extend((start, leg.dst), (leg,))
            if truncated:
                break

    loops = tuple(sorted(found.values(), key=lambda loop: (loop.nodes, loop.txn_ids)))
    return LoopSearch(
        loops=loops,
        truncated=truncated,
        reason=reason,
        visits=visits,
        nodes_searched=len(node_set),
        horizon=horizon,
    )


def _leg_ts(leg: Leg) -> int:
    return leg.ts_us


@dataclass(frozen=True, slots=True)
class _Rotation:
    """A loop with its start normalised, plus the two identities it needs.

    ``canonical_key`` answers "which ring is this" and ``leg_ids`` answers "which
    observation of it" — the first collapses three laps of a payroll cycle into one
    pattern, the second keeps those three laps visible to the periodicity test.
    """

    nodes: tuple[str, ...]
    legs: tuple[Leg, ...]
    canonical_key: tuple[str, ...]

    @property
    def leg_ids(self) -> tuple[str, ...]:
        return tuple(leg.txn_id for leg in self.legs)


def _canonical_rotation(path: tuple[str, ...], legs: tuple[Leg, ...]) -> _Rotation:
    """Rotate a loop to start at its smallest node key.

    A cycle has no natural first node. Without this, two runs that entered the same ring
    at different points would report different cycles, and dedup would double-count one
    typology into several pieces of evidence.
    """
    shift = min(range(len(path)), key=lambda index: path[index])
    return _Rotation(
        nodes=path[shift:] + path[:shift],
        legs=legs[shift:] + legs[:shift],
        canonical_key=path[shift:] + path[:shift],
    )


def near_miss_of(loop: Loop, *, reasons: Sequence[str]) -> NearMiss:
    return NearMiss(
        rule_id="R4",
        pattern=loop.nodes,
        reasons=tuple(reasons),
        measurements={
            "length": loop.length,
            "value_retention": round(loop.value_retention, 6),
            "currencies": list(loop.currencies),
            "retention_comparable_across_currencies": loop.retention_comparable,
            "amount_increases": loop.non_increasing_breaches,
            "timestamps_increasing": loop.timestamps_increasing,
            "ts_us": list(loop.ts_us),
            "txn_ids": list(loop.txn_ids),
        },
    )


def walk_chains(
    out: Mapping[str, tuple[Leg, ...]],
    nodes: Iterable[str],
    *,
    min_hops: int,
    max_depth: int,
    delta_us: int,
    require_non_increasing: bool,
    component_budget: int,
    timeout_ms: int,
) -> ChainSearch:
    """Time-respecting non-increasing paths, then keep only the maximal ones.

    "Maximal" is the dedup that makes one five-account chain one piece of evidence: a
    three-hop chain inside a four-hop chain is the same pattern seen from closer up, and
    reporting both would double-count the layering. Containment is checked as a
    contiguous node subsequence, longest chains first, so the surviving set is unique
    regardless of enumeration order.
    """
    node_set = frozenset(nodes)
    deadline = monotonic() + timeout_ms / 1000.0
    visits = 0
    truncated = False
    reason: str | None = None
    collected: list[Chain] = []

    def extend(path: tuple[str, ...], legs: tuple[Leg, ...]) -> None:
        nonlocal visits, truncated, reason
        if truncated:
            return
        if len(legs) >= max_depth:
            if len(legs) >= min_hops:
                collected.append(Chain(nodes=path, legs=legs))
            return
        head = legs[-1]
        candidates = out.get(head.dst, ())
        floor_ts = head.ts_us
        ceiling_ts = head.ts_us + delta_us
        for leg in candidates[bisect_right(candidates, floor_ts, key=_leg_ts) :]:
            if leg.ts_us > ceiling_ts:
                break
            if truncated:
                return
            visits += 1
            if visits > component_budget:
                truncated, reason = True, BUDGET_REASON
                return
            if visits % DEADLINE_CHECK_STRIDE == 0 and monotonic() > deadline:
                truncated, reason = True, TIMEOUT_REASON
                return
            if require_non_increasing and leg.amount_minor > head.amount_minor:
                continue
            if leg.dst in path:
                continue
            grown = (*legs, leg)
            if len(grown) >= min_hops:
                collected.append(Chain(nodes=(*path, leg.dst), legs=grown))
            extend((*path, leg.dst), grown)
            if truncated:
                return

    for start in sorted(node_set):
        if truncated:
            break
        for leg in out.get(start, ()):
            if leg.dst == start or leg.dst not in node_set:
                continue
            visits += 1
            if visits > component_budget:
                truncated, reason = True, BUDGET_REASON
                break
            extend((start, leg.dst), (leg,))
            if truncated:
                break

    return ChainSearch(
        chains=_keep_maximal(collected),
        truncated=truncated,
        reason=reason,
        visits=visits,
    )


def _keep_maximal(chains: Sequence[Chain]) -> tuple[Chain, ...]:
    ordered = sorted(chains, key=lambda chain: (-chain.hops, chain.nodes))
    kept: list[Chain] = []
    for chain in ordered:
        if any(_contains(other, chain) for other in kept):
            continue
        kept.append(chain)
    return tuple(sorted(kept, key=lambda chain: (chain.nodes[0], chain.txn_ids)))


def _contains(outer: Chain, inner: Chain) -> bool:
    """Is ``inner`` a contiguous piece of ``outer`` (and not the whole of it)?"""
    if outer is inner or inner.hops > outer.hops:
        return False
    needle, haystack = inner.nodes, outer.nodes
    span = len(needle)
    if span > len(haystack):
        return False
    return any(haystack[i : i + span] == needle for i in range(len(haystack) - span + 1))


def rails_and_singletons(ctx: RuleContext) -> tuple[frozenset[str], dict[str, int]]:
    """Nodes the graph typed out of traversal, and the counts that says.

    R4 and R12 walk the graph's adjacency, and the graph layer already decided that a
    rail is a destination rather than a corridor and that a singleton cannot close a
    loop (DEV-011). Re-deciding that here would put a second, drifting copy of the
    typing rule in the rules layer, so this reads the graph's own answer.
    """
    skip: set[str] = set()
    rails = 0
    singletons = 0
    for account, node_type in ctx.graph.node_types.items():
        if node_type == "rail":
            rails += 1
            skip.add(account)
    if ctx.settings.guards.exclude_singletons_from_graph_aggregates:
        for account in ctx.graph.node_types:
            if len(ctx.graph.simple_neighbours.get(account, frozenset())) <= 1:
                singletons += 1
                skip.add(account)
    return frozenset(skip), {"rails": rails, "singletons": singletons}


def rails_only(ctx: RuleContext) -> tuple[frozenset[str], dict[str, int]]:
    """Rails and nothing else, for a walk where a leaf is a legitimate endpoint."""
    skip: set[str] = set()
    rails = 0
    for account, node_type in ctx.graph.node_types.items():
        if node_type == "rail":
            rails += 1
            skip.add(account)
    return frozenset(skip), {"rails": rails, "singletons": 0}


def policy_near_misses(ctx: RuleContext, search: LoopSearch) -> tuple[list[Loop], list[NearMiss]]:
    """Apply R4's configured policy, returning hits and the refused loops with reasons."""
    from oxbow.rules.settings import CycleMemberSettings

    settings: CycleMemberSettings = ctx.settings.settings_for("R4")  # type: ignore[assignment]
    accepted: list[Loop] = []
    misses: list[NearMiss] = []
    for loop in search.loops:
        reasons = loop_reasons(
            loop,
            min_length=settings.min_length,
            max_length=settings.max_length,
            retention_floor=settings.retention,
            require_non_increasing=settings.non_increasing,
            same_currency=settings.currency_policy == CURRENCY_POLICY_SAME,
            require_strict_time=settings.require_strict_time_increase,
        )
        if reasons:
            if settings.emit_near_misses:
                misses.append(near_miss_of(loop, reasons=reasons))
            continue
        accepted.append(loop)
    return accepted, misses


__all__ = [
    "BUDGET_REASON",
    "DEADLINE_CHECK_STRIDE",
    "NEAR_MISS_DEPTH_SLACK",
    "REASON_AMOUNT_INCREASES",
    "REASON_CROSS_CURRENCY",
    "REASON_LENGTH_ABOVE_MAX",
    "REASON_LENGTH_BELOW_MIN",
    "REASON_RETENTION_FLOOR",
    "REASON_TIME_REVERSAL",
    "REASON_ZERO_VALUE",
    "TIMEOUT_REASON",
    "Chain",
    "ChainSearch",
    "Leg",
    "Loop",
    "LoopSearch",
    "adjacency",
    "build_loop",
    "loop_reasons",
    "policy_near_misses",
    "rails_and_singletons",
    "rails_only",
    "walk_chains",
    "walk_loops",
]
