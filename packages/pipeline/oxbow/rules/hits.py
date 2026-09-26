"""What a rule emits, and how a run's worth of hits becomes a report.

§9's first guard is structural, not stylistic: **a rule emits a hit, never a
boolean.** A boolean says "something" and survives review; a hit carries the
observation, the threshold it passed, the window that contained it, and the signature
that lets the same pattern be recognised in the neighbouring window. Three of the
consumers downstream depend on fields a boolean does not have:

* the scorecard needs ``severity`` to rank two accounts that hit the same rule;
* the evidence panel needs ``overlap_group`` so pass-through plus fast-cash-out on one
  event pair counts as one piece of evidence, not two (03 G);
* the run report needs ``hit_rates`` so a rule that fires on a third of accounts fails
  the run with a threshold suggestion instead of shipping as a constant.

Two invariants hold this together and both are enforced in
:meth:`RuleHit.__post_init__` rather than trusted:

1. ``severity`` is always inside ``[0, 1]`` — an out-of-range severity is a
   normalisation bug in the rule that produced it, and it is caught at construction so
   the failing message names the rule rather than the arithmetic three stages later.
2. ``hit_signature`` is always present. Signature-grain dedup is the only thing that
   stops a structuring ladder crossing a window boundary from being counted per window,
   and a hit that cannot be signed cannot be deduplicated.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from oxbow.rules.errors import HitRateCeilingError, Suggestion
from oxbow.rules.events import Window
from oxbow.rules.settings import RulesSettings
from oxbow.rules.severity import MAX_SEVERITY, MIN_SEVERITY

OBSERVATION_KEY: Final = "observation"
THRESHOLD_PARAM_KEY: Final = "threshold_param"
THRESHOLD_VALUE_KEY: Final = "threshold_value"
TXN_IDS_KEY: Final = "txn_ids"
REQUIRED_EVIDENCE_KEYS: Final[tuple[str, ...]] = (
    OBSERVATION_KEY,
    THRESHOLD_PARAM_KEY,
    THRESHOLD_VALUE_KEY,
)

STATUS_OK: Final = "ok"
STATUS_TOO_HOT: Final = "above_ceiling"
STATUS_DEAD: Final = "below_floor"

# A pattern is its participants plus its legs. Hashing that pair — rather than the
# window it was found in — is what makes "counted once" survive a window boundary: the
# same three transactions either side of midnight produce the same digest.
SIGNATURE_DIGEST_LENGTH: Final[int] = 16


def pattern_signature(rule_id: str, account_key: str, *parts: object) -> str:
    """A stable digest for one observed pattern.

    ``json.dumps`` with sorted keys keeps the digest a function of the content only,
    so a dict built in a different order in a different run signs identically.
    """
    payload = json.dumps(
        {"rule_id": rule_id, "account": account_key, "parts": list(parts)},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:SIGNATURE_DIGEST_LENGTH]


@dataclass(frozen=True, slots=True)
class RuleHit:
    """One rule firing on one account. The unit the evidence panel renders."""

    rule_id: str
    rule_name: str
    account_key: str
    severity: float
    evidence: Mapping[str, object]
    window: Window
    hit_signature: str
    overlap_group: str | None = None

    def __post_init__(self) -> None:
        if not MIN_SEVERITY <= self.severity <= MAX_SEVERITY:
            raise ValueError(
                f"{self.rule_id} produced severity {self.severity} for {self.account_key!r}; "
                "severity is normalised to [0, 1] by construction (00 §5.1), so a value "
                "outside it means the rule's own saturating function was mis-wired."
            )
        if not self.hit_signature:
            raise ValueError(
                f"{self.rule_id} emitted a hit for {self.account_key!r} with no signature. "
                "Signature-grain dedup is the only guard against counting one pattern once "
                "per overlapping window, so an unsigned hit cannot be accepted."
            )
        missing = [key for key in REQUIRED_EVIDENCE_KEYS if key not in self.evidence]
        if missing:
            raise ValueError(
                f"{self.rule_id}'s evidence for {self.account_key!r} is missing {missing}. A "
                "hit that does not name its observation and the threshold it passed is a "
                "boolean with extra steps, and the run report could not suggest a fix."
            )

    @property
    def observation(self) -> float:
        return _number(self.evidence[OBSERVATION_KEY])

    @property
    def threshold_param(self) -> str:
        return str(self.evidence[THRESHOLD_PARAM_KEY])

    @property
    def threshold_value(self) -> float:
        return _number(self.evidence[THRESHOLD_VALUE_KEY])

    @property
    def txn_ids(self) -> tuple[str, ...]:
        raw = self.evidence.get(TXN_IDS_KEY, ())
        return tuple(str(item) for item in raw) if isinstance(raw, Sequence) else ()

    def sort_key(self) -> tuple[float, str, str]:
        """Deterministic order: severity desc, then account, then rule id.

        §9's tie-break is ``(severity, account_key)`` and the rule id is appended
        because one account can carry two hits of equal severity, and a report whose
        row order depends on dict insertion order is not a reproducible report.
        """
        return (-self.severity, self.account_key, self.rule_id)

    def as_json_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "account_key": self.account_key,
            "severity": round(self.severity, 6),
            "evidence": dict(self.evidence),
            "window": {
                "start_us": self.window.start_us,
                "end_us": self.window.end_us,
                "label": self.window.label,
            },
            "hit_signature": self.hit_signature,
            "overlap_group": self.overlap_group,
        }


@dataclass(frozen=True, slots=True)
class NearMiss:
    """A pattern the policy excluded, kept with the named reason that excluded it.

    This type is DEV-015's answer. A rule that silently returns nothing on the corpus's
    own labelled positives is definitionally blind and indistinguishable from a quiet
    day; a rule that returns *why* it looked away is auditable, and the reason strings
    are what a threshold discussion is held against.
    """

    rule_id: str
    pattern: tuple[str, ...]
    reasons: tuple[str, ...]
    measurements: Mapping[str, object]

    def as_json_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "pattern": list(self.pattern),
            "reasons": list(self.reasons),
            "measurements": dict(self.measurements),
        }


@dataclass(frozen=True, slots=True)
class EvidenceGroup:
    """One account's hits inside a single overlap group, collapsed to one unit."""

    account_key: str
    group: str
    rule_ids: tuple[str, ...]
    severity: float
    representative: RuleHit

    def as_json_dict(self) -> dict[str, object]:
        return {
            "account_key": self.account_key,
            "group": self.group,
            "rule_ids": list(self.rule_ids),
            "severity": round(self.severity, 6),
            "collapsed_from": [hit.hit_signature for hit in self._members],
        }

    _members: tuple[RuleHit, ...] = ()


