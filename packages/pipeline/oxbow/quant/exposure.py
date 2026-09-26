"""Exposure at risk ``E_i``: the value that is still interceptable.

Plan §11 defines the quantity every economic figure traces back to, and the
definition is doing two jobs at once, so it is worth stating both:

* **Still interceptable, not merely large.** ``E_i`` is the value flowing *out* of
  the account and its 1-hop downstream inside the recovery window that starts at
  the first triggering event. After the window the money is gone, and an
  investigation that cannot reach it cannot intercept it.
* **Capped at inflow observed in the same window.** Outflow here is *gross*: every
  leg leaving the subject and every leg leaving its 1-hop downstream, so money that
  passes through the cluster on its way out is counted at each hop it moves. That
  overstatement is the reason the cap is in the definition rather than a note under
  it - what was intercepted cannot exceed what came in during the window, so
  ``E_i = min(outflow, inflow)``. It is also what makes the cap a statement about
  the trigger event, which is the only thing the alert is evidence about: an account
  draining a pre-existing balance scores nothing, because nothing arrived.

Money discipline: every total is a :class:`~oxbow.quant.money.Money`, sums refuse
mixed currencies, and the window/hop radius arrives as explicit arguments so no
caller can compute an exposure under an unstated definition.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from oxbow.quant.economics import Economics
from oxbow.quant.money import Money, QuantError

# Sentinel returned when a cluster has no outflow at all.
_NO_MONEY: Final = 0


class CrossCurrencyExposureError(QuantError):
    """Raised when a cluster's flows are not all in one currency.

    02 money rules: currency is part of every amount and there is no implicit FX.
    A cluster with both EUR and UGX legs has a real exposure in each, and adding
    them yields a number that is not a total of anything.
    """


class ExposureDefinitionError(QuantError):
    """Raised when an exposure is asked for under an impossible definition."""


# SEAM (plan §11, P3a integration): the 1-hop downstream set is produced by this
# injected callable so the graph layer owns traversal, `rail` typing and the
# supernode guard. P3a plugs in a function of exactly this shape over the
# time-stamped multigraph; the edge-derived walk below is the standalone fallback
# that keeps this module testable before the graph exists, and it is the reason
# `neighbours` has no default that silently changes the answer.
NeighbourhoodFn = Callable[[str], Sequence[str]]


@dataclass(frozen=True, slots=True)
class FlowEdge:
    """One directed movement of value: the minimum an exposure needs from an event.

    Field names mirror the canonical event so the adapter at
    :func:`flow_edge_from_row` is a rename, not a reinterpretation. ``ts_utc`` is
    the real instant, never a local hour: an exposure window is a duration, and
    mixing a wall-clock hour into it shifts the window by the deployment offset.
    """

    txn_id: str
    src_account: str
    dst_account: str
    ts_utc: datetime
    amount: Money

    def __post_init__(self) -> None:
        """Refuse a mis-ordered edge rather than propagating a datetime as money.

        The field order is positional-friendly by accident and swaps happen: an
        amount passed where the timestamp belongs raises here, at the construction
        site, instead of surfacing as an ``AttributeError`` three modules away.
        """
        if not isinstance(self.ts_utc, datetime):
            raise ExposureDefinitionError(
                f"{self.txn_id}: ts_utc must be a datetime, got {type(self.ts_utc).__name__}. "
                "FlowEdge order is (txn_id, src, dst, ts_utc, amount)."
            )
        if not isinstance(self.amount, Money):
            raise ExposureDefinitionError(
                f"{self.txn_id}: amount must be Money, got {type(self.amount).__name__}"
            )
        if self.src_account == self.dst_account:
            raise ExposureDefinitionError(
                f"{self.txn_id}: self-transfer (src == dst) has no edge to walk; the "
                "graph layer keeps these as a count feature, not as exposure."
            )
        if not self.txn_id:
            raise ExposureDefinitionError("an edge without a txn_id cannot be ordered")


def flow_edge_from_row(row: Mapping[str, object]) -> FlowEdge:
    """Build an edge from one canonical-event row.

    The canonical column names are repeated rather than imported because
    ``oxbow.ingest`` is another phase's surface and this layer must not depend on
    it to do arithmetic; ``test_flow_edge_from_row_matches_the_canonical_columns``
    pins the names against the contract instead, which is the failure mode that
    matters (a rename upstream, not a typo here).
    """
    amount_value = row["amount_minor"]
    if isinstance(amount_value, bool) or not isinstance(amount_value, int):
        raise CrossCurrencyExposureError(
            f"{row['txn_id']}: amount_minor must be integer minor units, got "
            f"{type(amount_value).__name__}"
        )
    currency = row["currency"]
    if not isinstance(currency, str):
        raise CrossCurrencyExposureError(f"{row['txn_id']}: currency must be text")
    timestamp = row["event_ts_utc"]
    if not isinstance(timestamp, datetime):
        raise CrossCurrencyExposureError(
            f"{row['txn_id']}: event_ts_utc must be a datetime, got {type(timestamp).__name__}"
        )
    src = row["account_from"]
    dst = row["account_to"]
    if not isinstance(src, str) or not isinstance(dst, str):
        raise CrossCurrencyExposureError(f"{row['txn_id']}: account keys must be text")
    return FlowEdge(
        txn_id=str(row["txn_id"]),
        src_account=src,
        dst_account=dst,
        ts_utc=timestamp,
        amount=Money(amount_value, currency),
    )


@dataclass(frozen=True, slots=True)
class ExposureResult:
    """``E_i`` with both sides of the cap visible.

    The outflow and inflow components travel with the total because the cap is the
    interesting part of the answer: a reviewer looking at an exposure that was cut
    in half by the inflow cap is looking at a different claim than one that was
    not, and the UI cannot re-derive the difference from the number alone.
    """

    account: str
    cluster: tuple[str, ...]
    first_trigger_ts: datetime
    window_end: datetime
    window_hours: int
    hops: int
    outflow: Money
    inflow: Money
    exposure: Money
    edges_in_window: int
    capped_by_inflow: bool

    @property
    def is_zero(self) -> bool:
        """No interceptable value: the account cannot pay for its own review."""
        return self.exposure.is_zero

    def in_window(self, edge: FlowEdge) -> bool:
        """Whether ``edge`` falls inside this result's half-open window."""
        return self.first_trigger_ts <= edge.ts_utc < self.window_end

    def provenance_line(self) -> str:
        """The one-line definition label for a figure built on this exposure."""
        return (
            f"E_i for {self.account}: {self.outflow.minor} minor outflow of "
            f"{self.cluster} over {self.window_hours} h from "
            f"{self.first_trigger_ts.isoformat()}, capped at {self.inflow.minor} minor "
            f"inflow (cap bound {'hit' if self.capped_by_inflow else 'not hit'})"
        )


