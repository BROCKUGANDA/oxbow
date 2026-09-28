"""The one command that runs all five folds, the ablation table and the leakage control.

`uv run python -m oxbow.backtest.run` is plan §12's "all five folds run from one command".
It wires the injected seams, runs the eight ablation rows plus the deliberate-lookahead
control arm through the harness, checks the leakage control visibly outperforms, and
serialises the result to the JSON the validation page reads.

FAKE vs REAL, stated at the top of every run: with ``--demo-fakes`` (the default, so the
gate is runnable today) every component comes from :mod:`oxbow.backtest.fakes`, and every
emitted figure carries ``provenance="fake_harness"`` — these numbers verify the harness and
are NOT results. Pointing ``--corpus`` at a real per-account parquet swaps in the real
splits module, scorer, allocator and rule-hit providers, and the figures carry
``provenance="real_corpus"``. No fake-derived number is ever labelled a result, which is
the reporting discipline plan §12's whole phase rests on.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import polars as pl

from oxbow.backtest import fakes, serialize
from oxbow.backtest.ablation import (
    ABLATION_ROWS,
    BASELINE_POLICIES,
    ROW_IDS,
    AblationSpec,
    build_ablation,
    check_leakage_control,
)
from oxbow.backtest.config_io import BacktestConfig, load_backtest_config
from oxbow.backtest.harness import VariantResult, assemble_run, validate_corpus
from oxbow.backtest.interfaces import (
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    AccountScore,
    RuleHit,
    ScoreResult,
)
from oxbow.backtest.model_card import build_model_card_payload
from oxbow.backtest.policies import (
    POLICY_EV_CPSAT,
    POLICY_EV_GREEDY,
    POLICY_RULES_ONLY,
    POLICY_THRESHOLD,
)

DEFAULT_OUT_DIR: Final = "out/backtest"
SPLIT_REPORT_LINE: Final = (
    "which_split_was_optimised_on: temporal_walk_forward | expanding-window walk-forward, "
    "purged, embargoed | entity_disjoint is a robustness check, not the headline (spec §7.1)"
)


def _demo_specs(config: BacktestConfig) -> list[AblationSpec]:
    """Build the eight ablation rows plus the leakage-control arm from the fakes.

    Rows 1-7 run on a PaySim-style fake tabular corpus; row 8 runs on a typology-labelled
    fake corpus standing in for IBM-AML, so the per-typology recall block is exercised. Each
    honest row gets a higher-quality fake scorer (a monotone ladder still strictly below the
    leakage control), and the control arm is appended last and flagged ``is_control`` so it
    can never be the headline.
    """
    quality_by_row: dict[str, float] = {
        "rules_only": 0.20,
        "scorecard_only": 0.45,
        "gbm_no_graph": 0.55,
        "gbm_with_graph": 0.68,
        "plus_ifusion": 0.74,
        "full_calibrated": 0.80,
        "threshold_vs_ev": 0.80,
        "full_on_ibm": 0.80,
    }
    policies_by_row: dict[str, tuple[str, ...]] = {
        "rules_only": (POLICY_THRESHOLD,),
        "scorecard_only": (POLICY_THRESHOLD, POLICY_EV_GREEDY),
        "gbm_no_graph": (POLICY_THRESHOLD, POLICY_EV_GREEDY),
        "gbm_with_graph": (POLICY_THRESHOLD, POLICY_EV_GREEDY),
        "plus_ifusion": (POLICY_THRESHOLD, POLICY_EV_GREEDY),
        "full_calibrated": (POLICY_THRESHOLD, POLICY_EV_GREEDY, POLICY_EV_CPSAT),
        "threshold_vs_ev": (POLICY_THRESHOLD, POLICY_EV_GREEDY, POLICY_EV_CPSAT),
        "full_on_ibm": (POLICY_THRESHOLD, POLICY_EV_GREEDY, POLICY_EV_CPSAT),
    }
    specs: list[AblationSpec] = []
    for row_id, label, question in ABLATION_ROWS:
        if row_id == "rules_only":
            scorer: Any = fakes.RulesOnlyScorer()
        else:
            scorer = fakes.HonestSignalScorer(
                model_version=f"fake-{row_id}", quality=quality_by_row[row_id]
            )
        specs.append(
            AblationSpec(
                row_id=row_id,
                label=label,
                question=question,
                scorer=scorer,
                policies=policies_by_row[row_id],
                corpus_name="ibm-aml-fake" if row_id == "full_on_ibm" else "paysim-fake",
            )
        )
    if config.leakage_control_enabled:
        specs.append(
            AblationSpec(
                row_id="leakage_control",
                label=config.leakage_label,
                question="CONTROL: does the harness detect lookahead?",
                scorer=fakes.LeakingLabelScorer(),
                policies=(POLICY_THRESHOLD, POLICY_EV_GREEDY),
                is_control=True,
                control_note=(
                    "Lookahead oracle reading the test label; included only to prove the "
                    "harness detects leakage. Never a shippable configuration or a result."
                ),
            )
        )
    return specs


def _demo_fold_provider(config: BacktestConfig) -> fakes.FakeFoldProvider:
    # One account per day over 500 days, so the fold builder's row-based embargo gap of
    # `embargo_days` rows equals `embargo_days` days in timestamp terms — the honest
    # arithmetic ``assert_fold_discipline`` checks. The 500-day span is chosen precisely
    # because a 5-fold walk-forward with a 30-day embargo cannot fit in PaySim's 30 days or
    # IBM's 18 (DEV-013); the fake demonstration uses a synthetic window that can.
    folds = fakes.make_fold_masks(
        height=500, n_folds=config.n_folds, embargo_days=config.embargo_days
    )
    return fakes.FakeFoldProvider(folds, embargo_days=config.embargo_days)


def run_demo(*, out_dir: Path, mlflow_uri: str | None) -> dict[str, Any]:
    """Execute the fake-harness demonstration and write the validation-page artifacts."""
    config = load_backtest_config()
    corpus = fakes.make_corpus(n_accounts=500, span_days=500, with_typology=True)
    fold_provider = _demo_fold_provider(config)
    allocator = fakes.GreedyAllocator()
    rule_hits = fakes.FakeRuleHits()

    specs = _demo_specs(config)
    variants = build_ablation(
        specs,
        corpus=corpus,
        corpus_name="paysim-fake",
        provenance="fake_harness",
        fold_provider=fold_provider,
        config=config,
        allocator=allocator,
        rule_hits=rule_hits,
    )
    # The IBM row's corpus carries typologies; rebuild that one against the same fake
    # frame (which already has typology) so the recall block populates.
    run = assemble_run(
        corpus,
        config,
        variants,
        corpus_name="paysim-fake+ibm-fake",
        split_report_line=SPLIT_REPORT_LINE,
        mlflow_uri=mlflow_uri,
    )
    control = check_leakage_control(variants, expect_outperforms=config.leakage_expect_outperforms)

    payload = run.to_dict()
    payload["leakage_control"] = {
        "detected": control.detected,
        "control_label": control.control_label,
        "control_pr_auc": control.control_pr_auc,
        "best_honest_label": control.honest_label,
        "best_honest_pr_auc": control.best_honest_pr_auc,
        "message": control.message,
    }
    payload["provenance_note"] = (
        "Every figure in this file came from the hand-computed fake harness "
        "(oxbow.backtest.fakes) and verifies the harness, NOT a corpus result."
    )
    ablation_path = out_dir / "ablation_results.json"
    card_path = out_dir / "model_card.json"
    serialize.write_json(payload, ablation_path)
    serialize.write_json(build_model_card_payload(run), card_path)
    _print_demo(variants, control, config)
    return {"ablation": str(ablation_path.resolve()), "model_card": str(card_path.resolve())}


def run_baseline_only(*, out_dir: Path) -> dict[str, Any]:
    """Run the four mandated baselines identically and write their table (plan §12 #5)."""
    config = load_backtest_config()
    corpus = fakes.make_corpus(n_accounts=500, span_days=500, with_typology=True)
    fold_provider = _demo_fold_provider(config)
    spec = AblationSpec(
        row_id="baselines",
        label="Baselines, backtested identically",
        question="random / score-threshold / rules-only / highest-amount-first",
        scorer=fakes.HonestSignalScorer(),
        policies=BASELINE_POLICIES,
    )
    variants = build_ablation(
        [spec],
        corpus=corpus,
        corpus_name="paysim-fake",
        provenance="fake_harness",
        fold_provider=fold_provider,
        config=config,
        allocator=fakes.GreedyAllocator(),
        rule_hits=fakes.FakeRuleHits(),
    )
    path = out_dir / "baseline_results.json"
    serialize.write_json({"variants": [_variant_row(v) for v in variants]}, path)
    return {"baselines": str(path.resolve())}


class RealLeakageControl:
    """The lookahead control arm for a *real* corpus — it reads the scored label directly.

    ``oxbow.backtest.fakes.LeakingLabelScorer`` is the same idea on the fake spec hash;
    against a real corpus its hash check would refuse before it could cheat, so this variant
    is parameterised with the corpus's own ``feature_spec_hash``. It is NOT a model. It is
    the configuration plan §12's gate requires in order to prove the harness can *see*
    lookahead: its outperformance over the honest arms is the evidence the measurement works,
    and it is never a headline or a shippable result.
    """

    def __init__(self, spec_hash: str) -> None:
        self._spec_hash = spec_hash

    def score(
        self,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> ScoreResult:
        del train, validation, seed
        if feature_spec_hash != self._spec_hash:
            raise fakes.FeatureHashMismatchError(
                "the real-corpus leakage control refuses a feature-hash mismatch too"
            )
        scores: dict[str, AccountScore] = {}
        for row in scored.to_dicts():
            key = str(row[COL_ACCOUNT_KEY])
            leaked = 0.99 if int(row["label_is_fraud"]) == 1 else 0.02
            scores[key] = AccountScore(
                account_key=key,
                p_calibrated=leaked,
                band_observed_rate=leaked,
                band_n=1,
            )
        return ScoreResult(
            scores=scores,
            model_version="real-corpus-control-lookahead",
            feature_spec_hash=self._spec_hash,
        )


class CorpusRuleHits:
    """A real ``RuleHitsProvider`` backed by the corpus's own rule-derived columns.

    The severity and hit count a fold needs are already scored onto each account by the
    P3b rules layer inside the bridge (``rule_severity_max`` / ``rule_hit_count``), so the
    provider reads them off the corpus rather than re-running the rules — which would be a
    second rules implementation. An account with no rule column carries a documented zero
    severity, not a missing row: the harness prices ``rules_only`` against every account it
    hands over, and a silent gap would rank an unaccounted account at nothing by accident.
    """

    def __init__(self, corpus: pl.DataFrame) -> None:
        severities: dict[str, RuleHit] = {}
        for row in corpus.to_dicts():
            key = str(row[COL_ACCOUNT_KEY])
            severity = row.get("rule_severity_max")
            hits = row.get("rule_hit_count")
            severities[key] = RuleHit(
                account_key=key,
                severity=min(1.0, max(0.0, float(severity or 0.0))),
                hit_count=max(0, int(hits or 0)),
            )
        self._by_account = severities

    def rule_hits(self, *, fold_index: int, accounts: Sequence[str]) -> dict[str, RuleHit]:
        del fold_index
        return {
            key: self._by_account.get(key, RuleHit(account_key=key, severity=0.0, hit_count=0))
            for key in accounts
        }


# Each honest row names the fitted object it is measured on, and the policy ladders it runs
# through. The profile is the model (DEV-027: a row labelled "Scorecard only" has to be scored
# by the scorecard); the ladder is the queueing decision, and the threshold-vs-EV row is the
# one row that is deliberately a policy comparison on one model.
#
# "rules_only" has no probability of its own — a rule severity sum is not a price — so its
# queue is chosen by severity (POLICY_RULES_ONLY) while its p column stays the full stack's.
# The payload's ``ablation_caveat`` is generated from this table, so the sentence a reviewer
# reads cannot drift from what the rows actually were.
_REAL_POLICIES_BY_ROW: dict[str, tuple[str, ...]] = {
    "rules_only": (POLICY_RULES_ONLY,),
    "scorecard_only": (POLICY_THRESHOLD, POLICY_EV_GREEDY),
    "gbm_no_graph": (POLICY_THRESHOLD, POLICY_EV_GREEDY),
    "gbm_with_graph": (POLICY_THRESHOLD, POLICY_EV_GREEDY),
    "plus_ifusion": (POLICY_THRESHOLD, POLICY_EV_GREEDY),
    "full_calibrated": (POLICY_THRESHOLD, POLICY_EV_GREEDY, POLICY_EV_CPSAT),
    "threshold_vs_ev": (POLICY_THRESHOLD, POLICY_EV_GREEDY, POLICY_EV_CPSAT),
    "full_on_ibm": (POLICY_THRESHOLD, POLICY_EV_GREEDY, POLICY_EV_CPSAT),
}

ABLATION_PROFILES: dict[str, str] = {
    "rules_only": "calibrated",
    "scorecard_only": "scorecard",
    "gbm_no_graph": "gbm_no_graph",
    "gbm_with_graph": "gbm",
    "plus_ifusion": "fused_uncalibrated",
    "full_calibrated": "calibrated",
    "threshold_vs_ev": "calibrated",
    "full_on_ibm": "calibrated",
}


def _ablation_caveat() -> str:
    """The caveat, generated from the profile table so it cannot disagree with the run.

    A hand-written sentence about what the table measures goes stale the moment a row changes,
    and a stale disclosure is worse than none: it teaches a reviewer to ignore the paragraph.
    These rows are read off :data:`ABLATION_PROFILES` and :data:`_REAL_POLICIES_BY_ROW` on
    every run, so the only way to change what this says is to change what the rows do.
    """
    from oxbow.models.scorer import PROFILES

    shared = sorted(
        row_id
        for row_id, profile in ABLATION_PROFILES.items()
        if any(
            other != row_id and ABLATION_PROFILES[other] == profile for other in ABLATION_PROFILES
        )
    )
    distinct = {PROFILES[profile] for profile in ABLATION_PROFILES.values()}
    return (
        f"Each row is scored by the column its label names, of {len(distinct)} distinct fitted "
        f"channels: {', '.join(f'{row}={ABLATION_PROFILES[row]}' for row in ROW_IDS)}. "
        "Scorecard, GBM-with-graph and GBM-without-graph are three separate models — the "
        "graph-free booster is refitted on the registry groups config/splits.yaml "
        "ablation.graph_feature_groups names — so a difference between those rows is a "
        "measurement of the graph, not of a policy ladder. "
        "The row 'Full system on IBM-AML corpus' was NOT run on the IBM-AML corpus: this run "
        "scores one corpus, and that row is the transfer check still to be made, not a result "
        f"of it. Rows sharing one probability column: {', '.join(shared) or 'none'}; for those, "
        "the discrimination columns are the same measurement and only the economics differ, "
        "because the harness's PR-AUC is taken from the ranking and not from the queue. "
        "The rules-only row has no probability of its own: its queue is ordered by rule "
        "severity, and its p column is the full stack's."
    )


# The artifact the score stage writes next to the feature matrix, and the only place the
# OBSERVED timeline the fold fractions were resolved against is recorded. Reading it is not a
# second source of fold arithmetic — the boundaries still come from `oxbow.backtest.splits`,
# and only from there; this supplies the *observation* the fractions are applied to.
FEATURES_MANIFEST_NAME: Final = "features_manifest.json"

# The per-row fold label the features layer stamps onto the matrix, which the score stage
# carries into the corpus. A column name, not boundary arithmetic: it exists here only to be
# checked against the plan, never to derive a boundary from.
FOLD_COLUMN: Final = "fold"


def _recorded_observed_window(
    corpus_path: Path, *, spec_hash: str, repo: Path
) -> tuple[datetime, datetime, Path] | None:
    """The event-grain window the run that produced this corpus resolved its fractions on.

    ``out/score/<run_id>/backtest_corpus.parquet`` is paired with
    ``out/features/<run_id>/features_manifest.json`` by run id, and the manifest's
    ``report.timeline_start`` / ``report.timeline_end`` are the min/max the score stage handed
    to ``build_walk_forward``. They are NOT the corpus's own min/max: the corpus is
    account-grain and ends ~12 days before the event window does, so re-deriving the span from
    the corpus moves every fold boundary (measured 2026-09-27: fold 0's cutoff lands
    2014-07-06T08:29:21Z against the run's printed 2014-07-09T18:16:52Z).

    Returns None — and the caller says so out loud — when no manifest sits beside this corpus
    or when the one that does carries a different ``feature_spec_hash``, because a foreign
    run's window is worse than no window: it would dress a drifted plan in the authority of a
    recorded one.
    """
    run_id = corpus_path.resolve().parent.name
    candidates = [corpus_path.resolve().parents[2] / "features" / run_id / FEATURES_MANIFEST_NAME]
    candidates.append(repo / "out" / "features" / run_id / FEATURES_MANIFEST_NAME)
    for manifest in candidates:
        if not manifest.is_file():
            continue
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        report = payload.get("report")
        if not isinstance(report, dict):
            continue
        recorded_hash = report.get("spec_hash")
        if recorded_hash is None or recorded_hash != spec_hash:
            # Same run id, different feature spec: the corpus was rebuilt after the features
            # were, so this window describes a different table. Refuse to borrow it.
            print(
                f"[backtest] {manifest} declares spec_hash={recorded_hash} but the corpus "
                f"carries {spec_hash}; its window is not used",
                file=sys.stderr,
            )
            continue
        raw_start, raw_end = report.get("timeline_start"), report.get("timeline_end")
        if raw_start is None or raw_end is None:
            continue
        return (
            datetime.fromisoformat(str(raw_start)),
            datetime.fromisoformat(str(raw_end)),
            manifest,
        )
    return None


def _fold_column_agreement(corpus: pl.DataFrame, plan: Any) -> dict[str, Any]:
    """Does the plan reproduce the corpus's own per-row `fold` label, row for row?

    The features layer stamps each account row with the fold whose scoring cutoff built it, so
    the column is an independent record of the boundaries the producing run used. Agreement is
    the measured proof that this backtest resolved the fractions against the same window;
    drift shows up here as a count instead of quietly changing which rows get scored. Rows are
    visited in the corpus's landed order, which the score stage wrote sorted by
    ``(as_of_ts, account_key, fold)``.
    """
    if FOLD_COLUMN not in corpus.columns:
        return {"available": False, "note": f"corpus has no {FOLD_COLUMN} column to check against"}
    cutoffs = [(fold.index, fold.feature_as_of_ts) for fold in plan.folds]
    stamps = corpus.get_column(COL_AS_OF_TS).to_list()
    labels = corpus.get_column(FOLD_COLUMN).to_list()
    agreeing = 0
    compared = 0
    for stamp, label in zip(stamps, labels, strict=True):
        expected = next((index for index, cutoff in cutoffs if stamp <= cutoff), None)
        if expected is None:
            continue
        compared += 1
        if expected == int(label):
            agreeing += 1
    return {
        "available": True,
        "rows_compared": compared,
        "rows_agreeing": agreeing,
        "agreement_share": round(agreeing / compared, 6) if compared else None,
        "rule": "corpus fold == first fold whose feature_as_of_ts >= as_of_ts",
    }


def _resolve_fold_plan(
    timeline: pl.DataFrame,
    *,
    registry: Any,
    repo: Path,
    corpus_path: Path,
    spec_hash: str,
) -> tuple[Any, dict[str, Any]]:
    """The plan this corpus must be walked on, plus a plain statement of what was resolved where.

    Two plans are built from the same config fractions: one on the corpus's own account-grain
    window and one on the window the producing run recorded. They are compared rather than
    guessed at, because the difference is the defect this function exists to close — on
    run 01M3FZ2GC3J71AYT1QEKPWEDKJ the corpus window moved fold 0's training cutoff from the
    run's printed ``2014-07-09T18:16:52.686449Z`` to ``2014-07-06T08:29:21.239239Z`` and no
    fold boundary landed where the scored bytes did. Identical windows mean nothing is being
    hidden; a missing recording means the corpus window is used and said out loud, never
    silently.
    """
    from oxbow.backtest.splits import build_walk_forward  # the ONE splits module

    stamps = timeline.get_column(COL_AS_OF_TS)
    own_window = (stamps.min(), stamps.max())
    corpus_window_plan = build_walk_forward(
        timeline, registry=registry, config_dir=repo / "config", ts_column=COL_AS_OF_TS
    )
    recorded = _recorded_observed_window(corpus_path, spec_hash=spec_hash, repo=repo)
    if recorded is None:
        print(
            "[backtest] fold plan resolved on the corpus's own window "
            f"{own_window[0]}..{own_window[1]}; no features_manifest.json for run "
            f"{corpus_path.parent.name} carries spec_hash={spec_hash}",
            file=sys.stderr,
        )
        return corpus_window_plan, {
            "window_source": "corpus_min_max",
            "recorded_window": None,
            "corpus_window": [own_window[0].isoformat(), own_window[1].isoformat()],
            "folds_whose_boundaries_would_differ": 0,
            "folds": len(corpus_window_plan.folds),
        }
    start, end, manifest = recorded
    plan = build_walk_forward(
        timeline,
        registry=registry,
        config_dir=repo / "config",
        ts_column=COL_AS_OF_TS,
        observed_window=(start, end),
    )
    differing = sum(
        0
        if (
            own.train_end_ts == rec.train_end_ts
            and own.test_start_ts == rec.test_start_ts
            and own.test_end_ts == rec.test_end_ts
        )
        else 1
        for own, rec in zip(corpus_window_plan.folds, plan.folds, strict=True)
    )
    if differing:
        print(
            f"[backtest] the corpus's own window would move {differing} of "
            f"{len(plan.folds)} folds' boundaries; resolving the fractions on the recorded "
            f"window {start}..{end} from {manifest.name} instead",
            file=sys.stderr,
        )
    return plan, {
        "window_source": str(manifest),
        "recorded_window": [start.isoformat(), end.isoformat()],
        "corpus_window": [own_window[0].isoformat(), own_window[1].isoformat()],
        "folds_whose_boundaries_would_differ": differing,
        "folds": len(plan.folds),
    }


class SharedFoldRuns:
    """One fit per fold, reused by every ablation row that reads a channel of it.

    WHY THIS IS NOT A SHORTCUT. A fold fit is the expensive unit: on this host one costs
    minutes and gigabytes, and the eight honest rows would otherwise ask for it eight times
    and get the same numbers. What the rows measure is a DIFFERENT fitted object each — the
    WOE logistic, the booster on every feature, the booster refitted without the graph
    groups, the meta-learner, the calibrated meta-learner — and all of them are columns of
    the one fold run. So the run is fitted once and each row projects its own column out of
    it (``WalkForwardScorer.scores_from``), which is an ablation; asking the same scorer for
    the same number under eight labels would be the imitation DEV-027 refused to ship.

    The key is the fold's own identity — row count and as-of span of each of the three
    slices, plus the spec hash and seed — so two folds can never share a result by accident,
    and a scorer handed a different slice re-fits.

    The leakage control is NOT run through this: it is a different scorer object with a
    different (cheating) behaviour, and it must stay a separate measurement.
    """

    def __init__(self, scorer: Any) -> None:
        self._scorer = scorer
        self._cache: dict[tuple[Any, ...], Any] = {}
        self.fits = 0

    def fold_run(
        self,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> Any:
        key = (
            feature_spec_hash,
            int(seed),
            _slice_identity(train, "train"),
            _slice_identity(validation, "validation"),
            _slice_identity(scored, "scored"),
        )
        hit = self._cache.get(key)
        if hit is None:
            hit = self._scorer.fold_run(
                train=train,
                validation=validation,
                scored=scored,
                feature_spec_hash=feature_spec_hash,
                seed=seed,
            )
            self._cache[key] = hit
            self.fits += 1
        return hit

    def score(
        self,
        *,
        profile: str,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> Any:
        from oxbow.models.scorer import WalkForwardScorer

        return WalkForwardScorer.scores_from(
            self.fold_run(
                train=train,
                validation=validation,
                scored=scored,
                feature_spec_hash=feature_spec_hash,
                seed=seed,
            ),
            profile=profile,
        )


class ProfileScorer:
    """The harness ``Scorer`` for one ablation row: the shared fold fit, its own column.

    A thin adapter, deliberately. ``run_variant`` only knows the ``Scorer`` protocol, so the
    row's profile is bound here rather than threaded through the harness — which keeps the
    harness free of model-layer vocabulary and keeps the mapping from label to fitted object
    visible in one line of ``run_real``.
    """

    def __init__(self, runs: SharedFoldRuns, profile: str) -> None:
        self._runs = runs
        self.profile = profile

    def score(
        self,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> Any:
        return self._runs.score(
            profile=self.profile,
            train=train,
            validation=validation,
            scored=scored,
            feature_spec_hash=feature_spec_hash,
            seed=seed,
        )


def _slice_identity(frame: pl.DataFrame, name: str) -> tuple[Any, ...]:
    """Height plus as-of span of one fold slice, as a hashable identity."""
    if frame.height == 0:
        return (name, 0, None, None, None)
    stamps = frame.get_column(COL_AS_OF_TS)
    keys = frame.get_column(COL_ACCOUNT_KEY)
    return (name, frame.height, str(stamps.min()), str(stamps.max()), str(keys[:1].item()))


def run_real(
    *, corpus_path: Path, out_dir: Path, mlflow_uri: str | None, root: Path | None = None
) -> dict[str, Any]:
    """Execute the walk-forward on a real per-account corpus and write the artifacts.

    This is the ``--corpus`` path the P6 gate wants: it imports the ONE splits module for
    folds, ``oxbow.models.run``'s runner for the calibrated scorer, and ``oxbow.quant``'s
    solvers for the allocator, then runs the eight honest rows plus the leakage control
    through the same harness the fake demonstration uses. The fold boundaries come from the
    purged/embargoed ``SplitPlan``; if the corpus window cannot support the configured
    embargo the splits module refuses, and that refusal is surfaced by name rather than
    papered over with a shortened lookback (DEV-013).
    """
    from oxbow.backtest.fold_provider import SplitsFoldProvider
    from oxbow.models.config import load_model_config
    from oxbow.models.config import load_split_config as load_model_split_config
    from oxbow.models.run import FoldModelRunner
    from oxbow.models.scorer import FrameRuleHitProvider, WalkForwardScorer
    from oxbow.quant.economics import load_economics
    from oxbow.scoring.config import load_feature_registry, load_scorecard_config

    repo = root or _resolve_repo(corpus_path)

    config = load_backtest_config(repo)
    corpus = pl.read_parquet(corpus_path)
    spec_hash = validate_corpus(corpus)

    registry = load_feature_registry(repo)
    model_cfg = load_model_config(repo)
    scorecard_cfg = load_scorecard_config(repo)
    split_cfg = load_model_split_config(repo)
    economics = load_economics(repo)

    timeline = corpus.select(
        pl.col(COL_AS_OF_TS),
        pl.col(COL_ACCOUNT_KEY).alias("entity"),
    )
    plan, plan_report = _resolve_fold_plan(
        timeline, registry=registry, repo=repo, corpus_path=corpus_path, spec_hash=spec_hash
    )
    fold_column_report = _fold_column_agreement(corpus, plan)
    fold_provider = SplitsFoldProvider(plan, as_of_column=COL_AS_OF_TS)

    def build_fold_scorer() -> WalkForwardScorer:
        runner = FoldModelRunner(
            model_cfg=model_cfg,
            scorecard_cfg=scorecard_cfg,
            split_cfg=split_cfg,
            feature_registry=registry,
            rules_provider=FrameRuleHitProvider(),
            provenance="real_feature_table",
            trial_budget=1,
            explain=False,
            root=repo,
            ablate_feature_groups=config.graph_feature_groups,
        )
        return WalkForwardScorer(runner, embargo_days=plan.embargo_days)

    # One fold fit, shared by every honest row — each row reading the column ITS model made.
    # The graph-free booster is the only extra fit this buys: without it the "without graph
    # features" row would have to be the with-graph model's number under a second label.
    shared_scores = SharedFoldRuns(build_fold_scorer())

    allocator = _p5_allocator(economics)
    rule_hits = CorpusRuleHits(corpus)

    specs: list[AblationSpec] = []
    for row_id, label, question in ABLATION_ROWS:
        specs.append(
            AblationSpec(
                row_id=row_id,
                label=label,
                question=question,
                scorer=ProfileScorer(shared_scores, ABLATION_PROFILES[row_id]),
                policies=_REAL_POLICIES_BY_ROW[row_id],
                corpus_name="real-corpus",
            )
        )
    if config.leakage_control_enabled:
        specs.append(
            AblationSpec(
                row_id="leakage_control",
                label=config.leakage_label,
                question="CONTROL: does the harness detect lookahead on the real corpus?",
                scorer=RealLeakageControl(spec_hash),
                policies=(POLICY_THRESHOLD, POLICY_EV_GREEDY),
                is_control=True,
                control_note=(
                    "Lookahead oracle reading the scored label; proves the harness detects "
                    "leakage. Never a shippable configuration or a result."
                ),
            )
        )

    variants = build_ablation(
        specs,
        corpus=corpus,
        corpus_name="real-corpus",
        provenance="real_corpus",
        fold_provider=fold_provider,
        config=config,
        allocator=allocator,
        rule_hits=rule_hits,
    )
    run = assemble_run(
        corpus,
        config,
        variants,
        corpus_name="real-corpus",
        split_report_line=plan.as_report_line(),
        mlflow_uri=mlflow_uri,
    )
    control = check_leakage_control(variants, expect_outperforms=config.leakage_expect_outperforms)
    payload = run.to_dict()
    payload["leakage_control"] = {
        "detected": control.detected,
        "control_label": control.control_label,
        "control_pr_auc": control.control_pr_auc,
        "best_honest_label": control.honest_label,
        "best_honest_pr_auc": control.best_honest_pr_auc,
        "message": control.message,
    }
    payload["ablation_caveat"] = _ablation_caveat()
    payload["ablation_profiles"] = dict(ABLATION_PROFILES)
    payload["provenance_note"] = (
        "Figures came from a real per-account corpus run through the P4b scorer, the P5 "
        "allocator and the ONE splits module; provenance=real_corpus."
    )
    payload["fold_plan_window"] = plan_report
    # The fold windows the ONE splits module computed and the harness applied, written out so a
    # consumer can state which dates a fold covered. They were never absent by design: `splits`
    # owns the arithmetic and nothing else may recompute it, so serialising the answer is the only
    # way a downstream table can hold the boundary without becoming a second source of it.
    payload["fold_windows"] = [
        {
            "fold_index": fold.index,
            "train_start": fold.train_start_ts.isoformat(),
            "train_end": fold.train_end_ts.isoformat(),
            "validation_start": fold.validation_start_ts.isoformat(),
            "embargo_end": fold.embargo_band[1].isoformat(),
            "test_start": fold.test_start_ts.isoformat(),
            "test_end": fold.test_end_ts.isoformat(),
            "purge_days": fold.purge_days,
            "label_window_days": fold.label_window_days,
        }
        for fold in plan.folds
    ]
    payload["corpus_fold_column_check"] = fold_column_report
    payload["honest_model_fits"] = shared_scores.fits
    payload["honest_ablation_rows"] = sum(1 for _ in ABLATION_ROWS)
    ablation_path = out_dir / "ablation_results.json"
    card_path = out_dir / "model_card.json"
    serialize.write_json(payload, ablation_path)
    serialize.write_json(build_model_card_payload(run), card_path)
    _print_real(variants, control, config, plan, plan_report, fold_column_report)
    return {"ablation": str(ablation_path.resolve()), "model_card": str(card_path.resolve())}


def _p5_allocator(economics: object) -> Any:
    from oxbow.backtest.allocators import P5Allocator

    return P5Allocator(economics)  # type: ignore[arg-type]


def _resolve_repo(corpus_path: Path) -> Path:
    """The repo root the corpus's config lives in.

    Walk up from the corpus file to the nearest directory holding ``config/features.yaml`` —
    the config the scorer, splits and economics are all read from — and fall back to
    :func:`oxbow.config.find_repo_root`. A corpus written under ``out/`` two levels down still
    resolves to the repo that produced it.
    """
    from oxbow.config import find_repo_root

    for parent in corpus_path.resolve().parents:
        if (parent / "config" / "features.yaml").is_file():
            return parent
    return find_repo_root().resolve()


def _print_real(
    variants: Sequence[VariantResult],
    control: Any,
    config: BacktestConfig,
    plan: Any,
    plan_report: dict[str, Any],
    fold_column_report: dict[str, Any],
) -> None:
    print(
        f"OXBOW P6 walk-forward backtest (real corpus) — seed {config.seed}, "
        f"embargo {config.embargo_days}d, provenance=real_corpus"
    )
    print(
        f"fold fractions resolved on: {plan_report['window_source']} "
        f"(recorded {plan_report['recorded_window']}, corpus's own {plan_report['corpus_window']}, "
        f"{plan_report['folds_whose_boundaries_would_differ']} of "
        f"{plan_report['folds']} folds would drift without it)"
    )
    print(
        "corpus fold column vs this plan: "
        + (
            f"{fold_column_report.get('rows_agreeing')}/{fold_column_report.get('rows_compared')} "
            "rows agree"
            if fold_column_report.get("available")
            else str(fold_column_report.get("note"))
        )
    )
    print(plan.as_report_line())
    print()
    header = f"{'variant':38} {'policy':14} {'PR-AUC':>8} {'net_benefit_minor':>18}"
    print(header)
    print("-" * len(header))
    for variant in variants:
        for name, agg in variant.policies.items():
            marker = " [CONTROL]" if variant.is_control else ""
            print(
                f"{variant.label[:37]:38} {name:14} {_fmt(agg.pr_auc):>8} "
                f"{agg.net_benefit_total_minor:>18}{marker}"
            )
    print()
    print(f"LEAKAGE CONTROL: {control.message}")
    full = next((v for v in variants if v.row_id == "full_calibrated"), None)
    if full is not None:
        primary = full.primary(config)
        if primary is not None:
            print(
                f"VaR95 mean {primary.var95_mean_minor} minor | ES97.5 mean "
                f"{primary.es975_mean_minor} minor | max drawdown "
                f"{primary.max_drawdown_minor} minor | risk-adjusted benefit ratio "
                f"{primary.risk_adjusted_ratio:.3f} (is_sharpe_ratio="
                f"{primary.risk_adjusted_ratio_is_sharpe})"
            )


def _variant_row(variant: VariantResult) -> dict[str, Any]:
    return {
        "label": variant.label,
        "provenance": variant.provenance,
        "policies": {name: agg.pr_auc for name, agg in variant.policies.items()},
        "net_benefit_minor_by_policy": {
            name: agg.net_benefit_total_minor for name, agg in variant.policies.items()
        },
    }


def _print_demo(variants: list[VariantResult], control: Any, config: BacktestConfig) -> None:
    """Print the ablation table, the leakage-control pair, and the non-Sharpe ratio line.

    The risk-adjusted benefit ratio is printed WITH its label, ``is_sharpe_ratio`` flag and
    formula — this console output is the third surface plan §12 requires (with the JSON and
    the model card) to carry the "explicitly NOT a Sharpe ratio" label.
    """
    print(
        f"OXBOW P6 walk-forward backtest — seed {config.seed}, "
        f"embargo {config.embargo_days}d, provenance=fake_harness"
    )
    print("provenance: these are harness-verification numbers, NOT a corpus result")
    print()
    header = f"{'variant':38} {'policy':14} {'PR-AUC':>8} {'CI 95%':>16} {'net_benefit_minor':>18}"
    print(header)
    print("-" * len(header))
    for variant in variants:
        for name, agg in variant.policies.items():
            ci = _ci_text(agg)
            marker = " [CONTROL]" if variant.is_control else ""
            print(
                f"{variant.label[:37]:38} {name:14} {_fmt(agg.pr_auc):>8} {ci:>16} "
                f"{agg.net_benefit_total_minor:>18}{marker}"
            )
    print()
    print(f"LEAKAGE CONTROL: {control.message}")
    print()
    full = next((v for v in variants if v.row_id == "full_calibrated"), None)
    if full is not None:
        primary = full.primary(config)
        if primary is not None:
            ratio = primary.risk_adjusted_ratio
            print(
                f"{primary.risk_adjusted_ratio_label}: {ratio:.3f}  "
                f"[is_sharpe_ratio={primary.risk_adjusted_ratio_is_sharpe} — EXPLICITLY NOT a "
                f"Sharpe ratio: no risk-free rate, no annualisation]\n  formula: "
                f"{primary.risk_adjusted_ratio_formula}"
            )


def _ci_text(agg: Any) -> str:
    if agg.pr_auc_ci_low is None or agg.pr_auc_ci_high is None:
        return "undefined"
    return f"[{_f4(agg.pr_auc_ci_low)}, {_f4(agg.pr_auc_ci_high)}]"


def _fmt(value: float | None) -> str:
    return "undefined" if value is None else _f4(value)


def _f4(value: float) -> str:
    return f"{value:.4f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "Run the P6 walk-forward backtest.")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(DEFAULT_OUT_DIR),
        help=f"Directory for the JSON artifacts (default {DEFAULT_OUT_DIR}).",
    )
    parser.add_argument(
        "--demo-fakes",
        action="store_true",
        default=True,
        help="Run against the hand-computed fake harness (the default; verifies the harness).",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Real per-account corpus parquet. If given, requires the P2/P4/P5 providers; "
        "until they land this path raises a named blocked-on message rather than fake-run.",
    )
    parser.add_argument("--mlflow-uri", default=None, help="Optional MLflow tracking URI.")
    parser.add_argument(
        "--baselines-only", action="store_true", help="Run only the four baselines."
    )
    args = parser.parse_args(argv)

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.corpus is not None:
        corpus_path = Path(args.corpus)
        if not corpus_path.is_file():
            raise SystemExit(
                f"--corpus {corpus_path} is not a file. `oxbow score` writes the per-account "
                "scored corpus this path reads; point --corpus at that Parquet."
            )
        from oxbow.backtest.splits import SplitError

        try:
            paths = run_real(
                corpus_path=corpus_path,
                out_dir=out_dir,
                mlflow_uri=args.mlflow_uri,
            )
        except SplitError as exc:
            # The ONE splits module refuses a corpus whose window cannot hold the configured
            # embargo. That is DEV-013's measured fact (IBM is 18 days against a 30-day
            # embargo), surfaced by name rather than answered with a shortened lookback or a
            # fake run. A caller wanting the multi-fold demonstration uses --demo-fakes.
            raise SystemExit(f"--corpus walk-forward refused by the splits module: {exc}") from exc
        except Exception:
            # A fold chain that died mid-fit (the `LightGBMError: bad allocation` of run
            # 01M3FZ2GC3J71AYT1QEKPWEDKJ) is a FAILURE, and it has to leave this function as
            # one. Letting the traceback escape unhandled made the status depend on how the
            # caller piped stderr: measured 2026-09-27, the same failure returned 1 when run
            # bare and 0 when its output was consumed by a pipeline, while the stage had
            # already created the (empty) artifact directory. The traceback is printed, the
            # reason is named, and the code is non-zero -- in that order, every time.
            traceback.print_exc()
            print(
                f"wrote NOTHING under {out_dir}: the fold chain raised; see the traceback above",
                file=sys.stderr,
            )
            return 1
        for kind, path in paths.items():
            print(f"wrote {kind}: {path}")
        return _artifacts_written_code(paths)
    if args.baselines_only:
        paths = run_baseline_only(out_dir=out_dir)
    else:
        paths = run_demo(out_dir=out_dir, mlflow_uri=args.mlflow_uri)
    for kind, path in paths.items():
        print(f"wrote {kind}: {path}")
    return _artifacts_written_code(paths)


def _artifacts_written_code(paths: Mapping[str, Any]) -> int:
    """0 only if every artifact the run says it wrote is on disk and non-empty; else 1.

    A phase gate that cannot fail is not a gate. ``run_real`` returns a dict of paths it
    believes it wrote, and an exit status derived from that return value alone would be a
    claim about the run rather than a measurement of it -- which is exactly how an empty
    ``out/backtest/<run-id>/`` came to sit behind a green exit. The check is the artifact:
    present, a file, and carrying bytes.
    """
    missing: list[str] = []
    for kind, value in paths.items():
        path = Path(str(value))
        if not path.is_file() or path.stat().st_size <= 0:
            missing.append(f"{kind}={path}")
    if missing:
        print(
            f"EXIT FAILED: the run declared {len(paths)} artifact(s) but {len(missing)} "
            f"of them are not on disk as non-empty files: {'; '.join(missing)}. "
            "A backtest that produced no artifact is not a successful run.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
