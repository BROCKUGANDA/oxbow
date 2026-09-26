"""Pure metric functions the P6 harness composes. No I/O, no configuration.

WHY A SEPARATE MODULE: plan §12 requires the harness itself to be verified with
hand-computed fakes *before* the real scorer/allocator/fold-provider land, which only
works if the metric arithmetic has no dependencies beyond numpy, polars and
``oxbow.quant.money``. Every function here takes plain arrays or integer minor-unit
amounts and returns a number whose value can be checked on paper — the tests carry
that paper arithmetic in a comment beside each assertion, so a metric that silently
regressed fails against a human figure, not against a re-run of the same code (00 §B:
"a fixture whose expected value came from the code it tests proves nothing").

MONEY (DEV-005): money in and money out are integer minor units. Money times a rate
goes through ``Money.scaled_by_micro`` so a binary float never enters a sum; a
benefit *ratio* is a ratio of two integer amounts and is therefore a float by
necessity, which is exactly the one quantity plan §12 names "explicitly NOT a Sharpe
ratio". The naming rule the float-money lint enforces is honoured here: nothing whose
name carries a money fragment is annotated as a float.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from oxbow.quant.money import Money, ratio_to_micro, scale_div


def _as_float_array(values: Sequence[float]) -> np.ndarray:
    return np.asarray(values, dtype=np.float64)


def _deterministic_score_order(scores: Sequence[float], keys: Sequence[str]) -> list[int]:
    """Descending by score, ties broken ascending by account key.

    Determinism (01 §A rule 4): queue order must not move between runs. Sorting only
    by score would let two equal-probability accounts swap on Python's sort
    stability depending on input order, changing precision at the budget boundary.
    """
    return sorted(range(len(scores)), key=lambda i: (-scores[i], keys[i]))


def pr_auc(scores: Sequence[float], labels: Sequence[int], keys: Sequence[str]) -> float:
    """Precision-recall area (primary metric, plan §12) as mean precision at recall.

    Defined only when at least one positive exists; the caller reports a
    zero-positive fold as *undefined* (plan §12: ``test_zero_positive_fold_reported``),
    so a bare ``0.0`` here would masquerade as a real low score rather than a skip.
    """
    total_positives = int(sum(labels))
    if total_positives == 0:
        raise ValueError("pr_auc undefined with zero positives; report the fold as skipped")
    order = _deterministic_score_order(scores, keys)
    true_positives = 0
    area = 0.0
    for rank, index in enumerate(order, start=1):
        if labels[index] == 1:
            true_positives += 1
            area += true_positives / rank
    return area / total_positives


def auroc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """ROC area via the Mann-Whitney U statistic with average-rank tie handling.

    Reported *for comparability and explicitly de-emphasised* (plan §12): on a
    0.1 % base rate AUROC is dominated by the huge negative mass and looks good while
    PR-AUC is bad, so the harness must never let it be the headline. The rank formula
    is exact and hand-checkable, unlike a trapezoid sweep over threshold buckets.
    """
    positives = int(sum(labels))
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        raise ValueError("auroc undefined when one class is empty")
    array = _as_float_array(scores)
    order = np.argsort(array, kind="stable")
    ranks = np.empty(len(array), dtype=np.float64)
    # Average rank across tied score groups so a tie cannot inflate or deflate the AUC.
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and array[order[j + 1]] == array[order[i]]:
            j += 1
        average_rank = (i + j) / 2 + 1
        for position in range(i, j + 1):
            ranks[order[position]] = average_rank
        i = j + 1
    positive_rank_sum = float(sum(ranks[k] for k in range(len(labels)) if labels[k] == 1))
    u_statistic = positive_rank_sum - positives * (positives + 1) / 2
    return u_statistic / (positives * negatives)


def precision_at(scores: Sequence[float], labels: Sequence[int], keys: Sequence[str], budget: int):
    """Precision and recall for the top ``budget`` ranked accounts.

    Returns ``(precision, recall, alert_count)``. Precision is *undefined* when the
    budget is zero (no alerts), reported by the caller as ``None`` with the alert
    count rather than 0.0 or 1.0 (plan §12: ``test_no_alerts_fold_undefined_not_zero``).
    """
    total_positives = int(sum(labels))
    if budget <= 0:
        return None, None, 0
    order = _deterministic_score_order(scores, keys)
    selected = order[:budget]
    hits = sum(1 for index in selected if labels[index] == 1)
    precision = hits / budget
    recall = (hits / total_positives) if total_positives else None
    return precision, recall, len(selected)


def recall_at(
    scores: Sequence[float], labels: Sequence[int], keys: Sequence[str], budget: int
) -> float | None:
    """Recall for the top ``budget`` ranked accounts; ``None`` if there are no positives."""
    precision, recall, _ = precision_at(scores, labels, keys, budget)
    del precision
    return recall


def brier_score(probabilities: Sequence[float], labels: Sequence[int]) -> float:
    """Mean squared calibration error, sum (p - y)^2 / n (plan §12)."""
    array = _as_float_array(probabilities)
    truth = _as_float_array(labels)
    return float(np.mean((array - truth) ** 2))


def reliability_curve(
    probabilities: Sequence[float], labels: Sequence[int], n_bins: int
) -> list[dict[str, float | int]]:
    """Binned mean-predicted vs observed rate for the reliability diagram (plan §12).

    Equal-width bins over [0, 1] so the curve is reproducible and the data page can
    plot predicted-vs-actual against the diagonal without a quantile sort that would
    depend on population order.
    """
    if n_bins <= 0:
        raise ValueError("reliability_curve needs a positive bin count")
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    array = _as_float_array(probabilities)
    truth = _as_float_array(labels)
    curve: list[dict[str, float | int]] = []
    for b in range(n_bins):
        low, high = edges[b], edges[b + 1]
        in_bin = (array >= low) & (array <= high if b == n_bins - 1 else array < high)
        count = int(np.count_nonzero(in_bin))
        mean_predicted = float(array[in_bin].mean()) if count else 0.0
        observed_rate = float(truth[in_bin].mean()) if count else 0.0
        curve.append(
            {
                "bin_low": float(low),
                "bin_high": float(high),
                "mean_predicted": mean_predicted,
                "observed_rate": observed_rate,
                "n": count,
            }
        )
    return curve


def per_typology_recall(
    probabilities: Sequence[float],
    labels: Sequence[int],
    typologies: Sequence[str | None],
    keys: Sequence[str],
    budget: int,
) -> dict[str, float | None]:
    """Recall of each labelled typology within the budgeted review set (plan §12).

    THE ROW THAT PROVES NETWORK DETECTION: point-anomaly detectors recall high on
    FAN-IN/FAN-OUT (an odd account) but collapse on CYCLE (a shape, not an outlier).
    Recall is computed per typology over only that typology's positives, so a single
    aggregate AUC cannot hide a typology the network layer misses. ``RANDOM`` is the
    planted negative control (DEV-014): it is laundering traffic with no typology, so
    a *high* RANDOM recall next to a low CYCLE recall is the signature of a detector
    that learned account-level oddity rather than network shape.
    """
    order = _deterministic_score_order(probabilities, keys)
    selected = set(order[:budget])
    grouped: dict[str, list[int]] = {}
    for index, typology in enumerate(typologies):
        if labels[index] != 1 or typology is None:
            continue
        grouped.setdefault(typology, []).append(index)
    result: dict[str, float | None] = {}
    for typology in sorted(grouped):
        members = grouped[typology]
        caught = sum(1 for index in members if index in selected)
        result[typology] = caught / len(members)
    return result


def alerts_per_10k_accounts(alert_count: int, total_accounts: int) -> float:
    """Alert-fatigue proxy: alerts raised per 10,000 accounts screened (plan §12)."""
    if total_accounts <= 0:
        raise ValueError("alerts_per_10k_accounts needs a positive account count")
    return alert_count / total_accounts * 10_000


def typology_recall_membership(
    labels: Sequence[int],
    typologies: Sequence[str | None],
    keys: Sequence[str],
    reviewed: set[str],
) -> dict[str, float]:
    """Recall of each typology as set membership of the policy's reviewed accounts.

    ``per_typology_recall`` ranks by score and cuts a budget; this variant measures a
    *capacity-constrained* policy's actual reviewed set, because an EV policy does not
    review the top-N by probability — it reviews a set that fits the analyst minutes.
    Same intent (network detection per typology, DEV-014), different selection rule.
    """
    grouped: dict[str, int] = {}
    caught: dict[str, int] = {}
    for label, typology, key in zip(labels, typologies, keys, strict=True):
        if label != 1 or typology is None:
            continue
        grouped[typology] = grouped.get(typology, 0) + 1
        if key in reviewed:
            caught[typology] = caught.get(typology, 0) + 1
    return {typology: caught.get(typology, 0) / grouped[typology] for typology in sorted(grouped)}


def spearman_rank_correlation(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman's rho: Pearson correlation of the two rank vectors.

    Used by the ±10 % amount-shift perturbation (plan §12): the *scores* under a
    money shift are compared by rank, not by value, because a monotone amount change
    should not reorder a robust policy even if it changes every absolute figure. A
    rho near 1 is the pass; a rho that drops is the finding worth reporting.
    """
    if len(x) != len(y) or len(x) < 2:
        raise ValueError("spearman needs two equal-length series of at least two points")
    rank_x = _average_ranks(x)
    rank_y = _average_ranks(y)
    mean_x = sum(rank_x) / len(rank_x)
    mean_y = sum(rank_y) / len(rank_y)
    covariance = sum((a - mean_x) * (b - mean_y) for a, b in zip(rank_x, rank_y, strict=True))
    var_x = math.sqrt(sum((a - mean_x) ** 2 for a in rank_x))
    var_y = math.sqrt(sum((b - mean_y) ** 2 for b in rank_y))
    if var_x == 0.0 or var_y == 0.0:
        return math.nan
    return covariance / (var_x * var_y)


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Fractional (average) ranks, so tied amounts get a single shared rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average_rank = (position + end) / 2 + 1
        for index in range(position, end + 1):
            ranks[order[index]] = average_rank
        position = end + 1
    return ranks


