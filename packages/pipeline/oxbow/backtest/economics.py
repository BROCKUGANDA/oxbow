"""Realized backtest economics: what a policy was actually worth on a fold's labels.

WHY REALIZED, NOT EXPECTED: plan §12 backtests a *policy*, and a backtest is judged
on what happened, not on the model's hope. The EV the allocator ranks by is expected
value under ``p_i``; the number this module reports is the net benefit that would have
been booked had the fold's true labels been known — recovered exposure on real
laundering, minus the review cost always spent, minus the friction cost of every
legitimate account wrongly touched. Both figures are reported: expected for the
allocator's own arithmetic (through ``oxbow.quant``), realized for the ledger.

MONEY (DEV-005): every amount here is integer minor units. The recovery-rate step
goes through ``metrics.money_scaled`` (``Money.scaled_by_micro``), so ``E_i * r`` is
exact integer arithmetic and the cumulative curve reconciles to the sum of its rows
(03 money rule: totals from unrounded minor units, round only at render). Nothing
here multiplies an amount by a Python float.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from oxbow.backtest.metrics import (
    TailRisk,
    alerts_per_10k_accounts,
    benefit_per_analyst_hour_minor,
    max_drawdown_minor,
    money_scaled,
    monte_carlo_tail_risk,
)

CURRENCY_DEFAULT: Final = "UGX"


@dataclass(frozen=True, slots=True)
class FoldAccount:
    """One account's backtest row: its truth, its money terms, its model score.

    ``label`` is the historical outcome (0/1); ``p_calibrated`` is the model's
    estimate for that account in this fold. The realized economics read ``label``; the
    ranking reads ``p_calibrated``. Keeping both visible is what lets the harness say
    a policy was *calibrated* (its p predicted its outcome) as well as *profitable*.
    """

    account_key: str
    label: int
    exposure_minor: int
    review_cost_minor: int
    review_minutes: int
    amount_minor: int
    p_calibrated: float
    typology: str | None = None
    account_age_days: int | None = None
    activity_volume: int | None = None
    community_size: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "exposure_minor",
            "review_cost_minor",
            "review_minutes",
            "amount_minor",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name}={value!r} for {self.account_key} is not an integer (DEV-005)")
        for name in ("account_age_days", "activity_volume", "community_size"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"{name}={value!r} for {self.account_key} must be an integer or None")
        if self.label not in (0, 1):
            raise ValueError(f"label={self.label} for {self.account_key} must be 0 or 1")
        if not 0.0 <= self.p_calibrated <= 1.0:
            raise ValueError(f"p_calibrated={self.p_calibrated} for {self.account_key}")


@dataclass(frozen=True, slots=True)
class FoldEconomics:
    """A policy's realized money on one fold, plus its residual tail risk.

    Amounts are integers; ratios are floats named for being ratios. ``net_benefit_minor``
    is what the cumulative curve integrates, and ``max_drawdown_minor`` is reported at
    the whole-run level (it is a property of a *sequence* of folds, not one fold), so it
    is intentionally absent from this per-fold record.
    """

    reviewed: tuple[str, ...]
    accounts_reviewed: int
    minutes_used: int
    captured_value_minor: int
    review_cost_minor: int
    friction_cost_minor: int
    cost_minor: int
    net_benefit_minor: int
    benefit_per_analyst_hour_minor: int
    true_positives: int
    false_positives: int
    expected_value_minor: int
    tail: TailRisk
    currency: str


def realized_fold_economics(
    accounts: list[FoldAccount],
    reviewed: tuple[str, ...],
    *,
    recovery_rate: float,
    friction_cost_minor: int,
    capacity_minutes: int,
    currency: str = CURRENCY_DEFAULT,
    mc_draws: int,
    mc_seed: int,
    var_alpha: float,
    es_alpha: float,
    expected_value_minor: int = 0,
) -> FoldEconomics:
    """Book one fold's outcomes under one policy, in integer minor units.

    A wrong touch costs friction only when the account is genuinely clean; a review
    spends its cost whether or not it was right; recovery lands only on a true
    positive. Those three are the whole ledger, and stating them explicitly is the
    point — a policy that "captured value" by reviewing everything would book a
    friction and review cost against every legitimate account and be shown net-negative,
    which is exactly the honest comparison the ablation's threshold-vs-EV row makes.
    """
    by_key = {account.account_key: account for account in accounts}
    reviewed_accounts = [by_key[key] for key in reviewed if key in by_key]

    captured_value = 0
    review_cost = 0
    friction = 0
    true_positives = 0
    false_positives = 0
    for account in reviewed_accounts:
        if account.label == 1:
            true_positives += 1
            captured_value += money_scaled(account.exposure_minor, recovery_rate, currency)
        else:
            false_positives += 1
            friction += friction_cost_minor
        review_cost += account.review_cost_minor

    minutes_used = sum(account.review_minutes for account in reviewed_accounts)
    if minutes_used > capacity_minutes:
        raise ValueError(
            f"policy booked {minutes_used} minutes over a {capacity_minutes}-minute capacity; "
            "the allocator must return a set that fits."
        )
    cost = review_cost + friction
    net_benefit = captured_value - cost

    reviewed_set = {account.account_key for account in reviewed_accounts}
    residual = [account for account in accounts if account.account_key not in reviewed_set]
    residual_loss = [money_scaled(account.exposure_minor, recovery_rate, currency) for account in residual]
    residual_prob = [account.p_calibrated for account in residual]
    tail = monte_carlo_tail_risk(
        residual_loss,
        residual_prob,
        alpha_var=var_alpha,
        alpha_es=es_alpha,
        draws=mc_draws,
        seed=mc_seed,
    )

    per_hour = (
        benefit_per_analyst_hour_minor(net_benefit, minutes_used)
        if minutes_used > 0
        else 0
    )
    return FoldEconomics(
        reviewed=tuple(account.account_key for account in reviewed_accounts),
        accounts_reviewed=len(reviewed_accounts),
        minutes_used=minutes_used,
        captured_value_minor=captured_value,
        review_cost_minor=review_cost,
        friction_cost_minor=friction,
        cost_minor=cost,
        net_benefit_minor=net_benefit,
        benefit_per_analyst_hour_minor=per_hour,
        true_positives=true_positives,
        false_positives=false_positives,
        expected_value_minor=expected_value_minor,
        tail=tail,
        currency=currency,
    )


def run_drawdown(net_benefit_per_fold: list[int]) -> int:
    """Max drawdown of the cumulative net-benefit curve across folds (integer minor)."""
    from oxbow.backtest.metrics import cumulative_benefit

    return max_drawdown_minor(cumulative_benefit(net_benefit_per_fold))


def fold_alerts_per_10k(alert_count: int, screened_accounts: int) -> float:
    """Alert-fatigue proxy for one fold (plan §12), a float count per 10k accounts."""
    return alerts_per_10k_accounts(alert_count, screened_accounts)


__all__ = [
    "CURRENCY_DEFAULT",
    "FoldAccount",
    "FoldEconomics",
    "fold_alerts_per_10k",
    "realized_fold_economics",
    "run_drawdown",
]
