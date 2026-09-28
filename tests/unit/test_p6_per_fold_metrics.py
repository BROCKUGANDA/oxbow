"""Per-fold discrimination, drawdown and entity-disjointness — the figures `backtest_fold` needs.

``backtest_fold`` is one row per ``(run, fold)`` and declares nine columns NOT NULL. Four of them
could not be filled at all, because the harness reduced AUROC, Brier and drawdown only over the
pooled run, and nothing anywhere recorded whether a fold's training accounts reappear in its test
window. The other five (the window bounds) come from the splits module through the artifact's
``fold_windows``; they are covered in ``test_p7_analytical_landing.py``.

Why the run-level number cannot be copied down into a fold row: five folds reporting one AUROC is
DEV-027 at a different grain — the reader would take five samples of one measurement as evidence
that the configuration is stable across time. So each fold now reduces its own decisions, and this
file pins two things that make that checkable rather than plausible:

* the per-fold drawdown is the SAME curve the aggregate reports, sampled at that fold, so the last
  fold's figure and the run's figure are one number by construction, not two that happen to agree;
* ``entity_disjoint`` is a measurement over the fold's own masks. A temporal walk-forward over
  accounts that keep transacting is usually NOT entity-disjoint — the purge and the embargo
  withhold *time*, not *entities* — and the table exists to say which folds were which.
"""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest

from oxbow.backtest import fakes
from oxbow.backtest.harness import (
    FoldError,
    HarnessFold,
    _entities_disjoint,
    run_variant,
    variant_to_dict,
)
from tests.unit.p6_fixtures import as_of_us, fast_config, hand_corpus

HEIGHT = 30
EMBARGO_DAYS = 5
SHARED = "ACC-SHARED"
# make_fold_masks(30, 2, 5): first_test = 10, test_block = 10.
#   fold 0: train rows 0-3, validation row 4, embargo rows 5-9, test rows 10-19
#   fold 1: train rows 0-11, validation rows 12-14, test rows 20-29
FOLD0_TRAIN = (1, 2, 3)
FOLD0_TEST = range(10, 20)

# The only positives: both inside fold 1's test window, so fold 0 has no positive at all.
POSITIVE_ROWS = (21, 22)


def _corpus() -> pl.DataFrame:
    """Thirty rows, one account reused across fold 0's boundary, positives only in fold 1.

    ``SHARED`` sits at row 1 (fold 0's training window) and row 12 (fold 1's training window). For
    fold 0 that is a train/test overlap, so its answer is False. Fold 1's test rows are all
    single-occurrence accounts, so its answer is True — one run, two folds, two different facts.
    """
    rows: list[dict[str, Any]] = []
    for index in range(HEIGHT):
        row: dict[str, Any] = {
            "account_key": SHARED if index in (1, 12) else f"ACC-{index:04d}",
            "as_of_ts": as_of_us(index),
            "label_is_fraud": 1 if index in POSITIVE_ROWS else 0,
            "exposure_minor": 5_000 if index in POSITIVE_ROWS else 1_000,
            "review_cost_minor": 100,
            "review_minutes": 30,
            "signal": 0.90 if index in POSITIVE_ROWS else 0.10,
        }
        rows.append(row)
    return hand_corpus(rows)


def _variant() -> Any:
    return run_variant(
        label="per-fold",
        corpus=_corpus(),
        corpus_name="hand-fake",
        provenance="fake_harness",
        question="does each fold report its own measurement?",
        fold_provider=fakes.FakeFoldProvider(
            fakes.make_fold_masks(height=HEIGHT, n_folds=2, embargo_days=EMBARGO_DAYS),
            embargo_days=EMBARGO_DAYS,
        ),
        scorer=fakes.HonestSignalScorer(quality=0.60),
        config=fast_config(),
        policies=("score_threshold",),
    )


def _folds(variant: Any) -> dict[int, Any]:
    return {fold.fold_index: fold for fold in variant.policies["score_threshold"].folds}


# ---------------------------------------------------------------------------


def test_each_fold_reports_its_own_auroc_and_brier_not_the_runs() -> None:
    """A fold with no positives cannot report the run's AUROC, and that is the point.

    Fold 0's test window holds no fraud at all, so its ranking has no positives to separate: the
    honest answer is None with the skip reason attached. Copying the run-level AUROC down would
    have fold 0 claim discrimination it never measured.
    """
    variant = _variant()
    folds = _folds(variant)
    aggregate = variant.policies["score_threshold"]

    assert folds[0].n_test_positive == 0, "the fixture, restated so the next assertions mean it"
    assert folds[0].auroc is None, folds[0].auroc
    assert folds[0].skipped_reason is not None and "zero_positive_fold" in folds[0].skipped_reason
    assert folds[1].n_test_positive == len(POSITIVE_ROWS)
    assert folds[1].auroc is not None, "fold 1 has two positives, so it has a rankable curve"
    assert 0.0 <= folds[1].auroc <= 1.0
    assert folds[0].brier is not None, "Brier is defined with no positives; AUROC is not"
    assert aggregate.auroc is not None
    assert folds[1].auroc != aggregate.auroc or folds[0].auroc is None, (
        "if a fold's AUROC equals the pooled run's while another fold has none, the per-fold "
        "reduction is not happening"
    )


