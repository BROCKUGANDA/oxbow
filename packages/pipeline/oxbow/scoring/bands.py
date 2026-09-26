"""Bands A-E, cut by observed bad rate on validation.

Plan §10: bands are cut by **observed** bad rate, never by a round number, and each
band carries population share, observed rate and the action it implies. The cut
procedure therefore has to produce a table the artefact can defend:

1. Smooth the empirical bad rate against the score monotonically. Higher points mean
   a safer account (the PDO scale: the reason-code example gives risky bins *minus*
   points), so the fitted rate is non-increasing in the score and isotonic regression
   is the right smoother. Without it a sparse score reads as a 100 % bad rate and
   drags a boundary across the population.
2. Give every distinct observed score the band whose rate target its smoothed rate
   falls into. Because the smoother is non-increasing and the targets rise from A to
   E, the band sequence over ascending scores is E, D, C, B, A -- contiguous by
   construction, so two bands cannot overlap and a row cannot match two bands.
3. Read the **observed** rate of each band from the rows that actually landed in it,
   and merge any adjacent pair whose observed rates come out inverted. The reported
   table is then measured on the assignment and monotone by construction.

Step 3 is why ``test_band_table_matches_observed_validation_rates`` can be a test at
all: the table and a recomputation from the frame must agree, and the inversion
check is what stops a thin band -- three validation rows, one bad -- from being
published as a rating with a 33 % default rate.

A band with no population is reported with a named reason rather than deleted. An
empty band is a finding about the corpus, not a layout problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Final

import numpy as np
from sklearn.isotonic import IsotonicRegression

from oxbow.scoring.config import BandsConfig
from oxbow.scoring.errors import BandFitError

BAND_ORDER: Final = ("A", "B", "C", "D", "E")


@dataclass(frozen=True, slots=True)
class BandRow:
    """One band as it is reported: boundary, population, observed rate, action."""

    band_id: str
    label: str
    action: str
    glyph: str
    min_points: int | None
    max_points: int | None
    population: int
    population_share: float
    observed_bad_rate: float
    observed_bad_count: int
    rate_target_multiple: float | None
    rate_target: float | None
    band_absent_reason: str | None
    merged_from: tuple[str, ...]
    merge_reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.band_id,
            "label": self.label,
            "action": self.action,
            "glyph": self.glyph,
            "min_points": self.min_points,
            "max_points": self.max_points,
            "population": self.population,
            "population_share": round(self.population_share, 6),
            "observed_bad_rate": round(self.observed_bad_rate, 8),
            "observed_bad_count": self.observed_bad_count,
            "rate_target_multiple": self.rate_target_multiple,
            "rate_target": None if self.rate_target is None else round(self.rate_target, 8),
            "band_absent_reason": self.band_absent_reason,
            "merged_from": list(self.merged_from),
            "merge_reason": self.merge_reason,
        }


@dataclass(frozen=True, slots=True)
class BandTable:
    """The fitted band table plus the boundary curve the UI shows as the ramp."""

    rows: tuple[BandRow, ...]
    base_rate: float
    n_rows: int
    smoothed_rate_at_cut: tuple[float, ...]
    method: str

    def boundaries(self) -> dict[str, int | None]:
        return {row.band_id: row.min_points for row in self.rows}

    def observed_rates(self) -> dict[str, float]:
        return {row.band_id: row.observed_bad_rate for row in self.rows}

    def is_rate_monotone(self) -> bool:
        """Observed bad rate must not decrease as the band gets riskier.

        Checked here as well as in the tests so a regression fails the build rather
        than appearing as a plausible-looking table on the validation page.
        """
        rates = [row.observed_bad_rate for row in self.rows if row.population > 0]
        return all(left <= right + 1e-12 for left, right in pairwise(rates))

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "base_rate": round(self.base_rate, 8),
            "n_rows": self.n_rows,
            "smoothed_rate_at_cut": [
                None if np.isnan(value) else round(value, 8) for value in self.smoothed_rate_at_cut
            ],
            "observed_rate_monotone": self.is_rate_monotone(),
            "bands": [row.to_dict() for row in self.rows],
        }


def assign_bands(
    scores: np.ndarray, boundaries: dict[str, int | None], band_ids: tuple[str, ...]
) -> np.ndarray:
    """Map scores to band ids using each band's minimum points.

    A score equal to a boundary belongs to the safer band, so the comparison is
    ``>=`` and the ordering is total: the highest floor the score clears wins, and an
    absent band (floor ``None``) simply never matches.
    """
    out = np.empty(scores.size, dtype=object)
    ordered = list(band_ids)
    raw_cuts = [(band, boundaries.get(band)) for band in ordered]
    cuts: list[tuple[str, int]] = sorted(
        [(band, cut) for band, cut in raw_cuts if cut is not None],
        key=lambda item: -item[1],
    )
    fallback = ordered[-1]
    for position, score in enumerate(scores):
        assigned = fallback
        for band, cut in cuts:
            if int(score) >= cut:
                assigned = band
                break
        out[position] = assigned
    return out


def _observed_statistics(
    assigned: list[str],
    labels: np.ndarray,
    band_ids: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Population and bad count per band, from the rows actually assigned."""
    out: dict[str, tuple[int, int]] = {band: (0, 0) for band in band_ids}
    for band, label in zip(assigned, labels.tolist(), strict=True):
        population, bad = out[band]
        out[band] = (population + 1, bad + int(label))
    return out


