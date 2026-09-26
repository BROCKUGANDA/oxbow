"""Graph to Parquet and back, so the API reads a built graph instead of building one.

Two guarantees, and the second is the one that decides the design:

**Determinism.** ``make verify-determinism`` re-runs a completed pipeline and
compares digests, so the artifact bytes must be a pure function of the events and
the config. That rules out three things that would otherwise be obvious: the
compression level and row-group size come from ``determinism`` rather than from
pyarrow's defaults; the manifest is sorted-key JSON with no timestamps; and no
``datetime.now()`` appears anywhere in the write path. Wall-clock lives in the run
record and the warehouse, per DEV-012, not in the artifact.

**Self-describing reads.** The manifest carries a SHA-256 and a row count per file,
and :func:`load_graph` verifies both before returning a frame. The API and the
explorer render a graph they did not build, so a truncated or stale artifact has to
fail at read time — a half-written graph otherwise renders as a real one, and a
missing community is indistinguishable from no community on screen.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import polars as pl

from oxbow.graph.errors import GraphArtifactError
from oxbow.graph.model import AccountGraph, Cycle, DegreeSummary, GraphStats
from oxbow.graph.settings import ParquetSettings

ARTIFACT_VERSION: Final = "oxbow-graph-v1"
MANIFEST_NAME: Final = "manifest.json"

NODES_FILE: Final = "nodes.parquet"
EDGES_FILE: Final = "edges.parquet"
PAIRS_FILE: Final = "pairs.parquet"
FLOWS_FILE: Final = "flows.parquet"
CYCLES_FILE: Final = "cycles.parquet"

ARTIFACT_FILES: Final[tuple[tuple[str, str], ...]] = (
    (NODES_FILE, "nodes"),
    (EDGES_FILE, "edges"),
    (PAIRS_FILE, "pairs"),
    (FLOWS_FILE, "flows"),
    (CYCLES_FILE, "cycles"),
)


@dataclass(frozen=True, slots=True)
class GraphArtifacts:
    """The persisted graph, as frames. Enough to render, not enough to re-derive."""

    nodes: pl.DataFrame
    edges: pl.DataFrame
    pairs: pl.DataFrame
    flows: pl.DataFrame
    cycles: pl.DataFrame
    stats: GraphStats

    def accounts_in_community(self, community_id: int) -> tuple[str, ...]:
        """Expand a collapsed meta-node without touching the build path."""
        rows = self.nodes.filter(pl.col("community_id") == community_id)["account"].to_list()
        return tuple(sorted(str(account) for account in rows))


@dataclass(frozen=True, slots=True)
class FileDigest:
    name: str
    rows: int
    sha256: str
    columns: tuple[str, ...]


def write_graph(graph: AccountGraph, out_dir: Path) -> Path:
    """Persist the five frames plus a manifest, and return the directory.

    Idempotent: the same graph written twice produces the same bytes at the same
    paths, which is what lets a resumed run skip the stage instead of trusting a
    checkpoint it cannot verify.
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    rows = {
        NODES_FILE: graph.nodes,
        EDGES_FILE: graph.events.frame,
        PAIRS_FILE: graph.pairs,
        FLOWS_FILE: graph.flows,
        CYCLES_FILE: cycles_to_frame(graph),
    }
    files: list[FileDigest] = []
    for name in sorted(rows):
        frame = rows[name]
        target = directory / name
        _write_parquet(frame, target, settings=graph.settings.parquet)
        files.append(
            FileDigest(
                name=name,
                rows=frame.height,
                sha256=_sha256(target),
                columns=tuple(frame.columns),
            )
        )
    manifest: dict[str, object] = {
        "artifact_version": ARTIFACT_VERSION,
        "stats": graph.stats.as_json_dict(),
        "files": [
            {
                "name": item.name,
                "rows": item.rows,
                "sha256": item.sha256,
                "columns": list(item.columns),
            }
            for item in sorted(files, key=lambda item: item.name)
        ],
    }
    (directory / MANIFEST_NAME).write_text(_dump(manifest), encoding="utf-8")
    return directory


