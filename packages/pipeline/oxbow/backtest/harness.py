"""The walk-forward backtest harness: run folds, policies and metrics from one call.

WHAT THIS IS (plan §12): the engine the ablation table, the leakage control and every
headline number come from. It consumes the injected seams in
:mod:`oxbow.backtest.interfaces` and the ONE splits module via ``SplitsFoldProvider``, so
it can be fully verified with hand-computed fakes before P2/P4/P5 land — which is the only
honest way to prove the harness *measures what it claims*. The named leakage-control test
(``test_embargo_blocks_leakage``) and the deliberate-lookahead control arm both run through
this same code path.

WHAT IT NEVER DOES:
  * It does not compute folds — that is ``oxbow.backtest.splits``' job, imported, never
    duplicated (plan §8).
  * It does not train on the full corpus and test on a part of it. Every fit (scorer,
    thresholds, rule hits) receives only the fold's train+validation rows; the test rows
    enter only to be scored. A fold where the fit set's timestamps reach into the test set
    past the embargo raises (``test_embargo_blocks_leakage``), rather than reporting a
    flattering number (plan §18: "a model trained on the full corpus then evaluated on part
    of it" is fatal).
  * It does not average metrics across corpora. A run is *one* corpus; the caller produces
    per-corpus results (plan §6/§12: report per corpus, never one headline).
  * It does not invent fold boundaries when the corpus cannot support the configured
    embargo. It records the corpus span, the embargo it was handed and the fold count, and
    if a fold violates the gap it raises — the 18-day-IBM-vs-30-day-embargo tension is
    reported, not papered over (see ``corpus_feasibility``).

MONEY (DEV-005): every amount that crosses this module is integer minor units; probabilities
and ratios are floats. ``scripts/no_float_money.py`` enforces the naming, and the aggregate
dicts it emits carry explicit ``_minor`` integer fields.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final

import polars as pl

from oxbow.backtest import metrics, serialize
from oxbow.backtest.config_io import BacktestConfig
from oxbow.backtest.economics import (
    FoldAccount,
    FoldEconomics,
    fold_alerts_per_10k,
    realized_fold_economics,
)
from oxbow.backtest.fairness import (
    amount_shift_rank_correlation,
    edge_drop_typology_recall_stability,
    fairness_report,
)
from oxbow.backtest.interfaces import (
    COL_ACCOUNT_AGE_DAYS,
    COL_ACCOUNT_KEY,
    COL_ACTIVITY_VOLUME,
    COL_AMOUNT_MINOR,
    COL_AS_OF_TS,
    COL_COMMUNITY_SIZE,
    COL_EXPOSURE_MINOR,
    COL_LABEL,
    COL_LABEL_TYPOLOGY,
    COL_REVIEW_COST_MINOR,
    COL_REVIEW_MINUTES,
    COL_SPEC_HASH,
    REQUIRED_CORPUS_COLUMNS,
    Allocator,
    BacktestError,
    FoldError,
    FoldProvider,
    HarnessFold,
    RuleHitsProvider,
    Scorer,
)
from oxbow.backtest.policies import (
    ALL_POLICIES,
    POLICY_EV_CPSAT,
    POLICY_EV_GREEDY,
    POLICY_THRESHOLD,
    PolicyOutcome,
    run_policy,
)

DEFAULT_POLICIES: Final = (
    POLICY_THRESHOLD,
    POLICY_EV_GREEDY,
    POLICY_EV_CPSAT,
)


class CorpusContractError(BacktestError):
    """Raised when the corpus frame is missing a column the harness is owed."""


@dataclass(frozen=True, slots=True)
class FoldResult:
    """One fold's outcome for one policy, fully reduced to JSON-ready values."""

    fold_index: int
    embargo_days: int
    n_fit_rows: int
    n_scored_rows: int
    n_decisions: int
    n_test_positive: int
    skipped_reason: str | None
    precision: float | None
    recall: float | None
    pr_auc: float | None
    alerts_per_10k: float
    typology_recall: dict[str, float]
    allocator_label: str
    economics: FoldEconomics


@dataclass(frozen=True, slots=True)
class PolicyAggregate:
    """A policy's run-level metrics: per-period ledger, tail risk, optimality, curve."""

    policy: str
    allocator_label: str
    pr_auc: float | None
    pr_auc_ci_low: float | None
    pr_auc_ci_high: float | None
    auroc: float | None
    brier: float | None
    mean_precision_at_budget: float | None
    mean_recall_at_budget: float | None
    net_benefit_per_fold_minor: list[int]
    cumulative_benefit_minor: list[int]
    max_drawdown_minor: int
    zero_drawdown_labelled: bool
    net_benefit_total_minor: int
    benefit_per_analyst_hour_minor: int
    risk_adjusted_ratio: float
    risk_adjusted_ratio_label: str
    risk_adjusted_ratio_formula: str
    risk_adjusted_ratio_is_sharpe: bool
    var95_mean_minor: int
    es975_mean_minor: int
    var95_reduction_vs_threshold_minor: int
    es975_reduction_vs_threshold_minor: int
    optimality_gap_minor: int | None
    mean_alerts_per_10k: float
    reliability_curve: list[dict[str, float | int]]
    folds: list[FoldResult]


