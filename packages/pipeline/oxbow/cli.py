"""The four pipeline stage verbs, wired to the packages they orchestrate.

01 D: ``oxbow ingest graph score backtest`` are the four stages, run separately and
resumably; ``make pipeline`` chains them and streams stage events so the UI ledger fills
with real rows and real elapsed times.

This module is the **composition root** (import-linter contract 4), which fixes what it
may and may not contain. It resolves the repo root, loads ``config/``, requires
``RUN_SALT``, chooses the adapter that lands bytes, and calls into the stage packages —
``ingest/run.py``, ``graph/``, ``rules/``, ``features/``, ``scoring/``, ``models/``,
``backtest/``, ``packet/``, ``eval.py``. Every number printed here is read off the object
the package returned, so a printed row count and a written artifact cannot tell two
different stories. No stage logic lives here: a gap inside a package is reported by name
at the boundary rather than improvised in the CLI, because a second implementation is a
second thing to keep true and the layer that owns the maths is the one that must.

Two failure disciplines run through the file.

* **Non-zero on a real failure.** A quarantine, an empty batch, an artifact whose SHA-256
  disagrees with its manifest, a refused source, a missing ``RUN_SALT`` — each exits
  non-zero. A stage that could not run because a package seam is unwired is emitted as
  ``unavailable`` with the missing module named, never as ``complete`` with zero rows.
* **Nothing half-written breaks the interface.** Packages other agents are editing are
  imported *inside* the verb bodies, so an import error in ``ingest/ibm_aml.py`` or
  ``features/build.py`` fails that verb loudly while ``oxbow --help`` still lists every
  command.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import polars as pl
import typer

from oxbow import __version__
from oxbow.adapters.io import dumps
from oxbow.config import (
    CONFIG_DIRNAME,
    ConfigError,
    PipelineConfig,
    find_repo_root,
    load_pipeline_config,
    resolve_run_salt,
)
from oxbow.identity import new_ulid, require_ulid, sha256_of_bytes
from oxbow.ports.warehouse import RunState, WarehouseSink
from oxbow.stage_events import StageEventEmitter, StageHandle

app = typer.Typer(
    name="oxbow",
    help=(
        "OXBOW - quantitative risk scoring and financial-crime intelligence for "
        "mobile-money networks. Research prototype on historical, de-identified data."
    ),
    no_args_is_help=True,
    add_completion=False,
)

STAGES: tuple[str, ...] = ("ingest", "graph", "score", "backtest")

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_REFUSED: Final = 2

DATA_DIRNAME: Final = "data"
INTERIM_DIRNAME: Final = "interim"
OUT_DIRNAME: Final = "out"
RAW_DIRNAME: Final = "raw"

# Default artifact directories under ``out/``, which is gitignored: these are evidence
# that a boundary was crossed, not files to commit (oxbow/adapters/io.py states the rule).
GRAPH_ARTIFACT_DIRNAME: Final = "graph"
FEATURE_ARTIFACT_DIRNAME: Final = "features"
RULE_ARTIFACT_DIRNAME: Final = "rules"
BACKTEST_ARTIFACT_DIRNAME: Final = "backtest"
PACKET_ARTIFACT_DIRNAME: Final = "packets"
CASE_SINK_DIRNAME: Final = "case_sink"
WAREHOUSE_DIRNAME: Final = "warehouse"
AUDIT_DIRNAME: Final = "audit"
LEDGER_FILENAME: Final = "stage_events.jsonl"

MODEL_VERSION_PREFIX: Final = "oxbow"
QUARANTINE_PREFIX: Final = "quarantine-"


class StageError(RuntimeError):
    """A stage measured a failure the operator must act on. Non-zero exit, always."""


class ArtifactError(StageError):
    """A landed artifact disagrees with the manifest that describes it."""


def _planned(stage: str) -> str:
    """The declared scope of each stage. Printed, never guessed at runtime."""
    return {
        "ingest": (
            "read declared sources through SourceAdapter, contract-check with Pandera, "
            "canonicalise to the canonical event v1, re-hash identifiers, quarantine "
            "the unmappable, write Parquet to data/interim and register DuckDB views"
        ),
        "graph": (
            "measure the degree distribution over the full corpus, build the directed "
            "time-stamped multigraph over the interactive slice, type rails and external "
            "nodes, enumerate time-respecting value-retaining cycles, detect communities, "
            "persist the graph artifact and read it back"
        ),
        "score": (
            "fire rules R1-R12, build the as-of-correct feature matrix through the "
            "registry, run the money, finite and label guards, fit the WOE scorecard, "
            "train the GBM and Isolation Forest, calibrate, fuse, persist SHAP, write "
            "scored rows with lineage"
        ),
        "backtest": (
            "walk forward over 5 purged folds with a 30-day embargo, price every alert, "
            "allocate under capacity, report economics, drawdown, VaR and ES"
        ),
    }[stage]


def _count(value: int) -> str:
    """Thousands-separated, because six million is unreadable as 6362620."""
    return f"{value:,}"


def _as_count(value: object, *, label: str) -> int:
    """Coerce a ledger or manifest figure to an int, or fail naming the field.

    DuckDB registrations, run manifests and stored ledger rows all hand back plain
    ``object`` values, and a silent ``None`` becoming ``0`` in a printed row count is the
    difference between an artifact and a claim about one (03 A rule 2).
    """
    if isinstance(value, bool) or value is None:
        raise ArtifactError(f"{label} is {value!r}; a count is never a bool or a null")
    try:
        return int(str(value))
    except ValueError as exc:
        raise ArtifactError(f"{label} holds {value!r}, which is not a count") from exc


def _repo_root(config_dir: Path | None) -> Path:
    """The directory holding ``config/pipeline.yaml``, or a refusal naming the search."""
    if config_dir is not None:
        return Path(config_dir).resolve().parent
    return find_repo_root()


def _config_hash(config_dir: Path) -> str:
    """A digest of every config file the run obeyed, in filename order.

    ``run.config_hash`` answers "which configuration produced this number" once ``config/``
    has moved on, so it covers the directory rather than ``pipeline.yaml`` alone: a rules
    threshold and a feature window are just as load-bearing as the seed.
    """
    parts = [
        f"{path.name}={sha256_of_bytes(path.read_bytes())}"
        for path in sorted(Path(config_dir).glob("*.yaml"))
    ]
    if not parts:
        raise ConfigError(
            f"{config_dir} holds no YAML to hash. A run with no config digest is a run "
            "nobody can reproduce, so this is a refusal and not an empty string."
        )
    return sha256_of_bytes("\n".join(parts).encode("utf-8"))


def _write_json(path: Path, payload: Mapping[str, object]) -> Path:
    """Write one deterministic JSON document: the shared encoder, never a local one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(payload) + "\n", encoding="utf-8")
    return path


@dataclass(slots=True)
class StageContext:
    """Everything a stage body needs that is not specific to that stage."""

    stage: str
    root: Path
    config_dir: Path
    cfg: PipelineConfig
    run_id: str
    salt: str
    emitter: StageEventEmitter
    dry_run: bool
    started_monotonic: float

    @property
    def out_root(self) -> Path:
        return self.root / OUT_DIRNAME

    def stage_dir(self, dirname: str) -> Path:
        """``out/<kind>/<run_id>/`` — one directory per run, so a resume finds its own bytes."""
        path = self.out_root / dirname / self.run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def echo(self, message: str, *, err: bool = False) -> None:
        """Print a stage line. ``err=True`` routes a refusal or a failure to stderr.

        The failure reporters below call this with ``err``; a signature that dropped it
        would raise TypeError from inside the exception handler and replace the real
        reason a stage stopped with a complaint about this function's arguments.
        """
        typer.echo(message, err=err)


