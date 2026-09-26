"""Allocation under capacity: greedy by EV density, and exact CP-SAT, compared.

Plan §11 requires both solvers *because the comparison is the result*: "the
simple policy is within 1.8 % of optimal" is a stronger claim than a marginally
better number, and it is only available if the simple policy was actually run
against the exact one. So this module does three things, not one:

1. Greedy by EV density — first-fit over ``(-EV_i/m_i, account_key)``, which is
   optimal for the fractional relaxation and fast enough to re-run behind a
   slider. The budget lives in config (``solver.greedy_budget_ms``) and is
   measured by a test, not asserted here.
2. Exact 0/1 knapsack via CP-SAT, offline and behind a hard deadline. The
   objective coefficients are *minor units*, so the solver works on exact
   integers — DEV-005 pays for itself right here, because with a float objective
   "proven optimal" would be a statement about floating-point arithmetic.
3. The gap between them, in money, with the denominator named.

Degradation is a first-class outcome, never an exception: an exceeded deadline or
a raising solver comes back as a greedy allocation whose ``allocator``,
``termination`` and ``message`` say so, because the UI renders that label verbatim
(03 §J: degraded, not broken; 01 §G: no broad catch returning an empty list).

Two further states are documented results rather than errors — zero capacity
(nothing can be reviewed; here is the value forgone) and no positive-EV candidate
(nothing pays for itself; here is the recovery rate that would change that).
"""

from __future__ import annotations

import enum
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from ortools.sat.python import cp_model

from oxbow.quant.economics import (
    CurrencyFigure,
    Economics,
    assumption_block,
    currency_figure_over_band,
)
from oxbow.quant.ev import (
    AccountEV,
    break_even_recovery,
    captured_exposure,
    ev_minor,
    expected_loss_avoided,
    expected_wrongly_touched_count,
    positive_ev_rows,
    review_minutes_total,
    wrong_touch_friction,
)
from oxbow.quant.money import Money, QuantError, scale_div

# Ratio precision: the gap is a fixed-point ratio with twelve decimal places,
# computed by integer division rather than a float quotient. The scale matters: at
# four decimals a real 0.003 % gap on a three-thousand-account queue rounds to zero
# and the gate would report perfect agreement, which is the flattering rounding this
# layer exists to refuse.
_RATIO_SCALE: int = 1_000_000_000_000

_EMPTY: tuple[AccountEV, ...] = ()


class AllocatorId(enum.StrEnum):
    """Which policy produced this allocation. Recorded on every result (plan §11)."""

    GREEDY = "greedy_ev_density"
    CP_SAT = "cpsat_exact"
    GREEDY_AFTER_CP_SAT_DEADLINE = "greedy_ev_density_after_cp_sat_deadline"
    GREEDY_AFTER_CP_SAT_ERROR = "greedy_ev_density_after_cp_sat_error"
    BASELINE_HIGHEST_EXPOSURE = "baseline_highest_exposure_first"
    BASELINE_RANDOM = "baseline_random_seeded"

    @property
    def label(self) -> str:
        """The sentence the UI renders next to the allocation.

        A property of the enum rather than a string at the call site, so no
        surface can paraphrase "degraded" into something that reads like a choice.
        """
        return {
            AllocatorId.GREEDY: (
                "greedy by EV density (fast approximation; optimal for the fractional "
                "relaxation)"
            ),
            AllocatorId.CP_SAT: (
                "CP-SAT exact 0/1 knapsack (optimality proven within the deadline)"
            ),
            AllocatorId.GREEDY_AFTER_CP_SAT_DEADLINE: (
                "greedy by EV density - DEGRADED: the exact CP-SAT solve passed its hard "
                "deadline, so no optimality gap is available for this result"
            ),
            AllocatorId.GREEDY_AFTER_CP_SAT_ERROR: (
                "greedy by EV density - DEGRADED: the exact CP-SAT solve failed and fell "
                "back rather than the queue going empty"
            ),
            AllocatorId.BASELINE_HIGHEST_EXPOSURE: (
                "baseline: highest-exposure-first, the policy most monitoring desks "
                "actually run (dominated, and kept to show it)"
            ),
            AllocatorId.BASELINE_RANDOM: (
                "baseline: random order under the configured seed (dominated)"
            ),
        }[self]


