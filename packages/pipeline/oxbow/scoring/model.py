"""The scorecard artefact: fit it, score with it, publish the arithmetic.

This is the module that produces the thing a human signs off. The fit may only see
train and validation rows -- bins, WOE tables and coefficients are fitted on
train+validation and bands on validation only (plan §8: scalers, bins, WOE tables,
calibrators and thresholds fit on train+validation only) -- and the artefact then
carries everything needed to recompute any row's score by hand: every bin's
population and class counts, its WOE and IV, the admission decision for every
declared feature including the refusals, the scaling constants, the points table,
the band table with observed rates, and the guard log recording which bins were
merged or smoothed.

Two invariants are enforced in code rather than in prose:

* **Feature-spec hash.** Scoring a frame whose hash differs from the one recorded at
  training raises (plan §8, 02 B seam 3).
* **The points add up.** ``score_points`` is *defined* as base points plus the sum of
  the per-attribute points, so the identity holds on every row by construction.
  :func:`verify_points_identity` re-derives it from the emitted per-row JSON -- the
  same artefact a UI would render, so a bug in the writer cannot hide a bug in the
  number -- and ``score_frame`` calls it on every row it produces.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

import numpy as np
import polars as pl

from oxbow.scoring.bands import BandRow, BandTable, assign_bands, fit_bands
from oxbow.scoring.binning import (
    KIND_MISSING,
    KIND_UNSEEN,
    KIND_ZERO,
    BinRow,
    FeatureBinning,
    assign_bins,
    fit_feature_binning,
)
from oxbow.scoring.config import FeatureRegistry, ScorecardConfig
from oxbow.scoring.errors import PointsInvariantError, ScorecardFitError, SeparationDetectedError
from oxbow.scoring.frame import (
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    COL_LABEL,
    COL_ROLE,
    ROLE_TRAIN,
    ROLE_VALIDATION,
    TrainingFrame,
    require_feature_hash_match,
    utc_now_iso,
)
from oxbow.scoring.reasons import top_reason_codes
from oxbow.scoring.scale import (
    SEPARATION_TOLERANCE,
    ScalingConstants,
    WoeLogisticFit,
    additive_scores,
    fit_woe_logistic,
    univariate_auc,
)
from oxbow.scoring.selection import AdmissionDecision, SelectionOutcome, select_features

VOLATILE_ARTIFACT_KEYS: Final = ("trained_at", "run_id", "model_version")

SCORED_COLUMNS: Final = (
    "score_points",
    "score_continuous",
    "band",
    "p_scorecard_uncalibrated",
    "points_json",
    "reason_codes",
    "features_in_missing_bin",
    "features_in_zero_bin",
    "features_in_unseen_bin",
)


@dataclass(frozen=True, slots=True)
class GuardLog:
    """The bins that needed help, and what was done for each."""

    bins_with_zero_bads: int
    bins_with_zero_goods: int
    merges_recorded: int
    smoothing_recorded: int
    zero_bads_unexplained: tuple[str, ...]
    binning_failures: tuple[str, ...]
    infinite_woe_features: tuple[str, ...]
    separations_found: tuple[str, ...]
    separation_auc_by_feature: tuple[tuple[str, float], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "bins_with_zero_bads": self.bins_with_zero_bads,
            "bins_with_zero_goods": self.bins_with_zero_goods,
            "merges_recorded": self.merges_recorded,
            "smoothing_recorded": self.smoothing_recorded,
            "zero_bads_unexplained": list(self.zero_bads_unexplained),
            "binning_failures": list(self.binning_failures),
            "infinite_woe_features": list(self.infinite_woe_features),
            "separations_found": list(self.separations_found),
            "separation_auc_by_feature": {
                feature: round(auc, 8) for feature, auc in self.separation_auc_by_feature
            },
            "gate_clause": (
                "no bin has zero bads without a recorded merge or smoothing rule: "
                f"{len(self.zero_bads_unexplained)} unexplained"
            ),
        }


@dataclass(frozen=True, slots=True)
class ScorecardModel:
    """The fitted scorecard, its refusals, and the numbers it was fitted on."""

    cfg: ScorecardConfig
    registry: FeatureRegistry
    feature_spec_hash: str
    binnings: dict[str, FeatureBinning]
    selection: SelectionOutcome
    fit: WoeLogisticFit
    bands: BandTable
    provenance: str
    fit_population: int
    fit_positive_rate: float
    validation_population: int
    mean_uncalibrated_probability: float
    guard_log: GuardLog
    points_deviation_max: float
    points_deviation_mean: float
    additive_vs_continuous_spearman: float
    notes: tuple[str, ...] = ()
    trained_at: str = field(default_factory=utc_now_iso)
    artifact_sha256: str = ""

    @property
    def admitted_features(self) -> tuple[str, ...]:
        return self.fit.features

    def attribute_label(self, feature: str) -> str:
        return self.registry.attribute_labels.get(feature, feature)

    def bin_kind(self, feature: str, label: str) -> str:
        return self.binnings[feature].row_for_label(label).kind

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact": "oxbow-scorecard-v1",
            "feature_spec_hash": self.feature_spec_hash,
            "provenance": self.provenance,
            "trained_at": self.trained_at,
            "artifact_sha256": self.artifact_sha256,
            "declared_feature_count": len(self.registry.names),
            "admitted_feature_count": len(self.fit.features),
            "fit_population": self.fit_population,
            "fit_positive_rate": round(self.fit_positive_rate, 8),
            "validation_population": self.validation_population,
            "mean_uncalibrated_probability": round(self.mean_uncalibrated_probability, 8),
            "scaling": self.fit.scaling.to_dict(),
            "coefficients": {
                "features": list(self.fit.features),
                "values": [round(value, 8) for value in self.fit.coefficients],
                "intercept": round(self.fit.intercept, 8),
                "converged": self.fit.converged,
                "iterations": self.fit.iterations,
                "regularisation": {
                    "type": self.cfg.fit.regularisation,
                    "strength": self.cfg.fit.regularisation_strength,
                },
            },
            "binning": {
                name: binning.to_dict() for name, binning in sorted(self.binnings.items())
            },
            "selection": self.selection.to_dict(),
            "band_table": self.bands.to_dict(),
            "guard_log": self.guard_log.to_dict(),
            "points_invariant": {
                "definition": "score_points = base_points + sum(points over admitted attributes)",
                "max_deviation_from_continuous_score": round(self.points_deviation_max, 6),
                "mean_deviation_from_continuous_score": round(self.points_deviation_mean, 6),
                "spearman_additive_vs_continuous": round(self.additive_vs_continuous_spearman, 8),
                "round_points_to_integer": self.cfg.fit.round_points_to_integer,
            },
            "admission_rule_visible_in_ui": {
                "iv_min": self.cfg.admission.iv_min,
                "iv_max": self.cfg.admission.iv_max,
                "above_max_action": self.cfg.admission.above_max_action,
                "show_rule_in_ui": self.cfg.admission.show_rule_in_ui,
                "suspected_leakage_refused": sorted(
                    decision.feature
                    for decision in self.selection.decisions
                    if decision.decision == "refused_suspected_leakage"
                ),
                "suspected_leakage_admitted_by_justification": sorted(
                    decision.feature
                    for decision in self.selection.decisions
                    if decision.decision == "admitted_with_written_justification"
                ),
            },
            "notes": list(self.notes),
            "bin_floors": {
                "min_bin_pct": self.cfg.binning.min_bin_pct,
                "min_bin_count": self.cfg.binning.min_bin_count,
                "max_bins": self.cfg.binning.max_bins,
                "enforce_monotonic_trend": self.cfg.binning.enforce_monotonic_trend,
                "smoothing": self.cfg.binning.smoothing,
                "laplace_alpha": self.cfg.binning.laplace_alpha,
                "special_bins": list(self.cfg.binning.special_bin_labels),
            },
        }

    def canonical_json(self, include_volatile: bool = False) -> str:
        """Canonical serialisation, with the wall clock excluded by default.

        The artifact hash covers the model's substance, not the moment it was
        written; otherwise ``make verify-determinism`` would be comparing timestamps
        and calling every re-run a change.
        """
        payload = self.to_dict()
        if not include_volatile:
            payload = {
                key: value for key, value in payload.items() if key not in VOLATILE_ARTIFACT_KEYS
            }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    def artifact_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def write_json(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return str(path.resolve())


def _column_values(frame: pl.DataFrame, feature: str, dtype: str) -> np.ndarray:
    """One feature column as an array, nulls preserved as NaN (numeric) or None."""
    column = frame.get_column(feature)
    if dtype == "categorical":
        return np.array(column.to_list(), dtype=object)
    return column.cast(pl.Float64).fill_null(np.nan).to_numpy(zero_copy_only=False).astype(
        np.float64
    )


def _scan_separation(
    woe_columns: Mapping[str, np.ndarray],
    labels: np.ndarray,
    cfg: ScorecardConfig,
) -> tuple[tuple[str, ...], tuple[tuple[str, float], ...]]:
    """Features whose WOE column alone ranks every positive above every negative.

    Scanned before IV admission rather than after, because separation is a property
    of the data while the IV ceiling is a policy: a separating feature would
    otherwise be refused as suspected leakage and the run would never state the
    sharper truth -- that the label is reachable through it. ``fit.separation_action``
    decides the response; the default fails loudly and names the feature
    (03 H: ``test_separation_detected``).
    """
    aucs = tuple(
        (feature, univariate_auc(woe_columns[feature], labels)) for feature in sorted(woe_columns)
    )
    if not cfg.fit.detect_separation:
        return (), aucs
    by_feature = dict(aucs)
    offenders = tuple(
        sorted(
            feature
            for feature, auc in aucs
            if auc >= cfg.fit.separation_auc_threshold - SEPARATION_TOLERANCE
            or auc <= 1.0 - cfg.fit.separation_auc_threshold + SEPARATION_TOLERANCE
        )
    )
    if not offenders:
        return (), aucs
    if cfg.fit.separation_action == "exclude_and_record":
        return offenders, aucs
    if cfg.fit.separation_action != "fail_named":
        raise SeparationDetectedError(
            offenders,
            f"fit.separation_action={cfg.fit.separation_action!r} is not a recognised policy; "
            "expected 'fail_named' or 'exclude_and_record'",
        )
    detail = ", ".join(f"{feature} AUC={by_feature[feature]:.6f}" for feature in offenders)
    raise SeparationDetectedError(
        offenders,
        (
            f"{detail}. The univariate AUC sits on fit.separation_auc_threshold="
            f"{cfg.fit.separation_auc_threshold}, meaning that column alone orders every "
            "confirmed fraud above every legitimate account. Regularisation is on, but a "
            "regularised coefficient on a separating feature is an arbitrary number dressed "
            "as evidence, so the fit stops instead of shipping it."
        ),
    )


def fit_scorecard(
    frame: TrainingFrame,
    cfg: ScorecardConfig,
    registry: FeatureRegistry,
    justifications: Mapping[str, str] | None = None,
) -> ScorecardModel:
    """Fit the scorecard on train+validation rows only.

    The test fold is never read here. Scoring it later reads the frozen table, which
    is the only reason a walk-forward number means anything.
    """
    fit_rows = frame.data.filter(pl.col(COL_ROLE).is_in([ROLE_TRAIN, ROLE_VALIDATION])).sort(
        [COL_AS_OF_TS, COL_ACCOUNT_KEY]
    )
    if fit_rows.height == 0:
        raise ScorecardFitError(
            "no train or validation rows in the frame, so there is nothing to fit. Scoring "
            "with a model that was never fitted is not degradation, it is a fiction."
        )
    labels = fit_rows.get_column(COL_LABEL).cast(pl.Int32).to_numpy().astype(np.int32)
    if int(labels.sum()) == 0:
        raise ScorecardFitError(
            "the fit population has zero positives: WOE and IV are undefined, so any "
            "scorecard built on it would be reporting invented points"
        )

    categorical = set(cfg.binning.categorical_features)
    binnings: dict[str, FeatureBinning] = {}
    woe_columns: dict[str, np.ndarray] = {}
    binning_failures: list[str] = []
    for feature in registry.names:
        dtype = "categorical" if feature in categorical else "numerical"
        values = _column_values(fit_rows, feature, dtype)
        binning = fit_feature_binning(feature, values, labels, cfg.binning, dtype)
        binnings[feature] = binning
        table = {row.label: row.woe for row in binning.rows}
        observed = assign_bins(values, binning, cfg.binning)
        column = np.array([table[label] for label in observed], dtype=np.float64)
        if not bool(np.all(np.isfinite(column))):
            binning_failures.append(f"{feature}: non-finite WOE column after smoothing")
        woe_columns[feature] = column

    separations, aucs = _scan_separation(woe_columns, labels, cfg)
    excluded = set(separations) if cfg.fit.separation_action == "exclude_and_record" else set()
    eligible = {name: binning for name, binning in binnings.items() if name not in excluded}
    selection = select_features(
        eligible,
        woe_columns,
        labels,
        cfg.admission,
        registry.guards.max_abs_correlation_with_label,
        justifications,
    )
    if not selection.admitted:
        raise ScorecardFitError(
            "IV admission left no feature, so no scorecard exists. First refusals: "
            + "; ".join(
                f"{decision.feature}: {decision.decision}" for decision in selection.decisions[:5]
            )
        )

    matrix = np.column_stack([woe_columns[feature] for feature in selection.admitted])
    bin_woe = {
        feature: {row.label: row.woe for row in binnings[feature].rows}
        for feature in selection.admitted
    }
    logistic = fit_woe_logistic(
        features=selection.admitted,
        woe_matrix=matrix,
        labels=labels,
        bin_woe=bin_woe,
        scaling_cfg=cfg.scaling,
        fit_cfg=cfg.fit,
    )

    row_labels = _row_label_maps(logistic.features, binnings, fit_rows, cfg)
    scores, points_matrix, p_bad, deviation = additive_scores(logistic, row_labels)

    # Positions come from the frame's own role column, never from account_key: the
    # same account appears in several folds, so a key cannot identify a row.
    validation_positions = np.flatnonzero(
        (fit_rows.get_column(COL_ROLE) == ROLE_VALIDATION).to_numpy()
    )
    if validation_positions.size == 0:
        raise ScorecardFitError(
            "the fit population has no validation rows, so bands cannot be cut by observed "
            "bad rate (plan §10). The role assignment is wrong, not the corpus."
        )
    bands, _ = fit_bands(scores[validation_positions], labels[validation_positions], cfg.bands)

    continuous = logistic.scaling.offset + logistic.scaling.factor * logistic.log_odds_good(matrix)
    zero_bads_unexplained = tuple(
        f"{name}:{row.label}"
        for name, binning in sorted(binnings.items())
        for row in binning.rows
        if row.n_bad == 0
        and row.population > 0
        and not row.merge_applied
        and not row.smoothing_applied
    )
    guard_log = GuardLog(
        bins_with_zero_bads=sum(
            1 for binning in binnings.values() for row in binning.rows if row.n_bad == 0
        ),
        bins_with_zero_goods=sum(
            1 for binning in binnings.values() for row in binning.rows if row.n_good == 0
        ),
        merges_recorded=sum(binning.merges_recorded for binning in binnings.values()),
        smoothing_recorded=sum(
            1 for binning in binnings.values() for row in binning.rows if row.smoothing_applied
        ),
        zero_bads_unexplained=zero_bads_unexplained,
        binning_failures=tuple(binning_failures),
        infinite_woe_features=tuple(
            feature
            for feature, column in woe_columns.items()
            if not bool(np.all(np.isfinite(column)))
        ),
        separations_found=separations,
        separation_auc_by_feature=aucs,
    )
    if points_matrix.shape[0] != scores.shape[0]:
        raise PointsInvariantError("the points matrix and the score vector disagree on length")

    notes = (
        f"bins and coefficients fitted on {fit_rows.height} train+validation rows; bands cut "
        f"on {validation_positions.size} validation rows; the test fold was not read",
        f"{len(logistic.features)} admitted attributes of {len(registry.names)} declared features",
        f"max integer-rounding deviation {float(deviation.max()):.3f} points, mean "
        f"{float(deviation.mean()):.3f}",
    )
    built = ScorecardModel(
        cfg=cfg,
        registry=registry,
        feature_spec_hash=frame.feature_spec_hash,
        binnings=binnings,
        selection=selection,
        fit=logistic,
        bands=bands,
        provenance=frame.provenance,
        fit_population=fit_rows.height,
        fit_positive_rate=float(labels.sum()) / float(labels.size),
        validation_population=int(validation_positions.size),
        mean_uncalibrated_probability=float(np.mean(p_bad)),
        guard_log=guard_log,
        points_deviation_max=float(deviation.max()),
        points_deviation_mean=float(deviation.mean()),
        additive_vs_continuous_spearman=float(_spearman(scores.astype(np.float64), continuous)),
        notes=notes,
    )
    return replace(built, artifact_sha256=built.artifact_hash())


def _spearman(left: np.ndarray, right: np.ndarray) -> float:
    """Rank correlation between the additive and the continuous score.

    Reported because the additive score is a rounding of the continuous one. If
    rounding ever reordered accounts materially, the auditable column would be a worse
    ranking than the model's own, and that has to be visible rather than assumed away.
    """
    from scipy.stats import spearmanr

    if left.size < 2 or float(np.std(left)) == 0.0 or float(np.std(right)) == 0.0:
        return 1.0
    return float(spearmanr(left, right).statistic)


def _row_label_maps(
    features: tuple[str, ...],
    binnings: Mapping[str, FeatureBinning],
    frame: pl.DataFrame,
    cfg: ScorecardConfig,
) -> list[dict[str, str]]:
    """Bin label per admitted feature for every row, in the frame's row order."""
    per_feature = {
        feature: assign_bins(
            _column_values(frame, feature, binnings[feature].dtype), binnings[feature], cfg.binning
        )
        for feature in features
    }
    return [
        {feature: per_feature[feature][position] for feature in features}
        for position in range(frame.height)
    ]


