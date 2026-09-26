"""OXBOW typology rules R1-R12: the detection layer the evidence panel is built on.

Plan §9. Everything tunable lives in ``config/rules.yaml`` and is resolved once, typed,
by :mod:`oxbow.rules.settings`; nothing in this package takes a threshold, a seed or a
default into its own hands, because a rule whose numbers cannot be pointed at in the
config is a rule nobody can defend in review (03 A rule 1).

The public surface, and what each piece is for:

``evaluate_rules(events, graph, cfg) -> RuleResult``
    The whole layer in one call: canonical event v1 plus the built graph in, structured
    hits, per-account evidence groups, a per-rule hit-rate report and the run's fitted
    thresholds out.
``RuleHit``
    One rule firing on one account — severity normalised to 0-1, the evidence that
    justifies it, the window it was observed in, a pattern signature, and its §9 overlap
    group. A hit, never a boolean: §9's first guard, because a boolean cannot be ranked,
    explained or deduplicated.
``NearMiss``
    A pattern the policy refused, with every reason that refused it. This is DEV-015's
    contribution: R4 measured against the corpus's own 54 labelled cycles fires on none
    of them under §9's definition, so a rule that returns nothing must be able to say
    *which setting* returned nothing.
``RULE_REGISTRY``
    The twelve config ids mapped to implementations, validated against the config rather
    than trusted, so a thirteenth stanza fails a run instead of going unscored.

One design stance worth stating where an importer will read it. This layer is where the
plan's guards stop being prose: the combinatorial searches report truncation instead of
hanging (``RuleResult.cycle_search_truncated``), overlap groups are collapsed so one
event pair is one piece of evidence, a rule that fires on a third of the corpus fails the
run *with a suggested threshold*, a permanently dead rule is removed with a reason rather
than left as decoration, cycles are time-respecting by construction, recurring cycles are
down-weighted rather than deleted, and the human-hours rules read ``local_hour`` and are
asserted to. Where a guard could not be honoured without changing what a rule *means* —
R4's definition against the corpus's own labels, R5's synthetic reporting threshold, the
rail typing that lives in ``config/pipeline.yaml`` — the knob is exposed in config and the
near-miss ledger says what it excluded.
"""

from __future__ import annotations

from oxbow.rules.base import FittedThresholds, RuleContext, RuleOutcome
from oxbow.rules.cycles import Chain, Leg, Loop
from oxbow.rules.errors import (
    DeadRuleError,
    HitRateCeilingError,
    RuleBudgetError,
    RuleConfigError,
    RuleContractError,
    RuleError,
)
from oxbow.rules.events import REQUIRED_RULE_COLUMNS, Event, RuleEvents, Window
from oxbow.rules.hits import (
    EvidenceGroup,
    NearMiss,
    RemovedRule,
    RuleHit,
    RuleHitRate,
    RuleResult,
    ThresholdFit,
    pattern_signature,
)
from oxbow.rules.network import chain_member, cycle_member
from oxbow.rules.registry import RULE_REGISTRY, evaluate_rules
from oxbow.rules.settings import RULE_IDS, RulesSettings, load_rules_settings

__all__ = [
    "REQUIRED_RULE_COLUMNS",
    "RULE_IDS",
    "RULE_REGISTRY",
    "Chain",
    "DeadRuleError",
    "Event",
    "EvidenceGroup",
    "FittedThresholds",
    "HitRateCeilingError",
    "Leg",
    "Loop",
    "NearMiss",
    "RemovedRule",
    "RuleBudgetError",
    "RuleConfigError",
    "RuleContext",
    "RuleContractError",
    "RuleError",
    "RuleEvents",
    "RuleHit",
    "RuleHitRate",
    "RuleOutcome",
    "RuleResult",
    "RulesSettings",
    "ThresholdFit",
    "Window",
    "chain_member",
    "cycle_member",
    "evaluate_rules",
    "load_rules_settings",
    "pattern_signature",
]
