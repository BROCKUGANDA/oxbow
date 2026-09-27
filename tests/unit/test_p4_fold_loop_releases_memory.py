"""The fold loop must release one fold before it fits the next (memory-retention guard).

THE MEASURED FAILURE THIS PINS: on 2026-09-27 the first full scored run landed fold 0 and
then died on folds 1-4 with ``LightGBMError: bad allocation`` on a 79,998-row corpus -- a
per-fold *accumulation*, not a corpus too big for the machine. The fold loop rebound its
``run`` variable only when the next ``FoldModelRunner.run_fold`` returned, so fold *k*'s
``FoldRun`` (its full train+validation+test scored frame with the per-row SHAP JSON, the
LightGBM booster, the Isolation Forest) stayed resident *while* fold *k+1* fitted, doubling
the peak. :func:`oxbow.cli.run_walk_forward_folds` scopes each fold's fit so its ``FoldRun``
dies before the next fold starts.

WHY THE ASSERTION IS OBJECT-COUNTED, NOT RSS: per-fold RSS on a shared, ~2 GB box is noisy
and this test must not flap. The invariant is exact and cheap -- no fold may *enter* a fit
while an earlier fold's fitted-model objects are still alive. A live ``lgb.Booster`` /
``GbmBundle`` / ``AnomalyBundle`` at the top of a fold means the previous fold was never
released, and each fold's ``FoldRun`` keeps its whole scored frame (every row's SHAP JSON)
in memory until it dies. Counting those retained objects is counting retained memory by its
handle: a leak grows the set monotonically fold after fold, the release keeps it pinned at
zero between fits.

DETERMINISM: nothing here compares a wall clock or iterates a set for order. It asserts the
loop produced scored rows and freed what it should.
"""

from __future__ import annotations

import dataclasses
import gc
from pathlib import Path
from typing import Final

import polars as pl

from oxbow.cli import run_walk_forward_folds
from oxbow.config import find_repo_root
from oxbow.models.anomaly import AnomalyBundle
from oxbow.models.config import load_model_config, load_split_config
from oxbow.models.gbm import GbmBundle
from oxbow.models.run import FoldModelRunner
from oxbow.models.scorer import FrameRuleHitProvider
from oxbow.scoring.config import load_feature_registry, load_scorecard_config
from oxbow.scoring.frame import (
    COL_ACCOUNT_KEY,
    COL_FOLD,
    COL_ROLE,
    ROLE_TEST,
    ROLE_TRAIN,
    ROLE_VALIDATION,
    PROVENANCE_GENERATED,
    build_training_frame,
)
from oxbow.scoring.generated import default_spec, generate_training_frame

REPO_ROOT: Path = find_repo_root()
N_ACCOUNTS: Final = 220
BASE_RATE: Final = 0.30
FOLD_INDICES: Final = (2, 3, 4)


def _fold_masks(frame: pl.DataFrame, fold_index: int) -> tuple[list[bool], list[bool], list[bool]]:
    """Expanding-window masks from the frame's own ``fold`` column (same rule as the CLI).

    ``train`` is every fold strictly before the validation window, ``validation`` the fold
    right before, ``test`` this fold -- the ladder :meth:`FoldModelRunner.run_fold` fits and
    scores one step of.
    """
    folds = frame.get_column(COL_FOLD)
    train = (folds <= fold_index - 2).to_list()
    validation = (folds == fold_index - 1).to_list()
    test = (folds == fold_index).to_list()
    return train, validation, test


def _live_fold_models() -> int:
    """Count live fitted-model objects the fold loop must not carry across iterations."""
    n = 0
    for obj in gc.get_objects():
        if isinstance(obj, GbmBundle | AnomalyBundle):
            n += 1
    return n


