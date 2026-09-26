"""P6 metric primitives, each verified against arithmetic done by hand in a comment.

This is the harness's calibration: before any real scorer lands, the numbers it will
report must already be checkable on paper (00 §B). Every expected value below is a human
computation, not a re-run of the function, so a regression that changes the arithmetic
fails against the figure in the comment.
"""

from __future__ import annotations

import math

import pytest

from oxbow.backtest import metrics


def test_pr_auc_hand_computed() -> None:
    # scores [0.9,0.8,0.7,0.6], labels [1,0,1,0]. Descending order A,B,C,D.
    # positives at rank 1 (precision 1/1) and rank 3 (precision 2/3). AP = (1 + 0.6667)/2 = 0.8333.
    assert metrics.pr_auc(
        [0.9, 0.8, 0.7, 0.6], [1, 0, 1, 0], ["A", "B", "C", "D"]
    ) == pytest.approx(0.8333333333, abs=1e-9)


def test_pr_auc_undefined_with_zero_positives() -> None:
    with pytest.raises(ValueError):
        metrics.pr_auc([0.9, 0.1], [0, 0], ["A", "B"])


def test_auroc_hand_computed() -> None:
    # scores [0.9,0.8,0.7,0.6], labels [1,0,1,0]. Positives {0.9,0.7}, negatives {0.8,0.6}.
    # Mann-Whitney pairs: (0.9>0.8)+(0.9>0.6)+(0.7>0.6)+(0.7<0.8 no) = 3 of 4 -> 0.75.
    assert metrics.auroc([0.9, 0.8, 0.7, 0.6], [1, 0, 1, 0]) == pytest.approx(0.75, abs=1e-9)


def test_precision_at_budget_hand_computed() -> None:
    # order A,B,C,D; budget 2 -> {A(1),B(0)} -> precision 1/2, recall 1/2 (2 positives total).
    precision, recall, count = metrics.precision_at(
        [0.9, 0.8, 0.7, 0.6], [1, 0, 1, 0], ["A", "B", "C", "D"], 2
    )
    assert precision == pytest.approx(0.5)
    assert recall == pytest.approx(0.5)
    assert count == 2


def test_precision_undefined_at_zero_budget_not_zero_or_one() -> None:
    # Plan §12 test_no_alerts_fold_undefined_not_zero, at the metric level: no budget ->
    # precision and recall are undefined (None), NOT 0.0 and NOT 1.0.
    precision, recall, count = metrics.precision_at([0.9, 0.8], [1, 0], ["A", "B"], 0)
    assert precision is None
    assert recall is None
    assert count == 0


def test_brier_score_hand_computed() -> None:
    # predictions [0.5,0.5], labels [1,0] -> ((0.5)^2 + (0.5)^2)/2 = 0.25.
    assert metrics.brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)


def test_reliability_curve_bins_sum_to_population() -> None:
    probs = [0.1, 0.2, 0.8, 0.9]
    labels = [0, 0, 1, 1]
    curve = metrics.reliability_curve(probs, labels, n_bins=2)
    assert sum(int(bin_row["n"]) for bin_row in curve) == 4
    # The top bin (0.5,1.0] holds the two predictions 0.8,0.9 -> observed rate 1.0.
    top = max(curve, key=lambda b: float(b["bin_high"]))
    assert top["observed_rate"] == pytest.approx(1.0)


def test_per_typology_recall_membership_hand_computed() -> None:
    labels = [1, 1, 1, 0]
    typologies = ["CYCLE", "FAN-IN", "FAN-IN", None]
    keys = ["A", "B", "C", "D"]
    reviewed = {"A", "B"}  # caught the one CYCLE and one of the two FAN-IN.
    recall = metrics.typology_recall_membership(labels, typologies, keys, reviewed)
    assert recall["CYCLE"] == pytest.approx(1.0)
    assert recall["FAN-IN"] == pytest.approx(0.5)
    assert "RANDOM" not in recall  # a typology with no positives simply is not reported.


def test_alerts_per_10k_accounts_hand_computed() -> None:
    assert metrics.alerts_per_10k_accounts(30, 1000) == pytest.approx(300.0)


