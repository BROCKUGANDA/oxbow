"""The scored window must open at the test start, and the embargo guard must still bite.

WHY THIS FILE EXISTS AND WHAT BROKE WITHOUT IT. On 2026-09-27 the first real per-account
corpus was handed to `oxbow backtest --corpus` and fold 0 refused with
"embargo gap is 0.00d but the embargo is 30d". The guard was right: ``Fold.test_mask`` opened
the scored window at the *training cutoff* rather than at the fold's own ``test_start_ts``, so
every row in the withheld 30-day band was scored as test data and the gap between the last fit
row and the first scored row collapsed to minutes. Nothing in the suite at the time asserted
WHERE the scored window opens — only that training stops at the cutoff — so the bug was
invisible until real bytes arrived.

These tests are written to be capable of failing in both directions:

* the mask assertion fails if anyone reopens the window at ``train_end_ts``;
* the harness assertion fails if the guard is ever softened, because it feeds the harness a
  fold that genuinely straddles the embargo and requires a ``FoldError``.

The timelines here are dense on purpose (six rows a day): with one row a day a band can be
empty and a wrong boundary passes vacuously, which is the failure mode
``test_p2_splits.py::test_embargo_band_is_withheld_from_training_and_validation`` names as
"a config field, not a guard".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

import polars as pl
import pytest

from oxbow.backtest.harness import assert_fold_discipline
from oxbow.backtest.interfaces import FoldError, HarnessFold
from oxbow.backtest.splits import SplitError, SplitPlan, build_walk_forward
from oxbow.config import find_repo_root
from oxbow.features.registry import FeatureRegistry, registry_from_config_dir

REPO_ROOT: Final = find_repo_root()
CONFIG_DIR: Final = REPO_ROOT / "config"
TS: Final = "as_of_ts"
DAY: Final = timedelta(days=1)
EPOCH: Final = datetime(2023, 1, 1, tzinfo=UTC)
ROWS_PER_DAY: Final = 6
DAYS: Final = 1600


@pytest.fixture(scope="module")
def registry() -> FeatureRegistry:
    return registry_from_config_dir(CONFIG_DIR)


@pytest.fixture(scope="module")
def dense_corpus() -> pl.DataFrame:
    """Rows every four hours over ~4.4 years, 500 accounts — dense enough to fill any band."""
    stamps = [
        EPOCH + timedelta(days=day, hours=hour)
        for day in range(DAYS)
        for hour in (2, 6, 11, 15, 19, 23)
    ]
    return pl.DataFrame(
        {
            TS: stamps,
            "account_key": [f"acct_{i % 500:04d}" for i in range(len(stamps))],
            "entity": [f"acct_{i % 500:04d}" for i in range(len(stamps))],
        }
    )


@pytest.fixture(scope="module")
def plan(registry: FeatureRegistry, dense_corpus: pl.DataFrame) -> SplitPlan:
    return build_walk_forward(
        dense_corpus.select([TS, "entity"]),
        registry=registry,
        config_dir=CONFIG_DIR,
        ts_column=TS,
    )


def _masks(frame: pl.DataFrame, fold: object) -> tuple[list[bool], list[bool], list[bool]]:
    def column(expr: pl.Expr) -> list[bool]:
        return [bool(v) for v in frame.select(expr.cast(pl.Boolean)).to_series().to_list()]

    return (
        column(fold.train_rows_mask(TS)),  # type: ignore[attr-defined]
        column(fold.validation_mask(TS)),  # type: ignore[attr-defined]
        column(fold.test_mask(TS)),  # type: ignore[attr-defined]
    )


def test_the_scored_window_opens_at_the_test_start_never_at_the_cutoff(
    plan: SplitPlan, dense_corpus: pl.DataFrame
) -> None:
    """The regression itself: no scored row may sit inside the fold's own withheld band.

    Asserted three ways, because each catches a different slip. (1) Zero test rows fall in
    ``(train_end_ts, test_start_ts)``. (2) The band is genuinely populated in this frame, so
    (1) cannot be vacuous. (3) The measured gap between the last fitting row and the first
    scored row is at least one embargo on EVERY fold — the number the harness compares.
    """
    stamps = dense_corpus.get_column(TS).to_list()
    embargo = timedelta(days=plan.embargo_days)
    for fold in plan.folds:
        train, validation, test = _masks(dense_corpus, fold)
        fit = [
            t
            for t, keep in zip(
                stamps, [a or b for a, b in zip(train, validation, strict=False)], strict=True
            )
            if keep
        ]
        scored = [t for t, keep in zip(stamps, test, strict=True) if keep]
        in_band = [
            t
            for t in scored
            if fold.train_end_ts < t < fold.test_start_ts  # type: ignore[union-attr]
        ]
        assert in_band == [], (
            f"{fold.fold_id}: {len(in_band)} scored row(s) sit inside the withheld band "
            f"{fold.train_end_ts}..{fold.test_start_ts}; the test window opened at the "  # type: ignore[union-attr]
            "training cutoff, which is the leakage the embargo exists to stop"
        )
        band_rows = [
            t
            for t in stamps
            if fold.train_end_ts < t < fold.test_start_ts  # type: ignore[union-attr]
        ]
        assert band_rows, f"{fold.fold_id}: empty band here, so the assertion above proves nothing"
        assert scored, f"{fold.fold_id}: nothing was scored"
        gap = min(scored) - max(fit)
        assert gap >= embargo, (
            f"{fold.fold_id}: last fit row {max(fit)} to first scored row {min(scored)} is "
            f"{gap}, short of the {plan.embargo_days}d embargo"
        )


def test_a_fold_that_scores_the_embargo_band_is_refused_by_the_harness(
    plan: SplitPlan, dense_corpus: pl.DataFrame
) -> None:
    """MUTATION PROOF: the guard still fires on a fold whose rows straddle the embargo.

    This feeds ``assert_fold_discipline`` the pre-fix predicate — fit rows up to the training
    cutoff, scored rows opening at that same cutoff — which is a genuine straddle: the first
    "test" row is hours, not 30 days, after the last fit row. If the check is ever loosened,
    widened or made to tolerate a small gap, this test goes red rather than going quiet.
    """
    fired: list[str] = []
    for fold in plan.folds:
        stamps = dense_corpus.get_column(TS).to_list()
        train = [t <= fold.train_end_ts for t in stamps]
        band_scoring_test = [
            fold.train_end_ts < t <= fold.test_end_ts
            for t in stamps  # type: ignore[union-attr]
        ]
        leaked = HarnessFold(
            index=fold.index,
            train_mask=tuple(train),
            validation_mask=tuple(False for _ in stamps),
            test_mask=tuple(band_scoring_test),
            embargo_days=plan.embargo_days,
        )
        with pytest.raises(FoldError, match="embargo gap") as exc:
            assert_fold_discipline(dense_corpus, leaked, as_of_column=TS)
        fired.append(str(exc.value))
    assert len(fired) == len(plan.folds), "a fold's straddle went unnoticed"
    assert "30" in fired[0], fired[0]


def test_a_straddle_measured_in_rows_not_masks_also_blocks(
    plan: SplitPlan, dense_corpus: pl.DataFrame
) -> None:
    """The guard's arithmetic is on timestamps, so a hand-built straddle must raise too.

    Ten rows a day apart: fit ends on day 6, scoring opens on day 8, so the honest gap is two
    days against a 30-day embargo. No mask predicate is involved, so this fails only if the
    check itself is loosened — and the message has to state the arithmetic it used.
    """
    stamps = [EPOCH + timedelta(days=day) for day in range(10)]
    corpus = pl.DataFrame(
        {TS: pl.Series(stamps, dtype=pl.Datetime(time_unit="us", time_zone="UTC"))}
    )
    fold = HarnessFold(
        index=99,
        train_mask=tuple(day <= 6 for day in range(10)),
        validation_mask=tuple(False for _ in stamps),
        test_mask=(
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
        ),
        embargo_days=plan.embargo_days,
    )
    assert sum(fold.train_mask) == 7 and sum(fold.test_mask) == 2
    with pytest.raises(FoldError, match="embargo gap is 2\\.00d but the embargo is 30d"):
        assert_fold_discipline(corpus, fold, as_of_column=TS)


def test_a_row_on_the_boundary_belongs_to_the_earlier_fold(
    plan: SplitPlan, dense_corpus: pl.DataFrame
) -> None:
    """``test_mask`` and ``fold_for`` cannot disagree about the instant they both name.

    The shipped ladder makes each fold's test start equal to the previous fold's test end, so
    the two windows would overlap at that instant unless one of them excluded it.
    """
    stamps = dense_corpus.get_column(TS).to_list()
    for fold in plan.folds:
        _, _, test = _masks(dense_corpus, fold)
        scored = {t for t, keep in zip(stamps, test, strict=True) if keep}
        assert (
            fold.test_start_ts not in scored
        ), f"{fold.fold_id}: the boundary instant was scored here and in the previous fold"
        owner = plan.fold_for(fold.test_start_ts)
        assert owner is None or owner.index != fold.index, (
            f"{fold.fold_id}: fold_for assigns its own test start to itself, which the mask "
            "contradicts"
        )


def test_the_recorded_window_moves_every_boundary_and_a_foreign_frame_is_refused(
    registry: FeatureRegistry, dense_corpus: pl.DataFrame
) -> None:
    """The plan resolves fractions on the window it is TOLD, and refuses a frame outside it.

    This is the second half of the 2026-09-27 defect: the backtest re-derived the observed
    window from the account-grain corpus, whose last row is days earlier than the event window
    the producing stage recorded, so all five folds' boundaries moved. The assertion is that
    passing the recorded window changes the boundaries in the expected direction, and that a
    window which does not contain the frame is a refusal rather than rows in no fold.
    """
    own = build_walk_forward(
        dense_corpus.select([TS, "entity"]),
        registry=registry,
        config_dir=CONFIG_DIR,
        ts_column=TS,
    )
    later_end = dense_corpus.get_column(TS).max() + timedelta(days=400)
    widened = build_walk_forward(
        dense_corpus.select([TS, "entity"]),
        registry=registry,
        config_dir=CONFIG_DIR,
        ts_column=TS,
        observed_window=(own.timeline_start, later_end),
    )
    assert widened.timeline_end == later_end
    for short, long_ in zip(own.folds, widened.folds, strict=True):
        assert long_.test_end_ts > short.test_end_ts, (
            f"{short.fold_id}: a longer recorded window did not move the boundary, so the "
            "fractions are not being resolved on it"
        )
    too_narrow = dense_corpus.get_column(TS).min() + timedelta(days=1)
    with pytest.raises(SplitError, match="outside the window"):
        build_walk_forward(
            dense_corpus.select([TS, "entity"]),
            registry=registry,
            config_dir=CONFIG_DIR,
            ts_column=TS,
            observed_window=(too_narrow, later_end),
        )
