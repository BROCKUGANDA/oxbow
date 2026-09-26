"""R5 ``STRUCTURING`` — the smurfing ladder.

Two things make this rule unusual and both are §9's requirements rather than
preferences: the salient threshold ``T`` does not exist in either corpus, so it is a
declared synthetic assumption that must be labelled as one and read from config; and the
pattern it detects is a *set* of transactions that can straddle the evaluation window
boundary, so hits must be identified by what they consist of rather than by where they
were found.

The maximal-ladder filter below is what stops one ladder producing four hits. An account
making four in-band withdrawals over four days sits inside a seven-day window anchored on
each of them, and the naive implementation reports the ladder once per anchor. Only the
widest set survives, because the three-transaction view of the same four transactions is
the same pattern seen from closer up — the containment rule is the same one
:func:`oxbow.rules.cycles._keep_maximal` uses for chains, and for the same reason.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from typing import Final

from oxbow.rules.base import RuleContext, RuleOutcome
from oxbow.rules.events import (
    ORIGINATED,
    Event,
    Window,
    days_to_us,
    ratio_at_least,
    ratio_of,
)
from oxbow.rules.hits import RuleHit, pattern_signature
from oxbow.rules.settings import StructuringSettings
from oxbow.rules.severity import clamp_unit, combine, excess_severity
from oxbow.rules.thresholds import STRUCTURING_FIT_NAME

# How close to ``T`` a transaction has to be to count as tight. The band already decides
# what is in; this only decides how much severity the closeness earns.
_TIGHTNESS_FLOOR: Final[float] = 0.0


def structuring(ctx: RuleContext) -> RuleOutcome:
    """R5: at least ``n`` originated transactions inside ``[low*T, high*T]`` within ``window_days``.

    Evaluated on transactions the account *originated*: a withdrawal ladder is the
    behaviour, and counting a cash agent that receives everyone's ladder would flag the
    institution rather than the smurfs.
    """
    settings: StructuringSettings = ctx.settings.settings_for("R5")  # type: ignore[assignment]
    threshold = ctx.thresholds.value_of(STRUCTURING_FIT_NAME)
    fit = ctx.thresholds.fits[STRUCTURING_FIT_NAME]
    window_us = days_to_us(settings.window_days)
    low_ok = settings.threshold_band_low
    high_ok = settings.threshold_band_high
    outcome = RuleOutcome()
    outcome.notes["threshold_derivation"] = fit.derivation
    for account in ctx.accounts():
        originated = ctx.legs("R5", account, ORIGINATED)
        in_band = tuple(
            event
            for event in originated
            if _in_band(event.amount_minor, threshold, low_ok, high_ok)
        )
        if len(in_band) < settings.n:
            continue
        ladders = _maximal_ladders(in_band, window_us, minimum=settings.n)
        for ladder in ladders:
            amounts = sorted(event.amount_minor for event in ladder)
            tightness = sum(ratio_of(amount, threshold) for amount in amounts) / len(amounts)
            tightness = max(tightness, _TIGHTNESS_FLOOR)
            txn_ids = tuple(sorted(event.txn_id for event in ladder))
            outcome.hits.append(
                RuleHit(
                    rule_id="R5",
                    rule_name=ctx.spec("R5").name,
                    account_key=account,
                    severity=combine(
                        excess_severity(float(len(ladder)), float(settings.n), float(2 * settings.n)),
                        clamp_unit((tightness - low_ok) / (high_ok - low_ok)),
                    ),
                    evidence={
                        "observation": float(len(ladder)),
                        "threshold_param": "n",
                        "threshold_value": settings.n,
                        "structuring_threshold_minor": threshold,
                        "threshold_source": fit.derivation,
                        "threshold_is_synthetic": settings.synthetic,
                        "threshold_label": settings.threshold_label,
                        "band_low_minor": _band_edge(threshold, low_ok, ceiling=False),
                        "band_high_minor": _band_edge(threshold, high_ok, ceiling=True),
                        "amounts_minor": amounts,
                        "mean_share_of_threshold": round(tightness, 6),
                        "window_days": settings.window_days,
                        "txn_ids": list(txn_ids),
                    },
                    window=Window(
                        start_us=ladder[0].ts_us,
                        end_us=ladder[0].ts_us + window_us,
                        label="R5_window",
                    ),
                    hit_signature=pattern_signature("R5", account, txn_ids),
                )
            )
    return outcome


def _in_band(amount: int, threshold: int, low: float, high: float) -> bool:
    """Is ``amount`` inside ``[low*T, high*T]``, decided with exact integer arithmetic.

    Both edges use the same ratio helper so the band cannot have a float boundary on one
    side and an exact one on the other. ``amount <= high * T`` is restated as
    ``T >= amount * (1/high)`` for that reason, and a transaction at exactly the upper
    edge is in the band, which is what "80 to 100 percent" says.
    """
    if amount <= 0 or threshold <= 0:
        return False
    return ratio_at_least(amount, threshold, low) and ratio_at_least(threshold, amount, 1.0 / high)


def _band_edge(threshold: int, factor: float, *, ceiling: bool) -> int:
    """The band edge in minor units, for the evidence panel to render.

    Rounded outward (down for the low edge, up for the high) so the rendered band always
    contains the amounts the exact comparison admitted, and never appears to exclude one.
    """
    raw = threshold * factor
    return int(raw // 1) if not ceiling else int(-(-raw // 1))


def _maximal_ladders(
    in_band: Sequence[Event], window_us: int, *, minimum: int
) -> tuple[tuple[Event, ...], ...]:
    """Every window-anchored set of at least ``minimum`` legs that no other such set contains.

    Anchoring on each transaction is sufficient and necessary: any window holding a set
    can be slid rightwards until its left edge touches a member without losing one, so a
    missed ladder would mean a missed anchor.
    """
    timestamps = [event.ts_us for event in in_band]
    candidates: list[tuple[Event, ...]] = []
    for index, start_us in enumerate(timestamps):
        end_us = start_us + window_us
        stop = bisect_right(timestamps, end_us)
        window = tuple(in_band[index:stop])
        if len(window) >= minimum:
            candidates.append(window)
    maximal: list[tuple[Event, ...]] = []
    for candidate in candidates:
        keys = frozenset(event.txn_id for event in candidate)
        if any(
            keys <= frozenset(event.txn_id for event in other) and len(keys) < len(other)
            for other in candidates
        ):
            continue
        maximal.append(candidate)
    deduped: dict[frozenset[str], tuple[Event, ...]] = {
        frozenset(event.txn_id for event in ladder): ladder for ladder in maximal
    }
    return tuple(
        deduped[key]
        for key in sorted(deduped, key=lambda item: (min(e.ts_us for e in deduped[item]), sorted(item)))
    )


__all__ = ["structuring"]
