"""The injected seams the P6 backtest harness runs against, and nothing else.

WHY THIS FILE EXISTS (plan §12 "Interfaces you consume — code against these,
injected, never imported concretely"): every component the walk-forward evaluates —
P2's fold provider, P4's calibrator, P5's allocator, P3b's rule hits — is being
written in another package by another agent right now. If `harness.py` imported them
by name it could not run until all four landed, could not be tested before they
exist, and could not be shown to bite on a hand-computed fixture. So the harness
depends only on the four Protocols here and accepts their implementations as
arguments.

THE DISCIPLINE THAT MAKES THE GATE MEANINGFUL: the harness does not compute folds.
`FoldProvider` is satisfied by the ONE splits module plan §8 mandates
(`oxbow.backtest.splits`, owned by the feature layer); the harness consumes the
masks it yields. Fold-boundary arithmetic living in two places is the "purge/embargo
misapplication" defect plan §8 calls "the single most common way a backtest becomes
fiction".

MONEY (DEV-005): every amount on these types is integer minor units, suffixed
`_minor`. Rates, probabilities and rank ratios are floats and named for what they
are. `scripts/no_float_money.py` walks the package and rejects a money-named symbol
annotated as a float, so a leaked `float` amount is a build failure, not a review
note.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import polars as pl

# The corpus is one row per account per as-of instant. These are the column names
# the harness reads; they match the P4 frame contract (`oxbow.scoring.frame`) where
# the two overlap so a single feature table serves both scoring and backtesting.
COL_ACCOUNT_KEY: str = "account_key"
COL_AS_OF_TS: str = "as_of_ts"
COL_LABEL: str = "label_is_fraud"
COL_LABEL_TYPOLOGY: str = "label_typology"
COL_SPEC_HASH: str = "feature_spec_hash"
# Economics per account, integer minor units (DEV-005).
COL_EXPOSURE_MINOR: str = "exposure_minor"
COL_REVIEW_COST_MINOR: str = "review_cost_minor"
COL_REVIEW_MINUTES: str = "review_minutes"
COL_AMOUNT_MINOR: str = "amount_minor"
# Fairness proxies. Neither corpus has a protected attribute, so the harness checks
# the realistic over-flagging axes plan §12 names instead (see fairness.py).
COL_ACCOUNT_AGE_DAYS: str = "account_age_days"
COL_ACTIVITY_VOLUME: str = "activity_volume"
COL_COMMUNITY_SIZE: str = "community_size"

# The join key from `data/processed/ibm_typologies.parquet` onto a corpus frame.
# DEV-014: the typology labels ride on transactions, addressed by ordinal.
COL_TXN_ORDINAL: str = "txn_ordinal"

# The columns a per-account backtest corpus must carry for the economic and
# statistical blocks. The typology label and the fairness proxies are optional:
# absent, the harness reports those blocks as *not computed* rather than zero.
REQUIRED_CORPUS_COLUMNS: tuple[str, ...] = (
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    COL_LABEL,
    COL_SPEC_HASH,
    COL_EXPOSURE_MINOR,
    COL_REVIEW_COST_MINOR,
    COL_REVIEW_MINUTES,
    COL_AMOUNT_MINOR,
)
OPTIONAL_CORPUS_COLUMNS: tuple[str, ...] = (
    COL_LABEL_TYPOLOGY,
    COL_TXN_ORDINAL,
    COL_ACCOUNT_AGE_DAYS,
    COL_ACTIVITY_VOLUME,
    COL_COMMUNITY_SIZE,
)


class BacktestError(RuntimeError):
    """A backtest precondition failed loudly rather than silently.

    01 §G makes "catching a broad exception and returning an empty list" a rejection
    trigger, so every refusal in this package is a named subclass of this one type.
    A fold the corpus cannot support, an embargo that does not block, a feature-hash
    mismatch — each raises this family, never returns an empty table.
    """


class FoldError(BacktestError):
    """Raised when supplied folds violate the temporal or embargo discipline."""


@dataclass(frozen=True, slots=True)
class HarnessFold:
    """One walk-forward split, as the harness consumes it: masks plus the embargo gap.

    WHY A SEPARATE TYPE FROM ``splits.Fold`` (which is ``oxbow.backtest.splits``, owned
    by the feature layer): the splits module's ``Fold`` carries timestamps and emits
    *polars expressions*; the harness only wants boolean masks aligned to the corpus it
    was handed, so it can be driven by a hand-built fake with no polars expression
    machinery. ``SplitsFoldProvider`` (in this package) is the adapter that turns the
    ONE splits module's expressions into these masks by evaluating them against the
    corpus. Masks make ``test_embargo_blocks_leakage`` a statement about the *timestamps*
    behind them — which is the whole point of the gap — while keeping fold computation in
    exactly one module.

    Named ``HarnessFold`` so a reader never confuses it with ``splits.Fold``.
    """

    index: int
    train_mask: tuple[bool, ...]
    validation_mask: tuple[bool, ...]
    test_mask: tuple[bool, ...]
    embargo_days: int

    def counts(self) -> tuple[int, int, int]:
        """(train, validation, test) row counts, for the audit trail."""
        return (sum(self.train_mask), sum(self.validation_mask), sum(self.test_mask))


@runtime_checkable
class FoldProvider(Protocol):
    """Satisfied by the ONE splits module (plan §8) via ``SplitsFoldProvider``.

    The harness never computes folds. This is the seam that keeps purge/embargo
    logic in exactly one place, which is the point of the whole phase: a fold the
    harness invented to make the arithmetic work is a leak dressed as a result.
    """

    def folds(self, corpus: pl.DataFrame) -> Iterator[HarnessFold]:
        """Yield each fold's masks, temporally ordered, over ``corpus`` row order."""
        ...

    def embargo_days(self) -> int:
        """The embargo the provider applies, which must equal the longest feature
        lookback in config/features.yaml (the split module asserts that agreement)."""
        ...


