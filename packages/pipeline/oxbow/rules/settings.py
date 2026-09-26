"""Typed, fail-loud read of ``config/rules.yaml``.

Why this module exists separately from :mod:`oxbow.config` and
:mod:`oxbow.rules.registry`: ``PipelineConfig`` hands out ``dict`` sections, and a
``params.get("p", 0.8)`` deep inside a rule is where a typo in the YAML becomes a
threshold nobody chose (03 A rule 1, and the same stance ``oxbow.graph.settings``
takes for ``graph:``). Every tunable the twelve rules obey is resolved once, here,
with its config path named in the error.

Three behaviours are deliberate and stricter than they look:

* **No default is ever substituted.** A missing key raises. A rule that quietly
  ran on an assumed ``p = 0.8`` would report hits that no config file claims to
  authorise, and the run's config hash would not describe the thresholds that
  actually fired.
* **Unknown keys raise as loudly as missing ones.** ``config/rules.yaml`` is the
  spec for this layer; a key the code does not read is a knob that does nothing,
  and a knob that does nothing is worse than a missing one because it survives
  review. The one exception is the free-text bookkeeping the file carries
  (``description``, ``severity``, ``typology``, comments), which is declared here
  as read-but-not-executed rather than silently ignored.
* **The two §9 ceilings are validated against each other.** A ceiling below the
  floor would make every rule simultaneously too hot and too dead, and the error
  is cheaper here than in a run report.

§9's identity model is also established here: rules are addressed by id
(``R1``…``R12``) and by name (``RAPID_PASS_THROUGH``…), and both must be present
and agree, because ``overlap_groups`` and ``uses_local_hour`` are written with
names while every hit, report and downstream consumer is keyed by id.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from oxbow.config import find_repo_root, load_yaml
from oxbow.rules.errors import RuleConfigError

RULES_FILENAME: Final = "rules.yaml"
RULES_SECTION: Final = "rules"

# The twelve ids this layer must ship, in report order. Declared here rather than
# derived from the file, so a rule dropped from the YAML is a config error and not
# a silently smaller panel (03 A rule 2: an absent rule and a dead rule are
# different statements).
RULE_IDS: Final[tuple[str, ...]] = tuple(f"R{index}" for index in range(1, 13))

CURRENCY_POLICY_SAME: Final = "same_currency"
CURRENCY_POLICY_IGNORE: Final = "ignore_currency"
CURRENCY_POLICIES: Final[frozenset[str]] = frozenset({CURRENCY_POLICY_SAME, CURRENCY_POLICY_IGNORE})

DEDUPLICATE_BY_SIGNATURE: Final = "pattern_signature"

# The keys a rule block may carry alongside ``params``. ``severity`` is prose for the
# evidence panel — it documents how a hit is normalised, and is read and published on
# :class:`RuleSpec` rather than executed — while ``exclude_node_types`` is executed by
# R2/R3. Both are named explicitly so an unknown key cannot pass as one of them.
_BLOCK_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "name", "typology", "description", "params", "severity", "exclude_node_types"}
)

_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "hit_rate_ceiling",
        "hit_rate_floor",
        "overlap_groups",
        "uses_local_hour",
        "rules",
        "guards",
        "rule_engine",
    }
)

_GUARD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "window_overlap_hours",
        "deduplicate_by",
        "exclude_reversals_from",
        "exclude_zero_value_from_value_rules",
        "exclude_self_loops_from",
        "exclude_singletons_from_graph_aggregates",
    }
)

# Every knob §9 requires that the original spec file did not carry. One block rather
# than new keys inside ``rules[].params`` so the pinned-parameter self-check in
# ``tests/golden/expected.yaml`` keeps comparing the §9 table verbatim.
_ENGINE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "cycle_non_increasing",
        "cycle_currency_policy",
        "cycle_max_visits",
        "cycle_timeout_ms",
        "periodic_tolerance_ratio",
        "structuring_threshold_minor",
        "structuring_threshold_label",
        "odd_hour_recent_days",
        "remove_dead_rules",
        "fail_on_dead_rule",
        "emit_cycle_near_misses",
    }
)


# --- scalar coercion ------------------------------------------------------
# Written here rather than imported from oxbow.graph.settings: the graph's helpers
# are private and raise GraphConfigError, and a rules-config typo must be
# attributable to the rules layer by type as well as by message.


def _require(mapping: Mapping[str, object], path: str, owner: str) -> object:
    value = mapping.get(path)
    if value is None or (isinstance(value, float) and value != value):
        raise RuleConfigError(f"config {owner}.{path} is missing; no default is substituted")
    return value


def _ratio(mapping: Mapping[str, object], path: str, *, owner: str) -> float:
    """A dimensionless number in [0, 1]: a retention floor, a share, a weight."""
    value = _require(mapping, path, owner)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuleConfigError(f"config {owner}.{path} must be a number in [0, 1], got {value!r}")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise RuleConfigError(f"config {owner}.{path} must be in [0, 1], got {number}")
    return number


def _positive(mapping: Mapping[str, object], path: str, *, owner: str, minimum: float) -> float:
    value = _require(mapping, path, owner)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuleConfigError(f"config {owner}.{path} must be a number, got {value!r}")
    number = float(value)
    if number < minimum:
        raise RuleConfigError(f"config {owner}.{path} must be >= {minimum}, got {number}")
    return number


def _integer(mapping: Mapping[str, object], path: str, *, owner: str, minimum: int) -> int:
    value = _require(mapping, path, owner)
    # bool is a subclass of int, so `k: true` would otherwise sail through as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleConfigError(f"config {owner}.{path} must be an int, got {value!r}")
    if value < minimum:
        raise RuleConfigError(f"config {owner}.{path} must be >= {minimum}, got {value}")
    return int(value)


def _boolean(mapping: Mapping[str, object], path: str, *, owner: str) -> bool:
    value = _require(mapping, path, owner)
    if not isinstance(value, bool):
        raise RuleConfigError(f"config {owner}.{path} must be a bool, got {value!r}")
    return value


def _text(mapping: Mapping[str, object], path: str, *, owner: str) -> str:
    value = _require(mapping, path, owner)
    if not isinstance(value, str) or not value.strip():
        raise RuleConfigError(f"config {owner}.{path} must be a non-empty string, got {value!r}")
    return value.strip()


def _mapping(value: object, *, owner: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuleConfigError(f"config {owner} must be a mapping, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _names(value: object, *, owner: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuleConfigError(f"config {owner} must be a list of strings, got {value!r}")
    # Sorted, not source order: two runs whose YAML is written in a different order
    # must exclude the same node types and the same transaction types.
    return tuple(sorted({item.strip() for item in value}))


def _reject_unknown(present: Iterable[str], allowed: Iterable[str], *, owner: str) -> None:
    allowed_set = set(allowed)
    stray = sorted({str(key) for key in present} - allowed_set)
    if stray:
        raise RuleConfigError(
            f"config {owner} carries keys this layer does not read: {stray}. A knob the "
            f"code ignores is a knob that does nothing; either wire it or delete it. "
            f"Known: {sorted(allowed_set)}"
        )


# --- per-rule settings ----------------------------------------------------
# One frozen dataclass per rule rather than a shared bag of optional fields, so
# `rule.params["delta_minutes"]` cannot be read by the rule that has no such knob.


@dataclass(frozen=True, slots=True)
class PassThroughSettings:
    """R1. Money in, money out, quickly, in the same currency."""

    p: float
    delta_minutes: int
    min_amount_minor: int


@dataclass(frozen=True, slots=True)
class FanInSettings:
    """R2. ``tau_minor`` is absent until a run fits it on the training window."""

    k: int
    window_hours: int
    tau_source: str
    tau_percentile: float
    exclude_node_types: tuple[str, ...]
    tau_minor: int | None = None


@dataclass(frozen=True, slots=True)
class FanOutSettings:
    k: int
    window_hours: int
    exclude_node_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CycleMemberSettings:
    """R4. The §9 shape, with the DEV-015 knobs read from ``rule_engine``.

    ``retention`` stays at the §9 value because silently changing it would move the
    definition of the typology without a decision entry. ``non_increasing`` and
    ``currency_policy`` are exposed so a run can *report what each knob excludes*
    instead of reporting nothing.
    """

    retention: float
    min_length: int
    max_length: int
    require_strict_time_increase: bool
    down_weight_periodic: bool
    periodic_period_hours: int
    periodic_down_weight: float
    periodic_tolerance_ratio: float
    non_increasing: bool
    currency_policy: str
    max_visits: int
    timeout_ms: int
    emit_near_misses: bool


@dataclass(frozen=True, slots=True)
class StructuringSettings:
    """R5. ``threshold_minor`` is the pinned synthetic T; ``None`` means derive."""

    n: int
    window_days: int
    threshold_source: str
    threshold_band_low: float
    threshold_band_high: float
    synthetic: bool
    threshold_minor: int | None
    threshold_label: str


@dataclass(frozen=True, slots=True)
class VelocitySettings:
    """R6. MAD-robust, and the observation day is inside the baseline it is scored
    against — that is what stops a single enormous day inflating its own z."""

    z: float
    baseline_days: int
    min_baseline_days: int
    robust_scale: str


@dataclass(frozen=True, slots=True)
class DormantSettings:
    dormant_days: int
    k: int
    window_hours: int


@dataclass(frozen=True, slots=True)
class OddHourSettings:
    q: float
    min_transactions: int
    quiet_hours_source: str
    recent_days: int


@dataclass(frozen=True, slots=True)
class RegimeSettings:
    m: float
    recent_days: int
    baseline_days: int


@dataclass(frozen=True, slots=True)
class CashOutSettings:
    s: float
    holding_hours: int
    # A taxonomy constant, not a tunable, so it is not read from YAML and there is no
    # threshold hiding in it: both corpora type a withdrawal as CASH_OUT (PaySim's own
    # five-value alphabet, and the golden fixture's six-value one), and 01 B forbids
    # inferring a cash-out from the counterparty instead.
    cash_out_txn_types: tuple[str, ...] = ("CASH_OUT",)


@dataclass(frozen=True, slots=True)
class NewCounterpartySettings:
    g: float
    c: int
    window_hours: int


@dataclass(frozen=True, slots=True)
class ChainSettings:
    """R12. Bounded three ways: depth, per-component visits, wall clock."""

    min_length: int
    delta_hours: int
    decay: float
    require_non_increasing: bool
    max_depth: int
    component_budget: int
    timeout_ms: int


RuleSettings = (
    PassThroughSettings
    | FanInSettings
    | FanOutSettings
    | CycleMemberSettings
    | StructuringSettings
    | VelocitySettings
    | DormantSettings
    | OddHourSettings
    | RegimeSettings
    | CashOutSettings
    | NewCounterpartySettings
    | ChainSettings
)


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """The identity of one rule, as the evidence panel and the run report cite it."""

    rule_id: str
    name: str
    typology: str
    description: str
    severity_note: str
    params: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Guards:
    """§9's global guards, resolved."""

    window_overlap_hours: int
    deduplicate_by: str
    exclude_reversals_from: frozenset[str]
    exclude_zero_value_from_value_rules: bool
    exclude_self_loops_from: frozenset[str]
    exclude_singletons_from_graph_aggregates: bool

    def excludes_reversals(self, rule_id: str) -> bool:
        return rule_id in self.exclude_reversals_from

    def excludes_self_loops(self, rule_id: str) -> bool:
        return rule_id in self.exclude_self_loops_from


