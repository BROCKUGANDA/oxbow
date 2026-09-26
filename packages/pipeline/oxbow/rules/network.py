"""R4 ``CYCLE_MEMBER`` and R12 ``CHAIN_MEMBER`` — the graph-typology pair.

Both ask whether value moved around a shape rather than from A to B, and both are bounded
searches: an unbounded enumeration on a dense component does not finish, so each carries
a depth cap, a per-component visit budget and a wall clock, and each records
``truncated`` with the reason instead of hanging or silently under-counting (§9).

**DEV-015 is the reason this module is written the way it is.** Measured against the
corpus's own 54 labelled cycles, R4 as specified in §9 — one currency per loop, amounts
that never increase, retention at least 0.6, three to six hops — fires on none of them:
38 of the 54 convert currency on the way round and 25 shed more value than the floor
tolerates. The defaults therefore stay exactly as §9 wrote them, because silently
weakening a rule's definition to make its own hit count look better is the thing the
guards exist to prevent. What changed is that the definition became *configurable and
honest*: the retention floor, the non-increasing requirement and the currency policy are
read from ``config/rules.yaml``, and a loop the policy refuses is emitted as a
:class:`~oxbow.rules.hits.NearMiss` naming every setting that excluded it. The run report
can then say "0.6 excludes 25 of these 54" instead of reporting a quiet day.

The graph engine's own enumeration is not re-used as the source of hits for a reason
worth stating: it prunes a cross-currency leg during the walk, so a refused loop is not
merely excluded, it is unobserved. :mod:`oxbow.rules.cycles` walks without the value
policy and classifies afterwards, and the two agree with the graph engine on the §9
default policy, which
``tests/unit/test_p3b_rules.py::test_r4_default_policy_agrees_with_graph_engine``
asserts on the fixture rather than asserting about.

R12 reports maximal chains only, and its decay enters severity rather than the decision:
a chain that sheds more than ``decay`` per hop is still layering, and gating on an exact
multiplier would hide real five-hop chains behind a rounding choice.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import pairwise
from typing import Final

from oxbow.rules.base import RuleContext, RuleOutcome
from oxbow.rules.cycles import (
    NEAR_MISS_DEPTH_SLACK,
    Chain,
    Loop,
    adjacency,
    near_miss_of,
    policy_near_misses,
    rails_and_singletons,
    rails_only,
    walk_chains,
    walk_loops,
)
from oxbow.rules.events import MICROSECONDS_PER_HOUR, Window, hours_to_us, ratio_of
from oxbow.rules.hits import RuleHit, pattern_signature
from oxbow.rules.settings import ChainSettings, CycleMemberSettings
from oxbow.rules.severity import clamp_unit, combine, excess_severity, share_severity

# Ties a periodic loop down-weighted rather than suppressed: payroll is real structure and
# pretending it is invisible would make the down-weight unauditable.
_PERIODIC_NOTE: Final[str] = "periodic_cycle_down_weighted"


def cycle_member(ctx: RuleContext) -> RuleOutcome:
    """R4: members of a time-respecting directed cycle, 3-6 hops, value retention above r.

    One hit per (account, cycle pattern). Recurring cycles — the payroll-merchant-supplier
    ring that closes every 168 hours — collapse to a single signature and are multiplied
    by ``periodic_down_weight`` rather than being dropped.
    """
    settings: CycleMemberSettings = ctx.settings.settings_for("R4")  # type: ignore[assignment]
    skip, skipped_counts = rails_and_singletons(ctx)
    out, nodes, dropped = adjacency(
        ctx.events,
        drop_reversals=ctx.settings.guards.excludes_reversals("R4"),
        drop_zero_value=ctx.settings.guards.exclude_zero_value_from_value_rules,
        skip_nodes=skip,
    )
    horizon = settings.max_length + (NEAR_MISS_DEPTH_SLACK if settings.emit_near_misses else 0)
    search = walk_loops(
        out,
        nodes,
        min_length=settings.min_length,
        horizon=horizon,
        max_visits=settings.max_visits,
        timeout_ms=settings.timeout_ms,
    )
    loops, misses = policy_near_misses(ctx, search)
    outcome = RuleOutcome(
        truncated=search.truncated,
        truncation_reason=search.reason,
        visits=search.visits,
    )
    outcome.near_misses.extend(misses)
    outcome.notes["cycle_search_nodes_searched"] = str(search.nodes_searched)
    outcome.notes["cycle_search_rails_excluded"] = str(skipped_counts["rails"])
    outcome.notes["cycle_search_singletons_excluded"] = str(skipped_counts["singletons"])
    outcome.notes.update({key: str(value) for key, value in dropped.items()})

    for _pattern, laps in _group_by_pattern(loops).items():
        periodic = _is_periodic(laps, settings)
        anchor = min(laps, key=lambda loop: (-loop.value_retention, loop.ts_us[0], loop.txn_ids))
        severity = share_severity(anchor.value_retention, settings.retention) if settings.retention < 1.0 else 0.0
        severity = combine(
            severity,
            excess_severity(
                float(anchor.length), float(settings.min_length), float(settings.max_length)
            ),
        )
        if settings.down_weight_periodic and periodic is not None:
            severity = clamp_unit(severity * settings.periodic_down_weight)
        signature = pattern_signature("R4", anchor.nodes[0], anchor.nodes)
        for account in sorted(anchor.nodes):
            outcome.hits.append(
                RuleHit(
                    rule_id="R4",
                    rule_name=ctx.spec("R4").name,
                    account_key=account,
                    severity=severity,
                    evidence={
                        "observation": round(anchor.value_retention, 6),
                        "threshold_param": "retention",
                        "threshold_value": settings.retention,
                        "cycle_nodes": list(anchor.nodes),
                        "cycle_length": anchor.length,
                        "currencies": list(anchor.currencies),
                        "retention_comparable_across_currencies": anchor.retention_comparable,
                        "amounts_minor": list(anchor.amounts_minor),
                        "amount_increases_along_loop": anchor.non_increasing_breaches,
                        "pattern_observations": len(laps),
                        "distinct_lap_starts": len({loop.ts_us[0] for loop in laps}),
                        "recurrence_period_hours": periodic,
                        "down_weighted": bool(periodic is not None and settings.down_weight_periodic),
                        "currency_policy": settings.currency_policy,
                        "non_increasing_required": settings.non_increasing,
                        "txn_ids": sorted(anchor.txn_ids),
                        "self_transfers_excluded": dropped["self_transfer"],
                    },
                    window=Window(
                        start_us=min(anchor.ts_us),
                        end_us=max(anchor.ts_us),
                        label="R4_cycle",
                    ),
                    hit_signature=signature,
                )
            )
    return outcome


def chain_member(ctx: RuleContext) -> RuleOutcome:
    """R12: a time-respecting path of at least ``min_length`` hops, non-increasing, each hop inside ``delta_hours``."""
    settings: ChainSettings = ctx.settings.settings_for("R12")  # type: ignore[assignment]
    # Rails stop the walk; singletons do not. A leaf account is exactly where a
    # layering chain starts or ends, so excluding singletons here would delete the two
    # ends of every real chain, while the cycle walk above keeps the exclusion because a
    # leaf cannot close a loop. `exclude_singletons_from_graph_aggregates` is about
    # aggregates (degree distributions, communities), and a chain is neither.
    skip, _skipped = rails_only(ctx)
    out, nodes, dropped = adjacency(
        ctx.events,
        drop_reversals=ctx.settings.guards.excludes_reversals("R12"),
        drop_zero_value=ctx.settings.guards.exclude_zero_value_from_value_rules,
        skip_nodes=skip,
    )
    search = walk_chains(
        out,
        nodes,
        min_hops=settings.min_length,
        max_depth=settings.max_depth,
        delta_us=hours_to_us(settings.delta_hours),
        require_non_increasing=settings.require_non_increasing,
        component_budget=settings.component_budget,
        timeout_ms=settings.timeout_ms,
    )
    outcome = RuleOutcome(
        truncated=search.truncated,
        truncation_reason=search.reason,
        visits=search.visits,
    )
    outcome.notes["chain_search_budget"] = str(settings.component_budget)
    for chain in search.chains:
        fidelity = _decay_fidelity(chain, settings.decay)
        severity = combine(
            excess_severity(
                float(chain.hops), float(settings.min_length), float(2 * settings.min_length)
            ),
            fidelity,
        )
        txn_ids = sorted(chain.txn_ids)
        signature = pattern_signature("R12", chain.nodes[0], chain.nodes, txn_ids)
        for account in sorted(chain.nodes):
            outcome.hits.append(
                RuleHit(
                    rule_id="R12",
                    rule_name=ctx.spec("R12").name,
                    account_key=account,
                    severity=severity,
                    evidence={
                        "observation": float(chain.hops),
                        "threshold_param": "min_length",
                        "threshold_value": settings.min_length,
                        "chain_nodes": list(chain.nodes),
                        "chain_hops": chain.hops,
                        "amounts_minor": list(chain.amounts_minor),
                        "hop_delta_hours_max": round(
                            (chain.legs[-1].ts_us - chain.legs[0].ts_us)
                            / MICROSECONDS_PER_HOUR
                            / max(chain.hops - 1, 1),
                            4,
                        ),
                        "decay": settings.decay,
                        "decay_fidelity": round(fidelity, 6),
                        "require_non_increasing": settings.require_non_increasing,
                        "txn_ids": txn_ids,
                        "self_transfers_excluded": dropped["self_transfer"],
                    },
                    window=chain.window,
                    hit_signature=signature,
                )
            )
    return outcome


def _group_by_pattern(loops: Iterable[Loop]) -> dict[tuple[str, ...], list[Loop]]:
    """Loops sharing one node rotation, which is what a recurring cycle looks like.

    Grouping on the rotation rather than the leg ids is the difference between reporting
    a weekly payroll ring three times and reporting it once: three laps of the same four
    accounts are three observations of one pattern, and the walk cannot know they are the
    same pattern by leg identity alone.
    """
    groups: dict[tuple[str, ...], list[Loop]] = {}
    for loop in loops:
        groups.setdefault(loop.nodes, []).append(loop)
    return {key: sorted(value, key=lambda loop: loop.ts_us[0]) for key, value in groups.items()}


def _is_periodic(laps: Sequence[Loop], settings: CycleMemberSettings) -> float | None:
    """The recurrence period in hours, when the laps really do recur.

    Returns the smallest gap between two observations of the ring that sits within
    ``periodic_tolerance_ratio`` of a whole multiple of the configured period, or None.
    The tolerance is stated rather than assumed because a corpus that pays on Mondays at
    08:00 and Tuesdays at 09:00 is not periodic, and calling that periodic would
    down-weight a real layering ring.
    """
    if len(laps) < 2:
        return None
    period_us = settings.periodic_period_hours * MICROSECONDS_PER_HOUR
    tolerance = settings.periodic_tolerance_ratio * period_us
    starts = sorted(loop.ts_us[0] for loop in laps)
    # One pair a whole multiple of the period apart is a recurrence. Requiring *every*
    # pair to conform would hide the periodicity behind a lap-mixing walk, which is an
    # artefact of a strictly time-respecting search over three laps rather than a second,
    # irregular pattern — the fixture's own payroll note says exactly this.
    gaps = [
        later - earlier
        for index, earlier in enumerate(starts)
        for later in starts[index + 1 :]
        if later > earlier
    ]
    conforming = [
        gap
        for gap in gaps
        if gap > 0 and abs(gap - round(gap / period_us) * period_us) <= max(tolerance, 1)
    ]
    if not conforming:
        return None
    return round(min(conforming) / MICROSECONDS_PER_HOUR, 4)


def _decay_fidelity(chain: Chain, decay: float) -> float:
    """How closely the chain tracks its configured per-hop retention.

    Each hop's ratio is measured against ``decay`` and capped at 1.0, so a chain that
    keeps *more* value than ``decay`` is not credited with extra fidelity — the knob
    describes the skim a layering ring expects to lose per hop, and keeping everything is
    a different story, not a stronger one.
    """
    if decay <= 0.0:
        return 0.0
    amounts = chain.amounts_minor
    if len(amounts) < 2:
        return 1.0
    ratios = [
        min(1.0, ratio_of(following, previous) / decay)
        for previous, following in pairwise(amounts)
        if previous > 0
    ]
    if not ratios:
        return 0.0
    return sum(ratios) / len(ratios)


__all__ = ["chain_member", "cycle_member", "near_miss_of"]
