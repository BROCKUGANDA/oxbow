"""`make verify-determinism` — run a stage twice, diff the artifact digests.

01 §D defines the reproduction check; DEV-007 keeps it separate from `make
verify`, which 00 §C uses for "every gate to date". Two names for two
obligations, because one green tick cannot discharge both.

The claim being tested is narrow and absolute: given the same input bytes and the
same run salt, two runs of a stage must produce byte-identical artifacts. Not
"close", not "the same rows in a different order" — the same *bytes*, which is
what makes a published number defensible a month later.

What is deliberately excluded from the compared set: the per-run identity
columns (`run_id`, `ingested_at`) and the batch id, which 01 §A rule 8 requires to
vary per run. Those live in the run manifest, not in the data bytes — see
DEV-012 — so excluding them here is not a loophole, it is the reason the split
exists.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INTERIM = REPO_ROOT / "data" / "interim"

# Anything whose name marks it as run-scoped is compared by presence, never by
# bytes. Kept as one tuple so a new per-run artifact has a single place to be
# declared rather than being silently ignored everywhere.
RUN_SCOPED_MARKERS: tuple[str, ...] = ("run_manifest", "manifest.json", "_meta")


def sha256_of(path: Path) -> str:
    """Digest a file in chunks; the corpora are far too large to read whole."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_scoped_key(root: Path, path: Path) -> str:
    """A relative path with any ULID directory segment replaced by ``<run_id>``.

    Ingest lands content-addressed batch files under a source directory, so two runs
    collide on one key. The later stages write ``out/<stage>/<run_id>/...``, where the
    run id *is* a path segment: keyed literally, run 1's artifacts would all be
    "present in run 1, absent in run 2" and the comparison would measure nothing while
    reporting a difference. Folding the segment makes the two runs comparable by
    content, which is the claim actually being tested.
    """
    from oxbow.identity import is_ulid

    parts = [segment if not is_ulid(segment) else "<run_id>" for segment in path.relative_to(root).parts]
    return "/".join(parts)


def digests(root: Path, *, run_scoped_paths: bool = False) -> dict[str, str]:
    """Map every non-run-scoped file under ``root`` to its digest.

    Keys are relative paths, so a comparison names the offending file rather than
    reporting that two directory trees differ somewhere.
    """
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(marker in relative for marker in RUN_SCOPED_MARKERS):
            continue
        if run_scoped_paths:
            found[_run_scoped_key(root, path)] = sha256_of(path)
            continue
        found[relative] = sha256_of(path)
    return found


def run_stage(command: Sequence[str], env: dict[str, str]) -> None:
    """Run one stage, inheriting stdout so the operator watches it happen."""
    proc = subprocess.run(
        list(command),
        cwd=REPO_ROOT,
        env=env,
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"stage exited {proc.returncode}: {' '.join(command)}")


def _any_recent(copied: Path, source_root: Path, since: float) -> bool:
    """True when at least one compared file was written after ``since``.

    mtimes are read from the source tree rather than the copy, because
    ``copytree`` resets them.
    """
    for path in copied.rglob("*"):
        if not path.is_file():
            continue
        original = source_root / path.relative_to(copied)
        if original.is_file() and original.stat().st_mtime >= since:
            return True
    return False


def snapshot_to(dest: Path, root: Path) -> dict[str, str]:
    """Copy the stage's own artifacts into ``dest`` and digest them.

    ``root`` is the tree the stage under test writes into. Passing the wrong one is
    the failure mode this function exists to avoid: an earlier version compared
    ``data/interim`` whatever the command was, so a graph run was "verified" against
    ingest artifacts it never touched and passed without measuring anything.
    """
    if not root.is_dir():
        raise SystemExit(
            f"no artifacts under {root}: run the stage first, or pass --artifacts for "
            "the tree it actually writes"
        )
    shutil.copytree(root, dest, dirs_exist_ok=True)
    table = digests(dest, run_scoped_paths=root != INTERIM)
    if not table:
        raise SystemExit(f"{root} holds no comparable artifacts for this command")
    return table