@dataclass(frozen=True, slots=True)
class RuleEngineSettings:
    """The P3b knobs that live outside the §9 params table (see module docstring)."""

    cycle_non_increasing: bool
    cycle_currency_policy: str
    cycle_max_visits: int
    cycle_timeout_ms: int
    periodic_tolerance_ratio: float
    structuring_threshold_minor: int
    structuring_threshold_label: str
    odd_hour_recent_days: int
    remove_dead_rules: bool
    fail_on_dead_rule: bool
    emit_cycle_near_misses: bool


@dataclass(frozen=True, slots=True)
class RulesSettings:
    """Everything ``config/rules.yaml`` says about how rules behave."""

    path: Path
    hit_rate_ceiling: float
    hit_rate_floor: float
    overlap_groups: Mapping[str, tuple[str, ...]]
    group_of_rule: Mapping[str, str]
    uses_local_hour: frozenset[str]
    guards: Guards
    engine: RuleEngineSettings
    specs: Mapping[str, RuleSpec]
    settings: Mapping[str, RuleSettings]

    def spec(self, rule_id: str) -> RuleSpec:
        try:
            return self.specs[rule_id]
        except KeyError as exc:
            raise RuleConfigError(f"no rule {rule_id!r} in {self.path}") from exc

    def settings_for(self, rule_id: str) -> RuleSettings:
        try:
            return self.settings[rule_id]
        except KeyError as exc:
            raise RuleConfigError(f"no settings resolved for rule {rule_id!r}") from exc

    def group_for(self, rule_id: str) -> str | None:
        """The overlap group this rule belongs to, if any (§9: count the group once)."""
        spec = self.spec(rule_id)
        return self.group_of_rule.get(spec.name)

    def fingerprint(self) -> dict[str, object]:
        """Every threshold that can move a hit, flattened and ready to hash.

        Recorded with each run (01 A rule 4): a hit-rate table whose thresholds are
        not pinned to a config hash cannot be re-run by anyone, which makes the
        ceiling report a story rather than a measurement.
        """
        payload: dict[str, object] = {
            "hit_rate_ceiling": self.hit_rate_ceiling,
            "hit_rate_floor": self.hit_rate_floor,
            "uses_local_hour": sorted(self.uses_local_hour),
            "overlap_groups": {
                name: list(members) for name, members in self.overlap_groups.items()
            },
            "guards": {
                "window_overlap_hours": self.guards.window_overlap_hours,
                "deduplicate_by": self.guards.deduplicate_by,
                "exclude_reversals_from": sorted(self.guards.exclude_reversals_from),
                "exclude_zero_value_from_value_rules": (
                    self.guards.exclude_zero_value_from_value_rules
                ),
                "exclude_self_loops_from": sorted(self.guards.exclude_self_loops_from),
                "exclude_singletons_from_graph_aggregates": (
                    self.guards.exclude_singletons_from_graph_aggregates
                ),
            },
            "rule_engine": {
                "cycle_non_increasing": self.engine.cycle_non_increasing,
                "cycle_currency_policy": self.engine.cycle_currency_policy,
                "cycle_max_visits": self.engine.cycle_max_visits,
                "cycle_timeout_ms": self.engine.cycle_timeout_ms,
                "periodic_tolerance_ratio": self.engine.periodic_tolerance_ratio,
                "structuring_threshold_minor": self.engine.structuring_threshold_minor,
                "odd_hour_recent_days": self.engine.odd_hour_recent_days,
                "remove_dead_rules": self.engine.remove_dead_rules,
                "emit_cycle_near_misses": self.engine.emit_cycle_near_misses,
            },
            "rules": {
                rule_id: _flatten_params(_as_mapping(item))
                for rule_id, item in _raw_rule_params(self).items()
            },
        }
        return payload


