"""P5 frontier: the capacity sweep, its monotonicity proof, and the dominated curve.

Plan §11 asks for achievable (accounts reviewed, expected loss avoided, customers
wrongly touched) swept over ``B``, the operating point marked, and at least one
dominated policy on the same axes. The monotone non-decreasing captured-value claim
is the part that can actually be *proved* rather than spot-checked, so it is asserted
on two inputs: the hand fixture, and a seeded few-hundred-account queue.

Why the invariant holds, in one line: greedy first-fit over a fixed density order,
with non-positive-EV accounts filtered out, cannot lose value when capacity grows by
one minute - at the first decision that flips, the item that newly fits has density
at least everything considered after it, and its weight is exactly one more than the
residual it displaced. The proof is on total EV, which is the objective; it is *not*
on gross loss avoided, whose own ratio is ordered differently once costs and friction
enter, so that quantity is measured and reported rather than asserted.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from oxbow.quant.allocate import AllocatorId
from oxbow.quant.economics import Economics, load_economics
from oxbow.quant.ev import AccountEV, CalibratedScore, price_account
from oxbow.quant.frontier import (
    DOMINATED_POLICIES,
    FrontierError,
    exact_frontier_point,
    sweep_frontier,
)
from oxbow.quant.money import Money
from tests.unit.p5_fixtures import hand_economics

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED = load_economics(REPO_ROOT)


@pytest.fixture(scope="module")
def cfg() -> Economics:
    return hand_economics()


def score(key: str, p_calibrated: float, alert_class: str) -> CalibratedScore:
    """A calibrated score, with its band's observed rate and sample size attached."""
    return CalibratedScore(key, p_calibrated, alert_class, p_calibrated, 250)


def equal_minutes_fixture(cfg: Economics) -> tuple[AccountEV, ...]:
    """Four alerts that all cost 5 minutes, so density ordering is value ordering.

    With equal weights, greedy by value is exactly optimal, which makes the dominance
    claim below structural rather than lucky. Arithmetic at r = 0.50, 100 per minute
    and friction 400:

    ======  ====  ======  =  ==========================  =======  ===============
    acct     p      E     m             EV              density  size-first rank
    ======  ====  ======  =  ==========================  =======  ===============
    ACC-P  0.90   2,000  5    900 - 500 -  40 =   360      72.0        3rd
    ACC-Q  0.50   6,000  5  1,500 - 500 - 200 =   800     160.0        2nd
    ACC-R  0.30   9,000  5  1,350 - 500 - 280 =   570     114.0        1st
    ACC-S  0.20   8,000  5    800 - 500 - 320 =   -20      -4.0   excluded, EV < 0
    ======  ====  ======  =  ==========================  =======  ===============

    On the grid {0, 5, 10, 15, 20, 25, 30} the EV policy captures 0, 800, 1370, 1730,
    1730, 1730, 1730 and highest-exposure-first captures 0, 570, 1370, 1730, 1730,
    1730, 1730: never better, strictly worse at 5 minutes by 230. And the operating
    point at 25 minutes sits on both, which is itself the honest reading - a desk with
    enough capacity stops caring about the order, and the argument is about the tight
    end of the curve.
    """
    return tuple(
        price_account(score(key, p, "A"), Money(exposure, cfg.currency), cfg)
        for key, p, exposure in (
            ("ACC-P", 0.90, 2_000),
            ("ACC-Q", 0.50, 6_000),
            ("ACC-R", 0.30, 9_000),
            ("ACC-S", 0.20, 8_000),
        )
    )


def agreement_rows(cfg: Economics) -> tuple[AccountEV, ...]:
    """The five-alert fixture from the allocation tests, reused rather than restated."""
    from tests.unit.test_p5_allocate import agreement_fixture

    return agreement_fixture(cfg)


def synthetic_rows(cfg: Economics, accounts: int, seed: int) -> tuple[AccountEV, ...]:
    """A seeded queue large enough for the sweep to be a curve rather than a sketch."""
    rng = np.random.default_rng(seed)
    classes = list(cfg.alert_classes)
    return tuple(
        price_account(
            score(
                f"ACC-{index:05d}",
                round(float(np.clip(rng.beta(1.4, 6.0), 0.01, 0.99)), 4),
                classes[int(rng.integers(0, len(classes)))],
            ),
            Money(max(int(rng.lognormal(mean=16.5, sigma=1.2)), 1), cfg.currency),
            cfg,
        )
        for index in range(accounts)
    )


# --- monotonicity ----------------------------------------------------------


