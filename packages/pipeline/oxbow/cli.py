"""The four pipeline stage verbs.

01 D: ``oxbow ingest graph score backtest`` are the four stages, run separately
and resumably. ``make pipeline`` chains them and streams stage events over SSE so
the UI ledger fills with real rows and real elapsed times.

Each stage in P0 prints its planned work and refuses to pretend. 00 B: never
report a result you did not observe. A stage that has not been built yet says so
in those words rather than exiting zero on an empty run.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer

from oxbow import __version__
from oxbow.config import load_pipeline_config

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


def _repo_root() -> Path:
    """Walk up to the repo root: the directory holding config/pipeline.yaml."""
    here = Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "pipeline.yaml").is_file():
            return candidate
    return here


def _announce(stage: str, root: Path) -> None:
    """Print what this stage is about to do, and the seed it will obey."""
    cfg = load_pipeline_config(root)
    typer.echo(f"[{stage}] repo root: {root}")
    typer.echo(f"[{stage}] seed: {cfg.seed}")
    typer.echo(f"[{stage}] timezone: {cfg.deployment_timezone}")
    typer.echo(f"[{stage}] planned work: {_planned(stage)}")


def _planned(stage: str) -> str:
    """The declared scope of each stage. Printed, never guessed at runtime."""
    return {
        "ingest": (
            "read declared sources through SourceAdapter, contract-check with Pandera, "
            "canonicalise to the canonical event v1, re-hash identifiers, quarantine "
            "the unmappable, write Parquet to data/interim and register DuckDB views"
        ),
        "graph": (
            "build the directed time-stamped multigraph, type rails and external nodes, "
            "enumerate time-respecting value-retaining cycles, detect communities"
        ),
        "score": (
            "fire rules R1-R12, fit the WOE scorecard, train the GBM and Isolation "
            "Forest, calibrate, fuse, persist SHAP, write scored rows with lineage"
        ),
        "backtest": (
            "walk forward over 5 purged folds with a 30-day embargo, price every alert, "
            "allocate under capacity, report economics, drawdown, VaR and ES"
        ),
    }[stage]


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", help="Print the version and exit."),
) -> None:
    """OXBOW command line. One verb per pipeline stage, run separately."""
    if version:
        typer.echo(__version__)


def _stage_command(stage: str) -> Any:
    """Build a Typer command for one stage.

    The P0 contract is that the verb EXISTS and prints its planned work. Later
    phases replace the body with the real stage, and the gate for that phase
    proves the swap happened.
    """

    @app.command(
        stage,
        help=_planned(stage),
        no_args_is_help=False,
    )
    def _run(
        run_id: str = typer.Option(None, "--run-id", help="Resume or name a specific run."),
        config_dir: Path = typer.Option(None, "--config-dir", help="Override config/."),
        stream: bool = typer.Option(False, "--stream", help="Stream stage events over SSE."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Print planned work only."),
    ) -> None:
        root = _repo_root() if config_dir is None else Path(config_dir).parent
        _announce(stage, root)
        # The options are accepted and echoed so the interface is stable, but the
        # stage body that consumes them lands with the stage. Echoing them is
        # honest: the operator can see the flag reached the CLI.
        if run_id:
            typer.echo(f"[{stage}] requested run_id: {run_id}")
        if stream:
            typer.echo(f"[{stage}] stream: SSE stage events requested")
        if dry_run:
            return
        typer.echo(
            f"[{stage}] NOT IMPLEMENTED in P0. This verb exists and its scope is "
            f"declared; the stage body lands in its own phase and the phase gate "
            f"proves it. Exiting non-zero so no caller mistakes this for a run."
        )
        raise typer.Exit(code=2)

    return _run


for _stage in STAGES:
    _stage_command(_stage)


@app.command("pipeline")
def pipeline_cmd(
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream over SSE."),
    config_dir: Path = typer.Option(None, "--config-dir", help="Override config/."),
) -> None:
    """Run all four stages in order, streaming stage events over SSE.

    Watching real work happen for forty seconds is more convincing than any
    animation, which is why the UI renders a ledger rather than a spinner.
    """
    root = _repo_root() if config_dir is None else Path(config_dir).parent
    cfg = load_pipeline_config(root)
    typer.echo(f"[pipeline] seed {cfg.seed} | stream={stream} | stages={','.join(STAGES)}")
    for stage in STAGES:
        typer.echo(f"[pipeline] -> {stage}: {_planned(stage)}")
    typer.echo("[pipeline] NOT IMPLEMENTED in P0. Exiting non-zero.")
    raise typer.Exit(code=2)


@app.command("verify-determinism")
def verify_determinism_cmd() -> None:
    """Run the pipeline twice and diff artifact checksums (01 D).

    Two runs of ``make pipeline`` on the same input must produce identical Parquet
    checksums. If they do not, that is a P1 bug and everything else waits.
    """
    typer.echo("[verify-determinism] runs the pipeline twice, diffs artifact checksums")
    typer.echo("[verify-determinism] NOT IMPLEMENTED in P0. Exiting non-zero.")
    raise typer.Exit(code=2)


@app.command("verify-audit")
def verify_audit_cmd() -> None:
    """Walk the decision hash chain, print OK or the first broken link."""
    typer.echo("[verify-audit] walks case_decisions and audit_log, verifying each hash")
    typer.echo("[verify-audit] NOT IMPLEMENTED in P0. Exiting non-zero.")
    raise typer.Exit(code=2)


@app.command("packet")
def packet_cmd(
    all_cases: bool = typer.Option(False, "--all", help="Render every pinned case."),
) -> None:
    """Render a case packet to out/packets/ (WeasyPrint, P9)."""
    typer.echo("[packet] renders evidence, SHAP, subgraph, decision and integrity to PDF")
    typer.echo(f"[packet] scope: {'every pinned case' if all_cases else 'one sample case'}")
    typer.echo("[packet] NOT IMPLEMENTED in P0. Exiting non-zero.")
    raise typer.Exit(code=2)


@app.command("eval")
def eval_cmd() -> None:
    """Regenerate every metric, curve, frontier and ablation row in the docs.

    ``make eval`` must reproduce the README's numbers exactly; if it does not,
    that is a bug with the same severity as a crash (spec 14).
    """
    typer.echo("[eval] regenerates every published number from a real run")
    typer.echo("[eval] NOT IMPLEMENTED in P0. Exiting non-zero.")
    raise typer.Exit(code=2)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover - module executed as a script
    main()


def _json_dumps(payload: dict[str, Any]) -> str:
    """Deterministic JSON for anything written to disk or streamed over SSE."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _stage_sequence() -> Sequence[str]:
    """The canonical stage order. Resumed strictly, never reordered."""
    return STAGES
