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


def digests(root: Path) -> dict[str, str]:
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


def snapshot_to(dest: Path) -> dict[str, str]:
    """Copy the current interim artifacts into ``dest`` and digest them."""
    if not INTERIM.is_dir():
        raise SystemExit(
            f"no artifacts under {INTERIM}: run the stage first "
            "(`make ingest`) before asking for a determinism check"
        )
    shutil.copytree(INTERIM, dest, dirs_exist_ok=True)
    return digests(dest)


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

    with tempfile.TemporaryDirectory(prefix="oxbow-determinism-") as tmp:
        root = Path(tmp)
        first: dict[str, str] = {}
        if not args.reuse:
            print(f"$ {' '.join(command)}  (run 1)")
            run_stage(command, child_env)
            first = snapshot_to(root / "run1")
        else:
            first = digests(INTERIM)
        print(f"$ {' '.join(command)}  (run 2)")
        run_stage(command, child_env)
        second = snapshot_to(root / "run2")

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
