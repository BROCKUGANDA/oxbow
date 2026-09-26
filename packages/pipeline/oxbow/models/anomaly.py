"""Isolation Forest, normalised by validation percentile rank.

Plan §10 gives the reason this channel exists at all: real financial crime is
under-reported, so a purely supervised model learns only what a labeller already found.
An unsupervised score keeps the product honest about label scarcity, and it earns its
place in the fusion by describing behaviour that is *unusual* whether or not anyone
labelled it.

Two rules shape the implementation:

* **Fitted on the train feature matrix only.** "Unusual" must mean unusual relative to
  what the model was allowed to see. Fitting on the whole corpus would make the
  evaluation rows part of the definition of normal, which is leakage by another name.
* **Normalised by validation percentile rank, never by a min-max on the scored data.**
  sklearn's ``score_samples`` returns a raw anomaly measure whose absolute scale is
  meaningless (and inverted: lower is more anomalous). A min-max fitted on the rows
  being scored would make the number depend on who else was scored that day, and the
  fusion would then be weighting a quantity that moves for reasons unrelated to the
  account. The validation distribution is fixed at fit time and frozen into the bundle,
  so the same account gets the same anomaly score in every run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import polars as pl
from sklearn.ensemble import IsolationForest

from oxbow.models.config import AnomalyConfig
from oxbow.models.errors import ModelLayerError
from oxbow.scoring.frame import COL_LABEL

REFERENCE_TIE_FLOOR: Final = 1e-9


@dataclass(frozen=True, slots=True)
class AnomalyBundle:
    """The fitted forest plus the frozen validation reference distribution."""

    forest: IsolationForest
    feature_names: tuple[str, ...]
    reference_raw_scores: np.ndarray
    reference_percentiles: np.ndarray
    normalisation: str
    fit_rows: int
    train_positive_share: float
    missing_sentinels: np.ndarray

    def raw_scores(self, matrix: np.ndarray) -> np.ndarray:
        """sklearn's anomaly measure: lower means more anomalous."""
        return np.asarray(self.forest.score_samples(matrix), dtype=np.float64)

    def normalise(self, matrix: np.ndarray) -> np.ndarray:
        """Percentile rank of each row's raw score inside the frozen reference.

        Interpolating the empirical CDF rather than counting matches keeps the result
        continuous on a small reference sample, and the direction is set so that
        **0-1 with 1 = most unusual**: the fusion input is a risk channel, and a sign
        flip buried in a helper is exactly the kind of thing that makes a printed
        coefficient uninterpretable.
        """
        if matrix.shape[1] != len(self.feature_names):
            raise ModelLayerError(
                f"anomaly expects {len(self.feature_names)} columns, got shape {matrix.shape}"
            )
        raw = self.raw_scores(matrix)
        # Ascending raw score means *less* anomalous, so the percentile is inverted.
        return _percentile_of(raw, self.reference_raw_scores, self.reference_percentiles)

    def to_dict(self) -> dict[str, object]:
        """The forest's identity for the model card: what it saw and how it normalised.

        The reference distribution's size and range are included because they are the
        facts a reviewer needs to judge the normalisation: a percentile rank against 40
        validation rows is a coarse instrument, and the artefact should say so rather
        than let a 0.94 read as precision.
        """
        return {
            "normalisation": self.normalisation,
            "fit_rows": self.fit_rows,
            "feature_count": len(self.feature_names),
            "reference_rows": int(self.reference_raw_scores.size),
            "reference_raw_lower": float(np.min(self.reference_raw_scores)),
            "reference_raw_upper": float(np.max(self.reference_raw_scores)),
            "train_positive_share": round(self.train_positive_share, 8),
            "note": (
                "fitted on train features only; the percentile reference is the "
                "validation slice's own raw scores, frozen at fit time"
            ),
        }


def _percentile_of(
    values: np.ndarray, reference_sorted: np.ndarray, reference_percentiles: np.ndarray
) -> np.ndarray:
    """Empirical-CDF percentile of ``values`` in the frozen reference."""
    positions = np.searchsorted(reference_sorted, values, side="left")
    low = np.clip(positions - 1, 0, reference_percentiles.size - 1)
    high = np.clip(positions, 0, reference_percentiles.size - 1)
    span = reference_sorted[high] - reference_sorted[low]
    weight = np.where(
        span > REFERENCE_TIE_FLOOR,
        (values - reference_sorted[low]) / np.maximum(span, REFERENCE_TIE_FLOOR),
        1.0,
    )
    interpolated = reference_percentiles[low] + weight * (
        reference_percentiles[high] - reference_percentiles[low]
    )
    return np.clip(1.0 - interpolated, 0.0, 1.0)


