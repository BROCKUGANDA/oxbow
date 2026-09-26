"""The efficient frontier: what each capacity level actually buys.

Plan §11 asks for a sweep over ``B`` emitting achievable (accounts reviewed,
expected loss avoided, customers wrongly touched), the operating point, and at
least one dominated policy on the same axes. The dominated curve is the point of
the picture: a frontier with nothing below it is just a line, and the claim the
product is making — that pricing the queue beats sizing the queue — only appears
as a gap between two curves on identical coordinates.

The monotonicity guarantee is arithmetic, not hope. Greedy first-fit over a fixed
density order is non-decreasing in total EV as capacity grows: at the first
decision that changes when capacity steps by one minute, the item that newly fits
has density at least that of everything considered after it, so the value it adds
can never be smaller than what the tighter packing collected afterwards. That is
why the invariant is asserted on the *objective* (total EV) and not on gross loss
avoided, whose own ratio is ordered differently once costs and friction enter —
and why non-positive-EV accounts are excluded before the scan, since a scheduled
loss would break the argument on purpose.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType

from oxbow.quant.allocate import (
    Allocation,
    AllocatorId,
    CachedQueue,
    compare_solvers,
)
from oxbow.quant.economics import CurrencyFigure, Economics, assumption_block
from oxbow.quant.ev import AccountEV
from oxbow.quant.money import Money, QuantError

# The policies swept alongside the EV objective. Both are dominated in the
# fixture and both are things real desks do, which is what makes the gap readable.
DOMINATED_POLICIES: tuple[AllocatorId, ...] = (
    AllocatorId.BASELINE_HIGHEST_EXPOSURE,
    AllocatorId.BASELINE_RANDOM,
)


class FrontierError(QuantError):
    """Raised when a sweep cannot be drawn, rather than drawn wrong."""


@dataclass(frozen=True, slots=True)
class FrontierPoint:
    """One capacity level on one policy's curve.

    Every quantity is derived from the :class:`~oxbow.quant.allocate.Allocation`
    that produced it, so a frontier row and the allocation behind it cannot
    disagree — two records of one fact is the drift this layer keeps refusing.
    """

    allocation: Allocation

    @property
    def policy(self) -> AllocatorId:
        """Which allocator produced this point."""
        return self.allocation.allocator

    @property
    def capacity_minutes(self) -> int:
        """The budget this point was computed under."""
        return self.allocation.capacity_minutes

    @property
    def accounts_reviewed(self) -> int:
        """First axis: how many alerts the budget touches."""
        return self.allocation.accounts_reviewed

    @property
    def minutes_used(self) -> int:
        """Analyst-minutes actually consumed at this capacity."""
        return self.allocation.minutes_used

    @property
    def captured_value(self) -> Money:
        """The objective the allocator maximises: total EV at the default ``r``.

        Named *captured value* because plan §11's monotonicity requirement is about
        the quantity the policy optimises, and that is net expected value rather
        than the gross loss-avoided headline.
        """
        return self.allocation.total_ev

    @property
    def expected_loss_avoided(self) -> Money:
        """Second axis: gross loss the reviewed set is expected to prevent."""
        return self.allocation.expected_loss_avoided

    @property
    def wrong_touched_expected(self) -> float:
        """Third axis: expected count of legitimate customers disturbed.

        A count, not money, and it is the axis that stops "more capacity" reading
        as pure benefit: every extra account reviewed is also another chance to
        freeze someone who was clean.
        """
        return self.allocation.wrong_touch_expected

    def captured_value_figure(self) -> CurrencyFigure:
        """This point's captured value over the recovery band."""
        return self.allocation.ev_figure()

    def loss_avoided_figure(self) -> CurrencyFigure:
        """This point's gross loss avoided over the band."""
        return self.allocation.loss_avoided_figure()


