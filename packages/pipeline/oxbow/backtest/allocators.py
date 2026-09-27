"""A real ``Allocator``: P5's greedy and exact CP-SAT, behind the harness's seam.

``oxbow.backtest.fakes.GreedyAllocator`` proved the harness *consumes* an allocator; this is
the one that *is* P5's. It wraps :func:`oxbow.quant.allocate.allocate_greedy` and
:func:`oxbow.quant.allocate.allocate_cpsat` — the density ordering, the integer knapsack and
the documented degradation labels all stay in ``oxbow.quant``, which owns them (import-linter
keeps ``quant`` free of the harness; this module reaches ``quant`` from the harness side,
which is the legal direction).

WHY THE ITEM CARRIES ITS OWN MONEY TERMS. The ``AllocationItem`` the harness hands over
already holds ``exposure_minor``, ``review_cost_minor`` and ``minutes_i`` — the primitives of
one alert as the fold's scorer and the corpus's economics measured them. ``quant``'s own
``price_account`` would re-derive ``c_i`` and ``m_i`` from a global alert class and an
analyst rate, which is right for a live triage queue but wrong *here*: the harness prices the
threshold baseline with the account's own ``review_cost_minor`` (``set_expected_ev_minor``),
so an EV policy that re-derived the cost from config would compare two policies against
different money and the threshold-vs-EV row — the v2 thesis — would be arithmetic on a
mismatch. This adapter therefore builds each :class:`~oxbow.quant.ev.AccountEV` with the
item's own primitives and hands ``allocate_greedy`` / ``allocate_cpsat`` a queue to order and
pack, not a queue to re-price.

THE <=2 % RULE, PRESERVED NOT REIMPLEMENTED. The agreement tolerance lives in
``config/economics.yaml`` (``solver.agreement_tolerance_ratio``) and is enforced by
:class:`oxbow.quant.allocate.SolverComparison`, which ``quant`` owns. The exact arm runs the
greedy solve too, prices the gap through that comparison, and folds the resulting
"within/outside the configured tolerance" sentence into ``allocator_label`` — so a degraded
or out-of-tolerance result can never be read as a clean optimum, which is plan §11's rule.
The greedy arm carries no gap sentence: a single policy is not a comparison, and inventing a
gap it did not measure is the fabrication the whole label discipline exists to stop.

MONEY (DEV-005): every amount here is an integer count of minor units wrapped in
:class:`~oxbow.quant.money.Money`; probabilities and rates go through
``ratio_to_micro``/``scaled_by_micro`` so the EV terms ``quant`` sums are exact integer
arithmetic, and ``total_ev_minor`` is an ``int``. No float touches an amount.
"""

from __future__ import annotations

from collections.abc import Sequence

from oxbow.backtest.interfaces import AllocationItem, AllocationResult
from oxbow.quant.allocate import (
    AllocatorId,
    SolverComparison,
    allocate_cpsat,
    allocate_greedy,
)
from oxbow.quant.economics import Economics
from oxbow.quant.ev import AccountEV, CalibratedScore
from oxbow.quant.money import MICRO, Money, QuantError, ratio_to_micro, scale_div

#: A placeholder alert class for the harness's per-item queue. ``quant`` never reads it on
#: this path (the item already carries its minutes and cost), but ``CalibratedScore`` names
#: a class, and inventing an A-E band here would imply a triage effort ``quant`` was not
#: asked to price.
_BACKTEST_ALERT_CLASS = "backtest"