def _merge_inverted_bands(
    assigned: list[str],
    labels: np.ndarray,
    cfg: BandsConfig,
) -> tuple[list[str], list[str]]:
    """Merge adjacent bands whose *observed* rates come out inverted.

    A thin validation slice can invert two neighbours purely by luck: three rows and
    one bad reads as a 33 % rate and sits next to a 900-row band reading 8 %. The plan
    says the band table is cut by observed rate, so a table that contradicts itself
    cannot be published. The smaller band merges into the larger, the reason is
    recorded on the surviving band, and the merge -- not the silent deletion -- is what
    the artefact shows.
    """
    order = list(cfg.band_ids)
    reasons: list[str] = []
    while True:
        stats = _observed_statistics(assigned, labels, tuple(order))
        rates: dict[str, float] = {
            band: (stats[band][1] / stats[band][0]) if stats[band][0] else 0.0 for band in order
        }
        populations = {band: stats[band][0] for band in order}
        inversion: tuple[str, str] | None = None
        for first, second in pairwise(order):
            if populations[first] == 0 or populations[second] == 0:
                continue
            if rates[first] > rates[second]:
                inversion = (first, second)
                break
        if inversion is None:
            return assigned, reasons
        first, second = inversion
        absorb_into = first if populations[first] >= populations[second] else second
        victim = second if absorb_into == first else first
        assigned = [absorb_into if band == victim else band for band in assigned]
        reasons.append(
            f"{victim} merged into {absorb_into}: observed bad rate "
            f"{'inverted' if rates[first] > rates[second] else 'non-monotone'} "
            f"({rates[first]:.6f} in {first} vs {rates[second]:.6f} in {second} across "
            f"{populations[first]} and {populations[second]} rows)"
        )
    # unreachable: the loop returns when no inversion remains


