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
import sys
from pathlib import Path
from typing import Any, Final

from oxbow.backtest import fakes, serialize
from oxbow.backtest.ablation import (
    ABLATION_ROWS,
    BASELINE_POLICIES,
    AblationSpec,
    build_ablation,
    check_leakage_control,
)
from oxbow.backtest.config_io import BacktestConfig, load_backtest_config
from oxbow.backtest.harness import VariantResult, assemble_run
from oxbow.backtest.model_card import build_model_card_payload
from oxbow.backtest.policies import POLICY_EV_CPSAT, POLICY_EV_GREEDY, POLICY_THRESHOLD

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
    folds = fakes.make_fold_masks(height=500, n_folds=config.n_folds, embargo_days=config.embargo_days)
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
    print(f"OXBOW P6 walk-forward backtest — seed {config.seed}, "
          f"embargo {config.embargo_days}d, provenance=fake_harness")
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
    parser.add_argument("--baselines-only", action="store_true", help="Run only the four baselines.")
    args = parser.parse_args(argv)

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.corpus is not None:
        # The real path needs P2's split provider + P4's calibrator + P5's allocator wired
        # through adapters. That wiring lives behind these packages landing; refusing here is
        # honest rather than silently fake-running a "real" corpus.
        raise SystemExit(
            "The --corpus path is blocked on the P2 FoldProvider, P4 Scorer and P5 "
            "Allocator adapters landing; wire oxbow.backtest.splits via SplitsFoldProvider "
            "and implement the Scorer/Allocator adapters against their APIs, then this runs."
        )
    if args.baselines_only:
        paths = run_baseline_only(out_dir=out_dir)
    else:
        paths = run_demo(out_dir=out_dir, mlflow_uri=args.mlflow_uri)
    for kind, path in paths.items():
        print(f"wrote {kind}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