def _as_mapping(value: object) -> Mapping[str, object]:
    return {} if not isinstance(value, Mapping) else {str(k): v for k, v in value.items()}


def _raw_rule_params(settings: RulesSettings) -> Mapping[str, Mapping[str, object]]:
    """The raw params actually read, recovered from the resolved dataclasses.

    Deliberately rebuilt from the frozen dataclasses instead of echoing the YAML:
    the hash must describe the values the code executed, not the values the file
    happens to spell, and the two can only diverge in one direction — this one.
    """
    from dataclasses import asdict

    return {rule_id: _as_mapping(asdict(item)) for rule_id, item in settings.settings.items()}


def _flatten_params(params: Mapping[str, object]) -> dict[str, object]:
    rendered: dict[str, object] = {}
    for key in sorted(params):
        value = params[key]
        rendered[key] = list(value) if isinstance(value, tuple) else value
    return rendered


def load_rules_settings(root: Path | None = None) -> RulesSettings:
    """Load and validate ``config/rules.yaml``, or raise. There is no partial load."""
    repo_root = (root or find_repo_root()).resolve()
    path = repo_root / "config" / RULES_FILENAME
    raw = load_yaml(path)
    _reject_unknown(raw, _TOP_LEVEL_KEYS, owner="rules.yaml")

    ceiling = _ratio(raw, "hit_rate_ceiling", owner="rules.yaml")
    floor = _ratio(raw, "hit_rate_floor", owner="rules.yaml")
    if ceiling <= floor:
        raise RuleConfigError(
            f"config rules.yaml hit_rate_ceiling ({ceiling}) must exceed hit_rate_floor "
            f"({floor}); with them inverted or equal, every rule is simultaneously too hot "
            "and too dead, and the run report cannot name either failure."
        )

    overlap_raw = _mapping(
        _require(raw, "overlap_groups", "rules.yaml"), owner="rules.yaml.overlap_groups"
    )
    overlap_groups = {
        str(name): _names(members, owner=f"rules.yaml.overlap_groups.{name}")
        for name, members in overlap_raw.items()
    }
    group_of_rule: dict[str, str] = {}
    for group, members in overlap_groups.items():
        for member in members:
            if member in group_of_rule:
                raise RuleConfigError(
                    f"rule name {member!r} appears in overlap groups {group_of_rule[member]!r} "
                    f"and {group!r}. A rule in two groups makes 'count the group once' "
                    "ambiguous, which is the exact doubling this table exists to stop."
                )
            group_of_rule[member] = group

    uses_local_hour = frozenset(
        _names(_require(raw, "uses_local_hour", "rules.yaml"), owner="rules.yaml.uses_local_hour")
    )

    rules_list = raw.get(RULES_SECTION)
    if not isinstance(rules_list, list):
        raise RuleConfigError("config rules.yaml rules must be a list of rule blocks")
    engine = _resolve_engine(_mapping(raw.get("rule_engine") or {}, owner="rules.yaml.rule_engine"))
    specs, settings_by_id, names = _resolve_rules(rules_list, path=path, engine=engine)

    missing_ids = sorted(set(RULE_IDS) - set(specs))
    if missing_ids:
        raise RuleConfigError(
            f"config rules.yaml declares no rule for {missing_ids}; this layer ships "
            f"exactly {list(RULE_IDS)} and a rule that vanished from the config must be "
            "removed in a decision entry, not by deleting a stanza"
        )
    known_names = set(names.values())
    unknown_names = sorted((set(group_of_rule) | set(uses_local_hour)) - known_names)
    if unknown_names:
        raise RuleConfigError(
            f"overlap_groups/uses_local_hour name rules that do not exist: {unknown_names}"
        )

    guards = _resolve_guards(
        _mapping(_require(raw, "guards", "rules.yaml"), owner="rules.yaml.guards"),
        names=names,
    )

    return RulesSettings(
        path=path,
        hit_rate_ceiling=ceiling,
        hit_rate_floor=floor,
        overlap_groups=overlap_groups,
        group_of_rule=dict(group_of_rule),
        uses_local_hour=uses_local_hour,
        guards=guards,
        engine=engine,
        specs=specs,
        settings=settings_by_id,
    )


