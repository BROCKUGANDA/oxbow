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

import contextlib
import gc
import importlib.util
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# NATIVE IMPORT-ORDER GUARD. Do not move the import below, and do not "tidy" it into a
# function: its whole value is that it runs at this point in the file, before the first
# ``from oxbow...`` line can pull ``pyarrow`` in through ``oxbow.adapters.io``.
#
# WHAT FAULTS. ``import optbinning`` reaches cvxpy, whose solver discovery imports osqp,
# whose package init resolves its algebra backend by importing the compiled extension
# module (``osqp/interface.py``: ``default_algebra`` -> ``algebra_available`` ->
# ``importlib.import_module("osqp.ext_builtin")``). Loading that second bundled
# OpenMP/BLAS runtime into a process that already carries pyarrow's is what kills it:
# exit 139, a segfault, no Python traceback. Measured on this machine, three lines each
# way -- ``import pyarrow; import cvxpy`` dies, ``import cvxpy; import pyarrow`` and
# ``import osqp; import pyarrow; import cvxpy`` both exit 0. So the fault is neither a
# missing DLL nor an import-order *preference*: it is one specific ordering, and the
# same fault is already pinned for the test session in ``tests/conftest.py``, whose
# docstring states that the composition root pins it too. Until this block existed, it
# did not, and ``oxbow score`` died inside the scorecard fit for exactly that reason.
#
# WHY THIS AND NOT A SOLVER SWITCH. optbinning 0.19's ``solver="mip"`` path is ortools
# (``binning/mip.py`` -> ``pywraplp``), not cvxpy, and cvxpy's OSQP probe happens during
# ``import cvxpy`` -- before any binning problem exists. Pinning SCS or CLARABEL in the
# scorecard therefore cannot avoid the fault, and the ``OSQP_ALGEBRA_BACKEND=<bogus>``
# workaround survives the crash only by making the probe raise ``KeyError`` and skip
# OSQP, i.e. by disabling a solver to dodge a defect. Importing osqp early disables
# nothing: OSQP stays installed, stays discoverable by cvxpy, and solves whatever it is
# asked to. The load that faults is simply moved to the one moment when it is safe, and
# every later probe is a cache hit that loads no new native code.
import osqp  # noqa: F401 -- imported for the native load it performs, not for the name
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
from oxbow.dtypes import PolarsDtype
from oxbow.identity import new_ulid, require_ulid, sha256_of_bytes
from oxbow.ports.warehouse import RunState, WarehouseSink
from oxbow.stage_events import StageEventEmitter, StageHandle


# The guard above is a side effect, and a side effect that silently stops happening is
# how this bug returns: an osqp that defers its algebra load again would leave the import
# line untouched and the segfault back. What is checkable is that the load occurred, so
# that is what is checked -- and only that.
#
# WHY NOT "osqp must precede pyarrow". That stricter form was tried here and it is wrong:
# it refuses `import oxbow.cli` in any process that touched pyarrow first, which includes
# the pytest session (tests/conftest.py makes optbinning, and so cvxpy, load before
# pyarrow, and osqp's own entry then lands later than pyarrow's). Three
# `tests/unit/test_p0_toolchain.py` tests went red on that check while the process that
# loaded osqp second never crashed. So osqp-after-pyarrow is survivable and the lethal
# ordering is the one conftest and this block each prevent in their own context:
# **cvxpy arriving after pyarrow**. Asserting anything tighter here would be asserting a
# preference, and a guard that fires on a healthy process is worse than no guard -- it
# teaches the next reader to reach for the skip.
def _require_solver_pin_ran() -> None:
    if not any(name.startswith("osqp") for name in sys.modules):
        raise ImportError(
            "osqp never loaded, so the native import-order guard in oxbow/cli.py is not "
            "running. Its algebra extension must be resolved here, before pyarrow arrives, "
            "or the scorecard fit segfaults this process (exit 139, no traceback)."
        )


_require_solver_pin_ran()

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
SCORE_ARTIFACT_DIRNAME: Final = "score"
BACKTEST_ARTIFACT_DIRNAME: Final = "backtest"
PACKET_ARTIFACT_DIRNAME: Final = "packets"
CASE_SINK_DIRNAME: Final = "case_sink"

# ---------------------------------------------------------------------------
# THE RULE HIT LEDGER. Declared schema, never an inferred one.
#
# polars builds a frame from dicts by sampling the rows it is given first
# (`infer_schema_length=100`), and `overlap_group` is None for every rule that has
# nothing to dedup against. A run whose first hundred hits were all ungrouped therefore
# typed that column `Null`, and the next grouped hit killed the stage with
# `ComputeError: could not append value "aggregation" of type str to the builder` --
# measured, on the first corpus that produced a grouped rule at all. The ledger is what
# the report, the near-miss table and the API's rule cards are built from, so its column
# types cannot depend on which rule happened to fire first.
_RULE_HIT_SCHEMA: Final[dict[str, PolarsDtype]] = {
    "rule_id": pl.Utf8,
    "rule_name": pl.Utf8,
    "account_key": pl.Utf8,
    "severity": pl.Float64,
    "hit_signature": pl.Utf8,
    "overlap_group": pl.Utf8,
    "window_start_us": pl.Int64,
    "window_end_us": pl.Int64,
    "observation": pl.Float64,
    "threshold_param": pl.Utf8,
    "threshold_value": pl.Float64,
    "txn_ids": pl.Utf8,
}


def _rule_hits_frame(hits: Sequence[Any]) -> pl.DataFrame:
    """The hit ledger, with its schema stated rather than guessed from the first rows."""
    return pl.DataFrame(
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
            for hit in hits
        ],
        schema=_RULE_HIT_SCHEMA,
    )


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