def test_the_last_fold_drawdown_is_the_run_drawdown_by_construction() -> None:
    """The fold figure is the run's curve sampled at that fold — one definition, two views.

    Asserted as an identity rather than a value, because the requirement is that the fold table and
    the aggregate can never disagree about the drawdown of the same money. The figure lives on the
    serialised fold record, not on ``FoldResult``: it is a property of the curve, and a fold that
    carried its own copy could drift from the run it belongs to.
    """
    variant = _variant()
    records = variant_to_dict(variant)["policies"]["score_threshold"]["folds"]
    by_index = {record["fold_index"]: record for record in records}
    aggregate = variant.policies["score_threshold"]

    # Fold 0's net benefit is a review cost with no capture, so its single-point curve has exactly
    # that much run-off: peak starts at 0, so the drop is -net.
    assert aggregate.folds[0].economics.net_benefit_minor < 0
    assert (
        by_index[0]["max_drawdown_minor"] == -by_index[0]["economics"]["net_benefit_minor"]
    ), "one point below zero is a drawdown of that point, not zero"
    assert by_index[1]["max_drawdown_minor"] == aggregate.max_drawdown_minor, (
        "the last prefix of the cumulative curve IS the curve; two numbers here would mean the "
        "fold row computed its own definition of drawdown"
    )


def test_entity_disjoint_is_measured_per_fold_and_differs_between_them() -> None:
    """The account that trained fold 0 also appears in its test window; fold 1 has no such overlap.

    A walk-forward purges and embargoes TIME. An account that keeps transacting across the boundary
    is in both slices, and the table would be lying if it said otherwise — which is why the column
    is a measurement of the masks and not a constant.
    """
    variant = _variant()
    folds = _folds(variant)

    assert (
        folds[0].entity_disjoint is False
    ), f"{SHARED} trains fold 0 at row 1 and is scored by it at row 12"
    assert (
        folds[1].entity_disjoint is True
    ), "fold 1's test accounts each appear once, after its whole fit window"


def test_a_fold_that_never_saw_the_account_twice_is_disjoint() -> None:
    """The helper on its own, with the same masks and the reused key renamed away."""
    fold = HarnessFold(
        index=0,
        train_mask=[index in FOLD0_TRAIN for index in range(HEIGHT)],
        validation_mask=[index == 4 for index in range(HEIGHT)],
        test_mask=[index in FOLD0_TEST for index in range(HEIGHT)],
        embargo_days=EMBARGO_DAYS,
    )
    corpus = _corpus()
    assert (
        _entities_disjoint(corpus, fold) is False
    ), f"{SHARED} is at row 1 (train) and row 12 (test)"

    unique = corpus.with_row_index("_row").with_columns(
        pl.concat_str([pl.lit("U"), pl.col("_row").cast(pl.String)]).alias("account_key")
    )
    assert _entities_disjoint(unique, fold) is True, "one key per row, so the masks cannot overlap"


def test_the_artifact_carries_the_per_fold_figures_the_warehouse_reads() -> None:
    """The mapper joins on these four keys; without them `backtest_fold` refuses every fold.

    Asserted against the serialised document rather than the dataclass, because the document is
    what the warehouse stage actually parses.
    """
    payload = variant_to_dict(_variant())
    records = payload["policies"]["score_threshold"]["folds"]

    assert len(records) == 2
    for record in records:
        for key in ("auroc", "brier", "max_drawdown_minor", "entity_disjoint"):
            assert key in record, f"{key} missing from the fold record: {sorted(record)}"
    by_index = {record["fold_index"]: record for record in records}
    assert by_index[0]["entity_disjoint"] is False and by_index[1]["entity_disjoint"] is True
    assert by_index[0]["auroc"] is None
    assert by_index[0]["max_drawdown_minor"] == -by_index[0]["economics"]["net_benefit_minor"]


def test_a_fold_with_no_recorded_disjoint_answer_is_refused_not_assumed() -> None:
    """Assuming `false` would state a property of the split that this run never checked."""
    from oxbow.backtest.economics import FoldAccount
    from oxbow.backtest.harness import _run_policies

    fold = fakes.make_fold_masks(height=HEIGHT, n_folds=1, embargo_days=EMBARGO_DAYS)[0]
    config = fast_config()
    accounts = [
        FoldAccount(
            account_key=str(row["account_key"]),
            label=int(row["label_is_fraud"]),
            exposure_minor=int(row["exposure_minor"]),
            review_cost_minor=int(row["review_cost_minor"]),
            review_minutes=int(row["review_minutes"]),
            amount_minor=int(row["exposure_minor"]),
            p_calibrated=float(row["signal"]),
        )
        for row in _corpus().filter(pl.Series(fold.test_mask)).to_dicts()
    ]

    with pytest.raises(FoldError, match="no recorded entity-disjoint answer"):
        _run_policies(
            ("score_threshold",),
            [fold],
            {fold.index: accounts},
            {fold.index: {}},
            {fold.index: len(accounts)},
            config,
            config.seed,
            None,
            {},
        )