def _resolve_guards(raw: Mapping[str, object], *, names: Mapping[str, str]) -> Guards:
    _reject_unknown(raw, _GUARD_KEYS, owner="rules.yaml.guards")
    deduplicate_by = _text(raw, "deduplicate_by", owner="rules.yaml.guards")
    if deduplicate_by != DEDUPLICATE_BY_SIGNATURE:
        raise RuleConfigError(
            f"config rules.yaml.guards.deduplicate_by is {deduplicate_by!r}; this layer "
            f"implements {DEDUPLICATE_BY_SIGNATURE!r} only. Window-grain deduplication is "
            "the defect the guard exists to fix — a structuring ladder that crosses "
            "midnight would otherwise be reported once per day."
        )
    id_of_name = {name: rule_id for rule_id, name in names.items()}

    def ids_for(value: object, *, owner: str) -> frozenset[str]:
        resolved: set[str] = set()
        for name in _names(value, owner=owner):
            rule_id = id_of_name.get(name)
            if rule_id is None:
                raise RuleConfigError(
                    f"config {owner} names {name!r}, which is not one of the twelve rule "
                    f"names {sorted(id_of_name)}. A guard written against a name that no "
                    "longer exists excludes nothing while looking like it excludes something."
                )
            resolved.add(rule_id)
        return frozenset(resolved)

    return Guards(
        window_overlap_hours=_integer(
            raw, "window_overlap_hours", owner="rules.yaml.guards", minimum=0
        ),
        deduplicate_by=deduplicate_by,
        exclude_reversals_from=ids_for(
            _require(raw, "exclude_reversals_from", "rules.yaml.guards"),
            owner="rules.yaml.guards.exclude_reversals_from",
        ),
        exclude_zero_value_from_value_rules=_boolean(
            raw, "exclude_zero_value_from_value_rules", owner="rules.yaml.guards"
        ),
        exclude_self_loops_from=ids_for(
            _require(raw, "exclude_self_loops_from", "rules.yaml.guards"),
            owner="rules.yaml.guards.exclude_self_loops_from",
        ),
        exclude_singletons_from_graph_aggregates=_boolean(
            raw, "exclude_singletons_from_graph_aggregates", owner="rules.yaml.guards"
        ),
    )