def score_frame(frame: TrainingFrame, model: ScorecardModel, cfg: ScorecardConfig) -> pl.DataFrame:
    """Score every row of a frame with the frozen scorecard.

    The frame's feature-spec hash is checked first and a mismatch raises: this is the
    seam that stops a model trained on one feature set from scoring another.
    """
    require_feature_hash_match(model.feature_spec_hash, frame.feature_spec_hash)
    data = frame.data.sort([COL_AS_OF_TS, COL_ACCOUNT_KEY])
    features = model.fit.features
    row_labels = _row_label_maps(features, model.binnings, data, cfg)
    scores, points_matrix, p_bad, deviation = additive_scores(model.fit, row_labels)
    bands = assign_bands(
        scores, {row.band_id: row.min_points for row in model.bands.rows}, cfg.bands.band_ids
    )

    points_payload: list[str] = []
    reason_payload: list[list[str]] = []
    missing_flags: list[list[str]] = []
    zero_flags: list[list[str]] = []
    unseen_flags: list[list[str]] = []
    for position, labels in enumerate(row_labels):
        contributions = [
            {
                "attribute": model.attribute_label(feature),
                "feature": feature,
                "bin_label": labels[feature],
                "points": int(points_matrix[position, index]),
                "woe": round(model.fit.bin_woe[feature][labels[feature]], 6),
                "bin_kind": model.bin_kind(feature, labels[feature]),
            }
            for index, feature in enumerate(features)
        ]
        contributions.sort(key=lambda item: (-abs(item["points"]), item["feature"]))
        points_payload.append(json.dumps(contributions, sort_keys=True, separators=(",", ":")))
        codes = top_reason_codes(
            {item["feature"]: item["points"] for item in contributions},
            labels,
            {item["feature"]: item["bin_kind"] for item in contributions},
            model.registry.attribute_labels,
            cfg.reasons,
        )
        reason_payload.append([code.text for code in codes])
        missing_flags.append(
            sorted(item["feature"] for item in contributions if item["bin_kind"] == KIND_MISSING)
        )
        zero_flags.append(
            sorted(item["feature"] for item in contributions if item["bin_kind"] == KIND_ZERO)
        )
        unseen_flags.append(
            sorted(item["feature"] for item in contributions if item["bin_kind"] == KIND_UNSEEN)
        )

    scored = data.with_columns(
        pl.Series("score_points", scores.astype(np.int64)),
        pl.Series(
            "score_continuous",
            model.fit.scaling.offset
            + model.fit.scaling.factor * model.fit.log_odds_good(model.fit.woe_matrix(row_labels)),
        ),
        pl.Series("band", list(bands), dtype=pl.String),
        pl.Series("p_scorecard_uncalibrated", p_bad.astype(np.float64)),
        pl.Series("points_json", points_payload),
        pl.Series("reason_codes", reason_payload, dtype=pl.List(pl.String)),
        pl.Series("features_in_missing_bin", missing_flags, dtype=pl.List(pl.String)),
        pl.Series("features_in_zero_bin", zero_flags, dtype=pl.List(pl.String)),
        pl.Series("features_in_unseen_bin", unseen_flags, dtype=pl.List(pl.String)),
        pl.Series("points_rounding_deviation", deviation.astype(np.float64)),
    )
    violations = verify_points_identity(scored, model.fit.scaling.base_points)
    if violations:
        raise PointsInvariantError(
            f"{len(violations)} row(s) where the per-attribute points do not sum to the score; "
            f"first failure {violations[0]}"
        )
    return scored


