"""`make demo` must not be a target that points at nothing.

STATE.md item 11 records that `make demo` has always run

    $(PY) scripts/demo_seed.py --restore --boot-budget 90

against a script that did not exist, while `make help` advertised the target. The seeder
exists now, so the phantom is closed -- but it closed once, and nothing prevented it from
opening again. `make help` printing a target is documentation; a target whose command names
a file that is not in the tree is a promise the reader will keep.

These tests read the Makefile and the tree rather than running Docker or Postgres, because
the failure they guard is structural: a target naming a missing file, or a seeder that
would snapshot an empty warehouse and call the result a demo. Both are checkable without
a database, and checking them without one is what makes them cheap enough to always run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.demo_seed import EVIDENCE_TABLES, SeederError, _audit_evidence, _container_path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
SEEDER = REPO_ROOT / "scripts" / "demo_seed.py"

_TARGET = re.compile(r"^([a-z][a-z0-9-]*):", re.MULTILINE)
_MAKE_COMMANDS = re.compile(r"^\t+@?\$\(|^\t+@?\$\(PY\)|^\t+@?[a-z]", re.MULTILINE)


def _target_recipe(target: str) -> str:
    """The recipe lines under one Makefile target, tab-indented commands only."""
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{target}:.*?$(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"the Makefile has no {target!r} target"
    return match.group(0)


def test_every_script_the_makefile_calls_actually_exists() -> None:
    """A recipe may not name a file under scripts/ that is not in the tree.

    The general form of the demo bug: `$(PY) scripts/<name>.py` is a promise, and a
    missing file fails at the moment a person runs the target rather than in review. Only
    scripts/ is checked, because that is where the Makefile's own helpers live; a shell
    command like `docker` is a system dependency, not a file in this repository.
    """
    missing: list[str] = []
    for match in re.finditer(r"scripts/[A-Za-z0-9_.-]+\.py", MAKEFILE.read_text(encoding="utf-8")):
        referenced = REPO_ROOT / match.group(0)
        if not referenced.is_file():
            missing.append(match.group(0))
    assert not missing, (
        f"the Makefile calls script(s) that do not exist: {sorted(set(missing))}. Every "
        "`$(PY) scripts/...` in a recipe is a target a reader will run."
    )


def test_every_gate_the_verify_script_defines_names_a_file_that_exists() -> None:
    """A phase gate that cannot execute is a phantom gate with a green name.

    P8's gate ran `pnpm --dir apps/web test:unit` on a host where pnpm is not installed, so
    the phase could never be verified and the definition still read as a passing check. It then
    spent its life as `node node_modules/vitest/vitest.mjs run`, a workaround that kept the gate
    executable while leaving the declared toolchain fictitious. DEV-022 is the plan amendment that
    makes the declaration and the gate agree -- `bun run test:unit --run`, the package script --
    and because that command is a PATH dependency rather than a repository path, this test cannot
    cover it; test_p8_gate_runs_a_script_the_manifest_declares holds the half that is in
    committed bytes.
    The point of this test is not the toolchain: it is that a gate whose command cannot be found
    is green for exactly as long as nobody runs it. The
    Makefile version of this test (above) covers `scripts/*.py` in recipes; this covers the
    other list of commands a reviewer trusts -- the phase table in scripts/verify.py.

    Only argv tokens that name a path inside the repository are checked. A gate starting
    with a bare command (`uv`, `node`, `pytest`) depends on PATH rather than on a file in
    this tree, so it is a different question and is left to the phase's own run.

    The phase table is imported rather than parsed out of the source. A regex over
    `Gate(...)` calls silently misses an argv that wraps across lines -- which is exactly
    how this gate was written -- and a check that quietly checks nothing is the failure
    mode this whole test exists to prevent.
    """
    from scripts.verify import PHASES

    missing: list[str] = []
    for phase in PHASES:
        for gate in phase.gates:
            # A gate that sets cwd runs its argv relative to there, so a path token has to
            # be resolved against that directory. Checking it against the repo root would
            # report the P8 vitest entry point missing when it is present and working.
            base = REPO_ROOT / gate.cwd if gate.cwd else REPO_ROOT
            for token in gate.argv:
                if "/" not in token and "\\" not in token:
                    continue  # a bare command: PATH, not a repo file
                if not (base / token).exists():
                    missing.append(f"{phase.name}: {token}")
                break  # one path token per gate is enough to place it

    assert not missing, (
        f"verify.py defines gate(s) naming a file that is not in the tree: {sorted(set(missing))}. "
        "A phase gate that cannot run cannot be verified, and its definition still reads as "
        "a passing check."
    )


def test_the_demo_target_points_at_a_seeder_that_exists() -> None:
    """`make demo` is advertised by `make help`, so its command must resolve."""
    recipe = _target_recipe("demo")
    assert (
        "scripts/demo_seed.py" in recipe
    ), f"the demo target no longer calls the seeder; it reads {recipe!r}"
    assert SEEDER.is_file()


def test_the_demo_target_advertises_a_budget_and_the_plan_agrees() -> None:
    """The 90 in the target is plan §15's number, so the two must not drift apart.

    A budget nobody checks is decoration. `make demo` passes `--boot-budget` and the
    seeder enforces it by failing the command, but only if the number it enforces is the
    number the plan asked for.
    """
    recipe = _target_recipe("demo")
    assert "--boot-budget" in recipe, "the demo target would run with the seeder's default budget"

    source = SEEDER.read_text(encoding="utf-8")
    default = re.search(r'"--boot-budget",[\s\S]{0,80}?default=([0-9.]+)', source)
    assert default is not None, "the seeder does not state a default boot budget"
    assert (
        float(default.group(1)) == 90.0
    ), f"the seeder's default budget is {default.group(1)}s, but plan §15 requires 90s"


def test_the_seeder_refuses_a_warehouse_with_no_evidence() -> None:
    """An empty warehouse is the blank-UI demo; the seeder must name what is missing.

    This is the behaviour the whole script exists to protect, so it is pinned here rather
    than left to the integration path. All three gaps are named at once: reporting only the
    first would leave someone fixing a score table and discovering the other two later.
    """
    outcomes = _audit_evidence({table: 0 for table, _label in EVIDENCE_TABLES})

    assert len(outcomes) == len(EVIDENCE_TABLES)
    for (_table, label), outcome in zip(EVIDENCE_TABLES, outcomes, strict=True):
        assert outcome.status == "FAIL"
        assert label in outcome.detail


def test_a_warehouse_holding_every_evidence_table_passes_the_audit() -> None:
    """The audit is satisfied by rows, not by the tables merely existing.

    One row is enough: the snapshot's job is to restore a demonstrable path, and a table
    with a single row proves the pipeline wrote through that path at least once.
    """
    assert _audit_evidence({table: 1 for table, _label in EVIDENCE_TABLES}) == []


def test_a_partially_populated_warehouse_names_only_what_is_missing() -> None:
    """Two of three present is still a refusal, and the message says which one is absent."""
    counts = {table: 4 for table, _label in EVIDENCE_TABLES}
    missing_table, missing_label = EVIDENCE_TABLES[1]
    counts[missing_table] = 0

    outcomes = _audit_evidence(counts)

    assert [o.status for o in outcomes] == ["FAIL"]
    assert missing_label in outcomes[0].detail
    assert missing_table in outcomes[0].detail


def test_the_evidence_tables_are_the_warehouse_s_own_names() -> None:
    """A typo in a table name would make the audit pass on a warehouse that has nothing.

    `_audit_evidence` treats a missing key as zero, so a misspelled table reads as "no
    evidence" and the script refuses forever. That is a safe failure, but it is a failure
    nobody would understand, so the names are checked against the migration that creates
    them.
    """
    migration = (
        REPO_ROOT
        / "apps"
        / "api"
        / "alembic"
        / "versions"
        / ("0001_7907d04c69bc_warehouse_read_model.py")
    )
    source = migration.read_text(encoding="utf-8")
    for table, _label in EVIDENCE_TABLES:
        assert f'"{table}"' in source, (
            f"the audit watches {table!r}, which the warehouse migration does not create; "
            "the name is either stale or misspelled"
        )


def test_a_snapshot_outside_the_repo_is_refused_rather_than_guessed_at() -> None:
    """The container reaches the dump through a repo-relative bind mount.

    Mapping a host path into the container is a path calculation, and a wrong answer makes
    pg_dump write somewhere the restore cannot see -- a silent failure that would read as
    "the dump was empty". Outside the repo the calculation has no answer, so it refuses.
    """
    inside = REPO_ROOT / "data" / "snapshots" / "demo.dump"
    assert _container_path(inside) == "/srv/data/data/snapshots/demo.dump"

    with pytest.raises(SeederError, match="outside the repo"):
        _container_path(Path("C:/somewhere-else/demo.dump"))
