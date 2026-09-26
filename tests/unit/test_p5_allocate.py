"""P5 allocation: the behaviours plan §11 names, plus the two measurements.

The arithmetic fixtures are hand-computed against ``tests/unit/p5_fixtures.py`` (the
paper-checkable economy: 100 minor units per analyst-minute, friction 400, default
``r`` 0.5, band 0.2/0.5/0.8, review minutes A=5 B=6 C=9 D=10 E=15 F=20). Every
expected value below is written out in the test that uses it, so it can be checked
with a pencil rather than by running the code under test.

The two *measurements* — greedy latency, and the greedy-versus-exact gap on a
realistic few-thousand-account queue — run against the shipped
``config/economics.yaml`` and print what they observed. Asserted: the latency budget,
and that an exact solve is never beaten by an approximation. Printed: the size of the
gap, because that is a measurement and not a fixture.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from oxbow.config import ConfigError
from oxbow.quant.allocate import (
    AllocationError,
    AllocationState,
    AllocatorId,
    CachedQueue,
    CpSatDeadlineError,
    SolverComparison,
    Termination,
    allocate,
    allocate_cpsat,
    allocate_greedy,
    allocate_highest_exposure,
    allocate_random,
    allocate_timed,
    compare_solvers,
    solve_cpsat,
)
from oxbow.quant.economics import Economics, load_economics
from oxbow.quant.ev import (
    AccountEV,
    CalibratedScore,
    PricingError,
    break_even_recovery,
    ev_figure,
    positive_ev_rows,
    price_account,
)
from oxbow.quant.money import CurrencyMismatchError, Money
from tests.unit.p5_fixtures import hand_economics

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED = load_economics(REPO_ROOT)


@pytest.fixture(scope="module")
def cfg() -> Economics:
    return hand_economics()


def priced(
    cfg: Economics, key: str, p_calibrated: float, minor: int, alert_class: str
) -> AccountEV:
    """One priced alert with a plausible calibration bin behind it."""
    return price_account(
        CalibratedScore(key, p_calibrated, alert_class, p_calibrated, 400),
        Money(minor, cfg.currency),
        cfg,
    )


def agreement_fixture(cfg: Economics) -> tuple[AccountEV, ...]:
    """Five alerts whose EVs add up on paper; capacity is 25 analyst-minutes.

    =====  ====  ======  ==  ==============================  =======  --------------
    acct    p      E     m   EV = p*E*r - m*100 - (1-p)*400  density  taken at B=25
    =====  ====  ======  ==  ==============================  =======  --------------
    ACC-A  0.80  10,000  10  4,000 - 1,000 -  80 =  2,920    292.00   yes
    ACC-B  0.50   8,000  20  2,000 - 2,000 - 200 =   -200    -10.00  excluded, EV<0
    ACC-C  0.90   5,000   5  2,250 -   500 -  40 =  1,710    342.00   yes
    ACC-D  0.60   9,000  15  2,700 - 1,500 - 160 =  1,040     69.33   no: 15 > 10 left
    ACC-E  0.70   3,000   6  1,050 -   600 - 120 =    330     55.00   yes
    =====  ====  ======  ==  ==============================  =======  --------------

    Greedy takes C (5), A (10), skips D (needs 15, 10 remain), takes E (6): 21
    minutes, EV 1,710 + 2,920 + 330 = **4,960**. Feasible subsets of the four viable
    accounts within 25 minutes: {A,D} exactly 25 for 3,960; {C,A} 15 for 4,630;
    {C,D} 20 for 2,750; {A,E} 16 for 3,250; {C,E} 11 for 2,040; {D,E} 21 for 1,370;
    {C,A,E} 21 for 4,960; and {C,A,D}, {A,D,E}, {C,D,E} all exceed 25. So 4,960 is
    optimal and the exact solve must return the same number: gap zero, which is the
    §11 gate clause.
    """
    return (
        priced(cfg, "ACC-A", 0.80, 10_000, "D"),
        priced(cfg, "ACC-B", 0.50, 8_000, "F"),
        priced(cfg, "ACC-C", 0.90, 5_000, "A"),
        priced(cfg, "ACC-D", 0.60, 9_000, "E"),
        priced(cfg, "ACC-E", 0.70, 3_000, "B"),
    )


def dominated_fixture(cfg: Economics) -> tuple[AccountEV, ...]:
    """A three-alert case where density ordering demonstrably loses.

    =====  ====  =====  ==  =============================  =======
    acct    p      E    m   EV                            density
    =====  ====  =====  ==  =============================  =======
    ACC-X  0.80  5,200  10  2,080 - 1,000 - 80 = 1,000     100.00
    ACC-Y  0.80  4,600   9  1,840 -   900 - 80 =   860      95.56
    ACC-Z  0.80  3,100   6  1,240 -   600 - 80 =   560      93.33
    =====  ====  =====  ==  =============================  =======

    At capacity 15 greedy takes X (10 minutes, 5 left) and then neither Y (9) nor Z
    (6) fits, for 1,000. The exact optimum is Y + Z at exactly 15 minutes for 1,420,
    so the gap is **420** minor units: 420/1,420 = 29.58 % of the exact total. A
    fixture where both solvers agree proves the mechanism only if a second one shows
    the machinery reporting disagreement, which is this one.
    """
    return (
        priced(cfg, "ACC-X", 0.80, 5_200, "D"),
        priced(cfg, "ACC-Y", 0.80, 4_600, "C"),
        priced(cfg, "ACC-Z", 0.80, 3_100, "B"),
    )


def unprofitable_fixture(cfg: Economics) -> tuple[AccountEV, ...]:
    """Two alerts that lose money at the configured ``r``, with the break-even solved.

    =====  ====  =====  ==  ============================
    acct    p      E    m   EV
    =====  ====  =====  ==  ============================
    ACC-L  0.30  2,000  15    300 - 1,500 - 280 = -1,480
    ACC-M  0.20  1,000   6    100 -   600 - 320 =   -820
    =====  ====  =====  ==  ============================

    Break-even r* = (c + (1-p)f) / (p*E): for L (1,500 + 280) / (0.30 * 2,000) =
    1,780/600 = **2.96667**; for M (600 + 320) / 200 = 4.60. Both sit above the
    admissible open interval (0, 1), so no recovery rate the product allows makes
    either review worth running - and the message has to carry the rate, not just
    the conclusion.
    """
    return (
        priced(cfg, "ACC-L", 0.30, 2_000, "E"),
        priced(cfg, "ACC-M", 0.20, 1_000, "B"),
    )


# --- the named tests ------------------------------------------------------


def test_zero_capacity_returns_forgone_value(cfg: Economics) -> None:
    """Plan §11: a valid input, an empty allocation, and the forgone value named.

    Capacity zero is neither an error nor an empty list: the result states which
    accounts were viable, how much expected value the constraint destroyed, and
    renders that as a band figure so the UI can show what the budget costs.
    """
    rows = agreement_fixture(cfg)
    result = allocate_greedy(rows, 0, cfg)
    assert result.state is AllocationState.NO_CAPACITY
    assert result.selected == ()
    assert result.minutes_used == 0
    assert len(result.candidates) == 4  # ACC-B is excluded for being unprofitable
    # 2,920 + 1,710 + 1,040 + 330 = 6,000 of viable EV, none of it reachable.
    assert result.forgone_ev == Money(6_000, cfg.currency)
    assert result.total_ev == Money(0, cfg.currency)
    assert "0 analyst-minutes" in result.message
    assert "6000 minor" in result.message
    assert "stated outcome" in result.message
    rendered = result.forgone_figure().render()
    assert "at r=0.20" in rendered and "at r=0.50" in rendered and "at r=0.80" in rendered
    assert "config/economics.yaml" in rendered


def test_all_negative_ev_recommends_nothing(cfg: Economics) -> None:
    """Plan §11: the message, *together with* the recovery rate that would change it.

    "Nothing pays for itself" without the break-even rate is a shrug. The solve is
    r* = (c + (1-p)f) / (p*E) per account, minimised over the set, and reported as
    inadmissible here rather than clamped into range.
    """
    rows = unprofitable_fixture(cfg)
    result = allocate_greedy(rows, 25, cfg)
    assert result.state is AllocationState.NOTHING_PAYS
    assert result.selected == ()
    assert result.total_ev == Money(0, cfg.currency)
    assert "under these assumptions no investigation pays for itself" in result.message
    assert "r = 2.9667" in result.message
    assert "ACC-L" in result.message
    assert "outside the admissible open interval" in result.message
    break_even = break_even_recovery(rows, cfg)
    assert break_even.rate is not None
    assert not break_even.admissible
    assert not break_even.pays_at_default
    # The rate is carried as an integer micro-ratio, so its quantum is 1e-6.
    assert math.isclose(break_even.rate, 1780 / 600, abs_tol=1e-6)


def test_one_profitable_account_makes_the_queue_non_empty(cfg: Economics) -> None:
    """The other side of the same message: what a marginal account turns on.

    ACC-C breaks even at (500 + 40) / (0.90 * 5,000) = 540/4,500 = 0.12, so at the
    default r = 0.50 it pays and the sentence must stop claiming nothing pays.
    """
    rows = (priced(cfg, "ACC-C", 0.90, 5_000, "A"),)
    break_even = break_even_recovery(rows, cfg)
    assert break_even.pays_at_default
    assert math.isclose(break_even.rate, 540 / 4500, abs_tol=1e-6)
    assert "already pay for themselves" in break_even.message
    assert allocate_greedy(rows, 25, cfg).state is AllocationState.SELECTED


def test_ev_density_no_zero_divide(cfg: Economics) -> None:
    """Plan §11: the minutes floor is enforced at load, so density never divides zero.

    Three layers, deliberately redundant because the failure mode is invisible:
    config load refuses a zero floor, ``price_account`` refuses a class below the
    floor even when the economy was assembled by hand (which is how a test or a later
    phase could bypass the loader), and an unconfigured class is refused outright. The
    density denominator is therefore never reached at zero.
    """
    with pytest.raises(ConfigError, match="min_review_minutes"):
        hand_economics(
            analyst=hand_economics().analyst.__class__(
                cost_per_hour_minor=6_000,
                cost_per_minute_minor=100,
                hours_per_period=1,
                min_review_minutes=0,
            )
        )
    zero_minute_class = hand_economics(review_minutes_by_alert_class={"A": 0, "B": 6})
    with pytest.raises(PricingError, match="below the floor"):
        priced(zero_minute_class, "ACC-Z", 0.90, 10_000, "A")
    row = priced(cfg, "ACC-N", 0.90, 5_000, "A")
    assert row.review_minutes == cfg.analyst.min_review_minutes
    assert row.density_ratio == row.ev.minor / row.review_minutes
    assert math.isfinite(row.density_ratio)
    with pytest.raises(ConfigError, match="no review minutes configured"):
        priced(cfg, "ACC-W", 0.90, 5_000, "Q")


def test_solver_timeout_falls_back(cfg: Economics, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan §11: a hard deadline returns greedy, labelled as such.

    The label is the deliverable: a UI that renders a degraded approximation
    identically to a proven optimum is presenting an unmeasured number as a measured
    one. ``compare_solvers`` then refuses to quote a gap against the fallback, because
    the gap between greedy and greedy is zero by construction and would read as
    perfect agreement.
    """

    def pretend_out_of_time(*args: object, **kwargs: object) -> tuple[frozenset[str], bool]:
        raise CpSatDeadlineError("CP-SAT found a feasible packing but no proof in 1 ms")

    monkeypatch.setattr("oxbow.quant.allocate.solve_cpsat", pretend_out_of_time)
    result = allocate_cpsat(agreement_fixture(cfg), 25, cfg, deadline_ms=1)
    assert result.allocator is AllocatorId.GREEDY_AFTER_CP_SAT_DEADLINE
    assert result.termination is Termination.DEADLINE_EXCEEDED
    assert result.deadline_ms == 1
    assert "DEGRADED" in result.message
    assert "no optimality gap" in result.message
    assert "DEGRADED" in result.allocator_label
    assert result.selected_keys == ("ACC-C", "ACC-A", "ACC-E")
    assert result.total_ev == Money(4_960, cfg.currency)
    with pytest.raises(AllocationError, match="no optimality gap is available"):
        compare_solvers(agreement_fixture(cfg), 25, cfg)