def verify_points_identity(scored: pl.DataFrame, base_points: int) -> list[dict[str, object]]:
    """Re-derive every score from the per-attribute points it emitted.

    Returns one record per failing row; an empty list is the invariant holding on
    every row of the frame, which is the clause the plan names.
    """
    if scored.height == 0:
        return []
    scores = scored.get_column("score_points").to_numpy()
    violations: list[dict[str, object]] = []
    for position, raw in enumerate(scored.get_column("points_json").to_list()):
        contributions = json.loads(raw)
        total = base_points + sum(int(item["points"]) for item in contributions)
        if total != int(scores[position]):
            violations.append(
                {
                    "row": position,
                    "account_key": scored.get_column(COL_ACCOUNT_KEY)[position],
                    "sum_of_points": total,
                    "score_points": int(scores[position]),
                }
            )
    return violations


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise ScorecardFitError(f"expected a list of mappings, got {type(value).__name__}")
    return [item for item in value if isinstance(item, Mapping)]


def _scalar(item: Mapping[str, object], key: str) -> object:
    if key not in item:
        raise ScorecardFitError(f"artefact is missing key {key!r}")
    return item[key]


def band_table_from_dict(payload: Mapping[str, object]) -> BandTable:
    """Rebuild a band table from artefact JSON, for scoring without retraining."""
    rows = tuple(
        BandRow(
            band_id=str(_scalar(band, "id")),
            label=str(_scalar(band, "label")),
            action=str(_scalar(band, "action")),
            glyph=str(_scalar(band, "glyph")),
            min_points=None if band["min_points"] is None else int(band["min_points"]),
            max_points=None if band["max_points"] is None else int(band["max_points"]),
            population=int(band["population"]),
            population_share=float(band["population_share"]),
            observed_bad_rate=float(band["observed_bad_rate"]),
            observed_bad_count=int(band["observed_bad_count"]),
            rate_target_multiple=(
                None
                if band["rate_target_multiple"] is None
                else float(band["rate_target_multiple"])
            ),
            rate_target=None if band["rate_target"] is None else float(band["rate_target"]),
            band_absent_reason=(
                None if band["band_absent_reason"] is None else str(band["band_absent_reason"])
            ),
        )
        for band in _mapping_list(payload["bands"])
    )
    return BandTable(
        rows=rows,
        base_rate=float(_scalar(payload, "base_rate")),
        n_rows=int(_scalar(payload, "n_rows")),
        smoothed_rate_at_cut=tuple(
            float(value) for value in _scalar(payload, "smoothed_rate_at_cut")
        ),
        method=str(_scalar(payload, "method")),
    )


