"""The feature registry: load config/features.yaml, validate it, hash it.

WHY IT LIVES IN CONFIG. Plan §8 makes the registry the declaration of what the
feature layer computes: "Every feature in config/features.yaml declares an as-of
timestamp and a lookback window." The consequence this module enforces is that the
Python side holds no feature list — `kinds.py` dispatches on the declared `kind`, so
an undeclared feature cannot be computed and a declared feature cannot be skipped
silently. The vocabulary constants below are the schema; `kinds.py` asserts at import
that its dispatch table covers exactly `KIND_NAMES`, which is what keeps the two
declared lists from drifting apart in either direction.

WHY IT FAILS LOUDLY. An unknown kind, a missing lookback, a blank sentence, a float
column named as money: each is a boundary failure, and the alternative is a feature
table that looks complete while one column quietly means something other than what
the scorecard's label claims. 03 A rule 1: fail loud at the boundary. Every message
names the feature it is about, because "the registry is invalid" is not actionable in
the middle of a run.

WHY THE HASH COVERS THE SENTENCES. The plan requires the hash to cover the ordered
ids, the declared windows and the code version. This covers the whole declared
parameter set, sentences included: the sentence is the SHAP panel's dictionary entry
and the scorecard's attribute label, so a changed sentence is a changed explanation
even where the arithmetic is identical. Over-invalidating downstream artifacts is
annoying; scoring against a matrix whose published labels no longer match is a defect.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final

import polars as pl

from oxbow.config import CONFIG_DIRNAME, ConfigError, load_yaml

FEATURES_FILENAME: Final = "features.yaml"

ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*$")
DURATION_PATTERN: Final = re.compile(r"^(\d+)([hd])$")

# Window values that mean "this kind does not use a bounded rolling window". They are
# written explicitly rather than left out: an absent lookback is indistinguishable
# from an omitted one, and plan §8 treats a missing lookback as a load failure.
NON_ROLLING_WINDOWS: Final[frozenset[str]] = frozenset({"lifetime", "point_in_time", "fold_scoped"})

# The accepted kinds. kinds.py must implement exactly this set, checked at import.
#
# `forward_window` is in the vocabulary for one reason: the quant layer has to compute
# exposure E_i over the recovery window *after* the trigger (plan §11), and the leakage
# gate has to be demonstrable on a column that really does read the future. The loader
# below makes it structurally unavailable to the matrix — a forward window may only be
# declared with `role: outcome` — so the same kernel that serves the outcome layer
# serves as the witness that the gate bites.
KIND_NAMES: Final[frozenset[str]] = frozenset(
    {
        "window_agg",
        "forward_window",
        "distinct_in_window",
        "cumulative_distinct",
        "first_seen_flag",
        "quotient_int",
        "diff_int",
        "row_flag",
        "row_value",
        "event_field",
        "float_stat",
        "recency",
        "cumulative",
        "graph_node",
        "rule_field",
    }
)
FORWARD_KINDS: Final[frozenset[str]] = frozenset({"forward_window"})

WINDOW_AGGREGATIONS: Final[frozenset[str]] = frozenset(
    {"count", "sum", "mean_int", "min", "max", "median", "quantile", "std"}
)
# Aggregations whose empty window is a genuine zero rather than an unknown.
COUNT_LIKE_AGGREGATIONS: Final[frozenset[str]] = frozenset({"count", "sum"})
# Aggregations that cannot answer about an empty window, so null_policy must say so.
ORDER_STAT_AGGREGATIONS: Final[frozenset[str]] = frozenset({"median", "quantile", "min", "max"})
# Aggregations computable as a difference of two running totals, which is what makes a
# *filtered* window exact at every anchor row: the window is (cutoff - w, cutoff] and a
# running total evaluated at both ends answers for a row the filter excluded, where a
# grouped rolling window evaluated at the last included row would answer for a wider
# window than the anchor owns. `std` rides along because Var = (Sxx - Sx^2/n)/(n-1).
CUMULATIVE_AGGREGATIONS_OK: Final[frozenset[str]] = frozenset(
    {"count", "sum", "mean_int", "std", "mean_float"}
)
CUMULATIVE_AGGREGATIONS: Final[frozenset[str]] = frozenset({"count", "sum", "age_days"})

# Row fields the kernels may read. `event_ts_utc` for ordering, `local_hour` and
# `event_date_local` for human hours: 03 C forbids one feature answering a human-hours
# question with a UTC field, so both are available and the registry says which is used.
ROW_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "row_unit",
        "amount_minor",
        "balance_after_minor",
        "balance_before_minor",
        "event_ts_utc",
        "local_hour",
        "event_date_local",
        "txn_type",
        "channel",
        "currency",
        "counterparty",
    }
)
# A *rolling* distinct count is not expressible as a difference of running totals: the test
# "has this key already been seen" depends on the anchor's window start, not the row's own,
# so a per-row first-in-window flag under-counts. Two kinds answer it exactly and they are
# not interchangeable. `cumulative_distinct` counts keys over the account's whole observed
# past, where first-ever-ness needs no knowledge of the anchor; `distinct_in_window` counts
# key *coverages* (see `kinds._coverage_running_total`), which is a sweep rather than a
# running-total difference, so the vocabulary below keeps the windowed kind on the one
# subject whose blocks are shorter than any window that reads them: a local date.
DISTINCT_IN_WINDOW_SUBJECTS: Final[frozenset[str]] = frozenset({"event_date_local"})
LIFETIME_DISTINCT_SUBJECTS: Final[frozenset[str]] = frozenset(
    {
        "counterparty",
        "txn_type",
        "channel",
        "currency",
        "event_date_local",
        "amount_minor",
    }
)
DISTINCT_SUBJECTS: Final[frozenset[str]] = LIFETIME_DISTINCT_SUBJECTS
EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"local_hour", "txn_type", "channel", "currency", "direction"}
)
FLOAT_STAT_FORMULAS: Final[frozenset[str]] = frozenset(
    {"robust_z", "burstiness", "benford_dev", "coefficient_of_variation"}
)
# Row-level predicates. A `where` may also name a declared bool feature, which the
# loader validates separately; these are the primitives that need no dependency.
PREDICATES: Final[frozenset[str]] = frozenset(
    {
        "always",
        "zero_amount",
        "is_nonzero",
        "is_inflow",
        "is_outflow",
        "is_inflow_nonzero",
        "is_outflow_nonzero",
        "is_inflow_not_self",
        "is_outflow_not_self",
        "self_transfer",
        "not_self_transfer",
        "reversal_txn",
        "has_prior_event",
        "has_balance",
        "has_balance_delta",
        "balance_delta_mismatch",
        "zero_balance_after",
        "overnight_local_hour",
        "weekend_local_date",
        "late_arrival",
    }
)
ROW_VALUES: Final[frozenset[str]] = frozenset(
    {
        "gap_since_previous_event_s",
        "gap_since_previous_event_days",
        "balance_delta_abs_minor",
    }
)

DTYPE_TO_POLARS: Final[Mapping[str, pl.DataType]] = {
    "int64": pl.Int64,
    "int32": pl.Int32,
    "float64": pl.Float64,
    "bool": pl.Boolean,
}

NULL_POLICIES: Final[frozenset[str]] = frozenset(
    {
        "never_null",
        "null_when_window_empty",
        "null_without_prior_event",
        "null_when_zero_denominator",
        "null_when_indeterminate",
        "null_when_unobserved",
        "null_when_balance_absent",
        "null_when_no_rule_hit",
    }
)

AS_OF_RULES: Final[frozenset[str]] = frozenset({"row_event_ts", "fold_scoped"})
GROUP_KEYS: Final[frozenset[str]] = frozenset({"entity", "currency", "counterparty", "direction"})
ROLES: Final[frozenset[str]] = frozenset({"feature", "intermediate", "outcome"})

# The shortest published sentence in this registry is 43 characters. The floor is a
# tripwire for "desc: count" style filler, not a prose judgement.
MIN_SENTENCE_CHARS: Final = 40
MIN_LEAKAGE_NOTE_CHARS: Final = 15
MATRIX_FEATURE_FLOOR: Final = 60
MATRIX_FEATURE_CEILING: Final = 75

BANNED_SENTENCE_FRAGMENTS: Final[tuple[str, ...]] = (
    "todo",
    "tbd",
    "fixme",
    "placeholder",
    "lorem ipsum",
    "this feature measures",
    "feature that",
)


class RegistryError(ConfigError):
    """Raised when config/features.yaml is missing, malformed or internally inconsistent.

    Subclasses ConfigError deliberately: a bad registry is the same class of boundary
    failure as a bad seed, and the CLI's existing "config is wrong, stop" handling
    should catch it without knowing the difference.
    """


def parse_window(raw: str) -> timedelta | None:
    """Turn a declared window into a duration, or None for the non-rolling markers.

    Only `Nh` and `Nd` are accepted. A bare number is refused rather than guessed at:
    "7" as days and "7" as hours differ by a factor of 168, and that difference is a
    feature quietly reading far more history than anyone approved.
    """
    if raw in NON_ROLLING_WINDOWS:
        return None
    match = DURATION_PATTERN.match(raw)
    if match is None:
        raise RegistryError(f"window {raw!r} is not a duration like '24h' or '30d'")
    amount, unit = int(match.group(1)), match.group(2)
    if amount <= 0:
        raise RegistryError(f"window {raw!r} must be positive")
    return timedelta(hours=amount) if unit == "h" else timedelta(days=amount)


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """One validated registry entry.

    Every field is a declared fact from config/features.yaml, including the ones only
    some kinds read. One flat shape rather than a subclass per kind is deliberate: the
    hash, the loader's validation and the SHAP dictionary all walk the same ordered
    list, and a subclass tree turns each of those into a dispatch site.
    """

    id: str
    group: str
    sentence: str
    kind: str
    as_of: str
    window: str
    group_by: tuple[str, ...]
    dtype: str
    null_policy: str
    leakage_sensitive: bool
    leakage_note: str
    role: str = "feature"
    winsorise: bool = False
    agg: str | None = None
    source: str | None = None
    where: str | None = None
    quantile: float | None = None
    subject: str | None = None
    value: str | None = None
    formula: str | None = None
    predicate: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    denominator_power: int | None = None
    scale: int | None = None
    a: str | None = None
    b: str | None = None
    graph_field: str | None = None
    rule_id: str | None = None
    categories: tuple[str, ...] | None = None
    null_reason: str | None = None
    declared_score_because: str | None = None

    @property
    def window_duration(self) -> timedelta | None:
        """The rolling lookback as a duration, or None for the non-rolling markers."""
        return parse_window(self.window)

    @property
    def polars_dtype(self) -> pl.DataType:
        """The dtype the published column is asserted to carry."""
        return DTYPE_TO_POLARS[self.dtype]

    @property
    def is_money(self) -> bool:
        """Minor-unit column, recognised by the naming discipline the loader enforces."""
        return self.dtype == "int64" and self.id.endswith("_minor")

    @property
    def is_rolling(self) -> bool:
        """True when the entry reads a bounded trailing window."""
        return self.window_duration is not None

    def hash_payload(self) -> dict[str, object]:
        """The declared facts that go into the feature-spec hash, in field order."""
        payload: dict[str, object] = {
            key: value for key, value in asdict(self).items() if value is not None
        }
        return payload


@dataclass(frozen=True, slots=True)
class RegistryGuards:
    """The guard block: rules that apply across every entry."""

    max_abs_correlation_with_label: float
    banned_sources: tuple[str, ...]
    require_finite: bool
    winsorise_enabled: bool
    winsorise_lower: float
    winsorise_upper: float
    winsorise_fit_scope: str
    reversal_txn_types: tuple[str, ...]
    reversal_excluded_from: tuple[str, ...]
    overnight_local_hours: tuple[int, ...]
    missing_value_policy: str
    null_renders_as: str


@dataclass(frozen=True, slots=True)
class WindowSemantics:
    """How a window is anchored, and the overlap the rule windows inherit."""

    as_of: str
    interval: str
    fold_interval: str
    sort_order: tuple[str, ...]
    sealed_windows: bool
    late_arrival_counter: bool
    overlap_hours: int


@dataclass(frozen=True, slots=True)
class FeatureRegistry:
    """The validated registry, in declared order, with its spec hash."""

    spec_version: int
    code_version: str
    max_lookback_days: int
    groups: tuple[str, ...]
    kinds: Mapping[str, str]
    semantics: WindowSemantics
    guards: RegistryGuards
    entries: tuple[FeatureSpec, ...]
    spec_hash: str
    _by_id: Mapping[str, FeatureSpec] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Indexed once at construction. The dataclass is frozen, so the index is built
        # with object.__setattr__ rather than mutated later: a registry that could be
        # edited after hashing would let the hash and the columns disagree.
        built = {entry.id: entry for entry in self.entries}
        if len(built) != len(self.entries):
            raise RegistryError("registry index does not cover every entry")
        object.__setattr__(self, "_by_id", built)

    def __getitem__(self, feature_id: str) -> FeatureSpec:
        entry = self._by_id.get(feature_id)
        if entry is None:
            raise RegistryError(
                f"feature {feature_id!r} is not declared in {CONFIG_DIRNAME}/{FEATURES_FILENAME}"
            )
        return entry

    @property
    def ids(self) -> tuple[str, ...]:
        """Every declared id, in declared order."""
        return tuple(entry.id for entry in self.entries)

    @property
    def matrix_ids(self) -> tuple[str, ...]:
        """The ids published as model columns — the count plan §8 bounds at 60-75."""
        return tuple(entry.id for entry in self.entries if entry.role == "feature")

    @property
    def outcome_ids(self) -> tuple[str, ...]:
        """Forward-looking columns for the quant layer. Never model inputs."""
        return tuple(entry.id for entry in self.entries if entry.role == "outcome")

    @property
    def computed_ids(self) -> tuple[str, ...]:
        """Everything the builder evaluates, matrix or not: the leakage gate runs on this."""
        return tuple(entry.id for entry in self.entries)

    @property
    def intermediate_ids(self) -> tuple[str, ...]:
        """Declared inputs that are computed and then dropped at the boundary."""
        return tuple(entry.id for entry in self.entries if entry.role == "intermediate")

    @property
    def matrix_entries(self) -> tuple[FeatureSpec, ...]:
        return tuple(entry for entry in self.entries if entry.role == "feature")

    @property
    def winsorised_ids(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self.entries if entry.winsorise)

    @property
    def graph_fields(self) -> tuple[str, ...]:
        """Node attributes the P3a fold-sealed graph table has to carry."""
        return tuple(
            entry.graph_field
            for entry in self.entries
            if entry.kind == "graph_node" and entry.graph_field
        )

    @property
    def rule_ids(self) -> tuple[str, ...]:
        """Rule identifiers the P3b fold-sealed hit table has to supply."""
        return tuple(entry.rule_id for entry in self.entries if entry.rule_id)

    @property
    def fold_scoped_ids(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self.entries if entry.as_of == "fold_scoped")

    @property
    def max_window(self) -> timedelta:
        """The longest declared rolling window, which is what the embargo must cover."""
        durations = [entry.window_duration for entry in self.entries]
        present = [duration for duration in durations if duration is not None]
        if not present:
            raise RegistryError("no feature declares a rolling window")
        return max(present)

    def sentences(self) -> dict[str, str]:
        """id -> sentence, for the SHAP panel and the scorecard's attribute labels."""
        return {entry.id: entry.sentence for entry in self.matrix_entries}

    def matrix_dtypes(self) -> dict[str, pl.DataType]:
        """id -> dtype, for the boundary assertion on the published frame."""
        return {entry.id: entry.polars_dtype for entry in self.matrix_entries}

    def groups_for(self, ids: Sequence[str]) -> dict[str, str]:
        """id -> taxonomy group, for the ablation table's grouping."""
        return {feature_id: self[feature_id].group for feature_id in ids}


