"""The context every rule is evaluated in, and the shape a rule returns.

One rule function, one signature: ``f(ctx) -> RuleOutcome``. Putting the guards in
:class:`RuleContext` rather than in each rule is the difference between eleven rules
that each remember to drop reversals and twelve that cannot forget. The accessors below
are the only sanctioned way to reach a guard, and each one names the config key it reads,
so a rule that excludes nothing can be traced to a line in ``config/rules.yaml``.

Why the outcome type carries ``truncated`` and ``near_misses`` at all: §9 requires a
bounded search that reports its own bounding ("the combinatorial budget must record
``cycle_search_truncated`` instead of hanging") and DEV-015 requires that a pattern the
policy excluded is reported *with a reason* instead of vanishing. Both are outcomes, so
both are on the outcome type, and a rule that cannot produce them has not implemented
the guard.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from oxbow.graph.model import AccountGraph
from oxbow.rules.events import ORIGINATED, Event, RuleEvents, Window
from oxbow.rules.hits import NearMiss, RuleHit, ThresholdFit
from oxbow.rules.settings import RuleSettings, RuleSpec, RulesSettings

# Rules whose pattern is built from money moving, and therefore read
# ``guards.exclude_zero_value_from_value_rules``. Counting rules (velocity, dormancy,
# new-counterparty) are excluded from that guard because a zero-value probe *is* activity
# for them — it is a ratio feature, not a count, that the probe corrupts.
VALUE_RULE_IDS: frozenset[str] = frozenset({"R1", "R2", "R4", "R5", "R9", "R10", "R12"})


@dataclass(frozen=True, slots=True)
class FittedThresholds:
    """Corpus-fitted thresholds, each with the window that produced it.

    Empty rather than guessed when a rule's fitter is not relevant to the run: an absent
    ``tau`` is a config or scope question the run report answers, while a defaulted one is
    a number nobody fitted.
    """

    fits: Mapping[str, ThresholdFit] = field(default_factory=dict)

    def value_of(self, name: str) -> int:
        try:
            return self.fits[name].value
        except KeyError as exc:
            raise ValueError(
                f"no fitted threshold {name!r} in this run; fitted: {sorted(self.fits)}. "
                "A rule silently substituting a default here would report hits that no "
                "window authorised."
            ) from exc

    def maybe_value_of(self, name: str) -> int | None:
        fit = self.fits.get(name)
        return None if fit is None else fit.value


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Everything a rule is allowed to look at."""

    events: RuleEvents
    graph: AccountGraph
    settings: RulesSettings
    thresholds: FittedThresholds
    fit_window: Window

    def spec(self, rule_id: str) -> RuleSpec:
        return self.settings.spec(rule_id)

    def legs(
        self, rule_id: str, account: str, side: str, *, incident: bool = False
    ) -> tuple[Event, ...]:
        """One account's legs on one side, with this rule's guards already applied.

        ``incident`` switches to the both-directions view a behaviour-break rule needs;
        a flow-typology rule never uses it, because "did they pass money through" is a
        question about what they *sent* after what they *received*, not about total
        traffic.
        """
        if incident:
            raw: tuple[Event, ...] = self.events.incident.get(account, ())
        elif side == ORIGINATED:
            raw = self.events.originated.get(account, ())
        else:
            raw = self.events.received.get(account, ())
        drop_reversals = self.settings.guards.excludes_reversals(rule_id)
        drop_zero = (
            rule_id in VALUE_RULE_IDS and self.settings.guards.exclude_zero_value_from_value_rules
        )
        kept: list[Event] = []
        for event in raw:
            # Self-transfers are dropped from *every* rule, not only the three
            # `guards.exclude_self_loops_from` names. The guard list is about the
            # self-loop trivially satisfying a cycle; the same arithmetic makes
            # `account_from == account_to` pass any retention ratio at exactly 1.0, so a
            # customer moving money between their own accounts would fire R1. The events
            # are still counted — `RuleEvents.self_transfer_count`, IBM-AML's 591,212 —
            # they are just never a hop.
            if event.is_self_transfer:
                continue
            if drop_reversals and event.is_reversal:
                continue
            if drop_zero and event.is_zero_value:
                continue
            kept.append(event)
        return tuple(kept)

    def incident_legs(self, rule_id: str, account: str) -> tuple[Event, ...]:
        """Both directions of one account's activity, guards applied.

        Named rather than reached by ``legs(..., incident=True)`` because a behaviour rule
        that asks "how many transactions did this account take part in" is not asking
        about a side at all, and a call site that passes a dummy side argument to get
        there reads like a mistake.
        """
        return self.legs(rule_id, account, "", incident=True)

    def excluded_node_types(self, rule_id: str) -> frozenset[str]:
        params: RuleSettings = self.settings.settings_for(rule_id)
        declared: Sequence[str] = getattr(params, "exclude_node_types", ())
        return frozenset(declared)

    def node_type(self, account: str) -> str:
        types = self.graph.node_types
        return str(types.get(account, "member"))

    def node_is_excluded(self, rule_id: str, account: str) -> bool:
        return self.node_type(account) in self.excluded_node_types(rule_id)

    def accounts(self) -> tuple[str, ...]:
        return self.events.accounts

    def fingerprint_of(self, rule_id: str) -> Mapping[str, object]:
        spec = self.settings.spec(rule_id)
        return {"rule_id": rule_id, "name": spec.name, "typology": spec.typology}


@dataclass(slots=True)
class RuleOutcome:
    """What one rule produced: hits, the patterns it refused, and how hard it looked."""

    hits: list[RuleHit] = field(default_factory=list)
    near_misses: list[NearMiss] = field(default_factory=list)
    truncated: bool = False
    truncation_reason: str | None = None
    visits: int = 0
    notes: dict[str, str] = field(default_factory=dict)

    def extend(self, hits: Iterable[RuleHit]) -> None:
        self.hits.extend(hits)

    def stop(self, reason: str) -> None:
        self.truncated = True
        if self.truncation_reason is None:
            self.truncation_reason = reason


RuleFunction = Callable[[RuleContext], RuleOutcome]

__all__ = ["VALUE_RULE_IDS", "FittedThresholds", "RuleContext", "RuleFunction", "RuleOutcome"]