def load_graph(directory: Path) -> GraphArtifacts:
    """Read a persisted graph, verifying every file against its own manifest."""
    root = Path(directory)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise GraphArtifactError(
            f"{manifest_path} is missing. Refusing to guess which Parquet files belong "
            "to this artifact: a graph read without its manifest cannot be checked for "
            "truncation, and a truncated graph renders as a real one."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("artifact_version") != ARTIFACT_VERSION:
        raise GraphArtifactError(
            f"{manifest_path} declares {manifest.get('artifact_version')!r}, this build "
            f"reads {ARTIFACT_VERSION!r}. Reconcile deliberately; do not read forward."
        )
    declared = {str(item["name"]): item for item in manifest["files"]}
    expected = {name for name, _ in ARTIFACT_FILES}
    unknown = sorted(set(declared) - expected)
    missing = sorted(expected - set(declared))
    if missing or unknown:
        raise GraphArtifactError(
            f"manifest files disagree with the artifact contract: missing={missing} "
            f"unexpected={unknown}"
        )

    frames: dict[str, pl.DataFrame] = {}
    for name, attribute in ARTIFACT_FILES:
        path = root / name
        if not path.is_file():
            raise GraphArtifactError(f"{path} is listed in the manifest but absent")
        entry = declared[name]
        digest = _sha256(path)
        if digest != str(entry["sha256"]):
            raise GraphArtifactError(
                f"{path} sha256 is {digest}, manifest says {entry['sha256']}. Truncated "
                "or rewritten artifact; rebuilding is the only safe answer."
            )
        frame = pl.read_parquet(path)
        if frame.height != int(entry["rows"]):
            raise GraphArtifactError(
                f"{path} holds {frame.height} rows, manifest says {entry['rows']}"
            )
        columns = tuple(str(item) for item in entry["columns"])
        if frame.columns != list(columns):
            raise GraphArtifactError(
                f"{path} columns {frame.columns} do not match the manifest {list(columns)}"
            )
        frames[attribute] = frame
    return GraphArtifacts(
        nodes=frames["nodes"],
        edges=frames["edges"],
        pairs=frames["pairs"],
        flows=frames["flows"],
        cycles=frames["cycles"],
        stats=stats_from_json(dict(manifest["stats"])),
    )


def cycles_to_frame(graph: AccountGraph) -> pl.DataFrame:
    """The surviving cycles as a columnar table, in report order.

    ``path`` and the leg arrays are lists per row: a cycle is one evidence object,
    and flattening it to one row per leg would let a consumer count the legs and
    report forty cycles where the search found six.
    """
    cycles: tuple[Cycle, ...] = graph.search.cycles
    if not cycles:
        return pl.DataFrame(
            schema={
                "canonical_key": pl.List(pl.Utf8),
                "length": pl.Int64,
                "currency": pl.Utf8,
                "amounts_minor": pl.List(pl.Int64),
                "ts_us": pl.List(pl.Int64),
                "txn_ids": pl.List(pl.Utf8),
                "first_amount_minor": pl.Int64,
                "last_amount_minor": pl.Int64,
                "value_retention": pl.Float64,
                "truncated": pl.Boolean,
                "truncation_reason": pl.Utf8,
            }
        )
    return pl.DataFrame(
        {
            "canonical_key": [list(cycle.canonical_key) for cycle in cycles],
            "length": [cycle.length for cycle in cycles],
            "currency": [cycle.currency for cycle in cycles],
            "amounts_minor": [list(cycle.amounts_minor) for cycle in cycles],
            "ts_us": [list(cycle.ts_us) for cycle in cycles],
            "txn_ids": [list(cycle.txn_ids) for cycle in cycles],
            "first_amount_minor": [cycle.first_amount_minor for cycle in cycles],
            "last_amount_minor": [cycle.last_amount_minor for cycle in cycles],
            "value_retention": [cycle.value_retention for cycle in cycles],
            # Carried per row so a reader who only has this file still learns the
            # count is a lower bound rather than a total.
            "truncated": [graph.search.truncated] * len(cycles),
            "truncation_reason": [graph.search.truncation_reason] * len(cycles),
        }
    )


# The codecs this writer will run, named exactly as polars accepts them. The config
# key is a plain string, so an unknown codec has to fail here rather than be swapped
# for a default: a differently compressed artifact is a different checksum, and
# ``verify-determinism`` compares checksums.
PARQUET_CODECS: Final[Mapping[str, Literal["zstd", "snappy", "gzip"]]] = {
    "zstd": "zstd",
    "snappy": "snappy",
    "gzip": "gzip",
}


def _write_parquet(frame: pl.DataFrame, target: Path, *, settings: ParquetSettings) -> None:
    """Write with the configured codec, level and row groups, nothing else.

    Every one of those three is a byte-level decision, so all three come from
    ``determinism`` rather than from a library default: a row-group size that changes
    is a checksum that changes. The writer's own version string is embedded in file
    metadata and is stable on a given machine — stated here rather than hidden,
    because a determinism failure nobody was warned about is a design choice.
    """
    codec = PARQUET_CODECS.get(settings.compression)
    if codec is None:
        raise GraphArtifactError(
            f"determinism.parquet_compression is {settings.compression!r}; this writer "
            f"implements {sorted(PARQUET_CODECS)}. Add the codec deliberately — the "
            "config already names it, so a silent substitution would change every "
            "checksum without changing any number."
        )
    frame.write_parquet(
        target,
        compression=codec,
        compression_level=settings.compression_level,
        row_group_size=settings.row_group_size,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dump(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n"


def stats_from_json(payload: dict[str, object]) -> GraphStats:
    """Rebuild the typed stats record from the manifest, checking every key.

    Explicit rather than ``GraphStats(**payload)``: a manifest written by a future
    version of this layer should fail here with the field named, not silently gain or
    lose a number that the UI then renders as truth.
    """
    wanted = {
        "event_count",
        "self_transfer_count",
        "self_transfer_value_minor",
        "edge_count",
        "pair_count",
        "node_count",
        "member_count",
        "external_count",
        "rail_count",
        "singleton_count",
        "in_graph_node_count",
        "rail_degree_threshold",
        "rail_degree_percentile",
        "currencies",
        "cycle_count",
        "cycle_search_truncated",
        "cycle_search_reason",
        "cycle_visits",
        "cycle_nodes",
        "community_count",
        "community_skipped_reason",
        "pagerank_skipped_reason",
        "degree_all_nodes",
        "degree_in_graph",
        "top_degrees",
        "settings_fingerprint",
    }
    absent = sorted(wanted - set(payload))
    extra = sorted(set(payload) - wanted)
    if absent or extra:
        raise GraphArtifactError(
            f"manifest stats do not match the GraphStats contract: missing={absent} "
            f"unexpected={extra}"
        )
    return GraphStats(
        event_count=_int(payload["event_count"]),
        self_transfer_count=_int(payload["self_transfer_count"]),
        self_transfer_value_minor=tuple(
            (
                str(_items(pair, "self_transfer_value_minor entry")[0]),
                _int(_items(pair, "self_transfer_value_minor entry")[1]),
            )
            for pair in _items(payload["self_transfer_value_minor"], "self_transfer_value_minor")
        ),
        edge_count=_int(payload["edge_count"]),
        pair_count=_int(payload["pair_count"]),
        node_count=_int(payload["node_count"]),
        member_count=_int(payload["member_count"]),
        external_count=_int(payload["external_count"]),
        rail_count=_int(payload["rail_count"]),
        singleton_count=_int(payload["singleton_count"]),
        in_graph_node_count=_int(payload["in_graph_node_count"]),
        rail_degree_threshold=_int(payload["rail_degree_threshold"]),
        rail_degree_percentile=float(str(payload["rail_degree_percentile"])),
        currencies=tuple(str(item) for item in _items(payload["currencies"], "currencies")),
        cycle_count=_int(payload["cycle_count"]),
        cycle_search_truncated=bool(payload["cycle_search_truncated"]),
        cycle_search_reason=_optional_str(payload["cycle_search_reason"]),
        cycle_visits=_int(payload["cycle_visits"]),
        cycle_nodes=_int(payload["cycle_nodes"]),
        community_count=_int(payload["community_count"]),
        community_skipped_reason=_optional_str(payload["community_skipped_reason"]),
        pagerank_skipped_reason=_optional_str(payload["pagerank_skipped_reason"]),
        degree_all_nodes=_degree(payload["degree_all_nodes"]),
        degree_in_graph=_degree(payload["degree_in_graph"]),
        top_degrees=tuple(
            (str(_items(pair, "top_degrees entry")[0]), _int(_items(pair, "top_degrees entry")[1]))
            for pair in _items(payload["top_degrees"], "top_degrees")
        ),
        settings_fingerprint={
            str(key): item for key, item in _mapping(payload["settings_fingerprint"]).items()
        },
    )


def _items(value: object, label: str) -> tuple[object, ...]:
    """A manifest list as a tuple, or a loud failure naming what was wrong."""
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise GraphArtifactError(f"manifest field {label} should be a list, got {value!r}")
    return tuple(value)


def _mapping(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise GraphArtifactError(f"manifest field should be a mapping, got {value!r}")
    return {str(key): item for key, item in value.items()}


def _int(value: object) -> int:
    if isinstance(value, bool):
        raise GraphArtifactError(f"expected an integer in the manifest, got a bool: {value!r}")
    try:
        return int(str(value))
    except ValueError as exc:
        raise GraphArtifactError(f"manifest holds {value!r}, which is not a count") from exc


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _degree(value: object) -> DegreeSummary:
    payload = _mapping(value)
    for key in ("accounts", "median", "p90", "p99", "max"):
        if key not in payload:
            raise GraphArtifactError(f"manifest degree summary is missing {key!r}: {payload}")
    return DegreeSummary(
        accounts=_int(payload["accounts"]),
        median=float(str(payload["median"])),
        p90=float(str(payload["p90"])),
        p99=float(str(payload["p99"])),
        max=_int(payload["max"]),
    )


__all__ = [
    "ARTIFACT_FILES",
    "ARTIFACT_VERSION",
    "CYCLES_FILE",
    "EDGES_FILE",
    "FLOWS_FILE",
    "MANIFEST_NAME",
    "NODES_FILE",
    "PAIRS_FILE",
    "FileDigest",
    "GraphArtifacts",
    "cycles_to_frame",
    "load_graph",
    "write_graph",
]