def _resolve_engine(raw: Mapping[str, object]) -> RuleEngineSettings:
    _reject_unknown(raw, _ENGINE_KEYS, owner="rules.yaml.rule_engine")
    policy = _text(raw, "cycle_currency_policy", owner="rules.yaml.rule_engine")
    if policy not in CURRENCY_POLICIES:
        raise RuleConfigError(
            f"config rules.yaml.rule_engine.cycle_currency_policy is {policy!r}; expected one "
            f"of {sorted(CURRENCY_POLICIES)}. 'converted' is deliberately absent: DEV-015's "
            "conversion path needs a declared rate block in config/economics.yaml, which this "
            "repository does not ship, and an undocumented rate is the implicit-FX error "
            "01 B forbids."
        )
    return RuleEngineSettings(
        cycle_non_increasing=_boolean(raw, "cycle_non_increasing", owner="rules.yaml.rule_engine"),
        cycle_currency_policy=policy,
        cycle_max_visits=_integer(
            raw, "cycle_max_visits", owner="rules.yaml.rule_engine", minimum=1
        ),
        cycle_timeout_ms=_integer(
            raw, "cycle_timeout_ms", owner="rules.yaml.rule_engine", minimum=1
        ),
        periodic_tolerance_ratio=_ratio(
            raw, "periodic_tolerance_ratio", owner="rules.yaml.rule_engine"
        ),
        structuring_threshold_minor=_integer(
            raw, "structuring_threshold_minor", owner="rules.yaml.rule_engine", minimum=1
        ),
        structuring_threshold_label=_text(
            raw, "structuring_threshold_label", owner="rules.yaml.rule_engine"
        ),
        odd_hour_recent_days=_integer(
            raw, "odd_hour_recent_days", owner="rules.yaml.rule_engine", minimum=1
        ),
        remove_dead_rules=_boolean(raw, "remove_dead_rules", owner="rules.yaml.rule_engine"),
        fail_on_dead_rule=_boolean(raw, "fail_on_dead_rule", owner="rules.yaml.rule_engine"),
        emit_cycle_near_misses=_boolean(
            raw, "emit_cycle_near_misses", owner="rules.yaml.rule_engine"
        ),
    )