def binning_from_dict(payload: Mapping[str, object]) -> FeatureBinning:
    """Rebuild one feature's bin table from artefact JSON."""
    rows = tuple(
        BinRow(
            label=str(row["label"]),
            kind=str(row["kind"]),
            population=int(row["population"]),
            n_good=int(row["n_good"]),
            n_bad=int(row["n_bad"]),
            population_share=float(row["population_share"]),
            bad_rate=float(row["bad_rate"]),
            woe=float(row["woe"]),
            iv_contribution=float(row["iv_contribution"]),
            lower=None if row["lower"] is None else float(row["lower"]),
            upper=None if row["upper"] is None else float(row["upper"]),
            categories=tuple(str(item) for item in row["categories"]),
            merge_applied=tuple(str(item) for item in row["merge_applied"]),
            smoothing_applied=bool(row["smoothing_applied"]),
            smoothing_reason=str(row["smoothing_reason"]),
        )
        for row in _mapping_list(payload["bins"])
    )
    return FeatureBinning(
        feature=str(_scalar(payload, "feature")),
        dtype=str(_scalar(payload, "dtype")),
        rows=rows,
        iv=float(_scalar(payload, "iv")),
        boundary_source=str(_scalar(payload, "boundary_source")),
        monotonic_direction=str(_scalar(payload, "monotonic_direction")),
        missing_share=float(_scalar(payload, "missing_share")),
        structural_zero_share=float(_scalar(payload, "structural_zero_share")),
        unseen_tail_source=(
            None if payload["unseen_tail_source"] is None else str(payload["unseen_tail_source"])
        ),
        merges_recorded=int(_scalar(payload, "merges_recorded")),
        bins_with_zero_bads=int(_scalar(payload, "bins_with_zero_bads")),
        bins_with_zero_goods=int(_scalar(payload, "bins_with_zero_goods")),
        notes=tuple(str(item) for item in _scalar(payload, "notes")),
    )