def _open_context(
    stage: str,
    *,
    run_id: str | None,
    config_dir: Path | None,
    dry_run: bool,
    stream: bool,
) -> StageContext:
    """Resolve root, config, salt and run identity — or refuse loudly.

    Each of these is a startup failure rather than a default. A stage that guessed a config
    directory, an identity or a salt would produce a run that cannot be reproduced and
    cannot be joined to the corpus it claims to describe (03 A rule 1, 01 A rule 8).
    """
    try:
        root = _repo_root(config_dir)
        cfg = load_pipeline_config(root)
    except ConfigError as exc:
        typer.echo(f"[{stage}] REFUSED: {exc}", err=True)
        raise typer.Exit(code=EXIT_REFUSED) from exc
    try:
        salt = resolve_run_salt(root)
    except ConfigError as exc:
        typer.echo(f"[{stage}] REFUSED: {exc}", err=True)
        raise typer.Exit(code=EXIT_REFUSED) from exc
    try:
        identity = require_ulid(
            new_ulid() if not run_id else run_id.strip().upper(), field="run_id"
        )
    except ValueError as exc:
        typer.echo(f"[{stage}] REFUSED: {exc}", err=True)
        raise typer.Exit(code=EXIT_REFUSED) from exc
    # The salt is never printed, at any length or in any digest form: 03 §D keeps it
    # outside the repo precisely so that holding the artifacts is not holding the mapping.

    from oxbow.adapters.null.warehouse import NullWarehouse

    sink: WarehouseSink = NullWarehouse(root / OUT_DIRNAME)
    emitter = StageEventEmitter(sink=sink, run_id=identity, stream=stream, echo=typer.echo)
    context = StageContext(
        stage=stage,
        root=root,
        config_dir=root / CONFIG_DIRNAME,
        cfg=cfg,
        run_id=identity,
        salt=salt,
        emitter=emitter,
        dry_run=dry_run,
        started_monotonic=time.monotonic(),
    )
    typer.echo(f"[{stage}] repo root: {root}")
    typer.echo(f"[{stage}] run_id: {identity}")
    typer.echo(f"[{stage}] seed: {cfg.seed} | timezone: {cfg.deployment_timezone}")
    typer.echo(f"[{stage}] RUN_SALT: resolved (value not printed)")
    if not dry_run:
        ledger = emitter.ensure_run_open(
            seed=cfg.seed,
            timezone=cfg.deployment_timezone,
            config_hash=_config_hash(context.config_dir),
            model_version=f"{MODEL_VERSION_PREFIX}/{__version__}",
            provenance="pipeline",
            dataset_ref=(context.root / DATA_DIRNAME / INTERIM_DIRNAME).as_posix(),
        )
        typer.echo(f"[{stage}] ledger: run {ledger}")
    return context


def _run_stage(ctx: StageContext, name: str, body: Callable[[StageHandle], int]) -> int:
    """Execute one stage body under its ledger event, honouring ``--run-id``.

    Resume is the point of naming a run: a stage already reported ``complete`` under this
    run has a committed row count and its artifacts on disk, so re-running it would be a
    second run pretending to be one. A dry run touches neither the ledger nor the sink —
    nothing was measured, so nothing may be recorded.
    """
    if ctx.dry_run:
        return body(StageHandle(stage=name))
    done = ctx.emitter.completed_event(name)
    if done is not None:
        typer.echo(
            f"[{ctx.stage}] {name} is already complete under run {ctx.run_id} with "
            f"{_count(_as_count(done['rows'], label='ledger rows'))} rows in "
            f"{_count(_as_count(done['elapsed_ms'], label='ledger elapsed_ms'))} ms; "
            "resuming past it. A new --run-id forces a fresh run."
        )
        return EXIT_OK
    with ctx.emitter.stage(name) as handle:
        code = body(handle)
        if code and not (handle.refused or handle.failed):
            # A non-zero return that named no reason is still a failure the ledger must
            # carry; a silent ``complete`` with a red exit code is the exact lie the
            # ledger exists to prevent.
            handle.mark_failed(f"{name} exited {code} without recording what failed")
        return code


def _execute(ctx: StageContext, name: str, body: Callable[[StageHandle], int]) -> int:
    """Run one stage and always move the run to a terminal state.

    A crash that left ``run.state`` at ``running`` would hold the API's SSE stream open
    until its budget expired, which reads as a stalled pipeline rather than a failed one
    (03 §J). The exception still propagates: the ledger records the failure, the operator
    sees the traceback, and the exit code stays non-zero.
    """
    try:
        code = _run_stage(ctx, name, body)
    except Exception as exc:
        if not ctx.dry_run:
            ctx.emitter.finish(RunState.FAILED, error=f"{type(exc).__name__}: {exc}")
        raise
    if not ctx.dry_run:
        ctx.emitter.finish(RunState.FAILED if code else RunState.COMPLETE)
    return code


# --- reading the canonical table the ingest stage landed ------------------


@dataclass(frozen=True, slots=True)
class CanonicalBatch:
    """One Parquet batch as its run manifest declares it."""

    source_id: str
    batch_id: str
    path: Path
    rows: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CorpusLineage:
    """One source's landed run: the manifest's own numbers, before anyone re-derives them."""

    source_id: str
    run_id: str
    manifest_path: Path
    declared_rows: int
    quarantine_rows: int
    ingested_at: str
    batches: tuple[CanonicalBatch, ...]


def _landed_sources(root: Path) -> list[str]:
    """Every corpus with a run manifest on disk, in directory order."""
    interim = root / DATA_DIRNAME / INTERIM_DIRNAME
    if not interim.is_dir():
        raise ArtifactError(
            f"{interim} does not exist. Run `oxbow ingest` first: graph, score and backtest "
            "read the canonical table ingest lands, and none of them reads a raw corpus."
        )
    return sorted(path.name for path in interim.iterdir() if path.is_dir())


def _resolve_sources(ctx: StageContext, requested: Sequence[str]) -> list[str]:
    """Which corpora this stage reads, from the flag or from the config's default scope.

    The default is ``ingest.default_sources`` in ``config/pipeline.yaml`` rather than "every
    directory under data/interim", because a corpus whose adapter is mid-flight lands an
    empty or quarantined manifest, and a graph stage that silently swallowed it would be
    describing a corpus that does not exist. ``all`` is the explicit opt-in to read
    everything landed, and anything else is refused by name.
    """
    landed = _landed_sources(ctx.root)
    wanted = [item.strip() for item in requested if item.strip()]
    if not wanted:
        ingest_block = ctx.cfg.raw.get("ingest")
        if not isinstance(ingest_block, dict):
            raise ArtifactError("config/pipeline.yaml has no `ingest` block to read a scope from")
        configured = [str(item) for item in ingest_block.get("default_sources", ())]
        if not configured:
            raise ArtifactError(
                "config/pipeline.yaml ingest.default_sources is empty and no --source was "
                "given, so this stage has no declared corpus to read. Refusing to guess."
            )
        skipped = sorted(set(landed) - set(configured))
        if skipped:
            ctx.echo(
                f"[{ctx.stage}] sources read: {', '.join(configured)} (from "
                f"ingest.default_sources); landed but not read: {', '.join(skipped)}. "
                "Pass --source all to read every landed corpus."
            )
        return configured
    if "all" in wanted:
        return landed
    unknown = sorted(set(wanted) - set(landed))
    if unknown:
        raise ArtifactError(
            f"no run manifest for {unknown} under {ctx.root / DATA_DIRNAME / INTERIM_DIRNAME}. "
            f"Landed: {landed}. Refusing to read a corpus the operator did not ingest."
        )
    return wanted


def _read_lineage(root: Path, *, sources: Sequence[str]) -> list[CorpusLineage]:
    """Every ``data/interim/<source>/run_manifest.json``, or a refusal naming what is missing.

    The manifest is the only way a downstream stage learns which Parquet files belong to the
    run it is reading. The directory accumulates batches from earlier runs taken at other
    ``--limit``s, so globbing it would double-count the corpus while appearing to find more
    of it.
    """
    from oxbow.adapters.file.canonical_sink import RUN_MANIFEST_FILENAME

    interim = root / DATA_DIRNAME / INTERIM_DIRNAME
    directories = [interim / name for name in sources]
    lineages: list[CorpusLineage] = []
    for directory in directories:
        manifest_path = directory / RUN_MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise ArtifactError(
                f"{manifest_path} is missing. Refusing to read {directory} by globbing it: "
                "without a manifest there is no way to tell which batches belong to this "
                "run, and a corpus assembled by guessing is a corpus nobody can reproduce."
            )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        batches = tuple(
            CanonicalBatch(
                source_id=str(entry["source_dataset"]),
                batch_id=str(entry["batch_id"]),
                path=Path(str(entry["path"])),
                rows=int(entry["rows"]),
                sha256=str(entry["sha256"]),
            )
            for entry in payload.get("batches", ())
        )
        if not batches:
            raise ArtifactError(
                f"{manifest_path} declares no batches. An empty canonical table is not a "
                "quiet corpus: `ingest.allow_empty_batch` is false for the same reason."
            )
        quarantine_rows = int(payload["quarantine_count"])
        if quarantine_rows:
            raise ArtifactError(
                f"{manifest_path} records {quarantine_rows} quarantined row(s) for run "
                f"{payload['run_id']}. A quarantine is a row that would have become nothing "
                "silently; this stage will not build on a table that lost rows quietly."
            )
        lineages.append(
            CorpusLineage(
                source_id=str(payload["source"]["source_id"]),
                run_id=str(payload["run_id"]),
                manifest_path=manifest_path,
                declared_rows=int(payload["canonical_count"]),
                quarantine_rows=quarantine_rows,
                ingested_at=str(payload["ingested_at"]),
                batches=batches,
            )
        )
    if not lineages:
        raise ArtifactError(f"{interim} holds no source directory with a run manifest")
    return lineages


