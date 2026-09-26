"""Review policies: which accounts a given strategy puts under the capacity line.

WHY POLICIES LIVE HERE AND NOT IN THE HARNESS: plan §12 requires four baselines
"backtested identically" (random, score-threshold, rules-only, highest-amount-first)
plus the two EV policies (greedy and exact CP-SAT, which come from P5 through the
injected :class:`~oxbow.backtest.interfaces.Allocator`). Treating them all as one
selection function over one ranked ordering is what makes the comparison fair — the
threshold-vs-EV row that carries the v2 thesis is only meaningful because both
policies face the *same* capacity and the *same* fold.

HIGHEST-AMOUNT-FIRST IS NOT A STRAWMAN: plan §11/§12 keep it because it is the policy
real monitoring teams actually run (review the biggest exposure first). Beating it on
net benefit under the same capacity is a substantive finding, so it is implemented
honestly as "rank by exposure", not crippled.

MONEY (DEV-005): ranking keys are integers (exposure, amount) or floats (probability,
severity); the *expected* value the harness reports for each set is recomputed in
integer minor units via ``money_scaled``, never by multiplying an amount by a float.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

from oxbow.backtest.economics import CURRENCY_DEFAULT, FoldAccount
from oxbow.backtest.interfaces import AllocationItem, Allocator, BacktestError
from oxbow.backtest.metrics import money_scaled

POLICY_THRESHOLD: Final = "score_threshold"
POLICY_RULES_ONLY: Final = "rules_only"
POLICY_HIGHEST_AMOUNT: Final = "highest_amount_first"
POLICY_RANDOM: Final = "random"
POLICY_EV_GREEDY: Final = "ev_greedy"
POLICY_EV_CPSAT: Final = "ev_cpsat"

HARNESS_POLICIES: Final = (POLICY_THRESHOLD, POLICY_RULES_ONLY, POLICY_HIGHEST_AMOUNT, POLICY_RANDOM)
ALLOCATOR_POLICIES: Final = (POLICY_EV_GREEDY, POLICY_EV_CPSAT)
ALL_POLICIES: Final = HARNESS_POLICIES + ALLOCATOR_POLICIES


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    """The accounts one policy chose, and the expected value it saw in them."""

    policy: str
    reviewed: tuple[str, ...]
    expected_ev_minor: int
    allocator_label: str


def _rank_fill(
    ordered: list[FoldAccount], minutes_lookup_capacity: int
) -> list[FoldAccount]:
    """Take accounts in rank order while each still fits the remaining capacity.

    Greedy fill (not first-fit-decreasing): a large account at the head of the ranking
    must not block every later small one, so the scan continues and admits any account
    whose minutes fit what remains. Determinism: ``ordered`` is already tie-broken by
    account key before it reaches here.
    """
    remaining = minutes_lookup_capacity
    chosen: list[FoldAccount] = []
    for account in ordered:
        if account.review_minutes <= remaining:
            chosen.append(account)
            remaining -= account.review_minutes
    return chosen


def set_expected_ev_minor(
    accounts: list[FoldAccount],
    *,
    recovery_rate: float,
    friction_cost_minor: int,
    currency: str = CURRENCY_DEFAULT,
) -> int:
    """``sum_i [p_i E_i r - c_i - (1-p_i) f]`` for an arbitrary set, in minor units.

    Needed so a non-EV baseline (threshold, highest-amount) still carries a comparable
    expected value; the EV formula is plan §11's, applied here with integer scaling so
    no float touches money.
    """
    total = 0
    for account in accounts:
        intercept = money_scaled(account.exposure_minor, account.p_calibrated * recovery_rate, currency)
        friction = money_scaled(friction_cost_minor, 1.0 - account.p_calibrated, currency)
        total += intercept - account.review_cost_minor - friction
    return total


def _ranked_by(accounts: list[FoldAccount]) -> list[FoldAccount]:
    return sorted(accounts, key=lambda a: (a.p_calibrated, a.account_key), reverse=True)


def _ranked_rules(accounts: list[FoldAccount], severity: dict[str, float]) -> list[FoldAccount]:
    return sorted(accounts, key=lambda a: (severity.get(a.account_key, 0.0), a.account_key), reverse=True)


def _ranked_amount(accounts: list[FoldAccount]) -> list[FoldAccount]:
    return sorted(accounts, key=lambda a: (a.exposure_minor, a.account_key), reverse=True)


def _ranked_random(accounts: list[FoldAccount], seed: int) -> list[FoldAccount]:
    order = sorted(
        range(len(accounts)),
        key=lambda i: (float(np.random.default_rng(seed + _hash_key(accounts[i].account_key)).random()), accounts[i].account_key),
    )
    return [accounts[i] for i in order]


def _hash_key(key: str) -> int:
    """A stable per-account integer salt so the random baseline is order-independent."""
    acc = 5381
    for byte in key.encode("utf-8"):
        acc = ((acc * 33) + byte) & 0xFFFFFFFF
    return acc


def run_policy(
    policy: str,
    accounts: list[FoldAccount],
    *,
    severity_by_account: dict[str, float],
    capacity_minutes: int,
    allocator: Allocator | None,
    seed: int,
    recovery_rate: float,
    friction_cost_minor: int,
    currency: str = CURRENCY_DEFAULT,
) -> PolicyOutcome:
    """Select the review set for one policy over one fold's scored accounts."""
    if policy in ALLOCATOR_POLICIES:
        if allocator is None:
            raise BacktestError(
                f"policy {policy!r} needs an injected Allocator (P5's greedy/CP-SAT)"
            )
        items = [
            AllocationItem(
                account_key=account.account_key,
                p_calibrated=account.p_calibrated,
                exposure_minor=account.exposure_minor,
                review_cost_minor=account.review_cost_minor,
                minutes_i=account.review_minutes,
            )
            for account in accounts
        ]
        result = allocator.allocate(
            items=items,
            capacity_minutes=capacity_minutes,
            policy="greedy" if policy == POLICY_EV_GREEDY else "cpsat",
            seed=seed,
        )
        return PolicyOutcome(
            policy=policy,
            reviewed=result.reviewed,
            expected_ev_minor=result.total_ev_minor,
            allocator_label=result.allocator_label,
        )

    if policy == POLICY_THRESHOLD:
        chosen = _rank_fill(_ranked_by(accounts), capacity_minutes)
        label = "baseline: score-threshold at the same analyst capacity"
    elif policy == POLICY_RULES_ONLY:
        chosen = _rank_fill(_ranked_rules(accounts, severity_by_account), capacity_minutes)
        label = "baseline: rules-only severity ranking"
    elif policy == POLICY_HIGHEST_AMOUNT:
        chosen = _rank_fill(_ranked_amount(accounts), capacity_minutes)
        label = "baseline: highest-exposure-first (what desks actually run)"
    elif policy == POLICY_RANDOM:
        chosen = _rank_fill(_ranked_random(accounts, seed), capacity_minutes)
        label = "baseline: random order under the configured seed"
    else:
        raise BacktestError(f"unknown policy {policy!r}; expected one of {ALL_POLICIES}")

    return PolicyOutcome(
        policy=policy,
        reviewed=tuple(a.account_key for a in chosen),
        expected_ev_minor=set_expected_ev_minor(
            chosen,
            recovery_rate=recovery_rate,
            friction_cost_minor=friction_cost_minor,
            currency=currency,
        ),
        allocator_label=label,
    )


__all__ = [
    "ALLOCATOR_POLICIES",
    "ALL_POLICIES",
    "HARNESS_POLICIES",
    "POLICY_EV_CPSAT",
    "POLICY_EV_GREEDY",
    "POLICY_HIGHEST_AMOUNT",
    "POLICY_RANDOM",
    "POLICY_RULES_ONLY",
    "POLICY_THRESHOLD",
    "PolicyOutcome",
    "run_policy",
    "set_expected_ev_minor",
]
