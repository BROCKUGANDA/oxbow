"""R1 ``RAPID_PASS_THROUGH`` and R10 ``FAST_CASH_OUT`` — the money-in/money-out pair.

Both ask the same question in different units: of the value that arrived, how much left
again, and how fast. They are the two rules most exposed to one specific wrong answer,
which is pairing a leg in one currency against a leg in another. A paymaster who receives
500,000 EUR and 700,000 USD and sends 450,000 USD out has moved 64.3 % of its dollars —
not 90 % of its euros — and the currency-blind pairing reports the second number. §8
forbids that arithmetic, so both rules here bucket by currency *before* pairing and the
fixture's ``S10_cross_currency_no_sum`` exists to prove it (03 G:
``test_cross_currency_sum_raises``).

Ratio decisions are exact integer comparisons
(:func:`oxbow.rules.events.ratio_at_least`), never ``amount >= p * threshold`` in float:
at the boundary a float comparison decides the hit, and a rule that fires on one machine
and not on another is worse than a rule that never fires.

The two rules share a helper and are deliberately in one module: §9 puts them in the
``extraction`` overlap group because they are usually the same two legs described twice,
and a reviewer comparing their pairing logic should not have to diff two files to find
out whether they agree.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from typing import Final

from oxbow.rules.base import RuleContext, RuleOutcome
from oxbow.rules.events import (
    ORIGINATED,
    RECEIVED,
    Event,
    Window,
    minutes_to_us,
    ratio_at_least,
    ratio_of,
)
from oxbow.rules.hits import RuleHit, pattern_signature
from oxbow.rules.settings import CashOutSettings, PassThroughSettings
from oxbow.rules.severity import share_severity

# A receive->send pairing is scanned, not index-searched, because the qualifying
# receive is the *smallest* one at or above the floor inside a time range — a two-
# dimensional query a single bisect cannot answer. The cap keeps a pathological
# thousand-event window from stalling the run, and tripping it is reported on the hit
# rather than changing the answer silently.
MAX_WINDOW_SCAN_LEGS: Final[int] = 4096

def rapid_pass_through(ctx: RuleContext) -> RuleOutcome:
    """R1: receives ``A``, then sends at least ``p * A`` within ``delta_minutes``.

    The account scored is the one that *received then sent* — the mule — and never the
    sender or the payee, because the typology is about the middle of the flow. Severity
    is the retained share's distance above ``p``, saturating at a full pass-through.
    """
    settings: PassThroughSettings = ctx.settings.settings_for("R1")  # type: ignore[assignment]
    delta_us = minutes_to_us(settings.delta_minutes)
    outcome = RuleOutcome()
    for account in ctx.accounts():
        receives = _by_currency(ctx.legs("R1", account, RECEIVED))
        if not receives:
            continue
        sends = ctx.legs("R1", account, ORIGINATED)
        best: tuple[float, Event, Event, bool] | None = None
        for send in sends:
            bucket = receives.get(send.currency, ())
            windowed = _legs_in_window(bucket, send.ts_us - delta_us, send.ts_us)
            scanned = windowed[:MAX_WINDOW_SCAN_LEGS]
            for receive in scanned:
                if receive.amount_minor < settings.min_amount_minor:
                    continue
                if not ratio_at_least(send.amount_minor, receive.amount_minor, settings.p):
                    continue
                ratio = ratio_of(send.amount_minor, receive.amount_minor)
                if best is None or ratio > best[0]:
                    best = (ratio, receive, send, len(windowed) > len(scanned))
        if best is None:
            continue
        ratio, receive, send, scan_truncated = best
        outcome.hits.append(
            RuleHit(
                rule_id="R1",
                rule_name=ctx.spec("R1").name,
                account_key=account,
                severity=share_severity(ratio, settings.p),
                evidence={
                    "observation": round(ratio, 6),
                    "threshold_param": "p",
                    "threshold_value": settings.p,
                    "received_txn_id": receive.txn_id,
                    "received_amount_minor": receive.amount_minor,
                    "sent_txn_id": send.txn_id,
                    "sent_amount_minor": send.amount_minor,
                    "currency": send.currency,
                    "elapsed_minutes": round((send.ts_us - receive.ts_us) / 60_000_000, 3),
                    "delta_minutes": settings.delta_minutes,
                    "min_amount_minor": settings.min_amount_minor,
                    "txn_ids": sorted([receive.txn_id, send.txn_id]),
                    "window_scan_truncated": scan_truncated,
                },
                window=Window(start_us=receive.ts_us, end_us=send.ts_us, label="R1_pair"),
                hit_signature=pattern_signature("R1", account, sorted([receive.txn_id, send.txn_id])),
            )
        )
    return outcome


def fast_cash_out(ctx: RuleContext) -> RuleOutcome:
    """R10: at least ``s`` of an inflow leaves as cash-out inside ``holding_hours``.

    The episode is anchored on one inflow, and the numerator is every cash-out that
    account made inside that inflow's holding window, so a mule that receives 800,000
    and empties it in two withdrawals is scored on the total (0.775), not on either
    withdrawal alone. Cash-out is recognised by ``txn_type`` rather than by counterparty
    because in both corpora the withdrawal is typed and the ATM is not an account.
    """
    settings: CashOutSettings = ctx.settings.settings_for("R10")  # type: ignore[assignment]
    holding_us = int(settings.holding_hours * 3_600 * 1_000_000)
    cash_types = frozenset(settings.cash_out_txn_types)
    outcome = RuleOutcome()
    for account in ctx.accounts():
        inflows = _by_currency(ctx.legs("R10", account, RECEIVED))
        if not inflows:
            continue
        exits = _by_currency(ctx.legs("R10", account, ORIGINATED))
        best: tuple[float, Event, int, int, tuple[str, ...]] | None = None
        for currency, bucket in sorted(inflows.items()):
            candidates = tuple(
                event for event in exits.get(currency, ()) if event.txn_type in cash_types
            )
            if not candidates:
                continue
            for inflow in bucket:
                windowed = _legs_in_window(candidates, inflow.ts_us, inflow.ts_us + holding_us)
                total = sum(event.amount_minor for event in windowed)
                if total <= 0 or not ratio_at_least(total, inflow.amount_minor, settings.s):
                    continue
                share = ratio_of(total, inflow.amount_minor)
                if best is None or share > best[0]:
                    best = (
                        share,
                        inflow,
                        total,
                        max(event.ts_us for event in windowed) - inflow.ts_us,
                        tuple(event.txn_id for event in windowed),
                    )
        if best is None:
            continue
        share, inflow, total, longest_hold, txn_ids = best
        outcome.hits.append(
            RuleHit(
                rule_id="R10",
                rule_name=ctx.spec("R10").name,
                account_key=account,
                severity=share_severity(share, settings.s),
                evidence={
                    "observation": round(share, 6),
                    "threshold_param": "s",
                    "threshold_value": settings.s,
                    "inflow_txn_id": inflow.txn_id,
                    "inflow_amount_minor": inflow.amount_minor,
                    "cash_out_amount_minor": total,
                    "cash_out_txn_ids": list(txn_ids),
                    "currency": inflow.currency,
                    "holding_minutes_observed": round(longest_hold / 60_000_000, 3),
                    "holding_hours": settings.holding_hours,
                    "txn_ids": sorted([inflow.txn_id, *txn_ids]),
                },
                window=Window(start_us=inflow.ts_us, end_us=inflow.ts_us + longest_hold, label="R10_episode"),
                hit_signature=pattern_signature("R10", account, inflow.txn_id, sorted(txn_ids)),
            )
        )
    return outcome


def _by_currency(legs: Sequence[Event]) -> dict[str, tuple[Event, ...]]:
    grouped: dict[str, list[Event]] = {}
    for leg in legs:
        grouped.setdefault(leg.currency, []).append(leg)
    # Each bucket stays in the frame's total order, so the bisects below are valid and
    # two runs with different input row order still slice the same window.
    return {
        currency: tuple(sorted(legs_in, key=lambda event: (event.ts_us, event.txn_id)))
        for currency, legs_in in grouped.items()
    }


def _legs_in_window(legs: tuple[Event, ...], start_us: int, end_us: int) -> tuple[Event, ...]:
    """Legs with ``start_us <= ts <= end_us``, by bisect on the total order."""
    if not legs:
        return ()
    timestamps = [leg.ts_us for leg in legs]
    return legs[bisect_left(timestamps, start_us) : bisect_right(timestamps, end_us)]


__all__ = ["MAX_WINDOW_SCAN_LEGS", "fast_cash_out", "rapid_pass_through"]
