"""Typed, fail-loud read of the ``graph:`` block.

Why this module exists separately from :mod:`oxbow.config`: ``PipelineConfig``
hands out ``dict`` sections, and a dict read deep inside an algorithm is where a
typo becomes a default. Every tunable the graph obeys is resolved once, here, with
its config path named in the error. 00 G: tunables live in ``config/``, and 03 A
rule 1: fail loud at the boundary.

Three validations are stricter than they look and are here on purpose:

* ``community.seed`` must equal the global ``seed``. A per-stage seed that
  disagrees with the run seed produces a graph that is reproducible but not the
  same graph twice — which reads as a flaky test rather than a config bug.
* ``resolution`` must be 1.0 when the objective is modularity, because
  ``leidenalg`` ignores resolution under modularity. A knob that silently does
  nothing is worse than a missing knob.
* ``sort_keys`` must be exactly the pair ``(instant, txn_id)``. The total order is
  the spine of every guarantee in this layer (P3a), so an unexpected third key is
  refused rather than quietly dropped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from oxbow.config import PipelineConfig
from oxbow.graph.errors import GraphConfigError

GRAPH_SECTION: Final = "graph"
EXPOSURE_SECTION: Final = "exposure"

# The canonical event v1 renamed the ordering instant, but ``determinism.sort_keys``
# in config still declares it by its logical name (and ``ports/source.py`` still
# publishes that name — DEV-012). One alias table, stated once, is the seam; a
# rename on either side lands here as an unknown key and raises.
SORT_KEY_ALIASES: Final[Mapping[str, str]] = {
    "ts_utc": "event_ts_utc",
    "event_ts_utc": "event_ts_utc",
    "txn_id": "txn_id",
}

# The only order this layer will accept: the UTC instant, broken by the
# transaction id. Never `local_hour` — 03 C forbids reasoning about human hours
# with an ordering column, and vice versa.
CANONICAL_ORDER: Final[tuple[str, str]] = ("event_ts_utc", "txn_id")

MODULARITY_OBJECTIVE: Final = "modularity"
CPM_OBJECTIVE: Final = "cpm"
SIZE_THEN_MIN_NODE_KEY: Final = "size_then_min_node_key"
LEIDEN_ALGORITHM: Final = "leiden"


def _lookup(section: Mapping[str, object], path: str, *, owner: str) -> object:
    value = section.get(path)
    if value is None or (isinstance(value, float) and value != value):
        raise GraphConfigError(f"config {owner}.{path} is missing; no default is substituted")
    return value


def _string(section: Mapping[str, object], path: str, *, owner: str) -> str:
    value = _lookup(section, path, owner=owner)
    if not isinstance(value, str) or not value.strip():
        raise GraphConfigError(f"config {owner}.{path} must be a non-empty string, got {value!r}")
    return value


def _boolean(section: Mapping[str, object], path: str, *, owner: str) -> bool:
    value = _lookup(section, path, owner=owner)
    if not isinstance(value, bool):
        raise GraphConfigError(f"config {owner}.{path} must be a bool, got {type(value).__name__}")
    return value


def _integer(section: Mapping[str, object], path: str, *, owner: str, minimum: int) -> int:
    value = _lookup(section, path, owner=owner)
    # bool is a subclass of int in Python, so `n_iterations: true` would otherwise
    # sail through as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraphConfigError(f"config {owner}.{path} must be an int, got {value!r}")
    if value < minimum:
        raise GraphConfigError(f"config {owner}.{path} must be >= {minimum}, got {value}")
    return value


def _ratio(section: Mapping[str, object], path: str, *, owner: str) -> float:
    """A dimensionless number in [0, 1]: a retention floor or a damping factor."""
    value = _lookup(section, path, owner=owner)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GraphConfigError(f"config {owner}.{path} must be a number in [0, 1], got {value!r}")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise GraphConfigError(f"config {owner}.{path} must be in [0, 1], got {number}")
    return number


def _percentile(section: Mapping[str, object], path: str, *, owner: str) -> float:
    value = _lookup(section, path, owner=owner)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GraphConfigError(f"config {owner}.{path} must be a number in (0, 100), got {value!r}")
    number = float(value)
    if not 0.0 < number < 100.0:
        raise GraphConfigError(f"config {owner}.{path} must be in (0, 100), got {number}")
    return number


def _section(section: Mapping[str, object], path: str, *, owner: str) -> Mapping[str, object]:
    value = _lookup(section, path, owner=owner)
    if not isinstance(value, Mapping):
        raise GraphConfigError(
            f"config {owner}.{path} must be a mapping, got {type(value).__name__}"
        )
    return {str(key): item for key, item in value.items()}


def _string_tuple(section: Mapping[str, object], path: str, *, owner: str) -> tuple[str, ...]:
    value = _lookup(section, path, owner=owner)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GraphConfigError(f"config {owner}.{path} must be a list of strings, got {value!r}")
    # Sorted, not source order: this tuple is compared against event payloads, and
    # two runs whose YAML is written in a different order must behave identically.
    return tuple(sorted(value))


@dataclass(frozen=True, slots=True)
class CommunitySettings:
    """Leiden parameters. Seed and iteration count are both fixed, so the same
    account lands in the same community across runs and can be given the same
    explanation twice (P3a).
    """

    algorithm: str
    seed: int
    n_iterations: int
    objective_function: str
    resolution: float
    canonical_order: str


@dataclass(frozen=True, slots=True)
class PageRankSettings:
    """Damping and iteration ceiling for the global-centrality feature."""

    alpha: float
    max_iter: int


@dataclass(frozen=True, slots=True)
class BetweennessSettings:
    """Pivot count for the sampled betweenness approximation."""

    pivots: int


@dataclass(frozen=True, slots=True)
class CycleSettings:
    """The bounded, time-respecting, value-retaining cycle search.

    ``max_visits_per_component`` and ``timeout_seconds`` are not tuning knobs.
    Without them the enumeration does not finish, and a pipeline that hangs is
    indistinguishable from one that is working.
    """

    min_length: int
    max_length: int
    value_retention_floor: float
    require_non_increasing: bool
    ignore_rails: bool
    excluded_txn_types: tuple[str, ...]
    max_visits_per_component: int
    max_cycles_reported: int
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ParquetSettings:
    """The writer settings that make two runs byte-identical (01 A rule 4)."""

    row_group_size: int
    compression: str
    compression_level: int
    hash_algorithm: str


@dataclass(frozen=True, slots=True)
class GraphSettings:
    """Everything the graph layer reads from ``config/pipeline.yaml``.

    Frozen and built once, so no stage can shift the rail percentile out from
    under a later one.
    """

    rail_degree_percentile: float
    subgraph_node_cap: int
    default_hops: int
    order_columns: tuple[str, str]
    interactive_txn_target: int
    exposure_window_hours: int
    exposure_downstream_hops: int
    community: CommunitySettings
    page_rank: PageRankSettings
    betweenness: BetweennessSettings
    cycles: CycleSettings
    parquet: ParquetSettings

    @property
    def exposure_window_us(self) -> int:
        """The recovery window in microseconds, the unit the edge table keeps."""
        return self.exposure_window_hours * 3_600 * 1_000_000


def resolve_order_columns(determinism: Mapping[str, object]) -> tuple[str, str]:
    """Map the declared logical sort keys onto the canonical event's columns."""
    declared = _lookup(determinism, "sort_keys", owner="determinism")
    if not isinstance(declared, list) or not all(isinstance(item, str) for item in declared):
        raise GraphConfigError(
            f"config determinism.sort_keys must be a list of strings, got {declared!r}"
        )
    if len(declared) != 2:
        raise GraphConfigError(
            "config determinism.sort_keys must name exactly two columns "
            f"(instant, txn_id); got {declared!r}. The graph's total order depends "
            "on both and on nothing else."
        )
    resolved: list[str] = []
    for name in declared:
        physical = SORT_KEY_ALIASES.get(str(name))
        if physical is None:
            raise GraphConfigError(
                f"config determinism.sort_keys names {name!r}, which is not a column "
                f"the graph layer knows. Known keys: {sorted(SORT_KEY_ALIASES)}"
            )
        resolved.append(physical)
    if tuple(resolved) != CANONICAL_ORDER:
        raise GraphConfigError(
            f"config determinism.sort_keys resolves to {tuple(resolved)}, but the graph "
            f"orders strictly on {CANONICAL_ORDER} (P3a). Never order on local_hour."
        )
    return CANONICAL_ORDER