class P5Allocator:
    """Satisfies ``oxbow.backtest.interfaces.Allocator`` using ``oxbow.quant``'s solvers.

    Constructed with the validated :class:`~oxbow.quant.economics.Economics` (the same
    recovery rate, friction cost, currency and solver tolerance the harness reads its
    threshold baseline against), so an EV policy and a threshold policy price against one
    assumption set rather than two.
    """

    def __init__(self, cfg: Economics) -> None:
        self._cfg = cfg

    def allocate(
        self,
        *,
        items: Sequence[AllocationItem],
        capacity_minutes: int,
        policy: str,
        seed: int,
    ) -> AllocationResult:
        del seed  # the queue is deterministic by EV density and account key, not by RNG draw
        rows = [self._as_priced_row(item) for item in items]
        if policy == "cpsat":
            exact = allocate_cpsat(rows, capacity_minutes, self._cfg)
            return AllocationResult(
                reviewed=exact.selected_keys,
                total_ev_minor=exact.total_ev.minor,
                minutes_used=exact.minutes_used,
                allocator_label=exact.allocator_label
                + self._gap_sentence(rows, capacity_minutes, exact),
            )
        if policy == "greedy":
            greedy = allocate_greedy(rows, capacity_minutes, self._cfg)
            return AllocationResult(
                reviewed=greedy.selected_keys,
                total_ev_minor=greedy.total_ev.minor,
                minutes_used=greedy.minutes_used,
                allocator_label=greedy.allocator_label,
            )
        raise QuantError(
            f"unknown allocation policy {policy!r}; the harness may request only 'greedy' or "
            "'cpsat' through this seam (oxbow.backtest.policies maps every other name)."
        )

    # -- pricing ------------------------------------------------------------

    def _as_priced_row(self, item: AllocationItem) -> AccountEV:
        """Turn one ``AllocationItem`` into an ``AccountEV`` carrying the item's own terms.

        The EV formula is ``quant``'s (``p*E*r - c - (1-p)*f``) with the money steps taken
        through integer micro-ratios; ``c`` and ``m`` come from the item, not re-derived from
        config, for the reason in the module docstring.
        """
        currency = self._cfg.currency
        exposure = Money(item.exposure_minor, currency)
        review_cost = Money(item.review_cost_minor, currency)
        p_micro = ratio_to_micro(item.p_calibrated)
        r_micro = ratio_to_micro(self._cfg.recovery.rate)
        joint_micro = scale_div(p_micro * r_micro, MICRO)
        intercept = exposure.scaled_by_micro(joint_micro)
        friction = self._cfg.friction_cost.scaled_by_micro(MICRO - p_micro)
        ev = intercept - review_cost - friction
        score = CalibratedScore(
            account_key=item.account_key,
            p_calibrated=item.p_calibrated,
            alert_class=_BACKTEST_ALERT_CLASS,
            band_observed_rate=0.0,
            band_n=0,
        )
        return AccountEV(
            score=score,
            exposure=exposure,
            review_minutes=item.minutes_i,
            review_cost=review_cost,
            expected_friction_cost=friction,
            expected_intercept=intercept,
            ev=ev,
            density_ratio=ev.minor / item.minutes_i,
        )

    def _gap_sentence(self, rows: Sequence[AccountEV], capacity_minutes: int, exact: object) -> str:
        """The greedy-vs-exact agreement sentence, or '' when no gap can be honestly stated.

        A degraded CP-SAT result (deadline, solver fault) has no proven optimum, therefore no
        gap; ``compare_solvers`` refuses to price one, and this adapter does the same by
        declining to add a sentence. A clean optimum gets the configured tolerance named with
        the measured gap beside it.
        """
        from oxbow.quant.allocate import Allocation

        if not isinstance(exact, Allocation) or exact.allocator is not AllocatorId.CP_SAT:
            return ""
        greedy = allocate_greedy(rows, capacity_minutes, self._cfg)
        if not greedy.selected or not exact.selected:
            # With an empty side the comparison is trivially zero and says nothing about the
            # policy; ``quant``'s own sentence still renders, but a label claiming agreement
            # on a null queue would be misleading rather than informative.
            return ""
        try:
            gap = SolverComparison(greedy=greedy, exact=exact).gap
        except QuantError:
            return ""
        verdict = "within" if gap.within_tolerance else "outside"
        return (
            f"; greedy sits {gap.gap_ratio:.2%} of the exact total EV below the CP-SAT "
            f"optimum ({gap.gap}), {verdict} the configured "
            f"{self._cfg.solver.agreement_tolerance_ratio:.0%} agreement tolerance"
        )


__all__ = ["P5Allocator"]