def _load_canonical_events(
    ctx: StageContext, *, sources: Sequence[str], with_sidecar: bool
) -> pl.DataFrame:
    """Read the declared batches, verifying each file's digest against its manifest.

    ``with_sidecar`` re-attaches DEV-012's two sidecar columns (``run_id``,
    ``ingested_at``), which the contract declares but the persisted Parquet deliberately
    omits so two runs of the same corpus are byte-comparable. The feature layer asks for
    the full contract, so the join happens here; it belongs in a canonical-table reader
    next to the writer (see the report's package gaps), not in every stage that reads one.
    """
    from oxbow.adapters.file.canonical_sink import sha256_of_file
    from oxbow.contracts.canonical_v1 import (
        CANONICAL_COLUMNS,
        PERSISTED_CANONICAL_COLUMNS,
    )

    lineages = _read_lineage(ctx.root, sources=sources)
    for lineage in lineages:
        ctx.echo(
            f"[{ctx.stage}] {lineage.source_id}: ingest run {lineage.run_id}, "
            f"{_count(lineage.declared_rows)} canonical rows across {len(lineage.batches)} "
            f"batch file(s), manifest {lineage.manifest_path}"
        )
        referenced = {batch.path.name for batch in lineage.batches}
        directory = lineage.manifest_path.parent
        unmanifested = sorted(
            path.name
            for path in directory.glob("*.parquet")
            if path.name not in referenced and not path.name.startswith(QUARANTINE_PREFIX)
        )
        if unmanifested:
            ctx.echo(
                f"[{ctx.stage}] {lineage.source_id}: NOTE {len(unmanifested)} Parquet file(s) "
                f"here are not in this manifest and are NOT read: {unmanifested[:5]}"
                f"{' ...' if len(unmanifested) > 5 else ''}. They are leftovers from earlier "
                "runs; reading the directory instead of the manifest would double-count the "
                "corpus."
            )
    frames: list[pl.DataFrame] = []
    for lineage in lineages:
        for batch in lineage.batches:
            if not batch.path.is_file():
                raise ArtifactError(
                    f"{batch.path} is declared by {batch.source_id}'s run manifest but is "
                    "absent on disk. A missing batch is a truncated corpus, and a truncated "
                    "corpus reads as a quiet one."
                )
            digest = sha256_of_file(batch.path)
            if digest != batch.sha256:
                raise ArtifactError(
                    f"{batch.path} has sha256 {digest}, the manifest records {batch.sha256}. "
                    "The artifact was rewritten or truncated after it landed; refusing to "
                    "build on it. Re-run `oxbow ingest`."
                )
            frame = pl.read_parquet(batch.path)
            if frame.height != batch.rows:
                raise ArtifactError(
                    f"{batch.path} holds {frame.height} rows, the manifest records {batch.rows}"
                )
            if with_sidecar:
                frame = frame.with_columns(
                    pl.lit(lineage.run_id, dtype=pl.String).alias("run_id"),
                    pl.Series(
                        "ingested_at",
                        [lineage.ingested_at] * frame.height,
                        dtype=pl.Datetime("us", "UTC"),
                    ),
                ).select(list(CANONICAL_COLUMNS))
            elif sorted(frame.columns) != sorted(PERSISTED_CANONICAL_COLUMNS):
                raise ArtifactError(
                    f"{batch.path} carries {sorted(frame.columns)}, which is not the "
                    f"persisted canonical list {sorted(PERSISTED_CANONICAL_COLUMNS)}. "
                    "The contract is the shape of the trust boundary; a drift here is "
                    "schema drift, not a detail."
                )
            frames.append(frame)
    # ``vertical_strict`` so a batch whose dtypes drifted fails here rather than being
    # widened into a superset column. The polars stub list lags the runtime (1.32 accepts
    # the value and raises on a genuine schema mismatch), hence the ignore on the argument
    # only, not on the call.
    events = pl.concat(frames, how="vertical_strict")  # type: ignore[arg-type]
    if events.height == 0:
        raise ArtifactError("the canonical table is empty")
    ctx.echo(
        f"[{ctx.stage}] canonical events read: {_count(events.height)} "
        f"from {len(lineages)} source(s), digests verified against their manifests"
    )
    return events


def _slice_for_interactive_build(
    ctx: StageContext, events: pl.DataFrame, *, max_events: int | None
) -> tuple[pl.DataFrame, str]:
    """The connected subcorpus ``build_graph`` may hold, chosen by the config's own policy.

    ``config/pipeline.yaml`` fixes ``sampling.strategy`` at ``connected_subcorpus`` and says
    why a random slice would not preserve network structure, so the slice comes from the one
    function implementing that strategy
    (:func:`oxbow.features.compute.induced_subcorpus`). A different declared strategy is a
    refusal rather than a silent substitution of the method the config rejects.
    """
    from oxbow.features.compute import induced_subcorpus

    target = int(ctx.cfg.sampling["interactive_txn_target"])
    note = f"sampling.interactive_txn_target={_count(target)}"
    if max_events is not None:
        if max_events < target:
            ctx.echo(
                f"[{ctx.stage}] --max-events {_count(max_events)} narrows the slice below "
                f"{note}; the cap is the operator's, the target is the config's"
            )
        target = min(max_events, target)
    if events.height <= target:
        return events, (
            f"corpus of {_count(events.height)} events is at or below the target "
            f"({_count(target)}): the whole corpus is the interactive slice. {note}"
        )
    strategy = str(ctx.cfg.sampling.get("strategy", ""))
    if strategy != "connected_subcorpus":
        raise StageError(
            f"sampling.strategy is {strategy!r} and the corpus exceeds the interactive "
            "target. This command implements only 'connected_subcorpus', because that is "
            "the strategy the config declares; it will not substitute a random slice to "
            "make itself finish."
        )
    slice_events = induced_subcorpus(events, target_rows=target)
    return slice_events, (
        f"sampling.strategy=connected_subcorpus via "
        f"features.compute.induced_subcorpus(target_rows={_count(target)}): "
        f"{_count(slice_events.height)} of {_count(events.height)} events"
    )


@dataclass(frozen=True, slots=True)
class SummaryView:
    """The gate's five degree statistics, as printed. A view, not a second domain type."""

    accounts: int
    median: float
    p90: float
    p99: float
    max: int


@dataclass(frozen=True, slots=True)
class DegreeGate:
    """What 00 §D day-3 demands be printed, from either measurement path.

    ``degree_measurement`` (full corpus, no traversal structures) and ``build_graph``'s
    ``GraphStats`` (the interactive slice) report the same quantities on different
    populations. Funneling both through one printer is what makes them comparable rather
    than merely similar.
    """

    label: str
    degree_all: SummaryView
    degree_in: SummaryView
    rail_count: int
    external_count: int
    member_count: int
    singleton_count: int
    rail_degree_threshold: int
    top_degrees: tuple[tuple[str, int], ...]


def _gate_from_measurement(measurement: object, *, label: str) -> DegreeGate:
    """Adapt :class:`oxbow.graph.build.DegreeMeasurement` to the printed gate."""
    from oxbow.graph.build import DegreeMeasurement

    if not isinstance(measurement, DegreeMeasurement):
        raise StageError(f"expected a graph DegreeMeasurement, got {type(measurement).__name__}")
    return DegreeGate(
        label=label,
        degree_all=SummaryView(
            measurement.degree_all_nodes.accounts,
            measurement.degree_all_nodes.median,
            measurement.degree_all_nodes.p90,
            measurement.degree_all_nodes.p99,
            measurement.degree_all_nodes.max,
        ),
        degree_in=SummaryView(
            measurement.degree_in_graph.accounts,
            measurement.degree_in_graph.median,
            measurement.degree_in_graph.p90,
            measurement.degree_in_graph.p99,
            measurement.degree_in_graph.max,
        ),
        rail_count=measurement.rail_count,
        external_count=measurement.external_count,
        member_count=measurement.member_count,
        singleton_count=measurement.singleton_count,
        rail_degree_threshold=measurement.rail_degree_threshold,
        top_degrees=measurement.top_degrees,
    )