@dataclass(frozen=True, slots=True)
class BootstrapCI:
    """A 95 % bootstrap interval with its seed and resample count recorded."""

    point: float
    low: float
    high: float
    resamples: int
    seed: int


def bootstrap_ci(
    probabilities: Sequence[float],
    labels: Sequence[int],
    keys: Sequence[str],
    statistic: Callable[[Sequence[float], Sequence[int], Sequence[str]], float],
    *,
    resamples: int,
    seed: int,
    confidence: float = 0.95,
) -> BootstrapCI:
    """Non-parametric bootstrap CI over the accounts, seeded and count-stored.

    Plan §12 fixes 1000 resamples at 95 %; the seed and the count are returned so a
    reviewer can reproduce the exact interval (01 §A rule 4, ``test_var_reproducible``
    spirit). Resampling accounts (rows) rather than thresholds keeps PR-AUC's
    dependence on the positive set honest: a lucky draw of positives moves the
    interval, and the report should say so.
    """
    rng = np.random.default_rng(seed)
    n = len(probabilities)
    if n == 0:
        raise ValueError("bootstrap needs at least one scored account")
    stats: list[float] = []
    for _ in range(resamples):
        idx = rng.integers(0, n, size=n)
        sample_labels = [labels[k] for k in idx]
        if sum(sample_labels) == 0:
            continue  # a resample with no positives has an undefined PR-AUC; skip it
        sample_probs = [probabilities[k] for k in idx]
        sample_keys = [keys[k] for k in idx]
        try:
            stats.append(statistic(sample_probs, sample_labels, sample_keys))
        except ValueError:
            continue
    if not stats:
        raise ValueError("no bootstrap resample retained a positive; interval undefined")
    array = np.asarray(stats, dtype=np.float64)
    tail = (1.0 - confidence) / 2.0
    low = float(np.quantile(array, tail, method="lower"))
    high = float(np.quantile(array, 1.0 - tail, method="higher"))
    point = statistic(list(probabilities), list(labels), list(keys))
    return BootstrapCI(
        point=point,
        low=low,
        high=high,
        resamples=resamples,
        seed=seed,
    )