@dataclass(frozen=True, slots=True)
class RuleHitRate:
    """One row of the per-run hit-rate report §9 demands."""

    rule_id: str
    rule_name: str
    accounts_hit: int
    accounts_scored: int
    hit_rate: float
    status: str
    ceiling: float
    floor: float
    suggestion: Suggestion | None = None
    note: str | None = None

    @property
    def within_ceiling(self) -> bool:
        return self.status != STATUS_TOO_HOT

    def as_json_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "accounts_hit": self.accounts_hit,
            "accounts_scored": self.accounts_scored,
            "hit_rate": round(self.hit_rate, 6),
            "status": self.status,
            "ceiling": self.ceiling,
            "floor": self.floor,
            "suggestion": None if self.suggestion is None else self.suggestion.as_dict(),
            "note": self.note,
        }

    def as_row(self) -> str:
        return (
            f"{self.rule_id:<4} {self.rule_name:<24} hits={self.accounts_hit:>6}/{self.accounts_scored:<6} "
            f"rate={self.hit_rate:>7.4f}  {self.status}"
            + ("" if self.suggestion is None else f" -> {self.suggestion.param}={self.suggestion.suggested}")
            + ("" if self.note is None else f"  [{self.note}]")
        )


@dataclass(frozen=True, slots=True)
class RemovedRule:
    """A rule taken out of the scored panel because it scored nothing, with the reason."""

    rule_id: str
    rule_name: str
    reason: str
    hit_rate: float
    reenable: str

    def as_json_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "reason": self.reason,
            "hit_rate": round(self.hit_rate, 6),
            "reenable": self.reenable,
        }


@dataclass(frozen=True, slots=True)
class ThresholdFit:
    """A run-time threshold, the window that produced it, and how it was derived.

    §9 pins thresholds with the window that produced them: a bare number in a report is
    indistinguishable from a number fitted on the test set, which is the leakage 03 G
    names. The label is what makes "fitted on the training window only" checkable.
    """

    name: str
    rule_id: str
    value: int
    derivation: str
    window: Window
    sample_size: int

    def as_json_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "rule_id": self.rule_id,
            "value": self.value,
            "derivation": self.derivation,
            "window": {
                "start_us": self.window.start_us,
                "end_us": self.window.end_us,
                "label": self.window.label,
            },
            "sample_size": self.sample_size,
        }


