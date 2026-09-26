"""P5 Monte Carlo exposure propagation: reproducibility, the depth cap, and no point estimates.

The interval is the product's most persuasive number (plan §11), which makes it the
number most in need of being boring about how it was produced: same seed and settings
means the same interval, the depth cap is what the copy says it is, the transmission
probability is the value share and nothing else, and no field on the result can be
rendered as a single number.

The fixtures are chosen so the expected endpoints are derivable on paper rather than
approximately true:

* **Share 1.0 chain.** A node with one outgoing edge has that edge's share = 1, so it
  transmits every run. Five hops of 1,000,000 with a depth cap of 4 give exactly
  ``[4,000,000, 4,000,000]`` — a degenerate interval that proves the cap and the
  accumulation rule at the same time.
* **Eight equal exits.** Each of eight 1,000,000 legs has share 0.125, so the number
  that transmits is Binomial(8, 0.125) scaled by 1,000,000. The CDF is
  P(k<=0) = 0.3436, P(k<=1) = 0.7363, P(k<=2) = 0.9327, P(k<=3) = 0.9889, so the 5th
  percentile is 0 and the 95th is 3,000,000 - the interval is a hand-computed
  quantile of a named distribution.
* **75/25 split.** Outcomes are 0, 250,000, 750,000, 1,000,000 with probabilities
  0.1875, 0.0625, 0.5625, 0.1875; the cumulative mass reaches 0.8125 at 750,000, so
  the 95th percentile is 1,000,000 and the 5th is 0.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oxbow.quant.economics import Economics, load_economics
from oxbow.quant.exposure import ExposureDefinitionError, FlowEdge
from oxbow.quant.money import Money
from oxbow.quant.monte_carlo import (
    ExposureInterval,
    PropagationError,
    build_component_graph_from_frame,
    build_propagation_graph,
    simulate_exposure_interval,
)
from tests.unit.p5_fixtures import hand_economics

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 1, 1, tzinfo=UTC)
ARRIVAL = 1_000_000


@pytest.fixture(scope="module")
def cfg() -> Economics:
    return hand_economics()


def edge(txn_id: str, src: str, dst: str, minor: int) -> FlowEdge:
    """One in-window movement, built with keywords because the field order bites."""
    return FlowEdge(
        txn_id=txn_id, src_account=src, dst_account=dst, ts_utc=T0, amount=Money(minor, "UGX")
    )


def chain(hops: int = 5) -> tuple[list[str], list[FlowEdge]]:
    """``hops`` movements of ``ARRIVAL`` each, in a line from ACC-A onwards."""
    accounts = [f"ACC-{chr(ord('A') + index)}" for index in range(hops + 1)]
    edges = [
        edge(f"txn:{index}", accounts[index], accounts[index + 1], ARRIVAL)
        for index in range(hops)
    ]
    return accounts, edges


def eight_way() -> tuple[list[str], list[FlowEdge]]:
    """One origin, eight exits of equal value."""
    leaves = [f"ACC-L{index}" for index in range(8)]
    accounts = ["ACC-A", *leaves]
    edges = [edge(f"txn:{index}", "ACC-A", leaf, ARRIVAL) for index, leaf in enumerate(leaves)]
    return accounts, edges


def graph_from(
    cfg: Economics, accounts: list[str], edges: list[FlowEdge], arrival: int = ARRIVAL
):
    return build_propagation_graph(
        edges,
        component=accounts,
        seed_account="ACC-A",
        seed_amount=Money(arrival, cfg.currency),
        cfg=cfg,
    )


# --- reproducibility -------------------------------------------------------


def test_var_reproducible(cfg: Economics) -> None:
    """01 §A rule 4 applied to this module: the seed and the run count fix the interval.

    Two calls agree, and so does a fresh process — which is the part the numba cache
    could break and the part ``make verify-determinism`` depends on, since artefacts
    embed these endpoints.
    """
    accounts, edges = chain()
    graph = graph_from(cfg, accounts, edges)
    first = simulate_exposure_interval(graph, cfg)
    second = simulate_exposure_interval(graph, cfg)
    assert (first.low, first.high) == (second.low, second.high)
    assert first.runs == cfg.monte_carlo.runs
    assert first.seed == cfg.monte_carlo.seed

    # The same call in a fresh interpreter, with the settings echoed back. A per-process
    # RNG, or a numba cache that recompiled to different code, both show up here, and
    # artefacts embedding these endpoints are compared across runs by
    # `make verify-determinism`.
    script = (
        "import sys; sys.path.insert(0, 'packages/pipeline'); "
        "from datetime import datetime, UTC; "
        "from pathlib import Path; "
        "from oxbow.quant.economics import load_economics; "
        "from oxbow.quant.exposure import FlowEdge; "
        "from oxbow.quant.money import Money; "
        "from oxbow.quant.monte_carlo import build_propagation_graph, simulate_exposure_interval; "
        "cfg = load_economics(Path('.')); t = datetime(2026,1,1,tzinfo=UTC); "
        "a = ['ACC-A','ACC-B','ACC-C','ACC-D','ACC-E','ACC-F']; "
        "e = [FlowEdge(txn_id=f'txn:{i}', src_account=x, dst_account=y, ts_utc=t, "
        "amount=Money(1000000, cfg.currency)) for i,(x,y) in enumerate(zip(a,a[1:]))]; "
        "g = build_propagation_graph(e, component=a, seed_account='ACC-A', "
        "seed_amount=Money(1000000, cfg.currency), cfg=cfg); "
        "runs, depth, seed = (int(x) for x in sys.argv[1:4]); "
        "r = simulate_exposure_interval(g, cfg, runs=runs, max_depth=depth, seed=seed); "
        "print(r.low.minor, r.high.minor, r.runs, r.seed)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(first.runs), str(first.max_depth), str(first.seed)],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
        encoding="utf-8",
    )
    low, high, runs, seed = completed.stdout.split()
    assert (int(low), int(high)) == (first.low.minor, first.high.minor)
    assert (int(runs), int(seed)) == (first.runs, first.seed)


def test_the_interval_is_stable_across_seeds_at_the_configured_run_count(
    cfg: Economics,
) -> None:
    """The seed is recorded and honoured, and the interval does not depend on which one.

    Measured on the eight-exit graph: five different seeds all return the same
    endpoints, because 2,000 draws of a Binomial(8, 0.125) put the 5th and 95th
    percentiles squarely inside their own buckets. That is the reason the run count is
    a configured 10,000 rather than a number chosen per call: an interval that moved
    with the seed would be a draw, not an estimate, and the case page would be
    quoting the random numbers rather than the model.
    """
    accounts, edges = eight_way()
    graph = graph_from(cfg, accounts, edges, arrival=8 * ARRIVAL)
    intervals = [
        simulate_exposure_interval(graph, cfg, max_depth=1, seed=seed)
        for seed in (1337, 4242, 7, 99, 2024)
    ]
    endpoints = {(item.low.minor, item.high.minor) for item in intervals}
    assert len(endpoints) == 1, endpoints
    assert endpoints == {(0, 3 * ARRIVAL)}
    assert [item.seed for item in intervals] == [1337, 4242, 7, 99, 2024]


# --- the model, at the points where it is exactly derivable ----------------


def test_depth_cap_bounds_the_walk(cfg: Economics) -> None:
    """Every hop transmits with probability 1 here, so the total is arithmetic.

    Six accounts, five movements of 1,000,000: a cap of 4 admits exactly four
    transmissions, so the interval is ``[4,000,000, 4,000,000]`` and ACC-F never
    receives. With a cap of 5 it is 5,000,000. The cap is demonstrated as a bound
    rather than described.
    """
    accounts, edges = chain()
    graph = graph_from(cfg, accounts, edges)
    assert graph.edge_count == 5
    assert simulate_exposure_interval(graph, cfg, max_depth=4).low == Money(
        4 * ARRIVAL, cfg.currency
    )
    capped = simulate_exposure_interval(graph, cfg, max_depth=4)
    deeper = simulate_exposure_interval(graph, cfg, max_depth=5)
    assert capped.low == capped.high == Money(4 * ARRIVAL, cfg.currency)
    assert deeper.low == deeper.high == Money(5 * ARRIVAL, cfg.currency)
    assert capped.width.is_zero and capped.max_depth == 4


def test_transmission_probability_is_the_value_share(cfg: Economics) -> None:
    """The eight-exit and 75/25 cases, against quantiles computed by hand above."""
    accounts, edges = eight_way()
    graph = graph_from(cfg, accounts, edges, arrival=8 * ARRIVAL)
    assert graph.value_share == tuple([0.125] * 8)
    interval = simulate_exposure_interval(graph, cfg, max_depth=1)
    assert (interval.low.minor, interval.high.minor) == (0, 3 * ARRIVAL)

    star_accounts = ["ACC-A", "ACC-H", "ACC-L"]
    star = graph_from(
        cfg,
        star_accounts,
        [edge("txn:heavy", "ACC-A", "ACC-H", 750_000), edge("txn:light", "ACC-A", "ACC-L", 250_000)],
        arrival=1_000_000,
    )
    assert star.value_share == (0.75, 0.25)
    star_interval = simulate_exposure_interval(star, cfg, max_depth=1)
    assert (star_interval.low.minor, star_interval.high.minor) == (0, 1_000_000)

    certain = graph_from(cfg, ["ACC-A", "ACC-H"], [edge("txn:only", "ACC-A", "ACC-H", ARRIVAL)])
    assert simulate_exposure_interval(certain, cfg, max_depth=1).low == Money(ARRIVAL, cfg.currency)


def test_value_shares_are_computed_per_node(cfg: Economics) -> None:
    """Shares normalise against each node's own outflow, not the component total.

    A node paying out 1,000,000 in one leg is a certain transmitter even though the
    component moves far more; normalising globally would shrink every share and make
    the interval collapse towards zero for reasons that have nothing to do with the
    shape of the network.
    """
    edges = [
        edge("txn:1", "ACC-A", "ACC-B", 1_000_000),
        edge("txn:2", "ACC-A", "ACC-C", 3_000_000),
        edge("txn:3", "ACC-B", "ACC-D", 1_000_000),
        edge("txn:4", "ACC-C", "ACC-D", 3_000_000),
    ]
    graph = graph_from(cfg, ["ACC-A", "ACC-B", "ACC-C", "ACC-D"], edges)
    assert graph.value_share == (0.25, 0.75, 1.0, 1.0)
    assert graph.indptr == (0, 2, 3, 4, 4)
    assert graph.node_count == 4


def test_arrivals_at_one_account_are_merged_per_level(cfg: Economics) -> None:
    """Value reaching the same account twice is one balance moving on, not two.

    A holds 4,000,000 and splits 1,000,000 to B and 3,000,000 to C, i.e. shares 0.25
    and 0.75, and both forward everything they receive to D, which pays one 4,000,000
    leg onward. The totals are therefore 0 (neither leg fires, p = 0.1875), 3,000,000
    (B only, 0.0625), 9,000,000 (C only, 0.5625) or 12,000,000 (both, 0.1875), where
    the last case is 1M + 3M into D plus 1M + 3M onward plus D's own 4M. The CDF
    reaches 0.8125 at 9,000,000, so the 95th percentile is 12,000,000 and the 5th is 0.
    If D's two arrivals were walked as two independent paths instead of one merged
    balance, D would forward twice and the upper endpoint would be 16,000,000.
    """
    edges = [
        edge("txn:1", "ACC-A", "ACC-B", 1_000_000),
        edge("txn:2", "ACC-A", "ACC-C", 3_000_000),
        edge("txn:3", "ACC-B", "ACC-D", 1_000_000),
        edge("txn:4", "ACC-C", "ACC-D", 3_000_000),
        edge("txn:5", "ACC-D", "ACC-E", 4_000_000),
    ]
    graph = graph_from(
        cfg, ["ACC-A", "ACC-B", "ACC-C", "ACC-D", "ACC-E"], edges, arrival=4_000_000
    )
    interval = simulate_exposure_interval(graph, cfg, max_depth=4)
    assert (interval.low.minor, interval.high.minor) == (0, 12_000_000)


def test_component_external_edges_are_excluded_and_counted(cfg: Economics) -> None:
    """The component is P3a's answer, so this layer must not widen it silently."""
    accounts, edges = chain()
    leaking = edges + [edge("txn:leak", "ACC-A", "ACC-GHOST", 9 * ARRIVAL)]
    graph = graph_from(cfg, accounts, leaking)
    assert graph.excluded_edges == 1
    assert graph.edge_count == 5
    assert "ACC-GHOST" not in graph.accounts
    assert simulate_exposure_interval(graph, cfg).high.minor <= 5 * ARRIVAL