def load_graph_settings(cfg: PipelineConfig) -> GraphSettings:
    """Build :class:`GraphSettings` from a loaded pipeline config, or raise."""
    graph = _section(cfg.raw, GRAPH_SECTION, owner="config")
    determinism = _section(cfg.raw, "determinism", owner="config")
    sampling = _section(cfg.raw, "sampling", owner="config")
    exposure = _section(cfg.raw, EXPOSURE_SECTION, owner="config")

    community_raw = _section(graph, "community", owner="graph")
    pagerank_raw = _section(graph, "page_rank", owner="graph")
    betweenness_raw = _section(graph, "betweenness", owner="graph")
    cycles_raw = _section(graph, "cycles", owner="graph")

    algorithm = _string(community_raw, "algorithm", owner="graph.community")
    if algorithm != LEIDEN_ALGORITHM:
        raise GraphConfigError(
            f"config graph.community.algorithm is {algorithm!r}, but the only community "
            "implementation this layer ships is 'leiden' (igraph + leidenalg). Falling "
            "back to a different algorithm silently would change every explanation."
        )
    seed = _integer(community_raw, "seed", owner="graph.community", minimum=0)
    if seed != cfg.seed:
        raise GraphConfigError(
            f"config graph.community.seed is {seed} but the run seed is {cfg.seed}. "
            "01 A rule 4 fixes one seed everywhere: two runs would otherwise produce "
            "different communities and the phrase 'stable between runs' would be false."
        )
    objective = _string(community_raw, "objective_function", owner="graph.community")
    if objective not in {MODULARITY_OBJECTIVE, CPM_OBJECTIVE}:
        raise GraphConfigError(
            f"config graph.community.objective_function is {objective!r}; expected "
            f"'{MODULARITY_OBJECTIVE}' or '{CPM_OBJECTIVE}'."
        )
    resolution = _percentile_or_unit(objective, community_raw)
    canonical_order = _string(community_raw, "canonical_order", owner="graph.community")
    if canonical_order != SIZE_THEN_MIN_NODE_KEY:
        raise GraphConfigError(
            f"config graph.community.canonical_order is {canonical_order!r}; the only "
            f"remap this layer implements is '{SIZE_THEN_MIN_NODE_KEY}'. Any other order "
            "relabels communities between runs, which is the defect the remap exists to "
            "prevent."
        )

    min_length = _integer(cycles_raw, "min_length", owner="graph.cycles", minimum=3)
    max_length = _integer(cycles_raw, "max_length", owner="graph.cycles", minimum=min_length)
    if max_length > 12:
        # A 13-hop round robin is not a typology, it is a search that will not
        # terminate inside any honest budget.
        raise GraphConfigError(
            f"config graph.cycles.max_length is {max_length}, above the 12 the layer "
            "will enumerate: beyond that the per-component budget is always exhausted "
            "and every reported cycle count is a truncation artefact."
        )

    return GraphSettings(
        rail_degree_percentile=_percentile(graph, "rail_degree_percentile", owner="graph"),
        subgraph_node_cap=_integer(graph, "subgraph_node_cap", owner="graph", minimum=2),
        default_hops=_integer(graph, "default_hops", owner="graph", minimum=1),
        order_columns=resolve_order_columns(determinism),
        interactive_txn_target=_integer(
            sampling, "interactive_txn_target", owner="sampling", minimum=1
        ),
        exposure_window_hours=_integer(exposure, "window_hours", owner="exposure", minimum=1),
        exposure_downstream_hops=_integer(exposure, "downstream_hops", owner="exposure", minimum=0),
        community=CommunitySettings(
            algorithm=algorithm,
            seed=seed,
            n_iterations=_integer(
                community_raw, "n_iterations", owner="graph.community", minimum=1
            ),
            objective_function=objective,
            resolution=resolution,
            canonical_order=canonical_order,
        ),
        page_rank=PageRankSettings(
            alpha=_ratio(pagerank_raw, "alpha", owner="graph.page_rank"),
            max_iter=_integer(pagerank_raw, "max_iter", owner="graph.page_rank", minimum=1),
        ),
        betweenness=BetweennessSettings(
            pivots=_integer(betweenness_raw, "pivots", owner="graph.betweenness", minimum=1)
        ),
        cycles=CycleSettings(
            min_length=min_length,
            max_length=max_length,
            value_retention_floor=_ratio(cycles_raw, "value_retention_floor", owner="graph.cycles"),
            require_non_increasing=_boolean(
                cycles_raw, "require_non_increasing", owner="graph.cycles"
            ),
            ignore_rails=_boolean(cycles_raw, "ignore_rails", owner="graph.cycles"),
            excluded_txn_types=_string_tuple(
                cycles_raw, "excluded_txn_types", owner="graph.cycles"
            ),
            max_visits_per_component=_integer(
                cycles_raw, "max_visits_per_component", owner="graph.cycles", minimum=1
            ),
            max_cycles_reported=_integer(
                cycles_raw, "max_cycles_reported", owner="graph.cycles", minimum=1
            ),
            timeout_seconds=_integer(
                cycles_raw, "timeout_seconds", owner="graph.cycles", minimum=1
            ),
        ),
        parquet=ParquetSettings(
            row_group_size=_integer(
                determinism, "parquet_row_group_size", owner="determinism", minimum=1
            ),
            compression=_string(determinism, "parquet_compression", owner="determinism"),
            compression_level=_integer(
                determinism, "parquet_compression_level", owner="determinism", minimum=1
            ),
            hash_algorithm=_string(determinism, "hash_algorithm", owner="determinism"),
        ),
    )


def _percentile_or_unit(objective: str, community_raw: Mapping[str, object]) -> float:
    """Read ``resolution``, refusing the one case where it has no effect."""
    resolution = _ratio(community_raw, "resolution", owner="graph.community")
    if objective == MODULARITY_OBJECTIVE and resolution != 1.0:
        raise GraphConfigError(
            f"config graph.community.resolution is {resolution} under objective_function "
            f"'{MODULARITY_OBJECTIVE}'. leidenalg ignores resolution for modularity, so "
            "the knob would do nothing while looking like it did something. Set "
            "resolution: 1.0 or switch objective_function to 'cpm'."
        )
    return resolution


__all__ = [
    "CANONICAL_ORDER",
    "CPM_OBJECTIVE",
    "LEIDEN_ALGORITHM",
    "MODULARITY_OBJECTIVE",
    "SIZE_THEN_MIN_NODE_KEY",
    "SORT_KEY_ALIASES",
    "BetweennessSettings",
    "CommunitySettings",
    "CycleSettings",
    "GraphSettings",
    "PageRankSettings",
    "ParquetSettings",
    "load_graph_settings",
    "resolve_order_columns",
]