@dataclass(frozen=True, slots=True)
class RuleResult:
    """Everything one run of the twelve rules produced."""

    hits: tuple[RuleHit, ...]
    hit_rates: tuple[RuleHitRate, ...]
    evidence_groups: tuple[EvidenceGroup, ...]
    near_misses: tuple[NearMiss, ...]
    removed_rules: tuple[RemovedRule, ...]
    thresholds: tuple[ThresholdFit, ...]
    scored_accounts: int
    window: Window
    fit_window: Window
    settings_fingerprint: Mapping[str, object]
    cycle_search_truncated: bool
    cycle_search_visits: int
    cycle_search_reason: str | None
    chain_search_truncated: bool
    chain_search_reason: str | None
    self_transfer_count: int
    zero_value_excluded: int
    reversal_excluded: int
    currencies: tuple[str, ...]
    local_offset_hours: tuple[float, ...]

    def hits_for(self, account_key: str) -> tuple[RuleHit, ...]:
        return tuple(hit for hit in self.hits if hit.account_key == account_key)

    def rule_ids(self) -> tuple[str, ...]:
        return tuple(row.rule_id for row in self.hit_rates)

    def scored_rule_ids(self) -> tuple[str, ...]:
        """The rules the score may consume: present, and not removed as dead."""
        removed = {rule.rule_id for rule in self.removed_rules}
        return tuple(row.rule_id for row in self.hit_rates if row.rule_id not in removed)

    def evidence_units(self, account_key: str) -> int:
        """Distinct pieces of evidence for an account, overlap groups counted once.

        A hit carrying an overlap-group label is only collapsed when a second hit in the
        same group actually fired: a lone member of a group is one piece of evidence and
        must not vanish from the count, which is the mistake this method existed to make
        visible.
        """
        collapsed = {
            (group.account_key, rule_id) for group in self.evidence_groups for rule_id in group.rule_ids
        }
        units = 0
        seen_groups: set[tuple[str, str]] = set()
        for hit in self.hits:
            if hit.account_key != account_key:
                continue
            if hit.overlap_group is None:
                units += 1
                continue
            key = (hit.account_key, hit.overlap_group)
            if (hit.account_key, hit.rule_id) in collapsed:
                if key not in seen_groups:
                    seen_groups.add(key)
                    units += 1
                continue
            units += 1
        return units

    def as_json_dict(self) -> dict[str, object]:
        return {
            "window": {
                "start_us": self.window.start_us,
                "end_us": self.window.end_us,
                "label": self.window.label,
            },
            "fit_window": {
                "start_us": self.fit_window.start_us,
                "end_us": self.fit_window.end_us,
                "label": self.fit_window.label,
            },
            "scored_accounts": self.scored_accounts,
            "hits": [hit.as_json_dict() for hit in self.hits],
            "hit_rates": [row.as_json_dict() for row in self.hit_rates],
            "evidence_groups": [group.as_json_dict() for group in self.evidence_groups],
            "near_misses": [miss.as_json_dict() for miss in self.near_misses],
            "removed_rules": [rule.as_json_dict() for rule in self.removed_rules],
            "thresholds": [fit.as_json_dict() for fit in self.thresholds],
            "cycle_search_truncated": self.cycle_search_truncated,
            "cycle_search_visits": self.cycle_search_visits,
            "cycle_search_reason": self.cycle_search_reason,
            "chain_search_truncated": self.chain_search_truncated,
            "chain_search_reason": self.chain_search_reason,
            "self_transfer_count": self.self_transfer_count,
            "zero_value_excluded": self.zero_value_excluded,
            "reversal_excluded": self.reversal_excluded,
            "currencies": list(self.currencies),
            "local_offset_hours": list(self.local_offset_hours),
            "settings_fingerprint": dict(self.settings_fingerprint),
        }

    def hit_rate_table(self) -> str:
        """The printed report §9 asks for every run. Sorted by rule id, always twelve rows."""
        header = (
            f"{'id':<4} {'rule':<24} {'hits/scored':>14} {'rate':>9}  status"
            f"\n{'-' * 78}"
        )
        return "\n".join([header, *(row.as_row() for row in self.hit_rates)])