def _total_by_currency(amounts: Iterable[Money], *, context: str) -> dict[str, int]:
    """Group minor-unit totals by currency, never across currencies."""
    totals: dict[str, int] = {}
    for amount in amounts:
        totals[amount.currency] = totals.get(amount.currency, _NO_MONEY) + amount.minor
    if len(totals) > 1:
        raise CrossCurrencyExposureError(
            f"{context} mixes currencies {sorted(totals)}; total per currency or fail, "
            "there is no implicit FX (02 money rules)."
        )
    return totals


def downstream_cluster(
    events: Iterable[FlowEdge],
    account: str,
    first_trigger_ts: datetime,
    window_hours: int,
    hops: int,
    *,
    neighbours: NeighbourhoodFn | None = None,
) -> tuple[tuple[FlowEdge, ...], tuple[str, ...]]:
    """The in-window edges and the ``hops``-hop downstream cluster around ``account``.

    Returned together because the window filter is the expensive half and the
    exposure total needs the same edge set. The walk follows outbound legs only:
    "downstream" means where the money went, not who sent it, and including
    upstream accounts would let a victim's other customers inflate an exposure
    the intervention cannot reach.
    """
    if hops < 0:
        raise ExposureDefinitionError(f"hops must be >= 0, got {hops}")
    if window_hours < 1:
        raise ExposureDefinitionError(f"window_hours must be >= 1, got {window_hours}")
    window_end = first_trigger_ts + timedelta(hours=window_hours)
    in_window = sorted(
        (edge for edge in events if first_trigger_ts <= edge.ts_utc < window_end),
        # The configured total order, so the cluster is identical however the
        # caller happened to iterate its rows (01 A rule 4, `determinism.sort_keys`).
        key=lambda edge: (edge.ts_utc, edge.txn_id),
    )
    cluster: set[str] = {account}
    frontier: set[str] = {account}
    for _ in range(hops):
        nxt: set[str] = set()
        for node in sorted(frontier):
            if neighbours is not None:
                candidates = neighbours(node)
            else:
                candidates = [edge.dst_account for edge in in_window if edge.src_account == node]
            for candidate in candidates:
                if candidate not in cluster:
                    nxt.add(candidate)
        cluster |= nxt
        frontier = nxt
        if not nxt:
            break
    return tuple(in_window), tuple(sorted(cluster))


