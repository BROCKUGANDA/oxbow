"""Fairness and robustness checks the harness runs *even though* no protected attribute exists.

Plan §12 (spec §7.7) mandates the sequence verbatim: neither PaySim nor IBM-AML carries
a protected attribute, so the first honest statement is that fact — and then the check
runs anyway, against the axes where over-flagging is *realistic* in this domain: small
-value accounts and newly-active accounts, which the plan names as the population a
naive model wrongly escalates. A clean protected-attribute table would be theatre; a
false-positive rate by amount decile and account age is the finding that matters.

The two perturbation tests are the second half of the phase: a ±10 % shift of every
amount must not reorder the review queue (Spearman near 1), and losing a tenth of the
network's edges must not move per-typology recall far. Where a perturbation genuinely
needs the graph or the model to be recomputed — the true "drop 10 % of *edges* and
re-fit" — this module computes the recall-stability proxy the harness can measure from
the fold it already scored and states plainly that the full edge-drop re-fit is a P3/P4
dependency, rather than presenting a proxy as if it were the real thing (00 §B: a
mocked metric is never a result).

MONEY (DEV-005): the amount-shift rescale uses ``money_scaled`` (integer, round-half-up),
so a ±10 % perturbation is an integer minor-unit transformation and the rank it produces
is exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from oxbow.backtest.economics import CURRENCY_DEFAULT, FoldAccount
from oxbow.backtest.metrics import money_scaled, spearman_rank_correlation

PROTECTED_ATTRIBUTES_NOTE: Final = (
    "Neither corpus carries a protected attribute (payee/payer identifiers are salted "
    "hashes and no gender, age-band, ethnicity, nationality or account-holder-type field "
    "is present in PaySim or IBM-AML), so a demographic fairness table cannot be computed "
    "honestly. The check is run anyway against the realistic over-flagging proxies in this "
    "domain: transaction amount, account age, activity volume and community size."
)

FAIRNESS_AXES: Final = (
    "amount_minor",
    "account_age_days",
    "activity_volume",
    "community_size",
)


@dataclass(frozen=True, slots=True)
class BucketRate:
    """False-positive rate for one bucket of one proxy axis."""

    axis: str
    bucket: str
    n_accounts: int
    n_clean: int
    n_false_positives: int
    false_positive_rate: float


@dataclass(frozen=True, slots=True)
class AxisFairness:
    """One fairness axis: its buckets, or an explicit not-available marker.

    ``available`` False with a reason is a real result, not a gap: if the corpus does not
    carry the axis, the honest output says so rather than emitting an empty table the page
    would render as "no disparity found".
    """

    axis: str
    available: bool
    reason: str = ""
    buckets: tuple[BucketRate, ...] = ()


@dataclass(frozen=True, slots=True)
class FairnessReport:
    """The whole fairness block: the protected-attribute statement and every proxy axis."""

    protected_attributes_note: str = PROTECTED_ATTRIBUTES_NOTE
    axes: tuple[AxisFairness, ...] = ()


def _values_for(accounts: list[FoldAccount], axis: str) -> list[int | None]:
    return [getattr(account, axis, None) for account in accounts]


def false_positive_rate_by_axis(
    accounts: list[FoldAccount],
    reviewed: set[str],
    axis: str,
    *,
    n_buckets: int = 4,
) -> AxisFairness:
    """Split accounts into quantile buckets on one proxy axis and report FP rate each.

    A false positive is a *clean* account the policy reviewed. Rate is per bucket over
    the clean accounts in that bucket, which is the disparity that matters operationally:
    reviewing 5 % of whale accounts and 40 % of micro-accounts at the same score is a bias
    a single global FP rate hides.
    """
    values = _values_for(accounts, axis)
    present = [v for v in values if v is not None]
    if len(present) < n_buckets:
        return AxisFairness(
            axis=axis,
            available=False,
            reason=f"axis {axis!r} has fewer than {n_buckets} non-null values in this corpus",
        )
    array = np.asarray([float(v) for v in values], dtype=np.float64)
    finite = array[~np.isnan(array)]
    cut_points = np.quantile(finite, np.linspace(0.0, 1.0, n_buckets + 1)[1:-1])
    edges = np.concatenate(([float("-inf")], cut_points.astype(np.float64), [float("inf")]))
    buckets: list[BucketRate] = []
    for index in range(len(edges) - 1):
        low, high = edges[index], edges[index + 1]
        member_positions = [
            i for i, value in enumerate(array) if not math.isnan(value) and low < value <= high
        ]
        clean = [i for i in member_positions if accounts[i].label == 0]
        false_positives = [i for i in clean if accounts[i].account_key in reviewed]
        if not member_positions:
            continue
        rate = (len(false_positives) / len(clean)) if clean else 0.0
        buckets.append(
            BucketRate(
                axis=axis,
                bucket=f"({low:g}, {high:g}]",
                n_accounts=len(member_positions),
                n_clean=len(clean),
                n_false_positives=len(false_positives),
                false_positive_rate=rate,
            )
        )
    return AxisFairness(axis=axis, available=True, buckets=tuple(buckets))


def fairness_report(accounts: list[FoldAccount], reviewed: set[str]) -> FairnessReport:
    """Run every proxy axis and assemble the fairness block."""
    axes = tuple(false_positive_rate_by_axis(accounts, reviewed, axis) for axis in FAIRNESS_AXES)
    return FairnessReport(axes=axes)


@dataclass(frozen=True, slots=True)
class AmountShiftResult:
    """Spearman rank correlation of the review score under a ±10 % money shift."""

    shift_ratio: float
    spearman: float
    reordered_at_cutoff: bool


def _density_rank_score(
    account: FoldAccount,
    *,
    recovery_rate: float,
    friction_cost_minor: int,
    scale_factor: float,
    currency: str,
) -> float:
    """EV density under a multiplicative shift applied to amount *and* exposure.

    The shift scales the money terms only (``amount_minor`` and ``exposure_minor``);
    probability, review cost and minutes are untouched, which is the right semantics for
    "shift all amounts ±10 %": a bigger account with the same suspiciousness is worth
    more to review, and a robust policy should rank it consistently. The result is a
    *ranking key* (a per-minute rate), a float by nature, not a money sum — so it is
    named for what it is and never rendered as an amount.
    """
    scaled_exposure = money_scaled(account.exposure_minor, scale_factor, currency)
    intercept = money_scaled(scaled_exposure, account.p_calibrated * recovery_rate, currency)
    friction = money_scaled(friction_cost_minor, 1.0 - account.p_calibrated, currency)
    ev_minor = intercept - account.review_cost_minor - friction
    return ev_minor / account.review_minutes


def amount_shift_rank_correlation(
    accounts: list[FoldAccount],
    *,
    recovery_rate: float,
    friction_cost_minor: int,
    shift_ratio: float,
    cutoff_rank: int,
    currency: str = CURRENCY_DEFAULT,
) -> AmountShiftResult:
    """Correlate the EV-density ranking nominal vs a ±10 % amount shift (plan §12).

    ``cutoff_rank`` marks the capacity line: the flag ``reordered_at_cutoff`` is True when
    an account crosses it under the shift, because a policy whose *decision* is stable is
    worth more than one whose full ranking is stable but whose head swaps in and out of
    the reviewable set.
    """
    if len(accounts) < 2:
        return AmountShiftResult(shift_ratio, math.nan, False)
    nominal = [
        _density_rank_score(
            a,
            recovery_rate=recovery_rate,
            friction_cost_minor=friction_cost_minor,
            scale_factor=1.0,
            currency=currency,
        )
        for a in accounts
    ]
    shifted = [
        _density_rank_score(
            a,
            recovery_rate=recovery_rate,
            friction_cost_minor=friction_cost_minor,
            scale_factor=1.0 + shift_ratio,
            currency=currency,
        )
        for a in accounts
    ]
    rho = spearman_rank_correlation(nominal, shifted)
    nominal_order = sorted(
        range(len(accounts)), key=lambda i: (-nominal[i], accounts[i].account_key)
    )
    shifted_order = sorted(
        range(len(accounts)), key=lambda i: (-shifted[i], accounts[i].account_key)
    )
    top_nominal = set(nominal_order[:cutoff_rank])
    top_shifted = set(shifted_order[:cutoff_rank])
    return AmountShiftResult(
        shift_ratio=shift_ratio,
        spearman=rho,
        reordered_at_cutoff=top_nominal != top_shifted,
    )


@dataclass(frozen=True, slots=True)
class TypologyRecallShift:
    """Nominal vs edge-dropped per-typology recall, and the gap."""

    typology: str
    nominal_recall: float
    dropped_recall: float
    abs_shift: float


@dataclass(frozen=True, slots=True)
class EdgeDropResult:
    """Typology-recall stability under a seeded 10 % row drop (plan §12)."""

    drop_ratio: float
    seed: int
    per_typology: tuple[TypologyRecallShift, ...] = field(default_factory=tuple)
    max_abs_shift: float = 0.0
    caveat: str = (
        "recall recomputed on a seeded 90 % subsample of the fold's labelled test rows; "
        "the full drop-10%-of-EDGES-and-recompute-graph-features variant needs the P3 "
        "graph layer and is a named dependency, not this proxy"
    )


def edge_drop_typology_recall_stability(
    accounts: list[FoldAccount],
    reviewed: set[str],
    *,
    drop_ratio: float,
    seed: int,
) -> EdgeDropResult:
    """Report how per-typology recall moves when a tenth of the fold's rows are dropped.

    Uses the labelled typology carried on the accounts (the IBM pattern join, DEV-014).
    ``RANDOM`` is included and is the negative control: if a typology-specific drop moves
    RANDOM recall as much as CYCLE recall, the detector is not typology-aware.
    """
    labelled = [a for a in accounts if a.typology is not None and a.label == 1]
    if not labelled:
        return EdgeDropResult(drop_ratio=drop_ratio, seed=seed, per_typology=(), max_abs_shift=0.0)
    nominal = _recall_by_typology(labelled, reviewed)
    rng = np.random.default_rng(seed)
    keep_count = int(round(len(labelled) * (1.0 - drop_ratio)))
    keep_count = max(keep_count, 1)
    keep_positions = sorted(rng.choice(len(labelled), size=keep_count, replace=False).tolist())
    kept = [labelled[i] for i in keep_positions]
    dropped = _recall_by_typology(kept, reviewed)
    shifts: list[TypologyRecallShift] = []
    for typology in sorted(nominal):
        base = nominal[typology]
        after = dropped.get(typology, 0.0)
        shifts.append(
            TypologyRecallShift(
                typology=typology,
                nominal_recall=base,
                dropped_recall=after,
                abs_shift=abs(after - base),
            )
        )
    max_shift = max((s.abs_shift for s in shifts), default=0.0)
    return EdgeDropResult(
        drop_ratio=drop_ratio, seed=seed, per_typology=tuple(shifts), max_abs_shift=max_shift
    )


def _recall_by_typology(labelled: list[FoldAccount], reviewed: set[str]) -> dict[str, float]:
    totals: dict[str, int] = {}
    caught: dict[str, int] = {}
    for account in labelled:
        assert account.typology is not None
        totals[account.typology] = totals.get(account.typology, 0) + 1
        if account.account_key in reviewed:
            caught[account.typology] = caught.get(account.typology, 0) + 1
    return {t: caught.get(t, 0) / totals[t] for t in sorted(totals)}


__all__ = [
    "FAIRNESS_AXES",
    "PROTECTED_ATTRIBUTES_NOTE",
    "AmountShiftResult",
    "AxisFairness",
    "BucketRate",
    "EdgeDropResult",
    "FairnessReport",
    "TypologyRecallShift",
    "amount_shift_rank_correlation",
    "edge_drop_typology_recall_stability",
    "fairness_report",
    "false_positive_rate_by_axis",
]