def _gate_from_stats(stats: object, *, label: str) -> DegreeGate:
    """Adapt :class:`oxbow.graph.model.GraphStats` to the printed gate."""
    from oxbow.graph.model import GraphStats

    if not isinstance(stats, GraphStats):
        raise StageError(f"expected a graph GraphStats, got {type(stats).__name__}")
    return DegreeGate(
        label=label,
        degree_all=SummaryView(
            stats.degree_all_nodes.accounts,
            stats.degree_all_nodes.median,
            stats.degree_all_nodes.p90,
            stats.degree_all_nodes.p99,
            stats.degree_all_nodes.max,
        ),
        degree_in=SummaryView(
            stats.degree_in_graph.accounts,
            stats.degree_in_graph.median,
            stats.degree_in_graph.p90,
            stats.degree_in_graph.p99,
            stats.degree_in_graph.max,
        ),
        rail_count=stats.rail_count,
        external_count=stats.external_count,
        member_count=stats.member_count,
        singleton_count=stats.singleton_count,
        rail_degree_threshold=stats.rail_degree_threshold,
        top_degrees=stats.top_degrees,
    )


def _print_degree_gate(ctx: StageContext, gate: DegreeGate) -> None:
    """Print the degree distribution and the top-20 degrees, which is the P3a gate."""
    for name, summary in (
        ("all nodes", gate.degree_all),
        ("in-graph nodes", gate.degree_in),
    ):
        ctx.echo(
            f"[graph] degree distribution ({gate.label}, {name}): "
            f"accounts={_count(summary.accounts)} median={summary.median:g} "
            f"p90={summary.p90:g} p99={summary.p99:g} max={summary.max}"
        )
    ctx.echo(
        f"[graph] node typing ({gate.label}): rails={_count(gate.rail_count)} "
        f"externals={_count(gate.external_count)} members={_count(gate.member_count)} "
        f"singletons={_count(gate.singleton_count)} "
        f"(a node is a rail above degree {gate.rail_degree_threshold})"
    )
    ctx.echo(f"[graph] top {_count(len(gate.top_degrees))} degrees ({gate.label}):")
    for position, (account, degree) in enumerate(gate.top_degrees, start=1):
        ctx.echo(f"[graph]   {position:>2}. {account} degree={degree}")


# --- stage bodies ---------------------------------------------------------


def run_ingest_stage(
    ctx: StageContext,
    handle: StageHandle,
    *,
    source: Sequence[str],
    limit: int | None,
    out: Path | None,
    batch_rows: int | None,
) -> int:
    """Stage 1: read declared sources, contract-check, canonicalise, land artifacts.

    Non-zero whenever anything was quarantined, any source produced no events, or the
    resolved scope was empty. With ``allow_silent_coercion`` false in config, a green run
    means zero rows were coerced or dropped quietly — and that statement is about rows this
    process actually read, printed above it.
    """
    from oxbow.adapters.file.canonical_sink import CanonicalSink
    from oxbow.ingest.run import declared_sources, resolve_scope, run_ingest

    cfg = ctx.cfg
    declared = declared_sources(ctx.config_dir)
    ingest_config = cfg.raw.get("ingest", {})
    default_scope = [str(item) for item in ingest_config.get("default_sources", ())]
    try:
        scope = resolve_scope(list(source), declared, default=default_scope or ["paysim"])
    except Exception as exc:  # a refused source id is the last line the operator reads
        ctx.echo(f"[ingest] REFUSED: {exc}", err=True)
        return EXIT_REFUSED

    ctx.echo("[ingest] scope: " + (", ".join(item.source_id for item in scope) or "(empty)"))
    for name, entry in sorted(declared.items()):
        if not entry.readable:
            ctx.echo(f"[ingest] declared but never ingested: {name} -- {entry.refusal_reason()}")
    ctx.echo(
        "[ingest] rows: "
        f"limit={limit if limit is not None else 'full corpus'} "
        f"batch_rows={batch_rows if batch_rows is not None else ingest_config.get('batch_rows', 100_000)}"
    )
    ctx.echo(f"[ingest] planned work: {_planned('ingest')}")
    if ctx.dry_run:
        ctx.echo("[ingest] --dry-run: nothing was read and nothing was written.")
        return EXIT_OK

    sink = CanonicalSink.for_repo(
        ctx.root,
        out_override=out.resolve() if out is not None else None,
        determinism=cfg.determinism,
    )
    # Printed from the sink's own paths, not recomputed here: the operator's idea of where
    # the artifacts went and the writer's must not be two different numbers.
    ctx.echo(f"[ingest] artifacts: {sink.interim_root} | duckdb: {sink.duckdb_path}")

    report = run_ingest(
        root=ctx.root,
        config=cfg.raw,
        run_salt=ctx.salt,
        scope=scope,
        declared=declared,
        writer=sink,
        limit=limit,
        batch_rows=batch_rows,
        run_id=ctx.run_id,
    )

    ctx.echo(f"[ingest] run_id: {report.run_id}")
    for state in report.runs:
        ctx.echo(
            f"[ingest] {state.source.source_id}: rows_read={_count(state.rows_read)}"
            f" canonicalised={_count(state.canonical_count)}"
            f" quarantined={_count(state.quarantine_count)}"
            f" duplicates_dropped={_count(state.duplicates_dropped)}"
            f" money_texts_expanded={_count(state.money_texts_expanded)}"
            f" rounding_decisions={_count(state.rounding_decisions)}"
            f" batches={len(state.batches)}"
            f" in {state.elapsed_seconds:.1f}s"
        )
        ctx.echo(
            f"[ingest] {state.source.source_id}: base rates "
            + " ".join(f"{name}={rate}" for name, rate in sorted(state.base_rates.items()))
        )
        ctx.echo(
            f"[ingest] {state.source.source_id}: window "
            f"{state.window_start.isoformat() if state.window_start else 'none'} .. "
            f"{state.window_end.isoformat() if state.window_end else 'none'}"
        )
        for view in state.views:
            ctx.echo(
                f"[ingest] {state.source.source_id}: duckdb view {view['view']}"
                f" files={_as_count(view['files'], label='duckdb view files')}"
                f" rows={_count(_as_count(view['rows'], label='duckdb view rows'))}"
            )
        ctx.echo(f"[ingest] {state.source.source_id}: manifest {state.manifest_path}")

    handle.rows = sum(state.canonical_count for state in report.runs)
    messages = report.failure_messages()
    if messages:
        for message in messages:
            ctx.echo(f"[ingest] FAIL: {message}", err=True)
        ctx.echo(
            "[ingest] RESULT: FAILED. A quarantine is a row that became nothing silently "
            "unless it is counted, landed and read.",
            err=True,
        )
        handle.mark_failed("; ".join(messages))
        return EXIT_FAILED
    ctx.echo(
        f"[ingest] RESULT: OK ({_count(handle.rows)}) canonical events across "
        f"{len(report.runs)} source(s), 0 quarantined, 0 silently coerced"
    )
    return EXIT_OK