def _fit_sentinels(matrix: np.ndarray) -> np.ndarray:
    """One sentinel per column: below the finite minimum by a fraction of its spread."""
    sentinels = np.zeros(matrix.shape[1], dtype=np.float64)
    for index in range(matrix.shape[1]):
        finite = matrix[:, index][np.isfinite(matrix[:, index])]
        if finite.size == 0:
            # A column with no finite training value at all: 0.0 is the only defensible
            # placement, and every row of it is missing anyway.
            sentinels[index] = 0.0
            continue
        low = float(np.min(finite))
        high = float(np.max(finite))
        span = high - low
        sentinels[index] = low - (0.25 * span if span > 0.0 else 1.0)
    return sentinels


def fit_anomaly(
    train_frame: pl.DataFrame,
    valid_frame: pl.DataFrame,
    feature_names: tuple[str, ...],
    categorical_features: tuple[str, ...],
    cfg: AnomalyConfig,
) -> AnomalyBundle:
    """Fit the forest on train rows and freeze the validation reference distribution."""
    if cfg.normalisation != "validation_percentile_rank":
        raise ModelLayerError(
            f"anomaly.normalisation={cfg.normalisation!r} is not implemented on purpose: the "
            "plan forbids a min-max fitted on the data being scored"
        )
    x_train, sentinels = _matrix(train_frame, feature_names, categorical_features)
    x_valid, _ = _matrix(valid_frame, feature_names, categorical_features, sentinels)
    contamination = "auto" if cfg.contamination == "auto" else float(cfg.contamination)
    max_samples: int | float | str = "auto" if cfg.max_samples == "auto" else float(cfg.max_samples)
    forest = IsolationForest(
        n_estimators=cfg.n_estimators,
        contamination=contamination,
        max_samples=max_samples,
        random_state=cfg.random_state,
        n_jobs=1,
    )
    forest.fit(x_train)
    reference = np.sort(forest.score_samples(x_valid).astype(np.float64))
    if reference.size < 2:
        raise ModelLayerError(
            f"the validation slice has {reference.size} rows; percentile ranks need a "
            "distribution to rank inside, so the normalisation cannot be fitted"
        )
    percentiles = (np.arange(reference.size, dtype=np.float64) + 0.5) / reference.size
    labels = train_frame.get_column(COL_LABEL).cast(pl.Float64).to_numpy()
    return AnomalyBundle(
        forest=forest,
        feature_names=feature_names,
        reference_raw_scores=reference,
        reference_percentiles=percentiles,
        normalisation=cfg.normalisation,
        fit_rows=int(x_train.shape[0]),
        train_positive_share=float(np.mean(labels)),
        missing_sentinels=sentinels,
    )


def apply_anomaly(
    bundle: AnomalyBundle, frame: pl.DataFrame, categorical_features: tuple[str, ...]
) -> np.ndarray:
    """``anomaly_norm`` for every row of a frame, 1 meaning most unusual.

    The missing sentinels come from the bundle, never recomputed here: rescoring the same
    row twice must give the same number, and a sentinel re-derived from the rows being
    scored would make the population decide where "missing" sits.
    """
    matrix, _ = _matrix(frame, bundle.feature_names, categorical_features, bundle.missing_sentinels)
    return bundle.normalise(matrix)


def _matrix(
    frame: pl.DataFrame,
    feature_names: tuple[str, ...],
    categorical_features: tuple[str, ...],
    sentinels: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Feature matrix as float64, with nulls mapped to a frozen per-column sentinel.

    IsolationForest cannot take NaN (unlike LightGBM, which learns a default direction),
    and the declared missingness policy in config/features.yaml is ``explicit_bin`` --
    missing is its own category, never a mean. So nulls are placed below the observed
    range of the *training* column, which makes a missing value a distinct region of
    feature space rather than an average account. The sentinel is computed once on train
    and stored on the bundle, because a sentinel derived from the rows being scored would
    let the scored population decide where "missing" sits. Categories are coded by the
    encoder the scorecard frame uses -- see :func:`oxbow.models.inputs.feature_matrix`.
    """
    from oxbow.models.inputs import feature_matrix

    matrix = feature_matrix(frame, feature_names, categorical_features)
    if sentinels is None:
        sentinels = _fit_sentinels(matrix)
    elif sentinels.shape[0] != matrix.shape[1]:
        raise ModelLayerError(
            f"the frozen missing sentinels describe {sentinels.shape[0]} columns and the "
            f"frame has {matrix.shape[1]}: the feature set changed after the forest was fitted"
        )
    filled = matrix.copy()
    missing = ~np.isfinite(filled)
    if missing.any():
        filled[missing] = np.take(sentinels, np.flatnonzero(missing) % matrix.shape[1])
    return filled, sentinels


__all__ = ["AnomalyBundle", "apply_anomaly", "fit_anomaly"]