def test_solver_error_is_labelled_separately_from_a_deadline(
    cfg: Economics, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken model and a slow model are different problems, not one sentence.

    Collapsing them would let a real fault hide behind the reassurance written for an
    ordinary slider drag.
    """

    def explode(*args: object, **kwargs: object) -> tuple[frozenset[str], bool]:
        raise ZeroDivisionError("model build failed")

    monkeypatch.setattr("oxbow.quant.allocate.solve_cpsat", explode)
    result = allocate_cpsat(agreement_fixture(cfg), 25, cfg)
    assert result.allocator is AllocatorId.GREEDY_AFTER_CP_SAT_ERROR
    assert result.termination is Termination.SOLVER_ERROR
    assert "ZeroDivisionError" in result.message
    assert "DEGRADED" in result.allocator_label


def test_every_result_records_which_allocator_produced_it(cfg: Economics) -> None:
    """The plan asks for the producing policy on *every* result, empties included."""
    rows = agreement_fixture(cfg)
    for capacity, allocator in [
        (25, AllocatorId.GREEDY),
        (0, AllocatorId.GREEDY),
        (25, AllocatorId.BASELINE_HIGHEST_EXPOSURE),
        (25, AllocatorId.BASELINE_RANDOM),
    ]:
        result = allocate(rows, capacity, cfg, allocator=allocator)
        assert result.allocator is allocator
        assert result.allocator_label
    empty = allocate((), 25, cfg)
    assert empty.state is AllocationState.NOTHING_ALERTED
    assert "no scored accounts" in empty.message
    assert empty.allocator is AllocatorId.GREEDY
    assert all(not row.is_worth_reviewing for row in unprofitable_fixture(cfg))


# --- the two solvers on one input ----------------------------------------


def test_greedy_and_exact_agree_on_the_hand_fixture(cfg: Economics) -> None:
    """The §11 gate clause: both solvers within 2 % of total EV, on the arithmetic above."""
    comparison = compare_solvers(agreement_fixture(cfg), 25, cfg)
    assert comparison.greedy.total_ev == Money(4_960, cfg.currency)
    assert comparison.exact.total_ev == Money(4_960, cfg.currency)
    assert comparison.gap.gap == Money(0, cfg.currency)
    assert comparison.gap.gap_ratio == 0.0
    assert comparison.gap.within_tolerance
    assert comparison.gap.exact_proven
    assert comparison.exact.allocator is AllocatorId.CP_SAT
    assert set(comparison.exact.selected_keys) == set(comparison.greedy.selected_keys)
    assert "at r=0.50" in comparison.gap_figure().render()


def test_exact_solve_beats_greedy_where_density_ordering_loses(cfg: Economics) -> None:
    """The gap machinery has to bite, or the agreement above proves nothing.

    Expected from :func:`dominated_fixture`: greedy 1,000, exact 1,420, gap 420,
    which is 29.58 % of the exact total and outside the configured 2 % tolerance.
    """
    comparison = compare_solvers(dominated_fixture(cfg), 15, cfg)
    assert comparison.greedy.total_ev == Money(1_000, cfg.currency)
    assert comparison.exact.total_ev == Money(1_420, cfg.currency)
    assert comparison.gap.gap == Money(420, cfg.currency)
    assert comparison.gap.gap_ratio == pytest.approx(420 / 1420, abs=1e-9)
    assert not comparison.gap.within_tolerance
    assert comparison.exact.selected_keys == ("ACC-Y", "ACC-Z")
    assert "29.58%" in comparison.sentence()


def test_exact_solver_refuses_a_worse_objective_than_the_heuristic(cfg: Economics) -> None:
    """A knapsack solver that returns less than greedy is a broken model, not a result.

    Fed two allocations in inverted order. Without the guard the comparison would
    publish a negative gap and the policy page would claim the approximation beats
    optimality.
    """
    rows = agreement_fixture(cfg)
    greedy = allocate_greedy(rows, 25, cfg)
    smaller = allocate_greedy(rows, 5, cfg)
    assert smaller.total_ev < greedy.total_ev
    with pytest.raises(AllocationError, match="worse objective"):
        # The bare access IS the assertion: the guard fires while evaluating `gap`,
        # so there is nothing to compare the result against.
        SolverComparison(greedy=greedy, exact=smaller).gap  # noqa: B018


def test_both_solvers_report_the_same_empty_state(cfg: Economics) -> None:
    """Zero capacity and 'capacity too small' resolve identically under either solver."""
    rows = agreement_fixture(cfg)
    for capacity in (0, 3):
        exact = allocate_cpsat(rows, capacity, cfg)
        greedy = allocate_greedy(rows, capacity, cfg)
        assert exact.state is AllocationState.NO_CAPACITY
        assert greedy.state is AllocationState.NO_CAPACITY
        assert exact.forgone_ev == greedy.forgone_ev == Money(6_000, cfg.currency)
    assert "0 analyst-minutes" in allocate_cpsat(rows, 0, cfg).message
    assert "smaller than the cheapest viable review" in allocate_cpsat(rows, 3, cfg).message


def test_negative_capacity_is_an_error_and_zero_is_not(cfg: Economics) -> None:
    """The boundary between an impossible request and a documented empty answer."""
    with pytest.raises(AllocationError, match=">= 0"):
        allocate_greedy(agreement_fixture(cfg), -5, cfg)


# --- ranking, determinism, and the baselines -----------------------------


def test_density_tie_break_is_deterministic(cfg: Economics) -> None:
    """Ties break on ``account_key``, so queue order cannot change between runs.

    Two accounts with identical inputs and different keys must produce the same
    recommendation under any input permutation, which is what 01 §A rule 4 asks for
    and what a bare float comparison would quietly lose.
    """
    twin_a = priced(cfg, "ACC-AAA", 0.80, 10_000, "D")
    twin_b = priced(cfg, "ACC-BBB", 0.80, 10_000, "D")
    assert twin_a.density_ratio == twin_b.density_ratio
    forward = allocate_greedy([twin_a, twin_b], 10, cfg)
    backward = allocate_greedy([twin_b, twin_a], 10, cfg)
    assert forward.selected_keys == ("ACC-AAA",)
    assert backward.selected_keys == forward.selected_keys
    assert [row.account_key for row in positive_ev_rows([twin_b, twin_a])] == [
        "ACC-AAA",
        "ACC-BBB",
    ]


def test_baselines_are_dominated_at_the_capacity_where_density_is_optimal(
    cfg: Economics,
) -> None:
    """Highest-exposure-first loses to EV density where density is provably optimal.

    At 25 minutes the density policy attains the proven optimum (4,960), so no
    ordering of the same candidate set can exceed it and anything below is strictly
    dominated. Dominance is *not* claimed universally: at 10 minutes size-first picks
    ACC-A for 2,920 while density picks ACC-C for 1,710, because greedy by density is
    optimal only for the fractional relaxation. Asserting it everywhere would claim
    something the policy does not guarantee.
    """
    rows = agreement_fixture(cfg)
    by_density = allocate_greedy(rows, 25, cfg)
    by_size = allocate_highest_exposure(rows, 25, cfg)
    by_chance = allocate_random(rows, 25, cfg)
    assert by_density.total_ev == Money(4_960, cfg.currency)
    assert by_size.total_ev == Money(3_960, cfg.currency)
    assert by_density.total_ev > by_size.total_ev
    assert by_density.total_ev >= by_chance.total_ev
    assert by_size.selected_keys == ("ACC-A", "ACC-D")
    assert "dominated baseline" in by_size.message
    assert (
        allocate_highest_exposure(rows, 10, cfg).total_ev > allocate_greedy(rows, 10, cfg).total_ev
    )


def test_random_baseline_is_reproducible_and_draws_only_the_config_seed(cfg: Economics) -> None:
    """A baseline that cannot be re-run is an anecdote; global RNG state must not move it."""
    rows = agreement_fixture(cfg)
    first = allocate_random(rows, 25, cfg)
    second = allocate_random(rows, 25, cfg)
    assert first.selected_keys == second.selected_keys
    assert str(cfg.seed) in first.message
    random.seed(7)
    against_seven = allocate_random(rows, 25, cfg)
    random.seed(999)
    against_nine_nine_nine = allocate_random(rows, 25, cfg)
    assert against_seven.selected_keys == against_nine_nine_nine.selected_keys


def test_cached_queue_and_fresh_pricing_agree(cfg: Economics) -> None:
    """The slider path and the precompute path must not disagree about anything.

    ``CachedQueue`` exists so re-running allocation does not re-price; if the two
    returned different sets, the dashboard would show one number and the batch report
    another.
    """
    rows = agreement_fixture(cfg)
    queue = CachedQueue.build(rows, cfg)
    assert queue.allocate(25).selected_keys == allocate_greedy(rows, 25, cfg).selected_keys
    assert queue.allocate(0).state is AllocationState.NO_CAPACITY
    assert queue.allocate(25, AllocatorId.CP_SAT).allocator is AllocatorId.CP_SAT
    with pytest.raises(AllocationError, match="degradation label"):
        allocate(rows, 25, cfg, allocator=AllocatorId.GREEDY_AFTER_CP_SAT_DEADLINE)
    with pytest.raises(AllocationError, match="only means something to the exact solver"):
        allocate(rows, 25, cfg, deadline_ms=10)


def test_selecting_an_unpriced_account_is_refused(cfg: Economics) -> None:
    """A queue that references an account nobody priced is a join bug, not a zero."""
    with pytest.raises(PricingError, match="not priced"):
        ev_figure(agreement_fixture(cfg), ["ACC-GHOST"], cfg)


def test_pricing_refuses_a_cross_currency_exposure(cfg: Economics) -> None:
    """An exposure in another money cannot be netted against UGX costs."""
    score = CalibratedScore("ACC-Q", 0.9, "A", 0.9, 10)
    with pytest.raises(CurrencyMismatchError, match="two unrelated monies"):
        price_account(score, Money(5_000, "EUR"), cfg)


def test_a_probability_outside_zero_one_is_refused(cfg: Economics) -> None:
    """A p of 1.4 is an upstream calibration bug, and here it would be a money bug."""
    with pytest.raises(PricingError, match=r"outside \[0, 1\]"):
        CalibratedScore("ACC-P", 1.4, "A", 0.5, 10)
    with pytest.raises(PricingError, match=r"outside \[0, 1\]"):
        CalibratedScore("ACC-P", 0.5, "A", -0.1, 10)


def test_solve_cpsat_is_callable_on_its_own(cfg: Economics) -> None:
    """The exact solver is exposed for offline jobs that already hold the candidate set."""
    rows = positive_ev_rows(agreement_fixture(cfg))
    selected, proven = solve_cpsat(rows, 25, cfg, cfg.solver.cpsat_deadline_ms)
    assert proven
    assert selected == {"ACC-A", "ACC-C", "ACC-E"}


def test_allocation_carries_both_money_axes_with_the_band(cfg: Economics) -> None:
    """Net EV and gross loss avoided, each rendered with its assumptions.

    Gross at r = 0.50 is 4,000 + 2,250 + 1,050 = 7,300, and net is 7,300 - 2,100
    review cost (10+5+6 minutes at 100) - 240 friction (80+40+120) = 4,960, so the
    two figures differ by exactly the costs and neither is the other wearing a label.
    """
    result = allocate_greedy(agreement_fixture(cfg), 25, cfg)
    ev_text = result.ev_figure().render()
    loss_text = result.loss_avoided_figure().render()
    assert "greedy_ev_density" in ev_text
    assert ev_text.splitlines()[0].count(" at r=") == 3
    assert loss_text.splitlines()[0].count(" at r=") == 3
    assert "73.00 UGX at r=0.50" in loss_text
    assert "49.60 UGX at r=0.50" in ev_text
    assert result.minutes_used == 21
    assert result.exposure_at_risk == Money(18_000, cfg.currency)
    assert result.friction_cost == Money(240, cfg.currency)
    assert result.wrong_touch_expected == pytest.approx(0.2 + 0.1 + 0.3)


# --- measurements against the shipped economics --------------------------


def synthetic_rows(cfg: Economics, accounts: int, seed: int) -> tuple[AccountEV, ...]:
    """A queue at the scale the gate names, with a realistic exposure/probability mix.

    Log-normal exposures (median about 400,000 UGX, long tail) and a beta-shaped
    probability distribution, drawn from the configured seed. The point is not to
    imitate a corpus - P1b and P4 own that - it is to give both solvers an input large
    enough that a 200 ms budget and a multi-second deadline mean something, with
    enough sign mixing that the capacity constraint actually binds.
    """
    rng = np.random.default_rng(seed)
    classes = list(cfg.alert_classes)
    rows: list[AccountEV] = []
    for index in range(accounts):
        p_calibrated = float(np.clip(rng.beta(1.4, 6.0), 0.01, 0.99))
        exposure_minor = max(int(rng.lognormal(mean=17.5, sigma=1.15)), 1)
        rows.append(
            priced(
                cfg,
                f"ACC-{index:05d}",
                round(p_calibrated, 4),
                exposure_minor,
                classes[int(rng.integers(0, len(classes)))],
            )
        )
    return tuple(rows)


def _timed_microseconds(call: Callable[[], object]) -> int:
    """Elapsed microseconds of ``call``, from ``perf_counter`` and nowhere else.

    Microseconds because the cached slider path answers well inside a millisecond and
    a millisecond timer would report it as zero. A duration, never a timestamp:
    nothing measured here reaches an artefact (01 §A rule 4).
    """
    started = time.perf_counter()
    call()
    return round((time.perf_counter() - started) * 1_000_000)


def test_greedy_latency_within_config_budget() -> None:
    """Plan §11 performance gate: the greedy answer must arrive inside 200 ms.

    Measured on the *slider* path - a cached queue, allocation re-run at a new
    capacity - because that is the interaction the budget is written for. The
    precompute pass is timed separately so the two are not confused, and the cold
    path is timed too, which is the pessimistic case.
    """
    rows = synthetic_rows(SHIPPED, 3_000, SHIPPED.seed)
    priced_us = _timed_microseconds(lambda: synthetic_rows(SHIPPED, 3_000, SHIPPED.seed))
    queue = CachedQueue.build(rows, SHIPPED)
    runs = sorted(
        _timed_microseconds(lambda: queue.allocate(SHIPPED.capacity.review_minutes_per_period))
        for _ in range(20)
    )
    fastest, median_us, slowest = runs[0], runs[len(runs) // 2], runs[-1]
    budget = SHIPPED.solver.greedy_budget_ms
    cold = allocate_timed(rows, SHIPPED.capacity.review_minutes_per_period, SHIPPED)
    print(
        f"greedy over {len(rows)} priced alerts at capacity "
        f"{SHIPPED.capacity.review_minutes_per_period} min, cached queue over 20 runs: "
        f"fastest {fastest / 1000:.3f} ms, median {median_us / 1000:.3f} ms, slowest "
        f"{slowest / 1000:.3f} ms against a {budget} ms budget. Cold path: "
        f"{priced_us / 1000:.1f} ms to draw and price 3,000 alerts, "
        f"{cold.latency_ms} ms end to end "
        f"for {cold.allocation.accounts_reviewed} selected accounts; budget respected: "
        f"{cold.within_greedy_budget}"
    )
    assert median_us < budget * 1000
    assert slowest < budget * 1000
    assert cold.within_greedy_budget


def test_realistic_input_reports_the_greedy_vs_exact_gap() -> None:
    """The comparison itself at scale, with the shipped assumptions.

    Asserted: the exact solve finishes inside the deadline it was given, never returns
    less value than the heuristic, and lands within the configured agreement
    tolerance. Printed: the size of the gap and both latencies, because those are
    measurements rather than fixtures - and one hand-tuned case is not the general
    result, which is why the 2 % clause is checked at this scale as well as on the
    fixture.
    """
    rows = synthetic_rows(SHIPPED, 3_000, SHIPPED.seed)
    capacity = SHIPPED.capacity.review_minutes_per_period
    greedy_run = allocate_timed(rows, capacity, SHIPPED)
    exact_run = allocate_timed(
        rows, capacity, SHIPPED, allocator=AllocatorId.CP_SAT, deadline_ms=30_000
    )
    assert exact_run.allocation.allocator is AllocatorId.CP_SAT, exact_run.allocation.message
    comparison = SolverComparison(greedy=greedy_run.allocation, exact=exact_run.allocation)
    print(
        f"{len(rows)} alerts at capacity {capacity} min: greedy selected "
        f"{greedy_run.allocation.accounts_reviewed} accounts for "
        f"{greedy_run.allocation.total_ev.minor:,} minor units in "
        f"{greedy_run.latency_ms} ms; CP-SAT selected "
        f"{exact_run.allocation.accounts_reviewed} for "
        f"{exact_run.allocation.total_ev.minor:,} in {exact_run.latency_ms} ms; gap "
        f"{comparison.gap.gap.minor:,} minor units = "
        f"{comparison.gap.gap_ratio:.6%} of the exact total; within tolerance "
        f"{comparison.gap.within_tolerance}"
    )
    assert comparison.gap.gap.minor >= 0
    assert comparison.gap.within_tolerance, comparison.sentence()
    assert greedy_run.allocation.accounts_reviewed > 0
    assert exact_run.latency_ms < 30_000
    assert comparison.gap.exact_proven
    assert "at r=" in comparison.sentence()


def test_gap_ratio_precision_does_not_round_a_real_gap_to_zero() -> None:
    """A tolerance check that quantises the ratio to zero would always pass.

    On the 3,000-alert queue the measured gap is a few ten-thousandths of a percent.
    At four decimal places of fixed point that rounds to 0.0 and the gate would
    report perfect agreement, so the ratio is carried at twelve.
    """
    rows = synthetic_rows(SHIPPED, 3_000, SHIPPED.seed)
    comparison = compare_solvers(
        rows, SHIPPED.capacity.review_minutes_per_period, SHIPPED, deadline_ms=30_000
    )
    exact_ratio = comparison.gap.gap.minor / comparison.gap.exact.minor
    print(f"gap ratio at full precision: {comparison.gap.gap_ratio!r} against {exact_ratio!r}")
    assert comparison.gap.gap_ratio == pytest.approx(exact_ratio, abs=1e-9)
    if comparison.gap.gap.minor:
        assert comparison.gap.gap_ratio > 0.0, "a non-zero gap must not quantise to zero"


def test_the_shipped_economics_defines_the_numbers_being_measured() -> None:
    """The measurements above are judged against config, not against this test."""
    assert SHIPPED.capacity.review_minutes_per_period == 12_000
    assert SHIPPED.solver.greedy_budget_ms == 200
    assert SHIPPED.solver.cpsat_workers == 1
    assert SHIPPED.solver.agreement_tolerance_ratio == 0.02
    assert SHIPPED.seed == 1337
    assert SHIPPED.recovery.band == (0.2, 0.35, 0.5)