class Termination(enum.StrEnum):
    """How the producing solver stopped. Kept separate from the allocator identity."""

    COMPLETED = "completed"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SOLVER_ERROR = "solver_error"
    APPROXIMATE = "approximate_by_construction"


class AllocationState(enum.StrEnum):
    """What the allocator decided, including the documented empty outcomes."""

    SELECTED = "selected"
    NOTHING_ALERTED = "nothing_alerted"
    NOTHING_PAYS = "nothing_pays_for_itself"
    NO_CAPACITY = "no_capacity"


class AllocationError(QuantError):
    """Raised for an impossible allocation request, as opposed to an empty one."""


class CpSatDeadlineError(QuantError):
    """Internal signal that the exact solve ran out of its deadline."""


@dataclass(frozen=True, slots=True)
class Allocation:
    """A recommendation: which accounts, produced how, worth what, under which assumptions.

    ``cfg`` travels with the result on purpose. Every money total here is derived
    from ``selected`` under those assumptions rather than stored beside them, so
    the queue total and the sum of its rows cannot drift apart (03 money rules:
    totals come from unrounded minor units), and no consumer can render a number
    from a period whose assumptions it never saw.
    """

    allocator: AllocatorId
    termination: Termination
    state: AllocationState
    capacity_minutes: int
    selected: tuple[AccountEV, ...]
    candidates: tuple[AccountEV, ...]
    deadline_ms: int
    message: str
    cfg: Economics

    @property
    def selected_keys(self) -> tuple[str, ...]:
        """The reviewed accounts, in the order the allocator took them."""
        return tuple(row.account_key for row in self.selected)

    @property
    def accounts_reviewed(self) -> int:
        """How many alerts the period actually touches."""
        return len(self.selected)

    @property
    def minutes_used(self) -> int:
        """Analyst-minutes consumed, which can be below capacity by construction."""
        return review_minutes_total(self.selected, None)

    @property
    def total_ev(self) -> Money:
        """Net expected value at the configured default ``r``."""
        return Money(
            ev_minor(self.selected, None, self.cfg.recovery.rate, self.cfg), self.cfg.currency
        )

    @property
    def expected_loss_avoided(self) -> Money:
        """Gross ``sum p*E*r`` at the configured default ``r``."""
        return expected_loss_avoided(self.selected, None, self.cfg.recovery.rate, self.cfg)

    @property
    def exposure_at_risk(self) -> Money:
        """``sum E_i`` inside the reviewed set."""
        return captured_exposure(self.selected, None, self.cfg)

    @property
    def friction_cost(self) -> Money:
        """``sum (1-p) f``: the priced harm of the alerts that were wrong."""
        return wrong_touch_friction(self.selected, None, self.cfg)

    @property
    def wrong_touch_expected(self) -> float:
        """Expected count of legitimate customers disturbed. A count, not money."""
        return expected_wrongly_touched_count(self.selected, None)

    @property
    def unscheduled(self) -> tuple[AccountEV, ...]:
        """Viable accounts the capacity constraint kept out."""
        chosen = set(self.selected_keys)
        return tuple(row for row in self.candidates if row.account_key not in chosen)

    @property
    def forgone_ev(self) -> Money:
        """Positive EV that exists but is not scheduled — the price of the constraint."""
        return Money(
            ev_minor(self.unscheduled, None, self.cfg.recovery.rate, self.cfg), self.cfg.currency
        )

    @property
    def allocator_label(self) -> str:
        """The visible label: which policy produced this, and whether it degraded."""
        return self.allocator.label

    def ev_figure(self) -> CurrencyFigure:
        """This allocation's EV as a band figure — money never leaves as a scalar."""
        block = assumption_block(self.cfg)
        return currency_figure_over_band(
            f"Expected value of the recommended set ({self.allocator.value})",
            block,
            lambda rate: Money(ev_minor(self.selected, None, rate, self.cfg), self.cfg.currency),
            provenance=(f"{self.accounts_reviewed} accounts, {self.minutes_used} minutes",),
        )

    def loss_avoided_figure(self) -> CurrencyFigure:
        """This allocation's gross loss avoided as a band figure."""
        block = assumption_block(self.cfg)
        return currency_figure_over_band(
            f"Expected loss avoided ({self.allocator.value})",
            block,
            lambda rate: expected_loss_avoided(self.selected, None, rate, self.cfg),
        )

    def forgone_figure(self) -> CurrencyFigure:
        """The value the capacity constraint costs, as a band figure."""
        block = assumption_block(self.cfg)
        remainder = self.unscheduled
        return currency_figure_over_band(
            "Expected value forgone by the capacity constraint",
            block,
            lambda rate: Money(ev_minor(remainder, None, rate, self.cfg), self.cfg.currency),
        )


