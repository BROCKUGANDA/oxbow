"""The first real per-account corpus, walked forward: what the leakage guard demanded.

Added the day the score stage landed its first real bytes
(``out/score/<run_id>/backtest_corpus.parquet``, 79,998 rows x 75 features, five folds,
108 positives) and ``oxbow backtest --corpus`` refused fold 0 with
"embargo gap is 0.00d but the embargo is 30d". Every P6 claim before that came from
``--demo-fakes``, i.e. the harness checking itself, so this is the first test in the suite
that reads corpus bytes rather than fixtures.

It skips with a named reason when no corpus is landed: ``out/`` is gitignored, so a fresh
checkout has no bytes here, and a green run that quietly measured nothing is the failure mode
this repo already rejects. Where the bytes exist, the assertions are the ones the 2026-09-27
incident turned on: the plan must resolve on the window the producing run recorded, no scored
row may sit in a withheld band, and the measured gap on every fold must be at least the
embargo the features depend on.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Final

import polars as pl
import pytest

from oxbow.backtest.fold_provider import SplitsFoldProvider
from oxbow.backtest.harness import assert_fold_discipline, validate_corpus
from oxbow.backtest.interfaces import COL_ACCOUNT_KEY, COL_AS_OF_TS, COL_LABEL
from oxbow.backtest.run import _fold_column_agreement, _recorded_observed_window, _resolve_repo
from oxbow.backtest.splits import build_walk_forward
from oxbow.config import find_repo_root
from oxbow.scoring.config import load_feature_registry

REPO_ROOT: Final = find_repo_root()
MONEY_SUFFIXES: Final = ("_minor",)
DAY: Final = timedelta(days=1)


def _landed_corpora() -> list[Path]:
    root = REPO_ROOT / "out" / "score"
    if not root.is_dir():
        return []
    return sorted((path / "backtest_corpus.parquet") for path in root.iterdir() if path.is_dir())


CORPORA: Final[list[Path]] = _landed_corpora()
pytestmark = pytest.mark.skipif(
    not CORPORA,
    reason="no per-account corpus is landed under out/score/ (out/ is gitignored), so there "
    "are no real bytes to walk forward here; run `uv run oxbow score` first",
)


@pytest.mark.parametrize("corpus_path", CORPORA, ids=lambda p: str(p.parent.name))
def test_every_landed_fold_clears_the_embargo_it_was_configured_with(corpus_path: Path) -> None:
    """Fit-to-score gap >= the embargo on every fold, or this run repeats the incident."""
    corpus = pl.read_parquet(corpus_path)
    spec_hash = validate_corpus(corpus)
    repo = _resolve_repo(corpus_path)
    registry = load_feature_registry(repo)
    timeline = corpus.select(pl.col(COL_AS_OF_TS), pl.col(COL_ACCOUNT_KEY).alias("entity"))
    recorded = _recorded_observed_window(corpus_path, spec_hash=spec_hash, repo=repo)
    plan = build_walk_forward(
        timeline,
        registry=registry,
        config_dir=repo / "config",
        ts_column=COL_AS_OF_TS,
        observed_window=None if recorded is None else (recorded[0], recorded[1]),
    )
    assert plan.embargo_days == registry.max_lookback_days, (
        "the embargo stopped equalling the longest feature lookback, which is the number the "
        "gap below is supposed to clear"
    )
    folds = SplitsFoldProvider(plan, as_of_column=COL_AS_OF_TS).folds(corpus)
    stamps = corpus.get_column(COL_AS_OF_TS).to_list()
    measured: list[tuple[int, float, int, int]] = []
    for fold in folds:
        assert_fold_discipline(corpus, fold, as_of_column=COL_AS_OF_TS)
        fit = [i for i in range(len(stamps)) if fold.train_mask[i] or fold.validation_mask[i]]
        scored = [i for i in range(len(stamps)) if fold.test_mask[i]]
        assert scored, f"fold {fold.index}: nothing to score"
        gap = (
            min(stamps[i] for i in scored) - max(stamps[i] for i in fit)
        ).total_seconds() / 86_400
        assert gap >= plan.embargo_days, (
            f"fold {fold.index}: fit-to-score gap {gap:.4f}d is under the {plan.embargo_days}d "
            "embargo the 30-day features need"
        )
        plan_fold = plan.folds[fold.index]
        in_band = sum(
            1 for i in scored if plan_fold.train_end_ts < stamps[i] < plan_fold.test_start_ts
        )
        assert in_band == 0, f"fold {fold.index}: {in_band} scored rows sit in the withheld band"
        measured.append((fold.index, round(gap, 4), len(fit), len(scored)))
    assert len(measured) == len(plan.folds)

    if recorded is not None:
        check = _fold_column_agreement(corpus, plan)
        assert check["available"] and check["agreement_share"] == 1.0, (
            f"the plan disagrees with the corpus's own fold column: {check}; the fractions were "
            "resolved on a window other than the one that produced these rows"
        )


@pytest.mark.parametrize("corpus_path", CORPORA, ids=lambda p: str(p.parent.name))
def test_money_landed_as_integer_minor_units(corpus_path: Path) -> None:
    """No float ever touches a *_minor column (DEV-005)."""
    corpus = pl.read_parquet(corpus_path)
    floats = [
        name
        for name, dtype in corpus.schema.items()
        if name.endswith(MONEY_SUFFIXES) and dtype in {pl.Float32, pl.Float64}
    ]
    assert floats == [], f"money columns arrived as floats: {floats}"
    assert corpus.get_column(COL_LABEL).dtype in {pl.Int8, pl.Int16, pl.Int32, pl.Int64}


@pytest.mark.parametrize("corpus_path", CORPORA, ids=lambda p: str(p.parent.name))
def test_the_corpus_is_one_feature_spec_over_one_ordered_timeline(corpus_path: Path) -> None:
    """One spec hash, and the total order the determinism claim rests on."""
    corpus = pl.read_parquet(corpus_path)
    assert corpus.get_column("feature_spec_hash").n_unique() == 1
    ordered = corpus.sort([COL_AS_OF_TS, COL_ACCOUNT_KEY])
    assert ordered.equals(corpus), "the corpus is not in (as_of_ts, account_key) order"
    pairs = corpus.select([COL_ACCOUNT_KEY, COL_AS_OF_TS])
    assert (
        pairs.height == pairs.unique().height
    ), "an account appears twice at the same as-of, so a fold's scored set is ambiguous"
    manifest = corpus_path.parent / "score_run_manifest.json"
    if manifest.is_file():
        report = json.loads(manifest.read_text(encoding="utf-8"))
        assert report["corpus_rows"] == corpus.height
        assert len(report["spec_hash"]) == 64
