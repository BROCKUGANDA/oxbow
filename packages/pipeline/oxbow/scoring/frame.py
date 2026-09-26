"""The training-frame input contract, validated at the boundary.

Plan §10 / P2's feature layer hands P4 one frame and P4 must be able to say
exactly what it accepted. The contract is agreed and injected, so this module is
where the agreement is *checked*: column names, dtypes, the feature-spec hash,
finite values, one row per account and as-of, and the fold column. Every failure
names the offending column, because "the frame is invalid" is not an error
message anyone can act on at 2 a.m. on day 7.

Two rules from the plan drive the design:

* **The feature-spec hash refuses to score on mismatch** (02 B seam 3). Training
  on one feature set and scoring with another produces a confident number from
  the wrong inputs, and nothing downstream can tell.
* **Missing is a bin, not a mean** (03 H). Nulls are therefore *not* dropped and
*not* imputed here; they travel to the binning layer, which gives them their own
WOE. ``inf`` however is a corruption, not a missing value, and it fails the run
naming its column -- LightGBM tolerates it, the scorecard does not, and the two
silently diverging is the exact defect features.yaml's ``require_finite`` guards.

Dev-011 consequence: on PaySim the graph-derived groups are null for nearly every
account, so the null path is the normal case on the primary tabular corpus rather
than an edge case. It is exercised by every test in this phase.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import polars as pl

from oxbow.scoring.config import FeatureRegistry
from oxbow.scoring.errors import FeatureSpecHashMismatch, FrameContractError

# Provenance strings are constants, not free text: a statistic emitted without a
# provable provenance label cannot be reported, and the two allowed values are
# the only ones the report may contain.
PROVENANCE_REAL: str = "real_feature_table"
PROVENANCE_GENERATED: str = "generated_frame_seed_1337"
PROVENANCE_VALUES: tuple[str, ...] = (PROVENANCE_REAL, PROVENANCE_GENERATED)

COL_ACCOUNT_KEY: str = "account_key"
COL_AS_OF_TS: str = "as_of_ts"
COL_FOLD: str = "fold"
COL_LABEL: str = "label_is_fraud"
COL_LABEL_TYPOLOGY: str = "label_typology"
COL_SPEC_HASH: str = "feature_spec_hash"
COL_ROLE: str = "role"

ROLE_TRAIN: str = "train"
ROLE_VALIDATION: str = "validation"
ROLE_TEST: str = "test"
ROLE_VALUES: tuple[str, ...] = (ROLE_TRAIN, ROLE_VALIDATION, ROLE_TEST)

# The dtype vocabulary the registry actually uses (P2 declares bool, int32, int64,
# float64 and string). Booleans are accepted as numeric because the scorecard reads a
# flag as a 0/1 column; the contract is stated as a set rather than inline so the
# validator and the matrix builder cannot drift apart.
_INTEGER_DTYPES: tuple[pl.DataType, ...] = (
    pl.Int8,
    pl.Int16,
    pl.Int32,
    pl.Int64,
    pl.UInt32,
    pl.UInt64,
)
_NUMERIC_DTYPES: tuple[pl.DataType, ...] = (
    *_INTEGER_DTYPES,
    pl.Float32,
    pl.Float64,
    pl.Boolean,
)

META_COLUMNS: tuple[str, ...] = (
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    COL_FOLD,
    COL_LABEL,
    COL_LABEL_TYPOLOGY,
    COL_SPEC_HASH,
)

# Columns a later stage is allowed to attach to a frame it received. Anything
# else is an undeclared column and fails closed, the same way the contracts layer
# fails an unknown input column (03 B: test_unknown_column_fails_closed).
ALLOWED_ATTACHED_COLUMNS: dict[str, str] = {
    "rule_severity_max": "attached by the injected rules provider",
    "rule_hit_count": "attached by the injected rules provider",
    "cycle_flag": "attached by the injected rules provider",
    "fan_flag": "attached by the injected rules provider",
}


def canonical_spec_hash(registry: FeatureRegistry) -> str:
    """The feature-spec hash P4 recomputes for a declared registry.

    One authority, and it is P2's. This used to be a second digest -- ``sha256`` over
    the sorted names, the categorical subset and the spec version -- taken from P4's
    narrower view of ``config/features.yaml``. P2's own hash covers every declared
    parameter of every entry: the windows, the aggregation, the predicate, the dtype,
    the sentence. Two digests for one file is not "checked twice", it is one guard that
    can never pass: a frame built by the feature layer carries P2's digest, this
    function returned something else for the same registry, and 02 B seam 3's mismatch
    check therefore refused *every* real frame while accepting only frames that had
    been stamped by this function. Delegating is the fix; ``registry.spec_hash`` is
    produced by :func:`oxbow.features.registry.parse_registry` when the file is loaded,
    so what is compared here is the declaration, not a projection of it.

    Consequence worth stating plainly: shortening a window, changing a transform or
    adding a column moves this digest while leaving the column *names* identical, so a
    frame carrying the parent's digest is refused. That is the guard the leakage
    argument rests on, and it is what ``tests/unit/test_p2_spec_hash.py`` exists to
    prove bites.
    """
    return registry.spec_hash


def require_feature_hash_match(recorded_hash: str, frame_hash: str) -> None:
    """Refuse to proceed when the frame's spec hash is not the trained-on hash.

    Plan §8 / §10: ``test_feature_hash_mismatch_refuses``. The message carries both
    hashes because an operator comparing artefacts needs to see which side moved.
    """
    if recorded_hash != frame_hash:
        raise FeatureSpecHashMismatch(
            "feature-spec hash mismatch: the model was trained on "
            f"{recorded_hash[:16]}… and this frame declares {frame_hash[:16]}…. "
            "Refusing to score: the feature set changed after training, so any "
            "number produced here would be computed from columns the model never "
            "saw (02 B seam 3)."
        )


@dataclass(frozen=True, slots=True)
class TrainingFrame:
    """A validated frame plus the metadata that made validation possible."""

    data: pl.DataFrame
    feature_names: tuple[str, ...]
    categorical_features: tuple[str, ...]
    provenance: str
    feature_spec_hash: str
    frame_sha256: str
    n_rows: int
    n_positive: int
    base_rate: float
    validation_fraction: float
    # Populated by the frame builder for the generator's own audit trail; empty on
    # a real frame, where P2 owns the missingness story.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def positives_by_role(self) -> dict[str, int]:
        """Positives per role: the number that decides the calibration branch."""
        out: dict[str, int] = {}
        for role in ROLE_VALUES:
            subset = self.data.filter(pl.col(COL_ROLE) == role)
            out[role] = int(subset.get_column(COL_LABEL).sum()) if subset.height else 0
        return out

    @property
    def folds(self) -> tuple[int, ...]:
        """Distinct fold ids, ascending."""
        values = self.data.get_column(COL_FOLD).unique().to_list()
        return tuple(sorted(int(value) for value in values))

    def role_slice(self, role: str) -> pl.DataFrame:
        """Rows assigned to one role, in deterministic order."""
        if role not in ROLE_VALUES:
            raise FrameContractError(f"unknown role {role!r}; expected one of {ROLE_VALUES}")
        return self.data.filter(pl.col(COL_ROLE) == role).sort([COL_AS_OF_TS, COL_ACCOUNT_KEY])

    def fold_slice(self, fold: int, roles: Sequence[str] | None = None) -> pl.DataFrame:
        """Rows of one fold, optionally restricted to roles, in deterministic order."""
        out = self.data.filter(pl.col(COL_FOLD) == fold)
        if roles is not None:
            unknown = [role for role in roles if role not in ROLE_VALUES]
            if unknown:
                raise FrameContractError(f"unknown roles {unknown}")
            out = out.filter(pl.col(COL_ROLE).is_in(list(roles)))
        return out.sort([COL_AS_OF_TS, COL_ACCOUNT_KEY])

    def feature_matrix(self, frame: pl.DataFrame | None, dtype: str = "float64") -> np.ndarray:
        """Features as a numeric matrix, nulls kept as NaN.

        Categorical columns are encoded by their *fit* category key, which the
        binning layer owns; ``frame`` must therefore be the frame the binning was
        fitted on, or one scored through the same encoder. This method is used for
        the GBM, which takes NaN natively and bins it itself.
        """
        source = self.data if frame is None else frame
        columns: list[np.ndarray] = []
        for name in self.feature_names:
            column = source.get_column(name)
            if name in set(self.categorical_features):
                codes = _stable_category_codes(column)
                columns.append(codes.astype(dtype))
            else:
                columns.append(column.cast(pl.Float64).to_numpy().astype(dtype))
        if not columns:
            raise FrameContractError("frame has no feature columns to matrix")
        matrix = np.column_stack(columns)
        return np.ascontiguousarray(matrix, dtype=np.float64 if dtype == "float64" else np.float32)

    def labels(self, frame: pl.DataFrame | None = None) -> np.ndarray:
        """Label vector as int32, in the same row order as ``feature_matrix``."""
        source = self.data if frame is None else frame
        return source.get_column(COL_LABEL).cast(pl.Int32).to_numpy().astype(np.int32)

    def keys(self, frame: pl.DataFrame | None = None) -> pl.Series:
        source = self.data if frame is None else frame
        return source.get_column(COL_ACCOUNT_KEY)

    def as_of(self, frame: pl.DataFrame | None = None) -> pl.Series:
        source = self.data if frame is None else frame
        return source.get_column(COL_AS_OF_TS)

    def fold_ids(self, frame: pl.DataFrame | None = None) -> pl.Series:
        source = self.data if frame is None else frame
        return source.get_column(COL_FOLD)


def _stable_category_codes(column: pl.Series) -> np.ndarray:
    """Hash-derived float code per category, stable across frames and runs.

    Not an ordinal encoding of frequency: a frequency-ordered encoding would let
    the GBM learn the training population order as a numeric trend. The FNV-1a
    hash of the category string is a deterministic permutation with no monotone
    semantics, so the tree has to use the binning layer for category evidence.
    """
    values = column.fill_null("").cast(pl.String).to_list()
    codes = np.empty(len(values), dtype=np.float64)
    for index, value in enumerate(values):
        if value == "":
            codes[index] = np.nan
            continue
        digest = 0x811C9DC5
        for byte in value.encode("utf-8"):
            digest = ((digest ^ byte) * 0x01000193) & 0xFFFFFFFF
        codes[index] = float(digest)
    return codes


def _check_column_present(frame: pl.DataFrame, name: str, role: str) -> None:
    if name not in frame.columns:
        raise FrameContractError(f"{role} column {name!r} is missing from the training frame")


def _validate(frame: pl.DataFrame, registry: FeatureRegistry, categorical: Sequence[str]) -> None:
    """Every structural rule the frame must satisfy, checked before anything fits."""
    if frame.height == 0:
        raise FrameContractError("training frame has zero rows: there is nothing to fit")

    for name in META_COLUMNS:
        _check_column_present(frame, name, "contract")

    dtypes = dict(zip(frame.columns, frame.dtypes, strict=True))
    if dtypes[COL_ACCOUNT_KEY] != pl.String:
        raise FrameContractError(f"{COL_ACCOUNT_KEY} must be String, got {dtypes[COL_ACCOUNT_KEY]}")
    if dtypes[COL_AS_OF_TS] != pl.Datetime("us", "UTC"):
        raise FrameContractError(
            f"{COL_AS_OF_TS} must be Datetime[μs, UTC], got {dtypes[COL_AS_OF_TS]}"
        )
    if dtypes[COL_FOLD] not in _INTEGER_DTYPES:
        raise FrameContractError(f"{COL_FOLD} must be an integer fold id, got {dtypes[COL_FOLD]}")
    if dtypes[COL_LABEL] not in (pl.Int8, pl.Int16, pl.Int32, pl.Boolean):
        raise FrameContractError(
            f"{COL_LABEL} must be an integer label (int8 per the contract), got {dtypes[COL_LABEL]}"
        )
    if dtypes[COL_LABEL_TYPOLOGY] != pl.String:
        raise FrameContractError(
            f"{COL_LABEL_TYPOLOGY} must be String or null, got {dtypes[COL_LABEL_TYPOLOGY]}"
        )
    if dtypes[COL_SPEC_HASH] != pl.String:
        raise FrameContractError(f"{COL_SPEC_HASH} must be String, got {dtypes[COL_SPEC_HASH]}")

    if frame.get_column(COL_LABEL).null_count():
        raise FrameContractError(
            f"{COL_LABEL} has {frame.get_column(COL_LABEL).null_count()} nulls: an unknown "
            "label is not a zero and must never enter training as one (03 A rule 2)"
        )
    label_values = set(frame.get_column(COL_LABEL).unique().to_list())
    if not label_values.issubset({0, 1}):
        raise FrameContractError(f"{COL_LABEL} must be 0/1, found {sorted(label_values)}")
    if frame.get_column(COL_SPEC_HASH).null_count():
        raise FrameContractError(f"{COL_SPEC_HASH} must be present on every row")
    if frame.get_column(COL_FOLD).null_count():
        raise FrameContractError(f"{COL_FOLD} must be present on every row")

    declared = set(registry.names)
    missing = sorted(declared - set(frame.columns))
    if missing:
        raise FrameContractError(
            f"{len(missing)} declared feature column(s) are absent from the frame: {missing}"
        )
    allowed = set(META_COLUMNS) | declared | {COL_ROLE, *ALLOWED_ATTACHED_COLUMNS}
    extras = sorted(set(frame.columns) - allowed)
    if extras:
        raise FrameContractError(
            f"frame carries undeclared column(s) {extras}: an undeclared column is an "
            "undeclared feature, and features come from config/features.yaml"
        )

    numeric_dtypes = _NUMERIC_DTYPES
    categorical_set = set(categorical)
    unknown_categorical = sorted(categorical_set - declared)
    if unknown_categorical:
        raise FrameContractError(
            f"declared categorical feature(s) {unknown_categorical} are not published entries "
            "of config/features.yaml"
        )
    for name in registry.names:
        dtype = dtypes[name]
        if name in categorical_set:
            # A coded category is still a category: P2 types local_hour_code and
            # txn_type_code as int32 with a declared category mapping, and ordering
            # those codes as numbers would teach the scorecard a trend that is an
            # artefact of the coding, not of the behaviour.
            if dtype not in (pl.String, *numeric_dtypes):
                raise FrameContractError(
                    f"feature {name!r} is declared categorical and must be String or an "
                    f"integer code, got {dtype}"
                )
            continue
        if dtype == pl.String:
            raise FrameContractError(
                f"feature {name!r} is String but is not declared categorical in "
                "scorecard.yaml binning.categorical_features"
            )
        if dtype not in numeric_dtypes:
            raise FrameContractError(
                f"feature {name!r} must be numeric (int64/float64/bool per the registry), "
                f"got {dtype}"
            )
        if registry.guards.require_finite and dtype != pl.Boolean:
            values = frame.get_column(name).cast(pl.Float64).drop_nulls().to_numpy()
            if values.size and not bool(np.all(np.isfinite(values))):
                raise FrameContractError(
                    f"feature {name!r} contains a non-finite value at the frame boundary. "
                    "LightGBM tolerates it, the scorecard does not, and the two silently "
                    "diverge (features.yaml guards.require_finite)."
                )
            # The distinction this check draws is deliberate: a polars NULL is
            # *missing*, and missing is a bin with its own WOE (03 H). A float NaN is
            # not missing, it is an arithmetic accident that survived into the table.
            # features.yaml's require_finite exists because the two look identical to
            # LightGBM and nothing else; rejecting NaN here means the scorecard's
            # missing bin can only ever hold true nulls.

    duplicates = (
        frame.select([COL_ACCOUNT_KEY, COL_AS_OF_TS])
        .group_by([COL_ACCOUNT_KEY, COL_AS_OF_TS])
        .agg(pl.len().alias("dup_rows"))
        .filter(pl.col("dup_rows") > 1)
    )
    if duplicates.height:
        sample = duplicates.head(3).to_dicts()
        raise FrameContractError(
            f"frame has {duplicates.height} duplicated (account_key, as_of_ts) keys, e.g. {sample}"
        )


def frame_content_hash(frame: pl.DataFrame) -> str:
    """Content hash of the frame, sorted into a canonical row order.

    ``make verify-determinism`` compares artifact digests, not row counts, so the
    frame hash must be order-independent: rows are sorted by the total order
    (as_of_ts, account_key) before serialisation.
    """
    ordered = frame.sort([COL_AS_OF_TS, COL_ACCOUNT_KEY])
    # Bytes, not a file: the hash is of the frame's content, and writing through the
    # repo filesystem from inside a validator would leave a stray artifact per call.
    sink = io.BytesIO()
    ordered.write_parquet(sink, use_pyarrow=False)
    return hashlib.sha256(sink.getvalue()).hexdigest()


def assign_roles(
    frame: pl.DataFrame,
    validation_fraction: float,
    n_folds: int,
) -> pl.Series:
    """Derive train / validation / test roles from ``fold`` and ``as_of_ts``.

    WHY THIS EXISTS: the sanctioned splits module (P2) owns purging and the 30-day
    embargo, and P4 must not invent its own split. Until that module emits a role
    column, roles are derived here from the boundaries *P0 already declared* in
    config/splits.yaml -- validation is the last ``fraction_of_train`` slice of each
    training period, and the highest fold is the untouched test fold. When P2 ships
    the column, it wins and this function is not called; the fallback is explicit in
    the payload so a reader can tell which produced the numbers.

    Determinism: rows are ordered by (as_of_ts, account_key) inside a fold, so the
    boundary index is arithmetic, not a random split.
    """
    if not 0.0 < validation_fraction < 0.9:
        raise FrameContractError("validation fraction must be in (0, 0.9)")
    if frame.get_column(COL_FOLD).null_count():
        raise FrameContractError(f"{COL_FOLD} must be present on every row")
    folds = sorted(int(value) for value in frame.get_column(COL_FOLD).unique().to_list())
    if len(folds) < 2:
        raise FrameContractError(
            f"need at least 2 folds to hold out a test fold; the frame carries {folds}"
        )
    if n_folds != len(folds):
        raise FrameContractError(
            f"config declares {n_folds} folds, the frame carries {len(folds)}: {folds}"
        )

    ordered = frame.sort([COL_FOLD, COL_AS_OF_TS, COL_ACCOUNT_KEY])
    fold_index = [int(value) for value in ordered.get_column(COL_FOLD).to_list()]
    # Microseconds since epoch, read off the column's physical representation: the
    # boundary is then integer arithmetic on a monotone sequence rather than a
    # datetime compare per row.
    as_of_us = ordered.get_column(COL_AS_OF_TS).to_physical().cast(pl.Int64).to_numpy()
    test_fold = max(folds)

    # One pass over rows already sorted by (fold, as_of_ts, account_key) to get each
    # fold's time span. Fold start/end are the only inputs the boundary needs, so the
    # validation window is a time cut and its positive count moves with the data.
    spans: dict[int, tuple[int, int]] = {}
    for position, fold_value in enumerate(fold_index):
        low, high = spans.get(fold_value, (as_of_us[position], as_of_us[position]))
        spans[fold_value] = (min(low, int(as_of_us[position])), max(high, int(as_of_us[position])))

    cuts: dict[int, int] = {}
    for fold_value, (start_us, end_us) in spans.items():
        span_us = max(end_us - start_us, 1)
        cuts[fold_value] = start_us + int(span_us * (1.0 - validation_fraction))

    roles = [
        ROLE_TEST
        if fold_value == test_fold
        else (ROLE_VALIDATION if as_of_us[position] >= cuts[fold_value] else ROLE_TRAIN)
        for position, fold_value in enumerate(fold_index)
    ]

    # Join back onto the incoming row order: callers attach the series to the frame
    # they handed in, which is sorted differently.
    reordered = ordered.select([COL_ACCOUNT_KEY, COL_AS_OF_TS]).with_columns(
        pl.Series("role", roles)
    )
    joined = frame.join(reordered, on=[COL_ACCOUNT_KEY, COL_AS_OF_TS], how="left").get_column(
        "role"
    )
    if joined.null_count():
        raise FrameContractError(
            "role assignment lost rows during the join back: the frame's "
            "(account_key, as_of_ts) pair is not unique"
        )
    return joined


def fold_time_spans(frame: pl.DataFrame) -> dict[int, tuple[datetime, datetime]]:
    """Per-fold (first, last) as-of timestamps, for the split-def audit trail.

    P6 reports the walk-forward diagram from these numbers, so they are read off
    the frame rather than recomputed from config fractions.
    """
    ordered = frame.sort([COL_FOLD, COL_AS_OF_TS, COL_ACCOUNT_KEY])
    out: dict[int, tuple[datetime, datetime]] = {}
    for group in ordered.partition_by([COL_FOLD], maintain_order=True):
        fold = int(group.get_column(COL_FOLD)[0])
        column = group.get_column(COL_AS_OF_TS)
        out[fold] = (column[0], column[-1])
    return dict(sorted(out.items()))


def build_training_frame(
    frame: pl.DataFrame,
    registry: FeatureRegistry,
    categorical_features: Sequence[str],
    provenance: str,
    validation_fraction: float,
    n_folds: int,
    notes: Sequence[str] = (),
) -> TrainingFrame:
    """Validate a raw frame and return the typed container P4 works in.

    ``provenance`` is a required argument with no default: there is no way to
    build a frame and accidentally report it as measured on PaySim.
    """
    if provenance not in PROVENANCE_VALUES:
        raise FrameContractError(
            f"provenance must be one of {PROVENANCE_VALUES}, got {provenance!r}. Every "
            "statistic P4 emits carries this label, so it cannot be inferred."
        )
    _validate(frame, registry, categorical_features)

    hashes = set(frame.get_column(COL_SPEC_HASH).unique().to_list())
    if len(hashes) != 1:
        raise FrameContractError(
            f"frame carries {len(hashes)} distinct feature_spec_hash values; one frame is "
            "one feature spec"
        )
    spec_hash = next(iter(hashes))
    expected = canonical_spec_hash(registry)
    if spec_hash != expected:
        raise FrameContractError(
            f"feature_spec_hash on the frame ({spec_hash[:16]}…) does not match the declared "
            f"registry ({expected[:16]}…). Both sides are the digest "
            "oxbow.features.registry computes over the whole declaration, so this is not a "
            "difference of opinion about how to hash: either the registry changed after the "
            "frame was built, or the frame came from a different spec."
        )

    if COL_ROLE in frame.columns:
        existing = set(frame.get_column(COL_ROLE).unique().to_list())
        if not existing.issubset(set(ROLE_VALUES)):
            raise FrameContractError(f"role column has unknown values {sorted(existing)}")
        roles = frame.get_column(COL_ROLE)
    else:
        roles = assign_roles(frame, validation_fraction=validation_fraction, n_folds=n_folds)

    validated = frame.with_columns(roles.alias(COL_ROLE))
    n_positive = int(validated.get_column(COL_LABEL).sum())
    return TrainingFrame(
        data=validated,
        feature_names=registry.names,
        categorical_features=tuple(categorical_features),
        provenance=provenance,
        feature_spec_hash=spec_hash,
        frame_sha256=frame_content_hash(validated),
        n_rows=validated.height,
        n_positive=n_positive,
        base_rate=n_positive / validated.height,
        validation_fraction=validation_fraction,
        notes=tuple(notes),
    )


def utc_now_iso() -> str:
    """Timestamp for artefacts, second resolution in UTC."""
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "ALLOWED_ATTACHED_COLUMNS",
    "COL_ACCOUNT_KEY",
    "COL_AS_OF_TS",
    "COL_FOLD",
    "COL_LABEL",
    "COL_LABEL_TYPOLOGY",
    "COL_ROLE",
    "COL_SPEC_HASH",
    "META_COLUMNS",
    "PROVENANCE_GENERATED",
    "PROVENANCE_REAL",
    "PROVENANCE_VALUES",
    "ROLE_TEST",
    "ROLE_TRAIN",
    "ROLE_VALIDATION",
    "ROLE_VALUES",
    "TrainingFrame",
    "assign_roles",
    "build_training_frame",
    "canonical_spec_hash",
    "fold_time_spans",
    "frame_content_hash",
    "require_feature_hash_match",
    "utc_now_iso",
]
