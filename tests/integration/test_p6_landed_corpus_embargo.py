"""The embargo guard against the first real per-account corpus, not against fixtures.

Added on 2026-09-27, the day ``oxbow backtest --corpus`` read real bytes for the first time
and fold 0 refused with "embargo gap is 0.00d but the embargo is 30d". Every P6 claim before
that came from ``--demo-fakes``, i.e. the harness checking itself, so nothing in the suite had
ever compared a fold boundary with bytes a stage had actually landed.

``out/`` is gitignored, so this file skips with a named reason where no corpus is landed
rather than passing vacuously. Point it at a specific corpus with
``OXBOW_BACKTEST_CORPUS=path/to/backtest_corpus.parquet``; with no env var it takes the
newest landed one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import polars as pl
import pytest

from oxbow.backtest.fold_provider import SplitsFoldProvider
from oxbow.backtest.harness import assert_fold_discipline, validate_corpus
from oxbow.backtest.interfaces import COL_ACCOUNT_KEY, COL_AS_OF_TS
from oxbow.backtest.run import _fold_column_agreement, _recorded_observed_window, _resolve_repo
from oxbow.backtest.splits import SplitPlan, build_walk_forward
from oxbow.config import find_repo_root
from oxbow.scoring.config import load_feature_registry

REPO_ROOT: Final = find_repo_root()
FLOAT_DTYPES: Final = {pl.Float32, pl.Float64}


def _landed_corpus() -> Path | None:
    """The corpus this checkout actually has, or None when there are no bytes to read."""
    override = os.environ.get("OXBOW_BACKTEST_CORPUS")
    if override:
        path = Path(override).resolve()
        return path if path.is_file() else None
    root = REPO_ROOT / "out" / "score"
    if not root.is_dir():
        return None
    candidates = sorted(
        (directory / "backtest_corpus.parquet" for directory in root.iterdir() if directory.is_dir()),
        key=lambda path: path.stat().st_mtime if path.is_file() else 0,
    )
    return next((path for path in reversed(candidates) if path.is_file()), None)


CORPUS: Final[Path | None] = _landed_corpus()

pytestmark = pytest.mark.skipif(
    CORPUS is None,
    reason="no per-account corpus is landed under out/score/ (out/ is gitignored); set "
    "OXBOW_BACKTEST_CORPUS to a real backtest_corpus.parquet to run this against bytes",
)


@pytest.fixture(scope="module")
def corpus() -> pl.DataFrame:
    assert CORPUS is not None
    return pl.read_parquet(CORPUS)


@pytest.fixture(scope="module")
def plan_and_corpus(corpus: pl.DataFrame) -> tuple[SplitPlan, pl.DataFrame]:
    assert CORPUS is not None
    spec_hash = validate_corpus(corpus)
    repo = _resolve_repo(CORPUS)
    registry = load_feature_registry(repo)
    recorded = _recorded_observed_window(CORPUS, spec_hash=spec_hash, repo=repo)
    timeline = corpus.select(pl.col(COL_AS_OF_TS), pl.col(COL_ACCOUNT_KEY).alias("entity"))
    plan = build_walk_forward(
        timeline,
        registry=registry,
        config_dir=repo / "config",
        ts_column=COL_AS_OF_TS,
        observed_window=None if recorded is None else (recorded[0], recorded[1]),
    )
    return plan, corpus


def test_no_scored_row_sits_in_a_withheld_band(plan_and_corpus: tuple[SplitPlan, pl.DataFrame]) -> None:
    """The defect, stated as bytes: a fold's scored set must not contain its own embargo band.

    Also checks the scored set agrees row-for-row with ``SplitPlan.fold_for``, so the mask and
    the per-row fold id the feature layer stamps cannot drift apart again.
    """
    plan, corpus = plan_and_corpus
    stamps = corpus.get_column(COL_AS_OF_TS).to_list()
    for fold, harness in zip(
        plan.folds, SplitsFoldProvider(plan, as_of_column=COL_AS_OF_TS).folds(corpus), strict=True
    ):
        scored = [stamps[i] for i, keep in enumerate(harness.test_mask) if keep]
        assert scored, f"fold {fold.index}: empty scored window"
        in_band = [t for t in scored if fold.train_end_ts < t < fold.test_start_ts]
        assert not in_band, (
            f"fold {fold.index}: {len(in_band)} scored rows lie inside the withheld band "
            f"{fold.train_end_ts}..{fold.test_start_ts}"
        )
        on_or_after_start = all(t > fold.test_start_ts for t in scored)
        assert on_or_after_start, f"fold {fold.index}: scored rows open before the test start"
        for moment in scored:
            owner = plan.fold_for(moment)
            assert owner is not None and owner.index == fold.index, (
                f"fold {fold.index} scored {moment}, which fold_for assigns to "
                f"{None if owner is None else owner.fold_id}"
            )


def test_the_landed_fold_column_matches_the_plan_it_was_scored_under(
    plan_and_corpus: tuple[SplitPlan, pl.DataFrame]
) -> None:
    """The corpus records which fold's scoring cutoff built each row; the plan must reproduce it.

    This is the check that would have caught the backtest re-deriving the observed window from
    the account-grain corpus instead of the event window the score stage recorded: on
    2026-09-27 that drift moved all five folds' boundaries and 0 of 79,998 rows kept their fold.
    """
    plan, corpus = plan_and_corpus
    report = _fold_column_agreement(corpus, plan)
    if not report.get("available"):
        pytest.skip(f"the landed corpus carries no fold column to check: {report.get('note')}")
    assert report["agreement_share"] == 1.0, report


def test_every_fold_clears_the_embargo_it_was_configured_with(
    plan_and_corpus: tuple[SplitPlan, pl.DataFrame]
) -> None:
    """The guard, on real bytes: fit-to-score gap >= the embargo, which equals the lookback."""
    plan, corpus = plan_and_corpus
    registry = load_feature_registry(_resolve_repo(CORPUS))
    assert plan.embargo_days == registry.max_lookback_days
    stamps = corpus.get_column(COL_AS_OF_TS).to_list()
    gaps: list[float] = []
    for harness in SplitsFoldProvider(plan, as_of_column=COL_AS_OF_TS).folds(corpus):
        assert_fold_discipline(corpus, harness, as_of_column=COL_AS_OF_TS)
        fit = [
            stamps[i]
            for i in range(len(stamps))
            if harness.train_mask[i] or harness.validation_mask[i]
        ]
        scored = [stamps[i] for i, keep in enumerate(harness.test_mask) if keep]
        gaps.append(round((min(scored) - max(fit)).total_seconds() / 86_400, 4))
    assert len(gaps) == len(plan.folds) == 5
    assert all(gap >= plan.embargo_days for gap in gaps), gaps


def test_money_is_integer_minor_units_and_the_label_is_not_a_float(corpus: pl.DataFrame) -> None:
    """DEV-005 on landed bytes: no amount ever became a float on the way to disk."""
    floats = sorted(
        name for name, dtype in corpus.schema.items() if name.endswith("_minor") and dtype in FLOAT_DTYPES
    )
    assert not floats, f"money columns stored as floats: {floats}"
    assert corpus.get_column("label_is_fraud").dtype in {pl.Int8, pl.Int16, pl.Int32, pl.Int64}
    assert corpus.get_column("exposure_minor").min() is not None