def deduplicate_by_signature(hits: Iterable[RuleHit]) -> tuple[RuleHit, ...]:
    """Collapse repeated sightings of one pattern into one hit (§9, ``pattern_signature``).

    The merged hit keeps the strongest severity — two windows that both rank the pattern
    should not disagree, and the louder ranking is the one a reviewer sees — and the union
    of their legs, so the evidence still names every transaction the pattern was made of.
    """
    merged: dict[tuple[str, str, str], RuleHit] = {}
    for hit in hits:
        key = (hit.rule_id, hit.account_key, hit.hit_signature)
        current = merged.get(key)
        if current is None:
            merged[key] = hit
            continue
        best, loser = (
            (current, hit) if current.severity >= hit.severity else (hit, current)
        )
        union = sorted({*best.txn_ids, *loser.txn_ids})
        evidence = dict(best.evidence)
        evidence[TXN_IDS_KEY] = union
        evidence["windows_observed"] = _number(evidence.get("windows_observed", 1)) + 1
        merged[key] = replace(best, evidence=evidence)
    return tuple(sorted(merged.values(), key=RuleHit.sort_key))


def collapse_overlap_groups(hits: Sequence[RuleHit]) -> tuple[EvidenceGroup, ...]:
    """One unit of evidence per (account, overlap group), at the group's loudest severity."""
    buckets: dict[tuple[str, str], list[RuleHit]] = {}
    for hit in hits:
        if hit.overlap_group is None:
            continue
        buckets.setdefault((hit.account_key, hit.overlap_group), []).append(hit)
    groups: list[EvidenceGroup] = []
    for (account, group), members in buckets.items():
        if len(members) < 2:
            # A group of one is not an overlap: nothing was doubled, so nothing collapses.
            continue
        members.sort(key=RuleHit.sort_key)
        loudest = members[0]
        groups.append(
            EvidenceGroup(
                account_key=account,
                group=group,
                rule_ids=tuple(sorted({hit.rule_id for hit in members})),
                severity=max(hit.severity for hit in members),
                representative=loudest,
                _members=tuple(members),
            )
        )
    return tuple(sorted(groups, key=lambda group: (group.account_key, group.group)))


def build_hit_rate_report(
    *,
    settings: RulesSettings,
    hits: Sequence[RuleHit],
    accounts_scored: int,
    dead_notes: Mapping[str, str],
) -> tuple[tuple[RuleHitRate, ...], tuple[RemovedRule, ...], list[HitRateCeilingError]]:
    """The per-rule rate, its status, a threshold suggestion, and the dead-rule list.

    The suggestion is derived, not invented: hits are ordered by observation and the
    new threshold is the observation at the rank the ceiling allows. That answers the
    question the operator actually has — "what value makes this rule stop shouting" —
    without pretending to know their risk appetite.
    """
    rows: list[RuleHitRate] = []
    removed: list[RemovedRule] = []
    failures: list[HitRateCeilingError] = []
    for rule_id in sorted(settings.specs, key=_rule_sort_index):
        spec = settings.spec(rule_id)
        hits_for_rule = sorted(
            (hit for hit in hits if hit.rule_id == rule_id),
            key=lambda hit: (-hit.observation, hit.account_key),
        )
        accounts_hit = len({hit.account_key for hit in hits_for_rule})
        rate = 0.0 if accounts_scored == 0 else accounts_hit / accounts_scored
        suggestion: Suggestion | None = None
        if rate > settings.hit_rate_ceiling:
            suggestion = _threshold_suggestion(rule_id, hits_for_rule, accounts_scored, settings)
            status = STATUS_TOO_HOT
            failures.append(
                HitRateCeilingError(
                    (
                        f"{rule_id} ({spec.name}) fired on {accounts_hit} of {accounts_scored} "
                        f"scored accounts = {rate:.1%}, above the {settings.hit_rate_ceiling:.0%} "
                        "ceiling. A rule this hot contributes no information per hit; the run is "
                        "refused rather than shipped. Suggested: "
                        + (
                            ", ".join(
                                f"{rule_id}.params.{item.param} -> {item.suggested}"
                                for item in (suggestion,)
                            )
                            if suggestion
                            else "no derivable suggestion"
                        )
                    ),
                    rule_id=rule_id,
                    hit_rate=rate,
                    accounts_hit=accounts_hit,
                    accounts_scored=accounts_scored,
                    ceiling=settings.hit_rate_ceiling,
                    suggested_params={} if suggestion is None else {suggestion.param: suggestion.suggested},
                )
            )
        elif rate < settings.hit_rate_floor:
            status = STATUS_DEAD
        else:
            status = STATUS_OK
        note = dead_notes.get(rule_id)
        rows.append(
            RuleHitRate(
                rule_id=rule_id,
                rule_name=spec.name,
                accounts_hit=accounts_hit,
                accounts_scored=accounts_scored,
                hit_rate=rate,
                status=status,
                ceiling=settings.hit_rate_ceiling,
                floor=settings.hit_rate_floor,
                suggestion=suggestion,
                note=note,
            )
        )
        if status == STATUS_DEAD and settings.engine.remove_dead_rules:
            removed.append(
                RemovedRule(
                    rule_id=rule_id,
                    rule_name=spec.name,
                    reason=(
                        f"0 hits on {accounts_scored} scored accounts; below the "
                        f"hit_rate_floor {settings.hit_rate_floor:g}. {note or ''}".strip()
                    ),
                    hit_rate=rate,
                    reenable=(
                        f"raise nothing: this is a reported result. Set "
                        f"rules.yaml.rule_engine.remove_dead_rules: false to keep {rule_id} "
                        "visible in the panel while it stays dead."
                    ),
                )
            )
    return tuple(rows), tuple(removed), failures