# --- the shape of the result ----------------------------------------------


def test_the_interval_is_an_interval_and_never_a_point(cfg: Economics) -> None:
    """No field on the result renders as a single number, and the text shows two.

    Plan §11 asks for an interval with its assumption line. A ``mean`` or ``point``
    field would be the headline within a day, so the type does not offer one.
    """
    accounts, edges = eight_way()
    interval = simulate_exposure_interval(
        graph_from(cfg, accounts, edges, arrival=8 * ARRIVAL), cfg
    )
    fields = set(interval.__dataclass_fields__)
    assert {"low", "high"} <= fields
    assert not fields & {"mean", "point", "estimate", "median", "value", "best"}
    rendered = interval.render(cfg)
    assert rendered.splitlines()[0].count(" at r=") == 3
    assert rendered.splitlines()[1].count(" at r=") == 3
    assert "config/economics.yaml" in rendered
    assert "not a measurement" in rendered
    assert f"{interval.runs:,}" in rendered
    assert interval.width == interval.high - interval.low
    low_figure, high_figure = interval.figures(cfg)
    assert low_figure.default <= high_figure.default


def test_the_interval_reports_the_settings_that_made_it(cfg: Economics) -> None:
    """The case page quotes runs, seed and depth beside the number, so they are on it."""
    accounts, edges = chain(hops=2)
    interval = simulate_exposure_interval(graph_from(cfg, accounts, edges), cfg)
    assert interval.runs == cfg.monte_carlo.runs
    assert interval.seed == cfg.monte_carlo.seed
    assert interval.max_depth == cfg.monte_carlo.max_depth
    assert interval.node_count == 3
    assert interval.edge_count == 2
    assert interval.level == pytest.approx(0.90)
    assert all(line for line in interval.assumption_lines())