def _positive_capacity(capacity_minutes: int) -> None:
    if capacity_minutes < 0:
        raise AllocationError(
            f"capacity must be >= 0 analyst-minutes, got {capacity_minutes}. A negative "
            "budget is not the zero-budget case, which is a documented outcome."
        )


def _result(
    *,
    cfg: Economics,
    allocator: AllocatorId,
    termination: Termination,
    state: AllocationState,
    selected: Sequence[AccountEV],
    candidates: Sequence[AccountEV],
    capacity_minutes: int,
    deadline_ms: int,
    message: str,
) -> Allocation:
    """One construction point for every allocation, all keyword, so no two call
    sites can disagree about which positional slot holds what."""
    return Allocation(
        allocator=allocator,
        termination=termination,
        state=state,
        capacity_minutes=capacity_minutes,
        selected=tuple(selected),
        candidates=tuple(candidates),
        deadline_ms=deadline_ms,
        message=message,
        cfg=cfg,
    )


def _nothing_alerted(cfg: Economics, allocator: AllocatorId, deadline_ms: int) -> Allocation:
    """Nothing was scored — distinct from 'scored, but nothing pays'."""
    return _result(
        cfg=cfg,
        allocator=allocator,
        termination=Termination.APPROXIMATE,
        state=AllocationState.NOTHING_ALERTED,
        selected=_EMPTY,
        candidates=_EMPTY,
        capacity_minutes=cfg.capacity.review_minutes_per_period,
        deadline_ms=deadline_ms,
        message=(
            "no scored accounts were supplied, so there is nothing to allocate. An empty "
            "input is reported as itself rather than as an empty recommendation, because "
            "the UI renders those differently (01 §G: no empty state standing in for a "
            "server bug)."
        ),
    )


def _nothing_pays(
    cfg: Economics,
    rows: Sequence[AccountEV],
    capacity_minutes: int,
    allocator: AllocatorId,
    deadline_ms: int,
) -> Allocation:
    """Every candidate is unprofitable: recommend nothing, and say what would change it."""
    return _result(
        cfg=cfg,
        allocator=allocator,
        termination=Termination.APPROXIMATE,
        state=AllocationState.NOTHING_PAYS,
        selected=_EMPTY,
        candidates=_EMPTY,
        capacity_minutes=capacity_minutes,
        deadline_ms=deadline_ms,
        message=break_even_recovery(rows, cfg).message,
    )


def _no_capacity(
    cfg: Economics,
    candidates: Sequence[AccountEV],
    capacity_minutes: int,
    allocator: AllocatorId,
    deadline_ms: int,
) -> Allocation:
    """Capacity cannot schedule a single review: empty allocation, value forgone, in words.

    The message quotes minor units because it is a log line and a ``state`` payload;
    the figure a human is shown is :meth:`Allocation.forgone_figure`, which carries
    the assumption block. Collapsing the two would put an assumption-free currency
    figure in a place designed to be assumption-free.
    """
    forgone = ev_minor(candidates, None, cfg.recovery.rate, cfg)
    smallest = min((row.review_minutes for row in candidates), default=0)
    cause = (
        "capacity is 0 analyst-minutes"
        if capacity_minutes == 0
        else f"capacity of {capacity_minutes} analyst-minutes is smaller than the "
        f"cheapest viable review ({smallest} minutes)"
    )
    return _result(
        cfg=cfg,
        allocator=allocator,
        termination=Termination.APPROXIMATE,
        state=AllocationState.NO_CAPACITY,
        selected=_EMPTY,
        candidates=candidates,
        capacity_minutes=capacity_minutes,
        deadline_ms=deadline_ms,
        message=(
            f"{cause}, so the allocation is empty by construction: {forgone} minor "
            f"{cfg.currency} of positive expected value across {len(candidates)} viable "
            "accounts is forgone. This is a stated outcome, not a failure."
        ),
    )


