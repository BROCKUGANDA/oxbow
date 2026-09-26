"""OXBOW scoring layer: the auditable WOE scorecard (plan §10, phase P4a).

The scorecard and the GBM ship as a pair and disagree in public; the disagreement
is the product (DEV-001). This package owns the transparent half: monotonic optimal
binning, WOE and IV, IV-based feature admission with refusals recorded in the
artefact, PDO scaling to whole-number points that sum exactly to the score on every
row, bands cut on observed validation bad rates, adverse-action reason codes, and
the PSI/CSI drift layer whose action degrades scoring rather than continuing
quietly.

Nothing here learns from a label after the fit fold, and nothing here calls a
language model: a score a human must defend is arithmetic on a frozen table.

See ``config/scorecard.yaml`` for every tunable this layer reads.
"""

from __future__ import annotations

from oxbow.scoring.bands import BandRow, BandTable, fit_bands
from oxbow.scoring.binning import BinRow, FeatureBinning, assign_bins, fit_feature_binning
from oxbow.scoring.config import FeatureRegistry, ScorecardConfig, load_scorecard_config
from oxbow.scoring.drift import (
    DriftReport,
    FeatureDrift,
    MigrationMatrix,
    drift_decision,
    per_feature_drift,
    rating_migration,
    score_psi,
)
from oxbow.scoring.errors import (
    FeatureSpecHashMismatch,
    FrameContractError,
    PointsInvariantError,
    ScorecardFitError,
    SeparationDetectedError,
    WoeNotFiniteError,
)
from oxbow.scoring.frame import (
    PROVENANCE_GENERATED,
    PROVENANCE_REAL,
    TrainingFrame,
    build_training_frame,
    require_feature_hash_match,
)
from oxbow.scoring.generated import default_spec, generate_training_frame
from oxbow.scoring.model import (
    ScorecardModel,
    fit_scorecard,
    score_frame,
    verify_points_identity,
)
from oxbow.scoring.reasons import ReasonCode, top_reason_codes
from oxbow.scoring.selection import SelectionOutcome, select_features

__all__ = [
    "PROVENANCE_GENERATED",
    "PROVENANCE_REAL",
    "BandRow",
    "BandTable",
    "BinRow",
    "DriftReport",
    "FeatureBinning",
    "FeatureDrift",
    "FeatureRegistry",
    "FeatureSpecHashMismatch",
    "FrameContractError",
    "MigrationMatrix",
    "PointsInvariantError",
    "ReasonCode",
    "ScorecardConfig",
    "ScorecardFitError",
    "ScorecardModel",
    "SelectionOutcome",
    "SeparationDetectedError",
    "TrainingFrame",
    "WoeNotFiniteError",
    "assign_bins",
    "build_training_frame",
    "default_spec",
    "drift_decision",
    "fit_bands",
    "fit_feature_binning",
    "fit_scorecard",
    "generate_training_frame",
    "load_scorecard_config",
    "per_feature_drift",
    "rating_migration",
    "require_feature_hash_match",
    "score_frame",
    "score_psi",
    "select_features",
    "top_reason_codes",
    "verify_points_identity",
]