def _execute(
    ctx: StageContext, name: str, body: Callable[[StageHandle], int], *, terminal: bool = True
) -> int:
    """Run one stage and, unless told otherwise, move the run to a terminal state.

    A crash that left ``run.state`` at ``running`` would hold the API's SSE stream open
    until its budget expired, which reads as a stalled pipeline rather than a failed one
    (03 §J). The exception still propagates: the ledger records the failure, the operator
    sees the traceback, and the exit code stays non-zero.

    ``terminal=False`` is for a stage composed into a larger run by ``pipeline``, which
    owns the run's terminal state itself. Without it the first stage closed the run and
    the composition root's own ``finish`` hit the warehouse's deliberate
    "a completed run does not change state" guard, so `oxbow pipeline` could not finish
    whatever its stages did. A failure is still terminal here, because a raised
    exception stops the whole run at that stage.
    """
    try:
        code = _run_stage(ctx, name, body)
    except Exception as exc:
        if not ctx.dry_run:
            ctx.emitter.finish(RunState.FAILED, error=f"{type(exc).__name__}: {exc}")
        raise
    if not ctx.dry_run and terminal:
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
        # Batch paths are recorded repo-relative (see canonical_sink._relative_to_root) so
        # a manifest written by the container image at /srv is still readable from the host.
        # They resolve against the repo root this function was handed, not the manifest's own
        # ancestors, so an --out relocation still resolves. An absolute path in an older
        # manifest is honoured as-is: joining it to the root is a no-op for an absolute Path.
        batches = tuple(
            CanonicalBatch(
                source_id=str(entry["source_dataset"]),
                batch_id=str(entry["batch_id"]),
                path=Path(str(entry["path"]))
                if Path(str(entry["path"])).is_absolute()
                else root / str(entry["path"]),
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


def _parse_instant(text: str) -> datetime:
    """The manifest's ``ingested_at`` as a UTC instant, or a refusal naming the text.

    DEV-012 keeps ``run_id`` and ``ingested_at`` out of the Parquet bytes so two runs of
    one corpus are byte-comparable, which means a stage that needs the contract's full
    column list re-attaches them from the manifest. Parsing is not optional: a str reaching
    a Datetime column is a polars error several minutes into a run.
    """
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise ArtifactError(
            f"run manifest ingested_at {text!r} is not an ISO-8601 instant; the sidecar "
            "column cannot be reconstructed"
        ) from exc


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
                        [_parse_instant(lineage.ingested_at)] * frame.height,
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
    # ``vertical`` is already the strict form: on polars 1.32 a batch whose dtype
    # drifted raises SchemaError rather than being widened into a superset column
    # (measured, not assumed -- the previous value here, "vertical_strict", is not a
    # polars keyword at all, so the standalone `oxbow graph` verb died on any corpus
    # with more than one landed batch). ``vertical_relaxed`` is the one that coerces.
    events = pl.concat(frames, how="vertical")
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
        for note in state.notes:
            # A slice that holds none of the corpus's typology annotations is not a
            # failure, but it is a fact about every number this run can support, so it
            # goes to stderr where an operator scrolling a green run still meets it.
            ctx.echo(f"[ingest] NOTE: {note}", err=True)
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
    "the model stack (WOE scorecard, LightGBM, Isolation Forest, calibration, fusion, SHAP) "
    "is wired and reached from this stage: the txn->account grain bridge "
    "(oxbow.features.bridge), the two fold-scoped providers (oxbow.features.fold_providers) "
    "and a real fold-fitting Scorer (oxbow.models.scorer.WalkForwardScorer over "
    "oxbow.models.run.FoldModelRunner). Two refusals are still reachable and are reported by "
    "name rather than softened. First, a corpus whose timeline cannot hold the configured "
    "30-day embargo across five folds: the ONE splits module "
    "(oxbow.backtest.splits.build_walk_forward) raises a SplitError then, because inventing "
    "fold boundaries to make the arithmetic work is the leakage plan §8 forbids -- measured "
    "on a 20,000-row slice spanning 7 days, which is why the score stage reads a landed "
    "window and not a head slice. Second, a fold whose fit population the scorecard cannot "
    "defend, which the run reports as that fold being SKIPPED with the reason, and if no "
    "fold survives it refuses the stage rather than scoring nothing and calling it a model. "
    "Calibration likewise refuses rather than emit a below-floor probability (isotonic then "
    "Platt then refuse), and the row says so. The multi-fold demonstration against synthetic "
    "spec hashes runs on `oxbow backtest --demo-fakes` and is labelled provenance=fake_harness; "
    "it is a harness check, never a result."
)


def _load_p4_configs(root: Path) -> object:
    """The five config views the score stage composes, or a refusal naming the file."""
    from oxbow.models.config import load_model_config, load_split_config
    from oxbow.scoring.config import load_feature_registry, load_scorecard_config

    return {
        "model": load_model_config(root),
        "scorecard": load_scorecard_config(root),
        "split": load_split_config(root),
        "scoring_registry": load_feature_registry(root),
    }


def run_walk_forward_folds(
    runner: Any,
    training: Any,
    frame: pl.DataFrame,
    fold_pairs: Sequence[tuple[int, Sequence[bool], Sequence[bool], Sequence[bool]]],
    *,
    embargo_days: int,
    echo: Callable[..., None] | None = None,
    observe: Callable[[str, int], None] | None = None,
) -> tuple[list[pl.DataFrame], list[dict[str, object]]]:
    """Score every fold through ``runner.run_fold``, releasing each fold before the next.

    The one thing this function owns that the caller cannot get wrong: a fold's
    :class:`~oxbow.models.run.FoldRun` keeps the *full* train+validation+test scored frame
    (with the per-row SHAP JSON on every row, not only the test rows) plus the fitted
    LightGBM booster and the Isolation Forest. A naive loop that rebinds ``run`` only when
    the next ``run_fold`` returns leaves fold *k*'s whole object resident *while* fold
    *k+1* fits, so peak memory is fold-k-plus-fold-(k+1) rather than one fold. That is
    exactly what landed fold 0 and starved folds 1-4 with ``LightGBMError: bad allocation``
    on a 79,998-row corpus on 2026-09-27 -- the failure was per-fold accumulation, not
    corpus size.

    Encapsulating one fold's fit here makes the retention structurally impossible: the
    ``FoldRun`` is local, only its test-row slice (an independent Polars frame, verified --
    ``filter`` materialises and does not view the parent) and a plain summary dict survive
    the call, and the ``gc.collect()`` at the end of each iteration clears any transient
    cyclic reference before the next fold's allocation. It changes no number a fold
    produces; it only frees what the fold no longer needs.

    ``observe`` (a test seam, unused in production) is called as ``observe("before_fit",
    fold_index)`` immediately before each fold fits, so a regression test can assert that no
    prior fold's model objects are still resident at that point.
    """
    from oxbow.models.scorer import FixedFoldSlicesProvider
    from oxbow.scoring.frame import ROLE_TEST, ROLE_TRAIN, ROLE_VALIDATION

    def say(message: str, *, err: bool = False) -> None:
        if echo is not None:
            echo(message, err=err)

    # SHAP must be imported once, OUTSIDE any fold's call stack, before the first fit.
    # ``import shap`` runs optional-dependency probes (cv2 / pyspark / the CUDA ``_cext_gpu``
    # extension) that raise ModuleNotFoundError / ImportError and keep those exception objects
    # alive on a module-level structure. CPython retains the whole traceback of a live
    # exception, and the traceback's frames reach back up the stack that first imported shap:
    # ``_explainer_values -> explain_and_report -> _explain -> run_fold``. Imported lazily from
    # inside ``run_fold``, that pins the *first* fitted fold's frame locals -- the full
    # train+validation+test scored frame (every row's SHAP JSON), the booster and the
    # Isolation Forest -- for the rest of the run. This was the measured accumulator: with
    # SHAP on, one fold's model objects stayed resident across every later fold; with the
    # import hoisted to here (a neutral frame holding no fold), they did not. Importing shap
    # now costs nothing on the folds themselves.
    with contextlib.suppress(Exception):  # shap missing degrades SHAP, must not break scoring
        import shap  # noqa: F401  (warm the optional-import probes at a neutral frame)

    scored_frames: list[pl.DataFrame] = []
    runs_summary: list[dict[str, object]] = []
    for fold_index, train_mask, validation_mask, test_mask in fold_pairs:
        if observe is not None:
            observe("before_fit", fold_index)
        train = frame.filter(pl.Series(train_mask)).with_columns(pl.lit(ROLE_TRAIN).alias("role"))
        validation = frame.filter(pl.Series(validation_mask)).with_columns(
            pl.lit(ROLE_VALIDATION).alias("role")
        )
        scored = frame.filter(pl.Series(test_mask)).with_columns(pl.lit(ROLE_TEST).alias("role"))
        if scored.height == 0:
            runs_summary.append({"fold": fold_index, "skipped": "empty test window"})
            continue
        provider = FixedFoldSlicesProvider(train, validation, scored, embargo_days=embargo_days)
        try:
            run = runner.run_fold(
                training, fold_index, provider=provider, evaluation_role=ROLE_TEST
            )
        except Exception as exc:  # a fold the corpus cannot fit is reported by name, not faked
            runs_summary.append(
                {"fold": fold_index, "skipped": f"{type(exc).__name__}: {str(exc)[:300]}"}
            )
            say(
                f"[score] fold {fold_index}: SKIPPED — the fit population is not "
                f"defensible ({type(exc).__name__}: {exc})",
                err=True,
            )
            del train, validation, scored, provider
            gc.collect()
            continue
        test_rows = run.scored.filter(pl.col("role") == ROLE_TEST)
        scored_frames.append(test_rows)
        runs_summary.append(
            {
                "fold": fold_index,
                "mode": run.mode,
                "calibrated": run.calibrated,
                "rows": test_rows.height,
                "channel_skips": dict(run.channel_skips),
            }
        )
        say(
            f"[score] fold {fold_index}: mode={run.mode} scored={test_rows.height} "
            f"calibrated={run.calibrated}"
        )
        # Release the fold's models and its full scored frame before the next fold starts.
        # ``scored_frames`` holds only ``test_rows`` (independent of ``run``), so dropping
        # every name bound in this iteration frees the fold's footprint now, not on rebind.
        # F821 is ruff reading a `del` at the foot of a loop as a use of names the *previous*
        # iteration deleted; every one of them is bound above, and a fold that reached this
        # line had them all. The del is the point: it is what frees the fold's footprint.
        del run, test_rows, train, validation, scored, provider  # noqa: F821
        gc.collect()
    return scored_frames, runs_summary


def _score_models_and_land(
    ctx: StageContext,
    handle: StageHandle,
    *,
    slice_events: pl.DataFrame,
    feature_registry: object,
    configs: Mapping[str, object],
    plan: object,
) -> int:
    """Bridge the matrix to account grain, fit the fold stack, and land the artifacts.

    Every step here is a real call into a package that owns it: the split plan from
    ``oxbow.backtest.splits``, the fold-scoped graph/rule providers from
    ``oxbow.features.fold_providers``, the txn->account carry from ``oxbow.features.bridge``,
    the validated ``TrainingFrame`` from ``oxbow.scoring.frame``, and one
    :class:`oxbow.models.run.FoldModelRunner` fit per fold. The scored rows carry the fused
    probability, the calibration band's measured rate and count, and the per-row SHAP
    payload; the corpus frame written beside them is the ``oxbow backtest --corpus`` input.
    """
    from oxbow.backtest.fold_provider import SplitsFoldProvider
    from oxbow.features.bridge import build_account_frame
    from oxbow.features.fold_providers import fold_providers
    from oxbow.models.run import FoldModelRunner, stack_scored_frames
    from oxbow.models.scorer import FrameRuleHitProvider
    from oxbow.quant.economics import load_economics
    from oxbow.scoring.frame import (
        COL_FOLD,
        PROVENANCE_REAL,
        build_training_frame,
    )

    model_cfg = configs["model"]
    scorecard_cfg = configs["scorecard"]
    split_cfg = configs["split"]
    scoring_registry = configs["scoring_registry"]
    categorical = tuple(scorecard_cfg.binning.categorical_features)

    graph, rules = fold_providers(slice_events, feature_registry, ctx.cfg)
    grain = build_account_frame(
        slice_events, feature_registry, plan, graph=graph, rules=rules, cfg=ctx.cfg
    )
    report = grain.report
    ctx.echo(f"[score] grain bridge: {report.sentence()}")

    frame = grain.frame
    try:
        training = build_training_frame(
            frame,
            scoring_registry,
            categorical,
            PROVENANCE_REAL,
            split_cfg.validation_fraction_of_train,
            split_cfg.n_folds,
        )
    except Exception as exc:  # a contract refusal is reported by name, never swallowed
        ctx.echo(f"[score] REFUSED (training frame): {exc}", err=True)
        handle.mark_failed(f"the bridged frame did not satisfy the scoring contract: {exc}")
        return EXIT_FAILED
    ctx.echo(
        f"[score] training frame: {training.n_rows} account-instant rows x "
        f"{len(training.feature_names)} features, {training.n_positive} positives "
        f"(base rate {training.base_rate:.6f}), spec hash {training.feature_spec_hash[:16]}…"
    )

    runner = FoldModelRunner(
        model_cfg=model_cfg,
        scorecard_cfg=scorecard_cfg,
        split_cfg=split_cfg,
        feature_registry=scoring_registry,
        rules_provider=FrameRuleHitProvider(),
        provenance=PROVENANCE_REAL,
        trial_budget=1,  # verification-bounded: no Optuna sweep; see the report's cost note
        root=ctx.root,
    )
    fold_provider = SplitsFoldProvider(plan, as_of_column="as_of_ts")
    fold_pairs = [
        (
            fold.index,
            harness_fold.train_mask,
            harness_fold.validation_mask,
            harness_fold.test_mask,
        )
        for fold, harness_fold in zip(plan.folds, fold_provider.folds(frame), strict=True)
    ]
    scored_frames, runs_summary = run_walk_forward_folds(
        runner,
        training,
        frame,
        fold_pairs,
        embargo_days=plan.embargo_days,
        echo=lambda message, *, err=False: ctx.echo(message, err=err),
    )

    if not scored_frames:
        ctx.echo("[score] REFUSED: no fold produced a scored test window", err=True)
        handle.mark_failed("the walk-forward produced no scored rows to land")
        return EXIT_FAILED
    # The folds do not agree on columns: the ones that degraded carry no `p_gbm`, the ones that
    # ran the full stack carry no `drift_*`. `vertical_relaxed` aligns by position and refuses
    # that with `ComputeError: schema names differ`, which is how a run died after scoring every
    # fold but before landing any of them. See `stack_scored_frames` for the contract check.
    scored_rows = stack_scored_frames(scored_frames).sort(["as_of_ts", "account_key", COL_FOLD])

    economics = load_economics(ctx.root)
    corpus = _attach_corpus_economics(frame, economics)

    score_dir = ctx.stage_dir(SCORE_ARTIFACT_DIRNAME)
    scored_rows.write_parquet(score_dir / "scored_rows.parquet", statistics=False)
    corpus.write_parquet(score_dir / "backtest_corpus.parquet", statistics=False)
    _write_json(
        score_dir / "score_run_manifest.json",
        {
            "spec_hash": training.feature_spec_hash,
            "provenance": PROVENANCE_REAL,
            "rows_scored": scored_rows.height,
            "corpus_rows": corpus.height,
            "trial_budget": 1,
            "folds": runs_summary,
            "grain_report": report.sentence(),
        },
    )
    ctx.echo(
        f"[score] model artifacts: {score_dir} — {scored_rows.height} scored rows with SHAP, "
        f"{corpus.height}-row backtest corpus (economics in {economics.currency} minor units)"
    )
    # The count has to be the number of folds that produced scored rows, not the number of
    # entries in the summary: on 2026-09-27 the run landed fold 0 and skipped folds 1-4 to
    # `LightGBMError: bad allocation`, and this line still read "5 fold(s) scored". A
    # progress line that counts refusals as passes is how a phase gets claimed on a tree
    # that never finished fitting, which is the failure this repository keeps finding.
    scored_folds = [entry for entry in runs_summary if "skipped" not in entry]
    skipped_folds = [entry for entry in runs_summary if "skipped" in entry]
    ctx.echo(
        f"[score] RESULT: rules, features, the grain bridge, the fold-scoped providers and the "
        f"model stack all ran; {len(scored_folds)} of {len(runs_summary)} fold(s) scored and "
        f"landed under {score_dir}"
    )
    for entry in skipped_folds:
        ctx.echo(
            f"[score] fold {entry.get('fold')} did NOT score: {entry.get('skipped')} — the "
            f"artifact carries it as a skip, and the phase cannot be claimed on the folds that ran"
        )
    return EXIT_OK


def _attach_corpus_economics(frame: pl.DataFrame, economics: object) -> pl.DataFrame:
    """Add the four economic columns the backtest corpus is owed, in integer minor units.

    THE AGGREGATION, CHOSEN AND STATED. ``exposure_minor`` is the account's own 30-day
    outflow (``amount_out_30d_minor``) — the value still inside the recovery window and the
    only per-account money the feature matrix already measured; a null outflow is zero, not a
    guess. ``review_minutes`` is scaled from that exposure and clamped to the configured
    alert-class band so the capacity constraint binds rather than reviewing everything; the
    per-minute price and the floor are read from ``config/economics.yaml``, never restated.
    ``review_cost_minor`` is minutes times that price and ``amount_minor`` mirrors exposure
    for the highest-amount-first baseline. Every step is integer arithmetic.
    """
    cost_per_minute = int(economics.analyst.cost_per_minute_minor)  # type: ignore[attr-defined]
    floor = int(economics.analyst.min_review_minutes)  # type: ignore[attr-defined]
    ceiling = max(int(economics.minutes_for("E")), floor)  # type: ignore[attr-defined]
    outflow = (
        pl.col("amount_out_30d_minor").cast(pl.Int64).fill_null(0)
        if "amount_out_30d_minor" in frame.columns
        else pl.lit(0, dtype=pl.Int64)
    )
    return (
        frame.with_columns(outflow.alias("exposure_minor"))
        .with_columns(
            pl.col("exposure_minor").alias("amount_minor"),
            (pl.col("exposure_minor") / 1_000_000)
            .cast(pl.Int64)
            .clip(floor, ceiling)
            .alias("review_minutes"),
        )
        .with_columns((pl.col("review_minutes") * cost_per_minute).alias("review_cost_minor"))
    )


def run_score_stage(
    ctx: StageContext, handle: StageHandle, *, source: Sequence[str], max_events: int | None
) -> int:
    """Stage 3: rules and features for real, then the model stack through the seam.

    The parts that exist are called against the landed corpus in order: ``evaluate_rules``
    over the real graph, ``build_feature_table`` over the registry, the grain bridge into an
    account-grain ``TrainingFrame``, and one fold-fitted model stack per fold — scorecard,
    GBM, Isolation Forest, calibration, fusion and per-row SHAP — landed as artifacts beside
    the backtest corpus. A corpus whose timeline cannot hold the embargo, or a fold the
    splits module refuses, stops the model step by name; the refusal is the feature, and a
    scorecard the CLI assembled by hand would be a second grain contract with nobody to
    defend it.
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

    # THE CHEAPEST CHECK IN THIS STAGE RUNS FIRST. Building the graph, firing the twelve
    # rules and computing 75 features is 20-45 minutes of work; deciding whether the landed
    # corpus can hold the configured embargo across five folds is arithmetic on two
    # timestamps. Running the arithmetic last meant refusing a 5,000-row corpus (or one
    # whose slice happened to land in the first days of the timeline) after paying the whole
    # cost to find out -- three attempts this session, and `make verify` re-points the
    # corpus the score stage reads, so it can recur at any time.
    #
    # The refusal itself is the correct behaviour and stays: inventing fold boundaries to
    # make the arithmetic work is the leakage plan §8 forbids. What changed is only when it
    # is discovered. The plan computed here is the same object the model stack would have
    # computed, and is the one it now uses.
    from oxbow.backtest.splits import SplitError, build_walk_forward
    from oxbow.features.build import entity_event_frame
    from oxbow.features.compute import registry_from_repo
    from oxbow.features.kinds import ENTITY, EVENT_TS

    feature_registry = registry_from_repo(str(ctx.root))
    timeline = entity_event_frame(slice_events).select([EVENT_TS, ENTITY]).sort([EVENT_TS, ENTITY])
    try:
        split_plan = build_walk_forward(
            timeline, registry=feature_registry, config_dir=ctx.config_dir
        )
    except SplitError as exc:
        window = ", ".join(
            str(value)
            for value in (
                timeline.get_column(EVENT_TS).min(),
                timeline.get_column(EVENT_TS).max(),
            )
        )
        ctx.echo(
            f"[score] REFUSED (fold plan, before the graph or the features): {exc} | this "
            f"slice spans {window}; widen --max-events, or read a longer corpus, rather than "
            "shortening the embargo the features depend on",
            err=True,
        )
        handle.mark_unavailable(
            f"the corpus cannot support the configured walk-forward, so no stage of the "
            f"model stack ran: {exc}"
        )
        return EXIT_FAILED
    ctx.echo(f"[score] split: {split_plan.as_report_line()}")

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
    hits_frame = _rule_hits_frame(rules_result.hits)
    hits_frame.write_parquet(rules_dir / "rule_hits.parquet")
    ctx.echo(f"[score] rules artifacts: {rules_dir} ({_count(hits_frame.height)} hit rows landed)")
    handle.rows = len(rules_result.hits)

    from oxbow.features.compute import (
        assert_totals_match_rendered_rows,
        build_feature_table,
        graph_null_rate,
    )

    registry = feature_registry
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

    configs = _load_p4_configs(ctx.root)
    return _score_models_and_land(
        ctx,
        handle,
        slice_events=slice_events,
        feature_registry=registry,
        configs=configs,
        plan=split_plan,
    )


def _read_canonical_for_ids(ctx: StageContext, wanted: pl.DataFrame) -> pl.DataFrame:
    """The canonical events whose ``txn_id`` is in ``wanted``, read batch by batch.

    Only the columns an account is described by and a transaction is evidenced with are read,
    and the semi-join happens per batch rather than after a full load: the corpus is 6.36M events and the slice is 40k, so
    holding the whole frame to discard 99.4% of it is the shape of the memory failure that has
    already cost this project two runs on this host.

    The batch list comes from each source's ``run_manifest.json``, the same authority
    ``_canonical_events`` reads, and each batch is digest-checked before it is used — a
    directory listing would pick up leftovers from earlier runs and double-count the corpus,
    and a rewritten batch would land accounts nobody verified.
    """
    from oxbow.adapters.file.canonical_sink import sha256_of_file
    from oxbow.adapters.warehouse.landing import LandingError

    # The seven columns `account` aggregates by, plus the ones `transaction` and
    # `evidence_event` carry into the case workspace: type, local date and hour, the four balances
    # and the two labels. Read per batch and column-limited, so widening the list costs the read
    # time, never a copy of the corpus.
    wanted_columns = [
        "txn_id",
        "event_ts_utc",
        "event_date_local",
        "local_hour",
        "txn_type",
        "account_from",
        "account_to",
        "amount_minor",
        "currency",
        "src_balance_before_minor",
        "src_balance_after_minor",
        "dst_balance_before_minor",
        "dst_balance_after_minor",
        "label_is_fraud",
        "label_typology",
        "source_dataset",
    ]
    found: list[pl.DataFrame] = []
    interim = ctx.root / DATA_DIRNAME / INTERIM_DIRNAME
    for manifest_path in sorted(interim.glob("*/run_manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for batch in manifest.get("batches") or []:
            path = ctx.root / str(batch["path"])
            if not path.is_file():
                raise LandingError(
                    f"{batch['path']} is declared by {manifest_path.parent.name}'s manifest but "
                    "is absent; refusing to describe accounts from a truncated corpus"
                )
            digest = sha256_of_file(path)
            if digest != str(batch["sha256"]):
                raise LandingError(
                    f"{path.name} has sha256 {digest}, the manifest records {batch['sha256']}"
                )
            available = set(pl.read_parquet_schema(path))
            columns = [name for name in wanted_columns if name in available]
            frame = pl.read_parquet(path, columns=columns)
            matched = frame.join(wanted, on="txn_id", how="semi")
            if matched.height:
                found.append(matched)
    if not found:
        raise LandingError(
            "none of the slice's txn_ids resolved against the landed canonical batches"
        )
    return pl.concat(found)


#: The two artifacts ``oxbow backtest`` writes for a run, and the provenance value that says the
#: figures came off the real corpus. A fixture's numbers are demo material, and DEV-003 put a
#: `provenance` column on `run` precisely so the API can tell the two apart in its own responses;
#: the landing checks it first, because a fixture row in ``ablation_row`` would be quoted as a
#: measurement inside a week.
BACKTEST_ABLATION_NAME: Final = "ablation_results.json"
BACKTEST_CARD_NAME: Final = "model_card.json"
MEASURED_BACKTEST_PROVENANCE: Final = "real_corpus"
#: How many refusal lines per table are printed before the count stands in for the rest. A refusal
#: is a finding, not a log: the first two say what happened, the count says how much of it.
REFUSAL_LINES_SHOWN: Final = 2
REFUSAL_PREVIEW_CHARS: Final = 150
#: THE REPORTING ORDER, STATED: the three tables the queue joins, then the case workspace's
#: evidence, then the studio's, then the backtest's, then the graph's. Declared rather than taken
#: from dict order, so two runs print the same lines in the same sequence.
LANDING_REPORT_ORDER: Final = (
    "account",
    "score",
    # The queue is `score` joined to `account` joined to `economics`, so the priced table is reported
    # beside the score it prices and its refusals are never read as a separate stage.
    "economics",
    "rule_hit",
    "transaction",
    "evidence_event",
    "scorecard_bin",
    "scorecard_point",
    "band_definition",
    "drift_period",
    # The policy tables are shaped from the backtest document and reported with it. `policy` is
    # written first because `policy_summary.policy_id` is a NOT NULL foreign key to it, and the
    # landing writes in this order, so a summary could not otherwise land ahead of its parent.
    "policy",
    "policy_summary",
    "ablation_row",
    "validation_metric",
    "fairness_row",
    "perturbation_row",
    "backtest_fold",
    "community",
    "graph_edge",
    "account_membership",
)
#: The two tables the command dashboard reads for its currency tiles, named once here because the
#: sink's declared handoff vocabulary is what decides whether they can be written at all.
POLICY_TABLES: Final = ("policy", "policy_summary")


def _read_json_object(path: Path) -> dict[str, Any]:
    """A decoded JSON object, or a refusal naming the file it could not read."""
    from oxbow.adapters.warehouse.landing import LandingError

    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LandingError(f"{path} cannot be read as JSON: {str(exc)[:120]}") from exc
    if not isinstance(parsed, dict):
        raise LandingError(f"{path} is a {type(parsed).__name__}, not the object this stage reads")
    return parsed


def _backtest_pair_for_run(root: Path, run_id: str) -> tuple[Path, Path] | None:
    """The backtest artifact pair that records *this* run's fold plan, or None when nothing does.

    ``oxbow backtest`` writes under ``out/backtest/<run>/`` inside a pipeline and under a named
    directory when an operator points it at a corpus with ``--out``, so the directory name is not
    the identity. The artifact carries it: ``fold_plan_window.window_source`` is the path of the
    features manifest the fold plan was read from, and that path names the run. Requiring the match
    is what stops one run's ablation figures being landed under another run's id.
    """
    from oxbow.adapters.warehouse.landing import LandingError

    base = root / OUT_DIRNAME / BACKTEST_ARTIFACT_DIRNAME
    matched: list[tuple[Path, Path]] = []
    for ablation_path in sorted(base.glob(f"*/{BACKTEST_ABLATION_NAME}")):
        card_path = ablation_path.with_name(BACKTEST_CARD_NAME)
        if not card_path.is_file():
            continue
        document = _read_json_object(ablation_path)
        window = document.get("fold_plan_window")
        source = str(window.get("window_source") or "") if isinstance(window, Mapping) else ""
        if run_id in source or ablation_path.parent.name.upper() == run_id:
            matched.append((ablation_path, card_path))
    if not matched:
        return None
    if len(matched) > 1:
        raise LandingError(
            f"{len(matched)} backtest artifacts claim this run's fold plan: "
            f"{', '.join(path[0].as_posix() for path in matched)} — landing them together would "
            "put two ablation tables under one run id, so the operator picks the directory"
        )
    return matched[0]


def _backtest_tables(
    ctx: StageContext, run_id: str
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    """``policy``, ``policy_summary`` and the five backtest-document tables.

    A missing or fixture-provenanced artifact refuses these tables and nothing else: the
    analytical half of the handoff is additive, and "there is no backtest for this run" is a
    different finding from "the backtest failed to shape".

    The two policy tables carry an extra gate the five do not. ``PostgresWarehouseSink.write`` calls
    ``assert_writable_table`` first, and ``policy``/``policy_summary`` are absent from
    ``oxbow.ports.warehouse.WAREHOUSE_TABLES`` — the port's declared handoff vocabulary — so a write
    for either raises ``WarehouseTableError`` and the landing's single transaction rolls the whole
    run back: 43k scores, half a million rule hits and the queue gone, all because of a table the
    port never declared. So the shape runs, and its result is held back with a refusal naming the
    declaration that would let it through, rather than being handed to a sink that refuses it. The
    same gate covers the second gap: ``sink.write`` stamps ``run_id`` onto every row it inserts and
    ``policy`` declares no ``run_id`` column (a policy is a standing configuration;
    ``policy_summary`` is a run's measurement of it), so that table needs the stamp made
    conditional on the table having the column. Neither file is in this stage's edit scope, so both
    are reported rather than patched around here.
    """
    from oxbow.adapters.warehouse.landing import (
        LandingError,
        ablation_rows,
        backtest_fold_rows,
        fairness_rows,
        perturbation_rows,
        policy_rows,
        policy_summary_rows,
        validation_metric_rows,
    )
    from oxbow.backtest.config_io import load_backtest_config
    from oxbow.ports.warehouse import WAREHOUSE_TABLES
    from oxbow.quant.economics import load_economics

    names = (
        *POLICY_TABLES,
        "ablation_row",
        "validation_metric",
        "fairness_row",
        "perturbation_row",
        "backtest_fold",
    )
    tables: dict[str, list[dict[str, Any]]] = {}
    refusals: dict[str, list[str]] = {}
    base = (ctx.root / OUT_DIRNAME / BACKTEST_ARTIFACT_DIRNAME).as_posix()
    try:
        pair = _backtest_pair_for_run(ctx.root, run_id)
        if pair is None:
            message = (
                f"nothing under {base}*/ names run {run_id} in its fold_plan_window; the files "
                f"that would be needed are {BACKTEST_ABLATION_NAME} and {BACKTEST_CARD_NAME}, "
                "written by `oxbow backtest`"
            )
            return tables, {name: [message] for name in names}
        ablation = _read_json_object(pair[0])
        card = _read_json_object(pair[1])
        provenances = {
            str(variant.get("provenance"))
            for variant in ablation.get("variants") or []
            if isinstance(variant, Mapping)
        }
        if not provenances or provenances != {MEASURED_BACKTEST_PROVENANCE}:
            message = (
                f"{pair[0].as_posix()} records provenance {sorted(provenances)}, not "
                f"[{MEASURED_BACKTEST_PROVENANCE!r}]: a fixture or demo figure is never landed as a "
                "measurement (plan §19, DEV-003's provenance column)"
            )
            return tables, {name: [message] for name in names}
        resamples = int(load_backtest_config(ctx.root).bootstrap_resamples)
        economics = load_economics(ctx.root)
        tables["policy"], refusals["policy"] = policy_rows(ablation, card, economics)
        (
            tables["policy_summary"],
            refusals["policy_summary"],
        ) = policy_summary_rows(ablation, card, economics, run_id=run_id)
        for name in POLICY_TABLES:
            if name in WAREHOUSE_TABLES:
                continue
            shaped = tables.pop(name, [])
            refusals[name] = [
                f"{name} shaped {len(shaped)} row(s) from {pair[0].as_posix()} and handed them "
                f"back: {name} is not declared in "
                "oxbow.ports.warehouse.WAREHOUSE_TABLES, so assert_writable_table would refuse the "
                "write and the whole landing transaction would roll back. The port declaration, and "
                "(for `policy`) the unconditional run_id stamp in PostgresWarehouseSink.write, are "
                "the two changes that land these rows"
            ]
        tables["ablation_row"], refusals["ablation_row"] = ablation_rows(
            card, ablation, declared_resamples=resamples
        )
        tables["validation_metric"], refusals["validation_metric"] = validation_metric_rows(
            ablation, card
        )
        tables["fairness_row"], refusals["fairness_row"] = fairness_rows(card)
        tables["perturbation_row"], refusals["perturbation_row"] = perturbation_rows(card, ablation)
        tables["backtest_fold"], refusals["backtest_fold"] = backtest_fold_rows(ablation)
    except LandingError as exc:
        for name in names:
            refusals.setdefault(name, [f"the backtest artifact could not be shaped: {exc}"])
    return tables, refusals


def _frame_tables(
    ctx: StageContext,
    *,
    scored: pl.DataFrame,
    events: pl.DataFrame,
    corpus: pl.DataFrame | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    """The studio and evidence tables, each shaped from the run's own landed rows.

    The band's action and its review effort are not measurements of the run — they are the declared
    configuration the run consumed, read from ``config/scorecard.yaml`` and
    ``config/economics.yaml`` through their own loaders rather than restated here, the same way
    ``_attach_corpus_economics`` reads the analyst price and the floor.

    ``economics`` is priced off the same loaded configuration and is passed no Monte Carlo intervals,
    because the run recorded none to pass: propagating an account's exposure needs the fold's landed
    edge list, and ``oxbow score`` builds its graph in memory for the rules layer and never writes it
    (see :func:`_graph_tables`). The builder therefore returns no rows and one named refusal for a
    score-only run — which is reported per table rather than swallowed — and lands every account whose
    measured intervals a caller does supply.
    """
    from oxbow.adapters.warehouse.landing import (
        band_definition_rows,
        drift_period_rows,
        economics_rows,
        evidence_event_rows,
        scorecard_bin_rows,
        scorecard_point_rows,
        transaction_rows,
    )
    from oxbow.quant.economics import load_economics
    from oxbow.scoring.config import load_scorecard_config

    bands = load_scorecard_config(ctx.root).bands
    actions = {entry.band_id: entry.action for entry in bands.entries}
    economics = load_economics(ctx.root)
    minutes = {entry.band_id: economics.minutes_for(entry.band_id) for entry in bands.entries}

    tables: dict[str, list[dict[str, Any]]] = {}
    refusals: dict[str, list[str]] = {}
    tables["transaction"], refusals["transaction"] = transaction_rows(events)
    tables["evidence_event"], refusals["evidence_event"] = evidence_event_rows(events, scored)
    tables["band_definition"], refusals["band_definition"] = band_definition_rows(
        scored, actions=actions, review_minutes=minutes
    )
    tables["economics"], refusals["economics"] = economics_rows(
        scored, config=economics, corpus=corpus
    )
    tables["scorecard_bin"], refusals["scorecard_bin"] = scorecard_bin_rows(scored)
    tables["scorecard_point"], refusals["scorecard_point"] = scorecard_point_rows(scored)
    tables["drift_period"], refusals["drift_period"] = drift_period_rows(scored)
    return tables, refusals


def _graph_tables(
    ctx: StageContext, run_id: str
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    """``community``, ``graph_edge`` and ``account_membership`` from the graph stage's own artifact.

    The graph is read from ``out/graph/<run_id>/``, the directory the graph stage writes for the run
    it built. ``oxbow score`` builds a graph in memory for its rules layer and never lands it, so a
    score-only run has no graph artifact — and copying community numbers out of a *different* run's
    directory would be that run's measurement wearing this run's id, which is the one thing the
    partition key exists to prevent. So the absence is reported with the path that would fix it.
    """
    from oxbow.adapters.warehouse.landing import (
        LandingError,
        account_membership_rows,
        community_index_by_raw_label,
        community_rows,
        graph_edge_rows,
    )
    from oxbow.graph.persist import MANIFEST_NAME

    names = ("community", "graph_edge", "account_membership")
    directory = ctx.root / OUT_DIRNAME / GRAPH_ARTIFACT_DIRNAME / run_id
    nodes_path = directory / "nodes.parquet"
    pairs_path = directory / "pairs.parquet"
    if not nodes_path.is_file() or not pairs_path.is_file():
        message = (
            f"{directory.as_posix()} holds no nodes.parquet/pairs.parquet; the graph stage writes "
            "them, so `oxbow graph` and `oxbow score` have to run under one run id before "
            "community, graph_edge and account_membership can be read"
        )
        return {}, {name: [message] for name in names}
    try:
        nodes = pl.read_parquet(nodes_path)
        pairs = pl.read_parquet(pairs_path)
        manifest = _read_json_object(directory / MANIFEST_NAME)
        stats = manifest.get("stats") if isinstance(manifest, Mapping) else None
        index_by_raw, _, label_refusals = community_index_by_raw_label(nodes)
        communities, community_refusals = community_rows(nodes, pairs, stats=stats)
        memberships, membership_refusals = account_membership_rows(nodes, index_by_raw)
        edges, edge_refusals = graph_edge_rows(pairs, nodes)
    except (LandingError, OSError, ValueError) as exc:
        return {}, {name: [f"{directory.as_posix()}: {exc}"] for name in names}
    return (
        {"community": communities, "graph_edge": edges, "account_membership": memberships},
        {
            "community": label_refusals + community_refusals,
            "graph_edge": edge_refusals,
            "account_membership": membership_refusals,
        },
    )


def _existing_txn_ids(session: Any, wanted: Sequence[str], *, chunk: int = 500) -> set[str]:
    """The txn_ids already in the table, looked up in bounded blocks.

    ``transaction.txn_id`` is the primary key and the corpus is shared between runs, so a second run
    over overlapping events cannot re-land the rows the first one wrote. Asking which are already
    there is what keeps a re-run idempotent instead of ending in one rollback of the whole warehouse.
    """
    from sqlalchemy import select

    from oxbow.adapters.warehouse.models import Base

    table = Base.metadata.tables["transaction"]
    ordered = sorted(wanted)
    found: set[str] = set()
    for start in range(0, len(ordered), chunk):
        block = ordered[start : start + chunk]
        rows = session.execute(select(table.c.txn_id).where(table.c.txn_id.in_(block))).scalars()
        found.update(str(value) for value in rows)
    return found


def _rows_already_landed(session: Any, table_name: str, run_id: str) -> int:
    """How many rows one table already holds for this run — the idempotency check.

    ``evidence_event`` has no unique key to collide with, so a second landing of an open run would
    quietly double the case timeline. The count is asked before the write, not after.
    """
    from sqlalchemy import func, select

    from oxbow.adapters.warehouse.models import Base

    table = Base.metadata.tables[table_name]
    return int(
        session.scalar(select(func.count()).select_from(table).where(table.c.run_id == run_id)) or 0
    )


def land_warehouse_rows(ctx: StageContext, handle: StageHandle, *, run_id: str) -> int:
    """Land the run's rows in every warehouse table its artifacts can describe, and say what it could not.

    Called from the score stage, not from a verb of its own: 01 §D fixes the CLI as
    `oxbow ingest graph score backtest` — "no more, no fewer", asserted by the P0 gate — so a
    fifth stage command would be a plan violation, while the landing it performs is exactly the
    work ``jobs.PIPELINE_STAGES`` had been promising a queued pipeline job would do.

    The slice, not the corpus. ``account`` describes the accounts that were scored, so the
    events it aggregates are exactly those whose ``txn_id`` the run's feature frame carries —
    re-deriving the sample independently would be a second sampler disagreeing with the first,
    and the run identity that selected the slice is salted and cannot be replayed here.

    A run whose folds refused calibration still lands its scores: ``score`` carries
    ``calibration_kind='uncalibrated'`` with the fold's refusal reason, and the four
    measurement columns stay NULL rather than taking a zero (see ``landing.score_rows`` and
    migration 0003). What is still refused is a row with no score at all, and refusals of both
    kinds are reported with a count, not swallowed, because "0 rows landed" and "0 rows could be
    landed for this named reason" are different findings and the second is the one an operator
    can act on. The analytical tables are reported the same way, table by table: on the landed
    40k run ``backtest_fold``, ``drift_period`` and the three graph tables refuse for named
    reasons, and the run still lands everything it did measure.
    """
    import os

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from oxbow.adapters.warehouse.landing import (
        LandingError,
        account_rows,
        assert_run_identifiable,
        rule_hit_rows,
        score_rows,
    )
    from oxbow.adapters.warehouse.postgres import PostgresWarehouseSink
    from oxbow.ports.warehouse import RunState

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        # Not a failure. The null-file warehouse under out/warehouse is what `make demo` and
        # the offline boot read, and a host with no Postgres up is the ordinary case for a
        # pipeline run. The line exists so nobody reads the null-file rows as "already in the
        # database": on this host the API's live read model has nothing to serve.
        ctx.echo(
            "[warehouse] DATABASE_URL is unset, so only the null-file warehouse holds this run.",
        )
        return EXIT_OK

    assert_run_identifiable(run_id)
    score_dir = ctx.root / "out" / "score" / run_id
    feature_dir = ctx.root / "out" / "features" / run_id
    scored_path = score_dir / "scored_rows.parquet"
    frame_path = feature_dir / "features.parquet"
    if not scored_path.is_file() or not frame_path.is_file():
        ctx.echo(
            f"[warehouse] REFUSED: {scored_path} and {frame_path} are the two artifacts this "
            "landing reads, and one of them is not there.",
            err=True,
        )
        return EXIT_REFUSED

    scored = pl.read_parquet(scored_path)
    # DEV-032: the run's own measured exposure lives in this artifact, not in the scored
    # frame, which carries no exposure column at all. Reconstructing E_i from activity
    # features instead prices every PaySim account at zero, because the plan's 24-hour cap
    # is degenerate on a corpus where no account both receives and sends inside the window
    # (0 of 43,720). So the corpus is read here and threaded into the one table that needs
    # it; when it is absent the queue is knowingly built on a proxy, and that is said out
    # loud rather than left for a reader to discover in the numbers.
    corpus_path = score_dir / "backtest_corpus.parquet"
    corpus = pl.read_parquet(corpus_path) if corpus_path.is_file() else None
    if corpus is None:
        ctx.echo(
            f"[warehouse] {corpus_path} is not there, so economics falls back to the "
            "activity-feature reconstruction of E_i. On this corpus that prices every "
            "account at zero exposure; the rows will land and every expected value will be "
            "negative. Read that as the proxy it is, not as a finding about the money.",
            err=True,
        )
    # The frame is one row per event in the slice: that set of txn_ids IS the sample.
    slice_ids = pl.read_parquet(frame_path, columns=["txn_id"]).get_column("txn_id").to_frame()

    payload: dict[str, list[dict[str, Any]]] = {}
    refusals: dict[str, list[str]] = {}
    try:
        events = _read_canonical_for_ids(ctx, slice_ids)
        accounts = account_rows(events)
        scores, score_refusals = score_rows(scored)
        payload["account"] = accounts
        payload["score"] = scores
        payload["rule_hit"] = rule_hit_rows(scored)
        for shapes in (
            _frame_tables(ctx, scored=scored, events=events, corpus=corpus),
            _backtest_tables(ctx, run_id),
            _graph_tables(ctx, run_id),
        ):
            payload.update(shapes[0])
            for name, reasons in shapes[1].items():
                refusals.setdefault(name, []).extend(reasons)
    except LandingError as exc:
        ctx.echo(f"[warehouse] REFUSED: {exc}", err=True)
        handle.mark_failed(str(exc)[:300])
        return EXIT_FAILED

    engine = create_engine(url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)  # SQLAlchemy's own naming
    skipped: dict[str, int] = {}
    with Session() as session:
        sink = PostgresWarehouseSink(session)
        try:
            try:
                sink.run_state(run_id)
            except Exception:  # "no such run" is the normal case here
                session.rollback()
                # The same identity the null-file ledger was opened with, so a row in either
                # warehouse names the seed, the config hash and the model version that produced
                # it. A landed run whose provenance cannot be recovered is unauditable.
                sink.open_run(
                    run_id,
                    seed=ctx.cfg.seed,
                    timezone=ctx.cfg.deployment_timezone,
                    provenance="pipeline",
                    config_hash=_config_hash(ctx.config_dir),
                    model_version=f"{MODEL_VERSION_PREFIX}/{__version__}",
                    dataset_ref=(ctx.root / DATA_DIRNAME / INTERIM_DIRNAME).as_posix(),
                )
            # Idempotency, once per reason. A transaction's key is the corpus id, so an event that
            # is already in the table stays with the run that landed it; a timeline row has no key
            # at all, so the table's own contents decide whether this run already has one.
            already = _existing_txn_ids(
                session, [str(row["txn_id"]) for row in payload.get("transaction", [])]
            )
            if already:
                skipped["transaction"] = len(already)
                payload["transaction"] = [
                    row for row in payload["transaction"] if str(row["txn_id"]) not in already
                ]
            for name in ("evidence_event",):
                if name not in payload:
                    continue
                existing = _rows_already_landed(session, name, run_id)
                if existing:
                    skipped[name] = existing
                    payload[name] = []
            written: dict[str, int] = {}
            for name in LANDING_REPORT_ORDER:
                if name not in payload:
                    continue
                written[name] = sink.write(name, run_id, payload[name])
            sink.complete_run(run_id, RunState.COMPLETE)
            session.commit()
        except Exception as exc:  # a rejected row must not half-land
            session.rollback()
            ctx.echo(
                f"[warehouse] FAILED: {type(exc).__name__}: {str(exc)[:300]} — nothing was "
                "committed, so the run keeps whatever it landed before this attempt",
                err=True,
            )
            handle.mark_failed(f"warehouse write rejected: {type(exc).__name__}")
            return EXIT_FAILED
    engine.dispose()

    for name in LANDING_REPORT_ORDER:
        lines = refusals.get(name) or []
        if name not in written:
            # A table that landed nothing still owes the reader its reason. `written` only holds
            # tables the sink accepted a write for, so a table that refused wholesale — or that the
            # port will not let be written at all — used to print nothing, which left an operator
            # with an empty Postgres table and no line to grep. Silence is how a refusal turns into
            # a mystery.
            if not lines:
                continue
            ctx.echo(f"[warehouse] {name}: 0 row(s) landed under run {run_id}")
        else:
            note = (
                f" ({skipped[name]:,} row(s) already landed under another key, skipped)"
                if name in skipped
                else ""
            )
            ctx.echo(
                f"[warehouse] {name}: {written[name]:,} row(s) landed under run {run_id}{note}"
            )
        for reason in lines[:REFUSAL_LINES_SHOWN]:
            ctx.echo(f"[warehouse]   {name} refused: {reason[:REFUSAL_PREVIEW_CHARS]}")
        if len(lines) > REFUSAL_LINES_SHOWN:
            ctx.echo(
                f"[warehouse]   {name}: {len(lines) - REFUSAL_LINES_SHOWN:,} further refusal line(s)"
            )
    ctx.echo(
        f"[warehouse] score: {len(scores):,} landed, {len(score_refusals):,} refused"
        + (f" — first reason: {score_refusals[0][:140]}" if score_refusals else "")
    )
    if not written["score"]:
        ctx.echo(
            "[warehouse] RESULT: PARTIAL. The queue cannot render scores that were refused; "
            "see the refusal above (DEV-024 is the calibration arithmetic behind it).",
        )
        return EXIT_OK
    ctx.echo(
        "[warehouse] RESULT: OK — every table the run's artifacts measured is readable by the API; "
        "any table that refused is named above with the reason it refused."
    )
    return EXIT_OK


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
    and it is the entry point being called. Its ``--corpus`` path is real: it drives
    ``oxbow.backtest.splits`` for folds, ``oxbow.models.scorer.WalkForwardScorer`` for the
    calibrated scorer and ``oxbow.backtest.allocators.P5Allocator`` for the queue, and it
    runs the eight honest rows plus the leakage control through the same harness the fake
    demonstration uses. If the corpus cannot support the configured embargo the splits module
    refuses, and that refusal is printed verbatim rather than softened. ``--demo-fakes`` runs
    the whole harness on hand-computed fixtures: the run is real, every figure is labelled
    ``provenance=fake_harness``, and no part of it is quoted as a corpus result.

    With no ``--corpus`` given (the pipeline chain), the stage uses the per-account corpus the
    score stage landed under ``out/score/<run_id>/`` in this same run; if that file is absent
    the score stage refused before writing it, which is reported by name rather than faked.
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
            "provenance=fake_harness: the harness, every fold, the ablation rows and the "
            f"leakage control ran for real and wrote {payload}; every figure in it verifies "
            "the harness and is NOT a corpus result"
        )
        ctx.echo(f"[backtest] artifact: {payload} ({_count(handle.rows)} fold-policy rows)")
        return EXIT_OK if code == 0 else EXIT_FAILED

    if corpus is None:
        landed = ctx.out_root / SCORE_ARTIFACT_DIRNAME / ctx.run_id / "backtest_corpus.parquet"
        if landed.is_file():
            corpus = landed
        else:
            gap = (
                "no per-account scored corpus exists to price: the score stage did not write "
                f"one to {landed}. Either the model stack refused (a corpus timeline too short "
                "for the configured embargo, reported by name in the score stage) or `oxbow "
                "score` has not run under this --run-id. Point `oxbow backtest --corpus PATH` "
                "at a real per-account corpus, or run `oxbow backtest --demo-fakes` for the "
                "harness self-check (labelled provenance=fake_harness, not a result)."
            )
            ctx.echo(f"[backtest] UNAVAILABLE: {gap}", err=True)
            handle.mark_unavailable(gap)
            return EXIT_FAILED

    ctx.echo(f"[backtest] corpus: {corpus}")
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
        code = _execute(ctx, name, bodies[name], terminal=False)
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
    "verified" means. The script's own vocabulary is kept and printed verbatim: a chain
    that cannot be reached is SKIPPED with its reason and never counted as a pass, so a
    host where no decision has been recorded reports SKIPPED for all three chains and a
    zero that says so out loud. A broken link -- an edited row, a deleted row, a
    re-linked chain -- is a non-zero exit naming the sequence number.
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

    ``data/DATASET_CARD.md`` is the one document it does *not* write. A dataset card carries
    authored judgement that no artifact can generate, and a past run of this command
    overwrote it. :mod:`oxbow.dataset_card` resolves each figure the card states against the
    artifact or config that owns it, fails naming the field and both values when they drift,
    and reports the checks it could not run instead of counting them as passes.
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
