"""P5 exposure: ``E_i`` as defined in plan §11, checked against arithmetic on paper.

The definition is "funds that flow out of the account and its 1-hop downstream
within the recovery window, capped at inflow observed in the same window", and each
clause is load-bearing, so each is pinned with numbers that can be added by hand:

===========================  ===========================================
edge                         effect on ``E_ACC-A``
===========================  ===========================================
txn:1  ACC-IN  -> ACC-A      inflow  +5,000,000  (T0 + 1 h)
txn:2  ACC-A   -> ACC-B      internal, counted on neither side (T0 + 2 h)
txn:3  ACC-A   -> ACC-C      internal, counted on neither side (T0 + 3 h)
txn:4  ACC-B   -> ACC-OUT    outflow +1,500,000  (T0 + 4 h)
txn:5  ACC-C   -> ACC-OUT    outflow +  800,000  (T0 + 5 h)
txn:6  ACC-A   -> ACC-OUT    outside the 24 h window, ignored (T0 + 25 h)
txn:7  ACC-OUT -> ACC-A      before the trigger, ignored        (T0 - 2 h)
===========================  ===========================================

Outflow is *gross*, so a leg between two cluster members still counts at the hop it
moves: at one hop the cluster is {A, B, C} and the outflow is 2,000,000 + 1,000,000
+ 1,500,000 + 800,000 = 5,300,000 against an inflow of 5,000,000, so the cap binds
and ``E_i = 5,000,000``. At zero hops the cluster is {A}: outflow 3,000,000, inflow
5,000,000, the cap does not bind and ``E_i = 3,000,000``. That difference - the cap
doing the work of bounding double-counted value, rather than an arbitrary choice
about which legs to see - is what §11's phrase "capped at inflow observed in the
same window" is for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oxbow.config import ConfigError
from oxbow.quant.economics import Economics, load_economics
from oxbow.quant.exposure import (
    CrossCurrencyExposureError,
    ExposureDefinitionError,
    FlowEdge,
    downstream_cluster,
    exposure_at_risk,
    exposure_batch,
    flow_edge_from_row,
)
from oxbow.quant.money import Money
from tests.unit.p5_fixtures import hand_economics

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def edge(txn: str, src: str, dst: str, minor: int, offset_hours: int) -> FlowEdge:
    """One flow edge at ``T0 + offset_hours``, in the fixture currency."""
    return FlowEdge(txn, src, dst, T0 + timedelta(hours=offset_hours), Money(minor, "UGX"))


def fixture_edges(inflow: int = 5_000_000) -> list[FlowEdge]:
    """The table in the module docstring."""
    return [
        edge("txn:1", "ACC-IN", "ACC-A", inflow, 1),
        edge("txn:2", "ACC-A", "ACC-B", 2_000_000, 2),
        edge("txn:3", "ACC-A", "ACC-C", 1_000_000, 3),
        edge("txn:4", "ACC-B", "ACC-OUT", 1_500_000, 4),
        edge("txn:5", "ACC-C", "ACC-OUT", 800_000, 5),
        edge("txn:6", "ACC-A", "ACC-OUT", 500_000, 25),
        edge("txn:7", "ACC-OUT", "ACC-A", 9_000_000, -2),
    ]


@pytest.fixture(scope="module")
def cfg() -> Economics:
    return hand_economics()


def test_exposure_one_hops_gross_outflow_is_bounded_by_inflow(cfg: Economics) -> None:
    """The cluster arithmetic in the module docstring, asserted exactly."""
    result = exposure_at_risk(fixture_edges(), "ACC-A", T0, 24, 1, cfg)
    assert result.cluster == ("ACC-A", "ACC-B", "ACC-C")
    assert result.outflow == Money(5_300_000, "UGX")
    assert result.inflow == Money(5_000_000, "UGX")
    assert result.exposure == Money(5_000_000, "UGX")
    assert result.capped_by_inflow
    # txn:6 (25 h) and txn:7 (before the trigger) are outside the window entirely.
    assert result.edges_in_window == 5


def test_exposure_zero_hops_prices_only_the_accounts_own_exit(cfg: Economics) -> None:
    """Same edges, radius 0: the two internal legs become outflow.

    The difference — 3,000,000 against 2,300,000 — is the whole reason the hop
    radius is a stated part of the definition rather than a detail.
    """
    result = exposure_at_risk(fixture_edges(), "ACC-A", T0, 24, 0, cfg)
    assert result.cluster == ("ACC-A",)
    assert result.outflow == Money(3_000_000, "UGX")
    assert result.exposure == Money(3_000_000, "UGX")
    assert not result.capped_by_inflow


def test_inflow_cap_binds_and_says_so(cfg: Economics) -> None:
    """The cap is what stops a pre-existing balance counting as interceptable.

    Cluster outflow is unchanged at 5,300,000 but only 1,000,000 came in during the
    window, so ``E_i`` is 1,000,000 and the result records that the cap bound - a
    reviewer reading the capped number without that flag would draw a different
    conclusion from the same figure: the exposure is what arrived, not what left.
    """
    result = exposure_at_risk(fixture_edges(inflow=1_000_000), "ACC-A", T0, 24, 1, cfg)
    assert result.inflow == Money(1_000_000, "UGX")
    assert result.exposure == Money(1_000_000, "UGX")
    assert result.capped_by_inflow


def test_window_is_half_open(cfg: Economics) -> None:
    """An edge exactly ``window_hours`` after the trigger is outside it.

    Inclusive on both ends would double-count a movement sitting on the shared
    boundary of two consecutive windows, and the boundary case is where two
    implementations disagree silently.
    """
    legs = [
        edge("txn:in", "ACC-IN", "ACC-A", 1_000_000, 1),
        edge("txn:b", "ACC-A", "ACC-OUT", 700, 24),
    ]
    assert (
        exposure_at_risk(legs, "ACC-A", T0, 24, 1, cfg).exposure.minor == 0
    ), "an event at exactly T0 + 24 h must fall outside a 24 h window"
    assert exposure_at_risk(legs, "ACC-A", T0, 25, 1, cfg).exposure == Money(700, "UGX")
    opening = [legs[0], edge("txn:o", "ACC-A", "ACC-OUT", 700, 0)]
    assert exposure_at_risk(opening, "ACC-A", T0, 24, 1, cfg).exposure == Money(700, "UGX")


def test_account_with_no_inflow_has_no_exposure(cfg: Economics) -> None:
    """A cluster that drains a balance it already had is not exposure under this definition.

    Zero here is the definition working, not a missing value: the trigger event
    brought nothing in, so nothing the intervention reaches can be intercepted.
    """
    edges = fixture_edges(inflow=0)[:0] + [edge("txn:x", "ACC-A", "ACC-OUT", 4_000_000, 2)]
    result = exposure_at_risk(edges, "ACC-A", T0, 24, 1, cfg)
    assert result.outflow == Money(4_000_000, "UGX")
    assert result.exposure.is_zero
    assert result.is_zero
    assert result.capped_by_inflow


def test_unknown_account_exposes_nothing_rather_than_raising(cfg: Economics) -> None:
    """An account absent from the window is a legitimate zero, not a lookup failure.

    The queue is assembled from alerts, and an alert whose subject has no in-window
    movement is exactly the case the EV layer needs to price at a loss (review cost
    with nothing to intercept) rather than crash on.
    """
    result = exposure_at_risk(fixture_edges(), "ACC-NONE", T0, 24, 1, cfg)
    assert result.cluster == ("ACC-NONE",)
    assert result.exposure.is_zero
    assert result.edges_in_window == 5


def test_injected_neighbourhood_changes_the_cluster(cfg: Economics) -> None:
    """The P3a seam is honoured: an injected downstream set beats the edge-derived one.

    ``ACC-D`` receives nothing from ``ACC-A`` inside the window, so the edge-derived
    walk cannot reach it; the injected function stands for the graph layer, which
    knows the account is downstream even when the window is too narrow to show it.
    """
    legs = [
        edge("txn:in", "ACC-IN", "ACC-A", 10_000_000, 1),
        edge("txn:own", "ACC-A", "ACC-N1", 500, 2),
        edge("txn:away", "ACC-D", "ACC-OUT", 3_000_000, 6),
    ]

    def neighbours(account: str) -> list[str]:
        return ["ACC-D"] if account == "ACC-A" else []

    without = exposure_at_risk(legs, "ACC-A", T0, 24, 1, cfg)
    with_injection = exposure_at_risk(legs, "ACC-A", T0, 24, 1, cfg, neighbours=neighbours)
    assert "ACC-D" not in without.cluster
    assert without.exposure == Money(500, "UGX")
    assert "ACC-D" in with_injection.cluster
    assert with_injection.exposure == Money(3_000_500, "UGX")


def test_two_hops_reach_the_second_layer(cfg: Economics) -> None:
    """Radius is a parameter, and the configured default is one hop."""
    edges = [*fixture_edges(), edge("txn:8", "ACC-B", "ACC-E", 600_000, 6)]
    one = exposure_at_risk(edges, "ACC-A", T0, 24, 1, cfg)
    two = exposure_at_risk(edges, "ACC-A", T0, 24, 2, cfg)
    assert "ACC-E" not in one.cluster
    assert "ACC-E" in two.cluster


def test_result_is_independent_of_input_order(cfg: Economics) -> None:
    """Determinism: the cluster and the totals come from the data, not the iteration order.

    The walk sorts by the configured total order ``(ts_utc, txn_id)`` precisely so a
    caller that reads rows in a different sequence gets the same exposure.
    """
    edges = fixture_edges()
    forward, reversed_order = (
        exposure_at_risk(edges, "ACC-A", T0, 24, 1, cfg),
        exposure_at_risk(list(reversed(edges)), "ACC-A", T0, 24, 1, cfg),
    )
    assert forward.cluster == reversed_order.cluster
    assert forward.exposure == reversed_order.exposure
    assert forward.outflow == reversed_order.outflow


def test_batch_shares_one_edge_list_and_sorts_by_account(cfg: Economics) -> None:
    """Per-account precomputation, which is what makes the slider cheap later."""
    triggers = {"ACC-B": T0, "ACC-A": T0}
    batch = exposure_batch(fixture_edges(), triggers, cfg)
    assert list(batch) == ["ACC-A", "ACC-B"]
    assert batch["ACC-A"].exposure == Money(5_000_000, "UGX")
    # ACC-B receives 2,000,000 in the window and pays 1,500,000 out to a counterparty
    # that is itself inside B's one-hop cluster, so the cap does not bind and the
    # exposure is what left.
    assert batch["ACC-B"].cluster == ("ACC-B", "ACC-OUT")
    assert batch["ACC-B"].outflow == Money(1_500_000, "UGX")
    assert batch["ACC-B"].exposure == Money(1_500_000, "UGX")


def test_impossible_definitions_are_refused(cfg: Economics) -> None:
    """A zero-hour window or a negative radius is a call-site bug, not an empty answer."""
    with pytest.raises(ExposureDefinitionError, match="hops must be"):
        exposure_at_risk(fixture_edges(), "ACC-A", T0, 24, -1, cfg)
    with pytest.raises(ExposureDefinitionError, match="window_hours must be"):
        exposure_at_risk(fixture_edges(), "ACC-A", T0, 0, 1, cfg)


def test_cross_currency_legs_raise(cfg: Economics) -> None:
    """Both sides of the cap must be the same money, or the comparison is meaningless."""
    mixed = [
        FlowEdge("txn:m1", "ACC-A", "ACC-OUT", T0, Money(3_000, "EUR")),
        FlowEdge("txn:m2", "ACC-IN", "ACC-A", T0, Money(9_000, "UGX")),
    ]
    with pytest.raises(CrossCurrencyExposureError, match="different currencies"):
        exposure_at_risk(mixed, "ACC-A", T0, 24, 0, cfg)
    foreign = [FlowEdge("txn:f", "ACC-A", "ACC-OUT", T0, Money(3_000, "USD"))]
    with pytest.raises(CrossCurrencyExposureError, match="economic assumptions"):
        exposure_at_risk(foreign, "ACC-A", T0, 24, 1, hand_economics(currency="EUR"))


def test_downstream_cluster_walk_is_public_and_ordered(cfg: Economics) -> None:
    """The walk is exported because the frontier and the packet both name the cluster.

    The window filter and the ``(ts_utc, txn_id)`` sort are visible in the returned
    edges, which is what a reviewer needs in order to check an exposure against the
    rows that produced it.
    """
    in_window, cluster = downstream_cluster(fixture_edges(), "ACC-A", T0, 24, 1)
    assert cluster == ("ACC-A", "ACC-B", "ACC-C")
    assert [item.txn_id for item in in_window] == [
        "txn:1",
        "txn:2",
        "txn:3",
        "txn:4",
        "txn:5",
    ]


def test_flow_edge_from_row_reads_the_canonical_columns() -> None:
    """The adapter from a canonical event row, pinned to the contract's column names.

    ``txn_id, event_ts_utc, amount_minor, currency, account_from, account_to`` are
    the fields P1b persists. A rename upstream has to break this test rather than
    produce an exposure built from a defaulted account name.
    """
    row = {
        "txn_id": "paysim:42",
        "event_ts_utc": T0,
        "amount_minor": 12_345,
        "currency": "UGX",
        "account_from": "ACC-A",
        "account_to": "ACC-B",
    }
    built = flow_edge_from_row(row)
    assert built == FlowEdge("paysim:42", "ACC-A", "ACC-B", T0, Money(12_345, "UGX"))
    with pytest.raises(CrossCurrencyExposureError, match="integer minor units"):
        flow_edge_from_row({**row, "amount_minor": 12.34})


def test_the_shipped_config_defines_a_24_hour_one_hop_window() -> None:
    """The product's window and radius, read from the real config rather than assumed.

    Plan §11 sets the default at 24 h and one hop; a retune would change every
    exposure figure in the product, so the value is asserted where it is configured.
    """
    shipped = load_economics(REPO_ROOT)
    assert shipped.exposure.window_hours == 24
    assert shipped.exposure.downstream_hops == 1
    result = exposure_at_risk(
        fixture_edges(),
        "ACC-A",
        T0,
        shipped.exposure.window_hours,
        shipped.exposure.downstream_hops,
        shipped,
    )
    assert result.exposure == Money(5_000_000, "UGX")
    with pytest.raises(ConfigError, match="no review minutes"):
        shipped.minutes_for("Q")