def _resolve_rules(
    rules_list: list[object],
    *,
    path: Path,
    engine: RuleEngineSettings,
) -> tuple[dict[str, RuleSpec], dict[str, RuleSettings], dict[str, str]]:
    specs: dict[str, RuleSpec] = {}
    resolved: dict[str, RuleSettings] = {}
    names: dict[str, str] = {}
    seen_ids: set[str] = set()
    for block in rules_list:
        raw = _mapping(block, owner=f"{path.name} rules[]")
        rule_id = _text(raw, "id", owner="rules.yaml.rules[]")
        name = _text(raw, "name", owner=f"{rule_id}")
        if rule_id in seen_ids:
            raise RuleConfigError(f"config {path.name} declares rule id {rule_id!r} twice")
        seen_ids.add(rule_id)
        if rule_id not in RULE_IDS:
            raise RuleConfigError(
                f"config {path.name} declares rule {rule_id!r}, outside the {list(RULE_IDS)} "
                "this layer ships"
            )
        params = _mapping(_require(raw, "params", owner=rule_id), owner=f"{rule_id}.params")
        _reject_unknown(params, _ALLOWED_PARAMS[rule_id], owner=f"{rule_id}.params")
        _reject_unknown(raw.keys(), _BLOCK_KEYS, owner=rule_id)
        specs[rule_id] = RuleSpec(
            rule_id=rule_id,
            name=name,
            typology=_text(raw, "typology", owner=rule_id),
            description=_text(raw, "description", owner=rule_id),
            severity_note=_text(raw, "severity", owner=rule_id),
            params=params,
        )
        names[rule_id] = name
        resolved[rule_id] = _settings_for(
            rule_id,
            params,
            engine=engine,
            exclude_node_types=_names(
                raw.get("exclude_node_types", []), owner=f"{rule_id}.exclude_node_types"
            ),
        )
    return specs, resolved, names


