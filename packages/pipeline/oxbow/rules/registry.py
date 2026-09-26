"""The registry and the one entry point: ``evaluate_rules(events, graph, cfg)``.

The registry is a dict from the twelve ids in ``config/rules.yaml`` to their
implementations, validated against the config at import of a run rather than trusted:
``test_registry_covers_the_config_exactly`` proves that a thirteenth stanza in the YAML
fails loudly instead of being silently unscored, which is the same class of defect as a
rule that exists in code but is not wired up — both produce a panel with a gap nobody can
see.

``evaluate_rules`` owns the four things no individual rule can do for itself:

1. **Fit the corpus thresholds** on the training window and carry the window with them,
   so ``tau`` and the synthetic structuring ``T`` are pinned rather than assumed (§9).
2. **Deduplicate by pattern signature** across the whole run, so one ladder or one cycle
   seen from two overlapping windows is one piece of evidence (§9's ``deduplicate_by``).
3. **Collapse overlap groups** per account, so R1 and R10 on the same two legs count once
   in the score while both stay visible in the panel (03 G).
4. **Report the per-rule hit rate**, failing the run above the ceiling *with a threshold
   suggestion* and removing a permanently dead rule rather than presenting it as
   capability. The failure is a raised :class:`~oxbow.rules.errors.HitRateCeilingError`
   because a run that printed a plausible score while one rule flagged a third of the
   corpus has already misled whoever reads it.

Order everywhere is ``(severity desc, account_key, rule_id)``, and every set is sorted
before it is returned, so two runs over the same frame emit the same bytes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Final

from oxbow.config import PipelineConfig
from oxbow.graph.model import AccountGraph
from oxbow.rules.amounts import structuring
from oxbow.rules.base import FittedThresholds, RuleContext, RuleOutcome
from oxbow.rules.behaviour import (
    amount_regime_shift,
    dormant_reactivation,
    odd_hour_shift,
    velocity_spike,
)
from oxbow.rules.errors import RuleConfigError
from oxbow.rules.events import RuleEvents, Window, require_rule_events
from oxbow.rules.fan import fan_in, fan_out, new_counterparty_surge
from oxbow.rules.flow import fast_cash_out, rapid_pass_through
from oxbow.rules.hits import (
    NearMiss,
    RuleHit,
    RuleResult,
    ThresholdFit,
    attach_overlap_groups,
    build_hit_rate_report,
    collapse_overlap_groups,
    deduplicate_by_signature,
    pattern_signature,
)
from oxbow.rules.network import chain_member, cycle_member
from oxbow.rules.settings import (
    RULE_IDS,
    FanInSettings,
    RulesSettings,
    StructuringSettings,
    load_rules_settings,
)
from oxbow.rules.thresholds import (
    STRUCTURING_FIT_NAME,
    TAU_FIT_NAME,
    fit_structuring_threshold,
    fit_tau,
)

# Rules whose subject is network topology rather than one account's own behaviour. The
# label exists for the dead-rule note: on a star-shaped corpus these rules score zero
# *correctly* (DEV-011), and saying so is the difference between a reported result and a
# bug somebody hides.
GRAPH_TOPOLOGY_RULE_IDS: Final[frozenset[str]] = frozenset({"R2", "R3", "R4", "R11", "R12"})

RULE_REGISTRY: Mapping[str, Callable[[RuleContext], RuleOutcome]] = {
    "R1": rapid_pass_through,
    "R2": fan_in,
    "R3": fan_out,
    "R4": cycle_member,
    "R5": structuring,
    "R6": velocity_spike,
    "R7": dormant_reactivation,
    "R8": odd_hour_shift,
    "R9": amount_regime_shift,
    "R10": fast_cash_out,
    "R11": new_counterparty_surge,
    "R12": chain_member,
}


def evaluate_rules(
    events: object,
    graph: AccountGraph,
    cfg: RulesSettings | PipelineConfig,
    *,
    fit_window: Window | None = None,
    enforce_hit_rate_ceiling: bool = True,
) -> RuleResult:
    """Run all twelve rules over ``events`` and return the auditable result.

    ``fit_window`` is the training window the corpus-fitted thresholds come from; it
    defaults to the whole run and must be stated explicitly on any run that also scores a
    later period, because a threshold fitted on the scored period is leakage that looks
    like tuning.

    ``enforce_hit_rate_ceiling`` is the one escape hatch and it is only for producing the
    report that explains a failure: when it is off the ceiling breach is still recorded on
    the row's status and suggestion, so nothing is lost by reading it.
    """
    settings = _resolve_settings(cfg)
    rule_events = require_rule_events(events)  # type: ignore[arg-type]
    window = rule_events.window
    fit = window if fit_window is None else fit_window

    thresholds, fits = _fit_thresholds(settings, rule_events, fit)
    context = RuleContext(
        events=rule_events,
        graph=graph,
        settings=settings,
        thresholds=thresholds,
        fit_window=fit,
    )

    raw_hits: list[RuleHit] = []
    near_misses: list[NearMiss] = []
    truncated = False
    truncation_reason: str | None = None
    chain_truncated = False
    chain_reason: str | None = None
    visits = 0
    notes: dict[str, str] = {}
    for rule_id in RULE_IDS:
        function = RULE_REGISTRY.get(rule_id)
        if function is None:
            raise RuleConfigError(
                f"{rule_id} is declared in {settings.path} but has no implementation in "
                "oxbow.rules.registry. An unwired rule is the invisible kind of dead: the "
                "panel still lists it and nothing ever evaluates it."
            )
        outcome = function(context)
        raw_hits.extend(outcome.hits)
        near_misses.extend(outcome.near_misses)
        visits += outcome.visits
        notes.update({f"{rule_id}.{key}": value for key, value in outcome.notes.items()})
        if outcome.truncated:
            if rule_id == "R4":
                truncated, truncation_reason = True, outcome.truncation_reason
            elif rule_id == "R12":
                chain_truncated, chain_reason = True, outcome.truncation_reason

    hits = deduplicate_by_signature(attach_overlap_groups(raw_hits, settings))
    accounts_scored = len(rule_events.accounts)
    dead_notes = _dead_rule_notes(settings, hits, graph, accounts_scored)
    rows, removed, failures = build_hit_rate_report(
        settings=settings,
        hits=hits,
        accounts_scored=accounts_scored,
        dead_notes=dead_notes,
    )
    result = RuleResult(
        hits=hits,
        hit_rates=rows,
        evidence_groups=collapse_overlap_groups(hits),
        near_misses=tuple(sorted(near_misses, key=lambda miss: (miss.rule_id, miss.pattern))),
        removed_rules=removed,
        thresholds=fits,
        scored_accounts=accounts_scored,
        window=window,
        fit_window=fit,
        settings_fingerprint=settings.fingerprint(),
        cycle_search_truncated=truncated,
        cycle_search_visits=visits,
        cycle_search_reason=truncation_reason,
        chain_search_truncated=chain_truncated,
        chain_search_reason=chain_reason,
        self_transfer_count=rule_events.self_transfer_count,
        zero_value_excluded=rule_events.zero_value_count,
        reversal_excluded=rule_events.reversal_count,
        currencies=rule_events.currencies,
        local_offset_hours=rule_events.local_offset_hours,
    )
    if enforce_hit_rate_ceiling and failures:
        raise failures[0]
    return result


def _resolve_settings(cfg: RulesSettings | PipelineConfig) -> RulesSettings:
    if isinstance(cfg, RulesSettings):
        return cfg
    if isinstance(cfg, PipelineConfig):
        return load_rules_settings(cfg.root)
    raise RuleConfigError(
        f"evaluate_rules takes RulesSettings or PipelineConfig, got {type(cfg).__name__}"
    )


def _fit_thresholds(
    settings: RulesSettings, events: RuleEvents, fit: Window
) -> tuple[FittedThresholds, tuple[ThresholdFit, ...]]:
    """Derive only what the scored rules ask for, and record each with its window.

    A rule absent from the config does not get a threshold fitted for it: an unused
    measurement in the run report is decoration, and on a large corpus it is also work
    nobody asked for.
    """
    fits: list[ThresholdFit] = []
    values: dict[str, ThresholdFit] = {}
    fan = settings.settings.get("R2")
    if isinstance(fan, FanInSettings):
        tau = fit_tau(events, fit, quantile_percent=fan.tau_percentile)
        values[TAU_FIT_NAME] = tau
        fits.append(tau)
    band = settings.settings.get("R5")
    if isinstance(band, StructuringSettings):
        threshold = fit_structuring_threshold(
            events,
            fit,
            band_low=band.threshold_band_low,
            band_high=band.threshold_band_high,
            pinned_minor=band.threshold_minor,
            pinned_label=band.threshold_label,
        )
        values[STRUCTURING_FIT_NAME] = threshold
        fits.append(threshold)
    return FittedThresholds(fits=values), tuple(fits)


def _dead_rule_notes(
    settings: RulesSettings, hits: Sequence[RuleHit], graph: AccountGraph, accounts_scored: int
) -> dict[str, str]:
    """Why a zero-hit rule is zero, in the words the report needs.

    DEV-011 decides the wording. On a star-shaped corpus a topology rule hitting nothing
    is a correct measurement, and the note has to carry that sentence so nobody reads the
    removal as a defect to be tuned away.
    """
    hit_rules = {hit.rule_id for hit in hits}
    notes: dict[str, str] = {}
    for rule_id in RULE_IDS:
        if rule_id in hit_rules:
            continue
        if rule_id not in settings.settings:
            continue
        if rule_id in GRAPH_TOPOLOGY_RULE_IDS:
            notes[rule_id] = (
                f"no qualifying topology observed: the graph found "
                f"{graph.stats.cycle_count} time-respecting cycles over "
                f"{graph.stats.node_count} nodes (median total degree "
                f"{graph.stats.degree_all_nodes.median:g}, rails "
                f"{graph.stats.rail_count}, self-transfers excluded "
                f"{graph.stats.self_transfer_count}). A star-shaped corpus has no "
                "multi-account structure to detect and a zero here is a measurement, "
                "not a failure (DEV-011)."
            )
        else:
            notes[rule_id] = (
                f"no account out of {accounts_scored} crossed this rule's threshold in "
                "the scored window."
            )
    return notes


def signature_for(rule_id: str, account_key: str, *parts: object) -> str:
    """Public alias for the signature helper, so P8 can recompute a stored one."""
    return pattern_signature(rule_id, account_key, *parts)


__all__ = ["GRAPH_TOPOLOGY_RULE_IDS", "RULE_IDS", "RULE_REGISTRY", "evaluate_rules", "signature_for"]