def _greedy_scan(candidates: Sequence[AccountEV], capacity_minutes: int) -> tuple[AccountEV, ...]:
    """First-fit over an already-ordered candidate list. The hot loop, and pure.

    Sorting once and scanning here is what lets the frontier sweep dozens of
    capacities without re-sorting: the slider re-runs only this loop over a cached
    array (plan §11 performance rule).
    """
    remaining = capacity_minutes
    chosen: list[AccountEV] = []
    for row in candidates:
        if row.review_minutes <= remaining:
            chosen.append(row)
            remaining -= row.review_minutes
        if remaining == 0:
            break
    return tuple(chosen)


def _policy_message(
    policy: AllocatorId, count: int, used: int, capacity_minutes: int, cfg: Economics
) -> str:
    """One sentence per policy, because a baseline that sounds like the policy is misleading."""
    if policy is AllocatorId.BASELINE_HIGHEST_EXPOSURE:
        return (
            f"{count} accounts chosen by exposure size alone ({used} of {capacity_minutes} "
            "minutes); a dominated baseline, kept so the EV policy has a number to beat."
        )
    if policy is AllocatorId.BASELINE_RANDOM:
        return (
            f"{count} accounts chosen in random order under seed {cfg.seed} ({used} of "
            f"{capacity_minutes} minutes); a dominated baseline that exists to be beaten."
        )
    if policy in (AllocatorId.GREEDY_AFTER_CP_SAT_DEADLINE, AllocatorId.GREEDY_AFTER_CP_SAT_ERROR):
        return (
            f"{count} accounts selected by EV density as the exact-solve fallback ({used} "
            f"of {capacity_minutes} minutes spent)."
        )
    return (
        f"{count} accounts selected by EV density, {used} of {capacity_minutes} "
        "analyst-minutes spent."
    )


