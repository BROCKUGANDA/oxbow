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
    # Run the command somewhere other than the repo root. A JS test runner resolves its
    # config and its `include` globs against the working directory, so the P8 gate has to
    # run inside apps/web; passing a relative --config instead breaks esbuild's own path
    # resolution, which is a more confusing failure than stating where the command runs.
    cwd: str | None = None
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
            # Withdrawn on the evidence, and left out rather than slowed down or
            # narrowed: `oxbow graph` takes no input root, so it re-reads whatever
            # batches happen to sit in data/interim. After three ingest runs that was
            # 600k+ events and this gate ran past 26 minutes without finishing -- a
            # phase gate whose runtime and compared byte-set grow with unrelated
            # artifacts on disk is not reproducible in either dimension, which is the
            # one property a determinism gate may not trade away. The claim itself was
            # verified by hand on a bounded corpus: 5 graph artifacts byte-identical
            # across two runs (STATE.md ledger). It returns with --in/--dataset on the
            # graph verb so the gate can pin its own input.
        ),
    ),
    Phase(
        name="P2",
        done=True,
        pending_reason="",
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
        done=True,
        pending_reason="",
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
        pending_reason="models/ and scoring/ exist with tests, but nothing has trained end to end because the score stage stops at the bridge seam",
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
        pending_reason="backtest/ modules and metrics tests exist; no fold has produced a real number, so the model and economics cards carry placeholders",
        gates=(
            Gate(
                "all five folds, the ablation table and the leakage control",
                ("uv", "run", "pytest", "-q", "tests/unit", "-k", "p6"),
            ),
        ),
    ),
    Phase(
        name="P7",
        done=True,
        pending_reason="",
        gates=(
            Gate(
                "port conformance across every adapter",
                ("uv", "run", "pytest", "-q", "tests/contracts_adapters"),
            ),
            Gate(
                "architecture contracts: no adapter import outside adapters/",
                ("uv", "run", "lint-imports"),
            ),
            # The write path is the phase. Before this line existed, nothing had ever
            # imported apps/api at all: the routers 500'd on their own response
            # validation, and jobs.py enqueued a dotted name whose module was missing,
            # so no queued job had ever run. These four files are the only contact the
            # API, the outbox drain, the audit chain and the worker have with a real
            # Postgres, which is the only place any of it can be observed.
            Gate(
                "the served API, the append race, the outbox and the RQ worker",
                (
                    "uv",
                    "run",
                    "pytest",
                    "-q",
                    "tests/integration/test_p7_api.py",
                    "tests/integration/test_p7_worker.py",
                    "tests/integration/test_p7_session_hygiene.py",
                    "tests/integration/test_envelope_doctrine.py",
                ),
            ),
        ),
    ),
    Phase(
        name="P8",
        done=False,
        # Re-measured on the cached chromium on 2026-09-27 after the queue page was rebuilt
        # around slot-matched geometry: the CLS that was the open defect is gone. What is open
        # now is one spec that stopped being about geometry and became about the API.
        pending_reason=(
            "24 of 26 playwright specs pass and one is a recorded skip -- the 1,500-node "
            "fps probe, which the null-file warehouse cannot draw and says so rather than "
            "passing vacuously. Measured CLS is 0.000000 on /alerts and 0.000000 on "
            "/cases/[id] against a zero budget, and 0.000438 on /dev/states against 0.001. "
            "51 vitest tests over 11 files, tsc --noEmit clean, biome clean at zero findings. "
            "The one red spec is the dataset-fidelity clause driven against the live API: "
            "13 of /model's panes never leave their loading state even though their route "
            "answered 200, so the pane-error tier DESIGN.md §5 promises is not reached on "
            "that path, and /network's node shape still diverges from the client decoder "
            "(apps/api serves id/label/is_seed, contract.ts:773 requires "
            "key/node_type/flagged/is_cycle_member/hops/true_size)"
        ),
        gates=(
            # `node node_modules/vitest/vitest.mjs run` -- not a package-manager script, and
            # not `node_modules/.bin/vitest` either: that is a shell script, and subprocess on
            # Windows cannot CreateProcess one (WinError 2). Going through node is the same
            # resolution order the .CMD shim uses, so the gate measures the installed tree
            # whoever installed it, which is the whole point of a gate.
            #
            # The declared toolchain stays pnpm: plan §T2 pins `packageManager:
            # "pnpm@9.15.9"` with a committed `pnpm-lock.yaml`, and §13 puts `pnpm audit` in
            # CI. This host has no pnpm, which is a fact about the host -- so it is worked
            # around here, in the gate, and not in the manifest. DEV-022 records the Bun
            # proposal as rejected, and
            # test_the_declared_js_toolchain_is_the_one_every_recipe_uses holds the whole
            # chain (manifest, Dockerfile, Makefile, pre-commit, this table) to that answer.
            #
            # prerequisite= names the entry point: an uninstalled tree reads SKIPPED with the
            # path in the report rather than crashing, and never reads as a pass.
            #
            # cwd="apps/web" is load-bearing: vitest resolves its config and its
            # `include: tests/unit/**` globs against the working directory, and passing a
            # relative --config instead breaks esbuild's own path resolution.
            Gate(
                "unit tests for the design system and state craft",
                ("node", "node_modules/vitest/vitest.mjs", "run"),
                cwd="apps/web",
                prerequisite="apps/web/node_modules/vitest/vitest.mjs",
            ),
            # The two checks `make typecheck` and `make lint-web` name, gated the same way.
            # Both ran for real on 2026-09-27: tsc emitted zero errors, and biome zero findings
            # over 86 files after 76 were cleared -- 60 of them in the icon codegen, where
            # noUnusedTemplateLiteral and useTemplate oscillate against each other on
            # multi-line SVG literals, which is what the scoped override in apps/web/biome.json
            # is for. The other a11y rule turned off there is the mirror image of
            # useSemanticElements, already off: the ARIA list-box pattern this app's keyboard
            # routes implement puts role="listbox" on a ul, which is what that rule rejects.
            Gate(
                "typecheck every route and component",
                ("node", "node_modules/typescript/bin/tsc", "--noEmit"),
                cwd="apps/web",
                prerequisite="apps/web/node_modules/typescript/bin/tsc",
            ),
            Gate(
                "biome lint and format (make lint-web)",
                ("node", "node_modules/@biomejs/biome/bin/biome", "check", "."),
                cwd="apps/web",
                prerequisite="apps/web/node_modules/@biomejs/biome/bin/biome",
            ),
        ),
    ),
    Phase(
        name="P9",
        done=False,
        # The seeder exists now (scripts/demo_seed.py), and it refuses to snapshot a
        # warehouse with no score, fold or decision rather than producing the blank demo
        # STATE.md item 11 warned about -- so what is left is the evidence itself, not the
        # script. The packet is blocked on the same missing case, and separately on
        # Pango/GObject being absent on this host, which the deploy image carries.
        pending_reason=(
            "docs render and the packet refuses for want of a landed case; the seeder "
            "exists and refuses to snapshot until a score, a backtest fold and a decision "
            "have been landed, and the PDF step needs Pango/GObject, which this host "
            "lacks and the deploy image carries"
        ),
        gates=(
            Gate(
                "audit hash chain verifies",
                ("uv", "run", "python", "scripts/verify_audit.py"),
            ),
            # The seeder is a gate in its own right, and it is conditional: there is
            # nothing to snapshot until a score, a fold and a decision exist, so a missing
            # demo.dump is SKIPPED-PREREQUISITE with the path named rather than a pass.
            # This is the phantom-gate fix made executable -- before, `make demo` called a
            # script that did not exist and nothing checked; now the script exists, and
            # this gate is how a reviewer learns whether a snapshot has been taken.
            Gate(
                "a pinned demo snapshot exists and is restorable",
                ("uv", "run", "python", "scripts/demo_seed.py", "--restore",
                 "--boot-budget", "90"),
                prerequisite="data/snapshots/demo.dump",
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
        cwd=str(REPO_ROOT / gate.cwd) if gate.cwd else REPO_ROOT,
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