def _require_str(mapping: Mapping[str, object], key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{where}: field {key!r} must be a non-empty string")
    return value


def _optional_str(mapping: Mapping[str, object], key: str, where: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{where}: field {key!r} must be a non-empty string when present")
    return value


def _require_bool(mapping: Mapping[str, object], key: str, where: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise RegistryError(f"{where}: field {key!r} must be true or false, got {value!r}")
    return value


def _require_int(mapping: Mapping[str, object], key: str, where: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RegistryError(f"{where}: field {key!r} must be an integer, got {value!r}")
    return value


def _optional_number(mapping: Mapping[str, object], key: str, where: str) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RegistryError(f"{where}: field {key!r} must be a number, got {value!r}")
    return float(value)


def _entry_to_spec(raw: Mapping[str, object], index: int) -> FeatureSpec:
    """Convert one mapping into a FeatureSpec, checking shape but not meaning."""
    blind_where = f"features[{index}]"
    feature_id = _require_str(raw, "id", blind_where)
    where = f"feature {feature_id!r}"
    if ID_PATTERN.match(feature_id) is None:
        raise RegistryError(f"{where}: id must be lower snake_case")

    group_by_raw = raw.get("group_by")
    if not isinstance(group_by_raw, list) or not group_by_raw:
        raise RegistryError(f"{where}: group_by must be a non-empty list of partition keys")

    categories_raw = raw.get("categories")
    categories: tuple[str, ...] | None = None
    if categories_raw is not None:
        if not isinstance(categories_raw, list) or not categories_raw:
            raise RegistryError(f"{where}: categories must be a non-empty list")
        categories = tuple(str(item) for item in categories_raw)

    quantile = _optional_number(raw, "quantile", where)
    return FeatureSpec(
        id=feature_id,
        group=_require_str(raw, "group", where),
        sentence=_require_str(raw, "sentence", where),
        kind=_require_str(raw, "kind", where),
        as_of=_require_str(raw, "as_of", where),
        window=_require_str(raw, "window", where),
        group_by=tuple(str(key) for key in group_by_raw),
        dtype=_require_str(raw, "dtype", where),
        null_policy=_require_str(raw, "null_policy", where),
        leakage_sensitive=_require_bool(raw, "leakage_sensitive", where),
        leakage_note=_require_str(raw, "leakage_note", where),
        role=_optional_str(raw, "role", where) or "feature",
        winsorise=raw.get("winsorise") is True,
        agg=_optional_str(raw, "agg", where),
        source=_optional_str(raw, "source", where),
        where=_optional_str(raw, "where", where),
        quantile=quantile,
        subject=_optional_str(raw, "subject", where),
        value=_optional_str(raw, "value", where),
        formula=_optional_str(raw, "formula", where),
        predicate=_optional_str(raw, "predicate", where),
        numerator=_optional_str(raw, "numerator", where),
        denominator=_optional_str(raw, "denominator", where),
        denominator_power=(
            _require_int(raw, "denominator_power", where) if "denominator_power" in raw else None
        ),
        scale=_require_int(raw, "scale", where) if "scale" in raw else None,
        a=_optional_str(raw, "a", where),
        b=_optional_str(raw, "b", where),
        graph_field=_optional_str(raw, "field", where),
        rule_id=_optional_str(raw, "rule_id", where),
        categories=categories,
        null_reason=_optional_str(raw, "null_reason", where),
        declared_score_because=_optional_str(raw, "declared_score_because", where),
    )


def sorted_categories(categories: Sequence[str]) -> list[str]:
    """Canonical order for a declared category list: numeric when all numeric.

    ``local_hour`` is declared 0..23 and must code in hour order, not in the string
    order that puts 10 after 1. Both the loader's sortedness check and the kernel's code
    map go through here, so there is one answer to "what index does this category get".
    """
    if all(category.lstrip("-").isdigit() for category in categories):
        return sorted(categories, key=lambda text: int(text))
    return sorted(categories)


def money_like(reference: str) -> bool:
    """Whether a declared column or source name carries money or elapsed time."""
    return reference in {
        "amount_minor",
        "balance_after_minor",
        "balance_before_minor",
        "event_ts_utc",
    } or reference.endswith(("_minor", "_s", "_days", "_hours"))


def _validate_entry(
    entry: FeatureSpec,
    registry_groups: Sequence[str],
    declared: Mapping[str, FeatureSpec],
) -> list[str]:
    """Semantic validation for one entry: one message per problem, all naming the feature."""
    problems: list[str] = []
    where = f"feature {entry.id!r}"

    if entry.kind not in KIND_NAMES:
        problems.append(
            f"{where}: kind {entry.kind!r} is not a declared feature kind "
            f"(known: {', '.join(sorted(KIND_NAMES))})"
        )
    if entry.group not in registry_groups:
        problems.append(
            f"{where}: group {entry.group!r} is not in the taxonomy ({', '.join(registry_groups)})"
        )
    if entry.dtype not in DTYPE_TO_POLARS:
        problems.append(f"{where}: dtype {entry.dtype!r} is not one of {sorted(DTYPE_TO_POLARS)}")
    if entry.null_policy not in NULL_POLICIES:
        problems.append(f"{where}: null_policy {entry.null_policy!r} is not a declared policy")
    if entry.as_of not in AS_OF_RULES:
        problems.append(f"{where}: as_of {entry.as_of!r} is not a declared cutoff rule")
    if entry.role not in ROLES:
        problems.append(f"{where}: role {entry.role!r} must be one of {sorted(ROLES)}")
    for key in entry.group_by:
        if key not in GROUP_KEYS:
            problems.append(f"{where}: group_by key {key!r} is not a declared partition key")
    if entry.as_of == "fold_scoped" and entry.kind not in {"graph_node", "rule_field"}:
        problems.append(
            f"{where}: only graph and rule columns are fold-scoped; a rolling window "
            "anchors on the scored row"
        )
    # The forward-looking vocabulary is quarantined by role. Exposure E_i is a real
    # quantity the quant layer must compute (plan §11) and a real leak if it reaches the
    # matrix, so the registry allows the kind and refuses the role.
    if entry.kind in FORWARD_KINDS and entry.role != "outcome":
        problems.append(
            f"{where}: a forward-looking window reads rows after the cutoff, so it may only be "
            f"declared with role 'outcome'; {entry.role!r} would put it in the model matrix "
            "(spec §7.2)"
        )
    if entry.role == "outcome" and entry.kind not in FORWARD_KINDS:
        problems.append(
            f"{where}: role 'outcome' is reserved for forward-looking quantities; a backward "
            "column is a feature or an intermediate"
        )
    if entry.kind in FORWARD_KINDS and not entry.leakage_sensitive:
        problems.append(
            f"{where}: a forward-looking column cannot declare itself leakage-insensitive"
        )

    if len(entry.sentence) < MIN_SENTENCE_CHARS:
        problems.append(
            f"{where}: sentence is {len(entry.sentence)} characters. The SHAP panel and the "
            f"scorecard label need a sentence a human can act on (minimum "
            f"{MIN_SENTENCE_CHARS})"
        )
    lowered = entry.sentence.lower()
    for fragment in BANNED_SENTENCE_FRAGMENTS:
        if fragment in lowered:
            problems.append(f"{where}: sentence contains filler {fragment!r}")
    if entry.sentence.strip().rstrip(".").replace("_", " ") == entry.id.replace("_", " "):
        problems.append(f"{where}: sentence repeats the id instead of describing the quantity")
    if len(entry.leakage_note) < MIN_LEAKAGE_NOTE_CHARS:
        problems.append(f"{where}: leakage_note must justify leakage_sensitive in a real sentence")

    duration: timedelta | None = None
    try:
        duration = parse_window(entry.window)
    except RegistryError as exc:
        problems.append(f"{where}: {exc}")
        duration = None

    # A float column may never be money: DEV-005 fixes minor units at int64, and the
    # naming discipline is what makes that checkable from the schema alone.
    if entry.dtype == "float64" and entry.id.endswith("_minor"):
        problems.append(f"{where}: float64 money is a defect (DEV-005); use int64 minor units")
    if entry.dtype == "float64" and not entry.declared_score_because:
        problems.append(
            f"{where}: a float64 column must carry declared_score_because, so the registry "
            "states why this value is a score and not money"
        )
    if entry.dtype == "int64" and entry.id.endswith("_bps") and entry.scale not in (None, 10_000):
        problems.append(f"{where}: a *_bps column is scaled by 10_000, got {entry.scale}")
    if entry.winsorise and entry.dtype != "int64":
        problems.append(
            f"{where}: winsorise is declared only for int64 money or count columns; a score is "
            "already bounded"
        )
    if entry.winsorise and entry.kind in {"row_flag", "event_field"}:
        # The outer kind set is a superset of these two, so one condition says what the
        # nested pair said: a flag or a rule code is a category, and a category has no
        # tail to clip.
        problems.append(f"{where}: a flag or a code is not winsorised")

    def check_reference(label: str, ref: str | None, required: bool) -> bool:
        if ref is None:
            if required:
                problems.append(f"{where}: {label} is required for kind {entry.kind}")
            return True
        if ref in declared or ref in ROW_SOURCES or ref in PREDICATES:
            return True
        problems.append(f"{where}: {label} {ref!r} is neither a row field nor a declared feature")
        return False

    if entry.kind == "window_agg":
        if entry.agg is None:
            problems.append(f"{where}: window_agg needs agg")
        elif entry.agg not in WINDOW_AGGREGATIONS:
            problems.append(
                f"{where}: agg {entry.agg!r} is not implemented "
                f"(known: {', '.join(sorted(WINDOW_AGGREGATIONS))})"
            )
        check_reference("source", entry.source, required=True)
        if entry.where is None:
            problems.append(f"{where}: window_agg needs where, even when it is 'always'")
        check_reference("where", entry.where, required=False)
        if duration is None:
            problems.append(f"{where}: window_agg must declare a rolling window")
        if entry.agg in ORDER_STAT_AGGREGATIONS and entry.where not in (None, "always"):
            problems.append(
                f"{where}: a filtered {entry.agg} cannot be answered exactly at a row the "
                "filter excluded — a grouped rolling window would silently widen to the last "
                "included row's window. Use a cumulative aggregation (count, sum, mean_int, "
                "std) or partition by the direction instead of filtering on it."
            )
        if (
            entry.agg == "std"
            and entry.where in (None, "always")
            and entry.source
            in {
                "balance_after_minor",
                "amount_minor",
            }
        ):
            problems.append(
                f"{where}: an unfiltered {entry.agg} over {entry.source} includes the rows a "
                "value feature should ignore; declare the predicate the value stands for"
            )
        if entry.agg in COUNT_LIKE_AGGREGATIONS and entry.null_policy != "never_null":
            problems.append(
                f"{where}: {entry.agg} over an empty window is a real zero; null_policy should "
                "be never_null"
            )
        if entry.agg == "quantile" and entry.quantile is None:
            problems.append(f"{where}: agg quantile needs quantile in (0, 1)")
        if entry.quantile is not None and not 0.0 < entry.quantile < 1.0:
            problems.append(f"{where}: quantile must be inside (0, 1)")
        if (
            entry.agg in {"sum", "median", "quantile", "max", "min", "mean_int"}
            and entry.dtype == "int64"
            and duration is not None
            and money_like(entry.source or "")
            and not money_like(entry.id)
        ):
            problems.append(
                f"{where}: an aggregate of money or elapsed time must name its unit "
                "(_minor, _s, _days, _hours) so no reader has to guess the scale"
            )
    elif entry.kind == "cumulative_distinct":
        if entry.subject not in LIFETIME_DISTINCT_SUBJECTS:
            problems.append(
                f"{where}: subject {entry.subject!r} cannot be counted over a lifetime "
                f"(known: {', '.join(sorted(LIFETIME_DISTINCT_SUBJECTS))})"
            )
        check_reference("where", entry.where, required=False)
        if entry.window != "lifetime":
            problems.append(
                f"{where}: a distinct count over a bounded window is not expressible as a "
                "difference of running totals; declare window 'lifetime' or use "
                "distinct_in_window on a timestamp-ordered subject"
            )
        if entry.null_policy != "never_null":
            problems.append(f"{where}: a distinct count of nothing is zero, not null")
    elif entry.kind == "distinct_in_window":
        if entry.subject not in DISTINCT_IN_WINDOW_SUBJECTS:
            problems.append(
                f"{where}: subject {entry.subject!r} is not distinct-countable "
                f"(known: {', '.join(sorted(DISTINCT_SUBJECTS))})"
            )
        check_reference("where", entry.where, required=False)
        if duration is None:
            problems.append(f"{where}: distinct_in_window must declare a rolling window")
        if entry.null_policy != "never_null":
            problems.append(f"{where}: a distinct count of nothing is zero, not null")
    elif entry.kind == "first_seen_flag":
        if entry.subject not in DISTINCT_SUBJECTS:
            problems.append(f"{where}: first_seen_flag needs a declared subject")
        if entry.window != "lifetime":
            problems.append(
                f"{where}: first_seen_flag is a lifetime quantity; window must be 'lifetime'"
            )
        check_reference("where", entry.where, required=False)
    elif entry.kind in {"quotient_int", "diff_int"}:
        numerator = entry.numerator if entry.kind == "quotient_int" else entry.a
        denominator = entry.denominator if entry.kind == "quotient_int" else entry.b
        for label, ref in (("numerator", numerator), ("denominator", denominator)):
            if ref is None:
                problems.append(f"{where}: {entry.kind} needs {label}")
                continue
            if ref not in declared:
                problems.append(f"{where}: {label} {ref!r} is not a declared feature")
                continue
            referenced = declared[ref]
            if referenced.dtype != "int64":
                problems.append(
                    f"{where}: {label} {ref!r} is {referenced.dtype}; an integer quotient needs "
                    "int64 operands so no money becomes a float"
                )
        if entry.kind == "quotient_int":
            if entry.scale is None or entry.scale <= 0:
                problems.append(f"{where}: quotient_int needs a positive integer scale")
            if entry.denominator_power not in (None, 1, 2):
                problems.append(f"{where}: denominator_power is only declared as 1 or 2")
            if entry.null_policy != "null_when_zero_denominator":
                problems.append(
                    f"{where}: a quotient over a zero denominator is null; a fabricated zero "
                    "would read as evidence (03 A rule 2)"
                )
    elif entry.kind == "row_flag":
        if entry.predicate not in PREDICATES:
            problems.append(
                f"{where}: predicate {entry.predicate!r} is not implemented "
                f"(known: {', '.join(sorted(PREDICATES))})"
            )
        if entry.dtype != "bool":
            problems.append(f"{where}: row_flag publishes a bool column")
        if entry.window != "point_in_time":
            problems.append(
                f"{where}: a row predicate reads the scored row; window must be point_in_time"
            )
    elif entry.kind == "row_value":
        if entry.value not in ROW_VALUES:
            problems.append(
                f"{where}: value {entry.value!r} is not implemented "
                f"(known: {', '.join(sorted(ROW_VALUES))})"
            )
        if entry.window != "point_in_time":
            problems.append(f"{where}: a row-derived value must declare window point_in_time")
        if entry.dtype != "int64":
            problems.append(f"{where}: row_value publishes an int64 quantity")
    elif entry.kind == "event_field":
        if entry.source not in EVENT_FIELDS:
            problems.append(
                f"{where}: source {entry.source!r} is not a carried event field "
                f"(known: {', '.join(sorted(EVENT_FIELDS))})"
            )
        if not entry.categories:
            problems.append(f"{where}: event_field needs an explicit category list")
        elif list(entry.categories) != sorted_categories(entry.categories):
            problems.append(
                f"{where}: categories must be declared in canonical order (numeric when all "
                "numeric), or the emitted code depends on declaration order and stops being "
                "reproducible"
            )
        if entry.window != "point_in_time":
            problems.append(f"{where}: an event field is a fact about the scored row")
        if entry.dtype != "int32":
            problems.append(f"{where}: event_field publishes an int32 code")
    elif entry.kind == "float_stat":
        if entry.formula not in FLOAT_STAT_FORMULAS:
            problems.append(
                f"{where}: formula {entry.formula!r} is not implemented "
                f"(known: {', '.join(sorted(FLOAT_STAT_FORMULAS))})"
            )
        if entry.dtype != "float64":
            problems.append(f"{where}: float_stat publishes a float64 score")
        check_reference("source", entry.source, required=True)
        check_reference("where", entry.where, required=False)
        if duration is None:
            problems.append(f"{where}: float_stat needs a trailing reference window")
        if entry.null_policy != "null_when_indeterminate":
            problems.append(
                f"{where}: a score with too few samples or zero spread is null, not 0.0"
            )
    elif entry.kind == "recency":
        check_reference("where", entry.where, required=True)
        if entry.source != "event_ts_utc":
            problems.append(
                f"{where}: recency is measured on event_ts_utc; a human-hours question belongs "
                "in a local_hour predicate"
            )
        if entry.dtype != "int64" or not money_like(entry.id):
            problems.append(f"{where}: recency must be an int64 quantity with a named unit")
        if entry.window != "lifetime":
            problems.append(
                f"{where}: recency reaches back over the account's own past, so window must be "
                "lifetime; a capped recency is a window count, not an age"
            )
    elif entry.kind == "cumulative":
        if entry.agg not in CUMULATIVE_AGGREGATIONS:
            problems.append(
                f"{where}: agg {entry.agg!r} is not a cumulative aggregation "
                f"(known: {', '.join(sorted(CUMULATIVE_AGGREGATIONS))})"
            )
        if entry.window != "lifetime":
            problems.append(f"{where}: cumulative needs window 'lifetime'")
        check_reference("source", entry.source, required=True)
        check_reference("where", entry.where, required=False)
    elif entry.kind == "graph_node":
        if entry.graph_field is None:
            problems.append(f"{where}: graph_node needs the fold-graph field name it reads")
        if entry.as_of != "fold_scoped":
            problems.append(
                f"{where}: a graph feature is fold-scoped by rule; anything else reads the "
                "whole corpus's structure"
            )
        if entry.null_policy != "null_when_unobserved":
            problems.append(
                f"{where}: an absent node is null with a stated reason, never an imputed zero"
            )
        if not entry.null_reason:
            problems.append(f"{where}: null_when_unobserved requires null_reason (DEV-011)")
    elif entry.kind == "rule_field":
        if entry.rule_id is None:
            problems.append(f"{where}: rule_field needs the rule id it reads")
        if entry.as_of != "fold_scoped":
            problems.append(f"{where}: rule severities arrive fold-sealed, or not at all")
        if entry.null_policy != "null_when_no_rule_hit":
            problems.append(
                f"{where}: no hit inside the fold is null with a reason, not zero severity"
            )
        if not entry.null_reason:
            problems.append(f"{where}: null_when_no_rule_hit requires null_reason")

    return problems


def _spec_hash(
    spec_version: int,
    code_version: str,
    entries: Sequence[FeatureSpec],
    semantics: WindowSemantics,
) -> str:
    """sha256 over the ordered declared parameters, the windows and the code version."""
    payload = {
        "spec_version": spec_version,
        "code_version": code_version,
        "semantics": asdict(semantics),
        "features": [entry.hash_payload() for entry in entries],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_guards(raw: Mapping[str, object]) -> RegistryGuards:
    guards = raw.get("guards")
    if not isinstance(guards, dict):
        raise RegistryError("guards: the guard block is missing")
    winsorise = guards.get("winsorise")
    if not isinstance(winsorise, dict):
        raise RegistryError("guards.winsorise: the winsorisation block is missing")
    lower = _optional_number(winsorise, "lower_percentile", "guards.winsorise")
    upper = _optional_number(winsorise, "upper_percentile", "guards.winsorise")
    if lower is None or upper is None or not 0.0 <= lower < upper <= 1.0:
        raise RegistryError(
            f"guards.winsorise: percentiles must satisfy 0 <= lower < upper <= 1, got {lower}, {upper}"
        )
    correlation = _optional_number(guards, "max_abs_correlation_with_label", "guards")
    if correlation is None or not 0.0 < correlation < 1.0:
        raise RegistryError("guards.max_abs_correlation_with_label must be a number in (0, 1)")
    banned = guards.get("banned_sources")
    if not isinstance(banned, list) or not banned:
        raise RegistryError("guards.banned_sources must name the label columns")
    overnight = guards.get("overnight_local_hours")
    if (
        not isinstance(overnight, list)
        or not overnight
        or not all(
            isinstance(hour, int) and not isinstance(hour, bool) and 0 <= hour <= 23
            for hour in overnight
        )
    ):
        raise RegistryError("guards.overnight_local_hours must be hours in 0-23")
    reversals = guards.get("reversal_txn_types")
    if not isinstance(reversals, list) or not reversals:
        raise RegistryError("guards.reversal_txn_types must name the reversal types (03 D)")
    excluded = guards.get("reversal_excluded_from")
    if not isinstance(excluded, list) or not excluded:
        raise RegistryError("guards.reversal_excluded_from must list the cycle features")
    return RegistryGuards(
        max_abs_correlation_with_label=correlation,
        banned_sources=tuple(str(item) for item in banned),
        require_finite=guards.get("require_finite") is True,
        winsorise_enabled=winsorise.get("enabled") is True,
        winsorise_lower=lower,
        winsorise_upper=upper,
        winsorise_fit_scope=_require_str(winsorise, "fit_scope", "guards.winsorise"),
        reversal_txn_types=tuple(str(item) for item in reversals),
        reversal_excluded_from=tuple(str(item) for item in excluded),
        overnight_local_hours=tuple(int(hour) for hour in overnight),
        missing_value_policy=_require_str(guards, "missing_value_policy", "guards"),
        null_renders_as=_require_str(guards, "null_renders_as", "guards"),
    )


def _parse_semantics(raw: Mapping[str, object]) -> WindowSemantics:
    semantics = raw.get("window_semantics")
    if not isinstance(semantics, dict):
        raise RegistryError(
            "window_semantics: the block describing how windows are anchored is missing"
        )
    sort_order = semantics.get("sort_order")
    if not isinstance(sort_order, list) or not sort_order:
        raise RegistryError("window_semantics.sort_order must name the total order")
    overlap = semantics.get("overlap_hours")
    if not isinstance(overlap, int) or isinstance(overlap, bool) or overlap <= 0:
        raise RegistryError("window_semantics.overlap_hours must be a positive number of hours")
    return WindowSemantics(
        as_of=_require_str(semantics, "as_of", "window_semantics"),
        interval=_require_str(semantics, "interval", "window_semantics"),
        fold_interval=_require_str(semantics, "fold_interval", "window_semantics"),
        sort_order=tuple(str(item) for item in sort_order),
        sealed_windows=semantics.get("sealed_windows") is True,
        late_arrival_counter=semantics.get("late_arrival_counter") is True,
        overlap_hours=overlap,
    )


def parse_registry(raw: Mapping[str, object]) -> FeatureRegistry:
    """Validate a parsed features.yaml mapping into a FeatureRegistry.

    Raises RegistryError naming every offending feature in one pass. Collecting rather
    than failing on the first is deliberate: someone fixing the registry should see all
    twenty problems at once instead of discovering them one load at a time.
    """
    spec_version = _require_int(raw, "spec_version", "features.yaml")
    code_version = _require_str(raw, "code_version", "features.yaml")
    max_lookback_days = _require_int(raw, "max_lookback_days", "features.yaml")
    if max_lookback_days <= 0:
        raise RegistryError(f"max_lookback_days must be positive, got {max_lookback_days}")

    registry_groups = raw.get("groups")
    if not isinstance(registry_groups, list) or not registry_groups:
        raise RegistryError("groups: the taxonomy list is missing")
    group_names = tuple(str(name) for name in registry_groups)

    declared_kinds = raw.get("kinds")
    if not isinstance(declared_kinds, dict):
        raise RegistryError("kinds: the dispatch table declaration is missing")
    unknown = sorted(set(declared_kinds) - set(KIND_NAMES))
    undocumented = sorted(set(KIND_NAMES) - set(declared_kinds))
    if unknown or undocumented:
        raise RegistryError(
            "kinds: config/features.yaml and oxbow.features.registry disagree — declared "
            f"but not accepted: {unknown}; accepted but undeclared: {undocumented}"
        )

    raw_features = raw.get("features")
    if not isinstance(raw_features, list) or not raw_features:
        raise RegistryError("features: the registry declares no features")

    guards_raw = raw.get("guards")
    banned_raw = guards_raw.get("banned_sources") if isinstance(guards_raw, dict) else None
    banned_sources = {str(item) for item in banned_raw} if isinstance(banned_raw, list) else set()

    entries: list[FeatureSpec] = []
    declared: dict[str, FeatureSpec] = {}
    problems: list[str] = []
    for index, item in enumerate(raw_features):
        if not isinstance(item, dict):
            problems.append(f"features[{index}]: an entry must be a mapping")
            continue
        entry = _entry_to_spec(item, index)
        if entry.id in declared:
            problems.append(f"feature {entry.id!r}: id is declared twice")
            continue
        if entry.id in banned_sources:
            problems.append(
                f"feature {entry.id!r}: a label column may never be a feature (spec §7.2)"
            )
        declared[entry.id] = entry
        entries.append(entry)

    # A referenced id must be declared *before* the referrer: the builder computes in
    # declared order, so a forward reference is not a hint to reorder but a missing column.
    computed: set[str] = set()
    for entry in entries:
        for ref in (
            entry.source,
            entry.where,
            entry.numerator,
            entry.denominator,
            entry.a,
            entry.b,
        ):
            if ref is None or ref in computed:
                continue
            if ref in declared:
                problems.append(
                    f"feature {entry.id!r}: references {ref!r}, which is declared later. The "
                    "builder computes in declared order."
                )
        if ref_in(entry, banned_sources):
            problems.append(
                f"feature {entry.id!r}: reads a banned label column "
                f"({', '.join(sorted(banned_sources))})"
            )
        computed.add(entry.id)
        problems.extend(_validate_entry(entry, group_names, declared))

    for entry in entries:
        duration = parse_window(entry.window)
        if duration is not None and duration.days > max_lookback_days:
            problems.append(
                f"feature {entry.id!r}: window {entry.window} exceeds max_lookback_days="
                f"{max_lookback_days}, which is the length the split module's embargo must equal"
            )

    matrix_count = sum(1 for entry in entries if entry.role == "feature")
    if not MATRIX_FEATURE_FLOOR <= matrix_count <= MATRIX_FEATURE_CEILING:
        problems.append(
            f"features: {matrix_count} published features, but plan §8 bounds the registry at "
            f"{MATRIX_FEATURE_FLOOR}-{MATRIX_FEATURE_CEILING}"
        )

    graph_fields = [entry.graph_field for entry in entries if entry.kind == "graph_node"]
    for field_name in graph_fields:
        if graph_fields.count(field_name) > 1:
            problems.append(f"graph_node field {field_name!r} is read by two features")
    rule_list = [entry.rule_id for entry in entries if entry.kind == "rule_field"]
    for rule in rule_list:
        if rule_list.count(rule) > 1:
            problems.append(f"rule_field rule_id {rule!r} is read by two features")

    if problems:
        raise RegistryError(
            f"{CONFIG_DIRNAME}/{FEATURES_FILENAME} is not a valid registry:\n  "
            + "\n  ".join(sorted(set(problems)))
        )

    semantics = _parse_semantics(raw)
    guards = _parse_guards(raw)
    entry_tuple = tuple(entries)
    return FeatureRegistry(
        spec_version=spec_version,
        code_version=code_version,
        max_lookback_days=max_lookback_days,
        groups=group_names,
        kinds=dict(declared_kinds),
        semantics=semantics,
        guards=guards,
        entries=entry_tuple,
        spec_hash=_spec_hash(spec_version, code_version, entry_tuple, semantics),
    )


def ref_in(entry: FeatureSpec, banned: set[str]) -> bool:
    """True when the entry reads a banned label column through any reference field."""
    references = {
        entry.source,
        entry.where,
        entry.subject,
        entry.numerator,
        entry.denominator,
        entry.a,
        entry.b,
    }
    return bool(references & banned)


def hash_registry(
    entries: Sequence[FeatureSpec],
    *,
    spec_version: int,
    code_version: str,
    semantics: WindowSemantics,
) -> str:
    """The spec hash for a declared entry list, computed by the loader's own function.

    Public because ``compute.py`` has to produce a *correct* hash for a variant registry —
    a shortened window, a bumped code version — and the only way to do that honestly is to
    run the same function the loader ran. A caller that hand-wrote a digest, or reused the
    parent's after changing an entry, would defeat the whole seam: the mismatch check would
    be comparing a number nobody derived from the columns it claims to describe.
    """
    return _spec_hash(spec_version, code_version, entries, semantics)


def load_registry(path: Path) -> FeatureRegistry:
    """Load and validate config/features.yaml from an explicit path."""
    try:
        raw = load_yaml(path)
    except ConfigError as exc:
        raise RegistryError(f"{path}: {exc}") from exc
    return parse_registry(raw)


def registry_from_config_dir(config_dir: Path) -> FeatureRegistry:
    """Load the registry from a config directory."""
    return load_registry(config_dir / FEATURES_FILENAME)


__all__ = [
    "AS_OF_RULES",
    "BANNED_SENTENCE_FRAGMENTS",
    "CONFIG_DIRNAME",
    "COUNT_LIKE_AGGREGATIONS",
    "CUMULATIVE_AGGREGATIONS",
    "DISTINCT_IN_WINDOW_SUBJECTS",
    "DISTINCT_SUBJECTS",
    "DTYPE_TO_POLARS",
    "EVENT_FIELDS",
    "FEATURES_FILENAME",
    "FLOAT_STAT_FORMULAS",
    "GROUP_KEYS",
    "ID_PATTERN",
    "KIND_NAMES",
    "LIFETIME_DISTINCT_SUBJECTS",
    "MATRIX_FEATURE_CEILING",
    "MATRIX_FEATURE_FLOOR",
    "NON_ROLLING_WINDOWS",
    "NULL_POLICIES",
    "ORDER_STAT_AGGREGATIONS",
    "PREDICATES",
    "ROLES",
    "ROW_SOURCES",
    "ROW_VALUES",
    "FeatureRegistry",
    "FeatureSpec",
    "RegistryError",
    "RegistryGuards",
    "WindowSemantics",
    "hash_registry",
    "load_registry",
    "parse_registry",
    "parse_window",
    "registry_from_config_dir",
]