@dataclass(frozen=True, slots=True)
class CachedQueue:
    """Priced accounts plus one ordering per policy, computed once.

    Plan §11 requires the capacity slider to re-run only the allocation over a
    cached array. The join, the EV pricing and the sort orders are the expensive
    part and none of them depend on ``B``, so this object does them once per scored
    run and the frontier sweep and the simulator both hit it instead of
    re-sorting a few thousand rows per slider move.
    """

    rows: tuple[AccountEV, ...]
    candidates: tuple[AccountEV, ...]
    orders: Mapping[AllocatorId, tuple[AccountEV, ...]]
    cfg: Economics

    @classmethod
    def build(cls, rows: Sequence[AccountEV], cfg: Economics) -> CachedQueue:
        """Price once and sort three ways. Ties break on ``account_key`` everywhere.

        The random baseline's order is drawn from the configured seed, not from
        process state, so a dominated policy stays reproducible — a baseline that
        cannot be re-run is an anecdote.
        """
        candidates = positive_ev_rows(rows)
        by_exposure = tuple(
            sorted(candidates, key=lambda row: (-row.exposure.minor, row.account_key))
        )
        shuffled = list(candidates)
        random.Random(cfg.seed).shuffle(shuffled)
        return cls(
            rows=tuple(rows),
            candidates=candidates,
            orders=MappingProxyType(
                {
                    AllocatorId.GREEDY: candidates,
                    AllocatorId.GREEDY_AFTER_CP_SAT_DEADLINE: candidates,
                    AllocatorId.GREEDY_AFTER_CP_SAT_ERROR: candidates,
                    AllocatorId.BASELINE_HIGHEST_EXPOSURE: by_exposure,
                    AllocatorId.BASELINE_RANDOM: tuple(shuffled),
                }
            ),
            cfg=cfg,
        )

    def allocate(
        self, capacity_minutes: int, policy: AllocatorId = AllocatorId.GREEDY
    ) -> Allocation:
        """Run one policy at one capacity. This is the entire slider path."""
        _positive_capacity(capacity_minutes)
        deadline_ms = self.cfg.solver.greedy_budget_ms
        if policy is AllocatorId.CP_SAT:
            return allocate_cpsat(self.rows, capacity_minutes, self.cfg)
        if not self.rows:
            return _nothing_alerted(self.cfg, policy, deadline_ms)
        if not self.candidates:
            return _nothing_pays(self.cfg, self.rows, capacity_minutes, policy, deadline_ms)
        if capacity_minutes == 0:
            return _no_capacity(self.cfg, self.candidates, capacity_minutes, policy, deadline_ms)
        chosen = _greedy_scan(self.orders[policy], capacity_minutes)
        if not chosen:
            # Capacity exists but is smaller than the cheapest viable review: the
            # same documented outcome as a zero budget, with its own cause in the
            # message rather than a silently empty selection.
            return _no_capacity(self.cfg, self.candidates, capacity_minutes, policy, deadline_ms)
        used = sum(row.review_minutes for row in chosen)
        return _result(
            cfg=self.cfg,
            allocator=policy,
            termination=Termination.APPROXIMATE,
            state=AllocationState.SELECTED,
            selected=chosen,
            candidates=self.candidates,
            capacity_minutes=capacity_minutes,
            deadline_ms=deadline_ms,
            message=_policy_message(policy, len(chosen), used, capacity_minutes, self.cfg),
        )


def allocate_greedy(
    rows: Sequence[AccountEV],
    capacity_minutes: int,
    cfg: Economics,
    *,
    allocator: AllocatorId = AllocatorId.GREEDY,
) -> Allocation:
    """Greedy by EV density under ``capacity_minutes`` analyst-minutes.

    Ties break on ``account_key`` ascending — the repo's deterministic tie-break —
    so two runs over the same scored input recommend the same set.
    """
    return CachedQueue.build(rows, cfg).allocate(capacity_minutes, allocator)


def allocate_highest_exposure(
    rows: Sequence[AccountEV], capacity_minutes: int, cfg: Economics
) -> Allocation:
    """Highest-exposure-first baseline: what most monitoring desks actually do.

    Plan §11 names it because it is the comparison an operations reader believes —
    the queue that reviews the biggest numbers first, and loses money doing it.
    """
    return CachedQueue.build(rows, cfg).allocate(
        capacity_minutes, AllocatorId.BASELINE_HIGHEST_EXPOSURE
    )


def allocate_random(rows: Sequence[AccountEV], capacity_minutes: int, cfg: Economics) -> Allocation:
    """Random-order baseline, drawn from ``monte_carlo.seed`` and no other source."""
    return CachedQueue.build(rows, cfg).allocate(capacity_minutes, AllocatorId.BASELINE_RANDOM)


def solve_cpsat(
    candidates: Sequence[AccountEV],
    capacity_minutes: int,
    cfg: Economics,
    deadline_ms: int,
) -> tuple[frozenset[str], bool]:
    """Exact 0/1 knapsack on EV in minor units. Returns ``(selected keys, proven)``.

    Raises rather than quietly substituting a heuristic: the callers own the
    degradation, and a function that returned greedy results under an exact label
    is the specific lie this module exists to prevent.
    """
    model = cp_model.CpModel()
    variables = [model.NewBoolVar(f"review_{index}") for index in range(len(candidates))]
    model.Add(
        sum(row.review_minutes * var for row, var in zip(candidates, variables, strict=True))
        <= capacity_minutes
    )
    model.Maximize(sum(row.ev.minor * var for row, var in zip(candidates, variables, strict=True)))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = deadline_ms / 1000.0
    solver.parameters.num_search_workers = cfg.solver.cpsat_workers
    solver.parameters.random_seed = cfg.seed
    status = solver.Solve(model)
    if status == cp_model.OPTIMAL:
        return (
            frozenset(
                row.account_key
                for row, var in zip(candidates, variables, strict=True)
                if solver.Value(var)
            ),
            True,
        )
    if status == cp_model.FEASIBLE:
        # A feasible incumbent with no dual bound: there is no gap to report, so it
        # is surfaced as a deadline rather than passed off as the exact answer.
        raise CpSatDeadlineError(
            f"CP-SAT found a feasible packing but no proof of optimality in {deadline_ms} ms"
        )
    raise AllocationError(
        f"CP-SAT returned {solver.StatusName(status)} for a knapsack that always has a "
        "feasible solution (the empty set), which means the model or the input is wrong."
    )


