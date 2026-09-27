"""The declared categoricals must reach LightGBM as dense category INDICES (memory gate).

THE MEASURED FAILURE THIS PINS
------------------------------
Run ``01M3FZ2GC3J71AYT1QEKPWEDKJ`` landed fold 0 and skipped folds 1-4 with
``LightGBMError: bad allocation``, and ``oxbow backtest --corpus`` on the same corpus died the
same way in a fresh process. The corpus is 79,998 x 85 and the fold-1 fit matrix is 23.8 MB,
so nothing in ``config/model.yaml`` (``max_bin: 255``, ``num_leaves: 31``, ``num_threads: 1``)
asks for gigabytes. What did was the CATEGORY CODING: ``config/scorecard.yaml`` declares
``binning.categorical_features = [local_hour_code, txn_type_code, graph_community_id]``, those
three columns are *already* dense integer codes (0..23, 0..5, 0..2498) from the features
layer, and :func:`oxbow.models.inputs.feature_matrix` ran every declared categorical -- integer
or not -- through P4a's FNV-1a string encoder, turning them into 2.42e9 / 0.92e9 / 4.26e9
values and handing those to ``lightgbm.Dataset(categorical_feature=...)`` as category indices.
LightGBM sizes its per-category structures by the LARGEST index, not by the category count.
Measured 2026-09-27 with ``tests/unit/_scratch_fold1_repro.py`` on this host: the same
fold-1 frame, same 75 features, same params, same seed --

    prefix  (FNV-magnitude codes named as category indices): 900 s and still fitting; the
             identical run earlier reached 7,339 MB RSS (peak working set 8,470 MB) and then
             raised LightGBMError: bad allocation.
    postfix (the features layer's own dense codes):          OK in 3.9 s, peak RSS 484 MB.

LightGBM said the same thing itself: ``[Warning] Met categorical feature which contains sparse
values. Consider renumbering to consecutive integers started from zero`` -- 486 negative-value
warnings and 3 sparse-value warnings in one fold.

WHAT THE GATE IS
----------------
Two branches, split by dtype: an integer categorical column is a category index and passes
through untouched; a string categorical column has no numeric code and stays on the FNV-1a
encoder, which is a permutation and therefore coded NUMERICALLY -- it is not offered to
``categorical_feature``. A code magnitude above ``MAX_CATEGORY_INDEX`` is refused by name
before a Dataset is built, so the multi-gigabyte allocation is unreachable through the
production path rather than merely unlikely. The ceiling is a guard, not a knob; the tests
below fail if it is widened to let a hash through.

DETERMINISM: nothing here reads a wall clock for a value, and the coding is a pure function of
the column's own integers, so two runs on the same frame produce the same matrix.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from oxbow.config import find_repo_root
from oxbow.models.config import load_model_config
from oxbow.models.errors import FrameContractViolationError
from oxbow.models.gbm import fit_gbm
from oxbow.models.inputs import MAX_CATEGORY_INDEX, categorical_for_lightgbm, feature_matrix
from oxbow.scoring.frame import COL_LABEL

REPO_ROOT: Path = find_repo_root()

#: The three columns ``config/scorecard.yaml`` names, with the code magnitudes measured in the
#: landed corpus's features layer (``local_hour_code`` 0..23, ``txn_type_code`` 0..5,
#: ``graph_community_id`` 0..2498) and the FNV-1a magnitudes the defect produced instead.
DENSE_HOUR_CODES = list(range(24)) * 8
DENSE_COMMUNITY_IDS = list(range(0, 2498, 4))[:48] * 4
FNV_MAGNITUDE = 4_255_337_285  # graph_community_id's measured hashed maximum


def _frame(**columns: pl.Series) -> pl.DataFrame:
    return pl.DataFrame(columns)


def _labelled(rows: int, positives: int) -> pl.Series:
    labels = np.zeros(rows, dtype=np.int8)
    labels[np.linspace(0, rows - 1, positives, dtype=int)] = 1
    return pl.Series(COL_LABEL, labels.tolist(), dtype=pl.Int8)


def test_integer_categorical_codes_pass_through_unhashed() -> None:
    """An already-coded integer column must reach the matrix as the code, not as a hash.

    This is the whole defect. Before the fix this line returned 2.4e9-magnitude FNV values for
    ``local_hour_code``; LightGBM then built per-category structures to that magnitude.
    """
    frame = _frame(
        local_hour_code=pl.Series("local_hour_code", DENSE_HOUR_CODES, dtype=pl.Int32),
        graph_community_id=pl.Series("graph_community_id", DENSE_COMMUNITY_IDS, dtype=pl.Int64),
        activity=pl.Series("activity", [float(i % 17) for i in range(len(DENSE_HOUR_CODES))]),
        **{COL_LABEL: _labelled(len(DENSE_HOUR_CODES), 6)},
    )
    matrix = feature_matrix(
        frame,
        ("local_hour_code", "graph_community_id", "activity"),
        ("local_hour_code", "graph_community_id"),
    )
    hours = matrix[:, 0]
    communities = matrix[:, 1]
    assert hours.max() <= 23.0, f"hour codes were re-encoded, got max {hours.max()}"
    assert communities.max() <= 2498.0, f"community codes were re-encoded, max {communities.max()}"
    assert set(np.unique(hours)) == {float(v) for v in range(24)}, (
        "the coding must be the column's own 24 hour codes: anything else and the tree splits "
        "on a feature space the features layer never published"
    )


def test_string_categorical_stays_on_the_shared_hash_encoder() -> None:
    """A string category is still hashed with P4a's encoder, so GBM and scorecard agree."""
    from oxbow.scoring.frame import _stable_category_codes

    frame = _frame(
        channel=pl.Series("channel", ["a", "b", "c", "a", "b", "c"] * 8, dtype=pl.String),
        **{COL_LABEL: _labelled(48, 6)},
    )
    matrix = feature_matrix(frame, ("channel",), ("channel",))
    assert np.array_equal(matrix[:, 0], _stable_category_codes(frame.get_column("channel")))
    assert matrix[:, 0].max() > MAX_CATEGORY_INDEX, (
        "the string encoder is still a hash; if this ever stops being true the numeric branch "
        "and the categorical branch have silently merged"
    )