def test_capacity_sweep_is_monotone_non_decreasing(cfg: Economics) -> None:
    """Plan §11's gate clause: captured value never falls as the budget grows.

    Checked at every consecutive pair on the grid, on the hand fixture (where the
    expected sequence is written out in :func:`equal_minutes_fixture`) and on a
    400-alert seeded queue under the shipped economics.
    """
    frontier = sweep_frontier(equal_minutes_fixture(cfg), cfg)
    captured = [point.captured_value.minor for point in frontier.points]
    assert captured == [0, 800, 1370, 1730, 1730, 1730, 1730]
    assert frontier.is_monotone
    assert frontier.monotonicity_violations() == ()

    large = sweep_frontier(synthetic_rows(SHIPPED, 400, SHIPPED.seed), SHIPPED)
    assert large.is_monotone, large.monotonicity_violations()
    values = [point.captured_value.minor for point in large.points]
    assert values[0] == 0
    assert values[-1] > values[1]


def test_scheduling_a_losing_review_is_what_would_break_the_invariant(cfg: Economics) -> None:
    """The EV filter the monotonicity proof rests on, tested rather than trusted.

    An unfiltered allocator would spend capacity on ACC-S as soon as it fitted and
    captured value would then fall as capacity rose. ACC-S is large, cheap and
    unprofitable, which is exactly the shape that would slip into a queue ranked on
    size, so its absence from every point on the curve is the assertion.
    """
    rows = equal_minutes_fixture(cfg)
    assert any(not row.is_worth_reviewing for row in rows)
    frontier = sweep_frontier(rows, cfg)
    assert all(point.captured_value.minor >= 0 for point in frontier.points)
    scheduled = {key for point in frontier.points for key in point.allocation.selected_keys}
    assert "ACC-S" not in scheduled
    assert all(row.ev.is_positive for point in frontier.points for row in point.allocation.selected)


# --- the curve, the marker, and the dominated policy ----------------------


def test_the_operating_point_sits_on_the_curve(cfg: Economics) -> None:
    """The marker has to be on the line it marks, or the chart is decoration."""
    frontier = sweep_frontier(agreement_rows(cfg), cfg)
    operating = frontier.operating_point()
    assert operating.capacity_minutes == cfg.capacity.review_minutes_per_period
    assert operating.accounts_reviewed == 3
    assert operating.captured_value == Money(4_960, cfg.currency)
    assert operating.minutes_used == 21
    assert operating.wrong_touched_expected == pytest.approx(0.6)


def test_sweep_grid_is_increasing_and_forced_through_the_operating_point(cfg: Economics) -> None:
    """The grid is integers, deduplicated, ascending, and includes 0 and the marker."""
    grid = cfg.sweep_capacities()
    assert grid == (0, 5, 10, 15, 20, 25, 30)
    assert grid[0] == 0
    assert cfg.capacity.review_minutes_per_period in grid
    assert grid[-1] == cfg.capacity.sweep.max_minutes
    with pytest.raises(FrontierError, match="duplicates"):
        sweep_frontier(agreement_rows(cfg), cfg, capacities=[0, 5, 5, 10])
    with pytest.raises(FrontierError, match="empty capacity grid"):
        sweep_frontier(agreement_rows(cfg), cfg, capacities=[])
    with pytest.raises(FrontierError, match="no point at the operating capacity"):
        sweep_frontier(agreement_rows(cfg), cfg, capacities=[0, 5]).operating_point()


def test_a_dominated_policy_is_drawn_on_the_same_axes(cfg: Economics) -> None:
    """Plan §11: at least one dominated policy, so the frontier means something visually.

    Weakly worse at every capacity and strictly worse at one, on identical
    coordinates - a baseline swept over its own grid would not be a comparison.
    """
    frontier = sweep_frontier(equal_minutes_fixture(cfg), cfg)
    assert set(frontier.dominated) == set(DOMINATED_POLICIES)
    size_curve = frontier.dominated[AllocatorId.BASELINE_HIGHEST_EXPOSURE]
    assert [point.capacity_minutes for point in size_curve] == [
        point.capacity_minutes for point in frontier.points
    ]
    assert frontier.dominates(AllocatorId.BASELINE_HIGHEST_EXPOSURE)
    at_five = {point.capacity_minutes: point.captured_value.minor for point in frontier.points}[5]
    rival_five = {point.capacity_minutes: point.captured_value.minor for point in size_curve}[5]
    assert (at_five, rival_five) == (800, 570)
    # At the operating capacity both curves reach the same set, so the loss there is
    # zero and the table says so rather than implying a difference that is not there.
    assert frontier.loss_at(AllocatorId.BASELINE_HIGHEST_EXPOSURE) == Money(0, cfg.currency)
    with pytest.raises(FrontierError, match="was not swept"):
        frontier.curve(AllocatorId.CP_SAT)