def compare(first: dict[str, str], second: dict[str, str]) -> list[str]:
    """Return human-readable differences; empty means the two runs are identical."""
    problems: list[str] = []
    for name in sorted(set(first) | set(second)):
        if name not in second:
            problems.append(f"{name}: present in run 1, absent in run 2")
        elif name not in first:
            problems.append(f"{name}: absent in run 1, present in run 2")
        elif first[name] != second[name]:
            problems.append(f"{name}: digest {first[name][:12]} != {second[name][:12]}")
    return problems


def report(name: str, table: dict[str, str], stream: Iterable[str] = ()) -> None:
    """Print the digest table so the check is auditable, not just pass/fail."""
    print(f"--- {name} ({len(table)} artifacts) ---")
    for key, value in table.items():
        print(f"  {value[:16]}  {key}")
    for line in stream:
        print(line)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--command",
        default="uv run oxbow ingest --limit 200000",
        help="The stage to re-run, as a shell-split string. Two runs are compared.",
    )
    parser.add_argument(
        "--artifacts",
        default=None,
        help=(
            "Artifact root the stage writes into, relative to the repo. Defaults to "
            "data/interim, which is where ingest lands; the graph, score and backtest "
            "stages write out/<stage>/<run_id>/ instead, and comparing the wrong tree "
            "is a pass that measures nothing."
        ),
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="Compare the artifacts already on disk against a fresh second run "
        "instead of running the stage twice.",
    )
    args = parser.parse_args(argv)

    command: tuple[str, ...] = tuple(args.command.split())
    # Resolved through the pipeline's own resolver rather than a local
    # `os.environ.get`: the CLI falls back to `.env`, so a gate that only read the
    # environment would refuse to run in exactly the shells where `make ingest`
    # works. One resolution path is also the point -- two ways to find the salt is
    # how a check ends up comparing two runs that were keyed differently.
    sys.path.insert(0, str(REPO_ROOT / "packages" / "pipeline"))
    from oxbow.config import ConfigError, resolve_run_salt

    try:
        salt = resolve_run_salt(REPO_ROOT)
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(
            f"{exc}\nRUN_SALT must be identical across both runs -- account keys are "
            "salted, so a differing salt changes the bytes legitimately (01 §A rule 8)."
        ) from exc
    child_env = dict(os.environ, RUN_SALT=salt, PYTHONIOENCODING="utf-8")

    target = REPO_ROOT / args.artifacts if args.artifacts else INTERIM
    print(f"comparing artifacts under {target}")
    with tempfile.TemporaryDirectory(prefix="oxbow-determinism-") as tmp:
        root = Path(tmp)
        first: dict[str, str] = {}
        if not args.reuse:
            print(f"$ {' '.join(command)}  (run 1)")
            written_after = time.time() - 5
            run_stage(command, child_env)
            first = snapshot_to(root / "run1", target)
            # The tripwire: at least one compared file has to be one this run wrote.
            # Without it, a stage that writes nothing elsewhere still "passes" by
            # replaying a previous run's bytes, which is the exact empty claim a
            # reproduction gate must not be able to make.
            if not _any_recent(root / "run1", target, written_after):
                raise SystemExit(
                    f"neither run wrote anything under {target}: the comparison would "
                    "have replayed artifacts this command did not produce"
                )
        else:
            first = digests(target, run_scoped_paths=target != INTERIM)
        print(f"$ {' '.join(command)}  (run 2)")
        run_stage(command, child_env)
        second = snapshot_to(root / "run2", target)

    if not first:
        raise SystemExit("run 1 produced no comparable artifacts; nothing was verified")

    report("run 1", first)
    report("run 2", second)
    problems = compare(first, second)
    if problems:
        print(f"\nNOT DETERMINISTIC — {len(problems)} artifact(s) differ:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(
        f"\nDETERMINISTIC: {len(second)} artifact(s) byte-identical across two runs "
        f"of `{' '.join(command)}`"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