def selection_from_dict(payload: Mapping[str, object]) -> SelectionOutcome:
    """Rebuild the admission decisions so the refusals survive a round trip."""
    decisions = tuple(
        AdmissionDecision(
            feature=str(item["feature"]),
            iv=float(item["iv"]),
            decision=str(item["decision"]),
            rule=str(item["rule"]),
            justification=None if item["justification"] is None else str(item["justification"]),
            abs_correlation_with_label=float(item["abs_correlation_with_label"]),
            bins=int(item["bins"]),
        )
        for item in _mapping_list(payload["decisions"])
    )
    admitted = _scalar(payload, "admitted")
    if not isinstance(admitted, list):
        raise ScorecardFitError("artefact 'admitted' must be a list")
    names: Sequence[str] = [str(name) for name in admitted]
    return SelectionOutcome(admitted=tuple(names), decisions=decisions)


def scaling_from_dict(payload: Mapping[str, object]) -> ScalingConstants:
    return ScalingConstants(
        pdo=float(_scalar(payload, "pdo")),
        base_score=float(_scalar(payload, "base_score")),
        base_odds=float(_scalar(payload, "base_odds")),
        factor=float(_scalar(payload, "factor")),
        offset=float(_scalar(payload, "offset")),
        base_points=int(_scalar(payload, "base_points")),
    )


__all__ = [
    "SCORED_COLUMNS",
    "VOLATILE_ARTIFACT_KEYS",
    "BandRow",
    "BandTable",
    "GuardLog",
    "ScorecardModel",
    "band_table_from_dict",
    "binning_from_dict",
    "fit_scorecard",
    "scaling_from_dict",
    "score_frame",
    "selection_from_dict",
    "verify_points_identity",
]