def _fallback(
    greedy: Allocation,
    *,
    allocator: AllocatorId,
    termination: Termination,
    deadline_ms: int,
    cause: str,
) -> Allocation:
    """Re-label a greedy result as a degradation, keeping the greedy numbers.

    ``replace`` on a frozen dataclass rather than a mutable field: the label and
    the result cannot then be out of sync, because there is only one object.
    """
    return replace(
        greedy,
        allocator=allocator,
        termination=termination,
        deadline_ms=deadline_ms,
        message=f"DEGRADED: {cause}. {greedy.message}",
    )


def allocate_cpsat(
    rows: Sequence[AccountEV],
    capacity_minutes: int,
    cfg: Economics,
    *,
    deadline_ms: int | None = None,
) -> Allocation:
    """Exact allocation, degrading to a labelled greedy result on any solver trouble.

    A *time* limit and an *exception* get different labels: one is expected
    behaviour under a slider drag, the other means a broken model, and collapsing
    them would hide a fault behind a reassuring sentence.
    """
    resolved = cfg.solver.cpsat_deadline_ms if deadline_ms is None else deadline_ms
    _positive_capacity(capacity_minutes)
    if not rows:
        return _nothing_alerted(cfg, AllocatorId.CP_SAT, resolved)
    candidates = positive_ev_rows(rows)
    if not candidates:
        return _nothing_pays(cfg, rows, capacity_minutes, AllocatorId.CP_SAT, resolved)
    if capacity_minutes == 0:
        return _no_capacity(cfg, candidates, capacity_minutes, AllocatorId.CP_SAT, resolved)
    try:
        selected_keys, proven = solve_cpsat(candidates, capacity_minutes, cfg, resolved)
    except CpSatDeadlineError as exc:
        greedy = allocate_greedy(rows, capacity_minutes, cfg)
        return _fallback(
            greedy,
            allocator=AllocatorId.GREEDY_AFTER_CP_SAT_DEADLINE,
            termination=Termination.DEADLINE_EXCEEDED,
            deadline_ms=resolved,
            cause=(
                f"{exc}; the recommendation below is greedy by EV density and no "
                "optimality gap is available for it"
            ),
        )
    except AllocationError as exc:
        greedy = allocate_greedy(rows, capacity_minutes, cfg)
        return _fallback(
            greedy,
            allocator=AllocatorId.GREEDY_AFTER_CP_SAT_ERROR,
            termination=Termination.SOLVER_ERROR,
            deadline_ms=resolved,
            cause=(
                f"the exact CP-SAT solve raised {exc}; the recommendation below is greedy "
                "by EV density and the solver fault needs a look"
            ),
        )
    except Exception as exc:  # a solver fault must never empty the queue (03 §J)
        greedy = allocate_greedy(rows, capacity_minutes, cfg)
        return _fallback(
            greedy,
            allocator=AllocatorId.GREEDY_AFTER_CP_SAT_ERROR,
            termination=Termination.SOLVER_ERROR,
            deadline_ms=resolved,
            cause=(
                f"CP-SAT failed with {type(exc).__name__}: {exc}; the recommendation below "
                "is greedy by EV density"
            ),
        )
    chosen = tuple(row for row in candidates if row.account_key in selected_keys)
    if not chosen:
        # The exact solve agreed with the heuristic that nothing fits. Same documented
        # outcome and the same message, so a zero-capacity read does not depend on
        # which solver happened to be asked.
        return _no_capacity(cfg, candidates, capacity_minutes, AllocatorId.CP_SAT, resolved)
    used = sum(row.review_minutes for row in chosen)
    return _result(
        cfg=cfg,
        allocator=AllocatorId.CP_SAT,
        termination=Termination.COMPLETED,
        state=AllocationState.SELECTED if chosen else AllocationState.NO_CAPACITY,
        selected=chosen,
        candidates=candidates,
        capacity_minutes=capacity_minutes,
        deadline_ms=resolved,
        message=(
            f"{len(chosen)} of {len(candidates)} viable accounts selected by exact 0/1 "
            f"knapsack on EV in minor units, {used} of {capacity_minutes} analyst-minutes "
            f"spent; optimality {'proven' if proven else 'not proven'}. Selection order is "
            "density, so the queue renders identically to the greedy policy."
        ),
    )