def run_graph_stage(
    ctx: StageContext, handle: StageHandle, *, source: Sequence[str], max_events: int | None
) -> int:
    """Stage 2: measure the corpus, build and persist the interactive graph.

    The day-3 gate runs first because ``degree_measurement`` is the path that scales to the
    whole corpus; ``build_graph`` deliberately refuses anything above
    ``sampling.interactive_txn_target`` and is run on the connected subcorpus instead,
    because the traversal structures it keeps are the interactive artifact.
    """
    if ctx.dry_run:
        ctx.echo(f"[graph] planned work: {_planned('graph')}")
        ctx.echo("[graph] --dry-run: no artifact was read and no graph was written.")
        return EXIT_OK

    from oxbow.graph import (
        TOP_DEGREES_REPORTED,
        build_graph,
        degree_measurement,
        load_graph,
        neighbourhood,
        write_graph,
    )
    from oxbow.graph.persist import MANIFEST_NAME

    events = _load_canonical_events(ctx, sources=_resolve_sources(ctx, source), with_sidecar=False)
    gate = _gate_from_measurement(degree_measurement(events, ctx.cfg), label="full corpus")
    _print_degree_gate(ctx, gate)

    slice_events, slice_note = _slice_for_interactive_build(ctx, events, max_events=max_events)
    ctx.echo(f"[graph] interactive slice: {slice_note}")
    graph = build_graph(slice_events, ctx.cfg)
    stats = graph.stats
    ctx.echo(
        f"[graph] built: events={_count(stats.event_count)} edges={_count(stats.edge_count)}"
        f" pairs={_count(stats.pair_count)} nodes={_count(stats.node_count)}"
        f" members={_count(stats.member_count)} externals={_count(stats.external_count)}"
        f" rails={_count(stats.rail_count)} singletons={_count(stats.singleton_count)}"
        f" in-graph={_count(stats.in_graph_node_count)}"
    )
    ctx.echo(
        f"[graph] self-transfers retained as edges and excluded from loops: "
        f"{_count(stats.self_transfer_count)} | currencies: {', '.join(stats.currencies) or 'none'}"
    )
    ctx.echo(
        f"[graph] cycles: {stats.cycle_count} surviving time-respecting loops over "
        f"{stats.cycle_nodes} nodes ({_count(stats.cycle_visits)} visits, "
        f"truncated={stats.cycle_search_truncated})"
    )
    if stats.cycle_search_reason:
        ctx.echo(f"[graph] cycle search: {stats.cycle_search_reason}")
    ctx.echo(f"[graph] communities: {stats.community_count}")
    if stats.community_skipped_reason:
        ctx.echo(f"[graph] communities skipped because: {stats.community_skipped_reason}")
    if stats.pagerank_skipped_reason:
        ctx.echo(f"[graph] pagerank skipped because: {stats.pagerank_skipped_reason}")
    slice_gate = _gate_from_stats(stats, label="interactive slice")
    _print_degree_gate(ctx, slice_gate)
    if (
        len(gate.top_degrees) != TOP_DEGREES_REPORTED
        and gate.degree_all.accounts >= TOP_DEGREES_REPORTED
    ):
        ctx.echo(
            f"[graph] NOTE: top_degrees returned {len(gate.top_degrees)} entries where the "
            f"gate names {TOP_DEGREES_REPORTED}",
            err=True,
        )

    if stats.top_degrees:
        seed_account = stats.top_degrees[0][0]
        view = neighbourhood(graph, seed_account)
        ctx.echo(
            f"[graph] neighbourhood({seed_account}) hops={view.hops}: nodes={view.node_count}"
            f" edges={view.edges.height} reachable_before_cap={view.reachable_before_cap}"
            f" cap={view.cap} truncated={view.truncated}"
            f" collapsed_communities={len(view.meta_nodes)}"
        )
        if view.truncation_reason:
            ctx.echo(f"[graph] neighbourhood truncation: {view.truncation_reason}")

    artifact_dir = ctx.stage_dir(GRAPH_ARTIFACT_DIRNAME)
    write_graph(graph, artifact_dir)
    # Read it back through the verifier: ``load_graph`` re-hashes every file against the
    # manifest it just wrote, so "written" and "usable by the API" are one claim.
    artifacts = load_graph(artifact_dir)
    manifest = json.loads((artifact_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        row = entry if isinstance(entry, dict) else {}
        ctx.echo(
            f"[graph]   {row.get('name')}: rows={_count(_as_count(row.get('rows'), label='artifact rows'))}"
            f" sha256={str(row.get('sha256'))[:16]}…"
        )
    ctx.echo(
        f"[graph] artifact {artifact_dir}: nodes={_count(artifacts.nodes.height)}"
        f" edges={_count(artifacts.edges.height)} pairs={_count(artifacts.pairs.height)}"
        f" flows={_count(artifacts.flows.height)} cycles={_count(artifacts.cycles.height)}"
        f" | settings fingerprint sha256={sha256_of_bytes(dumps(stats.settings_fingerprint).encode('utf-8'))[:16]}…"
    )
    handle.rows = artifacts.edges.height
    ctx.echo(
        f"[graph] RESULT: OK ({_count(stats.event_count)} events graphed; "
        f"{_count(artifacts.edges.height)} edge rows persisted and re-read under {artifact_dir})"
    )
    return EXIT_OK


SCORECARD_GAP: Final[str] = (
    "scorecard, GBM, Isolation Forest, calibration, fusion and SHAP did not run: no module "
    "bridges the feature matrix's grain (txn_id x entity, "
    "oxbow/features/build.py::FeatureTable) to the account-grain TrainingFrame that "
    "oxbow/scoring/frame.py::build_training_frame requires (account_key x as_of_ts x fold, "
    "carrying label_is_fraud and feature_spec_hash). A bridge is package work, not "
    "composition-root work, so it is named here rather than improvised in the CLI. Two "
    "further seams are unwired and the scorecard needs them next: "
    "oxbow/features/registry.py's spec hash and "
    "oxbow/scoring/frame.py::canonical_spec_hash compute different digests for the same "
    "registry, so 02 B seam 3's mismatch guard cannot pass yet; and nothing implements "
    "oxbow.features.fold_scope.GraphFeatureProvider or RuleHitProvider, so every "
    "fold-scoped graph and rule column is null by construction (report fold_id=unsegmented) "
    "rather than fold-sealed as plan §8 requires."
)


def run_score_stage(
    ctx: StageContext, handle: StageHandle, *, source: Sequence[str], max_events: int | None
) -> int:
    """Stage 3: rules and features for real, then the scorecard boundary.

    The parts that exist are called against the landed corpus: ``evaluate_rules`` over the
    real graph, and ``build_feature_table`` over the registry with its money, finite and
    label guards live. What does not exist is reported by name, and the stage exits
    non-zero: a scorecard fitted on a frame the CLI assembled by hand would be a second
    implementation of the grain contract with nobody to defend it.
    """
    if ctx.dry_run:
        ctx.echo(f"[score] planned work: {_planned('score')}")
        ctx.echo("[score] --dry-run: no rule fired and no feature was computed.")
        return EXIT_OK

    from oxbow.graph import build_graph
    from oxbow.rules.registry import evaluate_rules

    events = _load_canonical_events(ctx, sources=_resolve_sources(ctx, source), with_sidecar=True)
    slice_events, slice_note = _slice_for_interactive_build(ctx, events, max_events=max_events)
    ctx.echo(f"[score] slice: {slice_note}")
    graph = build_graph(slice_events, ctx.cfg)
    ctx.echo(
        f"[score] graph for the rules layer: {_count(graph.stats.node_count)} nodes, "
        f"{graph.stats.cycle_count} cycles, {graph.stats.community_count} communities"
    )

    rules_result = evaluate_rules(slice_events, graph, ctx.cfg)
    ctx.echo(f"[score] rules over {_count(rules_result.scored_accounts)} accounts:")
    ctx.echo(rules_result.hit_rate_table())
    ctx.echo(
        f"[score] hits={_count(len(rules_result.hits))}"
        f" evidence_groups={_count(len(rules_result.evidence_groups))}"
        f" near_misses={_count(len(rules_result.near_misses))}"
        f" removed_rules={_count(len(rules_result.removed_rules))}"
    )
    for fit in rules_result.thresholds:
        ctx.echo(
            f"[score] fitted threshold {fit.name} (rule {fit.rule_id}): "
            f"value={fit.value} from {fit.derivation} over {_count(fit.sample_size)} "
            f"observation(s) in window {fit.window.label}"
        )
    if rules_result.removed_rules:
        for removed in rules_result.removed_rules:
            ctx.echo(f"[score] removed: {removed.rule_id} -- {removed.reason}")
    rules_dir = ctx.stage_dir(RULE_ARTIFACT_DIRNAME)
    _write_json(rules_dir / "rule_result.json", rules_result.as_json_dict())
    hits_frame = pl.DataFrame(
        [
            {
                "rule_id": hit.rule_id,
                "rule_name": hit.rule_name,
                "account_key": hit.account_key,
                "severity": round(hit.severity, 6),
                "hit_signature": hit.hit_signature,
                "overlap_group": hit.overlap_group,
                "window_start_us": hit.window.start_us,
                "window_end_us": hit.window.end_us,
                "observation": hit.observation,
                "threshold_param": hit.threshold_param,
                "threshold_value": hit.threshold_value,
                "txn_ids": ",".join(hit.txn_ids),
            }
            for hit in rules_result.hits
        ]
    )
    hits_frame.write_parquet(rules_dir / "rule_hits.parquet")
    ctx.echo(f"[score] rules artifacts: {rules_dir} ({_count(hits_frame.height)} hit rows landed)")
    handle.rows = len(rules_result.hits)

    from oxbow.features.compute import (
        assert_totals_match_rendered_rows,
        build_feature_table,
        graph_null_rate,
        registry_from_repo,
    )

    registry = registry_from_repo(str(ctx.root))
    table = build_feature_table(slice_events, registry, cfg=ctx.cfg)
    assert_totals_match_rendered_rows(slice_events, table)
    report = table.report
    ctx.echo(
        f"[score] features: rows_in={_count(report.rows_in)}"
        f" entity_rows={_count(report.entity_rows)} published_features={_count(report.feature_count)}"
        f" intermediates={report.intermediate_count} outcomes={report.outcome_count}"
        f" late_arrivals={_count(report.late_arrival_count)} fold={report.fold_id}"
    )
    ctx.echo(
        f"[score] feature spec hash: {report.spec_hash} (max lookback "
        f"{report.max_lookback_days}d; timeline {report.timeline_start} .. {report.timeline_end})"
    )
    ctx.echo(f"[score] {report.winsorisation}")
    ctx.echo(f"[score] {report.label_correlation_note}")
    ctx.echo(
        f"[score] label carried outside the matrix: {report.labels_carried_outside_matrix}"
        f" | currencies: {', '.join(report.distinct_currencies)}"
    )
    null_share, null_columns = graph_null_rate(table)
    ctx.echo(
        f"[score] fold-scoped graph columns null on {null_share:.1%} of cells across "
        f"{len(null_columns)} column(s); DEV-011 makes a high rate a finding about a "
        "star-shaped corpus rather than a build defect, and the two are told apart by the "
        "artifact's fold_id and graph_supplied flags"
    )
    features_dir = table.write(ctx.stage_dir(FEATURE_ARTIFACT_DIRNAME))
    ctx.echo(
        "[score] feature artifacts: "
        + ", ".join(f"{key}={value}" for key, value in sorted(features_dir.items()))
    )
    ctx.echo(f"[score] FAIL: {SCORECARD_GAP}", err=True)
    ctx.echo(
        f"[score] rules and features are real: {_count(report.feature_count)} features x "
        f"{_count(table.matrix.height)} rows and {_count(len(rules_result.hits))} rule hits "
        f"landed under {ctx.out_root / FEATURE_ARTIFACT_DIRNAME}/{ctx.run_id}"
    )
    handle.mark_failed(SCORECARD_GAP)
    return EXIT_FAILED


def run_backtest_stage(
    ctx: StageContext,
    handle: StageHandle,
    *,
    corpus: Path | None,
    demo_fakes: bool,
    baselines_only: bool,
    out: Path | None,
) -> int:
    """Stage 4: the walk-forward harness, against a corpus or against its own fakes.

    ``oxbow.backtest.run`` owns the fold loop, the ablation table and the leakage control,
    and it is the entry point being called. Its ``--corpus`` path refuses until a Scorer and
    an Allocator are implemented against ``oxbow.backtest.interfaces``; that refusal is
    printed verbatim rather than softened. ``--demo-fakes`` runs the whole harness on
    hand-computed fixtures: the run is real, every figure is labelled
    ``provenance=fake_harness``, and no part of it is quoted as a corpus result.
    """
    if ctx.dry_run:
        ctx.echo(f"[backtest] planned work: {_planned('backtest')}")
        ctx.echo("[backtest] --dry-run: no fold was walked.")
        return EXIT_OK

    from oxbow.backtest import run as backtest_run

    out_dir = out if out is not None else ctx.stage_dir(BACKTEST_ARTIFACT_DIRNAME)
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = ["--out", str(out_dir)]
    if baselines_only:
        argv.append("--baselines-only")

    if demo_fakes:
        code = backtest_run.main([*argv, "--demo-fakes"])
        payload = out_dir / ("baseline_results.json" if baselines_only else "ablation_results.json")
        handle.rows = _count_backtest_rows(payload)
        handle.detail = (
            f"provenance=fake_harness: the harness, {len(argv)} flag(s), all folds, the "
            f"ablation rows and the leakage control ran for real and wrote {payload}; the "
            "figures verify the harness and are NOT a corpus result"
        )
        ctx.echo(f"[backtest] artifact: {payload} ({_count(handle.rows)} fold-policy rows)")
        return EXIT_OK if code == 0 else EXIT_FAILED

    if corpus is None:
        gap = (
            "no per-account scored corpus exists to price. `oxbow backtest --corpus PATH` is "
            "the real path; nothing in this repository publishes that frame yet, because the "
            "score stage stops at the features-to-TrainingFrame bridge. oxbow/backtest/run.py"
            "::main additionally refuses --corpus until a Scorer and an Allocator are "
            "implemented against oxbow/backtest/interfaces.py. Run `oxbow backtest "
            "--demo-fakes` for the harness self-check, which is labelled "
            "provenance=fake_harness and is not a result."
        )
        ctx.echo(f"[backtest] UNAVAILABLE: {gap}", err=True)
        handle.mark_unavailable(gap)
        return EXIT_FAILED

    try:
        code = backtest_run.main([*argv, "--corpus", str(Path(corpus).resolve())])
    except SystemExit as exc:
        detail = str(exc.code) if exc.code else "oxbow.backtest.run refused the corpus"
        ctx.echo(f"[backtest] FAIL: {detail}", err=True)
        handle.detail = detail[:900]
        return EXIT_FAILED
    handle.rows = _count_backtest_rows(out_dir / "ablation_results.json")
    return EXIT_OK if code == 0 else EXIT_FAILED


def _count_backtest_rows(payload_path: Path) -> int:
    """Fold-policy rows the harness actually wrote, counted from the artifact.

    The ledger's ``rows`` is a measurement: reading the artifact is the only way to know
    how many fold results were committed, where ``n_folds x len(variants)`` would be a
    claim about arithmetic rather than about output.
    """
    if not payload_path.is_file():
        return 0
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    rows = 0
    for variant in payload.get("variants", ()):
        policies = variant.get("policies")
        if isinstance(policies, Mapping):
            for policy in policies.values():
                if isinstance(policy, Mapping):
                    rows += len(policy.get("folds", ()))
    return rows


def _run_script(name: str, argv: Sequence[str], *, config_dir: Path | None) -> int:
    """Run one gate script's ``main`` in-process, or name the module that is missing.

    The script is the gate: ``make verify-determinism`` and ``oxbow verify-determinism``
    must be the same command, so this calls the module rather than restating its logic. A
    script that has not landed yet is reported by filename and exits non-zero, which is the
    same discipline a stage verb applies to an unwired package.
    """
    root = _repo_root(config_dir)
    path = root / "scripts" / name
    if not path.is_file():
        typer.echo(
            f"[cli] scripts/{name} does not exist, so this verb has nothing to call. The "
            "command is wired to that module: land the module and it runs. Exiting non-zero "
            "so no caller mistakes an absent gate for a passed one.",
            err=True,
        )
        return EXIT_FAILED
    spec = importlib.util.spec_from_file_location(f"oxbow_script_{path.stem}", path)
    if spec is None or spec.loader is None:
        typer.echo(f"[cli] scripts/{name} could not be loaded as a module", err=True)
        return EXIT_FAILED
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: a module using dataclasses with postponed annotations resolves
    # them by looking itself up in sys.modules, and without this line the load fails with an
    # unrelated AttributeError rather than a useful message.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    entry = getattr(module, "main", None)
    if not callable(entry):
        typer.echo(f"[cli] scripts/{name} defines no main(argv)", err=True)
        return EXIT_FAILED
    return int(entry(list(argv)))


# --- commands -------------------------------------------------------------


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", help="Print the version and exit."),
) -> None:
    """OXBOW command line. One verb per pipeline stage, run separately."""
    if version:
        typer.echo(__version__)


@app.command("ingest", help=_planned("ingest"), no_args_is_help=False)
def ingest_cmd(
    source: list[str] = typer.Option(
        [],
        "--source",
        "-s",
        help=(
            "Source id to ingest, repeatable. 'all' takes every declared, "
            "ingestion-allowed source. Default comes from config/pipeline.yaml "
            "ingest.default_sources."
        ),
    ),
    limit: int | None = typer.Option(
        None, "--limit", min=1, help="Read at most N raw rows per source (a slice, for smoke runs)."
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Artifact root for Parquet + manifests. Default data/interim."
    ),
    batch_rows: int | None = typer.Option(
        None,
        "--batch-rows",
        min=1,
        help="Rows per canonicalisation batch. Default ingest.batch_rows.",
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Resume or name a specific run."),
    config_dir: Path | None = typer.Option(None, "--config-dir", help="Override config/."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan and the resolved scope, read nothing."
    ),
    stream: bool = typer.Option(
        False, "--stream", help="Emit each stage event as an SSE frame as well as to the ledger."
    ),
) -> None:
    """Stage 1: read declared sources, contract-check, canonicalise, land artifacts."""
    ctx = _open_context(
        "ingest", run_id=run_id, config_dir=config_dir, dry_run=dry_run, stream=stream
    )
    code = _execute(
        ctx,
        "ingest",
        lambda handle: run_ingest_stage(
            ctx, handle, source=list(source), limit=limit, out=out, batch_rows=batch_rows
        ),
    )
    if code:
        raise typer.Exit(code=code)


@app.command("graph", help=_planned("graph"), no_args_is_help=False)
def graph_cmd(
    source: list[str] = typer.Option(
        [],
        "--source",
        "-s",
        help=(
            "Corpus id(s) whose canonical batches to read, repeatable. 'all' reads every "
            "landed corpus. Default comes from config/pipeline.yaml ingest.default_sources."
        ),
    ),
    max_events: int | None = typer.Option(
        None,
        "--max-events",
        min=1,
        help="Cap the interactive slice. Default sampling.interactive_txn_target, never above it.",
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Resume or name a specific run."),
    config_dir: Path | None = typer.Option(None, "--config-dir", help="Override config/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print planned work only."),
    stream: bool = typer.Option(
        False, "--stream", help="Emit each stage event as an SSE frame as well as to the ledger."
    ),
) -> None:
    """Stage 2: measure the corpus, build the multigraph, persist and re-read the artifact.

    Prints the 00 §D day-3 gate -- the degree distribution and the top-20 degrees -- from
    ``degree_measurement`` over every canonical event the ingest manifest declares, then
    builds ``build_graph`` on the connected subcorpus the ``sampling`` policy defines,
    because that is the interactive artifact and it is capped on purpose.
    """
    ctx = _open_context(
        "graph", run_id=run_id, config_dir=config_dir, dry_run=dry_run, stream=stream
    )
    code = _execute(
        ctx,
        "graph",
        lambda handle: run_graph_stage(ctx, handle, source=list(source), max_events=max_events),
    )
    if code:
        raise typer.Exit(code=code)


@app.command("score", help=_planned("score"), no_args_is_help=False)
def score_cmd(
    source: list[str] = typer.Option(
        [],
        "--source",
        "-s",
        help=(
            "Corpus id(s) whose canonical batches to read, repeatable. 'all' reads every "
            "landed corpus. Default comes from config/pipeline.yaml ingest.default_sources."
        ),
    ),
    max_events: int | None = typer.Option(
        None,
        "--max-events",
        min=1,
        help="Cap the slice rules and features run on. Default sampling.interactive_txn_target.",
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Resume or name a specific run."),
    config_dir: Path | None = typer.Option(None, "--config-dir", help="Override config/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print planned work only."),
    stream: bool = typer.Option(
        False, "--stream", help="Emit each stage event as an SSE frame as well as to the ledger."
    ),
) -> None:
    """Stage 3: fire R1-R12, build the feature matrix, then report the scorecard boundary.

    Rules and features run for real against the landed corpus, with the money, finite and
    label guards live. The model channels report ``unavailable`` with the missing package
    seam named, so a red run here is a statement about the build rather than a green run
    about nothing.
    """
    ctx = _open_context(
        "score", run_id=run_id, config_dir=config_dir, dry_run=dry_run, stream=stream
    )
    code = _execute(
        ctx,
        "score",
        lambda handle: run_score_stage(ctx, handle, source=list(source), max_events=max_events),
    )
    if code:
        raise typer.Exit(code=code)


@app.command("backtest", help=_planned("backtest"), no_args_is_help=False)
def backtest_cmd(
    corpus: Path | None = typer.Option(
        None, "--corpus", help="Per-account scored corpus Parquet for the real walk-forward."
    ),
    demo_fakes: bool = typer.Option(
        False,
        "--demo-fakes",
        help="Run the harness on oxbow.backtest.fakes: verifies the harness, labelled "
        "provenance=fake_harness, never quoted as a result.",
    ),
    baselines_only: bool = typer.Option(
        False, "--baselines-only", help="Run only the four mandated baselines."
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Artifact directory. Default out/backtest/<run-id>."
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Resume or name a specific run."),
    config_dir: Path | None = typer.Option(None, "--config-dir", help="Override config/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print planned work only."),
    stream: bool = typer.Option(
        False, "--stream", help="Emit each stage event as an SSE frame as well as to the ledger."
    ),
) -> None:
    """Stage 4: walk forward over the purged, embargoed folds and price every alert."""
    ctx = _open_context(
        "backtest", run_id=run_id, config_dir=config_dir, dry_run=dry_run, stream=stream
    )
    code = _execute(
        ctx,
        "backtest",
        lambda handle: run_backtest_stage(
            ctx,
            handle,
            corpus=corpus,
            demo_fakes=demo_fakes,
            baselines_only=baselines_only,
            out=out,
        ),
    )
    if code:
        raise typer.Exit(code=code)


@app.command("pipeline")
def pipeline_cmd(
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream stage events."),
    source: list[str] = typer.Option([], "--source", "-s", help="Ingest scope for this run."),
    limit: int | None = typer.Option(
        None, "--limit", min=1, help="Rows per source for the ingest stage (a slice)."
    ),
    batch_rows: int | None = typer.Option(None, "--batch-rows", min=1, help="Ingest batch size."),
    run_id: str | None = typer.Option(
        None,
        "--run-id",
        help="Resume a run by id: stages already complete under it are not re-run.",
    ),
    config_dir: Path | None = typer.Option(None, "--config-dir", help="Override config/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan and the scope only."),
) -> None:
    """Run all four stages in order, streaming stage events over the ledger.

    Watching real work happen for forty seconds is more convincing than any animation,
    which is why the UI renders a ledger rather than a spinner: one run id, four stages in
    the canonical order, each event's row count and elapsed time measured by the stage that
    produced it. The first failure stops the chain, because a backtest over a corpus that
    failed to land is arithmetic on a fiction.
    """
    ctx = _open_context(
        "pipeline", run_id=run_id, config_dir=config_dir, dry_run=dry_run, stream=stream
    )
    typer.echo(f"[pipeline] seed {ctx.cfg.seed} | stream={stream} | stages={','.join(STAGES)}")
    for stage in STAGES:
        typer.echo(f"[pipeline] -> {stage}: {_planned(stage)}")
    if dry_run:
        typer.echo("[pipeline] --dry-run: no stage ran and nothing was written.")
        return

    bodies: dict[str, Callable[[StageHandle], int]] = {
        "ingest": lambda handle: run_ingest_stage(
            ctx, handle, source=list(source), limit=limit, out=None, batch_rows=batch_rows
        ),
        "graph": lambda handle: run_graph_stage(ctx, handle, source=[], max_events=None),
        "score": lambda handle: run_score_stage(ctx, handle, source=[], max_events=None),
        "backtest": lambda handle: run_backtest_stage(
            ctx, handle, corpus=None, demo_fakes=False, baselines_only=False, out=None
        ),
    }
    outcomes: list[tuple[str, int]] = []
    for name in STAGES:
        code = _execute(ctx, name, bodies[name])
        outcomes.append((name, code))
        if code != EXIT_OK:
            break
    failures = [f"{name} exited {code}" for name, code in outcomes if code != EXIT_OK]
    summary = {
        "run_id": ctx.run_id,
        "stages_attempted": [name for name, _ in outcomes],
        "failures": failures,
        "events": ctx.emitter.events,
        "elapsed_ms": _elapsed(ctx),
        "provenance": "pipeline",
    }
    _write_json(ctx.out_root / "pipeline" / f"{ctx.run_id}.json", summary)
    typer.echo(
        f"[pipeline] ledger: {ctx.out_root / WAREHOUSE_DIRNAME / LEDGER_FILENAME}"
        f" | events emitted: {_count(len(ctx.emitter.events))}"
    )
    if failures:
        ctx.emitter.finish(RunState.FAILED, error="; ".join(failures)[:900])
        typer.echo(f"[pipeline] FAILED: {'; '.join(failures)}", err=True)
        raise typer.Exit(code=EXIT_FAILED)
    ctx.emitter.finish(RunState.COMPLETE)
    typer.echo(
        f"[pipeline] COMPLETE: {', '.join(name for name, _ in outcomes)} under run {ctx.run_id}"
    )


def _elapsed(ctx: StageContext) -> int:
    """Milliseconds this process has been running, from one monotonic reading."""
    from oxbow.stage_events import elapsed_ms_since

    return elapsed_ms_since(ctx.started_monotonic)


@app.command("verify-determinism")
def verify_determinism_cmd(
    command: str = typer.Option(
        "uv run oxbow ingest --limit 200000",
        "--command",
        help="The stage to run twice, shell-split. Two runs' artifact digests are compared.",
    ),
    reuse: bool = typer.Option(
        False,
        "--reuse",
        help="Compare the artifacts already on disk against one fresh run instead of running twice.",
    ),
    config_dir: Path | None = typer.Option(None, "--config-dir", help="Override config/."),
) -> None:
    """Run the pipeline twice and diff artifact checksums (01 D).

    Two runs on the same input must produce identical Parquet checksums; if they do not,
    that is a P1 bug and everything else waits. The check lives in
    ``scripts/verify_determinism.py`` and is called, not restated, so ``make
    verify-determinism`` and this verb cannot drift into two different claims.
    """
    argv = ["--command", command, *(["--reuse"] if reuse else [])]
    code = _run_script("verify_determinism.py", argv, config_dir=config_dir)
    if code:
        raise typer.Exit(code=code)


@app.command("verify-audit")
def verify_audit_cmd(
    audit_dir: Path | None = typer.Option(
        None, "--audit-dir", help="Directory holding audit.jsonl. Default out/audit."
    ),
    database_url: str | None = typer.Option(
        None, "--database-url", help="Postgres URL for the warehouse chains. Default $DATABASE_URL."
    ),
    no_database: bool = typer.Option(
        False, "--no-database", help="Walk the file chain only, without touching DATABASE_URL."
    ),
    config_dir: Path | None = typer.Option(None, "--config-dir", help="Override config/."),
) -> None:
    """Walk the decision and audit hash chains, print OK or the first broken link.

    The gate lives in ``scripts/verify_audit.py`` — the same pure arithmetic the packet
    uses on export (:func:`oxbow.audit.chain.verify_chain`) — and this verb calls it so
    ``make verify-audit`` and ``oxbow verify-audit`` can never disagree about what
    "verified" means. A chain that cannot be reached is reported SKIPPED with its reason
    and is never counted as a pass; a missing file chain exits non-zero.
    """
    argv: list[str] = []
    if audit_dir is not None:
        argv += ["--audit-dir", str(Path(audit_dir))]
    if database_url is not None:
        argv += ["--database-url", database_url]
    if no_database:
        argv.append("--no-database")
    code = _run_script("verify_audit.py", argv, config_dir=config_dir)
    if code:
        raise typer.Exit(code=code)


@app.command("packet")
def packet_cmd(
    all_cases: bool = typer.Option(False, "--all", help="Render every landed case."),
    case: Path | None = typer.Option(None, "--case", help="One case bundle JSON to render."),
    case_dir: Path | None = typer.Option(
        None, "--case-dir", help="Bundle directory. Default out/case_sink."
    ),
    graph_dir: Path | None = typer.Option(
        None, "--graph-dir", help="Graph artifact directory. Default out/graph."
    ),
    audit_chain: Path | None = typer.Option(
        None, "--audit-chain", help="Audit chain JSONL. Default out/audit/audit.jsonl."
    ),
    warehouse_dir: Path | None = typer.Option(
        None, "--warehouse-dir", help="Run ledger directory. Default out/warehouse."
    ),
    out: Path | None = typer.Option(None, "--out", help="Output directory. Default out/packets."),
    config_dir: Path | None = typer.Option(None, "--config-dir", help="Override config/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the scope without rendering."),
) -> None:
    """Render a case packet to out/packets/ (WeasyPrint, P9).

    The packet layer is called, not reimplemented: ``build_packet_case`` reads the landed
    bundle, the pinned run's graph artifact, the audit chain and the run ledger, refuses
    cross-run mixing, and ``render_case_packet`` verifies the chain again on export before
    typesetting. A missing artifact fails by path.
    """
    root = _repo_root(config_dir)
    from oxbow.packet.loaders import AUDIT_CHAIN_FILENAME, build_packet_case, landed_bundles
    from oxbow.packet.render import render_case_packet
    from oxbow.quant.economics import assumption_block, load_economics

    bundles_dir = (
        Path(case_dir) if case_dir is not None else (root / OUT_DIRNAME / CASE_SINK_DIRNAME)
    )
    audit_path = (
        Path(audit_chain)
        if audit_chain is not None
        else (root / OUT_DIRNAME / AUDIT_DIRNAME / AUDIT_CHAIN_FILENAME)
    )
    graph_artifact_dir = (
        Path(graph_dir) if graph_dir is not None else (root / OUT_DIRNAME / GRAPH_ARTIFACT_DIRNAME)
    )
    warehouse = (
        Path(warehouse_dir)
        if warehouse_dir is not None
        else (root / OUT_DIRNAME / WAREHOUSE_DIRNAME)
    )
    out_dir = Path(out) if out is not None else root / OUT_DIRNAME / PACKET_ARTIFACT_DIRNAME
    timezone = load_pipeline_config(root).deployment_timezone
    typer.echo(
        f"[packet] bundles: {bundles_dir} | chain: {audit_path} | graph: {graph_artifact_dir}"
        f" | run ledger: {warehouse}"
    )
    typer.echo(
        f"[packet] scope: {'every landed case' if all_cases else (str(case) if case else 'the first landed case')}"
        f" -> {out_dir}"
    )
    if dry_run:
        typer.echo("[packet] --dry-run: nothing was rendered.")
        return

    econ = load_economics(root)
    block = assumption_block(econ)
    targets: list[Path] = []
    if case is not None:
        targets = [Path(case)]
    else:
        found = landed_bundles(bundles_dir)
        if not found:
            typer.echo(
                f"[packet] no case bundle under {bundles_dir}. Decisions are landed by the case "
                "sink at decision time (apps/api/decisions.py through oxbow.ports.case_sink); "
                "until one exists there is no exhibit to render, and inventing a case is the "
                "failure plan §15 names.",
                err=True,
            )
            raise typer.Exit(code=EXIT_FAILED)
        targets = [path for path, _bundle in (found if all_cases else found[:1])]

    rendered: list[Path] = []
    for bundle_path in targets:
        built = build_packet_case(
            bundle_path,
            graph_artifact_dir=graph_artifact_dir,
            audit_chain_path=audit_path,
            warehouse_dir=warehouse,
            economics=econ,
            assumption_block=block,
            deployment_timezone=timezone,
        )
        rendered.append(render_case_packet(built, out_dir=out_dir))
    for path in rendered:
        typer.echo(f"[packet] rendered {path}")
    typer.echo(f"[packet] RESULT: OK ({len(rendered)} packet(s))")


@app.command("eval")
def eval_cmd(
    json_only: bool = typer.Option(
        False, "--json-only", help="Write data/processed/eval.json without touching the documents."
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Print only the status line."),
    config_dir: Path | None = typer.Option(None, "--config-dir", help="Override config/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the scope without writing."),
) -> None:
    """Regenerate every metric, curve, frontier and ablation row in the docs.

    ``make eval`` must reproduce the README's numbers exactly; if it does not, that is a bug
    with the same severity as a crash (spec 14). :mod:`oxbow.eval` owns that: it reads the
    artifacts the pipeline actually wrote, publishes them as
    ``data/processed/eval.json`` with a source pointer per figure, and renders the documents
    from that JSON. It exits non-zero while any published section still has no artifact.
    """
    root = _repo_root(config_dir)
    typer.echo(f"[eval] root {root} | every figure read from a landed artifact")
    if dry_run:
        typer.echo("[eval] --dry-run: nothing was regenerated and no document was written.")
        return
    from oxbow import eval as oxbow_eval

    argv: list[str] = ["--root", str(root)]
    if json_only:
        argv.append("--json-only")
    if quiet:
        argv.append("--quiet")
    code = oxbow_eval.main(argv)
    if code:
        raise typer.Exit(code=code)


__all__ = [
    "STAGES",
    "ArtifactError",
    "DegreeGate",
    "StageContext",
    "StageError",
    "app",
    "main",
    "run_backtest_stage",
    "run_graph_stage",
    "run_ingest_stage",
    "run_score_stage",
]


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover - module executed as a script
    main()
