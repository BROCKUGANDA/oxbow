"""R2 ``FAN_IN``, R3 ``FAN_OUT``, R11 ``NEW_COUNTERPARTY_SURGE`` — the counting rules.

All three ask "how many distinct counterparties inside a sliding window", and all three
fail the same way if that is implemented carelessly: a merchant, an agent or a payroll
rail has the customer base as counterparties and would fire on the whole economy (§9's
R2 exclusion note). So the node-type exclusion is applied before any counting, and it is
read from the *graph's* typing rather than re-derived here, because the graph already
owns the one definition of "rail" this repository has
(``graph.rail_degree_percentile``, P3a). R11 carries no such exclusion in the config —
DEV-G4 flags that as a spec gap rather than inventing one — and the fixture answers it on
arithmetic instead.

The window is a real sliding window anchored on each account's own event instants, not a
calendar bucket. A daily bucket would split a gather that starts at 23:50 into four plus
five and report nine senders as two non-hits, which is the difference between a rule that
fires and a rule that quietly does not; R2's ``S14`` near-miss exists precisely to catch
it (nine senders spread over 26 hours is seven in any 24-hour window).

R11's numerator counts a counterparty as new only when its *first* pair-transaction with
this account falls inside the window, and its denominator counts counterparties *active*
inside the window. Both halves are required: a share with no floor count makes a
two-counterparty account read as 100 % new, which is why §9 gives the rule ``g`` and
``c`` rather than one number.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from typing import Final

from oxbow.rules.base import RuleContext, RuleOutcome
from oxbow.rules.events import ORIGINATED, RECEIVED, Event, Window, hours_to_us, ratio_of
from oxbow.rules.hits import RuleHit, pattern_signature
from oxbow.rules.settings import FanInSettings, FanOutSettings, NewCounterpartySettings
from oxbow.rules.severity import excess_severity

TAU_FIT_NAME: Final = "tau_minor"

# A peer is one gatherer however many times it paid, so distinctness is keyed on the peer
# and the amounts are counted per event: the config's word "distinct" governs the first,
# and the median it asks for is a statement about transactions.


def fan_in(ctx: RuleContext) -> RuleOutcome:
    """R2: at least ``k`` distinct senders in ``window_hours``, with median amount below tau.

    ``tau`` is fitted on the training window by :mod:`oxbow.rules.thresholds`, so the
    threshold this rule is judged against travels with the run instead of living in code.
    """
    settings: FanInSettings = ctx.settings.settings_for("R2")  # type: ignore[assignment]
    tau = ctx.thresholds.value_of(TAU_FIT_NAME)
    window_us = hours_to_us(settings.window_hours)
    outcome = RuleOutcome()
    for account in ctx.accounts():
        if ctx.node_is_excluded("R2", account):
            continue
        best = max_distinct_peer_window(ctx.legs("R2", account, RECEIVED), window_us)
        if best is None:
            continue
        distinct, legs_in_window, start_us = best
        if distinct < settings.k:
            continue
        median = median_of(sorted(event.amount_minor for event in legs_in_window))
        if median >= tau:
            # The second condition doing its job. A large-value gather is a corporate
            # treasury: §9's median-below-tau clause exists to keep R2 about many *small*
            # contributions, and skipping it would teach the panel to ignore the rule.
            continue
        txn_ids = tuple(sorted(event.txn_id for event in legs_in_window))
        outcome.hits.append(
            RuleHit(
                rule_id="R2",
                rule_name=ctx.spec("R2").name,
                account_key=account,
                severity=excess_severity(float(distinct), float(settings.k), float(2 * settings.k)),
                evidence={
                    "observation": float(distinct),
                    "threshold_param": "k",
                    "threshold_value": settings.k,
                    "median_inbound_amount_minor": median,
                    "tau_minor": tau,
                    "tau_percentile": settings.tau_percentile,
                    "window_hours": settings.window_hours,
                    "senders": sorted({event.peer for event in legs_in_window}),
                    "txn_ids": list(txn_ids),
                    "currencies": sorted({event.currency for event in legs_in_window}),
                    "excluded_node_types": sorted(settings.exclude_node_types),
                },
                window=Window(start_us=start_us, end_us=start_us + window_us, label="R2_window"),
                hit_signature=pattern_signature("R2", account, txn_ids),
            )
        )
    return outcome


def fan_out(ctx: RuleContext) -> RuleOutcome:
    """R3: at least ``k`` distinct receivers in ``window_hours``."""
    settings: FanOutSettings = ctx.settings.settings_for("R3")  # type: ignore[assignment]
    window_us = hours_to_us(settings.window_hours)
    outcome = RuleOutcome()
    for account in ctx.accounts():
        if ctx.node_is_excluded("R3", account):
            continue
        best = max_distinct_peer_window(ctx.legs("R3", account, ORIGINATED), window_us)
        if best is None:
            continue
        distinct, legs_in_window, start_us = best
        if distinct < settings.k:
            continue
        txn_ids = tuple(sorted(event.txn_id for event in legs_in_window))
        outcome.hits.append(
            RuleHit(
                rule_id="R3",
                rule_name=ctx.spec("R3").name,
                account_key=account,
                severity=excess_severity(float(distinct), float(settings.k), float(2 * settings.k)),
                evidence={
                    "observation": float(distinct),
                    "threshold_param": "k",
                    "threshold_value": settings.k,
                    "window_hours": settings.window_hours,
                    "receivers": sorted({event.peer for event in legs_in_window}),
                    "txn_ids": list(txn_ids),
                    "total_amount_minor": sum(event.amount_minor for event in legs_in_window),
                    "currencies": sorted({event.currency for event in legs_in_window}),
                    "excluded_node_types": sorted(settings.exclude_node_types),
                },
                window=Window(start_us=start_us, end_us=start_us + window_us, label="R3_window"),
                hit_signature=pattern_signature("R3", account, txn_ids),
            )
        )
    return outcome


def new_counterparty_surge(ctx: RuleContext) -> RuleOutcome:
    """R11: at least ``g`` of the counterparties active in ``window_hours`` are first-time,
    and at least ``c`` of them are.

    "First-time" is measured against the whole frame the run was given, not the training
    window: an account's history is not a threshold, and truncating it would call every
    counterparty new at the start of every window and make the rule a calendar artefact.
    """
    settings: NewCounterpartySettings = ctx.settings.settings_for("R11")  # type: ignore[assignment]
    window_us = hours_to_us(settings.window_hours)
    first_seen = _first_pair_instants(ctx)
    outcome = RuleOutcome()
    for account in ctx.accounts():
        legs = ctx.legs("R11", account, ORIGINATED, incident=True)
        if not legs:
            continue
        fresh_instants = sorted(
            instant for (owner, _peer), instant in first_seen.items() if owner == account
        )
        best = max_fresh_peer_window(legs, window_us, fresh_instants)
        if best is None:
            continue
        fresh, active, start_us = best
        if fresh < settings.c:
            continue
        share = ratio_of(fresh, active)
        if share < settings.g:
            continue
        end_us = start_us + window_us
        txn_ids = tuple(sorted(event.txn_id for event in legs if start_us <= event.ts_us <= end_us))
        outcome.hits.append(
            RuleHit(
                rule_id="R11",
                rule_name=ctx.spec("R11").name,
                account_key=account,
                severity=excess_severity(float(fresh), float(settings.c), float(2 * settings.c)),
                evidence={
                    "observation": float(fresh),
                    "threshold_param": "c",
                    "threshold_value": settings.c,
                    "fresh_share": round(share, 6),
                    "g": settings.g,
                    "distinct_active": active,
                    "window_hours": settings.window_hours,
                    "txn_ids": list(txn_ids),
                },
                window=Window(start_us=start_us, end_us=end_us, label="R11_window"),
                hit_signature=pattern_signature("R11", account, txn_ids),
            )
        )
    return outcome


def max_distinct_peer_window(
    legs: Sequence[Event], window_us: int
) -> tuple[int, tuple[Event, ...], int] | None:
    """The window holding the most distinct counterparties, and which legs held them.

    Two pointers over the total order, so a hundred-thousand-event account is one pass
    rather than a quadratic scan. Ties break to the earliest window, which is the only
    choice that keeps a re-run with reordered input reporting the same evidence.
    """
    if not legs:
        return None
    best: tuple[int, int, int] | None = None
    counts: dict[str, int] = {}
    low = 0
    for high in range(len(legs)):
        counts[legs[high].peer] = counts.get(legs[high].peer, 0) + 1
        while legs[high].ts_us - legs[low].ts_us > window_us:
            _release(counts, legs[low].peer)
            low += 1
        distinct = len(counts)
        if best is None or distinct > best[0]:
            best = (distinct, low, high)
    if best is None:
        return None
    distinct, low, high = best
    return distinct, tuple(legs[low : high + 1]), legs[low].ts_us


def max_fresh_peer_window(
    legs: Sequence[Event], window_us: int, fresh_instants: Sequence[int]
) -> tuple[int, int, int] | None:
    """The window holding the most first-time counterparties, and how many were active.

    A counterparty is fresh in a window exactly when its first pair-transaction with this
    account falls inside it, and that first transaction is itself inside the window, so
    freshness is a bisect over first-instants while activeness still needs the distinct
    count — the two numbers are computed the same way for every candidate window.
    """
    if not legs:
        return None
    best: tuple[int, int, int, int] | None = None
    counts: dict[str, int] = {}
    low = 0
    for high in range(len(legs)):
        counts[legs[high].peer] = counts.get(legs[high].peer, 0) + 1
        while legs[high].ts_us - legs[low].ts_us > window_us:
            _release(counts, legs[low].peer)
            low += 1
        start_us, end_us = legs[low].ts_us, legs[low].ts_us + window_us
        fresh = bisect_right(fresh_instants, end_us) - bisect_left(fresh_instants, start_us)
        active = len(counts)
        candidate = (fresh, -start_us, active, low)
        if best is None or candidate > best:
            best = candidate
    if best is None:
        return None
    fresh, negative_start, active, _low = best
    return fresh, active, -negative_start


def median_of(values: Sequence[int]) -> int:
    """Ordinary median, returned as an int.

    An even population's midpoint can land on a half minor unit, and the money contract
    has no float to put it in. Truncating towards the lower unit keeps the comparison
    against ``tau`` in Int64 and can only ever make R2 *less* likely to fire, which is the
    direction that does not manufacture a hit.
    """
    if not values:
        return 0
    size = len(values)
    if size % 2:
        return int(values[size // 2])
    return int((values[size // 2 - 1] + values[size // 2]) // 2)


def _release(counts: dict[str, int], peer: str) -> None:
    remaining = counts.get(peer, 0) - 1
    if remaining <= 0:
        counts.pop(peer, None)
    else:
        counts[peer] = remaining


def _first_pair_instants(ctx: RuleContext) -> dict[tuple[str, str], int]:
    """First contact per (account, counterparty), keyed in both directions.

    Both directions because (A, B) is one relationship whichever side moved first. A
    receiver-only exclusion would make every counterparty look brand new to an account
    that only ever receives money, which is the opposite of the rule's meaning.
    """
    seen: dict[tuple[str, str], int] = {}
    for owner, legs in ctx.events.originated.items():
        for event in legs:
            if event.is_self_transfer:
                continue
            for key in ((owner, event.peer), (event.peer, owner)):
                current = seen.get(key)
                if current is None or event.ts_us < current:
                    seen[key] = event.ts_us
    return seen


__all__ = [
    "TAU_FIT_NAME",
    "fan_in",
    "fan_out",
    "max_distinct_peer_window",
    "max_fresh_peer_window",
    "median_of",
    "new_counterparty_surge",
]
