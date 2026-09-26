"""Plan §19's anti-rubbish checklist, for the parts nothing else enforces.

The standing requirement is: no stubs, no placeholders, no TODO, no lorem, no
hardcoded numbers in components, no mocked responses presented as data. Every one of
those is grep-detectable, which means every one of them will eventually appear in a
large codebase unless something checks. A checklist that nobody runs is a wish.

Scope note: §19.1's unfinished-work markers are **already** enforced by
`tests/unit/test_p0_design_system.py`, with a pattern that does not misfire on the
`RECORDED_AT_DOWNLOAD` sentinel or on a comment explaining one. This file does not
duplicate that gate; it adds the two checks that were missing — the frontend `any`
ban in §19.2, which is what keeps the generated API client's error union typed, and
the "a claimed-complete phase's verb is actually wired to a stage runner" check,
which is the cheapest way to catch a phase that was marked done before its command
was built.

Each failure prints the offending path and line: "found 9 violations" is not
actionable, and a fix has to be locatable from the assertion message alone.

The one allowance is this file itself, which necessarily contains the forbidden
strings as patterns.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve()
CLI_PATH = REPO_ROOT / "packages" / "pipeline" / "oxbow" / "cli.py"

FRONTEND_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx"})

# §19.2 — the generated client must carry a typed error union, so `any` is the
# failure signal, not a style preference. Type annotations (`: any`), casts
# (`as any`) and generic arguments (`<any>`) are all banned.
TS_ANY_PATTERN: Final = re.compile(r":\s*any\b|\bas\s+any\b|<\s*any\s*[>,]")

# Directories that are not source: virtualenvs, build output, caches, vendored
# lockfiles and downloaded corpora. Listed explicitly rather than inferred, so a new
# cache directory shows up as a failure to update this list rather than as a silent
# scan of gigabytes.
SKIP_DIRS = frozenset(
    {
        ".venv",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".import_linter_cache",
        ".hypothesis",
        "node_modules",
        "dist",
        "build",
        ".next",
        "data",
        "out",
        "alembic",
        "__pycache__",
        ".qoder",
    }
)


def iter_source(roots: tuple[str, ...], suffixes: frozenset[str]) -> list[Path]:
    """Collect source files under ``roots`` with the given suffixes."""
    found: list[Path] = []
    for root_name in roots:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if SKIP_DIRS.intersection(path.parts):
                continue
            if path == HERE:
                continue
            found.append(path)
    return sorted(found)


def offenders(paths: list[Path], pattern: re.Pattern[str], *, strip_comments: bool) -> list[str]:
    """Return `path:line: text` for every match, so a failure is locatable."""
    hits: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            probe = line
            if strip_comments:
                # A URL fragment or an anchor named `#placeholder` is markup, not an
                # unfinished-work marker; the CSS/HTML comment form is stripped too.
                probe = re.sub(r"<!--.*?-->", "", line)
            if pattern.search(probe):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:120]}")
    return hits


def test_no_any_in_frontend_source() -> None:
    paths = iter_source(("apps/web/src",), FRONTEND_SUFFIXES)
    assert paths, "found no frontend sources to scan — the tree moved, update this test"
    hits = offenders(paths, TS_ANY_PATTERN, strip_comments=True)
    assert not hits, (
        "`any` in frontend source defeats the typed error union (§19.2):\n" + "\n".join(hits)
    )


# A completed phase replaces its verb's placeholder body; this maps each verb to the
# phase whose gate makes that verb real, so the check tightens as `make verify`
# claims phases rather than failing permanently while they are still in build.
STAGE_OWNERS: Final[dict[str, tuple[str, str]]] = {
    "ingest": ("P1b", "run_ingest_stage"),
    "graph": ("P3a", "run_graph_stage"),
    "score": ("P4", "run_score_stage"),
    "backtest": ("P6", "run_backtest_stage"),
}


def _calls_reachable_from(function: ast.FunctionDef) -> set[str]:
    """Every callable name mentioned anywhere inside one function, nested defs included."""
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def _completed_phases() -> set[str]:
    """Read the authoritative phase list out of `scripts/verify.py`.

    Imported by path rather than duplicated as a constant here: two copies of "which
    phases are done" is how a gate starts disagreeing with the command it guards.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "oxbow_verify", REPO_ROOT / "scripts" / "verify.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery
        raise AssertionError("scripts/verify.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: verify.py uses `dataclass(slots=True)` together with
    # `from __future__ import annotations`, and dataclasses resolves those postponed
    # annotations by looking the module up in sys.modules. Without this line the
    # import fails with an AttributeError about NoneType, not a useful message.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return {phase.name for phase in module.PHASES if phase.done}


def test_claimed_complete_stages_are_wired_to_a_runner() -> None:
    """For every phase `make verify` claims done, its verb must reach a stage runner.

    This reads the CLI's syntax tree rather than running the verbs. Running them was
    slow and blind at once: `oxbow ingest` with no arguments ingests the whole PaySim
    corpus, so `make test` was spawning a second full pipeline inside itself and
    timing out; and the string it grepped for, a "NOT IMPLEMENTED" banner, appears
    nowhere in the CLI, so an unwired verb passed it. A verb whose body never mentions
    the runner it is supposed to drive is the actual shape of "claimed complete,
    never built".
    """
    tree = ast.parse(CLI_PATH.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    completed = _completed_phases()
    checked: list[str] = []
    for verb, (owner, runner) in STAGE_OWNERS.items():
        if owner not in completed:
            continue
        command = verb if verb in functions else f"{verb}_cmd"
        assert command in functions, (
            f"{owner} is marked complete but `oxbow {verb}` defines no function named "
            f"{command!r} in {CLI_PATH.relative_to(REPO_ROOT)}"
        )
        reached = _calls_reachable_from(functions[command])
        assert runner in reached, (
            f"{owner} is marked complete in scripts/verify.py, but {command}() never "
            f"calls {runner}(), so it is a verb that does not run the stage it names. "
            f"Calls made inside it: {sorted(reached)}"
        )
        checked.append(f"{verb}->{runner}")
    assert checked, (
        "no claimed-complete phase mapped to a verb, so this test proved nothing; "
        f"scripts/verify.py reports completed={sorted(completed)}"
    )


def test_scanners_are_proven_to_bite() -> None:
    """Prove each pattern fires on a known-bad input and not on a near-miss.

    A gate that has never failed on a deliberate violation is decoration (00 §B): if
    a regex or a path filter silently breaks, this is the test that notices rather
    than a run that goes green for the wrong reason.
    """
    bad_ts = "export function f(x: any): string {\n  return (x as any).y;\n}\n"
    assert TS_ANY_PATTERN.search(bad_ts)
    assert TS_ANY_PATTERN.search("let rows: any[] = []")
    good_ts = "export function f(x: unknown): string {\n  return String(x);\n}\n"
    assert not TS_ANY_PATTERN.search(good_ts)
    # A named route or anchor must not read as a violation.
    assert not TS_ANY_PATTERN.search('<a href="/alerts#top">x</a>')
    assert not TS_ANY_PATTERN.search("type ProblemLike = { detail: string }")