def allocate(
    rows: Sequence[AccountEV],
    capacity_minutes: int,
    cfg: Economics,
    *,
    allocator: AllocatorId = AllocatorId.GREEDY,
    deadline_ms: int | None = None,
) -> Allocation:
    """The single entry point, so no caller can obtain an unlabelled allocation.

    The two ``*_after_cp_sat_*`` labels are refused as inputs: they are results the
    exact solver produces when it degrades, and requesting one directly would let
    a caller present a fallback as a chosen policy.
    """
    if allocator is AllocatorId.CP_SAT:
        return allocate_cpsat(rows, capacity_minutes, cfg, deadline_ms=deadline_ms)
    if allocator in (
        AllocatorId.GREEDY_AFTER_CP_SAT_DEADLINE,
        AllocatorId.GREEDY_AFTER_CP_SAT_ERROR,
    ):
        raise AllocationError(
            f"allocator {allocator.value!r} is a degradation label produced by "
            "allocate_cpsat, not a policy that can be requested."
        )
    if deadline_ms is not None:
        raise AllocationError(
            "deadline_ms only means something to the exact solver; the density and "
            "baseline policies are single passes with no internal budget to miss."
        )
    return CachedQueue.build(rows, cfg).allocate(capacity_minutes, allocator)


@dataclass(frozen=True, slots=True)
class OptimalityGap:
    """One policy's total EV against another's, in money, with the denominator named.

    The ratio is relative to the *exact* total, because that is what the
    approximation is measured against; over the greedy total it would flatter the
    approximation by the amount of its own shortfall.
    """

    approximate: Money
    exact: Money
    gap: Money
    gap_ratio: float
    within_tolerance: bool
    exact_proven: bool

    @property
    def as_percent(self) -> str:
        """The gap as a percentage sentence, for the policy page and the gate."""
        return f"{self.gap_ratio:.2%} of the exact total EV"


