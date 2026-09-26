"""Severity: the documented function from "how far past threshold" to 0-1.

§9 requires that two accounts hitting the same rule stay rankable, which means
severity cannot be a rule-level constant and cannot be an unbounded raw measurement
either. Every function here therefore has the same shape and states it once:

    severity = clamp((observation - threshold) / (saturation - threshold), 0, 1)

so 0.0 means "landed exactly on the threshold" and 1.0 means "at or beyond the
saturation point named in ``config/rules.yaml``" — the ``saturating at 2*k`` and
``saturating at 2*z`` phrases in the file are that point, not prose. Clamping is what
keeps a 40-sender account and a 30-sender account tieable rather than fighting over a
scale that has no top.

Why floats live here while money stays Int64: severity is a *rank*, printed to three
decimals and never summed into a currency figure. Threshold comparisons that decide a
fire/no-fire question are exact integer comparisons (:func:`oxbow.rules.events.
ratio_at_least`); this module only ever sees the winning side of that decision.

No function in here takes a default threshold or a default saturation. A silently
assumed saturation point would make every severity in the panel unexplained, and the
config file is the only place a threshold belongs.
"""

from __future__ import annotations

from math import log
from typing import Final

MIN_SEVERITY: Final[float] = 0.0
MAX_SEVERITY: Final[float] = 1.0
_EPSILON: Final[float] = 1e-12


def clamp_unit(value: float) -> float:
    """Squash into ``[0, 1]``. Named because three rules do it and one does it wrong."""
    if value <= MIN_SEVERITY:
        return MIN_SEVERITY
    if value >= MAX_SEVERITY:
        return MAX_SEVERITY
    return value


def excess_severity(observed: float, threshold: float, saturation: float) -> float:
    """Linear in how far ``observed`` sits past ``threshold``, saturating at ``saturation``.

    ``saturation <= threshold`` is a config error rather than a divide-by-zero, so it
    raises: a rule whose saturation equals its threshold would report every hit at
    full severity and read as a rule that is always certain.
    """
    if saturation <= threshold:
        raise ValueError(
            f"severity saturation {saturation} must exceed the threshold {threshold}; with "
            "them equal, every hit reports severity 1.0 and the panel stops being rankable"
        )
    return clamp_unit((observed - threshold) / (saturation - threshold))


def share_severity(observed_share: float, floor: float) -> float:
    """A share above a floor, normalised across the room left between floor and 1.0.

    Used by the share-typology rules (pass-through retention, cash-out share,
    first-counterparty share, cycle value retention): the span is by definition
    ``1 - floor``, so a share at 1.0 saturates at 1.0 severity with no extra knob.
    """
    span = 1.0 - floor
    if span <= _EPSILON:
        raise ValueError(
            f"a floor of {floor} leaves no room above it; severity would be a division by "
            f"{span}, which is a config error and not a flat 1.0"
        )
    return clamp_unit((observed_share - floor) / span)


def log_band_severity(ratio: float, band: float) -> float:
    """Distance outside a multiplicative band, measured in log so a 4x surge and a
    4x collapse rank the same.

    ``log ratio`` against ``log band`` puts a ratio at exactly the band edge at 0.0 and
    one a full band-width beyond it at 1.0. Measuring the raw ratio instead would make
    severity depend on which side of 1 the ratio lands, so a rule would flag inflation
    harder than deflation for no reason anyone chose.
    """
    if ratio <= 0.0 or band <= 1.0:
        raise ValueError(
            f"log band severity needs a positive ratio and a band above 1; got ratio={ratio} "
            f"band={band}. A band of 1.0 means 'no deviation is ever tolerated', which is a "
            "constant-firing rule and fails the hit-rate ceiling anyway."
        )
    return clamp_unit((abs(log(ratio)) - log(band)) / log(band))


def combine(*parts: float, weights: tuple[float, ...] | None = None) -> float:
    """Weighted mean of already-normalised parts, renormalised if they do not sum to 1.

    Two-sided rules (retention *and* length, burst count *and* dormancy) need to blend
    without either term dominating by accident. Averaging keeps the result inside
    ``[0, 1]`` and keeps each term's own saturation meaningful; the weights are exposed
    because §9's ``scales with X and with Y`` does not say in what proportion, and an
    unstated proportion is the one thing this module refuses to hide.
    """
    if not parts:
        raise ValueError("combine() needs at least one already-normalised severity part")
    for part in parts:
        if not MIN_SEVERITY <= part <= MAX_SEVERITY:
            raise ValueError(f"severity part {part} is outside [0, 1]; normalise it first")
    if weights is None:
        return clamp_unit(sum(parts) / len(parts))
    if len(weights) != len(parts):
        raise ValueError(
            f"combine() got {len(parts)} parts and {len(weights)} weights; a mismatch would "
            "silently drop a term from the score"
        )
    total = sum(weights)
    if total <= _EPSILON:
        raise ValueError("combine() weights sum to zero, which makes the blend undefined")
    return clamp_unit(sum(part * weight for part, weight in zip(parts, weights, strict=True)) / total)


__all__ = [
    "MAX_SEVERITY",
    "MIN_SEVERITY",
    "clamp_unit",
    "combine",
    "excess_severity",
    "log_band_severity",
    "share_severity",
]
