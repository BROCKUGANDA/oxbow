"""Hand-computed fakes for the four injected seams: they verify the harness itself.

WHY THEY LIVE IN THE PACKAGE, NOT ONLY IN TESTS: plan §12's gate is "all five folds run
from one command" with a leakage control that outperforms — and that command has to be
runnable *today*, before P2's fold provider, P4's calibrator and P5's allocator have
landed. So the same reference fakes the unit tests assert exact metrics against also
drive `python -m oxbow.backtest.run` to produce the demonstration artifacts. They are the
harness's calibration weights, not results: every number that comes out of a path built
from these fakes carries ``provenance="fake_harness"`` and must never be labelled a
result (00 §B: "a mocked metric presented as a result" is a fabrication).

THE FAKES ARE DELIBERATELY IMPERFECT. The honest scorer's probability is a deterministic
hash of a hidden ``signal`` column, so its PR-AUC lands mid-range rather than at 1.0 — a
fake that scored 0.99 would look like the leakage 00 §18 warns about even when honest. The
*control* scorer is the one that cheats: it reads the test label directly, which is exactly
what ``test_embargo_blocks_leakage`` and the leakage-control arm are built to catch, so its
visible outperformance proves the harness can see lookahead rather than proving the model is
good.

MONEY (DEV-005): every amount the fakes emit is integer minor units.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator, Sequence

import polars as pl

from oxbow.backtest.economics import FoldAccount
from oxbow.backtest.interfaces import (
    COL_ACCOUNT_KEY,
    COL_AMOUNT_MINOR,
    COL_AS_OF_TS,
    COL_EXPOSURE_MINOR,
    COL_LABEL,
    COL_LABEL_TYPOLOGY,
    COL_REVIEW_COST_MINOR,
    COL_REVIEW_MINUTES,
    COL_SPEC_HASH,
    AccountScore,
    AllocationItem,
    AllocationResult,
    HarnessFold,
    RuleHit,
    ScoreResult,
)
from oxbow.backtest.metrics import money_scaled

# A single fixed feature-spec hash for the fake corpora. It is a constant, not a
# registry-derived hash, because the fakes carry no feature registry; the harness only
# checks that the scorer's returned hash equals the corpus hash, which both fakes satisfy.
FAKE_SPEC_HASH: str = hashlib.sha256(b"oxbow-backtest-fake-spec-v1").hexdigest()

# Fixed epoch for the fake timeline: 2020-01-01T00:00:00Z in microseconds. A constant so
# the fake corpus is byte-reproducible and its fold gaps are exact arithmetic.
FAKE_EPOCH_US: int = 1_577_836_800_000_000

# The typology labels mirror DEV-014's real IBM distribution so the per-typology recall
# block is exercised with plausible categories, including the RANDOM negative control.
TYPOLOGY_POOL: tuple[str, ...] = (
    "GATHER-SCATTER",
    "SCATTER-GATHER",
    "STACK",
    "FAN-OUT",
    "FAN-IN",
    "CYCLE",
    "BIPARTITE",
    "RANDOM",
)


def _unit_hash(key: str, salt: str) -> float:
    """A deterministic value in [0, 1) from a string key and salt, order-independent."""
    digest = hashlib.sha256(f"{salt}|{key}".encode()).hexdigest()
    return int(digest[:12], 16) / float(1 << 48)


def make_corpus(
    *,
    n_accounts: int = 500,
    span_days: int = 240,
    positive_share: float = 0.10,
    spec_hash: str = FAKE_SPEC_HASH,
    with_typology: bool = True,
) -> pl.DataFrame:
    """Build a deterministic per-account backtest corpus whose timeline fits the embargo.

    ``span_days`` defaults to 240 because five expanding walk-forward folds with a 30-day
    embargo need a window long enough that the harness can honestly run them — which is
    precisely the thing the real 18-day IBM corpus is NOT, and the reason the multi-fold
    demonstration runs here (labelled fake) while IBM is reported single-holdout (plan §12,
    DEV-013).
    """
    base = FAKE_EPOCH_US
    rows: list[dict[str, object]] = []
    for i in range(n_accounts):
        account = f"ACC-{i:05d}"
        day = i * span_days // max(n_accounts, 1)
        as_of_us = base + day * 86_400_000_000 + i * 1000
        signal = _unit_hash(account, "signal")
        label = 1 if signal >= 1.0 - positive_share else 0
        # Exposure 1.0M .. 10.0M minor; review minutes 200..320 so the 12,000-minute
        # configured capacity BINDS over each ~66-account test fold (otherwise every policy
        # reviews everything and the ablation is degenerate — the policies must diverge for
        # the threshold-vs-EV row to say anything). Review cost scales with minutes but at a
        # rate that keeps high-exposure true positives net-positive, so the queue is non-empty.
        exposure = 1_000_000 + int(signal * 9_000_000)
        minutes = 200 + (i % 5) * 30
        review_cost = minutes * 5_000
        row: dict[str, object] = {
            COL_ACCOUNT_KEY: account,
            COL_AS_OF_TS: as_of_us,
            COL_LABEL: label,
            COL_SPEC_HASH: spec_hash,
            COL_EXPOSURE_MINOR: exposure,
            COL_REVIEW_COST_MINOR: review_cost,
            COL_REVIEW_MINUTES: minutes,
            COL_AMOUNT_MINOR: exposure,
            "signal": signal,
            "account_age_days": 30 + (i % 900),
            "activity_volume": 1 + (i % 50),
            "community_size": 2 + (i % 40),
        }
        if with_typology:
            row[COL_LABEL_TYPOLOGY] = TYPOLOGY_POOL[i % len(TYPOLOGY_POOL)] if label == 1 else None
        rows.append(row)
    frame = pl.DataFrame(rows)
    return frame.with_columns(
        pl.col(COL_AS_OF_TS).cast(pl.Datetime("us", "UTC")),
    ).sort([COL_AS_OF_TS, COL_ACCOUNT_KEY])


def make_fold_masks(height: int, n_folds: int, embargo_days: int) -> list[HarnessFold]:
    """Expanding-window masks over a time-sorted corpus with an honest embargo gap.

    The fit window ends ``embargo_days`` worth of rows before the test window opens, and
    because the corpus is sorted by ``as_of_ts`` with ~one account per day, withholding the
    last ``embargo_days`` rows before each test block reproduces the real gap. This is a
    fake fold provider for verifying the harness, not the production split.
    """
    folds: list[HarnessFold] = []
    first_test = height // 3
    test_block = (height - first_test) // n_folds
    for index in range(n_folds):
        test_end = first_test + test_block * (index + 1)
        if index == n_folds - 1:
            test_end = height
        test_start = test_end - test_block
        fit_end = max(test_start - embargo_days, 0)
        train_mask = [i < fit_end for i in range(height)]
        validation_mask = [fit_end - embargo_days // 5 <= i < fit_end for i in range(height)]
        for i in range(height):
            validation_mask[i] = validation_mask[i] and train_mask[i]
        train_mask = [train_mask[i] and not validation_mask[i] for i in range(height)]
        test_mask = [test_start <= i < test_end for i in range(height)]
        folds.append(
            HarnessFold(
                index=index,
                train_mask=tuple(train_mask),
                validation_mask=tuple(validation_mask),
                test_mask=tuple(test_mask),
                embargo_days=embargo_days,
            )
        )
    return folds


class FakeFoldProvider:
    """A ``FoldProvider`` over hand-built masks; the harness never sees this much logic."""

    def __init__(self, folds: Sequence[HarnessFold], *, embargo_days: int) -> None:
        self._folds = list(folds)
        self._embargo_days = embargo_days

    def folds(self, corpus: pl.DataFrame) -> Iterator[HarnessFold]:
        del corpus
        return iter(self._folds)

    def embargo_days(self) -> int:
        return self._embargo_days


class _BaseScorer:
    """Shared feature-spec plumbing so the honest and leaking fakes both honour the hash."""

    def __init__(self, spec_hash: str = FAKE_SPEC_HASH, model_version: str = "fake-v1") -> None:
        self._spec_hash = spec_hash
        self._model_version = model_version


class HonestSignalScorer(_BaseScorer):
    """A fake calibrated scorer: p is a noisy hash of the hidden ``signal`` column.

    It never reads ``label`` on the scored frame, so its PR-AUC is a genuine, imperfect
    result of a scorer that ranks by a feature — the honest baseline the control is beaten
    against. The noise (a second hash term) keeps it below the 0.9 leakage flag 00 §18 names.

    ``quality`` in [0, 1] sets how much p tracks the signal versus pure noise. The ablation
    ladder assigns increasing quality to rows 2-6 (scorecard < gbm-no-graph < gbm-with-graph
    < fusion < full-calibrated) so the table reads as a monotone, still-sub-1.0 progression
    rather than six identical cells. It is still a fake: a real ablation differs because
    different feature sets and models are fitted, not because a knob was dialled.
    """

    def __init__(
        self,
        spec_hash: str = FAKE_SPEC_HASH,
        model_version: str = "fake-v1",
        *,
        quality: float = 0.55,
    ) -> None:
        super().__init__(spec_hash, model_version)
        self._quality = min(1.0, max(0.0, quality))

    def score(
        self,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> ScoreResult:
        del train, validation, seed
        if feature_spec_hash != self._spec_hash:
            raise FeatureHashMismatchError(
                f"fake scorer trained on {self._spec_hash[:12]}…, scored frame declares "
                f"{feature_spec_hash[:12]}…: refusing to score a different feature set"
            )
        scores: dict[str, AccountScore] = {}
        for row in scored.to_dicts():
            key = str(row[COL_ACCOUNT_KEY])
            signal = float(row.get("signal", _unit_hash(key, "signal")))
            noisy = min(
                0.95,
                max(
                    0.05, self._quality * signal + (1.0 - self._quality) * _unit_hash(key, "noise")
                ),
            )
            scores[key] = AccountScore(
                account_key=key,
                p_calibrated=noisy,
                band_observed_rate=round(noisy, 2),
                band_n=200,
            )
        return ScoreResult(
            scores=scores, model_version=self._model_version, feature_spec_hash=self._spec_hash
        )


class LeakingLabelScorer(_BaseScorer):
    """The CONTROL arm: it reads the test label directly, so it must visibly win.

    This is the deliberately lookahead-leaking configuration plan §12's gate requires. It
    is NOT a model — it is a cheating oracle included on purpose so that, when the harness
    reports it far ahead of the honest arms, we know the harness can *see* lookahead. Its
    row carries ``is_control=True`` and is never the headline.
    """

    def score(
        self,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> ScoreResult:
        del train, validation, seed
        if feature_spec_hash != self._spec_hash:
            raise FeatureHashMismatchError(
                "fake leaking scorer refuses a feature-hash mismatch too"
            )
        scores: dict[str, AccountScore] = {}
        for row in scored.to_dicts():
            key = str(row[COL_ACCOUNT_KEY])
            label = int(row[COL_LABEL])
            leaked = 0.99 if label == 1 else 0.02
            scores[key] = AccountScore(
                account_key=key,
                p_calibrated=leaked,
                band_observed_rate=leaked,
                band_n=200,
            )
        return ScoreResult(
            scores=scores, model_version="fake-control-lookahead", feature_spec_hash=self._spec_hash
        )


class RulesOnlyScorer(_BaseScorer):
    """A weak fake scorer whose p tracks only rule severity (the 'rules only' ablation row)."""

    def score(
        self,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> ScoreResult:
        del train, validation, seed
        if feature_spec_hash != self._spec_hash:
            raise FeatureHashMismatchError("rules-only fake refuses a feature-hash mismatch")
        scores: dict[str, AccountScore] = {}
        for row in scored.to_dicts():
            key = str(row[COL_ACCOUNT_KEY])
            severity = _unit_hash(key, "rule")
            scores[key] = AccountScore(
                account_key=key,
                p_calibrated=min(0.9, severity),
                band_observed_rate=round(min(0.9, severity), 2),
                band_n=150,
            )
        return ScoreResult(
            scores=scores, model_version="fake-rules-only", feature_spec_hash=self._spec_hash
        )


class FeatureHashMismatchError(Exception):
    """Raised by the fake scorer on a feature-spec mismatch, mirroring P4's refusal."""