@dataclass(frozen=True, slots=True)
class SolverComparison:
    """Both policies on one input, and the money between them."""

    greedy: Allocation
    exact: Allocation

    @property
    def gap(self) -> OptimalityGap:
        """The gap at the configured default ``r``."""
        cfg = self.greedy.cfg
        approximate = self.greedy.total_ev
        exact = self.exact.total_ev
        difference = exact.minor - approximate.minor
        if difference < 0:
            raise AllocationError(
                f"greedy EV {approximate} exceeds exact EV {exact}: an exact solve that "
                "returns a worse objective than a heuristic means the model is wrong."
            )
        ratio = (
            0.0
            if exact.minor == 0
            else scale_div(difference * _RATIO_SCALE, exact.minor) / _RATIO_SCALE
        )
        return OptimalityGap(
            approximate=approximate,
            exact=exact,
            gap=Money(difference, cfg.currency),
            gap_ratio=ratio,
            within_tolerance=ratio <= cfg.solver.agreement_tolerance_ratio,
            exact_proven=self.exact.termination is Termination.COMPLETED,
        )

    def gap_figure(self) -> CurrencyFigure:
        """The gap over the recovery band: a money number, so it carries its block.

        Both sides are re-priced at each ``r``, so the difference stays a
        difference of two policies rather than one number scaled around.
        """
        cfg = self.greedy.cfg
        block = assumption_block(cfg)
        greedy, exact = self.greedy, self.exact

        def value_at(rate: float) -> Money:
            return Money(
                ev_minor(exact.selected, None, rate, cfg)
                - ev_minor(greedy.selected, None, rate, cfg),
                cfg.currency,
            )

        return currency_figure_over_band("Optimality gap, greedy vs CP-SAT exact", block, value_at)

    def sentence(self) -> str:
        """The finding, band first, for the report and the policy page."""
        gap = self.gap
        unproven = (
            ""
            if gap.exact_proven
            else " The exact solve was not proven optimal, so this gap is a lower bound "
            "on the true distance."
        )
        return (
            f"{self.gap_figure().render()}\n"
            f"Greedy by EV density sits {gap.gap_ratio:.2%} of the exact total EV below the "
            f"CP-SAT optimum ({gap.gap}), "
            + ("within" if gap.within_tolerance else "outside")
            + f" the configured {self.greedy.cfg.solver.agreement_tolerance_ratio:.0%} "
            f"agreement tolerance.{unproven}"
        )


def compare_solvers(
    rows: Sequence[AccountEV],
    capacity_minutes: int,
    cfg: Economics,
    *,
    deadline_ms: int | None = None,
) -> SolverComparison:
    """Run both allocators over one input and price the difference.

    Refuses to compare against a degraded exact solve: the gap between greedy and
    a greedy fallback is zero by construction and would be reported as agreement,
    which is the fabricated finding the anti-rubbish rule refuses.
    """
    greedy = allocate_greedy(rows, capacity_minutes, cfg)
    exact = allocate_cpsat(rows, capacity_minutes, cfg, deadline_ms=deadline_ms)
    if exact.allocator is not AllocatorId.CP_SAT:
        raise AllocationError(
            f"no optimality gap is available: the exact solve degraded "
            f"({exact.allocator.value}). Report its fallback label instead of a gap."
        )
    return SolverComparison(greedy=greedy, exact=exact)


@dataclass(frozen=True, slots=True)
class SolverRun:
    """An allocation plus the wall time of the call that produced it.

    ``latency_ms`` is a measurement of this run, not a property of the result, so
    it stays out of :class:`Allocation`: a duration inside a persisted artefact
    would make two runs of ``make verify-determinism`` differ on identical input.
    """

    allocation: Allocation
    latency_ms: int

    @property
    def within_greedy_budget(self) -> bool:
        """Whether the call answered inside ``solver.greedy_budget_ms``."""
        return self.latency_ms <= self.allocation.cfg.solver.greedy_budget_ms


def allocate_timed(
    rows: Sequence[AccountEV],
    capacity_minutes: int,
    cfg: Economics,
    *,
    allocator: AllocatorId = AllocatorId.GREEDY,
    deadline_ms: int | None = None,
) -> SolverRun:
    """Allocate and measure the call, keeping the measurement off the result.

    ``perf_counter`` because it measures elapsed time only; nothing here can make
    a run non-reproducible by comparison (01 §A rule 4, no wall-clock in artefacts).
    """
    started = time.perf_counter()
    allocation = allocate(rows, capacity_minutes, cfg, allocator=allocator, deadline_ms=deadline_ms)
    elapsed = time.perf_counter() - started
    return SolverRun(allocation=allocation, latency_ms=round(elapsed * 1000))


__all__ = [
    "Allocation",
    "AllocationError",
    "AllocationState",
    "AllocatorId",
    "CachedQueue",
    "CpSatDeadlineError",
    "OptimalityGap",
    "SolverComparison",
    "SolverRun",
    "Termination",
    "allocate",
    "allocate_cpsat",
    "allocate_greedy",
    "allocate_highest_exposure",
    "allocate_random",
    "allocate_timed",
    "compare_solvers",
    "solve_cpsat",
]