@dataclass(frozen=True, slots=True)
class AccountScore:
    """One account's calibrated probability plus the evidence behind the number.

    `band_observed_rate` and `band_n` travel with `p_calibrated` because plan §10
    forbids an invented confidence adjective: the UI label is "observed rate in this
    band: 71%, n=432", and the money downstream is multiplied by exactly this p, so
    a miscalibrated bin is a wrong currency figure, not merely a wrong rank.
    """

    account_key: str
    p_calibrated: float
    band_observed_rate: float
    band_n: int

    def __post_init__(self) -> None:
        for name in ("p_calibrated", "band_observed_rate"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise BacktestError(f"{name}={value} for {self.account_key} is outside [0, 1]")
        if self.band_n < 0:
            raise BacktestError(f"band_n={self.band_n} for {self.account_key} is negative")


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """A fold's calibrated output: per-account p, band evidence, model provenance.

    `model_version` is stored on every scored row (plan §10, 02 §B seam 4) so the
    backtest can say which model produced a number, and `feature_spec_hash` is the
    hash it was fitted against — the harness re-checks the fold's frame carries the
    same hash, which is the seam that refuses "trained on one feature set, scored
    with another".
    """

    scores: Mapping[str, AccountScore]
    model_version: str
    feature_spec_hash: str

    def p_of(self, account_key: str) -> float:
        """Calibrated probability for one account, refusing a silent zero.

        03 §A rule 2: never let an unknown become a zero. A missing account is a
        contract break, not a low score.
        """
        try:
            return self.scores[account_key].p_calibrated
        except KeyError as exc:
            raise BacktestError(
                f"scorer returned no probability for {account_key!r}: reporting a missing "
                "score as p=0 would move an account in the ranking silently."
            ) from exc


@runtime_checkable
class Scorer(Protocol):
    """P4's calibrator, injected. Fits on train+validation, predicts on `scored`.

    WHY `scored` IS AN ARGUMENT: plan §12 phrases the seam as "given a fold's
    train+validation frames ... returns per-account p_i". Physically, a fitted model
    must be handed *something to predict on*; P4's `score_frame` takes the frame to
    score. The harness passes the fold's test rows as `scored`, so the fitted model
    scores only the holdout and never sees its labels (they are not in `scored`'s
    feature columns). `seed` supports the 5-seed stability requirement without the
    harness knowing how a seed enters P4's training.

    MUST RAISE on a feature-hash mismatch (plan §8 / 02 §B seam 3): the harness
    passes the fold frame's declared `feature_spec_hash` and expects the scorer to
    refuse if it disagrees with what it trained on — the harness does not paper over
    that by catching and continuing.
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
        """Return per-account calibrated scores for `scored`."""
        ...


@dataclass(frozen=True, slots=True)
class AllocationItem:
    """One account as the allocator sees it: a probability and its money terms.

    `p_i` is a float probability; the three `_minor` amounts are integer minor units
    and `minutes_i` is an integer count. This is the exact tuple plan §12 names for
    the Allocator seam.
    """

    account_key: str
    p_calibrated: float
    exposure_minor: int
    review_cost_minor: int
    minutes_i: int

    def __post_init__(self) -> None:
        if not 0.0 <= self.p_calibrated <= 1.0:
            raise BacktestError(f"p_calibrated={self.p_calibrated} for {self.account_key}")
        for name in ("exposure_minor", "review_cost_minor", "minutes_i"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise BacktestError(
                    f"{name}={value!r} for {self.account_key} is not an integer; money and "
                    "minutes are int64 minor units (DEV-005)."
                )
        if self.minutes_i <= 0:
            raise BacktestError(
                f"minutes_i={self.minutes_i} for {self.account_key}: EV density divides by "
                "minutes, so a zero-minute alert scores an infinite density (plan §11 floor)."
            )


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """A policy's decision: which accounts to review, its expected value, its label.

    `allocator_label` names which solver produced the set (greedy vs exact CP-SAT
    vs a timeout fallback) — plan §11 forbids presenting a degraded result as if it
    were chosen. `total_ev_minor` is expected value at the default recovery rate; the
    *realized* backtest net benefit is computed by the harness from true labels,
    because a backtest is judged on what actually happened, not on the model's hope.
    """

    reviewed: tuple[str, ...]
    total_ev_minor: int
    minutes_used: int
    allocator_label: str

    def __post_init__(self) -> None:
        if isinstance(self.total_ev_minor, bool) or not isinstance(self.total_ev_minor, int):
            raise BacktestError("total_ev_minor must be integer minor units (DEV-005)")
        if isinstance(self.minutes_used, bool) or not isinstance(self.minutes_used, int):
            raise BacktestError("minutes_used must be an integer analyst-minute count")


@runtime_checkable
class Allocator(Protocol):
    """P5's allocation, injected. Greedy-by-EV-density and exact CP-SAT.

    Returns the review set under `capacity_minutes` and the total expected value,
    with the allocator label. The harness supplies the primitives (plan §12) so it
    does not need P5's `Economics`/`AccountEV` types to exist to be testable.
    """

    def allocate(
        self,
        *,
        items: Sequence[AllocationItem],
        capacity_minutes: int,
        policy: str,
        seed: int,
    ) -> AllocationResult:
        """Allocate under capacity for `policy` (greedy / cpsat), labelled."""
        ...


@dataclass(frozen=True, slots=True)
class RuleHit:
    """A rule-derived signal for one account: rankable severity and a hit count.

    Severity is normalised 0-1 as a documented function of how far the observation
    exceeds threshold (plan §9), so the rules-only baseline stays rankable rather
    than degenerate, and the fusion channel has a numeric input.
    """

    account_key: str
    severity: float
    hit_count: int

    def __post_init__(self) -> None:
        if not 0.0 <= self.severity <= 1.0:
            raise BacktestError(f"rule severity={self.severity} for {self.account_key}")
        if self.hit_count < 0:
            raise BacktestError(f"rule hit_count={self.hit_count} for {self.account_key}")


@runtime_checkable
class RuleHitsProvider(Protocol):
    """P3b's rule engine, injected. Supplies per-account severity and hit counts.

    Used for the rules-only baseline and the fusion input. Provided per fold because
    plan §8 requires rule thresholds fitted on the training window only, so the fold
    index is what selects the fitted thresholds.
    """

    def rule_hits(self, *, fold_index: int, accounts: Sequence[str]) -> Mapping[str, RuleHit]:
        """Return the rule hit for each requested account in this fold."""
        ...


# Fold-boundary time stamps carried alongside a Fold so the harness can *verify* the
# embargo on real timestamps instead of trusting the masks. A provider returns these
# via `folds()`; the plain `Fold` above keeps the interface minimal for hand-built
# fakes that only need masks.
@dataclass(frozen=True, slots=True)
class FoldWindow:
    """The (train_end, test_start, as-of bounds) a fold spans, for the audit trail.

    The harness reports the walk-forward diagram from these numbers (via
    `oxbow.scoring.frame.fold_time_spans`), so they are recorded per fold rather
    than re-derived from config fractions at render time.
    """

    index: int
    train_end: datetime
    test_start: datetime
    test_end: datetime
    embargo_days: int


__all__ = [
    "COL_ACCOUNT_AGE_DAYS",
    "COL_ACCOUNT_KEY",
    "COL_ACTIVITY_VOLUME",
    "COL_AMOUNT_MINOR",
    "COL_AS_OF_TS",
    "COL_COMMUNITY_SIZE",
    "COL_EXPOSURE_MINOR",
    "COL_LABEL",
    "COL_LABEL_TYPOLOGY",
    "COL_REVIEW_COST_MINOR",
    "COL_REVIEW_MINUTES",
    "COL_SPEC_HASH",
    "COL_TXN_ORDINAL",
    "OPTIONAL_CORPUS_COLUMNS",
    "REQUIRED_CORPUS_COLUMNS",
    "AccountScore",
    "AllocationItem",
    "AllocationResult",
    "Allocator",
    "BacktestError",
    "FoldError",
    "FoldProvider",
    "FoldWindow",
    "HarnessFold",
    "RuleHit",
    "RuleHitsProvider",
    "ScoreResult",
    "Scorer",
]
