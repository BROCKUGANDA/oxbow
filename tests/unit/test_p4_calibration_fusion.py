"""The §10 calibration, prior-correction, explanation-fallback and fusion guards.

WHAT THIS FILE OWNS. Four behaviours plan §10 names, all on the model side of P4 rather than
the scorecard side (which ``test_p4_scorecard_guards.py`` holds):

* **Calibration is refused below the floor and says so.** Module C multiplies these
  probabilities by money, so which branch runs is a load-bearing fact: isotonic at or above
  ``isotonic_min_positives``, Platt between that and ``min_positives_for_calibration``, and
  *refused* below the floor -- with the output labelled uncalibrated rather than carrying a
  number nobody will stand behind.
* **The oversampled training prior is corrected, and the correction is stated.** King & Zeng
  intercept correction, not a silent scale: the method name, both base rates and the shift all
  travel in the payload, and the shift is hand-computable from the config's own formula.
* **A degenerate tree falls back to scorecard points.** ``models/explain.py`` labels the case
  ``scorecard-explained``; the trigger condition is what this test pins, because the
  persistence half of that path currently raises -- see the note inside the test.
* **The scorecard-vs-GBM agreement matrix renders.** Cells sum to the rows scored, both axes
  are the declared bands, and the disagreement list is ordered by the biggest divergence.

DETERMINISM AND MONEY. Every slice is built from ``SEED`` with its counts stated in the test
that uses it, so the calibration branch that fires is arithmetic rather than luck, and no
assertion reads a clock. The cross-tab fixture is six rows written out by hand. Probabilities
and log-odds are the only floats here; no money column appears anywhere in this file.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Final

import numpy as np
import polars as pl
import pytest

from oxbow.config import find_repo_root
from oxbow.models.calibration import (
    CalibrationOutcome,
    calibrate,
    choose_method,
    correct_prior,
    logit,
    odds,
    reliability_curve,
    sigmoid,
)
from oxbow.models.config import CalibrationConfig, ModelConfig, load_model_config
from oxbow.models.errors import CalibrationRefusedError, ModelLayerError
from oxbow.models.evaluate import (
    agreement_matrix,
    average_precision,
    brier,
    ks_statistic,
    measure,
)
from oxbow.models.explain import SOURCE_SCORECARD, explain_and_report, is_degenerate
from oxbow.models.gbm import fit_gbm
from oxbow.scoring.frame import COL_ACCOUNT_KEY, COL_LABEL

SEED: Final = 1337
REPO_ROOT: Path = find_repo_root()


def _model_config() -> ModelConfig:
    return load_model_config(REPO_ROOT)


def _alternative_correction(calibration: CalibrationConfig) -> CalibrationConfig:
    """The other method the plan allows, so the branch that names one is falsifiable."""
    return replace(calibration, oversampling_correction="base_rate_preserving_holdout")


def _slice(positives: int, rows: int) -> tuple[np.ndarray, np.ndarray]:
    """A validation slice with exactly ``positives`` bads and a rankable raw score.

    The first ``positives`` rows are the bads and they sit above the good rows before noise,
    so a calibrator has something to fit while the positive count -- the only quantity the
    branch selection reads -- stays a chosen number rather than a draw.
    """
    rng = np.random.default_rng(SEED)
    labels = np.zeros(rows, dtype=np.int32)
    labels[:positives] = 1
    raw = np.clip(0.30 + 0.30 * labels + rng.normal(0.0, 0.10, rows), 1e-4, 1.0 - 1e-4)
    return raw, labels


def _uncorrected(
    raw: np.ndarray, labels: np.ndarray, calibration: CalibrationConfig
) -> CalibrationOutcome:
    """``calibrate`` with train and population priors equal, so no intercept shift is applied."""
    share = float(np.mean(labels))
    return calibrate(
        raw,
        labels,
        calibration,
        train_positive_share=share,
        population_positive_share=share,
    )


def test_calibration_refused_below_floor() -> None:
    """Below the declared floor calibration refuses, and the outcome says uncalibrated.

    The floor is config, not code. Both edges are asserted on the counts either side of them,
    so a moved number in ``config/model.yaml`` surfaces here rather than in a reported
    probability.
    """
    calibration = _model_config().calibration
    floor = calibration.min_positives_for_calibration
    isotonic_min = calibration.isotonic_min_positives
    assert (floor, isotonic_min) == (50, 500), calibration
    assert calibration.below_floor_action == "refuse_and_label_uncalibrated"
    assert (calibration.method, calibration.fallback_method) == ("isotonic", "platt")

    refused, refused_reason = choose_method(floor - 1, calibration)
    assert refused == "refused", refused_reason
    assert str(floor) in refused_reason
    at_floor, at_floor_reason = choose_method(floor, calibration)
    assert at_floor == "platt", at_floor_reason
    assert str(isotonic_min) in at_floor_reason
    below_isotonic, below_reason = choose_method(isotonic_min - 1, calibration)
    assert below_isotonic == "platt" and "isotonic would step on noise" in below_reason
    at_isotonic, at_isotonic_reason = choose_method(isotonic_min, calibration)
    assert at_isotonic == "isotonic" and "isotonic is fitted" in at_isotonic_reason

    raw, labels = _slice(positives=floor - 30, rows=400)
    outcome = _uncorrected(raw, labels, calibration)

    assert outcome.refused is True and outcome.method == "refused"
    assert outcome.calibrated is False
    assert outcome.validation_positives == 20 and outcome.validation_rows == 400
    assert outcome.floor == floor and outcome.isotonic_min_positives == isotonic_min
    reason = outcome.refusal_reason or ""
    assert "uncalibrated" in reason and str(floor) in reason
    assert calibration.below_floor_action in reason, reason
    label = outcome.confidence_label(0.5)
    assert label["kind"] == "uncalibrated"
    assert "probabilities are uncalibrated" in label["text"]
    assert label["observed_rate"] is None and label["sample_size"] is None
    assert outcome.confidence_label(None)["kind"] == "uncalibrated"

    # A refusal must not hand Module C a number to multiply.
    assert outcome.apply(raw) is None
    assert outcome.apply(raw, demand=False) is None
    with pytest.raises(CalibrationRefusedError, match="below"):
        outcome.apply(raw, demand=True)

    # The honest statement keeps what it measured: the metrics of the uncalibrated corrected
    # scores, and no fitted step function masquerading as one.
    assert outcome.isotonic_x == () and outcome.isotonic_y == ()
    assert outcome.platt_weight is None and outcome.platt_intercept is None
    assert math.isfinite(outcome.brier) and math.isfinite(outcome.ece)
    assert sum(bin_row.sample_size for bin_row in outcome.reliability) == 400
    assert len(outcome.reliability) == calibration.reliability_bins
    payload = outcome.to_dict()
    assert payload["refused"] is True and payload["calibrated"] is False
    assert "uncalibrated" in json.dumps(payload)

    platt_outcome = _uncorrected(*_slice(positives=120, rows=1000), calibration=calibration)
    assert platt_outcome.method == "platt", platt_outcome.branch_reason
    assert platt_outcome.refused is False and platt_outcome.calibrated is True
    assert platt_outcome.platt_weight is not None
    assert platt_outcome.platt_intercept is not None
    assert platt_outcome.confidence_label(0.5)["kind"] == "calibrated_band"

    isotonic_outcome = _uncorrected(
        *_slice(positives=isotonic_min, rows=2500), calibration=calibration
    )
    assert isotonic_outcome.method == "isotonic", isotonic_outcome.branch_reason
    assert isotonic_outcome.refused is False
    assert len(isotonic_outcome.isotonic_x) >= 2, "no step function was stored"
    band_label = isotonic_outcome.confidence_label(0.5)
    assert band_label["kind"] == "calibrated_band"
    assert "observed rate in this band" in band_label["text"]
    assert band_label["sample_size"] == 250
    applied = isotonic_outcome.apply(np.full(4, isotonic_outcome.mean_raw_probability))
    assert applied is not None and applied.shape == (4,)
    assert bool(np.all(np.isfinite(applied))) and bool(np.all((applied >= 0.0) & (applied <= 1.0)))


def test_prior_correction_applied() -> None:
    """An oversampled train prior gets a named King & Zeng intercept shift, not a scale.

    Hand-computed with the config's own formula, ``shift = ln(odds(population)/odds(train))``,
    for a train share of 0.20 against a population base rate of 0.02:

        odds(0.02) = 0.02 / 0.98 = 0.020408163265306121
        odds(0.20) = 0.20 / 0.80 = 0.25
        ratio      = 0.081632653061224489
        shift      = ln(ratio)   = -2.505525936990736

    Because the shift is added to the logit, a row sitting exactly on the train prior must
    land exactly on the population base rate: sigmoid(logit(0.20) - 2.505525936990736) = 0.02.
    """
    calibration = _model_config().calibration
    assert calibration.oversampling_correction == "king_zeng_intercept"
    assert calibration.oversampling_tolerance_multiple == 1.5

    train_share = 0.20
    population_share = 0.02
    expected_shift = math.log(odds(population_share) / odds(train_share))
    assert expected_shift == pytest.approx(-2.505525936990736, abs=1e-12)

    correction = correct_prior(train_share, population_share, calibration)
    assert correction.applied is True, correction.reason
    assert correction.method == "king_zeng_intercept", (
        f"the payload names {correction.method!r}: the plan allows exactly two methods and "
        "config must name the one that ran"
    )
    assert correction.logit_shift == pytest.approx(expected_shift, abs=1e-12)
    assert correction.train_positive_share == train_share
    assert correction.population_positive_share == population_share
    assert correction.tolerance_multiple == 1.5
    assert "King-Zeng" in correction.reason, correction.reason
    assert "1.5 x population share" in correction.decision_rule
    assert (
        correction.logit_shift < 0.0
    ), "oversampled positives must pull probabilities down toward the population rate, not up"

    corrected = sigmoid(logit(np.array([train_share])) + correction.logit_shift)
    assert corrected[0] == pytest.approx(population_share, abs=1e-12), corrected[0]

    # A whole slice moves toward the population rate, and it moves by the stated shift rather
    # than by a rescale: the log-odds difference is constant across rows.
    rng = np.random.default_rng(SEED)
    raw = np.clip(rng.normal(0.35, 0.15, 200), 1e-4, 1 - 1e-4)
    moved = sigmoid(logit(raw) + correction.logit_shift)
    assert float(np.mean(moved)) < float(np.mean(raw)) / 5, (
        f"the mean probability went {np.mean(raw):.4f} -> {np.mean(moved):.4f} against a "
        "population base rate of 0.02"
    )
    assert bool(np.allclose(logit(moved) - logit(raw), correction.logit_shift)), (
        "the shift is not constant in log-odds space, so what ran is a scale and not an "
        "intercept correction"
    )

    # Not oversampled -- inside the declared 1.5x tolerance -- and the outcome says so in
    # words rather than leaving the field blank.
    quiet = correct_prior(0.02, 0.02, calibration)
    assert quiet.applied is False and quiet.logit_shift == 0.0
    assert quiet.method == "king_zeng_intercept"
    assert "no oversampling to correct" in quiet.reason
    assert (
        correct_prior(0.03, 0.02, calibration).applied is False
    ), "0.03 against 0.02 sits exactly on the 1.5x tolerance, so it is inside it"
    assert correct_prior(0.031, 0.02, calibration).applied is True

    # And the correction the calibrator reports is the correction it applied.
    raw_slice, labels_slice = _slice(positives=200, rows=1000)
    outcome = calibrate(
        raw_slice,
        labels_slice,
        calibration,
        train_positive_share=train_share,
        population_positive_share=population_share,
    )
    assert outcome.prior_correction.applied is True
    assert outcome.prior_correction.logit_shift == pytest.approx(expected_shift, abs=1e-12)
    stored = outcome.to_dict()["prior_correction"]
    assert isinstance(stored, dict)
    assert stored["method"] == "king_zeng_intercept" and stored["applied"] is True
    assert "ln(odds(population) / odds(train))" in stored["formula"]

    held_out = calibrate(
        raw_slice,
        labels_slice,
        _alternative_correction(calibration),
        train_positive_share=train_share,
        population_positive_share=population_share,
    )
    assert held_out.prior_correction.method == "base_rate_preserving_holdout"
    assert held_out.prior_correction.applied is False
    assert held_out.prior_correction.logit_shift == 0.0
    assert "base-rate-preserving holdout" in held_out.prior_correction.reason


def test_shap_fallback_to_points() -> None:
    """A degenerate tree is the trigger for the scorecard-points explanation, both arms.

    WHAT THIS TEST PINS, AND WHAT IT CANNOT REACH. :func:`is_degenerate` is the public
    decision ``explain_and_report`` branches on, and both of its arms are asserted here against
    a booster that really never split: no feature was split on, and -- separately, on a healthy
    booster -- a total attribution under ``shap.degenerate_max_abs_contribution``.

    The unreachable half is the persistence of the fallback. ``explain_and_report`` does build
    the fallback rows correctly, then hands them to the private ``_scorecard_outcome``, which
    counts them with ``Series.filter(pl.col(...) == ...)`` -- a polars expression where a
    boolean series is required -- raising ``TypeError: Series constructor called with
    unsupported type 'Expr'``. That fires on every scorecard-explained path, including the one
    taken when no GBM was fitted at all, which is the mode this phase's own drift action
    selects. It is reported rather than asserted here: pinning the crash would produce a test
    that has to be deleted when the defect is fixed.
    """
    cfg = _model_config()
    shap_cfg = cfg.shap
    assert shap_cfg.on_degenerate_tree == "fallback_to_points"
    assert shap_cfg.degenerate_max_abs_contribution == 1e-9
    assert shap_cfg.explainer == "TreeExplainer"
    assert SOURCE_SCORECARD == "scorecard-explained"
    assert shap_cfg.persist_columns == ("shap_json", "shap_base_value", "explanation_source")

    rng = np.random.default_rng(SEED)
    rows = 200
    labels = np.zeros(rows, dtype=np.int8)
    labels[rng.integers(0, 120, 60)] = 1
    frame = pl.DataFrame(
        {
            "activity_index": pl.Series(rng.normal(0.0, 1.0, rows).tolist()),
            COL_LABEL: pl.Series(labels.tolist(), dtype=pl.Int8),
        }
    )

    never_split = fit_gbm(
        frame,
        frame,
        ("activity_index",),
        (),
        cfg.gbm,
        cfg.seed,
        {"min_child_samples": 100000},
    )
    assert never_split.split_feature_count == 0, never_split.split_feature_count
    assert never_split.degenerate is True
    assert is_degenerate(never_split, shap_cfg, 10.0) is True, (
        "an ensemble that split on nothing must be reported degenerate however large the "
        "attribution looks: TreeExplainer on it returns zeros, and an empty 'why this was "
        "flagged' pane is a lie about a detection system"
    )

    split = fit_gbm(frame, frame, ("activity_index",), (), cfg.gbm, cfg.seed, None)
    assert split.split_feature_count > 0 and split.degenerate is False
    assert is_degenerate(split, shap_cfg, shap_cfg.degenerate_max_abs_contribution / 2) is True, (
        "the second arm -- a flat explanation under the declared floor -- does not fire, so a "
        "tree that split on everything but learned nothing would still be published as SHAP"
    )
    assert not is_degenerate(split, shap_cfg, 1.0), (
        "the guard reports a healthy ensemble as degenerate, so every row would be labelled "
        "scorecard-explained and the SHAP pane would go quiet for nothing"
    )


def test_the_scorecard_fallback_completes_on_every_row() -> None:
    """The points fallback is only a fallback if the row it produces is readable.

    ``is_degenerate`` decides, and :func:`oxbow.models.explain.explain_and_report` then
    writes the points list. The second half was broken and nobody noticed: the outcome
    counter asked a polars ``Series`` to ``filter`` an expression, which raises
    ``TypeError: unsupported type 'Expr'``, so *every* scorecard-explained path died --
    including the ``bundle is None`` branch that a drift-degraded run takes. An
    attribution path that only works when attribution is available is not a fallback.
    """
    cfg = _model_config()
    shap_cfg = cfg.shap
    rows = 12
    frame = pl.DataFrame(
        {
            COL_ACCOUNT_KEY: [f"ACC-{index:012X}" for index in range(rows)],
            "score_points": pl.Series([640 - 7 * index for index in range(rows)], dtype=pl.Int64),
            "points_json": pl.Series(
                [
                    json.dumps(
                        [
                            {"feature": "velocity_7d", "attribute": "top decile", "points": -7},
                            {"feature": "night_share_30d", "attribute": "__unseen__", "points": 3},
                        ],
                        separators=(",", ":"),
                    )
                    for _ in range(rows)
                ]
            ),
            "p_fused": pl.Series([0.1 + 0.01 * index for index in range(rows)], dtype=pl.Float64),
        }
    )

    annotated, outcome = explain_and_report(frame, None, shap_cfg, "p_fused")

    assert outcome.source == SOURCE_SCORECARD, outcome.source
    assert outcome.rows == rows
    assert outcome.fallback_rows == rows, (
        f"{outcome.fallback_rows} of {rows} rows are counted as falling back; the counter "
        "must see the rows the fallback wrote, or the model card reports a SHAP coverage "
        "that the run never had"
    )
    assert outcome.explained_rows == 0
    assert outcome.fallback_reason and "no gbm" in outcome.fallback_reason
    assert annotated.get_column("explanation_source").unique().to_list() == [SOURCE_SCORECARD], (
        annotated.get_column("explanation_source").unique().to_list()
    )
    first = json.loads(annotated.get_column("shap_json")[0])
    assert [item["feature"] for item in first] == ["velocity_7d", "night_share_30d"], first
    assert [item["value"] for item in first] == [-7.0, 3.0], (
        "the points are the explanation; rescaling them would break the sum a reviewer "
        "can check by hand against score_points"
    )


def test_p4_gate_statistics_carry_hand_checked_values_and_a_bootstrap_ci() -> None:
    """§10's first gate clause, in one call: reliability curve, Brier, PR-AUC with a CI, KS.

    ``oxbow.models.evaluate`` is the module the model card reads, and until this file no
    test touched it -- ``test_p6_metrics.py`` covers ``oxbow.backtest.metrics``, which is a
    different implementation of some of the same names. Two modules computing "PR-AUC" and
    disagreeing is exactly the kind of defect a card cannot show, so every figure below is
    hand-computed on a ten-row ranking a person can follow:

    ranks    1     2   3     4   5     6    7   8   9   10
    labels   1     0   1     0   1     0    0   0   0   0
    scores  0.90 0.80 0.70  0.60 0.50 0.45 0.40 0.30 0.20 0.10

    PR-AUC   (1/3)(1.000) + (1/3)(2/3) + (1/3)(3/5)          = 0.755556
    KS       max |TPR - FPR| at rank 5: 3/3 - 2/7            = 0.714286
    Brier    sum of squared errors / 10 = 1.8525 / 10        = 0.185250
    """
    labels = np.array([1, 0, 1, 0, 1, 0, 0, 0, 0, 0], dtype=np.int32)
    scores = np.array([0.90, 0.80, 0.70, 0.60, 0.50, 0.45, 0.40, 0.30, 0.20, 0.10])

    assert average_precision(labels, scores) == pytest.approx(0.7555555556, abs=1e-9)
    assert ks_statistic(labels, scores) == pytest.approx(5 / 7, abs=1e-9)
    assert brier(labels, scores) == pytest.approx(0.18525, abs=1e-9)

    metrics = measure(
        "hand-checked",
        labels,
        scores,
        resamples=200,
        confidence=0.95,
        seed=SEED,
        provenance="test",
        probabilities=scores,
    )
    assert metrics.rows == 10 and metrics.positives == 3
    assert metrics.base_rate == pytest.approx(0.3)
    assert metrics.brier == pytest.approx(0.18525, abs=1e-9)
    assert metrics.ks == pytest.approx(5 / 7, abs=1e-9)
    assert metrics.provenance == "test"

    interval = metrics.pr_auc
    assert interval.estimate == pytest.approx(0.7555555556, abs=1e-9)
    assert interval.lower <= interval.estimate <= interval.upper, interval
    assert interval.confidence == 0.95 and interval.seed == SEED
    assert (
        interval.resamples > 0
    ), "a bootstrap CI over zero resamples is a point estimate wearing a bracket"
    assert interval.method == "percentile_bootstrap_rows"

    # The seed is the interval's identity: two calls with the same seed must land on the
    # same numbers, and that is what makes a published CI reproducible at all.
    again = measure(
        "hand-checked",
        labels,
        scores,
        resamples=200,
        confidence=0.95,
        seed=SEED,
        provenance="test",
        probabilities=scores,
    )
    assert again.pr_auc == interval
    other = measure(
        "hand-checked",
        labels,
        scores,
        resamples=200,
        confidence=0.95,
        seed=SEED + 1,
        provenance="test",
        probabilities=scores,
    )
    assert other.pr_auc.estimate == interval.estimate, "the point estimate moved with the seed"
    assert (other.pr_auc.lower != interval.lower) or (
        other.pr_auc.upper != interval.upper
    ), "a different seed redrew the same sample -- the seed is not reaching the generator"

    # The reliability curve is equal-count, five bins of two here, ordered low prediction to
    # high, and the observed rates are what the UI's "observed rate in this band" prints.
    bins = reliability_curve(scores, labels, 5)
    assert [bin_.sample_size for bin_ in bins] == [2, 2, 2, 2, 2]
    assert sum(bin_.sample_size for bin_ in bins) == 10, "the curve lost population"
    assert [round(bin_.mean_predicted, 6) for bin_ in bins] == [0.15, 0.35, 0.475, 0.65, 0.85]
    assert [round(bin_.observed_rate, 6) for bin_ in bins] == [0.0, 0.0, 0.5, 0.5, 0.5]
    assert [bin_.bin_index for bin_ in bins] == [0, 1, 2, 3, 4]


def test_agreement_matrix_renders() -> None:
    """The scorecard-vs-GBM cross-tab: cells sum to the rows, and both axes are declared bands.

    Six rows, written out by hand so every cell below is a count someone could reproduce on
    paper. The scorecard band runs down the rows and the GBM band across the columns, both in
    the declared safest-to-riskiest order A..E.
    """
    cfg = _model_config()
    band_ids = cfg.baselines.agreement_matrix_bands
    top_n = cfg.baselines.agreement_top_n_disagreements
    assert band_ids == ("A", "B", "C", "D", "E"), band_ids
    assert top_n == 25

    scored = pl.DataFrame(
        {
            COL_ACCOUNT_KEY: ["A0", "B1", "C2", "D3", "E4", "F5"],
            "band": ["A", "B", "C", "D", "E", "A"],
            "gbm_band": ["A", "C", "B", "E", "D", "B"],
            "p_scorecard": [0.01, 0.05, 0.20, 0.50, 0.80, 0.02],
            "p_gbm": [0.02, 0.30, 0.10, 0.95, 0.60, 0.10],
        }
    )
    matrix = agreement_matrix(scored, band_ids, top_n)
    payload = matrix.to_dict()

    assert payload["band_ids"] == list(band_ids)
    assert matrix.rows == scored.height == 6
    assert (
        sum(sum(row) for row in matrix.counts) == matrix.rows
    ), "the cross-tab does not conserve rows: an account was counted twice or not at all"
    assert payload["rows_scorecard_then_gbm"] == [list(row) for row in matrix.counts]
    assert matrix.counts == (
        (1, 1, 0, 0, 0),
        (0, 0, 1, 0, 0),
        (0, 1, 0, 0, 0),
        (0, 0, 0, 0, 1),
        (0, 0, 0, 1, 0),
    ), matrix.counts
    assert matrix.counts[0][0] == 1, "the only agreeing row is scorecard A against GBM A"
    off_diagonal = matrix.rows - sum(matrix.counts[i][i] for i in range(len(band_ids)))
    assert off_diagonal == 5, "the disagreement is the artefact, so it must be counted"
    assert matrix.exact_agreement == pytest.approx(1 / 6)
    assert matrix.within_one_band == pytest.approx(
        1.0
    ), "every divergence in the fixture is a single band step, so within-one-band is one"

    # Ordered by band distance, then probability gap, then account key: gaps are
    # D3 0.45, B1 0.25, E4 0.20, C2 0.10, F5 0.08, A0 0.01 (and A0 agrees).
    disagreements = matrix.disagreements
    assert len(disagreements) == min(top_n, matrix.rows)
    assert [row["account_key"] for row in disagreements] == ["D3", "B1", "E4", "C2", "F5", "A0"]
    assert [row["band_distance"] for row in disagreements] == [1, 1, 1, 1, 1, 0]
    assert [row["probability_gap"] for row in disagreements] == pytest.approx(
        [0.45, 0.25, 0.20, 0.10, 0.08, 0.01]
    )
    assert disagreements[0]["scorecard_band"] == "D" and disagreements[0]["gbm_band"] == "E"
    assert {"account_key", "scorecard_band", "gbm_band", "p_scorecard", "p_gbm"} <= set(
        disagreements[0]
    )
    assert "disagreement" in payload["note"]

    with pytest.raises(ModelLayerError, match="outside the declared band set"):
        agreement_matrix(
            scored.with_columns(pl.Series("gbm_band", ["A", "C", "B", "E", "D", "Z"])),
            band_ids,
            top_n,
        )
    with pytest.raises(ModelLayerError, match="needs column 'p_gbm'"):
        agreement_matrix(scored.drop("p_gbm"), band_ids, top_n)