@dataclass(frozen=True, slots=True)
class TailRisk:
    """VaR / expected shortfall over residual exposure, with the seed and draws stored.

    ``seed`` and ``draws`` are part of the value, not a footnote: plan §12
    (``test_var_reproducible``) requires the Monte Carlo generator's seed and the
    draw count to be stored so a re-run yields identical bytes.
    """

    var_minor: int
    es_minor: int
    alpha_var: float
    alpha_es: float
    draws: int
    seed: int


def monte_carlo_tail_risk(
    loss_minor: Sequence[int],
    probabilities: Sequence[float],
    *,
    alpha_var: float,
    alpha_es: float,
    draws: int,
    seed: int,
) -> TailRisk:
    """VaR and ES of *unactioned residual exposure* over ``draws`` seeded runs.

    Each Monte Carlo run draws an independent Bernoulli(probability) for every
    unreviewed account and sums the minor-unit loss of those that turn out to be real
    laundering; VaR at ``alpha`` is the inverse empirical CDF ``inf{l: P(L<=l)>=alpha}``
    and ES at ``alpha`` is ``E[L | L >= VaR_alpha]`` (plan §12 formulas). The loss is
    an *integer minor amount* per account, so a run's total is an integer sum: no
    float enters the money, and the reported VaR/ES are integers.

    Compared across policies this becomes tail reduction: a policy that lowers mean
    net benefit while fattening this residual tail is a bad policy (plan §12), which is
    why the harness reports ES as well as VaR.
    """
    if not 0.0 < alpha_var < alpha_es < 1.0:
        raise ValueError(f"alphas must satisfy 0 < {alpha_var} < {alpha_es} < 1")
    if draws <= 0:
        raise ValueError("draws must be positive")
    n = len(loss_minor)
    if n == 0:
        return TailRisk(0, 0, alpha_var, alpha_es, draws, seed)
    loss = np.asarray(loss_minor, dtype=np.int64)
    prob = np.asarray(probabilities, dtype=np.float64)
    if len(prob) != n:
        raise ValueError("loss and probability vectors differ in length")
    rng = np.random.default_rng(seed)
    totals = np.empty(draws, dtype=np.int64)
    # Chunked so the (draws x n) Bernoulli matrix never allocates in full: 10,000
    # draws over a large unreviewed set would otherwise demand gigabytes.
    chunk = max(1, min(draws, 2_000_000 // max(n, 1)))
    produced = 0
    while produced < draws:
        take = min(chunk, draws - produced)
        fired = (rng.random((take, n)) < prob[None, :]).astype(np.int64)
        totals[produced : produced + take] = fired @ loss
        produced += take
    ordered = np.sort(totals)
    var_minor = _empirical_inverse_cdf(ordered, alpha_var)
    es_alpha_rank = _empirical_inverse_cdf(ordered, alpha_es)
    tail_values = ordered[ordered >= es_alpha_rank]
    es_minor = (
        int(scale_div(int(tail_values.sum()), int(tail_values.size)))
        if tail_values.size
        else int(es_alpha_rank)
    )
    return TailRisk(
        var_minor=int(var_minor),
        es_minor=int(es_minor),
        alpha_var=alpha_var,
        alpha_es=alpha_es,
        draws=draws,
        seed=seed,
    )


def _empirical_inverse_cdf(sorted_draws: np.ndarray, alpha: float) -> int:
    """``inf{l: P(L <= l) >= alpha}`` on a sorted draw vector (plan §12 VaR formula)."""
    draws = sorted_draws.size
    # ceil(alpha * draws) - 1 is the smallest index whose empirical CDF reaches alpha.
    index = int(math.ceil(alpha * draws)) - 1
    index = min(max(index, 0), draws - 1)
    return int(sorted_draws[index])


def cumulative_benefit(net_benefit_minor_per_period: Sequence[int]) -> list[int]:
    """Equity-style running sum of per-period net benefit, in integer minor units.

    One line per policy (plan §12); integer partial sums so the cumulative curve
    reconciles to the sum of its rows exactly (03 money rule: totals from unrounded
    minor units, round only at render).
    """
    out: list[int] = []
    running = 0
    for period in net_benefit_minor_per_period:
        if isinstance(period, bool) or not isinstance(period, int):
            raise ValueError("cumulative_benefit takes integer minor-unit periods")
        running += period
        out.append(running)
    return out


def max_drawdown_minor(cumulative: Sequence[int]) -> int:
    """Largest peak-to-trough drop of the cumulative benefit curve, in minor units.

    A non-negative integer: the magnitude of the worst run-off. Zero is legitimate and
    must be *labelled* by the caller (plan §12: ``test_zero_drawdown_labelled``) — zero
    drawdown means the policy never lost money in these folds, not that the metric is
    missing.
    """
    peak = 0
    worst = 0
    for value in cumulative:
        if value > peak:
            peak = value
        shortfall = peak - value
        if shortfall > worst:
            worst = shortfall
    return worst


def risk_adjusted_ratio(net_benefit_minor_per_period: Sequence[int]) -> float:
    """Mean per-period net benefit divided by its standard deviation.

    EXPLICITLY NOT A SHARPE RATIO (plan §12, ``test_label_not_sharpe``). A Sharpe
    subtracts a risk-free rate and annualises; neither applies to a per-fold review
    ledger, so this is a *benefit ratio* and the harness prints the formula alongside
    it. The value is a ratio of two integer amounts and is therefore a float, which is
    why the function and its callers never name it with a money fragment.
    """
    values = np.asarray(net_benefit_minor_per_period, dtype=np.float64)
    if values.size < 2:
        raise ValueError("risk-adjusted ratio needs at least two periods")
    dispersion = float(values.std(ddof=1))
    if dispersion == 0.0:
        return math.inf
    return float(values.mean() / dispersion)


def benefit_per_analyst_hour_minor(net_benefit_minor: int, analyst_minutes: int) -> int:
    """Net benefit normalised to one analyst-hour, in integer minor units.

    The number a risk manager budgets against (plan §12). It is a *money per unit
    time* rate, kept integer by dividing minor units by whole hours with the shared
    round-half-up rule, so no float ever stands in for a money figure.
    """
    if analyst_minutes <= 0:
        raise ValueError("benefit per analyst-hour needs positive analyst minutes")
    hours = analyst_minutes / 60.0
    if hours <= 0.0:
        raise ValueError("analyst minutes rounded to zero hours")
    # integer minor-per-hour: net_benefit_minor * 60 // analyst_minutes, half-up.
    return scale_div(net_benefit_minor * 60, analyst_minutes)


def money_scaled(value_minor: int, rate: float, currency: str) -> int:
    """Scale an integer minor amount by a real rate, staying integer (round half up).

    The only sanctioned path from an exposure to a recovery-rate-weighted amount:
    ``Money(value).scaled_by_micro(ratio_to_micro(rate))`` converts the float out
    immediately, so ``E_i * r`` is exact integer arithmetic (DEV-005).
    """
    return Money(value_minor, currency).scaled_by_micro(ratio_to_micro(rate)).minor
