"""What P4b receives, and the protocols that make the receipt checkable.

The plan's coupling rules decide the shape of this module:

* **The rules layer is injected, never imported.** ``import-linter`` forbids
  ``models/`` reaching into ``rules/`` or any adapter, and the reason is not tidiness:
  the rules layer is P3b's, its hit vocabulary is still moving, and a hard import would
  let a change there break scoring silently. ``RuleHitProvider`` is the seam: any
  callable that returns one row per account with the declared severity columns will do,
  including P3b's real emitter and the deterministic provider the tests use.
* **The feature hash is checked before a model is fitted or applied.** A frame whose
  ``feature_spec_hash`` differs from the one the artefact recorded cannot be scored
  (plan §8, 02 B seam 3).
* **The scorecard arrives as columns, not as an object.** P4b consumes a frame already
  scored by ``oxbow.scoring``: ``p_scorecard_uncalibrated``, the points table and the
  band. models/ therefore does not need to know how the scorecard was fitted, which is
  what keeps DEV-001's two halves independently testable.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

import numpy as np
import polars as pl

from oxbow.models.errors import FrameContractViolationError, FusionInputsError
from oxbow.scoring.frame import (
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    COL_FOLD,
    COL_LABEL,
    COL_LABEL_TYPOLOGY,
    COL_ROLE,
    COL_SPEC_HASH,
    TrainingFrame,
    # One encoder for both layers; see feature_matrix below for why this is imported
    # rather than reimplemented.
    _stable_category_codes,
    require_feature_hash_match,
)

# The fusion input vector, exactly as config/model.yaml declares it. Kept as a tuple of
# names so a mismatch between config and code fails at load rather than silently
# reordering the coefficient list.
FUSION_INPUTS: Final = (
    "p_scorecard",
    "p_gbm",
    "anomaly_norm",
    "rule_severity_max",
    "rule_hit_count",
    "cycle_flag",
    "fan_flag",
)

# The rule-derived columns the meta-learner and the baseline both consume. The names
# mirror config/features.yaml's rule_derived group; the values come from P3b's emitted
# structured hits, never from a re-implementation here.
RULE_COLUMN_SEVERITY_MAX: Final = "rule_severity_max"
RULE_COLUMN_HIT_COUNT: Final = "rule_hit_count"
RULE_COLUMN_CYCLE_FLAG: Final = "cycle_flag"
RULE_COLUMN_FAN_FLAG: Final = "fan_flag"
RULE_COLUMN_SEVERITY_SUM: Final = "rule_severity_sum"

SCORED_FRAME_COLUMNS: Final = (
    "score_points",
    "score_continuous",
    "band",
    "p_scorecard_uncalibrated",
    "points_json",
    "reason_codes",
    "features_in_missing_bin",
)

REQUIRED_META_COLUMNS: Final = (
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    COL_FOLD,
    COL_LABEL,
    COL_LABEL_TYPOLOGY,
    COL_SPEC_HASH,
    COL_ROLE,
)


@runtime_checkable
class RuleHitProvider(Protocol):
    """P3b's structured rule hits, injected as a callable.

    The contract is the plan's: a rule emits a *hit*, not a boolean, with severity
    normalised to 0-1 so two accounts firing the same rule stay rankable. This layer
    therefore requires severity, not just a flag, and says so in the failure message.
    """

    def rule_hits(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Return one row per (account_key, as_of_ts) with the declared severity columns."""
        ...


