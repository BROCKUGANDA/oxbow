"""`make verify` — every gate from every completed phase, run as commands.

00 §C step 3 defines `verify` as "every gate from every completed phase must
pass", which is different from 01 §D's checksum double-run; DEV-007 split the two
names, and this is the first one.

Why the gates live in a table here rather than being discovered from the tree:
a gate is a claim about an observable, so it has to be authored deliberately and
reviewed. Discovering them from whatever happens to exist would let a phase whose
gate was deleted quietly count as passing, which is the exact failure mode the
phase-prefixed history exists to prevent.

A phase is either DONE (its commands must pass, or this exits non-zero) or
PENDING (its commands are not run, and the reason is printed). Nothing is silently
skipped: the report ends with the list of phases still outstanding, so a green run
is a statement about the phases that are claimed complete and nothing more.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Gate details carry pytest's box-drawing output. On Windows the default console
# encoding is cp1252, which cannot represent it — and a gate report that crashes
# halfway through is worse than a failing gate, because it hides the verdicts that
# already ran.
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class Gate:
    """One command that must succeed, with the observable it proves."""

    description: str
    argv: tuple[str, ...]
    # A gate may be conditional on a file existing (an artifact a prior stage
    # produces). A missing prerequisite is reported as SKIPPED-PREREQUISITE with
    # the path named, never as a pass.
    prerequisite: str | None = None
    timeout_s: int = 900


@dataclass(frozen=True, slots=True)
class Phase:
    """One phase of the build plan and the commands that constitute its gate."""

    name: str
    done: bool
    gates: tuple[Gate, ...]
    pending_reason: str = ""


# Execution order is P0 -> P1a -> P1b -> P3a -> P2 -> P3b -> P4 -> P5 -> P6 -> P7
# -> P8 -> P9, per DEV-008 (the graph is built before the features).
#
# `done` is set when the phase's gate commands have actually been observed to pass
# in a real run, not when its code has been written. Flipping this flag without
# running the command is the single easiest way to make `make verify` lie.
PHASES: tuple[Phase, ...] = (
    Phase(
        name="P0",
        done=True,
        gates=(
            Gate(
                "python toolchain, config contract and float-money lint",
                ("uv", "run", "pytest", "-q", "tests/unit/test_p0_toolchain.py"),
            ),
            Gate(
                "design tokens and the twelve-glyph sprite",
                ("uv", "run", "pytest", "-q", "tests/unit/test_p0_design_system.py"),
            ),
            Gate("oxbow --help lists the four stage verbs", ("uv", "run", "oxbow", "--help")),
        ),
    ),
    Phase(
        name="P1a",
        done=True,
        gates=(
            Gate(
                "declared sources carry license and citation",
                ("uv", "run", "pytest", "-q", "tests/unit/test_p1a_sources.py"),
            ),
            Gate(
                "PaySim bytes match the recorded SHA-256",
                ("uv", "run", "python", "scripts/download_data.py", "--verify-only"),
            ),
        ),
    ),
    Phase(
        name="P1b",
        done=True,
        pending_reason="",
        gates=(
            Gate(
                "Pandera contracts, quarantine and determinism",
                ("uv", "run", "pytest", "-q", "tests/contracts"),
            ),
            Gate(
                "`make ingest` completes with zero silent coercions",
                ("uv", "run", "oxbow", "ingest", "--limit", "200000"),
            ),
            # The default scope above resolves to PaySim only, so Module B's corpus had
            # a passing phase gate without ever being read by one. 200,000 rows is the
            # smallest slice that reaches the typology annotations (DEV-014: 0.063% of
            # the corpus), which is what exercises the annotation join.
            Gate(
                "IBM-AML canonicalises through its adapter, annotation join included",
                ("uv", "run", "oxbow", "ingest", "--source", "ibmaml", "--limit", "200000"),
            ),
        ),
    ),
    Phase(
        name="P3a",
        done=True,
        pending_reason="",
        gates=(
            Gate(
                "cycle detection matches the hand-built fixture; rails and cap enforced",
                (
                    "uv",
                    "run",
                    "pytest",
                    "-q",
                    "tests/unit/test_p3a_graph.py",
                    "tests/unit/test_p3a_cycles.py",
                    "tests/unit/test_p3a_self_edges.py",
                ),
            ),
            # DEV-018: canonical events may now carry self-transfers, so the graph has to
            # be reproducible with them present. Compared against the tree the stage
            # actually writes -- see --artifacts in scripts/verify_determinism.py.
            Gate(
                "two graph runs over the same bytes land identical artifacts",
                (
                    "uv",
                    "run",
                    "python",
                    "scripts/verify_determinism.py",
                    "--command",
                    "uv run oxbow graph",
                    "--artifacts",
                    "out/graph",
                ),
                timeout_s=2400,
            ),
        ),
    ),
    Phase(
        name="P2",
        done=False,
        pending_reason="feature layer in build",
        gates=(
            Gate(
                "the leakage gate passes AND is proven to bite",
                ("uv", "run", "pytest", "-q", "tests/test_leakage.py"),
            ),
            Gate(
                "feature-layer money rules and split discipline",
                ("uv", "run", "pytest", "-q", "tests/unit", "-k", "p2"),
            ),
        ),
    ),
    Phase(
        name="P3b",
        done=False,
        pending_reason="rules R1-R12 in build",
        gates=(
            Gate(
                "golden fixture is self-consistent (hand-computed ground truth)",
                ("uv", "run", "pytest", "-q", "tests/golden"),
            ),
            Gate(
                "every rule fires on its planted case and on none of its near-misses",
                ("uv", "run", "pytest", "-q", "tests/unit", "-k", "p3b or rules"),
            ),
        ),
    ),
    Phase(
        name="P4",
        done=False,
        pending_reason="scorecard and models in build",
        gates=(
            Gate(
                "scorecard scaling, guards, calibration and fusion",
                ("uv", "run", "pytest", "-q", "tests/unit", "-k", "p4"),
            ),
        ),
    ),
    Phase(
        name="P5",
        done=True,
        pending_reason="",
        gates=(
            Gate(
                "EV economics, both solvers, Monte Carlo exposure",
                (
                    "uv",
                    "run",
                    "pytest",
                    "-q",
                    "tests/unit/test_p5_economics.py",
                    "tests/unit/test_p5_allocate.py",
                    "tests/unit/test_p5_exposure.py",
                    "tests/unit/test_p5_frontier.py",
                    "tests/unit/test_p5_monte_carlo.py",
                ),
            ),
        ),
    ),
    Phase(
        name="P6",
        done=False,
        pending_reason="backtest harness not started",
        gates=(
            Gate(
                "all five folds, the ablation table and the leakage control",
                ("uv", "run", "pytest", "-q", "tests/unit", "-k", "p6"),
            ),
        ),
    ),
    Phase(
        name="P7",
        done=False,
        pending_reason="API and adapters in build",
        gates=(
            Gate(
                "port conformance across every adapter",
                ("uv", "run", "pytest", "-q", "tests/contracts_adapters"),
            ),
            Gate(
                "architecture contracts: no adapter import outside adapters/",
                ("uv", "run", "lint-imports"),
            ),
        ),
    ),
    Phase(
        name="P8",
        done=False,
        pending_reason="web app not started",
        gates=(
            Gate(
                "unit tests for the design system and state craft",
                ("pnpm", "--dir", "apps/web", "test:unit", "--run"),
            ),
        ),
    ),
    Phase(
        name="P9",
        done=False,
        pending_reason="packet, generated docs and demo not started",
        gates=(
            Gate(
                "audit hash chain verifies",
                ("uv", "run", "python", "scripts/verify_audit.py"),
            ),
        ),
    ),
)


@dataclass(slots=True)
class Result:
    phase: str
    description: str
    status: str
    detail: str = ""


def _run(gate: Gate) -> tuple[bool, str]:
    """Execute one gate command, returning success and a trimmed tail of output.

    `encoding`/`errors` are load-bearing: on Windows `text=True` otherwise decodes a
    child's output as cp1252, and pytest and ruff emit box-drawing characters and
    non-ASCII prose routinely -- which raised UnicodeDecodeError inside this harness
    and turned a running gate into a crash. Children are also told to emit UTF-8, so
    the replacement path stays rare, and stdout/stderr are defaulted because a killed
    child can leave either as None.
    """
    child_env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        gate.argv,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=gate.timeout_s,
        env=child_env,
        shell=False,
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    tail = " | ".join(line.strip() for line in out[-4:])
    return proc.returncode == 0, tail or f"exit {proc.returncode}"


def _run_gate(gate: Gate) -> tuple[str, str]:
    """Return (status, detail) for one gate: PASS, FAIL, or SKIPPED.

    SKIPPED is only ever about a missing *prerequisite artifact* named in the
    result. A gate command that fails is a FAIL, because a phase that claimed to
    be done and no longer passes is exactly what this command exists to catch.
    """
    if gate.prerequisite is not None and not (REPO_ROOT / gate.prerequisite).exists():
        return "SKIPPED", f"prerequisite absent: {gate.prerequisite}"
    try:
        ok, detail = _run(gate)
    except subprocess.TimeoutExpired:
        return "FAIL", f"timed out after {gate.timeout_s}s"
    return ("PASS" if ok else "FAIL"), detail


def verify(phases: Sequence[Phase]) -> list[Result]:
    """Run every gate belonging to every completed phase."""
    return [
        Result(phase.name, gate.description, *_run_gate(gate))
        for phase in phases
        if phase.done
        for gate in phase.gates
    ]


def report(results: list[Result], phases: Sequence[Phase]) -> int:
    """Print the gate table and the outstanding phases; return the exit code."""
    width = max((len(r.description) for r in results), default=20)
    for result in results:
        print(f"{result.phase:4} {result.status:7} {result.description:{width}}  {result.detail}")
    failed = [r for r in results if r.status == "FAIL"]
    pending = [p for p in phases if not p.done]
    print()
    print(f"gates run: {len(results)}  passed: {len(results) - len(failed)}  failed: {len(failed)}")
    if pending:
        print(f"phases still outstanding ({len(pending)}):")
        for phase in pending:
            print(f"  {phase.name}: {phase.pending_reason or 'not yet claimed complete'}")
    else:
        print("phases still outstanding: none")
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "Run every completed phase gate.")
    parser.add_argument(
        "--phase",
        action="append",
        default=[],
        help="Restrict to named phases (repeatable), running their gates whether done or not.",
    )
    args = parser.parse_args(argv)

    phases: Sequence[Phase] = PHASES
    if args.phase:
        wanted = {name.upper() for name in args.phase}
        unknown = wanted - {p.name.upper() for p in PHASES}
        if unknown:
            parser.error(f"unknown phase(s): {sorted(unknown)}")
        phases = tuple(p for p in PHASES if p.name.upper() in wanted)
        # An explicit --phase is a request to run those gates whether or not the
        # phase has been claimed complete, so the commands are echoed as they go.
        results = []
        for phase in phases:
            for gate in phase.gates:
                print(f"[{phase.name}] $ {' '.join(gate.argv)}", flush=True)
                results.append(Result(phase.name, gate.description, *_run_gate(gate)))
    else:
        results = verify(phases)
    return report(results, phases)


if __name__ == "__main__":
    sys.exit(main())