@dataclass(frozen=True, slots=True)
class VariantResult:
    """One ablation variant across every fold and policy, plus its fairness block.

    ``provenance`` labels whether the numbers came from a hand-computed fake harness
    (``fake_harness``) or a real corpus run (``real_corpus``). Nothing in the report may
    present a fake-derived figure as a result (00 §B), so this field travels with every
    emitted number and is asserted against in the tests.
    """

    label: str
    corpus: str
    provenance: str
    is_control: bool
    control_note: str | None
    question: str
    row_id: str
    fold_count: int
    n_total_rows: int
    n_total_positive: int
    base_rate: float
    embargo_days: int
    optimised_split: str
    policies: dict[str, PolicyAggregate]
    fairness: dict[str, Any]
    perturbations: dict[str, Any]
    seed_stability: dict[str, Any]
    model_version: str
    feature_spec_hash: str

    def primary(self, config: BacktestConfig) -> PolicyAggregate | None:
        """The policy the fairness/perturbation block and the headline attach to."""
        for name in (POLICY_EV_GREEDY, POLICY_THRESHOLD):
            if name in self.policies:
                return self.policies[name]
        del config
        return next(iter(self.policies.values()), None)


@dataclass(frozen=True, slots=True)
class BacktestRun:
    """The full run: one corpus, every variant, and the overfitting-control ledger.

    ``configs_evaluated`` counts the arms run against this corpus; plan §12's
    multiple-testing caveat is derived from it, and ``test_fold_touched_at`` is the single
    timestamp recording that the untouched test fold was read exactly once.
    """

    corpus: str
    config: BacktestConfig
    variants: list[VariantResult]
    configs_evaluated: int
    test_fold_touched_once: bool
    test_fold_touched_at: str
    mlflow: dict[str, Any]
    split_report_line: str
    feasibility: dict[str, Any]
    generated_by: str = "oxbow.backtest.harness"

    def to_dict(self) -> dict[str, Any]:
        """Render the run as a JSON-safe, byte-stable payload for the validation page.

        The payload carries one wall-clock field (``test_fold_touched_at``), which is
        required by plan §12 (the test fold is touched once, timestamped) and therefore
        differs between two runs of identical input. So the *content hash* is taken over a
        copy with the volatile timestamp and the mlflow block removed — exactly the pattern
        ``ScorecardModel.canonical_json`` uses — so ``make verify-determinism`` compares the
        substance of the run, not the moment it was written.
        """
        payload: dict[str, Any] = {
            "artifact": "oxbow-backtest-v1",
            "corpus": self.corpus,
            "disclaimer": serialize.DISCLAIMER,
            "seed": self.config.seed,
            "optimised_split": self.config.optimised_split,
            "which_split_was_optimised_on": self.split_report_line,
            "overfitting_controls": {
                "configs_evaluated": self.configs_evaluated,
                "selection_on": "validation",
                "test_fold_touched_once": self.test_fold_touched_once,
                "test_fold_touched_at": self.test_fold_touched_at,
                "multiple_testing_caveat": (
                    f"{self.configs_evaluated} configurations were evaluated against this "
                    "corpus; the best validation result among them is optimistically biased, "
                    "which is precisely why the headline is taken from the untouched test "
                    "fold, not from the validation peak."
                ),
            },
            "corpus_feasibility": self.feasibility,
            "mlflow": self.mlflow,
            "variants": [variant_to_dict(v) for v in self.variants],
        }
        content = {key: value for key, value in payload.items() if key != "mlflow"}
        content["overfitting_controls"] = {
            key: value
            for key, value in content["overfitting_controls"].items()
            if key != "test_fold_touched_at"
        }
        payload["content_sha256"] = serialize.stable_sha256(content)
        return payload


def validate_corpus(corpus: pl.DataFrame) -> str:
    """Check the corpus carries every owed column and one feature spec; return the hash.

    Fails loud naming the missing column (03 §A rule 1), because a corpus missing
    ``exposure_minor`` would make every net-benefit figure silently wrong rather than
    erroring.
    """
    missing = [name for name in REQUIRED_CORPUS_COLUMNS if name not in corpus.columns]
    if missing:
        raise CorpusContractError(f"corpus is missing required column(s): {missing}")
    if corpus.get_column(COL_LABEL).null_count():
        raise CorpusContractError("label column has nulls; an unknown label is not a zero")
    hashes = set(corpus.get_column(COL_SPEC_HASH).unique().to_list())
    if len(hashes) != 1:
        raise CorpusContractError(
            f"corpus carries {len(hashes)} distinct {COL_SPEC_HASH} values; one backtest is one feature spec"
        )
    return next(iter(hashes))


def assert_fold_discipline(
    corpus: pl.DataFrame, fold: HarnessFold, *, as_of_column: str = COL_AS_OF_TS
) -> None:
    """Verify a fold is temporal and embargo-respecting, or raise with the arithmetic.

    THREE THINGS THIS CHECKS, all plan §12 rejection triggers if violated:
      1. No row is in more than one of train/validation/test (masks disjoint).
      2. Every fit row's as-of is at-or-before every test row's as-of (temporal, never a
         random shuffle).
      3. The gap between the last fit as-of and the first test as-of is at least the
         embargo, so no feature window straddles the boundary into the scored period.
    """
    height = corpus.height
    for name, mask in (
        ("train", fold.train_mask),
        ("validation", fold.validation_mask),
        ("test", fold.test_mask),
    ):
        if len(mask) != height:
            raise FoldError(f"fold {fold.index}: {name}_mask length {len(mask)} != corpus {height}")
    overlap = [
        i
        for i in range(height)
        if (fold.train_mask[i] + fold.validation_mask[i] + fold.test_mask[i]) > 1
    ]
    if overlap:
        raise FoldError(
            f"fold {fold.index}: rows {overlap[:5]} land in more than one split; masks must be disjoint"
        )
    fit_rows = [i for i in range(height) if fold.train_mask[i] or fold.validation_mask[i]]
    test_rows = [i for i in range(height) if fold.test_mask[i]]
    if not test_rows:
        raise FoldError(
            f"fold {fold.index}: empty test window; a fold with nothing to score is a config fault"
        )
    stamps = corpus.get_column(as_of_column).to_list()
    fit_max = max((stamps[i] for i in fit_rows), default=None)
    test_min = min(stamps[i] for i in test_rows)
    if fit_max is not None and fit_max > test_min:
        raise FoldError(
            f"fold {fold.index}: a fit row at {fit_max} is dated after the test start {test_min}; "
            "this is a random split, which plan §12 forbids"
        )
    if fit_max is not None:
        gap_days = (test_min - fit_max).total_seconds() / 86_400.0
        if gap_days < fold.embargo_days - _SLACK_DAYS:
            raise FoldError(
                f"fold {fold.index}: embargo gap is {gap_days:.2f}d but the embargo is "
                f"{fold.embargo_days}d; a feature window from a fit row would read the scored "
                "period — leakage that must block, not pass (test_embargo_blocks_leakage)"
            )