def attach_rule_hits(frame: pl.DataFrame, provider: RuleHitProvider) -> pl.DataFrame:
    """Join the injected rule hits onto a scored frame, failing loud on a shortfall.

    Checked rather than assumed because the fusion inputs and the rules-only baseline
    are both defined on these columns: a provider that returns a boolean where a
    severity is required would silently turn the baseline into a count, and the
    comparison "does the ML earn its complexity" would then be measuring something
    other than what the plan describes.
    """
    if not callable(getattr(provider, "rule_hits", None)):
        raise FrameContractViolationError(
            "the injected rules provider has no callable rule_hits(frame); models/ must "
            "not import rules/, so the hits arrive through this seam or not at all"
        )
    hits = provider.rule_hits(frame)
    if hits.height == 0:
        raise FrameContractViolationError(
            f"rules provider returned {hits.height} hit rows for a {frame.height}-row frame"
        )
    required = [
        COL_ACCOUNT_KEY,
        COL_AS_OF_TS,
        RULE_COLUMN_SEVERITY_MAX,
        RULE_COLUMN_HIT_COUNT,
        RULE_COLUMN_CYCLE_FLAG,
        RULE_COLUMN_FAN_FLAG,
    ]
    # The rule-derived aggregates are also declared features in config/features.yaml, so a
    # frame built by the feature layer may already carry them. A column is satisfied when
    # *either* source has it; what is refused is a column neither source provides, because
    # the fusion inputs and the rules-only baseline are defined on it.
    missing = sorted(set(required) - set(hits.columns) - set(frame.columns))
    if missing:
        raise FrameContractViolationError(
            f"neither the rules provider nor the scored frame supplies {missing}; the fusion "
            "inputs and the rules-only baseline are defined on these columns (plan §9: every "
            "rule emits a structured hit with severity normalised 0-1)"
        )
    for column in (RULE_COLUMN_SEVERITY_MAX, RULE_COLUMN_HIT_COUNT):
        if column not in hits.columns:
            continue
        values = hits.get_column(column).cast(pl.Float64).drop_nulls().to_numpy()
        if values.size and (float(np.min(values)) < 0.0):
            raise FrameContractViolationError(
                f"rules provider column {column!r} carries a negative value; severity is "
                "normalised 0-1 and a hit count cannot be negative"
            )
    # The rule-derived aggregates are *also* declared features in config/features.yaml, so
    # a scored frame can already carry them from the feature build. When the injected
    # provider emits a column the frame has, that is two sources for one number: the
    # feature table's copy came from the rules version that built the features, and the
    # provider's copy is the current rules layer. Equal is a no-op and gets dropped from
    # the join; unequal is a version skew, and scoring against a silently-chosen one of
    # the two is how a rules change moves the ranking without anyone noticing.
    overlapping = [
        name
        for name in hits.columns
        if name in frame.columns and name not in (COL_ACCOUNT_KEY, COL_AS_OF_TS)
    ]
    aligned = hits.select([COL_ACCOUNT_KEY, COL_AS_OF_TS, *overlapping]).join(
        frame.select([COL_ACCOUNT_KEY, COL_AS_OF_TS, *overlapping]),
        on=[COL_ACCOUNT_KEY, COL_AS_OF_TS],
        how="inner",
        suffix="_frame",
    )
    for name in overlapping:
        provider_side = aligned.get_column(name).cast(pl.Float64).fill_null(0.0).to_numpy()
        frame_side = aligned.get_column(f"{name}_frame").cast(pl.Float64).fill_null(0.0).to_numpy()
        if provider_side.size and not np.allclose(provider_side, frame_side, rtol=1e-9, atol=1e-12):
            disagree = int(np.count_nonzero(np.abs(provider_side - frame_side) > 1e-9))
            raise FrameContractViolationError(
                f"rules provider column {name!r} disagrees with the frame's own copy on "
                f"{disagree} of {provider_side.size} matched row(s). Two rules versions are "
                "in play; fix the feature build or the provider, because averaging them "
                "would report a ranking nobody computed."
            )
    hits = hits.drop(overlapping) if overlapping else hits
    join_columns = [name for name in hits.columns if name != COL_FOLD]
    if len(join_columns) == 2:
        # Nothing to attach: the provider's whole output was columns the frame already
        # carries and matched. Returning the frame keeps the seam idempotent.
        return frame
    joined = frame.join(
        hits.select(join_columns).unique(subset=[COL_ACCOUNT_KEY, COL_AS_OF_TS]),
        on=[COL_ACCOUNT_KEY, COL_AS_OF_TS],
        how="left",
        coalesce=True,
    )
    if RULE_COLUMN_SEVERITY_SUM not in joined.columns:
        severity_columns = [
            name for name in joined.columns if name.startswith("rule_r") and name.endswith("severity")
        ]
        if not severity_columns:
            raise FrameContractViolationError(
                "no per-rule severity columns and no rule_severity_sum on the frame or the "
                "provider, so the rules-only baseline (model.yaml "
                "baselines.rules_only.score = sum_of_rule_severity) has nothing to sum"
            )
        joined = joined.with_columns(
            pl.sum_horizontal([pl.col(name).cast(pl.Float64).fill_null(0.0) for name in severity_columns])
            .alias(RULE_COLUMN_SEVERITY_SUM)
        )
    # Only the columns this join *added* are checked for unmatched rows: the frame's own
    # rule features can legitimately be null (the feature layer misses values by policy,
    # and the scorecard bins missing as its own category), so a null in a column the frame
    # already had is not evidence of a failed join.
    unmatched = 0
    for name in join_columns:
        if name in (COL_ACCOUNT_KEY, COL_AS_OF_TS) or name not in joined.columns:
            continue
        unmatched += joined.get_column(name).null_count()
    if unmatched:
        raise FrameContractViolationError(
            f"{unmatched} scored row(s) have no rule hit row. A missing hit is not a zero "
            "hit: it is a join failure, and 03 A rule 2 forbids turning it into a zero. "
            "The usual cause is a provider that rebuilds the key columns from Python lists "
            "instead of selecting them from the frame it was handed, which silently drops "
            "the timezone from as_of_ts and matches nothing."
        )
    return joined