def fit_bands(
    scores: np.ndarray, labels: np.ndarray, cfg: BandsConfig
) -> tuple[BandTable, np.ndarray]:
    """Fit the band boundaries on validation and assign every validation row.

    Raises :class:`BandFitError` when the validation slice has no positives: there is
    then no observed rate to cut on, and inventing boundaries would be exactly the
    "round number" the plan forbids.
    """
    if scores.size != labels.size:
        raise BandFitError("band fitting needs one label per score")
    if scores.size == 0:
        raise BandFitError("band fitting was given an empty validation slice")
    n_bad = int(labels.sum())
    if n_bad == 0:
        raise BandFitError(
            "no positives in the validation slice: bands are cut by OBSERVED bad rate "
            "(plan §10), so there is nothing observed to cut on. Refusing to invent "
            "boundaries from a round number."
        )
    base_rate = n_bad / scores.size

    order = np.argsort(scores, kind="stable")
    sorted_scores = scores[order].astype(np.float64)
    sorted_labels = labels[order].astype(np.float64)
    smoother = IsotonicRegression(increasing=False, out_of_bounds="clip")
    smoother.fit(sorted_scores, sorted_labels)
    unique_scores = np.unique(sorted_scores)
    smoothed_rates = np.asarray(smoother.predict(unique_scores), dtype=np.float64)

    targets: list[tuple[str, float | None]] = [
        (
            entry.band_id,
            None
            if entry.bad_rate_multiple_of_base is None
            else entry.bad_rate_multiple_of_base * base_rate,
        )
        for entry in cfg.entries
    ]
    score_to_band: dict[int, str] = {}
    rate_by_score: dict[int, float] = {}
    for score_value, rate in zip(unique_scores.tolist(), smoothed_rates.tolist(), strict=True):
        key = int(round(score_value))
        assigned_band = targets[-1][0]
        for band_id, target in targets:
            if target is None:
                continue
            if rate <= target:
                assigned_band = band_id
                break
        score_to_band[key] = assigned_band
        rate_by_score[key] = float(rate)

    assigned_list, merge_reasons = _merge_inverted_bands(
        [score_to_band[int(round(value))] for value in scores.tolist()], labels, cfg
    )
    band_ids = cfg.band_ids
    assigned = np.asarray(assigned_list, dtype=object)
    stats = _observed_statistics(assigned_list, labels, band_ids)

    rows: list[BandRow] = []
    smoothed_at_cut: list[float] = []
    merged_lookup = {band: "" for band in band_ids}
    for reason in merge_reasons:
        victim = reason.split(" ", 1)[0]
        merged_lookup[victim] = reason
    for entry in cfg.entries:
        band = entry.band_id
        population, bad_count = stats[band]
        member_scores = [
            value for value, target_band in score_to_band.items() if target_band == band
        ]
        lower = min(member_scores) if member_scores and population else None
        upper = max(member_scores) if member_scores and population else None
        smoothed_at_cut.append(
            float(np.mean([rate_by_score[value] for value in member_scores]))
            if member_scores
            else float("nan")
        )
        victim_reason = merged_lookup.get(band, "") or None
        rows.append(
            BandRow(
                band_id=band,
                label=entry.label,
                action=entry.action,
                glyph=entry.glyph,
                min_points=lower,
                max_points=upper,
                population=population,
                population_share=population / scores.size,
                observed_bad_rate=(bad_count / population) if population else 0.0,
                observed_bad_count=bad_count,
                rate_target_multiple=entry.bad_rate_multiple_of_base,
                rate_target=(
                    None
                    if entry.bad_rate_multiple_of_base is None
                    else entry.bad_rate_multiple_of_base * base_rate
                ),
                band_absent_reason=(
                    None
                    if population
                    else (
                        victim_reason
                        or "no validation row landed in this band at the fitted cut; the band "
                        "is reported as absent rather than dropped, because an empty band is a "
                        "statement about the corpus"
                    )
                ),
                merged_from=(
                    tuple(
                        other
                        for other in band_ids
                        if merged_lookup.get(other, "").startswith(f"{other} merged into {band}")
                    )
                ),
                merge_reason=(
                    "; ".join(
                        merged_lookup[other]
                        for other in band_ids
                        if merged_lookup.get(other, "").startswith(f"{other} merged into {band}")
                    )
                    or None
                ),
            )
        )

    if not all(
        row.min_points is None or row.max_points is None for row in rows if row.population == 0
    ):
        raise BandFitError("an absent band carries a boundary; the table would misreport range")
    table = BandTable(
        rows=tuple(rows),
        base_rate=base_rate,
        n_rows=int(scores.size),
        smoothed_rate_at_cut=tuple(smoothed_at_cut),
        method="observed_bad_rate_isotonic_cut_with_monotone_merge",
    )
    if not table.is_rate_monotone():
        raise BandFitError(
            f"band table is not monotone in observed bad rate after merging: {table.observed_rates()}"
        )
    return table, assigned


__all__ = [
    "BAND_ORDER",
    "BandRow",
    "BandTable",
    "assign_bands",
    "fit_bands",
]