_SLACK_DAYS: Final = 1e-6


def corpus_feasibility(
    corpus: pl.DataFrame, config: BacktestConfig, fold_count: int
) -> dict[str, Any]:
    """Report whether the corpus can *honestly* support the configured embargo + folds.

    THE HONEST CAVEAT (plan §12 / DEV-013): the IBM-AML window is 18 days of a
    synthetic-scenario corpus, and the configured embargo is 30 days (the longest feature
    lookback). A 30-day embargo cannot fit inside an 18-day timeline, so a literal 5-fold
    walk-forward with the full embargo is infeasible on IBM. This function does not invent
    data to make the arithmetic work: it states the span, the requested embargo, the
    maximum number of non-overlapping embargoed folds the span supports, and whether the
    actual fold count fits. A caller reporting IBM must run it as a single holdout, or run
    the multi-fold demonstration on the longer PaySim window and label which is which.
    """
    stamps = corpus.get_column(COL_AS_OF_TS).to_list()
    low, high = min(stamps), max(stamps)
    span_days = (high - low).total_seconds() / 86_400.0
    embargo = config.embargo_days
    feasible = span_days >= embargo * 2 and fold_count >= 1
    # A fold needs at least embargo + test space after the previous fold; the crude bound
    # is that the timeline must exceed two embargoes to have one honest train/test gap.
    max_embargoed_gaps = int(span_days // embargo) if embargo > 0 else 0
    return {
        "corpus_span_days": round(span_days, 4),
        "requested_embargo_days": embargo,
        "folds_supplied": fold_count,
        "max_full_embargo_gaps_in_span": max_embargoed_gaps,
        "supports_literal_walk_forward": bool(feasible and fold_count <= max_embargoed_gaps),
        "note": (
            "If supports_literal_walk_forward is False the run is reported as a "
            "single-holdout (or on a corpus whose window the embargo fits), never as a "
            "5-fold result invented from an insufficient timeline. The IBM-AML 18-day "
            "window against a 30-day embargo is exactly this case (DEV-013)."
        ),
    }


def _fold_accounts(
    corpus: pl.DataFrame,
    fold: HarnessFold,
    *,
    spec_hash: str,
    scorer: Scorer,
    rule_hits: RuleHitsProvider | None,
    seed: int,
) -> tuple[list[FoldAccount], int, dict[str, float], str]:
    """Score one fold's test rows and build the harness's decision view: (decisions, rows, ...).

    The scorer is fitted on the fold's train+validation frames and asked to predict only
    the test frame, so its output never sees a test label it could leak (the honest arms);
    the deliberate-lookahead *control* arm is a different injected scorer that does, which
    is exactly why the control outperforms and why its presence proves the harness bites.

    The corpus is one row per (account, as-of): an account that moved money at four
    timestamps inside one fold has four rows. The harness's unit is the opposite -- one
    review decision per account per fold, which is what `ScoreResult.p_of` returns, what
    the analyst capacity is booked against, and what the warehouse `score` table and the
    packet both name. So the rows are collapsed here, once, by the rule in
    :func:`_decisions_per_account`, and the second return value is how many rows the fold
    actually scored. (DEV-026: collapsing anywhere later double-booked one account's
    minutes as many times as it had rows, and the capacity postcondition was right to
    refuse the run.)
    """
    train = corpus.filter(pl.Series(fold.train_mask))
    validation = corpus.filter(pl.Series(fold.validation_mask))
    test = corpus.filter(pl.Series(fold.test_mask))
    result = scorer.score(
        train=train, validation=validation, scored=test, feature_spec_hash=spec_hash, seed=seed
    )
    accounts_present = test.get_column(COL_ACCOUNT_KEY).to_list()
    severity: dict[str, float] = {}
    if rule_hits is not None:
        hits = rule_hits.rule_hits(fold_index=fold.index, accounts=accounts_present)
        severity = {key: hit.severity for key, hit in hits.items()}
    rows: list[FoldAccount] = []
    stamps: list[Any] = []
    columns = test.to_dicts()
    for row in columns:
        account = str(row[COL_ACCOUNT_KEY])
        rows.append(
            FoldAccount(
                account_key=account,
                label=int(row[COL_LABEL]),
                exposure_minor=int(row[COL_EXPOSURE_MINOR]),
                review_cost_minor=int(row[COL_REVIEW_COST_MINOR]),
                review_minutes=int(row[COL_REVIEW_MINUTES]),
                amount_minor=int(row[COL_AMOUNT_MINOR]),
                p_calibrated=result.p_of(account),
                typology=_optional_str(row.get(COL_LABEL_TYPOLOGY)),
                account_age_days=_optional_int(row.get(COL_ACCOUNT_AGE_DAYS)),
                activity_volume=_optional_int(row.get(COL_ACTIVITY_VOLUME)),
                community_size=_optional_int(row.get(COL_COMMUNITY_SIZE)),
            )
        )
        stamps.append(row[COL_AS_OF_TS])
    return (
        _decisions_per_account(rows, stamps),
        len(rows),
        severity,
        result.model_version,
    )


def _decisions_per_account(rows: list[FoldAccount], stamps: list[Any]) -> list[FoldAccount]:
    """Collapse a fold's scored rows to one decision per account, by a stated rule.

    * The **latest as-of** row carries the money state (exposure, minutes, cost, amount).
      A rolling 24-hour exposure is the account's position at that stamp; summing the
      stamps would count the same euro in four windows, and the oldest stamp is stale by
      the time the analyst reaches the queue.
    * The **label is positive if any stamp in the fold was positive**. An account that
      committed fraud once in the period is a positive for the period; taking the last
      stamp's label would let a fraud followed by three clean movements grade as clean.
    * The probability comes from `ScoreResult.p_of`, which is already per account, so it
      cannot disagree with the row chosen above.
    * Order is first-appearance, which under the corpus's total order
      `(as_of_ts, account_key)` is the order the accounts entered the fold.

    The stamps must arrive in non-decreasing order per account; a corpus that does not
    hold that raises instead of silently choosing the wrong row as "latest".
    """
    decisions: dict[str, FoldAccount] = {}
    last_stamp: dict[str, Any] = {}
    for row, stamp in zip(rows, stamps, strict=True):
        previous_stamp = last_stamp.get(row.account_key)
        if previous_stamp is not None and stamp < previous_stamp:
            raise FoldError(
                f"fold rows for {row.account_key!r} arrive out of as-of order "
                f"({previous_stamp} then {stamp}); 'latest stamp' would name an arbitrary "
                "row. The corpus is written sorted by (as_of_ts, account_key) -- check the "
                "score stage's landing order before trusting this fold."
            )
        last_stamp[row.account_key] = stamp
        existing = decisions.get(row.account_key)
        if existing is None:
            decisions[row.account_key] = row
            continue
        decisions[row.account_key] = replace(
            row, label=1 if (existing.label == 1 or row.label == 1) else 0
        )
    return list(decisions.values())


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _fold_result(
    fold: HarnessFold,
    accounts: list[FoldAccount],
    outcome: PolicyOutcome,
    *,
    n_scored_rows: int,
    config: BacktestConfig,
    seed: int,
) -> FoldResult:
    """Reduce one fold + one policy to the reported metrics, honouring undefined cases."""
    labels = [a.label for a in accounts]
    probabilities = [a.p_calibrated for a in accounts]
    keys = [a.account_key for a in accounts]
    typologies = [a.typology for a in accounts]
    n_test_positive = sum(labels)
    reviewed_set = set(outcome.reviewed)

    economics = realized_fold_economics(
        accounts,
        outcome.reviewed,
        recovery_rate=config.recovery_rate,
        friction_cost_minor=config.friction_cost_minor,
        capacity_minutes=config.capacity_minutes,
        currency=config.currency,
        mc_draws=config.mc_draws,
        mc_seed=seed,
        var_alpha=config.var_alpha,
        es_alpha=config.es_alpha,
        expected_value_minor=outcome.expected_ev_minor,
    )

    # Precision/recall are UNDEFINED, not zero, when nothing was reviewed or the fold has
    # no positives (plan §12: test_no_alerts_fold_undefined_not_zero, test_zero_positive_fold_reported).
    if not reviewed_set or economics.accounts_reviewed == 0:
        precision: float | None = None
    else:
        precision = economics.true_positives / economics.accounts_reviewed
    recall = economics.true_positives / n_test_positive if n_test_positive > 0 else None
    try:
        pr_auc = metrics.pr_auc(probabilities, labels, keys)
    except ValueError:
        pr_auc = None

    alerts_per_10k = (
        fold_alerts_per_10k(economics.accounts_reviewed, len(accounts)) if accounts else 0.0
    )
    typology_recall = metrics.typology_recall_membership(labels, typologies, keys, reviewed_set)
    return FoldResult(
        fold_index=fold.index,
        embargo_days=fold.embargo_days,
        n_fit_rows=sum(fold.train_mask) + sum(fold.validation_mask),
        n_scored_rows=n_scored_rows,
        n_decisions=len(accounts),
        n_test_positive=n_test_positive,
        skipped_reason=(
            "zero_positive_fold: PR-AUC undefined, reported as skipped not scored"
            if n_test_positive == 0
            else None
        ),
        precision=precision,
        recall=recall,
        pr_auc=pr_auc,
        alerts_per_10k=alerts_per_10k,
        typology_recall=typology_recall,
        allocator_label=outcome.allocator_label,
        economics=economics,
    )


def _aggregate_policy(
    policy: str,
    fold_results: list[FoldResult],
    pooled: tuple[list[float], list[int], list[str], list[str | None]],
    config: BacktestConfig,
    *,
    threshold_es_by_fold: dict[int, int] | None = None,
    threshold_var_by_fold: dict[int, int] | None = None,
    greedy_ev_total: int | None = None,
) -> PolicyAggregate:
    """Fold results + pooled scored rows -> the run-level policy record."""
    probabilities, labels, keys, _ = pooled
    net_per_fold = [r.economics.net_benefit_minor for r in fold_results]
    cumulative = metrics.cumulative_benefit(net_per_fold)
    drawdown = metrics.max_drawdown_minor(cumulative)
    total_minutes = sum(r.economics.minutes_used for r in fold_results)
    total_net = sum(net_per_fold)
    pr_auc_value: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    if sum(labels) > 0 and len(labels) > 0:
        pr_auc_value = metrics.pr_auc(probabilities, labels, keys)
        ci = metrics.bootstrap_ci(
            probabilities,
            labels,
            keys,
            lambda p, y, k: metrics.pr_auc(p, y, k),
            resamples=config.bootstrap_resamples,
            seed=config.seed,
            confidence=config.bootstrap_confidence,
        )
        ci_low, ci_high = ci.low, ci.high
    try:
        auroc_value = metrics.auroc(probabilities, labels) if sum(labels) else None
    except ValueError:
        auroc_value = None
    brier_value = metrics.brier_score(probabilities, labels) if labels else None
    precision_values = [r.precision for r in fold_results if r.precision is not None]
    recall_values = [r.recall for r in fold_results if r.recall is not None]
    mean_precision = sum(precision_values) / len(precision_values) if precision_values else None
    mean_recall = sum(recall_values) / len(recall_values) if recall_values else None
    ratio = metrics.risk_adjusted_ratio(net_per_fold) if len(net_per_fold) >= 2 else float("nan")

    es_by_fold = {r.fold_index: r.economics.tail.es_minor for r in fold_results}
    var_by_fold = {r.fold_index: r.economics.tail.var_minor for r in fold_results}
    es_reduction = 0
    var_reduction = 0
    if threshold_es_by_fold is not None:
        es_reduction = sum(
            threshold_es_by_fold.get(idx, 0) - es_by_fold.get(idx, 0) for idx in es_by_fold
        ) // max(len(es_by_fold), 1)
    if threshold_var_by_fold is not None:
        var_reduction = sum(
            threshold_var_by_fold.get(idx, 0) - var_by_fold.get(idx, 0) for idx in var_by_fold
        ) // max(len(var_by_fold), 1)

    optimality_gap: int | None = None
    if policy == POLICY_EV_CPSAT and greedy_ev_total is not None:
        optimality_gap = (
            sum(r.economics.expected_value_minor for r in fold_results) - greedy_ev_total
        )

    return PolicyAggregate(
        policy=policy,
        allocator_label=fold_results[0].allocator_label if fold_results else policy,
        pr_auc=pr_auc_value,
        pr_auc_ci_low=ci_low,
        pr_auc_ci_high=ci_high,
        auroc=auroc_value,
        brier=brier_value,
        mean_precision_at_budget=mean_precision,
        mean_recall_at_budget=mean_recall,
        net_benefit_per_fold_minor=net_per_fold,
        cumulative_benefit_minor=cumulative,
        max_drawdown_minor=drawdown,
        zero_drawdown_labelled=drawdown == 0,
        net_benefit_total_minor=total_net,
        benefit_per_analyst_hour_minor=(
            metrics.benefit_per_analyst_hour_minor(total_net, total_minutes) if total_minutes else 0
        ),
        risk_adjusted_ratio=ratio,
        risk_adjusted_ratio_label=config.benefit_ratio.label,
        risk_adjusted_ratio_formula=config.benefit_ratio.formula,
        risk_adjusted_ratio_is_sharpe=config.benefit_ratio.is_sharpe_ratio,
        var95_mean_minor=sum(var_by_fold.values()) // max(len(var_by_fold), 1),
        es975_mean_minor=sum(es_by_fold.values()) // max(len(es_by_fold), 1),
        var95_reduction_vs_threshold_minor=var_reduction,
        es975_reduction_vs_threshold_minor=es_reduction,
        optimality_gap_minor=optimality_gap,
        mean_alerts_per_10k=(
            sum(r.alerts_per_10k for r in fold_results) / len(fold_results) if fold_results else 0.0
        ),
        reliability_curve=(
            metrics.reliability_curve(probabilities, labels, config.reliability_bins)
            if probabilities
            else []
        ),
        folds=fold_results,
    )


def run_variant(
    *,
    label: str,
    corpus: pl.DataFrame,
    corpus_name: str,
    provenance: str,
    question: str,
    fold_provider: FoldProvider,
    scorer: Scorer,
    config: BacktestConfig,
    policies: Sequence[str] = DEFAULT_POLICIES,
    allocator: Allocator | None = None,
    rule_hits: RuleHitsProvider | None = None,
    is_control: bool = False,
    control_note: str | None = None,
    row_id: str = "",
    seed: int | None = None,
) -> VariantResult:
    """Run every fold for every requested policy and assemble one ablation row's detail."""
    base_seed = config.seed if seed is None else seed
    spec_hash = validate_corpus(corpus)
    folds = list(fold_provider.folds(corpus))
    if not folds:
        raise FoldError("the fold provider yielded no folds; refusing to report an empty run")
    for fold in folds:
        assert_fold_discipline(corpus, fold)

    # Score each fold once (the honest scorer path), reused by every policy over it.
    fold_accounts: dict[int, list[FoldAccount]] = {}
    fold_severity: dict[int, dict[str, float]] = {}
    fold_scored_rows: dict[int, int] = {}
    model_version = ""
    for fold in folds:
        accounts, scored_rows, severity, version = _fold_accounts(
            corpus, fold, spec_hash=spec_hash, scorer=scorer, rule_hits=rule_hits, seed=base_seed
        )
        fold_accounts[fold.index] = accounts
        fold_scored_rows[fold.index] = scored_rows
        fold_severity[fold.index] = severity
        model_version = model_version or version

    # Threshold baseline tail per fold, computed first so other policies can report
    # their VaR/ES *reduction versus the threshold baseline* (plan §12 tail comparison).
    threshold_fold_results = _run_policies(
        [POLICY_THRESHOLD],
        folds,
        fold_accounts,
        fold_severity,
        fold_scored_rows,
        config,
        base_seed,
        allocator,
    )
    threshold_es = {
        r.fold_index: r.economics.tail.es_minor for r in threshold_fold_results[POLICY_THRESHOLD]
    }
    threshold_var = {
        r.fold_index: r.economics.tail.var_minor for r in threshold_fold_results[POLICY_THRESHOLD]
    }
    greedy_ev_total = None
    if POLICY_EV_GREEDY in policies and POLICY_EV_CPSAT in policies:
        greedy_fold = _run_policies(
            [POLICY_EV_GREEDY],
            folds,
            fold_accounts,
            fold_severity,
            fold_scored_rows,
            config,
            base_seed,
            allocator,
        )
        greedy_ev_total = sum(
            r.economics.expected_value_minor for r in greedy_fold[POLICY_EV_GREEDY]
        )

    aggregated: dict[str, PolicyAggregate] = {}
    for policy in policies:
        results = _run_policies(
            [policy],
            folds,
            fold_accounts,
            fold_severity,
            fold_scored_rows,
            config,
            base_seed,
            allocator,
        )
        fold_results = results[policy]
        pooled = _pooled_rows(fold_accounts, folds)
        aggregated[policy] = _aggregate_policy(
            policy,
            fold_results,
            pooled,
            config,
            threshold_es_by_fold=threshold_es,
            threshold_var_by_fold=threshold_var,
            greedy_ev_total=greedy_ev_total,
        )

    primary = (
        aggregated.get(POLICY_EV_GREEDY)
        or aggregated.get(POLICY_THRESHOLD)
        or next(iter(aggregated.values()))
    )
    all_accounts = [a for fold in folds for a in fold_accounts[fold.index]]
    reviewed_pool = {key for r in primary.folds for key in r.economics.reviewed}
    fairness_payload = fairness_to_dict(fairness_report(all_accounts, reviewed_pool))
    perturbations = _perturbations(all_accounts, reviewed_pool, primary, config, base_seed)
    stability = _seed_stability(corpus, folds, primary.policy, config, spec_hash, scorer, rule_hits)

    return VariantResult(
        label=label,
        corpus=corpus_name,
        provenance=provenance,
        is_control=is_control,
        control_note=control_note,
        question=question,
        row_id=row_id,
        fold_count=len(folds),
        n_total_rows=corpus.height,
        n_total_positive=int(corpus.get_column(COL_LABEL).sum()),
        base_rate=float(corpus.get_column(COL_LABEL).mean() or 0.0),
        embargo_days=folds[0].embargo_days,
        optimised_split=config.optimised_split,
        policies=aggregated,
        fairness=fairness_payload,
        perturbations=perturbations,
        seed_stability=stability,
        model_version=model_version,
        feature_spec_hash=spec_hash,
    )


def _run_policies(
    policies: Sequence[str],
    folds: list[HarnessFold],
    fold_accounts: dict[int, list[FoldAccount]],
    fold_severity: dict[int, dict[str, float]],
    fold_scored_rows: dict[int, int],
    config: BacktestConfig,
    seed: int,
    allocator: Allocator | None,
) -> dict[str, list[FoldResult]]:
    out: dict[str, list[FoldResult]] = {policy: [] for policy in policies}
    for policy in policies:
        for fold in folds:
            accounts = fold_accounts[fold.index]
            outcome = run_policy(
                policy,
                accounts,
                severity_by_account=fold_severity[fold.index],
                capacity_minutes=config.capacity_minutes,
                allocator=allocator,
                seed=seed + fold.index,
                recovery_rate=config.recovery_rate,
                friction_cost_minor=config.friction_cost_minor,
                currency=config.currency,
            )
            out[policy].append(
                _fold_result(
                    fold,
                    accounts,
                    outcome,
                    n_scored_rows=fold_scored_rows[fold.index],
                    config=config,
                    seed=seed,
                )
            )
    return out


def _pooled_rows(
    fold_accounts: dict[int, list[FoldAccount]],
    folds: list[HarnessFold],
) -> tuple[list[float], list[int], list[str], list[str | None]]:
    probabilities: list[float] = []
    labels: list[int] = []
    keys: list[str] = []
    typologies: list[str | None] = []
    for fold in folds:
        for account in fold_accounts[fold.index]:
            probabilities.append(account.p_calibrated)
            labels.append(account.label)
            keys.append(account.account_key)
            typologies.append(account.typology)
    return probabilities, labels, keys, typologies


def _perturbations(
    accounts: list[FoldAccount],
    reviewed: set[str],
    primary: PolicyAggregate,
    config: BacktestConfig,
    seed: int,
) -> dict[str, Any]:
    nominal_fold_keys = [r.fold_index for r in primary.folds]
    cutoff_rank = max(len(reviewed) // max(len(nominal_fold_keys), 1), 1)
    plus = amount_shift_rank_correlation(
        accounts,
        recovery_rate=config.recovery_rate,
        friction_cost_minor=config.friction_cost_minor,
        shift_ratio=0.10,
        cutoff_rank=cutoff_rank,
        currency=config.currency,
    )
    minus = amount_shift_rank_correlation(
        accounts,
        recovery_rate=config.recovery_rate,
        friction_cost_minor=config.friction_cost_minor,
        shift_ratio=-0.10,
        cutoff_rank=cutoff_rank,
        currency=config.currency,
    )
    edges = edge_drop_typology_recall_stability(accounts, reviewed, drop_ratio=0.10, seed=seed)
    return {
        "amount_shift_plus10": {
            "shift_ratio": plus.shift_ratio,
            "spearman": plus.spearman,
            "reordered_at_cutoff": plus.reordered_at_cutoff,
        },
        "amount_shift_minus10": {
            "shift_ratio": minus.shift_ratio,
            "spearman": minus.spearman,
            "reordered_at_cutoff": minus.reordered_at_cutoff,
        },
        "edge_drop_typology_recall": {
            "drop_ratio": edges.drop_ratio,
            "seed": edges.seed,
            "max_abs_shift": edges.max_abs_shift,
            "per_typology": [
                {
                    "typology": s.typology,
                    "nominal_recall": s.nominal_recall,
                    "dropped_recall": s.dropped_recall,
                    "abs_shift": s.abs_shift,
                }
                for s in edges.per_typology
            ],
            "caveat": edges.caveat,
        },
    }


def _seed_stability(
    corpus: pl.DataFrame,
    folds: list[HarnessFold],
    policy: str,
    config: BacktestConfig,
    spec_hash: str,
    scorer: Scorer,
    rule_hits: RuleHitsProvider | None,
) -> dict[str, Any]:
    """Re-score under each configured seed; report mean ± sd of pooled PR-AUC.

    Plan §12 wants "seed stability across 5 seeds as mean ± sd, not a lucky run". Each
    seed re-runs the scorer (a real P4 model retrains with the seed and its p moves; a
    fake that ignores the seed yields sd 0, which the provenance label discloses). PR-AUC
    is the stability target because it is the primary metric and does not depend on the
    allocator, so the pass isolates model-seed sensitivity from allocation.
    """
    values: list[float] = []
    used_seeds: list[int] = []
    for seed_value in config.stability_seeds:
        probabilities: list[float] = []
        labels: list[int] = []
        keys: list[str] = []
        for fold in folds:
            accounts, _n_rows, _severity, _version = _fold_accounts(
                corpus,
                fold,
                spec_hash=spec_hash,
                scorer=scorer,
                rule_hits=rule_hits,
                seed=seed_value,
            )
            for account in accounts:
                probabilities.append(account.p_calibrated)
                labels.append(account.label)
                keys.append(account.account_key)
        if sum(labels) == 0:
            continue
        try:
            values.append(metrics.pr_auc(probabilities, labels, keys))
            used_seeds.append(seed_value)
        except ValueError:
            continue
    if not values:
        return {"available": False, "reason": "no stability seed produced a positive fold"}
    array = pl.Series(values).to_numpy()
    return {
        "available": True,
        "metric": "pr_auc",
        "policy": policy,
        "seeds": used_seeds,
        "values": [float(v) for v in values],
        "mean": float(array.mean()),
        "std": float(array.std(ddof=1)) if len(values) > 1 else 0.0,
    }


def fairness_to_dict(report: Any) -> dict[str, Any]:
    """Serialise a ``FairnessReport`` to JSON-safe dicts (used by the run payload)."""
    return {
        "protected_attributes_note": report.protected_attributes_note,
        "axes": [
            {
                "axis": axis.axis,
                "available": axis.available,
                "reason": axis.reason,
                "buckets": [
                    {
                        "bucket": b.bucket,
                        "n_accounts": b.n_accounts,
                        "n_clean": b.n_clean,
                        "n_false_positives": b.n_false_positives,
                        "false_positive_rate": b.false_positive_rate,
                    }
                    for b in axis.buckets
                ],
            }
            for axis in report.axes
        ],
    }


def variant_to_dict(variant: VariantResult) -> dict[str, Any]:
    """Render a ``VariantResult`` (one ablation row) into the JSON payload shape."""
    return {
        "row_id": variant.row_id,
        "label": variant.label,
        "corpus": variant.corpus,
        "provenance": variant.provenance,
        "is_control": variant.is_control,
        "control_note": variant.control_note,
        "question": variant.question,
        "fold_count": variant.fold_count,
        "n_total_rows": variant.n_total_rows,
        "n_total_positive": variant.n_total_positive,
        "base_rate": variant.base_rate,
        "embargo_days": variant.embargo_days,
        "optimised_split": variant.optimised_split,
        "model_version": variant.model_version,
        "feature_spec_hash": variant.feature_spec_hash,
        "policies": {name: _policy_to_dict(a) for name, a in variant.policies.items()},
        "fairness": variant.fairness,
        "perturbations": variant.perturbations,
        "seed_stability": variant.seed_stability,
    }


def _policy_to_dict(aggregate: PolicyAggregate) -> dict[str, Any]:
    return {
        "policy": aggregate.policy,
        "allocator_label": aggregate.allocator_label,
        "pr_auc": aggregate.pr_auc,
        "pr_auc_ci": {
            "low": aggregate.pr_auc_ci_low,
            "high": aggregate.pr_auc_ci_high,
            "confidence_note": "bootstrap 95% CI, resamples and seed stored",
        },
        # AUROC is de-emphasised on purpose: reported for comparability only.
        "auroc_comparability_only": aggregate.auroc,
        "brier": aggregate.brier,
        "mean_precision_at_budget": aggregate.mean_precision_at_budget,
        "mean_recall_at_budget": aggregate.mean_recall_at_budget,
        "net_benefit_per_fold_minor": aggregate.net_benefit_per_fold_minor,
        "cumulative_benefit_minor": aggregate.cumulative_benefit_minor,
        "max_drawdown_minor": aggregate.max_drawdown_minor,
        "zero_drawdown_labelled": aggregate.zero_drawdown_labelled,
        "net_benefit_total_minor": aggregate.net_benefit_total_minor,
        "benefit_per_analyst_hour_minor": aggregate.benefit_per_analyst_hour_minor,
        "risk_adjusted_benefit_ratio": {
            "value": aggregate.risk_adjusted_ratio,
            "label": aggregate.risk_adjusted_ratio_label,
            "formula": aggregate.risk_adjusted_ratio_formula,
            "is_sharpe_ratio": aggregate.risk_adjusted_ratio_is_sharpe,
            "not_sharpe_because": "no risk-free rate subtracted, no annualisation",
        },
        "var95_mean_minor": aggregate.var95_mean_minor,
        "es975_mean_minor": aggregate.es975_mean_minor,
        "var95_reduction_vs_threshold_minor": aggregate.var95_reduction_vs_threshold_minor,
        "es975_reduction_vs_threshold_minor": aggregate.es975_reduction_vs_threshold_minor,
        "optimality_gap_minor": aggregate.optimality_gap_minor,
        "mean_alerts_per_10k_accounts": aggregate.mean_alerts_per_10k,
        "reliability_curve": aggregate.reliability_curve,
        "folds": [
            {
                "fold_index": fr.fold_index,
                "embargo_days": fr.embargo_days,
                "n_fit_rows": fr.n_fit_rows,
                "n_scored_rows": fr.n_scored_rows,
                "n_decisions": fr.n_decisions,
                "rows_collapsed_into_decisions": fr.n_scored_rows - fr.n_decisions,
                "n_test_positive": fr.n_test_positive,
                "skipped_reason": fr.skipped_reason,
                "precision": fr.precision,
                "recall": fr.recall,
                "pr_auc": fr.pr_auc,
                "alerts_per_10k_accounts": fr.alerts_per_10k,
                "typology_recall": fr.typology_recall,
                "economics": {
                    "accounts_reviewed": fr.economics.accounts_reviewed,
                    "minutes_used": fr.economics.minutes_used,
                    "captured_value_minor": fr.economics.captured_value_minor,
                    "review_cost_minor": fr.economics.review_cost_minor,
                    "friction_cost_minor": fr.economics.friction_cost_minor,
                    "cost_minor": fr.economics.cost_minor,
                    "net_benefit_minor": fr.economics.net_benefit_minor,
                    "benefit_per_analyst_hour_minor": fr.economics.benefit_per_analyst_hour_minor,
                    "true_positives": fr.economics.true_positives,
                    "false_positives": fr.economics.false_positives,
                    "expected_ev_minor": fr.economics.expected_value_minor,
                    "var95_minor": fr.economics.tail.var_minor,
                    "es975_minor": fr.economics.tail.es_minor,
                    "mc_seed": fr.economics.tail.seed,
                    "mc_draws": fr.economics.tail.draws,
                    "currency": fr.economics.currency,
                },
            }
            for fr in aggregate.folds
        ],
    }


def _utc_now_iso() -> str:
    """Second-resolution UTC timestamp for the touched-once marker."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def assemble_run(
    corpus: pl.DataFrame,
    config: BacktestConfig,
    variants: Sequence[VariantResult],
    *,
    corpus_name: str,
    split_report_line: str,
    mlflow_uri: str | None = None,
) -> BacktestRun:
    """Bundle every variant into the run, stamping the overfitting controls.

    THE TEST FOLD IS TOUCHED ONCE (plan §12): scoring already happened inside
    ``run_variant``, so this timestamp records the single moment the untouched holdout was
    read. ``configs_evaluated`` is the multiple-testing denominator; it is *how many arms
    ran*, and the model-card caveat is derived from it so the headline is understood to come
    from the holdout, not from the best-of-N validation peak.

    MLflow logging is optional and attempted only when a tracking URI is supplied — a unit
    test must not create an ``./mlruns`` directory as a side effect — but the touched-once
    timestamp is always recorded in the payload, degraded or not, because plan §12 requires
    that fact to exist and be visible rather than to live only inside MLflow.
    """
    fold_count = variants[0].fold_count if variants else 0
    touched_at = _utc_now_iso()
    mlflow_status = _log_to_mlflow(
        corpus_name=corpus_name,
        variants=variants,
        config=config,
        touched_at=touched_at,
        uri=mlflow_uri,
    )
    return BacktestRun(
        corpus=corpus_name,
        config=config,
        variants=list(variants),
        configs_evaluated=len(variants),
        test_fold_touched_once=True,
        test_fold_touched_at=touched_at,
        mlflow=mlflow_status,
        split_report_line=split_report_line,
        feasibility=corpus_feasibility(corpus, config, fold_count),
    )


def _log_to_mlflow(
    *,
    corpus_name: str,
    variants: Sequence[VariantResult],
    config: BacktestConfig,
    touched_at: str,
    uri: str | None,
) -> dict[str, Any]:
    """Log the run to MLflow if a URI is given and MLflow imports; never crash the run.

    Graceful degradation (plan §13 / 03 §J): if MLflow is unavailable or the URI is absent,
    the touched-once fact is still in the payload with a named reason, rather than the whole
    backtest failing on a missing tracking server.
    """
    base: dict[str, Any] = {
        "enabled": uri is not None,
        "test_fold_touched_at": touched_at,
        "configs_evaluated": len(variants),
    }
    if uri is None:
        return {
            **base,
            "logged": False,
            "reason": "no tracking URI supplied (degraded, not failed)",
        }
    try:
        import mlflow  # imported lazily so an absent/unconfigured server never breaks import

        mlflow.set_tracking_uri(uri)
        with mlflow.start_run(run_name=f"oxbow-backtest-{corpus_name}"):
            mlflow.log_param("seed", config.seed)
            mlflow.log_param("n_folds", config.n_folds)
            mlflow.log_param("embargo_days", config.embargo_days)
            mlflow.log_param("configs_evaluated", len(variants))
            mlflow.set_tag("test_fold_touched_once", "true")
            mlflow.set_tag("test_fold_touched_at", touched_at)
            for variant in variants:
                primary = variant.primary(config)
                if primary and primary.pr_auc is not None:
                    mlflow.log_metric(f"{variant.label}.pr_auc", primary.pr_auc)
        return {**base, "logged": True, "tracking_uri": uri}
    except Exception as exc:  # degrade with a named reason, never fail the backtest run
        return {
            **base,
            "logged": False,
            "reason": f"mlflow unavailable: {type(exc).__name__}: {exc}",
        }


__all__ = [
    "ALL_POLICIES",
    "DEFAULT_POLICIES",
    "BacktestRun",
    "CorpusContractError",
    "FoldResult",
    "PolicyAggregate",
    "VariantResult",
    "assemble_run",
    "assert_fold_discipline",
    "corpus_feasibility",
    "run_variant",
    "variant_to_dict",
]