def test_the_shipped_config_drives_ten_thousand_runs_at_depth_four() -> None:
    """The product's own settings, measured, with the interval printed for the report."""
    shipped = load_economics(REPO_ROOT)
    assert shipped.monte_carlo.runs == 10_000
    assert shipped.monte_carlo.max_depth == 4
    assert shipped.monte_carlo.seed == 1337
    assert (shipped.monte_carlo.lower_quantile, shipped.monte_carlo.upper_quantile) == (0.05, 0.95)
    accounts, edges = chain(hops=5)
    branch = [edge("txn:branch", "ACC-B", "ACC-G", ARRIVAL)]
    graph = build_propagation_graph(
        [*edges, *branch],
        component=[*accounts, "ACC-G"],
        seed_account="ACC-A",
        seed_amount=Money(20 * ARRIVAL, shipped.currency),
        cfg=shipped,
    )
    interval = simulate_exposure_interval(graph, shipped)
    assert (interval.runs, interval.seed, interval.max_depth) == (10_000, 1337, 4)
    print(
        f"branching component, shipped settings: 90 % interval "
        f"{interval.low.minor:,} to {interval.high.minor:,} minor "
        f"{shipped.currency} over {interval.runs:,} runs, seed {interval.seed}, "
        f"depth {interval.max_depth}, {interval.node_count} accounts / "
        f"{interval.edge_count} edges"
    )
    assert interval.low <= interval.high


