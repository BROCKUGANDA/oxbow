"""Run-time threshold fitting, pinned to the window that produced it.

§9 has two thresholds that are not numbers in the config file:

* R2's ``tau`` is the corpus p25 of transaction amount, "computed at run time from the
  training window and recorded in the run's config hash".
* R5's salient reporting threshold ``T`` is derived from the amount histogram, and
  neither corpus carries a real one (DEV-014), so it is a declared synthetic assumption.

Both are fitted here and nowhere else, because the leakage risk is not the formula, it
is *which rows went into the formula*. Every function below therefore takes an explicit
window, returns a :class:`~oxbow.rules.hits.ThresholdFit` carrying it, and refuses to
fall back to a hardcoded number when the window holds nothing to fit — a p25 of an empty
frame is not "0", it is "this run cannot support R2", which is a different sentence and
has to be sayable.

The percentile is nearest-rank, matching
:func:`oxbow.graph.model.percentile_nearest_rank`: an interpolated 22000.5 minor units
is not a threshold any account's median can be compared against, and two fitters that
disagree about what "p25" means would silently move R2's hit count between the layer
that typed the nodes and the layer that scored them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from oxbow.rules.events import ORIGINATED, RuleEvents, Window
from oxbow.rules.hits import ThresholdFit

TAU_FIT_NAME: Final = "tau_minor"
STRUCTURING_FIT_NAME: Final = "structuring_threshold_minor"

# Histogram-mode search grid. The candidate thresholds are the observed amounts
# themselves: a reporting threshold that no transaction ever carried would not be a
# mode of anything, so searching the empirical support is the honest reading of
# "derived from histogram mode detection".
_MIN_HISTOGRAM_MODE_SUPPORT: Final[int] = 3


def _nearest_rank(values: Sequence[int], quantile: float) -> int:
    ordered = sorted(values)
    if not ordered:
        raise ValueError(
            "nearest-rank percentile over an empty population. Returning 0 here would "
            "turn 'nothing to fit' into a threshold every amount clears."
        )
    index = int(quantile * len(ordered))
    return ordered[min(max(index, 0), len(ordered) - 1)]


def fit_tau(
    events: RuleEvents, window: Window, *, quantile_percent: float, rule_id: str = "R2"
) -> ThresholdFit:
    """R2's ``tau``: the nearest-rank percentile of transaction amounts in the window.

    Every *transaction* once, both directions of an account's activity included. The
    originated view of a row and its received view are the same movement of the same
    money, so counting both would double every observation and describe no population
    that exists; counting only what one side initiated would describe a different
    population than the median it gates.
    """
    amounts = [
        event.amount_minor
        for event in events.events
        if event.side == ORIGINATED and window.contains(event.ts_us) and not event.is_zero_value
    ]
    if not amounts:
        raise ValueError(
            f"no non-zero amounts inside the fit window {window.label} "
            f"({window.start_us}..{window.end_us}); {rule_id} cannot be given a tau it "
            "did not measure. Widen the window or run R2 with the rule removed."
        )
    value = _nearest_rank(amounts, quantile_percent / 100.0)
    return ThresholdFit(
        name=TAU_FIT_NAME,
        rule_id=rule_id,
        value=value,
        derivation=(
            f"nearest-rank p{quantile_percent:g} of amount_minor over non-zero events in "
            "the training window (config tau_source: corpus_percentile)"
        ),
        window=window,
        sample_size=len(amounts),
    )


def fit_structuring_threshold(
    events: RuleEvents,
    window: Window,
    *,
    band_low: float,
    band_high: float,
    pinned_minor: int | None,
    pinned_label: str = "",
    rule_id: str = "R5",
) -> ThresholdFit:
    """R5's salient ``T``: the histogram mode if one exists, else the pinned assumption.

    When ``pinned_minor`` is set it wins, and the derivation says it was pinned: §9
    demands the threshold be config-sourced and labelled as an assumption, so an operator
    who wants the synthetic reporting line stated rather than inferred sets it in the
    YAML. With it unset the mode is derived, and if the histogram has no cluster to find
    the fit raises rather than inventing a threshold — silently returning a mode of a flat
    histogram is the fabricated number this whole module exists to prevent.
    """
    amounts = [
        event.amount_minor
        for event in events.events
        if event.side == ORIGINATED and window.contains(event.ts_us) and event.amount_minor > 0
    ]
    if pinned_minor is not None:
        return ThresholdFit(
            name=STRUCTURING_FIT_NAME,
            rule_id=rule_id,
            value=pinned_minor,
            derivation=(
                "config-pinned synthetic threshold "
                f"(rules.yaml.rule_engine.structuring_threshold_minor): {pinned_label}"
            ),
            window=window,
            sample_size=len(amounts),
        )
    best_value = 0
    best_support = 0
    for candidate in sorted(set(amounts)):
        support = sum(1 for amount in amounts if band_low * candidate <= amount <= band_high * candidate)
        if support > best_support:
            best_value, best_support = candidate, support
    if best_support < _MIN_HISTOGRAM_MODE_SUPPORT or best_value <= 0:
        raise ValueError(
            f"no histogram mode for {rule_id}: no observed amount has a "
            f"[{band_low:g}T, {band_high:g}T] band holding at least "
            f"{_MIN_HISTOGRAM_MODE_SUPPORT} originated transactions in the fit window. "
            "Pin rules.yaml.rule_engine.structuring_threshold_minor instead of letting a "
            "flat histogram invent a reporting threshold."
        )
    return ThresholdFit(
        name=STRUCTURING_FIT_NAME,
        rule_id=rule_id,
        value=best_value,
        derivation=(
            f"histogram mode: the observed amount whose [{band_low:g}, {band_high:g}] band "
            f"contains the most originated transactions ({best_support} of {len(amounts)})"
        ),
        window=window,
        sample_size=len(amounts),
    )


__all__ = ["STRUCTURING_FIT_NAME", "TAU_FIT_NAME", "fit_structuring_threshold", "fit_tau"]
