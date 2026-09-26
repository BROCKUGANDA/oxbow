"""The eight-row ablation table — "the rigor score" (spec §7.6, plan §12) — and the baselines.

WHY IT IS DECLARATIVE: plan §12 fixes the eight variants *exactly* and puts the table on
the never-cut list (§17), so the rows are named here in the mandated order with the
question each one answers, and every row runs through the same harness with the same
policies. A table whose rows could be silently reordered or dropped is not the rigor
score; a table whose rows are constants an assertion checks against §12 is.

THE TWO ROWS THAT ARE THE WHOLE ARGUMENT:
  * "LightGBM with graph features" — how much the graph adds, the thesis in one row.
  * "Threshold policy vs EV policy" — how much pricing the queue adds, *in money*, the
    v2 thesis in one row. That row is why every variant runs all three review policies
    (threshold, greedy-EV, CP-SAT): the comparison is read off a single uniform run, not
    a special-case calculation that could drift from the rest of the table.

THE LEAKAGE CONTROL (plan §12 gate): the deliberately lookahead-leaking configuration runs
as a *control arm* and must visibly outperform. ``check_leakage_control`` compares it to
the best honest arm; if it does not win, the harness is not measuring what it claims, and
that is reported loudly rather than shipped silently.

Injected seams only: the scorer/allocator/rule-hit providers for each row are supplied by
the caller, so the table is fully exercisable with hand-computed fakes before P2/P4/P5
land, and the same call shape drives a real corpus run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import polars as pl

from oxbow.backtest.config_io import BacktestConfig
from oxbow.backtest.harness import DEFAULT_POLICIES, VariantResult, run_variant
from oxbow.backtest.interfaces import Allocator, RuleHitsProvider, Scorer
from oxbow.backtest.policies import (
    POLICY_HIGHEST_AMOUNT,
    POLICY_RANDOM,
    POLICY_RULES_ONLY,
    POLICY_THRESHOLD,
)

# The eight rows, verbatim from plan §12, in the mandated order. Each entry is
# (row_id, label, question). The row_id is stable so a regression test can assert the
# table has exactly these eight rows and cannot lose one to a merge conflict.
ABLATION_ROWS: Final[tuple[tuple[str, str, str], ...]] = (
    ("rules_only", "Rules only", "Does the ML earn its complexity?"),
    ("scorecard_only", "Scorecard only (WOE logistic)", "Is the transparent model enough?"),
    ("gbm_no_graph", "LightGBM without graph features", "How much does gradient boosting add alone?"),
    (
        "gbm_with_graph",
        "LightGBM with graph features",
        "How much does the graph add? The thesis in one row",
    ),
    (
        "plus_ifusion",
        "Plus Isolation Forest fusion",
        "Does the unsupervised channel catch unlabelled behaviour?",
    ),
    ("full_calibrated", "Full system, calibrated", "Final statistical configuration"),
    (
        "threshold_vs_ev",
        "Threshold policy vs EV policy",
        "How much does pricing the queue add, in money? The v2 thesis in one row",
    ),
    ("full_on_ibm", "Full system on IBM-AML corpus", "Does any of it transfer across corpora?"),
)

ROW_IDS: Final[tuple[str, ...]] = tuple(row[0] for row in ABLATION_ROWS)

# Baselines "all backtested identically" (plan §12 #5): random, score-threshold,
# rules-only, and highest-amount-first — the last because it is what real monitoring
# desks actually run.
BASELINE_POLICIES: Final[tuple[str, ...]] = (
    POLICY_RANDOM,
    POLICY_THRESHOLD,
    POLICY_RULES_ONLY,
    POLICY_HIGHEST_AMOUNT,
)


@dataclass(frozen=True, slots=True)
class AblationSpec:
    """One row of the table: a scorer, the policies it runs, and whether it is a control.

    ``corpus`` / ``corpus_name`` let a single row target a different corpus than the run
    default — the eighth row ("Full system on IBM-AML corpus") is that override, so the
    same table carries a cross-corpus transfer check without a second code path.
    """

    row_id: str
    label: str
    question: str
    scorer: Scorer
    policies: tuple[str, ...] = DEFAULT_POLICIES
    is_control: bool = False
    control_note: str | None = None
    corpus: pl.DataFrame | None = None
    corpus_name: str | None = None


def build_ablation(
    specs: Sequence[AblationSpec],
    *,
    corpus: pl.DataFrame,
    corpus_name: str,
    provenance: str,
    fold_provider: Any,
    config: BacktestConfig,
    allocator: Allocator | None = None,
    rule_hits: RuleHitsProvider | None = None,
    seed: int | None = None,
) -> list[VariantResult]:
    """Run each ablation row through the same harness and return one result per row.

    ``provenance`` is required and non-defaulted at this boundary: it is what keeps a
    fake-harness verification number from ever being reported as a corpus result.
    """
    results: list[VariantResult] = []
    for spec in specs:
        needs_allocator = any(policy.startswith("ev_") for policy in spec.policies)
        results.append(
            run_variant(
                label=spec.label,
                corpus=spec.corpus if spec.corpus is not None else corpus,
                corpus_name=spec.corpus_name or corpus_name,
                provenance=provenance,
                question=spec.question,
                fold_provider=fold_provider,
                scorer=spec.scorer,
                config=config,
                policies=spec.policies,
                allocator=allocator if needs_allocator else None,
                rule_hits=rule_hits,
                is_control=spec.is_control,
                control_note=spec.control_note,
                row_id=spec.row_id,
                seed=seed,
            )
        )
    return results


@dataclass(frozen=True, slots=True)
class LeakageCheck:
    """The result of the gate's leakage-control comparison, with both rows' numbers."""

    detected: bool
    control_pr_auc: float | None
    best_honest_pr_auc: float | None
    control_label: str
    honest_label: str
    message: str


def check_leakage_control(variants: Sequence[VariantResult], *, expect_outperforms: bool) -> LeakageCheck:
    """Confirm the lookahead control arm beats every honest arm, or report loudly.

    This is the point of the gate (plan §12): without a control that *should* cheat and
    visibly does, a green honest backtest and a secretly-leaking one look identical. If
    the control does NOT outperform, the harness is not measuring what it claims, and the
    caller must treat that as a failure of the harness, not a good result.
    """
    controls = [v for v in variants if v.is_control]
    honest = [v for v in variants if not v.is_control]
    if not controls or not honest:
        return LeakageCheck(
            detected=False,
            control_pr_auc=None,
            best_honest_pr_auc=None,
            control_label=controls[0].label if controls else "<none>",
            honest_label=honest[0].label if honest else "<none>",
            message="leakage control check needs at least one control arm and one honest arm",
        )

    def headline(variant: VariantResult) -> float | None:
        for policy in ("ev_greedy", "score_threshold"):
            agg = variant.policies.get(policy)
            if agg is not None and agg.pr_auc is not None:
                return agg.pr_auc
        for agg in variant.policies.values():
            if agg.pr_auc is not None:
                return agg.pr_auc
        return None

    control = max(controls, key=lambda v: (headline(v) or -1.0))
    best_honest = max(honest, key=lambda v: (headline(v) or -1.0))
    control_auc = headline(control)
    honest_auc = headline(best_honest)
    detected = (
        control_auc is not None
        and honest_auc is not None
        and control_auc > honest_auc
    )
    if expect_outperforms and not detected:
        message = (
            "LEAKAGE CONTROL FAILED: the deliberately lookahead-leaking arm did NOT "
            f"outperform the best honest arm ({control_auc} vs {honest_auc}); the harness "
            "is not detecting leakage it is supposed to expose — report this, do not ship."
        )
    elif detected:
        message = (
            f"leakage control detected as expected: control PR-AUC {control_auc} exceeds "
            f"the best honest arm {honest_auc}, proving the harness can see lookahead."
        )
    else:
        message = "leakage control not enabled for this run"
    return LeakageCheck(
        detected=detected,
        control_pr_auc=control_auc,
        best_honest_pr_auc=honest_auc,
        control_label=control.label,
        honest_label=best_honest.label,
        message=message,
    )


__all__ = [
    "ABLATION_ROWS",
    "BASELINE_POLICIES",
    "ROW_IDS",
    "AblationSpec",
    "LeakageCheck",
    "build_ablation",
    "check_leakage_control",
]
