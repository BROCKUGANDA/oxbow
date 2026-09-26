"""Strict readers for ``config/scorecard.yaml`` and ``config/features.yaml``.

00 G: every tunable lives in config, not in code. ``oxbow.config`` owns the file
discovery and the fail-loud contract, and this module extends exactly that pattern
to the scorecard's own keys: a missing or mistyped key raises ``ConfigError`` at
load time naming the path, because a silently defaulted PDO is a silently wrong
score and the score is what a human signs off.

Read-only on ``features.yaml``: the feature registry belongs to P2. P4 consumes
the declared names and the declared guards; it never redefines them. That includes
the feature-spec digest, which is computed by :func:`oxbow.features.registry.parse_registry`
and carried here as ``FeatureRegistry.spec_hash`` rather than recomputed from P4's
narrower view of the file -- see :func:`oxbow.scoring.frame.canonical_spec_hash`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from oxbow.config import ConfigError, find_repo_root, load_yaml
from oxbow.features.registry import parse_registry as parse_declared_registry

SCORECARD_FILENAME: Final = "scorecard.yaml"
FEATURES_FILENAME: Final = "features.yaml"
CONFIG_DIRNAME: Final = "config"

# The scorecard emits a risk score whose points go DOWN with risk: the plan's own
# adverse-action example is "Pass-through ratio in top decile: minus 48 points",
# so a risky bin must contribute a negative number. That fixes the direction of
# every derived quantity, band ordering included (DEV-001 pair: A is the safest
# band, E the riskiest).
DIRECTION_HIGHER_POINTS_ARE_SAFER: Final = "higher_points_are_safer"


def _node(mapping: Mapping[str, object], path: str) -> object:
    """Walk a dotted key path, failing loud with the path in the message."""
    node: object = mapping
    walked: list[str] = []
    for part in path.split("."):
        walked.append(part)
        if not isinstance(node, Mapping) or part not in node:
            raise ConfigError(f"config key '{'.'.join(walked)}' is missing")
        node = node[part]
    return node


def require_int(mapping: Mapping[str, object], path: str) -> int:
    """Read an int key, rejecting bool because bool is an int in Python."""
    value = _node(mapping, path)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"config key '{path}' must be an int, got {type(value).__name__}")
    return value


def require_float(mapping: Mapping[str, object], path: str) -> float:
    """Read a float key.

    Probabilities, WOE, IV, PSI and score-scaling constants are legitimately
    real-valued; ``scripts/no_float_money.py`` bans only float *money*, and none
    of the names bound here denote an amount.
    """
    value = _node(mapping, path)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"config key '{path}' must be a number, got {type(value).__name__}")
    return float(value)


def require_optional_float(mapping: Mapping[str, object], path: str) -> float | None:
    """Read a float key that may be explicitly null (declared, not absent)."""
    value = _node(mapping, path)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"config key '{path}' must be a number or null, got {type(value).__name__}")
    return float(value)


def require_str(mapping: Mapping[str, object], path: str) -> str:
    """Read a non-empty string key."""
    value = _node(mapping, path)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"config key '{path}' must be a non-empty string")
    return value


def require_bool(mapping: Mapping[str, object], path: str) -> bool:
    """Read a bool key."""
    value = _node(mapping, path)
    if not isinstance(value, bool):
        raise ConfigError(f"config key '{path}' must be a bool, got {type(value).__name__}")
    return value


def require_str_list(mapping: Mapping[str, object], path: str) -> tuple[str, ...]:
    """Read a list of non-empty strings, failing on a null or a scalar."""
    value = _node(mapping, path)
    if not isinstance(value, list):
        raise ConfigError(f"config key '{path}' must be a list, got {type(value).__name__}")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ConfigError(f"config key '{path}[{index}]' must be a non-empty string")
        out.append(item)
    return tuple(out)


def require_optional_str_list(mapping: Mapping[str, object], path: str) -> tuple[str, ...]:
    """Read a list of strings that is allowed to be explicitly empty or null."""
    value = _node(mapping, path)
    if value is None:
        return ()
    if isinstance(value, list) and not value:
        return ()
    return require_str_list(mapping, path)


@dataclass(frozen=True, slots=True)
class ScalingConfig:
    """PDO scaling: ``score = offset + factor * ln(odds)``, odds = good:bad."""

    pdo: float
    base_score: float
    base_odds: float
    show_formula_in_ui: bool

    @property
    def factor(self) -> float:
        """``PDO / ln(2)``: points that double the odds."""
        import math

        return self.pdo / math.log(2.0)

    @property
    def offset(self) -> float:
        """``base - factor * ln(base_odds)``, so the anchor lands on the base."""
        import math

        return self.base_score - self.factor * math.log(self.base_odds)

    def score_at_odds(self, odds: float) -> float:
        """The score for a given good:bad odds.

        Exposed as a method because the named gate clause is a point on this
        curve: 600 at 50:1, asserted in ``tests/unit/test_p4_scorecard.py``.
        """
        import math

        if odds <= 0.0:
            raise ConfigError(f"odds must be positive to scale a score, got {odds}")
        return self.offset + self.factor * math.log(odds)


@dataclass(frozen=True, slots=True)
class BinningConfig:
    """optbinning settings plus the floors, smoothers and special bins."""

    algorithm: str
    min_bin_pct: float
    max_bins: int
    enforce_monotonic_trend: bool
    missing_bin: str
    structural_zero_bin: str
    min_bin_count: int
    smoothing: str
    laplace_alpha: float
    unseen_bin: str
    solver: str
    prebinning_method: str
    max_n_prebins: int
    min_prebin_size: float
    split_digits: int
    time_limit_seconds: float
    categorical_features: tuple[str, ...]
    unseen_tail_population_share: float
    floor_merge_preference: str

    @property
    def special_bin_labels(self) -> tuple[str, ...]:
        """The bin labels that are not value ranges."""
        return (self.missing_bin, self.structural_zero_bin, self.unseen_bin)


@dataclass(frozen=True, slots=True)
class AdmissionConfig:
    """IV admission, and what happens at each boundary."""

    iv_min: float
    iv_max: float
    above_max_action: str
    below_min_action: str
    show_rule_in_ui: bool
    record_refusals_in_artifact: bool


@dataclass(frozen=True, slots=True)
class FitConfig:
    """The WOE logistic fit and its separation guard."""

    regularisation: str
    regularisation_strength: float
    detect_separation: bool
    max_iter: int
    round_points_to_integer: bool
    solver: str
    tolerance: float
    max_abs_coefficient: float
    separation_auc_threshold: float
    separation_action: str
    max_abs_correlation_with_label: float


@dataclass(frozen=True, slots=True)
class BandEntry:
    """One band: identity, the action it implies, and the rate target it is cut to."""

    band_id: str
    label: str
    action: str
    glyph: str
    bad_rate_multiple_of_base: float | None
    min_points: float | None
    max_points: float | None


@dataclass(frozen=True, slots=True)
class BandsConfig:
    """A-E bands, cut by observed bad rate on validation."""

    direction: str
    fit_on: str
    isotonic_smooth: bool
    entries: tuple[BandEntry, ...]
    band_cut_points: tuple[float, ...] | None

    @property
    def band_ids(self) -> tuple[str, ...]:
        return tuple(entry.band_id for entry in self.entries)


@dataclass(frozen=True, slots=True)
class ReasonConfig:
    """Adverse-action reason codes: deterministic, no model needed to write them."""

    top_n: int
    template: str
    points_signed_format: str
    negative_word_form: bool
    sign_convention: str
    bin_label_source: str


@dataclass(frozen=True, slots=True)
class DriftConfig:
    """PSI / CSI thresholds, the degradation action, and rating migration."""

    psi_watch: float
    psi_action: float
    csi_watch: float
    csi_action: float
    on_action: str
    migration_matrix: bool
    downgrade_rate_alert: float
    score_psi_bins: int
    expected_reference: str
    period_column: str


@dataclass(frozen=True, slots=True)
class DeterminismConfig:
    """Tie-break order and the artifact hash algorithm."""

    tie_break: tuple[str, ...]
    hash_algorithm: str


@dataclass(frozen=True, slots=True)
class ScorecardConfig:
    """Everything the scorecard reads, validated at load."""

    root: Path
    scaling: ScalingConfig
    binning: BinningConfig
    admission: AdmissionConfig
    fit: FitConfig
    bands: BandsConfig
    reasons: ReasonConfig
    drift: DriftConfig
    determinism: DeterminismConfig
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class FeatureGuard:
    """The guards P2 declares for the feature table, read as constraints here too."""

    max_abs_correlation_with_label: float
    require_finite: bool
    missing_value_policy: str


@dataclass(frozen=True, slots=True)
class FeatureDeclaration:
    """One published entry of the P2 registry, as P4 needs to see it.

    Only the fields P4 actually consumes are read. The rest (kind, window, group_by,
    leakage_note) belong to the feature layer's own kernels, and copying them here
    would create a second statement of a fact that can fall out of date.
    """

    name: str
    group: str
    sentence: str
    dtype: str
    role: str
    categories: tuple[str, ...] | None
    null_policy: str

    @property
    def is_money(self) -> bool:
        """DEV-005: P2 names every minor-unit column ``*_minor`` and types it int64."""
        return self.name.endswith("_minor") and self.dtype in {"int64", "int32"}

    @property
    def is_basis_points(self) -> bool:
        """An exact integer quotient in basis points, never a float share."""
        return self.name.endswith("_bps") and self.dtype in {"int64", "int32"}


@dataclass(frozen=True, slots=True)
class FeatureRegistry:
    """The declared feature names, in declaration order, with their groups.

    Declaration order is the canonical column order for the feature-spec hash, so
    the hash is stable across processes and independent of dict iteration. Entries
    with ``role: intermediate`` are excluded: P2 declares them consumed by other
    features and never published in the matrix, and fitting on an unpublished column
    is exactly the trained-on-one-thing-scored-on-another failure the spec hash
    exists to catch.
    """

    declarations: Mapping[str, FeatureDeclaration]
    names: tuple[str, ...]
    groups: Mapping[str, tuple[str, ...]]
    guards: FeatureGuard
    spec_version: int
    max_lookback_days: int
    # The plain-English sentence per feature (features.yaml `sentence`). P2 writes it
    # as "both the SHAP panel's dictionary and the scorecard's attribute label", so the
    # reason codes read the same source rather than a second naming scheme.
    attribute_labels: Mapping[str, str]
    # 02 B seam 3, the single source of the feature-spec digest. This is *not* a P4
    # recomputation: P4's view of features.yaml carries names, groups, dtypes and
    # categories only, and a second digest taken from a narrower projection is exactly
    # how "trained on one spec, scored on another" survives a green run -- the two
    # hashes disagreed for the same file, so the mismatch guard could never pass. The
    # value here is what ``oxbow.features.registry`` hashed when it validated the whole
    # declaration (every window, every transform, every sentence), so a frame carrying
    # an older digest is refused rather than quietly re-scored.
    spec_hash: str

    @property
    def by_group(self) -> Mapping[str, tuple[str, ...]]:
        return self.groups

    def declaration(self, name: str) -> FeatureDeclaration:
        if name not in self.declarations:
            raise ConfigError(f"feature {name!r} is not a published entry of the registry")
        return self.declarations[name]


def load_scorecard_config(root: Path | None = None) -> ScorecardConfig:
    """Load and validate ``config/scorecard.yaml``.

    Every key the scorecard needs is required explicitly rather than defaulted:
    a binning floor that silently disappears from config turns the scorecard into
    an unbounded WOE machine, and the artefact would still look well formed.
    """
    repo_root = (root or find_repo_root()).resolve()
    raw = load_yaml(repo_root / CONFIG_DIRNAME / SCORECARD_FILENAME)

    scaling = ScalingConfig(
        pdo=require_float(raw, "scaling.pdo"),
        base_score=require_float(raw, "scaling.base_score"),
        base_odds=require_float(raw, "scaling.base_odds"),
        show_formula_in_ui=require_bool(raw, "scaling.show_formula_in_ui"),
    )
    if scaling.base_odds <= 0.0:
        raise ConfigError("scaling.base_odds must be positive")
    if scaling.pdo <= 0.0:
        raise ConfigError("scaling.pdo must be positive")

    binning = BinningConfig(
        algorithm=require_str(raw, "binning.algorithm"),
        min_bin_pct=require_float(raw, "binning.min_bin_pct"),
        max_bins=require_int(raw, "binning.max_bins"),
        enforce_monotonic_trend=require_bool(raw, "binning.enforce_monotonic_trend"),
        missing_bin=require_str(raw, "binning.special_bins.missing"),
        structural_zero_bin=require_str(raw, "binning.special_bins.structural_zero"),
        min_bin_count=require_int(raw, "binning.min_bin_count"),
        smoothing=require_str(raw, "binning.smoothing"),
        laplace_alpha=require_float(raw, "binning.laplace_alpha"),
        unseen_bin=require_str(raw, "binning.unseen_category_bin"),
        solver=require_str(raw, "binning.solver"),
        prebinning_method=require_str(raw, "binning.prebinning_method"),
        max_n_prebins=require_int(raw, "binning.max_n_prebins"),
        min_prebin_size=require_float(raw, "binning.min_prebin_size"),
        split_digits=require_int(raw, "binning.split_digits"),
        time_limit_seconds=require_float(raw, "binning.time_limit_seconds"),
        categorical_features=require_optional_str_list(raw, "binning.categorical_features"),
        unseen_tail_population_share=require_float(raw, "binning.unseen_tail_population_share"),
        floor_merge_preference=require_str(raw, "binning.floor_merge_preference"),
    )
    if binning.smoothing != "laplace":
        raise ConfigError(f"binning.smoothing must be 'laplace', got {binning.smoothing!r}")
    if not 0.0 < binning.min_bin_pct < 1.0:
        raise ConfigError("binning.min_bin_pct must be in (0, 1)")
    if binning.laplace_alpha <= 0.0:
        raise ConfigError("binning.laplace_alpha must be positive: it is what bounds the WOE")
    if len(set(binning.special_bin_labels)) != 3:
        raise ConfigError("binning special bin labels must be three distinct strings")
    if not 0.0 < binning.min_prebin_size < 1.0:
        raise ConfigError("binning.min_prebin_size must be in (0, 1)")
    if binning.solver not in {"mip", "cp", "ls"}:
        raise ConfigError("binning.solver must be one of mip/cp/ls (optbinning's solvers)")
    if binning.max_bins < 2:
        raise ConfigError("binning.max_bins must be at least 2")

    admission = AdmissionConfig(
        iv_min=require_float(raw, "iv_bounds.min"),
        iv_max=require_float(raw, "iv_bounds.max"),
        above_max_action=require_str(raw, "iv_bounds.above_max_action"),
        below_min_action=require_str(raw, "iv_bounds.below_min_action"),
        show_rule_in_ui=require_bool(raw, "iv_bounds.show_rule_in_ui"),
        record_refusals_in_artifact=require_bool(raw, "iv_bounds.record_refusals_in_artifact"),
    )
    if admission.iv_min >= admission.iv_max:
        raise ConfigError("iv_bounds.min must be below iv_bounds.max")

    fit = FitConfig(
        regularisation=require_str(raw, "fit.regularisation"),
        regularisation_strength=require_float(raw, "fit.regularisation_strength"),
        detect_separation=require_bool(raw, "fit.detect_separation"),
        max_iter=require_int(raw, "fit.max_iter"),
        round_points_to_integer=require_bool(raw, "fit.round_points_to_integer"),
        solver=require_str(raw, "fit.solver"),
        tolerance=require_float(raw, "fit.tolerance"),
        max_abs_coefficient=require_float(raw, "fit.max_abs_coefficient"),
        separation_auc_threshold=require_float(raw, "fit.separation_auc_threshold"),
        separation_action=require_str(raw, "fit.separation_action"),
        max_abs_correlation_with_label=require_float(raw, "fit.max_abs_correlation_with_label"),
    )
    if fit.regularisation not in {"l1", "l2", "elasticnet", "none"}:
        raise ConfigError(f"fit.regularisation must be l1/l2/elasticnet/none, got {fit.regularisation!r}")
    if fit.regularisation == "none":
        # Plan §10: regularisation is ON by default precisely because separation
        # diverges an unregularised fit. Turning it off must be a config edit with a
        # decision log line, not an accident.
        raise ConfigError(
            "fit.regularisation is 'none'; the scorecard requires regularisation on "
            "by default (plan §10) so a separating feature cannot silently produce "
            "huge points."
        )
    if not fit.round_points_to_integer:
        raise ConfigError("fit.round_points_to_integer must be true: points are whole numbers")

    entries = _load_band_entries(raw)
    band_cut_raw = _node(raw, "band_cut_points")
    if band_cut_raw is None:
        cut_points = None
    elif isinstance(band_cut_raw, list) and all(
        isinstance(v, int | float) and not isinstance(v, bool) for v in band_cut_raw
    ):
        cut_points = tuple(float(v) for v in band_cut_raw)
    else:
        raise ConfigError("band_cut_points must be null or a list of numbers")
    bands = BandsConfig(
        direction=require_str(raw, "bands.direction"),
        fit_on=require_str(raw, "bands.fit_on"),
        isotonic_smooth=require_bool(raw, "bands.isotonic_smooth"),
        entries=entries,
        band_cut_points=cut_points,
    )
    if bands.direction != DIRECTION_HIGHER_POINTS_ARE_SAFER:
        raise ConfigError(
            f"bands.direction must be {DIRECTION_HIGHER_POINTS_ARE_SAFER!r}; the reason-code "
            "example in plan §10 gives risky bins negative points, which fixes the scale direction"
        )
    if bands.fit_on != "validation":
        raise ConfigError("bands.fit_on must be 'validation': fitting bands on the fold you "
                          "report on is the leakage the split exists to prevent")

    reasons = ReasonConfig(
        top_n=require_int(raw, "reason_codes.top_n"),
        template=require_str(raw, "reason_codes.template"),
        points_signed_format=require_str(raw, "reason_codes.points_signed_format"),
        negative_word_form=require_bool(raw, "reason_codes.negative_word_form"),
        sign_convention=require_str(raw, "reason_codes.sign_convention"),
        bin_label_source=require_str(raw, "reason_codes.bin_label_source"),
    )
    if reasons.top_n < 1:
        raise ConfigError("reason_codes.top_n must be at least 1")

    drift = DriftConfig(
        psi_watch=require_float(raw, "drift.psi_watch"),
        psi_action=require_float(raw, "drift.psi_action"),
        csi_watch=require_float(raw, "drift.csi_watch"),
        csi_action=require_float(raw, "drift.csi_action"),
        on_action=require_str(raw, "drift.on_action"),
        migration_matrix=require_bool(raw, "drift.migration_matrix"),
        downgrade_rate_alert=require_float(raw, "drift.downgrade_rate_alert"),
        score_psi_bins=require_int(raw, "drift.score_psi_bins"),
        expected_reference=require_str(raw, "drift.expected_reference"),
        period_column=require_str(raw, "drift.period_column"),
    )
    if drift.psi_watch >= drift.psi_action:
        raise ConfigError("drift.psi_watch must be below drift.psi_action")
    if drift.score_psi_bins < 2:
        raise ConfigError("drift.score_psi_bins must be at least 2")

    determinism = DeterminismConfig(
        tie_break=require_str_list(raw, "determinism.tie_break"),
        hash_algorithm=require_str(raw, "determinism.hash_algorithm"),
    )
    if determinism.tie_break[-1] != "account_key":
        raise ConfigError("determinism.tie_break must end in account_key or queue order is not total")

    return ScorecardConfig(
        root=repo_root,
        scaling=scaling,
        binning=binning,
        admission=admission,
        fit=fit,
        bands=bands,
        reasons=reasons,
        drift=drift,
        determinism=determinism,
        raw=raw,
    )


def _load_band_entries(raw: Mapping[str, object]) -> tuple[BandEntry, ...]:
    """Read the band table, requiring the plan's per-band fields.

    Each band needs its action and the rate target it is cut to; a band with no
    action is decoration, and the plan is explicit that a band implies an action.
    """
    value = _node(raw, "bands.entries")
    if not isinstance(value, list) or not value:
        raise ConfigError("bands.entries must be a non-empty list")
    entries: list[BandEntry] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(f"bands.entries[{index}] must be a mapping")
        entry_map: Mapping[str, object] = item
        entries.append(
            BandEntry(
                band_id=require_str(entry_map, "id"),
                label=require_str(entry_map, "label"),
                action=require_str(entry_map, "action"),
                glyph=require_str(entry_map, "glyph"),
                bad_rate_multiple_of_base=require_optional_float(
                    entry_map, "bad_rate_multiple_of_base"
                ),
                min_points=require_optional_float(entry_map, "min_points"),
                max_points=require_optional_float(entry_map, "max_points"),
            )
        )
    ids = [entry.band_id for entry in entries]
    if len(set(ids)) != len(ids):
        raise ConfigError("bands.entries ids must be unique")
    if entries[-1].bad_rate_multiple_of_base is not None:
        raise ConfigError("the last band must be open-ended (bad_rate_multiple_of_base: null)")
    multiples = [entry.bad_rate_multiple_of_base for entry in entries[:-1]]
    if any(m is None for m in multiples):
        raise ConfigError("every band but the last needs a bad_rate_multiple_of_base")
    # noinspection SpellCheckingInspection
    ordered = [float(m) for m in multiples if m is not None]
    if ordered != sorted(ordered):
        raise ConfigError("band rate targets must increase from the safest band to the riskiest")
    return tuple(entries)


def load_feature_registry(root: Path | None = None) -> FeatureRegistry:
    """Read the declared feature names and guards from ``config/features.yaml``.

    Read-only by design: P2 owns the registry. P4 needs it for two reasons: the
    feature-spec hash contract, and a deterministic generated frame when the real
    feature table is not on disk yet.
    """
    repo_root = (root or find_repo_root()).resolve()
    raw = load_yaml(repo_root / CONFIG_DIRNAME / FEATURES_FILENAME)

    # The digest is computed by the layer that owns the declaration, over the same
    # mapping this reader is about to walk a second, narrower time. `parse_registry` is
    # P2's validator: it refuses an unknown kind, a missing window, a float money column
    # and a 76th published feature, and then hashes what it accepted. A P4 view that
    # accepted a file P2 would reject would be a second definition of "valid registry",
    # so the strict parse runs first and its digest is the one that travels.
    declared = parse_declared_registry(raw)

    features_raw = _node(raw, "features")
    if not isinstance(features_raw, list) or not features_raw:
        raise ConfigError("features.yaml 'features' must be a non-empty list of entries")
    allowed_roles = {"feature", "intermediate"}
    declared_groups = _node(raw, "groups")
    if not isinstance(declared_groups, list):
        raise ConfigError("features.yaml 'groups' must be the list of taxonomy group names")
    known_groups = {str(group) for group in declared_groups}

    declarations: dict[str, FeatureDeclaration] = {}
    names: list[str] = []
    labels: dict[str, str] = {}
    groups: dict[str, list[str]] = {str(group): [] for group in known_groups}
    for index, entry in enumerate(features_raw):
        if not isinstance(entry, Mapping):
            raise ConfigError(f"features.yaml features[{index}] must be a mapping")
        item: Mapping[str, object] = entry
        name = require_str(item, "id")
        group = require_str(item, "group")
        if group not in known_groups:
            raise ConfigError(
                f"features.yaml features[{index}] ({name}) declares group {group!r}, "
                "which is not in the file's group list"
            )
        sentence = require_str(item, "sentence")
        dtype = require_str(item, "dtype")
        if dtype not in {"bool", "int32", "int64", "float64", "string"}:
            raise ConfigError(
                f"features.yaml feature {name!r} declares dtype {dtype!r}; the registry's "
                "vocabulary is bool/int32/int64/float64/string"
            )
        role = "feature" if "role" not in item else require_str(item, "role")
        if role not in allowed_roles:
            raise ConfigError(f"features.yaml feature {name!r} has role {role!r}")
        categories_value = item.get("categories")
        categories: tuple[str, ...] | None = None
        if isinstance(categories_value, list):
            categories = tuple(str(value) for value in categories_value)
        declaration = FeatureDeclaration(
            name=name,
            group=group,
            sentence=sentence,
            dtype=dtype,
            role=role,
            categories=categories,
            null_policy=require_str(item, "null_policy"),
        )
        if name in declarations:
            raise ConfigError(f"features.yaml declares {name!r} twice")
        declarations[name] = declaration
        if role == "feature":
            names.append(name)
            labels[name] = sentence
            groups[group].append(name)
    if not names:
        raise ConfigError("features.yaml publishes no feature with role 'feature'")
    if names != list(declared.matrix_ids):
        raise ConfigError(
            "P4's published-feature view and P2's registry disagree on the column order: "
            f"{names[:5]}… against {list(declared.matrix_ids)[:5]}…. Declaration order is the "
            "canonical feature order, so a divergence here means one of the two readers is "
            "skipping or re-adding an entry"
        )
    groups_by_name = {group: tuple(members) for group, members in sorted(groups.items())}

    guards = FeatureGuard(
        max_abs_correlation_with_label=require_float(raw, "guards.max_abs_correlation_with_label"),
        require_finite=require_bool(raw, "guards.require_finite"),
        missing_value_policy=require_str(raw, "guards.missing_value_policy"),
    )
    if guards.missing_value_policy != "explicit_bin":
        raise ConfigError(
            "features.yaml guards.missing_value_policy must be 'explicit_bin': the scorecard "
            "bins missing as its own category and never imputes a mean"
        )
    return FeatureRegistry(
        declarations=declarations,
        names=tuple(names),
        groups=groups_by_name,
        guards=guards,
        spec_version=require_int(raw, "spec_version"),
        max_lookback_days=require_int(raw, "max_lookback_days"),
        attribute_labels=labels,
        spec_hash=declared.spec_hash,
    )


__all__ = [
    "CONFIG_DIRNAME",
    "DIRECTION_HIGHER_POINTS_ARE_SAFER",
    "SCORECARD_FILENAME",
    "AdmissionConfig",
    "BandEntry",
    "BandsConfig",
    "BinningConfig",
    "DeterminismConfig",
    "DriftConfig",
    "FeatureDeclaration",
    "FeatureGuard",
    "FeatureRegistry",
    "FitConfig",
    "ReasonConfig",
    "ScalingConfig",
    "ScorecardConfig",
    "load_feature_registry",
    "load_scorecard_config",
    "require_bool",
    "require_float",
    "require_int",
    "require_optional_float",
    "require_optional_str_list",
    "require_str",
    "require_str_list",
]