# --- refusals --------------------------------------------------------------


def test_a_component_that_cannot_propagate_is_refused(cfg: Economics) -> None:
    """Every guard here stops a zero or a crash being read as a finding."""
    with pytest.raises(PropagationError, match="empty component"):
        build_propagation_graph(
            [],
            component=[],
            seed_account="ACC-A",
            seed_amount=Money(1, cfg.currency),
            cfg=cfg,
        )
    with pytest.raises(PropagationError, match="not in the component"):
        build_propagation_graph(
            [edge("txn:1", "ACC-A", "ACC-B", 10)],
            component=["ACC-B"],
            seed_account="ACC-A",
            seed_amount=Money(1, cfg.currency),
            cfg=cfg,
        )
    # Every leg leaves the component, so nothing is left to propagate along.
    isolated = build_propagation_graph(
        [edge("txn:1", "ACC-A", "ACC-GHOST", 10)],
        component=["ACC-A"],
        seed_account="ACC-A",
        seed_amount=Money(1, cfg.currency),
        cfg=cfg,
    )
    with pytest.raises(PropagationError, match="no internal edges"):
        simulate_exposure_interval(isolated, cfg)


def test_a_second_currency_in_the_component_is_refused(cfg: Economics) -> None:
    """Value shares across two monies are not shares, and the seed amount must match too."""
    with pytest.raises(PropagationError, match="assumptions are stated in"):
        build_propagation_graph(
            [edge("txn:1", "ACC-A", "ACC-B", 10)],
            component=["ACC-A", "ACC-B"],
            seed_account="ACC-A",
            seed_amount=Money(10, "EUR"),
            cfg=cfg,
        )
    foreign = FlowEdge(
        txn_id="txn:2",
        src_account="ACC-A",
        dst_account="ACC-B",
        ts_utc=T0,
        amount=Money(10, "EUR"),
    )
    with pytest.raises(PropagationError, match="priced in"):
        build_propagation_graph(
            [foreign],
            component=["ACC-A", "ACC-B"],
            seed_account="ACC-A",
            seed_amount=Money(10, cfg.currency),
            cfg=cfg,
        )


