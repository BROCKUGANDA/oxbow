"""Monte Carlo exposure propagation for a flagged component (spec §6.6).

The case page's most persuasive number is "exposure if nobody acts": value does
not stop at the account the alert fired on, it keeps moving through the component.
This module simulates that movement and returns a **90 % interval**, never a point
estimate — a point estimate from a stochastic propagation would imply a precision
the model does not have, and plan §11 makes the interval the deliverable.

The propagation model, stated because the interval is only as defensible as it is:

* Each outgoing edge transmits with probability equal to its **value share** at
  that node (edge value over the node's total outflow). A channel carrying 70 % of
  an account's out money carries 70 % of the probability that money moves on. The
  shares sum to 1, so the process is contractive in expectation: expected
  out-movement at a node is ``arrived * sum(share ** 2)``, which is at most
  ``arrived``.
* Amount moved along an edge is ``arrived * share``, rounded half up in minor
  units — the same rule as the rest of the layer, not a second one.
* Depth is capped at ``monte_carlo.max_depth`` (4) and arrivals at a node are
  **merged per level**, because value reaching the same account twice is one
  balance moving onward, not two independent movements. Merging also bounds the
  state at one slot per account per level, so a component with cycles — which is
  the whole reason it is flagged — cannot blow the walk up.
* ``monte_carlo.runs`` draws from ``monte_carlo.seed`` inside a numba kernel that
  seeds itself and runs single-threaded. Single-threaded is a determinism choice,
  not a performance one: numba's parallel backend seeds per thread, after which
  the interval depends on the core count of the machine that rendered the page.

Quantiles are nearest-rank over the simulated totals, so both endpoints are
amounts that occurred in a run rather than an interpolation between two of them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numba
import numpy as np
import polars as pl

from oxbow.quant.economics import (
    BAND_WIDTH,
    CurrencyFigure,
    Economics,
    assumption_block,
    currency_figure,
)
from oxbow.quant.exposure import FlowEdge, flow_edge_from_row
from oxbow.quant.money import Money, QuantError

# One simulated transmission that rounds below this is dropped rather than pushed
# to the next level: keeping it would let a chain of rounding artefacts occupy
# level slots and shift the draw order of the real edges behind them.
_MINIMUM_MOVED_MINOR: int = 1

# Columns the edge frame must carry; repeated rather than imported from
# ``oxbow.ingest`` because this layer must not depend on another phase's package
# to do arithmetic, and pinned against the contract by a test instead.
_EDGE_FRAME_COLUMNS: frozenset[str] = frozenset(
    {"txn_id", "event_ts_utc", "account_from", "account_to", "amount_minor", "currency"}
)


class PropagationError(QuantError):
    """Raised when a component cannot be propagated, rather than propagating to zero."""


@numba.njit(cache=True, parallel=False)
def _propagate_kernel(
    seed_index: numba.int64,
    indptr: numba.int64[:],
    destinations: numba.int64[:],
    value_share: numba.float64[:],
    seed_amount: numba.int64,
    runs: numba.int64,
    max_depth: numba.int64,
    node_count: numba.int64,
) -> numba.int64[:]:
    """Total value still moving within ``max_depth`` hops, one entry per run.

    Seeded inside the kernel, so the stream is a property of the arguments and
    nothing else: the same call returns the same 10,000 totals on any machine,
    which is what ``test_var_reproducible`` demands of every seeded draw here.
    """
    totals = np.empty(runs, dtype=np.int64)
    amounts = np.zeros((2, node_count), dtype=np.int64)
    levels = np.zeros((2, node_count), dtype=np.int64)
    np.random.seed(seed_index)
    for run in range(runs):
        total = np.int64(0)
        current = np.int64(0)
        count = np.int64(1)
        levels[0, 0] = seed_index
        amounts[0, seed_index] = seed_amount
        for _depth in range(max_depth):
            following = np.int64(1) - current
            next_count = np.int64(0)
            for position in range(count):
                node = levels[current, position]
                arrived = amounts[current, node]
                amounts[current, node] = np.int64(0)
                if arrived <= 0:
                    continue
                for edge in range(indptr[node], indptr[node + 1]):
                    share = value_share[edge]
                    if np.random.random() < share:
                        moved = np.int64(arrived * share + 0.5)
                        if moved < _MINIMUM_MOVED_MINOR:
                            continue
                        target = destinations[edge]
                        if amounts[following, target] == 0:
                            levels[following, next_count] = target
                            next_count += np.int64(1)
                        amounts[following, target] += moved
                        total += moved
            count = next_count
            current = following
            if count == 0:
                break
        totals[run] = total
    return totals


@dataclass(frozen=True, slots=True)
class PropagationGraph:
    """A flagged component in the compressed shape the kernel walks.

    CSR with accounts in ascending key order and edges ordered by
    ``(source_index, destination_index, txn_id)`` — the configured total order — so
    the random draws land on the same edges in the same sequence on every run.
    """

    accounts: tuple[str, ...]
    seed_account: str
    indptr: tuple[int, ...]
    destinations: tuple[int, ...]
    values: tuple[int, ...]
    value_share: tuple[float, ...]
    seed_amount: Money
    currency: str
    excluded_edges: int

    @property
    def node_count(self) -> int:
        """Accounts in the component."""
        return len(self.accounts)

    @property
    def edge_count(self) -> int:
        """Directed value movements the simulation can transmit along."""
        return len(self.destinations)

    @property
    def seed_index(self) -> int:
        """Kernel index of the account the propagation starts from."""
        return self.accounts.index(self.seed_account)

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """The three kernel inputs as contiguous arrays."""
        return (
            np.asarray(self.indptr, dtype=np.int64),
            np.asarray(self.destinations, dtype=np.int64),
            np.asarray(self.value_share, dtype=np.float64),
        )


def build_propagation_graph(
    edges: Iterable[FlowEdge],
    *,
    component: Sequence[str],
    seed_account: str,
    seed_amount: Money,
    cfg: Economics,
) -> PropagationGraph:
    """Turn a component's edges into the propagation structure.

    Edges leaving the component are counted and dropped (``excluded_edges``) rather
    than silently followed: the component is the set P3a flagged, and inventing
    destinations outside it would let this module disagree with the graph layer
    about what the network is.
    """
    accounts = tuple(sorted(set(component)))
    if not accounts:
        raise PropagationError("an empty component has nothing to propagate")
    if seed_account not in accounts:
        raise PropagationError(
            f"seed account {seed_account!r} is not in the component, so the interval "
            "would have no origin"
        )
    if seed_amount.currency != cfg.currency:
        raise PropagationError(
            f"seed amount is {seed_amount} but the assumptions are stated in {cfg.currency}"
        )
    index = {account: position for position, account in enumerate(accounts)}
    members = set(accounts)
    kept: list[FlowEdge] = []
    excluded = 0
    for edge in edges:
        if edge.src_account in members and edge.dst_account in members:
            if edge.amount.currency != cfg.currency:
                raise PropagationError(
                    f"edge {edge.txn_id} is {edge.amount} while the component is priced in "
                    f"{cfg.currency}: value shares across two monies are not shares"
                )
            kept.append(edge)
        else:
            excluded += 1
    kept.sort(key=lambda edge: (index[edge.src_account], index[edge.dst_account], edge.txn_id))
    degrees = [0] * len(accounts)
    outflow = [0] * len(accounts)
    for edge in kept:
        source = index[edge.src_account]
        degrees[source] += 1
        outflow[source] += edge.amount.minor
    indptr = [0]
    for degree in degrees:
        indptr.append(indptr[-1] + degree)
    destinations: list[int] = []
    values: list[int] = []
    shares: list[float] = []
    for edge in kept:
        source = index[edge.src_account]
        total = outflow[source]
        destinations.append(index[edge.dst_account])
        values.append(edge.amount.minor)
        shares.append(edge.amount.minor / total if total > 0 else 0.0)
    return PropagationGraph(
        accounts=accounts,
        seed_account=seed_account,
        indptr=tuple(indptr),
        destinations=tuple(destinations),
        values=tuple(values),
        value_share=tuple(shares),
        seed_amount=seed_amount,
        currency=cfg.currency,
        excluded_edges=excluded,
    )


def build_component_graph_from_frame(
    frame: pl.DataFrame,
    *,
    component: Sequence[str],
    seed_account: str,
    seed_amount: Money,
    cfg: Economics,
) -> PropagationGraph:
    """Build the propagation graph from canonical event rows."""
    columns = set(frame.columns)
    missing = sorted(_EDGE_FRAME_COLUMNS - columns)
    if missing:
        raise PropagationError(
            f"the edge frame is missing {missing}; it must carry the canonical event "
            f"columns {sorted(_EDGE_FRAME_COLUMNS)}, found {sorted(columns)}"
        )
    edges = [flow_edge_from_row(row) for row in frame.iter_rows(named=True)]
    return build_propagation_graph(
        edges,
        component=component,
        seed_account=seed_account,
        seed_amount=seed_amount,
        cfg=cfg,
    )


@dataclass(frozen=True, slots=True)
class ExposureInterval:
    """The 90 % interval on unactioned exposure, plus the settings that made it.

    Endpoints only, deliberately: no field here can be rendered as a point
    estimate, because plan §11 makes the interval the deliverable and a mean on the
    case page becomes the headline within a day.
    """

    low: Money
    high: Money
    level: float
    runs: int
    seed: int
    max_depth: int
    node_count: int
    edge_count: int
    excluded_edges: int

    def __post_init__(self) -> None:
        if self.high.minor < self.low.minor:
            raise PropagationError(
                f"interval endpoints are inverted: {self.low} below {self.high}"
            )

    @property
    def width(self) -> Money:
        """Interval width: how much is unresolved, in minor units."""
        return self.high - self.low

    def assumption_lines(self) -> tuple[str, ...]:
        """The provenance that must sit under the interval wherever it is shown."""
        return (
            f"Monte Carlo propagation of unactioned exposure: {self.runs:,} seeded runs "
            f"(seed {self.seed}), depth capped at {self.max_depth}, over {self.node_count} "
            f"accounts and {self.edge_count} directed value movements, "
            f"{self.excluded_edges} component-external edges excluded",
            f"{self.level:.0%} interval by nearest-rank quantile, so both endpoints are "
            "amounts that occurred in a simulated run rather than interpolated between two",
            "each outgoing edge transmits with probability equal to its share of the "
            "node's observed outflow; this is a simulation of a model, not a measurement",
        )

    def figures(self, cfg: Economics) -> tuple[CurrencyFigure, CurrencyFigure]:
        """The two endpoints as currency figures, each carrying the assumption block.

        The endpoints do not move with ``r`` — the recovery rate is applied to the
        interval downstream — so the band is shown at its three configured values
        with the reason stated, rather than the figure being dropped to avoid the
        question.
        """
        block = assumption_block(cfg)
        note = (
            f"the interval is over simulated propagation, not over r; both endpoints "
            f"appear at each of the {BAND_WIDTH} configured recovery rates because every "
            "currency figure carries the band"
        )
        provenance = (*self.assumption_lines(), note)
        return (
            currency_figure(
                "Exposure if unactioned, lower bound of the interval",
                dict.fromkeys(block.rates, self.low),
                block,
                provenance=provenance,
            ),
            currency_figure(
                "Exposure if unactioned, upper bound of the interval",
                dict.fromkeys(block.rates, self.high),
                block,
                provenance=provenance,
            ),
        )

    def render(self, cfg: Economics) -> str:
        """Both endpoints, their provenance, and the assumption block once."""
        low, high = self.figures(cfg)
        value_lines = [low.render().splitlines()[0], high.render().splitlines()[0]]
        return "\n".join([*value_lines, *self.assumption_lines(), assumption_block(cfg).text])


def _quantile_pair(totals: np.ndarray, lower: float, upper: float) -> tuple[int, int]:
    """Nearest-rank quantiles of the simulated totals, as integer minor units.

    ``method='nearest'`` because an interpolated quantile is a money amount that
    never occurred in any run, and the disclaimer already says these are model
    estimates; inventing a second layer of precision on top of that would not
    improve them.
    """
    low_value = np.quantile(totals, lower, method="nearest")
    high_value = np.quantile(totals, upper, method="nearest")
    return int(low_value), int(high_value)


def simulate_exposure_interval(
    graph: PropagationGraph,
    cfg: Economics,
    *,
    runs: int | None = None,
    max_depth: int | None = None,
    seed: int | None = None,
) -> ExposureInterval:
    """Run the propagation and return the configured interval.

    ``runs``, ``max_depth`` and ``seed`` default to ``config/economics.yaml`` and
    exist so a sensitivity sweep can move one at a time; the product path passes
    none of them, which is what keeps the case page's interval the configured one
    rather than whichever number a call site felt like.
    """
    settings = cfg.monte_carlo
    resolved_runs = settings.runs if runs is None else runs
    resolved_depth = settings.max_depth if max_depth is None else max_depth
    resolved_seed = settings.seed if seed is None else seed
    if resolved_runs < 1:
        raise PropagationError(f"runs must be >= 1, got {resolved_runs}")
    if resolved_depth < 1:
        raise PropagationError(f"max_depth must be >= 1, got {resolved_depth}")
    if graph.edge_count == 0:
        raise PropagationError(
            f"component around {graph.seed_account} has no internal edges to propagate "
            "along. An account in isolation has no network exposure, and a degenerate "
            "interval here would read as a finding."
        )
    indptr, destinations, value_share = graph.arrays()
    totals = _propagate_kernel(
        graph.seed_index,
        indptr,
        destinations,
        value_share,
        graph.seed_amount.minor,
        resolved_runs,
        resolved_depth,
        graph.node_count,
    )
    low, high = _quantile_pair(totals, settings.lower_quantile, settings.upper_quantile)
    return ExposureInterval(
        low=Money(low, graph.currency),
        high=Money(high, graph.currency),
        level=settings.upper_quantile - settings.lower_quantile,
        runs=resolved_runs,
        seed=resolved_seed,
        max_depth=resolved_depth,
        node_count=graph.node_count,
        edge_count=graph.edge_count,
        excluded_edges=graph.excluded_edges,
    )


__all__ = [
    "ExposureInterval",
    "PropagationError",
    "PropagationGraph",
    "build_component_graph_from_frame",
    "build_propagation_graph",
    "simulate_exposure_interval",
]