class GreedyAllocator:
    """A fake ``Allocator``: greedy by EV density, the primitive form of P5's policy.

    Used only to exercise the harness's EV path with hand-checkable arithmetic. The real
    allocation is P5's; this returns the same shape (reviewed set + total EV + label) so a
    timeout-free CP-SAT stand-in can be the exact solver for the optimality-gap row.
    """

    def allocate(
        self,
        *,
        items: Sequence[AllocationItem],
        capacity_minutes: int,
        policy: str,
        seed: int,
    ) -> AllocationResult:
        del seed
        priced = [
            (
                item.account_key,
                _expected_ev_minor(item, recovery_rate=0.35, friction_cost_minor=2_500_000)
                / item.minutes_i,
                item.minutes_i,
            )
            for item in items
        ]
        if policy == "cpsat":
            ordered = sorted(priced, key=lambda t: (-t[1], t[0]))
            chosen = _knapsack_exact(ordered, capacity_minutes)
            label = "fake cpsat-exact 0/1 knapsack"
        else:
            ordered = sorted(priced, key=lambda t: (-t[1], t[0]))
            chosen = _rank_fill(ordered, capacity_minutes)
            label = "fake greedy by EV density"
        reviewed = tuple(chosen)
        total = sum(
            _expected_ev_minor(item, recovery_rate=0.35, friction_cost_minor=2_500_000)
            for item in items
            if item.account_key in reviewed
        )
        minutes = sum(item.minutes_i for item in items if item.account_key in reviewed)
        return AllocationResult(
            reviewed=reviewed, total_ev_minor=total, minutes_used=minutes, allocator_label=label
        )