def exposure_at_risk(
    events: Iterable[FlowEdge],
    account: str,
    first_trigger_ts: datetime,
    window_hours: int,
    hops: int,
    cfg: Economics,
    *,
    neighbours: NeighbourhoodFn | None = None,
) -> ExposureResult:
    """``E_i`` = min(value leaving the cluster, value entering it) in the window.

    ``cfg`` is required rather than optional because it carries the currency the
    assumptions are priced in: an exposure totalled in a currency the review cost
    is not stated in cannot be turned into an expected value without an FX step
    this product does not have.
    """
    window_end = first_trigger_ts + timedelta(hours=window_hours)
    in_window, cluster = downstream_cluster(
        events,
        account,
        first_trigger_ts,
        window_hours,
        hops,
        neighbours=neighbours,
    )
    members = set(cluster)
    outflow_legs: list[Money] = []
    inflow_legs: list[Money] = []
    for edge in in_window:
        src_inside = edge.src_account in members
        dst_inside = edge.dst_account in members
        if src_inside:
            # Gross: a leg from one cluster member to another still moves value that
            # was interceptable at the trigger, and the inflow cap - not the choice of
            # which legs to count - is what keeps the double count bounded.
            outflow_legs.append(edge.amount)
        if dst_inside and not src_inside:
            inflow_legs.append(edge.amount)
    outflow_totals = _total_by_currency(outflow_legs, context=f"{account} outflow")
    inflow_totals = _total_by_currency(inflow_legs, context=f"{account} inflow")
    seen_currencies = sorted(set(outflow_totals) | set(inflow_totals))
    if len(seen_currencies) > 1:
        # Each side is internally pure, so neither helper above could see this:
        # outflow in one currency and inflow in another would silently make the cap
        # compare amounts that are not comparable.
        raise CrossCurrencyExposureError(
            f"exposure for {account} has outflow and inflow in different currencies "
            f"{seen_currencies}; the cap is a min() over two totals and only means "
            "something if they are the same money (02 money rules, no implicit FX)."
        )
    currency = seen_currencies[0] if seen_currencies else cfg.currency
    if currency != cfg.currency:
        raise CrossCurrencyExposureError(
            f"exposure for {account} is in {currency} but the economic assumptions in "
            f"{cfg.source_path.name} are stated in {cfg.currency}. Pricing across that "
            "gap would be an implicit FX rate nobody wrote down."
        )
    outflow = Money(outflow_totals.get(currency, _NO_MONEY), currency)
    inflow = Money(inflow_totals.get(currency, _NO_MONEY), currency)
    capped = inflow.minor < outflow.minor
    exposure = inflow if capped else outflow
    return ExposureResult(
        account=account,
        cluster=cluster,
        first_trigger_ts=first_trigger_ts,
        window_end=window_end,
        window_hours=window_hours,
        hops=hops,
        outflow=outflow,
        inflow=inflow,
        exposure=exposure,
        edges_in_window=len(in_window),
        capped_by_inflow=capped,
    )


def exposure_batch(
    events: Sequence[FlowEdge],
    triggers: Mapping[str, datetime],
    cfg: Economics,
    *,
    window_hours: int | None = None,
    hops: int | None = None,
    neighbours: NeighbourhoodFn | None = None,
) -> dict[str, ExposureResult]:
    """``E_i`` for many accounts in one pass over the same edge list.

    The window and hop radius default to the configured definition rather than to
    a literal, because a batch computed under a different radius than the product
    advertises is not a faster way to get the same numbers.
    """
    resolved_window = cfg.exposure.window_hours if window_hours is None else window_hours
    resolved_hops = cfg.exposure.downstream_hops if hops is None else hops
    return {
        account: exposure_at_risk(
            events,
            account,
            trigger,
            resolved_window,
            resolved_hops,
            cfg,
            neighbours=neighbours,
        )
        for account, trigger in sorted(triggers.items())
    }


__all__ = [
    "CrossCurrencyExposureError",
    "ExposureDefinitionError",
    "ExposureResult",
    "FlowEdge",
    "NeighbourhoodFn",
    "downstream_cluster",
    "exposure_at_risk",
    "exposure_batch",
    "flow_edge_from_row",
]
