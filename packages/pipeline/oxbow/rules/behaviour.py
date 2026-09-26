"""R6 ``VELOCITY_SPIKE``, R7 ``DORMANT_REACTIVATION``, R8 ``ODD_HOUR_SHIFT``,
R9 ``AMOUNT_REGIME_SHIFT`` — the behaviour-break rules.

These four share a shape that makes them the easiest rules in the set to get subtly
wrong: each compares an observation against the account's *own* history, so every one of
them is a two-window calculation, and every one of them has a degenerate case where the
baseline collapses to a shape that makes the comparison meaningless. The decisions below
are therefore mostly about when a rule refuses to score an account, which is the one
thing a boolean-valued detector can never express and the main reason §9 requires a
structured hit: "not scored, because the baseline has three days in it" is a sentence the
run report has to be able to say.

* **R6** uses a median-absolute-deviation scale, and a zero MAD is reported as not
  significant rather than as an infinite z-score. An account that sent exactly one
  transaction every day has no measured variability, and dividing by that zero would
  promote a single ordinary day into the loudest hit on the panel.
* **R7** measures dormancy in **local calendar days**, derived from ``local_hour``, and
  the burst that ends it in elapsed microseconds. Both are stated because the pair is
  the trap: an account that goes quiet over a weekend and a mule that goes quiet for a
  month differ by calendar days, and the same rule that reads the UTC hour for "night"
  also mis-measures the gap for a deployment east of Greenwich.
* **R8** reads ``local_hour`` and nothing else. The fixture plants the trap version
  (``S47``): transactions at 05:00Z are 08:00 in the deployment zone, so a UTC-reading
  implementation flags an ordinary business morning, which is the failure 03 C was
  written about.
* **R9** compares medians **within one currency**. A ratio between a euro median and a
  yen median is an implicit FX rate nobody wrote down (01 B, §8), so the rule evaluates
  each currency the account transacted in and reports the widest single-currency
  deviation instead of a mixed one.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from itertools import pairwise
from math import log
from typing import Final

from oxbow.rules.base import RuleContext, RuleOutcome
from oxbow.rules.events import (
    MICROSECONDS_PER_DAY,
    Event,
    Window,
    days_to_us,
    hours_to_us,
    ratio_of,
)
from oxbow.rules.fan import median_of
from oxbow.rules.hits import RuleHit, pattern_signature
from oxbow.rules.settings import (
    DormantSettings,
    OddHourSettings,
    RegimeSettings,
    VelocitySettings,
)
from oxbow.rules.severity import (
    clamp_unit,
    combine,
    excess_severity,
    log_band_severity,
    share_severity,
)

# The MAD-to-sigma factor for a normal distribution. Named because a z-score computed
# without it is a different quantity, and the config says "MAD-robust z-score".
MAD_TO_SIGMA: Final[float] = 1.4826
LOCAL_HOURS: Final[int] = 24
QUIET_DECILE: Final[float] = 0.10
# A decile of a 24-hour clock is 2.4 hours, truncated to 2. Taking "every hour with no
# recorded volume" instead would make a quiet set twenty-three hours wide, and any move
# into an hour the account had never used would read as a shift — which is the difference
# between a night-operation signal and a rule that fires on anyone who changes a habit.
QUIET_HOUR_COUNT: Final[int] = int(LOCAL_HOURS * QUIET_DECILE)
# An account with fewer distinct local hours of history than this has no meaningful
# "quiet" set: every hour looks quiet, which would flag any shift at all.
MIN_BASELINE_EVENTS_FOR_QUIET: Final[int] = 1


def velocity_spike(ctx: RuleContext) -> RuleOutcome:
    """R6: MAD-robust z of the busiest local day against the account's own baseline.

    The observation day is *inside* the baseline it is measured against, which is what
    stops a single enormous day inflating the dispersion it is scored on. A baseline with
    fewer than ``min_baseline_days`` active days is not scored at all.
    """
    settings: VelocitySettings = ctx.settings.settings_for("R6")  # type: ignore[assignment]
    baseline_us = days_to_us(settings.baseline_days)
    outcome = RuleOutcome()
    for account in ctx.accounts():
        daily = _daily_counts(ctx.incident_legs("R6", account))
        if not daily:
            continue
        best: tuple[float, int, int, int, float, float] | None = None
        for spike_day, spike_count in daily:
            prior = [
                count
                for day, count in daily
                if day != spike_day and spike_day - baseline_us < day < spike_day
            ]
            if len(prior) < settings.min_baseline_days:
                continue
            series = sorted([*prior, spike_count])
            median = median_float(series)
            mad = median_float(sorted(abs(count - median) for count in series))
            sigma = MAD_TO_SIGMA * mad
            if sigma <= 0.0:
                # A flat baseline has no measured variability. Calling that infinite z is
                # the arithmetic lie this guard exists to refuse.
                continue
            z = (spike_count - median) / sigma
            if z < settings.z:
                continue
            if best is None or z > best[0]:
                best = (z, spike_day, spike_count, len(prior), median, mad)
        if best is None:
            continue
        z, spike_day, spike_count, baseline_days, median, mad = best
        txn_ids = tuple(
            sorted(
                event.txn_id
                for event in ctx.incident_legs("R6", account)
                if event.local_day_us == spike_day
            )
        )
        outcome.hits.append(
            RuleHit(
                rule_id="R6",
                rule_name=ctx.spec("R6").name,
                account_key=account,
                severity=excess_severity(z, settings.z, 2 * settings.z),
                evidence={
                    "observation": round(z, 6),
                    "threshold_param": "z",
                    "threshold_value": settings.z,
                    "spike_local_day": _iso_day(spike_day),
                    "spike_txn_count": spike_count,
                    "baseline_median_count": round(median, 3),
                    "baseline_mad": round(mad, 6),
                    "baseline_active_days": baseline_days,
                    "min_baseline_days": settings.min_baseline_days,
                    "robust_scale": settings.robust_scale,
                    "txn_ids": list(txn_ids),
                },
                window=Window(
                    start_us=spike_day, end_us=spike_day + MICROSECONDS_PER_DAY, label="R6_day"
                ),
                hit_signature=pattern_signature("R6", account, txn_ids),
            )
        )
    return outcome


def median_float(values: Sequence[float]) -> float:
    """Median over a sorted-or-unsorted numeric population, float-preserving.

    Separate from :func:`oxbow.rules.fan.median_of`, which is int-typed for money
    comparisons: an account's median *count* of transactions can legitimately be 1.5, and
    rounding it to 1 would change the deviation the next day is measured against.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    size = len(ordered)
    if size % 2:
        return float(ordered[size // 2])
    return (float(ordered[size // 2 - 1]) + float(ordered[size // 2])) / 2.0


def dormant_reactivation(ctx: RuleContext) -> RuleOutcome:
    """R7: quiet for at least ``dormant_days`` local calendar days, then ``k`` in ``window_hours``.

    The gap is a difference of local calendar days — the same subtraction a person does
    when they say "they stopped talking to us five weeks ago" — and it is derived from
    ``local_hour``, so a deployment east of Greenwich does not lose or gain a day at the
    boundary. The burst that ends the silence is measured in elapsed microseconds,
    because "six transactions in a day" is a rate, not a date.
    """
    settings: DormantSettings = ctx.settings.settings_for("R7")  # type: ignore[assignment]
    gap_us = settings.dormant_days * MICROSECONDS_PER_DAY
    burst_us = hours_to_us(settings.window_hours)
    outcome = RuleOutcome()
    for account in ctx.accounts():
        legs = ctx.incident_legs("R7", account)
        if len(legs) < settings.k:
            continue
        days = sorted({event.local_day_us for event in legs})
        best: tuple[int, int, int, tuple[str, ...]] | None = None
        for previous, following in pairwise(days):
            gap = following - previous
            if gap < gap_us:
                continue
            burst_end = previous + gap
            burst = tuple(
                sorted(
                    (
                        event.txn_id
                        for event in legs
                        if burst_end <= event.ts_us <= burst_end + burst_us
                    ),
                )
            )
            if len(burst) < settings.k:
                continue
            dormancy_days = gap // MICROSECONDS_PER_DAY
            if best is None or dormancy_days > best[0]:
                best = (dormancy_days, len(burst), burst_end, burst)
        if best is None:
            continue
        dormancy_days, burst_count, burst_start, txn_ids = best
        severity = combine(
            excess_severity(float(burst_count), float(settings.k), float(2 * settings.k)),
            clamp_unit((dormancy_days - settings.dormant_days) / settings.dormant_days),
            weights=(1.0, 1.0),
        )
        outcome.hits.append(
            RuleHit(
                rule_id="R7",
                rule_name=ctx.spec("R7").name,
                account_key=account,
                severity=severity,
                evidence={
                    "observation": float(dormancy_days),
                    "threshold_param": "dormant_days",
                    "threshold_value": settings.dormant_days,
                    "dormant_calendar_days": dormancy_days,
                    "burst_txn_count": burst_count,
                    "k": settings.k,
                    "window_hours": settings.window_hours,
                    "reactivation_local_day": _iso_day(burst_start),
                    "txn_ids": list(txn_ids),
                    "uses_local_hour": True,
                },
                window=Window(
                    start_us=burst_start, end_us=burst_start + burst_us, label="R7_burst"
                ),
                hit_signature=pattern_signature("R7", account, txn_ids),
            )
        )
    return outcome


def odd_hour_shift(ctx: RuleContext) -> RuleOutcome:
    """R8: the share of volume in the account's historically quiet local hours jumps by ``q``.

    Quiet hours are the account's own low-volume decile of ``local_hour`` values, taken
    from its baseline period; the jump is absolute, not relative, because a relative
    change off a zero baseline is undefined and an absolute one is a statement.
    """
    settings: OddHourSettings = ctx.settings.settings_for("R8")  # type: ignore[assignment]
    recent_us = days_to_us(settings.recent_days - 1)
    outcome = RuleOutcome()
    for account in ctx.accounts():
        legs = ctx.incident_legs("R8", account)
        if len(legs) < settings.min_transactions:
            continue
        anchor = max(event.local_day_us for event in legs)
        recent_start = anchor - recent_us
        recent = [event for event in legs if event.local_day_us >= recent_start]
        baseline = [event for event in legs if event.local_day_us < recent_start]
        if len(recent) < settings.min_transactions:
            continue
        if len(baseline) < MIN_BASELINE_EVENTS_FOR_QUIET:
            # No history, no "historically quiet". Scoring this would call a brand-new
            # account's every hour a shift.
            continue
        per_hour = [0] * LOCAL_HOURS
        for event in baseline:
            per_hour[event.local_hour] += 1
        # Ranked by (volume, hour) rather than by a percentile of the counts: with a flat
        # baseline the percentile threshold would sweep in every unused hour, and the
        # tie-break on the hour number is what keeps the set identical between runs.
        quiet = {
            hour
            for _count, hour in sorted((count, hour) for hour, count in enumerate(per_hour))[
                :QUIET_HOUR_COUNT
            ]
        }
        if not quiet:
            continue
        baseline_share = ratio_of(
            sum(1 for event in baseline if event.local_hour in quiet), len(baseline)
        )
        recent_share = ratio_of(
            sum(1 for event in recent if event.local_hour in quiet), len(recent)
        )
        jump = recent_share - baseline_share
        if jump < settings.q:
            continue
        txn_ids = tuple(sorted(event.txn_id for event in recent))
        outcome.hits.append(
            RuleHit(
                rule_id="R8",
                rule_name=ctx.spec("R8").name,
                account_key=account,
                severity=share_severity(jump, settings.q),
                evidence={
                    "observation": round(jump, 6),
                    "threshold_param": "q",
                    "threshold_value": settings.q,
                    "recent_quiet_share": round(recent_share, 6),
                    "baseline_quiet_share": round(baseline_share, 6),
                    "quiet_hours": sorted(quiet),
                    "recent_txn_count": len(recent),
                    "min_transactions": settings.min_transactions,
                    "recent_days": settings.recent_days,
                    "txn_ids": list(txn_ids),
                    # The column this decision was made from. Asserted by
                    # tests/unit/test_p3b_rules.py rather than trusted.
                    "hour_source": "local_hour",
                },
                window=Window(
                    start_us=recent_start,
                    end_us=max(event.ts_us for event in recent),
                    label="R8_recent",
                ),
                hit_signature=pattern_signature("R8", account, txn_ids),
            )
        )
    return outcome


def amount_regime_shift(ctx: RuleContext) -> RuleOutcome:
    """R9: the account's median amount in the last ``recent_days`` against the prior ``baseline_days``.

    Outside ``1/m`` to ``m`` in either direction. Evaluated per currency, because a ratio
    across two currencies is an FX rate with no declared source, and the log-deviation
    severity treats a four-fold collapse and a four-fold surge identically on purpose —
    a regime break is a break, not an inflation preference.
    """
    settings: RegimeSettings = ctx.settings.settings_for("R9")  # type: ignore[assignment]
    recent_us = days_to_us(settings.recent_days - 1)
    baseline_us = days_to_us(settings.baseline_days)
    outcome = RuleOutcome()
    for account in ctx.accounts():
        legs = ctx.incident_legs("R9", account)
        if len(legs) < 2:
            continue
        anchor = max(event.local_day_us for event in legs)
        recent_start = anchor - recent_us
        baseline_start = recent_start - baseline_us
        by_currency: dict[str, tuple[list[Event], list[Event]]] = {}
        for event in legs:
            recent, baseline = by_currency.setdefault(event.currency, ([], []))
            if event.local_day_us >= recent_start:
                recent.append(event)
            elif baseline_start <= event.local_day_us < recent_start:
                baseline.append(event)
        best: tuple[float, str, int, int, tuple[str, ...]] | None = None
        for currency, (recent, baseline) in sorted(by_currency.items()):
            if not recent or not baseline:
                continue
            recent_median = median_of(sorted(event.amount_minor for event in recent))
            baseline_median = median_of(sorted(event.amount_minor for event in baseline))
            if recent_median <= 0 or baseline_median <= 0:
                continue
            ratio = recent_median / baseline_median
            if 1 / settings.m <= ratio <= settings.m:
                continue
            if best is None or abs(log(ratio)) > abs(log(best[0])):
                txn_ids = tuple(
                    sorted(
                        event.txn_id for event in recent + baseline if event.currency == currency
                    )
                )
                best = (ratio, currency, recent_median, baseline_median, txn_ids)
        if best is None:
            continue
        ratio, currency, recent_median, baseline_median, txn_ids = best
        outcome.hits.append(
            RuleHit(
                rule_id="R9",
                rule_name=ctx.spec("R9").name,
                account_key=account,
                severity=log_band_severity(ratio, settings.m),
                evidence={
                    "observation": round(ratio, 6),
                    "threshold_param": "m",
                    "threshold_value": settings.m,
                    "recent_median_amount_minor": recent_median,
                    "baseline_median_amount_minor": baseline_median,
                    "currency": currency,
                    "recent_days": settings.recent_days,
                    "baseline_days": settings.baseline_days,
                    "band_low": round(1 / settings.m, 6),
                    "band_high": settings.m,
                    "txn_ids": list(txn_ids),
                },
                window=Window(
                    start_us=baseline_start,
                    end_us=anchor + MICROSECONDS_PER_DAY,
                    label="R9_compare",
                ),
                hit_signature=pattern_signature("R9", account, txn_ids),
            )
        )
    return outcome


def _daily_counts(legs: Sequence[Event]) -> list[tuple[int, int]]:
    """Transaction counts per local calendar day, in day order."""
    counts: dict[int, int] = {}
    for event in legs:
        counts[event.local_day_us] = counts.get(event.local_day_us, 0) + 1
    return sorted(counts.items())


def _iso_day(local_day_us: int) -> str:
    """Local calendar day as an ISO date, for an evidence panel that must show a date."""
    return dt.datetime.fromtimestamp(local_day_us / 1_000_000, tz=dt.UTC).date().isoformat()


__all__ = [
    "MAD_TO_SIGMA",
    "amount_regime_shift",
    "dormant_reactivation",
    "median_float",
    "odd_hour_shift",
    "velocity_spike",
]