class FakeRuleHits:
    """A fake ``RuleHitsProvider`` deriving severity and hit counts from a hashed rule signal."""

    def rule_hits(self, *, fold_index: int, accounts: Sequence[str]) -> dict[str, RuleHit]:
        del fold_index
        return {
            key: RuleHit(
                account_key=key,
                severity=min(1.0, _unit_hash(key, "rule")),
                hit_count=int(_unit_hash(key, "hits") * 5),
            )
            for key in accounts
        }


def _expected_ev_minor(
    item: AllocationItem, *, recovery_rate: float, friction_cost_minor: int
) -> int:
    intercept = money_scaled(item.exposure_minor, item.p_calibrated * recovery_rate, "UGX")
    friction = money_scaled(friction_cost_minor, 1.0 - item.p_calibrated, "UGX")
    return intercept - item.review_cost_minor - friction


def _rank_fill(ordered: Sequence[tuple[str, float, int]], capacity_minutes: int) -> list[str]:
    remaining = capacity_minutes
    chosen: list[str] = []
    for key, _density, minutes in ordered:
        if minutes <= remaining:
            chosen.append(key)
            remaining -= minutes
    return chosen


def _knapsack_exact(ordered: Sequence[tuple[str, float, int]], capacity_minutes: int) -> list[str]:
    """A true 0/1 knapsack on density-ordered items, so the optimality gap is nonzero.

    Small-corpus only (O(n * capacity)); the fake never runs at corpus scale. Its point is
    to be *exactly* optimal so greedy-vs-exact shows a real money gap, which is the row
    plan §11/§12 want.
    """
    items = list(ordered)
    n = len(items)
    capacity = int(capacity_minutes)
    if n * capacity > 4_000_000:  # keep the demo fast; falls back to greedy fill above this
        return _rank_fill(items, capacity)
    best = [0.0] * (capacity + 1)
    keep = [[False] * (capacity + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        key, density, minutes = items[i - 1]
        del key
        for w in range(capacity, -1, -1):
            take = best[w - minutes] + density if w >= minutes else -math.inf
            if take > best[w]:
                best[w] = take
                keep[i][w] = True
    chosen: list[str] = []
    w = capacity
    for i in range(n, 0, -1):
        if keep[i][w]:
            chosen.append(items[i - 1][0])
            w -= items[i - 1][2]
    return chosen[::-1]


def hand_fold_accounts() -> list[FoldAccount]:
    """A tiny hand-checkable set of accounts for the metric tests' paper arithmetic."""
    return [
        FoldAccount("A", 1, 1_000_000, 100_000, 5, 1_000_000, 0.9, typology="CYCLE"),
        FoldAccount("B", 0, 2_000_000, 100_000, 5, 2_000_000, 0.8),
        FoldAccount("C", 1, 3_000_000, 100_000, 5, 3_000_000, 0.4, typology="FAN-IN"),
        FoldAccount("D", 0, 4_000_000, 100_000, 5, 4_000_000, 0.2),
    ]


__all__ = [
    "FAKE_SPEC_HASH",
    "FakeFoldProvider",
    "FakeRuleHits",
    "FeatureHashMismatchError",
    "GreedyAllocator",
    "HonestSignalScorer",
    "LeakingLabelScorer",
    "RulesOnlyScorer",
    "hand_fold_accounts",
    "make_corpus",
    "make_fold_masks",
]