@dataclass(frozen=True, slots=True)
class Frontier:
    """The swept curves, the operating point, and the dominance read-off.

    ``points`` is the EV policy's achievable set — the frontier proper.
    ``dominated`` holds the baselines on the same capacity grid, so a chart can
    draw them against it without re-running anything.
    """

    points: tuple[FrontierPoint, ...]
    dominated: Mapping[AllocatorId, tuple[FrontierPoint, ...]]
    operating_capacity_minutes: int
    cfg: Economics

    @property
    def is_monotone(self) -> bool:
        """Whether captured value never falls as capacity rises."""
        return not self.monotonicity_violations()

    def monotonicity_violations(self) -> tuple[tuple[int, int], ...]:
        """Capacity pairs where a larger budget captured *less* value.

        Empty is the contract; anything else is a bug in the ordering or in the
        non-positive-EV filter, and the caller gets the pairs rather than a bool so
        the failure is diagnosable from the message alone.
        """
        ordered = sorted(self.points, key=lambda point: point.capacity_minutes)
        bad: list[tuple[int, int]] = []
        for previous, current in pairwise(ordered):
            if current.captured_value.minor < previous.captured_value.minor:
                bad.append((previous.capacity_minutes, current.capacity_minutes))
        return tuple(bad)

    def operating_point(self) -> FrontierPoint:
        """The point at the configured capacity — the marker on the chart.

        Fails loud rather than returning ``None``: plan §11 requires the operating
        point to be *on* the curve, and a curve that misses it would leave a chart
        with a marker floating in blank space.
        """
        for point in self.points:
            if point.capacity_minutes == self.operating_capacity_minutes:
                return point
        raise FrontierError(
            f"the sweep has no point at the operating capacity "
            f"{self.operating_capacity_minutes} minutes; sweep_capacities() is supposed "
            "to force it into the grid."
        )

    def curve(self, policy: AllocatorId) -> tuple[FrontierPoint, ...]:
        """One policy's curve on the shared capacity grid."""
        if policy is AllocatorId.GREEDY:
            return self.points
        try:
            return self.dominated[policy]
        except KeyError as exc:
            raise FrontierError(
                f"policy {policy.value} was not swept; swept policies are "
                f"{sorted(item.value for item in self.dominated)}"
            ) from exc

    def dominates(self, policy: AllocatorId) -> bool:
        """Whether the EV curve is at least as good everywhere and better somewhere.

        Weak dominance plus one strict point. "Better somewhere" matters: two
        identical curves would dominate each other trivially and the chart would
        claim a difference that is not there.
        """
        other = {point.capacity_minutes: point for point in self.curve(policy)}
        if len(other) != len(self.points):
            raise FrontierError(
                f"cannot compare curves on different grids: {len(self.points)} EV points "
                f"against {len(other)} for {policy.value}"
            )
        strict = False
        for point in self.points:
            rival = other[point.capacity_minutes]
            if point.captured_value.minor < rival.captured_value.minor:
                return False
            if point.captured_value.minor > rival.captured_value.minor:
                strict = True
        return strict

    def loss_at(self, policy: AllocatorId) -> Money:
        """EV given up at the operating point by running ``policy`` instead.

        This is the number the policy page argues with: what the desk loses by
        reviewing the biggest accounts rather than the most valuable ones.
        """
        operating = self.operating_point()
        rival = next(
            (
                point
                for point in self.curve(policy)
                if point.capacity_minutes == operating.capacity_minutes
            ),
            None,
        )
        if rival is None:
            raise FrontierError(f"{policy.value} has no point at the operating capacity")
        return Money(
            operating.captured_value.minor - rival.captured_value.minor, self.cfg.currency
        )

    def render_table(self) -> str:
        """The sweep as text: counts, minutes, wrong touches, and minor units.

        Money appears as *minor units* here, never as a rendered currency amount,
        and the assumption block is appended below the table: a table of amounts is
        still a figure, and plan §18 does not exempt the ones a reviewer can read
        without opening a chart.
        """
        header = (
            f"capacity | accounts | minutes | wrong touches (expected) | captured EV "
            f"(minor {self.cfg.currency}) | policy"
        )
        lines = [header, "-" * len(header)]
        rows = [
            *self.points,
            *(point for curve in self.dominated.values() for point in curve),
        ]
        for point in sorted(rows, key=lambda item: (item.policy.value, item.capacity_minutes)):
            lines.append(
                f"{point.capacity_minutes:>8} | {point.accounts_reviewed:>8} | "
                f"{point.minutes_used:>7} | {point.wrong_touched_expected:>24.2f} | "
                f"{point.captured_value.minor:>21} | {point.policy.value}"
            )
        lines.append("")
        lines.append(assumption_block(self.cfg).text)
        return "\n".join(lines)


def sweep_frontier(
    rows: Sequence[AccountEV],
    cfg: Economics,
    *,
    capacities: Sequence[int] | None = None,
    dominated_policies: Sequence[AllocatorId] = DOMINATED_POLICIES,
) -> Frontier:
    """Sweep capacity and record what each budget buys, on one shared grid.

    The EV inputs are priced once and the capacity loop re-runs only the scan
    (plan §11: the slider moves over a cached array). Sweeping the baselines over
    the *same* grid is what makes the curves comparable; a baseline drawn on its
    own capacities is decoration.
    """
    grid = tuple(cfg.sweep_capacities()) if capacities is None else tuple(sorted(capacities))
    if len(set(grid)) != len(grid):
        raise FrontierError(f"capacity grid has duplicates: {sorted(grid)}")
    if not grid:
        raise FrontierError("an empty capacity grid is not a sweep")
    queue = CachedQueue.build(rows, cfg)
    return Frontier(
        points=tuple(
            FrontierPoint(queue.allocate(capacity, AllocatorId.GREEDY)) for capacity in grid
        ),
        dominated=MappingProxyType(
            {
                policy: tuple(
                    FrontierPoint(queue.allocate(capacity, policy)) for capacity in grid
                )
                for policy in dominated_policies
            }
        ),
        operating_capacity_minutes=cfg.capacity.review_minutes_per_period,
        cfg=cfg,
    )


def exact_frontier_point(
    rows: Sequence[AccountEV], cfg: Economics, *, capacity_minutes: int | None = None
) -> tuple[FrontierPoint, FrontierPoint]:
    """The operating point solved both ways, for the marker's "and the exact answer is" note.

    CP-SAT is offline work (plan §11: greedy answers the slider, the exact solve
    runs async), so this is deliberately a single point rather than a swept curve:
    sweeping an exact solver across 31 capacities would be a batch job pretending
    to be an interaction.
    """
    budget = cfg.capacity.review_minutes_per_period if capacity_minutes is None else capacity_minutes
    comparison = compare_solvers(rows, budget, cfg)
    return (
        FrontierPoint(comparison.greedy),
        FrontierPoint(comparison.exact),
    )


__all__ = [
    "DOMINATED_POLICIES",
    "Frontier",
    "FrontierError",
    "FrontierPoint",
    "exact_frontier_point",
    "sweep_frontier",
]
