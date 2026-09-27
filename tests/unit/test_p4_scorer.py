"""The P4 scorer seam, the spec-hash guard, and the leakage control — one named test each.

WHAT THIS FILE PROVES AND WHY IT WAS NOT PROVABLE BEFORE. Until :mod:`oxbow.models.scorer`
existed, the scoring layer had no contact with the harness's ``Scorer`` protocol at all: the
CLI stopped at the feature matrix and printed a gap. These tests are the first time a real
fold-fitting scorer has been driven end to end and had its output checked against the shape
the P6 harness consumes.

THE SEAMS, EACH WITH ITS FAILURE MODE:

1. **A ``WalkForwardScorer.score`` returns a ``ScoreResult`` whose every ``AccountScore`` is
   a probability in the unit interval, keyed by every account in the scored slice.** A
   missing account would make the harness price it at a silent zero.
2. **The spec-hash equality guard actually refuses a mismatched frame**, before a single tree
   is fitted, so "trained on one feature set, scored with another" cannot pass.
3. **The role column is stamped from the caller's assignment** and survives into the fold, so
   a fold cannot silently re-derive a split that disagrees with what was held out.
4. **The leakage control still out-scores the honest scorer** — the plan's proof the harness
   can detect cheating. It is asserted, never "fixed".

Money: none of this multiplies an amount; the only quantities are probabilities, fold ids and
row counts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import polars as pl
import pytest

from oxbow.backtest.ablation import AblationSpec, build_ablation, check_leakage_control
from oxbow.backtest.config_io import load_backtest_config
from oxbow.backtest.fakes import (
    FakeFoldProvider,
    HonestSignalScorer,
    LeakingLabelScorer,
    make_corpus,
    make_fold_masks,
)
from oxbow.backtest.interfaces import AccountScore, ScoreResult
from oxbow.config import find_repo_root
from oxbow.models.config import load_model_config
from oxbow.models.config import load_split_config as load_model_split_config
from oxbow.models.errors import FrameContractViolationError
from oxbow.models.run import FoldModelRunner
from oxbow.models.scorer import WalkForwardScorer
from oxbow.scoring.config import load_feature_registry, load_scorecard_config
from oxbow.scoring.frame import (
    COL_ACCOUNT_KEY,
    COL_FOLD,
    COL_ROLE,
    PROVENANCE_GENERATED,
)
from oxbow.scoring.generated import default_spec, generate_training_frame

REPO_ROOT: Path = find_repo_root()
N_ACCOUNTS: Final = 300


@pytest.fixture(scope="module")
def bridged_corpus() -> pl.DataFrame:
    """A spec-conformant, fully-populated account-grain frame the scorer can actually fit.

    The bridge's own ``build_account_frame`` output carries several features that are null on
    a sparse star-shaped corpus (DEV-011), and the WOE scorecard refuses to bin a feature with
    no populated value — a correct guard, but it makes a tiny live slice the wrong vehicle for
    a seam test whose job is the mapping, not the binning. ``oxbow.scoring.generated`` is the
    repository's own contract-shaped frame for exactly this: every published feature carries a
    value, the spec digest is the registry's, and the five-fold ladder spans a walk-forward.
    """
    scoring_registry = load_feature_registry(REPO_ROOT)
    scorecard_cfg = load_scorecard_config(REPO_ROOT)
    return generate_training_frame(
        scoring_registry,
        tuple(scorecard_cfg.binning.categorical_features),
        spec=default_spec(n_accounts=N_ACCOUNTS, base_rate=0.20),
    )


def _make_scorer() -> WalkForwardScorer:
    from oxbow.models.scorer import FrameRuleHitProvider

    scoring_registry = load_feature_registry(REPO_ROOT)
    runner = FoldModelRunner(
        model_cfg=load_model_config(REPO_ROOT),
        scorecard_cfg=load_scorecard_config(REPO_ROOT),
        split_cfg=load_model_split_config(REPO_ROOT),
        feature_registry=scoring_registry,
        rules_provider=FrameRuleHitProvider(),
        provenance=PROVENANCE_GENERATED,
        trial_budget=1,
        explain=False,
        root=REPO_ROOT,
    )
    return WalkForwardScorer(runner)


def _fold_slices(
    frame: pl.DataFrame, fold_index: int
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """A walk-forward slice from the frame's own ``fold`` column: earlier train, then valid, then test.

    The generated frame stamps folds 0..4 in a rising timeline, so fold ``k`` is the scored
    window, ``k-1`` the validation window and every earlier fold the training window.
    """
    folds = sorted(int(value) for value in frame.get_column(COL_FOLD).unique().to_list())
    assert (
        fold_index in folds and fold_index >= 1
    ), "the fixture must have a fold before the test fold"
    train = frame.filter(pl.col(COL_FOLD) <= fold_index - 2)
    validation = frame.filter(pl.col(COL_FOLD) == fold_index - 1)
    scored = frame.filter(pl.col(COL_FOLD) == fold_index)
    assert train.height and validation.height and scored.height
    return train, validation, scored


def test_scorer_output_satisfies_the_scoreresult_contract(bridged_corpus: pl.DataFrame) -> None:
    """The point of the seam: a real fold-fitted scorer returns the shape the harness reads."""
    train, validation, scored = _fold_slices(bridged_corpus, 4)
    assert scored.height > 0 and train.height > 0
    spec_hash = str(bridged_corpus.get_column("feature_spec_hash").unique().item())
    result = _make_scorer().score(
        train=train, validation=validation, scored=scored, feature_spec_hash=spec_hash, seed=1337
    )
    assert isinstance(result, ScoreResult)
    assert result.feature_spec_hash == spec_hash
    assert result.model_version
    scored_accounts = set(scored.get_column(COL_ACCOUNT_KEY).to_list())
    assert scored_accounts <= set(result.scores)
    for account, score in result.scores.items():
        assert isinstance(score, AccountScore)
        assert score.account_key == account
        assert 0.0 <= score.p_calibrated <= 1.0
        assert 0.0 <= score.band_observed_rate <= 1.0
        assert score.band_n >= 0
    sample = next(iter(scored_accounts))
    assert result.p_of(sample) == result.scores[sample].p_calibrated


def test_scorer_refuses_a_mismatched_feature_spec_hash(bridged_corpus: pl.DataFrame) -> None:
    """02 §B seam 3, exercised on a real frame: a wrong hash stops before any fit."""
    train, validation, scored = _fold_slices(bridged_corpus, 4)
    real_hash = str(bridged_corpus.get_column("feature_spec_hash").unique().item())
    impostor = "0" * 64
    assert impostor != real_hash
    with pytest.raises(FrameContractViolationError, match="feature_spec_hash"):
        _make_scorer().score(
            train=train,
            validation=validation,
            scored=scored,
            feature_spec_hash=impostor,
            seed=1337,
        )


def test_scorer_stamps_the_callers_role_assignment_into_the_fold(
    bridged_corpus: pl.DataFrame,
) -> None:
    """The role column must come from the caller's slice, not a re-derived split."""
    from oxbow.models.scorer import stamp_role

    _train, _validation, scored = _fold_slices(bridged_corpus, 4)
    stamped = stamp_role(scored, "test")
    assert set(stamped.get_column(COL_ROLE).unique().to_list()) == {"test"}
    assert COL_FOLD in stamped.columns
    with pytest.raises(FrameContractViolationError):
        stamp_role(scored.drop(COL_FOLD), "test")