def test_fold_loop_releases_each_fold_before_fitting_the_next() -> None:
    import lightgbm as lgb

    registry = load_feature_registry(REPO_ROOT)
    scorecard_cfg = load_scorecard_config(REPO_ROOT)
    model_cfg = load_model_config(REPO_ROOT)
    split_cfg = load_split_config(REPO_ROOT)
    categorical = tuple(scorecard_cfg.binning.categorical_features)

    # A test-only shrink of the boosting budget so three folds finish in seconds, not the
    # full 2,000-round sweep (whose SHAP internal model over ~2,000 trees is what times this
    # box out). This changes nothing about the retention being guarded -- the fold still fits
    # a real booster + Isolation Forest + per-row SHAP and reaches ``full_model_stack`` -- it
    # only makes the vehicle cheap. The production run keeps model.yaml's n_estimators.
    model_cfg = dataclasses.replace(
        model_cfg,
        gbm=dataclasses.replace(model_cfg.gbm, n_estimators=90, early_stopping_rounds=25),
    )

    frame = generate_training_frame(
        registry, categorical, spec=default_spec(n_accounts=N_ACCOUNTS, base_rate=BASE_RATE)
    )
    training = build_training_frame(
        frame,
        registry,
        categorical,
        PROVENANCE_GENERATED,
        split_cfg.validation_fraction_of_train,
        split_cfg.n_folds,
    )
    runner = FoldModelRunner(
        model_cfg=model_cfg,
        scorecard_cfg=scorecard_cfg,
        split_cfg=split_cfg,
        feature_registry=registry,
        rules_provider=FrameRuleHitProvider(),
        provenance=PROVENANCE_GENERATED,
        trial_budget=1,
        explain=True,  # the run under failure persisted SHAP per row; exercise that path
        root=REPO_ROOT,
    )
    fold_pairs = [(idx, *_fold_masks(frame, idx)) for idx in FOLD_INDICES]

    # Retained fold-model objects observed at the top of each fold's fit. A loop that frees
    # each fold keeps this at zero; one that holds the previous FoldRun makes it climb.
    models_at_fit_start: list[int] = []

    def observe(event: str, fold_index: int) -> None:
        del event, fold_index
        gc.collect()
        models_at_fit_start.append(_live_fold_models())

    gc.collect()
    before = _live_fold_models()
    scored_frames, runs_summary = run_walk_forward_folds(
        runner,
        training,
        frame,
        fold_pairs,
        embargo_days=split_cfg.embargo_days,
        observe=observe,
    )

    landed = [entry for entry in runs_summary if "skipped" not in entry]
    assert scored_frames, "the loop produced no scored rows -- an empty pass proves nothing"
    assert any(entry.get("mode") == "full_model_stack" for entry in landed), (
        "no fold fitted the full model stack, so no booster/SHAP path was exercised and the "
        "retention guard would pass vacuously; widen the fixture"
    )

    # THE GUARD: a fold may not enter its fit with a prior fold's models still resident, and
    # the retained-model count must not grow across folds (it is pinned at zero).
    assert before == 0
    assert len(models_at_fit_start) == len(FOLD_INDICES)
    assert all(count == 0 for count in models_at_fit_start), (
        f"a fold entered its fit while an earlier fold's model objects were still alive: "
        f"live GbmBundle/AnomalyBundle at fit start per fold = {models_at_fit_start}. The "
        "loop retained what it should free -- this is the bad-allocation accumulation."
    )
    assert not (
        models_at_fit_start[0] < models_at_fit_start[1] < models_at_fit_start[2]
    ), f"retained fold models grew monotonically across folds: {models_at_fit_start}"

    # And no booster may survive the whole run: after the loop every fold's FoldRun is gone.
    live_boosters = sum(1 for o in gc.get_objects() if isinstance(o, lgb.Booster))
    assert live_boosters == 0, f"{live_boosters} LightGBM booster(s) survived the fold loop"

    # Output sanity: rows are scored and carry the test role.
    total_rows = sum(df.height for df in scored_frames)
    assert total_rows > 0
    assert all((df.get_column(COL_ROLE) == ROLE_TEST).all() for df in scored_frames)
    assert all(COL_ACCOUNT_KEY in df.columns for df in scored_frames)
    # Silence lint on unused role imports that document the fixture contract.
    assert ROLE_TRAIN and ROLE_VALIDATION and ROLE_TEST