def test_a_hashed_string_is_never_named_to_lightgbm_as_a_category_index() -> None:
    """The admissibility filter, which is what makes the multi-GB allocation unreachable."""
    rows = len(DENSE_HOUR_CODES)
    frame = _frame(
        channel=pl.Series(
            "channel", [c for c in ("a", "b", "c") for _ in range(rows // 3)], dtype=pl.String
        ),
        local_hour_code=pl.Series("local_hour_code", DENSE_HOUR_CODES, dtype=pl.Int32),
        **{COL_LABEL: _labelled(rows, 6)},
    )
    admissible = categorical_for_lightgbm(
        frame, ("channel", "local_hour_code"), ("channel", "local_hour_code")
    )
    assert admissible == (
        "local_hour_code",
    ), f"a 2.4e9-magnitude FNV hash was offered as a category index: {admissible}"


def test_an_oversized_integer_category_code_is_refused_not_allocated() -> None:
    """The gate can fail, and it fails naming the column and the measured magnitude."""
    frame = _frame(
        graph_community_id=pl.Series(
            "graph_community_id", [FNV_MAGNITUDE, 1, 2, 3] * 32, dtype=pl.Int64
        ),
        **{COL_LABEL: _labelled(128, 6)},
    )
    with pytest.raises(FrameContractViolationError) as matrix_error:
        feature_matrix(frame, ("graph_community_id",), ("graph_community_id",))
    assert "graph_community_id" in str(matrix_error.value)
    assert "4255337285" in str(matrix_error.value), (
        "the refusal must state the magnitude it measured, or a reader cannot tell a guard "
        "from a guess"
    )

    with pytest.raises(FrameContractViolationError):
        categorical_for_lightgbm(frame, ("graph_community_id",), ("graph_community_id",))


def test_a_negative_category_code_is_refused() -> None:
    """LightGBM takes 0-based indices; a negative code is a contract break, not a NaN."""
    frame = _frame(
        local_hour_code=pl.Series("local_hour_code", [-1, 5, 9, 23] * 32, dtype=pl.Int32),
        **{COL_LABEL: _labelled(128, 6)},
    )
    with pytest.raises(FrameContractViolationError):
        feature_matrix(frame, ("local_hour_code",), ("local_hour_code",))


def test_fit_gbm_completes_on_dense_codes_and_reports_what_it_named() -> None:
    """The production fit path, end to end, on the coding the corpus actually carries.

    Timed on purpose: the pre-fix coding of this same shape did not finish in 900 s. The
    ceiling here is generous because six agents share this host, but it is a ceiling -- a fit
    that starts re-magnifying categories again will blow through it and fail loudly rather
    than quietly becoming a memory incident.
    """
    cfg = load_model_config(REPO_ROOT)
    rows = 900
    rng = np.random.default_rng(cfg.seed)
    frame = _frame(
        local_hour_code=pl.Series(
            "local_hour_code", rng.integers(0, 24, rows).tolist(), dtype=pl.Int32
        ),
        txn_type_code=pl.Series("txn_type_code", rng.integers(0, 6, rows).tolist(), dtype=pl.Int32),
        graph_community_id=pl.Series(
            "graph_community_id", rng.integers(0, 2499, rows).tolist(), dtype=pl.Int64
        ),
        activity=pl.Series("activity", rng.normal(0.0, 1.0, rows).tolist()),
        amount=pl.Series("amount", rng.normal(0.0, 1.0, rows).tolist()),
        **{COL_LABEL: _labelled(rows, 40)},
    )
    declared = ("local_hour_code", "txn_type_code", "graph_community_id")
    names = (*declared, "activity", "amount")
    started = time.perf_counter()
    bundle = fit_gbm(frame, frame, names, declared, cfg.gbm, cfg.seed, {"num_leaves": 7})
    elapsed = time.perf_counter() - started
    assert elapsed < 120.0, f"a fold-shaped fit took {elapsed:.1f}s; the coding has regressed"
    assert bundle.lightgbm_categorical_features == declared, (
        "every dense categorical must actually be named to lightgbm; a silent narrowing here "
        "is the same defect wearing the opposite sign"
    )
    assert bundle.degenerate is False
    matrix = feature_matrix(frame, names, declared)
    assert bundle.predict_matrix(matrix).shape == (rows,)


def test_the_matrix_the_booster_trained_on_is_the_matrix_shap_reads() -> None:
    """One coding for fit, predict and explain, or SHAP explains a model that never existed."""
    cfg = load_model_config(REPO_ROOT)
    rows = 400
    rng = np.random.default_rng(cfg.seed)
    frame = _frame(
        local_hour_code=pl.Series(
            "local_hour_code", rng.integers(0, 24, rows).tolist(), dtype=pl.Int32
        ),
        graph_community_id=pl.Series(
            "graph_community_id", rng.integers(0, 2499, rows).tolist(), dtype=pl.Int64
        ),
        **{COL_LABEL: _labelled(rows, 20)},
    )
    declared = ("local_hour_code", "graph_community_id")
    names = declared
    trained = feature_matrix(frame, names, declared)
    explained = feature_matrix(frame.sort("local_hour_code"), names, declared)
    assert np.array_equal(
        trained[np.argsort(trained[:, 0], kind="stable"), :],
        explained,
    ), "the coder depends on row order, so two runs of one fold are not one model"
