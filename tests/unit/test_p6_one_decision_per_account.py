"""P6 — a fold books one decision per account, not one per scored row (DEV-026).

Measured on the landed 40k corpus: 79,998 rows carry 77,691 distinct (account, fold) pairs,
because the corpus is one row per (account, as-of) and an account that moved money at four
timestamps inside one fold has four rows. The harness scored those rows into a per-row list
and then re-booked the money through a dict keyed by account, so an account the allocator
selected twice was charged its minutes twice against a capacity it had paid once. The
capacity postcondition caught it -- ``policy booked 12025 minutes over a 12000-minute
capacity`` -- after 70 minutes of fitting, which is the only reason this is known at all.

Every expected value below is computed on paper from the fixture in
:func:`_sixty_row_corpus`, not read back out of the code under test (00 §B).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import polars as pl
import pytest

from oxbow.backtest import fakes
from oxbow.backtest.economics import FoldAccount, realized_fold_economics
from oxbow.backtest.harness import (
    FoldError,
    _decisions_per_account,
    run_variant,
    validate_corpus,
)
from oxbow.backtest.interfaces import COL_AS_OF_TS
from tests.unit.p6_fixtures import as_of_us, fast_config, hand_corpus

REP = "ACC-REP"
HEIGHT = 60
EMBARGO_DAYS = 15
# make_fold_masks: first_test = 60 // 3 = 20, test_block = (60 - 20) // 2 = 20, so fold 0
# scores rows 20..39 and fold 1 scores rows 40..59. Rows 21 and 22 are the same account.
FOLD0_ROWS = 20
FOLD0_ACCOUNTS = 19


def _sixty_row_corpus() -> pl.DataFrame:
    """60 rows, one per day, with ``ACC-REP`` appearing twice inside fold 0's test window.

    Rows 20..23 are the designed block: AAA (40 min, clean), REP at day 21 (60 min, positive)
    and REP again at day 22 (90 min, clean), then BBB (10 min, clean). The other 56 rows are
    filler accounts at 30 minutes each, all clean, low signal.
    """
    rows: list[dict[str, Any]] = []
    for index in range(HEIGHT):
        row: dict[str, Any] = {
            "account_key": f"FILLER-{index:04d}",
            COL_AS_OF_TS: as_of_us(index),
            "label_is_fraud": 0,
            "exposure_minor": 1_000,
            "review_cost_minor": 100,
            "review_minutes": 30,
            "signal": 0.10,
        }
        if index == 20:
            row.update({"account_key": "ACC-AAA", "review_minutes": 40, "signal": 0.90})
        elif index == 21:
            row.update(
                {
                    "account_key": REP,
                    "label_is_fraud": 1,
                    "exposure_minor": 1_000,
                    "review_cost_minor": 300,
                    "review_minutes": 60,
                    "signal": 0.90,
                }
            )
        elif index == 22:
            row.update(
                {
                    "account_key": REP,
                    "label_is_fraud": 0,
                    "exposure_minor": 2_000,
                    "review_cost_minor": 450,
                    "review_minutes": 90,
                    "signal": 0.90,
                }
            )
        elif index == 23:
            row.update({"account_key": "ACC-BBB", "review_minutes": 10, "signal": 0.90})
        elif index == 45:
            # One positive in fold 1 so that fold reports a metric rather than a skip.
            row.update({"label_is_fraud": 1, "signal": 0.90})
        rows.append(row)
    return hand_corpus(rows)


def _fold0(variant: Any) -> Any:
    folds = variant.policies["score_threshold"].folds
    by_index = {fold.fold_index: fold for fold in folds}
    assert 0 in by_index, f"fold 0 did not report: {sorted(by_index)}"
    return by_index[0]


def test_the_fold_reports_rows_and_decisions_apart() -> None:
    """The population the fold scores is not the population it books money against."""
    config = fast_config()
    corpus = _sixty_row_corpus()
    assert validate_corpus(corpus) == fakes.FAKE_SPEC_HASH

    folds = fakes.make_fold_masks(height=HEIGHT, n_folds=2, embargo_days=EMBARGO_DAYS)
    variant = run_variant(
        label="grain",
        corpus=corpus,
        corpus_name="hand-fake",
        provenance="fake_harness",
        question="does one account in two rows become two reviews?",
        fold_provider=fakes.FakeFoldProvider(folds, embargo_days=EMBARGO_DAYS),
        scorer=fakes.HonestSignalScorer(quality=0.60),
        config=config,
        policies=("score_threshold",),
        allocator=fakes.GreedyAllocator(),
    )
    fold0 = _fold0(variant)

    assert fold0.n_scored_rows == FOLD0_ROWS, (
        "the fold's scored-row count is a fact about the corpus; collapsing the decisions must "
        "not restate it"
    )
    assert fold0.n_decisions == FOLD0_ACCOUNTS, (
        f"rows 21 and 22 are the same account inside fold 0, so 20 scored rows are 19 decisions; "
        f"got {fold0.n_decisions}. A fold that reports 20 decisions is booking ACC-REP's minutes "
        "twice, which is the DEV-026 failure."
    )


def test_the_latest_stamp_carries_the_money_and_any_stamp_can_make_the_account_positive() -> None:
    """The collapse rule, asserted on the hand-written numbers above."""
    corpus = _sixty_row_corpus()
    stamps = corpus.get_column(COL_AS_OF_TS).to_list()
    rows = [
        FoldAccount(
            account_key=str(corpus.row(index, named=True)["account_key"]),
            label=int(corpus.row(index, named=True)["label_is_fraud"]),
            exposure_minor=int(corpus.row(index, named=True)["exposure_minor"]),
            review_cost_minor=int(corpus.row(index, named=True)["review_cost_minor"]),
            review_minutes=int(corpus.row(index, named=True)["review_minutes"]),
            amount_minor=int(corpus.row(index, named=True)["amount_minor"]),
            p_calibrated=0.9,
        )
        for index in (20, 21, 22, 23)
    ]
    decisions = _decisions_per_account(rows, stamps[20:24])

    assert [decision.account_key for decision in decisions] == ["ACC-AAA", REP, "ACC-BBB"], (
        "first-appearance order is the fold's order; a set would make the queue non-deterministic"
    )
    rep = next(decision for decision in decisions if decision.account_key == REP)
    assert rep.review_minutes == 90 and rep.exposure_minor == 2_000, (
        "the latest as-of is the account's state when the analyst reaches the queue; summing "
        "two rolling 24h exposures would count the same euro twice"
    )
    assert rep.review_cost_minor == 450
    assert rep.label == 1, (
        "the account defrauded once in the period is a positive for the period; taking the "
        "latest stamp's label alone would grade it clean"
    )


def test_an_unordered_corpus_refuses_instead_of_choosing_an_arbitrary_latest_row() -> None:
    """`latest` only means something if the rows arrived in as-of order."""
    epoch = datetime(2014, 1, 1, tzinfo=None)  # the corpus is naive-UTC at the fake level
    earlier = epoch + timedelta(days=1)
    later = epoch + timedelta(days=4)
    rows = [
        FoldAccount("A", 0, 10, 10, 30, 10, 0.5),
        FoldAccount("A", 1, 20, 20, 40, 20, 0.5),
    ]

    with pytest.raises(FoldError, match="out of as-of order"):
        _decisions_per_account(rows, [later, earlier])

    decisions = _decisions_per_account(rows, [earlier, later])
    assert [decision.review_minutes for decision in decisions] == [40]
    assert decisions[0].label == 1


def test_booking_refuses_a_repeated_key_instead_of_charging_it_twice() -> None:
    """The exact shape that killed the 40k backtest run, kept as a standing gate."""
    config = fast_config()
    accounts = [
        FoldAccount("A", 1, 1_000, 300, 60, 1_000, 0.9),
        FoldAccount("B", 0, 500, 100, 30, 500, 0.2),
    ]

    with pytest.raises(ValueError, match="book its minutes twice"):
        realized_fold_economics(
            accounts,
            ("A", "A"),
            recovery_rate=config.recovery_rate,
            friction_cost_minor=config.friction_cost_minor,
            capacity_minutes=config.capacity_minutes,
            currency=config.currency,
            mc_draws=10,
            mc_seed=config.seed,
            var_alpha=config.var_alpha,
            es_alpha=config.es_alpha,
        )


def test_booking_refuses_a_key_the_fold_never_scored() -> None:
    """Dropping an unknown review would hide an allocator bug behind a smaller bill."""
    config = fast_config()
    accounts = [FoldAccount("A", 1, 1_000, 300, 60, 1_000, 0.9)]

    with pytest.raises(ValueError, match="never scored"):
        realized_fold_economics(
            accounts,
            ("A", "GHOST"),
            recovery_rate=config.recovery_rate,
            friction_cost_minor=config.friction_cost_minor,
            capacity_minutes=config.capacity_minutes,
            currency=config.currency,
            mc_draws=10,
            mc_seed=config.seed,
            var_alpha=config.var_alpha,
            es_alpha=config.es_alpha,
        )


def test_a_fold_that_names_the_same_account_twice_is_refused_before_booking() -> None:
    """The population itself must be one decision per account, or the bill is a guess."""
    config = fast_config()
    account = FoldAccount("A", 1, 1_000, 300, 60, 1_000, 0.9)

    with pytest.raises(ValueError, match="one decision per account per fold"):
        realized_fold_economics(
            [account, account],
            ("A",),
            recovery_rate=config.recovery_rate,
            friction_cost_minor=config.friction_cost_minor,
            capacity_minutes=config.capacity_minutes,
            currency=config.currency,
            mc_draws=10,
            mc_seed=config.seed,
            var_alpha=config.var_alpha,
            es_alpha=config.es_alpha,
        )


def test_the_happy_path_books_exactly_the_hand_computed_minutes() -> None:
    """Guard against a guard: the same call with a clean population must not raise."""
    config = fast_config()
    accounts = [
        FoldAccount("A", 1, 1_000, 300, 60, 1_000, 0.9),
        FoldAccount("B", 0, 500, 100, 30, 500, 0.2),
    ]
    economics = realized_fold_economics(
        accounts,
        ("A", "B"),
        recovery_rate=config.recovery_rate,
        friction_cost_minor=config.friction_cost_minor,
        capacity_minutes=config.capacity_minutes,
        currency=config.currency,
        mc_draws=10,
        mc_seed=config.seed,
        var_alpha=config.var_alpha,
        es_alpha=config.es_alpha,
    )
    assert economics.minutes_used == 90, "60 + 30, each account once"
    assert economics.accounts_reviewed == 2
    assert economics.true_positives == 1 and economics.false_positives == 1