def test_a_mis_ordered_edge_is_caught_at_construction(cfg: Economics) -> None:
    """The positional trap, closed: an amount where the timestamp belongs is refused."""
    with pytest.raises(ExposureDefinitionError, match="ts_utc must be a datetime"):
        FlowEdge("txn:1", "ACC-A", "ACC-B", 1_000_000, T0)  # type: ignore[arg-type]
    with pytest.raises(ExposureDefinitionError, match="self-transfer"):
        FlowEdge(
            txn_id="txn:2",
            src_account="ACC-A",
            dst_account="ACC-A",
            ts_utc=T0,
            amount=Money(10, cfg.currency),
        )


def test_frame_input_is_checked_for_the_canonical_columns(cfg: Economics) -> None:
    """The frame path fails on a missing column rather than defaulting an account name."""
    import polars as pl

    frame = pl.DataFrame(
        {
            "txn_id": ["txn:1", "txn:2"],
            "event_ts_utc": [T0, T0],
            "account_from": ["ACC-A", "ACC-B"],
            "account_to": ["ACC-B", "ACC-C"],
            "amount_minor": [4_000_000, 1_000_000],
            "currency": ["UGX", "UGX"],
        }
    )
    graph = build_component_graph_from_frame(
        frame,
        component=["ACC-A", "ACC-B", "ACC-C"],
        seed_account="ACC-A",
        seed_amount=Money(ARRIVAL, cfg.currency),
        cfg=cfg,
    )
    assert graph.value_share == (1.0, 1.0)
    assert graph.excluded_edges == 0
    with pytest.raises(PropagationError, match="missing"):
        build_component_graph_from_frame(
            frame.drop("amount_minor"),
            component=["ACC-A", "ACC-B", "ACC-C"],
            seed_account="ACC-A",
            seed_amount=Money(ARRIVAL, cfg.currency),
            cfg=cfg,
        )


def test_an_inverted_interval_is_refused(cfg: Economics) -> None:
    """A result whose upper bound sits below its lower bound is a bug, not a wide interval."""
    with pytest.raises(PropagationError, match="inverted"):
        ExposureInterval(
            low=Money(100, cfg.currency),
            high=Money(50, cfg.currency),
            level=0.9,
            runs=10,
            seed=1337,
            max_depth=4,
            node_count=2,
            edge_count=1,
            excluded_edges=0,
        )