def _settings_for(
    rule_id: str,
    params: Mapping[str, object],
    *,
    engine: RuleEngineSettings,
    exclude_node_types: tuple[str, ...],
) -> RuleSettings:
    owner = f"{rule_id}.params"
    match rule_id:
        case "R1":
            return PassThroughSettings(
                p=_ratio(params, "p", owner=owner),
                delta_minutes=_integer(params, "delta_minutes", owner=owner, minimum=1),
                min_amount_minor=_integer(params, "min_amount_minor", owner=owner, minimum=1),
            )
        case "R2":
            source = _text(params, "tau_source", owner=owner)
            if source != "corpus_percentile":
                raise RuleConfigError(
                    f"config {owner}.tau_source is {source!r}; the only fitter this layer "
                    "implements is 'corpus_percentile', because any other source would be a "
                    "threshold nobody can point at in the training window."
                )
            return FanInSettings(
                k=_integer(params, "k", owner=owner, minimum=2),
                window_hours=_integer(params, "window_hours", owner=owner, minimum=1),
                tau_source=source,
                tau_percentile=_positive(params, "tau_percentile", owner=owner, minimum=0.1),
                exclude_node_types=exclude_node_types,
            )
        case "R3":
            return FanOutSettings(
                k=_integer(params, "k", owner=owner, minimum=2),
                window_hours=_integer(params, "window_hours", owner=owner, minimum=1),
                exclude_node_types=exclude_node_types,
            )
        case "R4":
            retention = _ratio(params, "retention", owner=owner)
            min_length = _integer(params, "min_length", owner=owner, minimum=2)
            max_length = _integer(params, "max_length", owner=owner, minimum=min_length)
            period_hours = _integer(params, "periodic_period_hours", owner=owner, minimum=1)
            return CycleMemberSettings(
                retention=retention,
                min_length=min_length,
                max_length=max_length,
                require_strict_time_increase=_boolean(
                    params, "require_strict_time_increase", owner=owner
                ),
                down_weight_periodic=_boolean(params, "down_weight_periodic", owner=owner),
                periodic_period_hours=period_hours,
                periodic_down_weight=_ratio(params, "periodic_down_weight", owner=owner),
                periodic_tolerance_ratio=engine.periodic_tolerance_ratio,
                non_increasing=engine.cycle_non_increasing,
                currency_policy=engine.cycle_currency_policy,
                max_visits=engine.cycle_max_visits,
                timeout_ms=engine.cycle_timeout_ms,
                emit_near_misses=engine.emit_cycle_near_misses,
            )
        case "R5":
            source = _text(params, "threshold_source", owner=owner)
            if source not in {"histogram_mode"}:
                raise RuleConfigError(
                    f"config {owner}.threshold_source is {source!r}; the only derivation "
                    "implemented is 'histogram_mode'. The fallback value and its label live "
                    "in rules.yaml.rule_engine, so the synthetic threshold is pinned by "
                    "config and never invented by the rule (03 G "
                    "test_structuring_threshold_is_configured)."
                )
            low = _ratio(params, "threshold_band_low", owner=owner)
            high = _ratio(params, "threshold_band_high", owner=owner)
            if low >= high:
                raise RuleConfigError(
                    f"config {owner}.threshold_band_low ({low}) must be < high ({high})"
                )
            return StructuringSettings(
                n=_integer(params, "n", owner=owner, minimum=2),
                window_days=_integer(params, "window_days", owner=owner, minimum=1),
                threshold_source=source,
                threshold_band_low=low,
                threshold_band_high=high,
                synthetic=_boolean(params, "synthetic", owner=owner),
                threshold_minor=engine.structuring_threshold_minor,
                threshold_label=engine.structuring_threshold_label,
            )
        case "R6":
            scale = _text(params, "robust_scale", owner=owner)
            if scale != "mad":
                raise RuleConfigError(
                    f"config {owner}.robust_scale is {scale!r}; 'mad' is the whole point of "
                    "the rule — a standard-deviation baseline is set by the very spike it is "
                    "measuring."
                )
            return VelocitySettings(
                z=_positive(params, "z", owner=owner, minimum=1.0),
                baseline_days=_integer(params, "baseline_days", owner=owner, minimum=2),
                min_baseline_days=_integer(params, "min_baseline_days", owner=owner, minimum=1),
                robust_scale=scale,
            )
        case "R7":
            return DormantSettings(
                dormant_days=_integer(params, "dormant_days", owner=owner, minimum=1),
                k=_integer(params, "k", owner=owner, minimum=2),
                window_hours=_integer(params, "window_hours", owner=owner, minimum=1),
            )
        case "R8":
            source = _text(params, "quiet_hours_source", owner=owner)
            if source != "account_decile":
                raise RuleConfigError(
                    f"config {owner}.quiet_hours_source is {source!r}; quiet hours are the "
                    "account's own low-volume decile, because a global night definition "
                    "flags an entire timezone's ordinary evenings (03 C)."
                )
            return OddHourSettings(
                q=_ratio(params, "q", owner=owner),
                min_transactions=_integer(params, "min_transactions", owner=owner, minimum=1),
                quiet_hours_source=source,
                recent_days=engine.odd_hour_recent_days,
            )
        case "R9":
            m = _positive(params, "m", owner=owner, minimum=1.000001)
            return RegimeSettings(
                m=float(m),
                recent_days=_integer(params, "recent_days", owner=owner, minimum=1),
                baseline_days=_integer(params, "baseline_days", owner=owner, minimum=1),
            )
        case "R10":
            return CashOutSettings(
                s=_ratio(params, "s", owner=owner),
                holding_hours=_integer(params, "holding_hours", owner=owner, minimum=1),
            )
        case "R11":
            return NewCounterpartySettings(
                g=_ratio(params, "g", owner=owner),
                c=_integer(params, "c", owner=owner, minimum=2),
                window_hours=_integer(params, "window_hours", owner=owner, minimum=1),
            )
        case "R12":
            min_length = _integer(params, "min_length", owner=owner, minimum=2)
            max_depth = _integer(params, "max_depth", owner=owner, minimum=min_length)
            return ChainSettings(
                min_length=min_length,
                delta_hours=_integer(params, "delta_hours", owner=owner, minimum=1),
                decay=_ratio(params, "decay", owner=owner),
                require_non_increasing=_boolean(params, "require_non_increasing", owner=owner),
                max_depth=max_depth,
                component_budget=_integer(params, "component_budget", owner=owner, minimum=1),
                timeout_ms=_integer(params, "timeout_ms", owner=owner, minimum=1),
            )
    raise RuleConfigError(f"no settings resolver for {rule_id!r}")  # pragma: no cover