def feature_matrix(
    frame: pl.DataFrame,
    feature_names: tuple[str, ...],
    categorical_features: tuple[str, ...],
) -> np.ndarray:
    """The model input matrix: float64, nulls as NaN, categories coded by P4a's encoder.

    One encoder, deliberately shared with :func:`oxbow.scoring.frame._stable_category_codes`
    rather than reimplemented here. The scorecard's frame builder codes a category to a
    deterministic FNV-1a value with no ordinal meaning; if the GBM coded the same string to
    something else, the model would be scored in a feature space it was never fitted in --
    and it would still produce a confident number. A private-symbol import is the cheaper
    wrong-looking choice of the two available, so it is made explicitly.

    A plain ``cast(pl.Float64)`` on a string category used to produce an all-NaN column (or
    raise), which would have meant the boosted trees silently ignored every categorical
    feature. Nulls stay NaN: LightGBM learns a default direction for them, which is the
    ``explicit_bin`` policy expressed in the model rather than a mean imputation.
    """
    categorical = set(categorical_features)
    columns: list[np.ndarray] = []
    for name in feature_names:
        if name not in frame.columns:
            raise FrameContractViolationError(f"feature {name!r} is absent from the frame handed to the model")
        column = frame.get_column(name)
        if name in categorical:
            columns.append(_stable_category_codes(column).astype(np.float64))
        else:
            casted = column.cast(pl.Float64, strict=False) if column.dtype != pl.Float64 else column
            columns.append(
                casted.fill_null(np.nan).to_numpy(zero_copy_only=False).astype(np.float64)
            )
    if not columns:
        raise FrameContractViolationError("a model needs at least one feature column")
    matrix = np.column_stack(columns)
    if matrix.shape[0] != frame.height:
        raise FrameContractViolationError(
            f"the feature matrix has {matrix.shape[0]} rows for a {frame.height}-row frame"
        )
    return np.ascontiguousarray(matrix, dtype=np.float64)


def fusion_matrix(frame: pl.DataFrame, inputs: tuple[str, ...]) -> np.ndarray:
    """The fusion design matrix, in the declared input order.

    ``p_scorecard`` and ``p_gbm`` are read as calibrated-when-available columns; the
    order comes from config so the printed coefficients line up with the names a
    reader sees on the validation page.
    """
    missing = sorted(set(inputs) - set(frame.columns))
    if missing:
        raise FusionInputsError(
            f"fusion inputs {missing} are not on the scored frame; model.yaml declares "
            f"{list(inputs)}"
        )
    matrix = np.column_stack(
        [frame.get_column(name).cast(pl.Float64).fill_null(0.0).to_numpy() for name in inputs]
    )
    if not np.all(np.isfinite(matrix)):
        raise FusionInputsError("fusion input matrix carries a non-finite value")
    return matrix


def require_frame_hash(recorded_hash: str, frame: TrainingFrame) -> None:
    """Refuse to train or score on a frame whose spec hash is not the recorded one."""
    require_feature_hash_match(recorded_hash, frame.feature_spec_hash)


__all__ = [
    "FUSION_INPUTS",
    "REQUIRED_META_COLUMNS",
    "RULE_COLUMN_CYCLE_FLAG",
    "RULE_COLUMN_FAN_FLAG",
    "RULE_COLUMN_HIT_COUNT",
    "RULE_COLUMN_SEVERITY_MAX",
    "RULE_COLUMN_SEVERITY_SUM",
    "SCORED_FRAME_COLUMNS",
    "RuleHitProvider",
    "attach_rule_hits",
    "feature_matrix",
    "fusion_matrix",
    "require_frame_hash",
]