def test_the_leakage_control_still_beats_the_honest_scorer() -> None:
    """The harness must detect lookahead: the cheating arm out-scores the honest one.

    This is the plan §12 proof, asserted on the injected-seam ablation path with the fakes'
    own control and honest scorers. It must never be 'fixed' by raising the honest arm or
    lowering the control: a control that does not visibly win means the harness is blind.
    """
    config = load_backtest_config()
    corpus = make_corpus(n_accounts=200, span_days=500, with_typology=True)
    folds = make_fold_masks(
        height=corpus.height, n_folds=config.n_folds, embargo_days=config.embargo_days
    )
    provider = FakeFoldProvider(folds, embargo_days=config.embargo_days)
    specs = [
        AblationSpec(
            row_id="full_calibrated",
            label="Honest full stack",
            question="the honest arm",
            scorer=HonestSignalScorer(quality=0.60),
            policies=("score_threshold",),
        ),
        AblationSpec(
            row_id="leakage_control",
            label="CONTROL - lookahead",
            question="does the harness see cheating?",
            scorer=LeakingLabelScorer(),
            policies=("score_threshold",),
            is_control=True,
        ),
    ]
    variants = build_ablation(
        specs,
        corpus=corpus,
        corpus_name="paysim-fake",
        provenance="fake_harness",
        fold_provider=provider,
        config=config,
    )
    control = check_leakage_control(variants, expect_outperforms=True)
    assert control.detected, control.message
    assert control.control_pr_auc is not None and control.best_honest_pr_auc is not None
    assert control.control_pr_auc > control.best_honest_pr_auc