def test_dominance_is_measured_on_a_realistic_queue_not_assumed() -> None:
    """The same comparison at scale, printed so the report can quote a measurement.

    Asserted only that the EV policy is at least as good as each baseline at the
    operating capacity, because that is all the ordering argument supports at
    arbitrary capacity; the margin and the sweep-wide dominance verdict are printed.
    """
    rows = synthetic_rows(SHIPPED, 500, SHIPPED.seed)
    frontier = sweep_frontier(rows, SHIPPED)
    operating = frontier.operating_point()
    for policy in DOMINATED_POLICIES:
        rival = next(
            point
            for point in frontier.curve(policy)
            if point.capacity_minutes == operating.capacity_minutes
        )
        print(
            f"500-alert queue at {operating.capacity_minutes} min: EV policy captures "
            f"{operating.captured_value.minor:,} minor units against "
            f"{rival.captured_value.minor:,} for {policy.value}; loss "
            f"{frontier.loss_at(policy).minor:,}; dominates at every swept capacity: "
            f"{frontier.dominates(policy)}"
        )
        assert operating.captured_value >= rival.captured_value


# --- the three axes and the assumptions ----------------------------------


def test_all_three_axes_are_reported_with_the_band(cfg: Economics) -> None:
    """Accounts reviewed, expected loss avoided, customers wrongly touched.

    A count, a currency figure carrying its assumption block, and an expectation over
    probabilities. Naming them differently is the point: conflating a count with money
    is how a dashboard ends up summing incompatible things.
    """
    point = sweep_frontier(agreement_rows(cfg), cfg).operating_point()
    assert point.accounts_reviewed == 3
    assert point.wrong_touched_expected == pytest.approx(0.6)
    loss_text = point.loss_avoided_figure().render()
    assert "config/economics.yaml" in loss_text
    assert loss_text.splitlines()[0].count(" at r=") == 3
    # Gross at r = 0.50 across the selected set: 4,000 + 2,250 + 1,050 = 7,300.
    assert "73.00 UGX at r=0.50" in loss_text


def test_the_frontier_table_carries_its_assumptions(cfg: Economics) -> None:
    """A table of amounts is still a figure, so the block is appended below it.

    The table prints minor units rather than rendered currency: an unlabelled column
    of amounts is what plan §18 refuses, and minor units cannot be read as a price
    without the divisor the assumption block supplies.
    """
    table = sweep_frontier(agreement_rows(cfg), cfg).render_table()
    assert "config/economics.yaml" in table
    assert "EV_i = p_i * E_i * r" in table
    assert "captured EV (minor UGX)" in table
    assert "wrong touches (expected)" in table
    assert AllocatorId.CP_SAT.value not in table


def test_exact_point_is_solved_both_ways(cfg: Economics) -> None:
    """The marker's companion claim: what the exact solver would have chosen here."""
    greedy_point, exact_point = exact_frontier_point(agreement_rows(cfg), cfg)
    assert greedy_point.policy is AllocatorId.GREEDY
    assert exact_point.policy is AllocatorId.CP_SAT
    assert exact_point.captured_value >= greedy_point.captured_value
    assert exact_point.allocation.termination.value == "completed"


def test_an_empty_queue_sweeps_to_an_empty_frontier_rather_than_crashing(cfg: Economics) -> None:
    """No alerts is a state, and the curve shows it as one at every capacity."""
    frontier = sweep_frontier((), cfg)
    assert frontier.is_monotone
    assert all(point.accounts_reviewed == 0 for point in frontier.points)
    assert all(point.captured_value.is_zero for point in frontier.points)
    assert frontier.operating_point().allocation.state.value == "nothing_alerted"


def test_the_shipped_sweep_grid_has_the_width_the_ui_draws() -> None:
    """The curve the policy page renders is the one config defines, not one invented here."""
    grid = SHIPPED.sweep_capacities()
    assert len(grid) >= SHIPPED.capacity.sweep.points
    assert SHIPPED.capacity.sweep.min_minutes == 0
    assert SHIPPED.capacity.sweep.points == 31
    assert SHIPPED.capacity.sweep.max_minutes in grid
