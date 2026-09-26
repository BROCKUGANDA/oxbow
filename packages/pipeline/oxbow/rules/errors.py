"""Named failures for the rules layer.

Mirrors :mod:`oxbow.graph.errors`, and for the same reason: 03 A rule 2 says an
unknown must never become a zero, and in this layer the dangerous version of that
is an unknown becoming a *hit* — a rule that fired on something it cannot
actually evaluate reads exactly like a rule that caught a criminal.

Three of these deserve their existence explained rather than assumed:

``HitRateCeilingExceeded``
    The plan's §9 guard is not a warning, it fails the run. A rule that fires on
    a third of the corpus carries almost no information per hit and turns the
    evidence panel into noise, so shipping it is worse than stopping. The
    exception therefore carries a concrete threshold suggestion: refusing without
    one just sends the operator off to guess which knob to turn.
``RuleBudgetError``
    Raised only when a caller asked for a hard budget. The ordinary path records
    ``cycle_search_truncated`` on the result instead, because §9 requires the
    truncation to be a *reported outcome*, not a crash: a pipeline that dies on a
    dense component cannot produce a run report at all.
``DeadRuleError``
    Not an error a caller catches in normal operation — it is the payload type
    that says "this rule scored nothing and has been taken out of the panel".
    Keeping a dead rule visible and greyed out would present capability the
    system does not have.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final


class RuleError(RuntimeError):
    """Base for the layer. One catchable type at the CLI, still specific in the message."""


class RuleConfigError(RuleError):
    """``config/rules.yaml`` is missing, malformed, or contradicts itself.

    Read through :mod:`oxbow.rules.settings`, which substitutes no defaults. A
    default silently applied here would produce a run that is perfectly
    reproducible and perfectly wrong, and the only symptom would be a hit count
    nobody can explain.
    """


class RuleContractError(RuleError):
    """The event frame handed to ``evaluate_rules`` is not canonical event v1.

    Raised for a missing column (``local_hour`` and ``event_date_local`` in
    particular — 03 C makes those two load-bearing), a float amount, a naive
    timestamp, or a frame the accompanying graph was not built from.
    """


class DeadRuleError(RuleError):
    """A rule stayed below the configured hit-rate floor for the whole run.

    Carried as data on :class:`~oxbow.rules.hits.DeadRule` when the run is asked
    to degrade honestly; this type exists so the strict mode
    (``guards.fail_on_dead_rule``) can make it fatal instead.
    """


class HitRateCeilingError(RuleError):
    """A rule fired on more than ``hit_rate_ceiling`` of the scored accounts.

    ``suggested_params`` is the fix, not decoration: it is the threshold value
    computed from the observed distribution that would bring this rule back under
    the ceiling, so the run fails once and the next run is already corrected.
    """

    def __init__(
        self,
        message: str,
        *,
        rule_id: str,
        hit_rate: float,
        accounts_hit: int,
        accounts_scored: int,
        ceiling: float,
        suggested_params: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.hit_rate = hit_rate
        self.accounts_hit = accounts_hit
        self.accounts_scored = accounts_scored
        self.ceiling = ceiling
        self.suggested_params: Mapping[str, object] = dict(suggested_params or {})


class RuleBudgetError(RuleError):
    """A search passed its configured visit or time budget in a hard-budget run."""


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One concrete parameter change that would bring a rule back under the ceiling.

    Kept as a value object rather than a sentence because the CLI and the run
    report both render it, and because a suggestion nobody can compare against
    the config is a caption rather than a fix.
    """

    rule_id: str
    param: str
    current: object
    suggested: object
    basis: str = field(default="observed distribution over this run's scored window")

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "param": self.param,
            "current": self.current,
            "suggested": self.suggested,
            "basis": self.basis,
        }


PARAM_SEPARATOR: Final[str] = "."