def _threshold_suggestion(
    rule_id: str, hits_for_rule: Sequence[RuleHit], accounts_scored: int, settings: RulesSettings
) -> Suggestion | None:
    """The threshold that would have kept this rule under the ceiling, or None."""
    if not hits_for_rule or accounts_scored <= 0:
        return None
    allowed = max(1, int(settings.hit_rate_ceiling * accounts_scored))
    if allowed > len(hits_for_rule):
        return None
    chosen = hits_for_rule[allowed - 1]
    if chosen.threshold_param == "none":
        # A rule whose hits are structural (a membership, not a measured ratio) has no
        # ratio to raise; saying so is honest, and printing a fake number would not be.
        return None
    return Suggestion(
        rule_id=rule_id,
        param=chosen.threshold_param,
        current=chosen.threshold_value,
        suggested=_round_up(chosen.observation),
        basis=(
            f"{allowed} of {accounts_scored} accounts is the ceiling; this is the observation "
            f"at that rank, rounded up so the account that set it also stops firing"
        ),
    )


def _round_up(value: float) -> float:
    """Nudge a proposed threshold clear of the observation that set it.

    Shares (a retained fraction, a cash-out share) step by 0.01 and cannot exceed 1.0;
    counts and z-scores get five percent. Rounding up rather than down is what makes the
    suggestion actually stop the hit, since every rule compares with ``>=``.
    """
    if value <= 1.0:
        # floor-then-step rather than ceil: 0.92 * 100 is 91.999... in binary, so ceil
        # would hand back 0.92 again and the rule would keep firing at the suggestion.
        return math.floor(value * 100 + 1e-6) / 100 + 0.01
    return round(value * 1.05, 4)


def _number(value: object) -> float:
    """Evidence values arrive as ``object``; the numerics in them are ours, checked here.

    Cast rather than assert-and-hope: a hit whose observation is a string would otherwise
    become a float comparison against zero, which is the wrong-number shape 03 A rule 2
    refuses. A TypeError here names the rule that filed the wrong type of evidence.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"expected a numeric piece of evidence, got {value!r}")
    return float(value)


def _rule_sort_index(rule_id: str) -> int:
    return int(rule_id[1:])


def attach_overlap_groups(hits: Iterable[RuleHit], settings: RulesSettings) -> tuple[RuleHit, ...]:
    """Stamp each hit with its §9 group, resolved through the rule's *name*."""
    return tuple(replace(hit, overlap_group=settings.group_for(hit.rule_id)) for hit in hits)


__all__ = [
    "REQUIRED_EVIDENCE_KEYS",
    "SIGNATURE_DIGEST_LENGTH",
    "STATUS_DEAD",
    "STATUS_OK",
    "STATUS_TOO_HOT",
    "EvidenceGroup",
    "HitRateCeilingError",
    "NearMiss",
    "RemovedRule",
    "RuleHit",
    "RuleHitRate",
    "RuleResult",
    "ThresholdFit",
    "attach_overlap_groups",
    "build_hit_rate_report",
    "collapse_overlap_groups",
    "deduplicate_by_signature",
    "pattern_signature",
]