_ALLOWED_PARAMS: Final[Mapping[str, frozenset[str]]] = {
    "R1": frozenset({"p", "delta_minutes", "min_amount_minor"}),
    "R2": frozenset({"k", "window_hours", "tau_source", "tau_percentile"}),
    "R3": frozenset({"k", "window_hours"}),
    "R4": frozenset(
        {
            "retention",
            "min_length",
            "max_length",
            "require_strict_time_increase",
            "down_weight_periodic",
            "periodic_period_hours",
            "periodic_down_weight",
        }
    ),
    "R5": frozenset(
        {
            "n",
            "window_days",
            "threshold_source",
            "threshold_band_low",
            "threshold_band_high",
            "synthetic",
        }
    ),
    "R6": frozenset({"z", "baseline_days", "min_baseline_days", "robust_scale"}),
    "R7": frozenset({"dormant_days", "k", "window_hours"}),
    "R8": frozenset({"q", "min_transactions", "quiet_hours_source"}),
    "R9": frozenset({"m", "recent_days", "baseline_days"}),
    "R10": frozenset({"s", "holding_hours"}),
    "R11": frozenset({"g", "c", "window_hours"}),
    "R12": frozenset(
        {
            "min_length",
            "delta_hours",
            "decay",
            "require_non_increasing",
            "max_depth",
            "component_budget",
            "timeout_ms",
        }
    ),
}


__all__ = [
    "CURRENCY_POLICY_IGNORE",
    "CURRENCY_POLICY_SAME",
    "DEDUPLICATE_BY_SIGNATURE",
    "RULE_IDS",
    "CashOutSettings",
    "ChainSettings",
    "CycleMemberSettings",
    "DormantSettings",
    "FanInSettings",
    "FanOutSettings",
    "Guards",
    "NewCounterpartySettings",
    "OddHourSettings",
    "PassThroughSettings",
    "RegimeSettings",
    "RuleEngineSettings",
    "RuleSettings",
    "RuleSpec",
    "RulesSettings",
    "StructuringSettings",
    "VelocitySettings",
    "load_rules_settings",
]