def test_var_reproducible_seed_and_draws_stored() -> None:
    # Same seed and draw count -> identical VaR/ES; the seed and draws are part of the value
    # (plan §12 test_var_reproducible).
    losses = [100, 200, 300, 50, 75, 640, 90, 410]
    probs = [0.5, 0.4, 0.6, 0.2, 0.7, 0.3, 0.5, 0.5]
    a = metrics.monte_carlo_tail_risk(
        losses, probs, alpha_var=0.95, alpha_es=0.975, draws=400, seed=1337
    )
    b = metrics.monte_carlo_tail_risk(
        losses, probs, alpha_var=0.95, alpha_es=0.975, draws=400, seed=1337
    )
    assert a == b
    assert a.seed == 1337
    assert a.draws == 400
    # ES (the worse tail) is never below VaR.
    assert a.es_minor >= a.var_minor
    # The stored seed is the draw generator's seed; a different seed is a different,
    # separately-stored run (reproducibility is per (seed, draws), not across seeds).
    c = metrics.monte_carlo_tail_risk(
        losses, probs, alpha_var=0.95, alpha_es=0.975, draws=400, seed=999
    )
    assert c.seed == 999
    assert c.draws == 400
    assert (a.var_minor, a.es_minor) != (c.var_minor, c.es_minor)


def test_var_alphas_validated() -> None:
    with pytest.raises(ValueError):
        metrics.monte_carlo_tail_risk(
            [100], [0.5], alpha_var=0.975, alpha_es=0.95, draws=10, seed=1
        )


def test_cumulative_benefit_and_max_drawdown_hand_computed() -> None:
    # net benefit per fold [+10, -3, +5] -> cumulative [10, 7, 12]; peak 10, trough after it 7
    # -> drawdown 3 (never below the initial peak).
    cum = metrics.cumulative_benefit([10, -3, 5])
    assert cum == [10, 7, 12]
    assert metrics.max_drawdown_minor(cum) == 3


def test_zero_drawdown_is_zero_not_missing() -> None:
    # A monotone-increasing ledger has zero drawdown, which is a real value (plan §12
    # test_zero_drawdown_labelled): the curve never fell, so the drop is 0.
    assert metrics.max_drawdown_minor([5, 9, 14]) == 0


def test_risk_adjusted_ratio_hand_computed() -> None:
    # per-period [10,20,30]: mean 20, sample std (ddof=1) = 10 -> ratio 2.0. This is a
    # mean/std of NET BENEFIT, not a Sharpe ratio (no rf, no annualisation).
    assert metrics.risk_adjusted_ratio([10, 20, 30]) == pytest.approx(2.0)


def test_risk_adjusted_ratio_infinite_when_no_dispersion() -> None:
    assert metrics.risk_adjusted_ratio([5, 5, 5]) == math.inf


def test_benefit_per_analyst_hour_hand_computed() -> None:
    # net benefit 1200 minor over 60 analyst-minutes (1 hour) -> 1200 minor/hour.
    assert metrics.benefit_per_analyst_hour_minor(1200, 60) == 1200
    # 1200 minor over 120 minutes (2 hours) -> 600 minor/hour, integer.
    assert metrics.benefit_per_analyst_hour_minor(1200, 120) == 600


def test_money_scaled_stays_integer_minor() -> None:
    # 1_000_000 minor at r=0.35 -> 350_000 minor, integer, no float.
    assert metrics.money_scaled(1_000_000, 0.35, "UGX") == 350_000
    # 7 minor at r=0.5 -> round half up -> 4 minor (not 3.5).
    assert metrics.money_scaled(7, 0.5, "UGX") == 4


def test_spearman_perfect_and_reversed() -> None:
    assert metrics.spearman_rank_correlation([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert metrics.spearman_rank_correlation([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_bootstrap_ci_brackets_point_estimate() -> None:
    probs = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2]
    labels = [1, 1, 1, 0, 1, 0, 0, 0]
    keys = [f"K{i}" for i in range(len(probs))]
    ci = metrics.bootstrap_ci(probs, labels, keys, metrics.pr_auc, resamples=200, seed=1337)
    assert ci.low <= ci.point <= ci.high
    assert ci.seed == 1337
    assert ci.resamples == 200
