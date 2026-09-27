"""The Allocator seam: P5's greedy and CP-SAT behind the harness's ``Allocator`` protocol.

WHAT THIS FILE PROVES. ``oxbow.backtest.allocators.P5Allocator`` is the real queue the P6
ablation prices with. It has to (a) never book more analyst-minutes than the capacity it was
given, because ``realized_fold_economics`` raises if a policy overruns — an allocator that
returns an oversized set breaks the run rather than flattering it; (b) put the exact solver at
least as good a value as the greedy one, since a "proven optimal" answer worse than a
heuristic means a broken model; (c) keep the ≤2 % agreement sentence in the label so a
degraded or out-of-tolerance result is never read as a clean optimum; and (d) honour the
integer-money contract, since these are the exact figures Module C multiplies by probabilities.

Money: the EV and the money terms are integers here on purpose; the probabilities are floats.
"""

from __future__ import annotations

from pathlib import Path

from oxbow.backtest.allocators import P5Allocator
from oxbow.backtest.interfaces import AllocationItem
from oxbow.config import find_repo_root
from oxbow.quant.economics import load_economics

REPO_ROOT: Path = find_repo_root()


def _items() -> list[AllocationItem]:
    """A queue engineered so greedy-by-density and the exact knapsack must diverge.

    Two cheap high-density items sit alongside one large low-density item whose minutes would
    crowd the cheap ones out: greedy takes the cheap pair and cannot fit the big one, while the
    exact solver can trade the tail for a better total. That gap is what the tolerance sentence
    must then price honestly.
    """
    return [
        AllocationItem("A", 0.90, 8_000_000, 75_000, 5),
        AllocationItem("B", 0.80, 7_000_000, 180_000, 12),
        AllocationItem("C", 0.30, 60_000_000, 900_000, 60),
        AllocationItem("D", 0.05, 40_000_000, 1_800_000, 120),
        AllocationItem("E", 0.55, 5_000_000, 375_000, 25),
    ]


def test_allocator_respects_the_capacity_budget() -> None:
    """Neither solver may book more minutes than the period's capacity."""
    allocator = P5Allocator(load_economics(REPO_ROOT))
    capacity = 70
    for policy in ("greedy", "cpsat"):
        result = allocator.allocate(
            items=_items(), capacity_minutes=capacity, policy=policy, seed=1337
        )
        assert result.minutes_used <= capacity, f"{policy} overran the capacity it was handed"
        assert result.minutes_used >= 0
        assert len(set(result.reviewed)) == len(result.reviewed)


def test_exact_is_at_least_as_valuable_as_greedy_and_labels_the_gap() -> None:
    """CP-SAT's optimum dominates greedy, and the ≤2 % rule is stated in the label."""
    economics = load_economics(REPO_ROOT)
    allocator = P5Allocator(economics)
    capacity = 70
    greedy = allocator.allocate(
        items=_items(), capacity_minutes=capacity, policy="greedy", seed=1337
    )
    exact = allocator.allocate(items=_items(), capacity_minutes=capacity, policy="cpsat", seed=1337)
    assert isinstance(greedy.total_ev_minor, int) and isinstance(exact.total_ev_minor, int)
    assert exact.total_ev_minor >= greedy.total_ev_minor
    # The exact arm names its own solver and carries the agreement sentence against greedy;
    # the greedy arm is a single policy and must not invent a gap it did not measure.
    assert "CP-SAT" in exact.allocator_label
    assert "agreement tolerance" in exact.allocator_label
    assert "agreement tolerance" not in greedy.allocator_label
    assert greedy.allocator_label


def test_allocator_returns_total_ev_in_integer_minor_units() -> None:
    """A probability must never have been multiplied by money as a float (DEV-005)."""
    result = P5Allocator(load_economics(REPO_ROOT)).allocate(
        items=_items(), capacity_minutes=12_000, policy="greedy", seed=1337
    )
    assert isinstance(result.total_ev_minor, int)
    assert isinstance(result.minutes_used, int)
    # Generous capacity: every account whose EV is positive is reviewed. The p=0.05 tail
    # account D and the low-exposure E are priced below zero by the review + friction terms
    # and must stay out of the queue.
    assert set(result.reviewed) == {"A", "B", "C"}
