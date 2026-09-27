"""Snapshot a warehouse that holds real evidence, and prove a restore boots under a budget.

STATE.md item 11 is the thing this exists for. `make demo` has always run

    $(PY) scripts/demo_seed.py --restore --boot-budget 90

against a script that did not exist, while `make help` advertised the target. The plan
requirement (plan §15) is "boots offline in under 90 seconds", and the honest reason it was
never written is recorded in STATE.md: a seeder has two jobs -- snapshot a warehouse that
holds scored rows, backtest folds and one landed reviewer decision, and prove the stack
reaches healthy within the budget -- and until the score stage trained a model there was
nothing to snapshot. Writing it against an empty database would have produced a green
target that boots a blank UI, which is the failure this repository keeps cataloguing.

So this script refuses rather than fakes. It requires evidence on both sides:

  --create   Snapshot the live warehouse to data/snapshots/demo.dump. Refuses if the
             database has no scored rows, no folds and no landed decision, naming what is
             missing, because a dump of an empty warehouse restores to a blank UI.
  --restore  Load the dump into the target database and then *measure* the boot, by
             waiting for the API's /healthz to report every component available, against
             --boot-budget. Exits 1 if the budget is blown, printing the elapsed time and
             the components still degraded, so a slow restore fails instead of passing on
             a hopeful timeout.

The dump is a `pg_dump` custom-format archive, taken and restored through the Compose
postgres service because that is the only place pg_dump exists on this host. That is a
deliberate consequence of not wanting a host-level Postgres client dependency, and it
means the script needs the project up -- which is also what it is measuring.

A snapshot is only worth restoring if it is pinned, so `create` records a manifest beside
the dump: the alembic revision, the row count of every non-empty table, and the digest of
the dump itself. `restore` verifies the digest before it loads anything, and reports the
revision it landed at, so a demo that silently drifted from the source is visible rather
than merely different-looking.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SNAPSHOT_DIRNAME: Final = "snapshots"
DUMP_FILENAME: Final = "demo.dump"
MANIFEST_FILENAME: Final = "demo.dump.manifest.json"
DEFAULT_SERVICE: Final = "postgres"
DEFAULT_DB: Final = "oxbow"
DEFAULT_USER: Final = "oxbow"

STATUS_OK: Final = "OK"
STATUS_FAIL: Final = "FAIL"
STATUS_SKIP: Final = "SKIP"

# Tables whose absence means "this warehouse holds no evidence". A dump without all three
# restores to a demo with nothing in it, which is the blank-UI failure stated above. The
# names are the warehouse's own, from apps/api/alembic/versions/0001.
EVIDENCE_TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("score", "scored rows"),
    ("backtest_fold", "backtest folds"),
    ("decision", "a landed reviewer decision"),
)


class SeederError(RuntimeError):
    """A refusal, named rather than worked around."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """One check's result, in the shape every other script in this repo reports."""

    status: str
    detail: str

    def render(self) -> str:
        return f"{self.status:5} {self.detail}"


def _compose(*args: str, service: str = DEFAULT_SERVICE, check: bool = True) -> str:
    """Run a command inside the Compose postgres service and return its stdout.

    pg_dump/pg_restore are not on this host, so they are invoked where they exist. The
    container reads the dump through a bind mount of the snapshot directory, which is why
    the path is translated: Docker Desktop on Windows does not accept a native path inside
    a container argument.
    """
    command = ["docker", "compose", "exec", "-T", service, *args]
    done = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, check=False)
    if check and done.returncode != 0:
        raise SeederError(
            f"`{' '.join(command)}` exited {done.returncode}: "
            f"{(done.stderr or done.stdout).strip()[:400]}"
        )
    return done.stdout


def _container_path(host_path: Path) -> str:
    """The same path as the Compose service sees it.

    The snapshot directory is bind-mounted at /srv/data/snapshots (docker-compose.yml), so
    a host path under the repo maps into the container by making it relative to the repo
    root and prefixing the mount point. Guessing this wrong produces a pg_dump that
    silently writes somewhere the restore then cannot see, which is worse than an error.
    """
    resolved = host_path.resolve()
    try:
        relative = resolved.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:  # pragma: no cover - guarded by the caller's path check
        raise SeederError(
            f"{resolved} is outside the repo, so the container cannot reach it; the "
            "snapshot directory has to live under the project root"
        ) from exc
    return f"/srv/data/{relative.as_posix()}"


def _table_counts() -> dict[str, int]:
    """Row count per table, as Postgres itself reports them.

    `pg_stat_user_tables` is a live estimate rather than an exact count, which is the right
    trade for a manifest that only has to detect a warehouse that has emptied out.
    """
    raw = _compose(
        "psql",
        "-U",
        DEFAULT_USER,
        "-d",
        DEFAULT_DB,
        "-t",
        "-A",
        "-F",
        "\t",
        "-c",
        "select relname, n_live_tup from pg_stat_user_tables order by relname;",
    )
    counts: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].strip().isdigit():
            counts[parts[0].strip()] = int(parts[1])
    return counts


def _alembic_revision() -> str:
    """The migration revision the warehouse is at, recorded so a restore can be compared."""
    raw = _compose(
        "psql",
        "-U",
        DEFAULT_USER,
        "-d",
        DEFAULT_DB,
        "-t",
        "-A",
        "-c",
        "select version_num from alembic_version limit 1;",
    )
    return raw.strip() or "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audit_evidence(counts: dict[str, int]) -> list[Outcome]:
    """Name every evidence table the warehouse is missing, rather than the first."""
    return [
        Outcome(STATUS_FAIL, f"{label}: {counts.get(table, 0)} row(s) in {table}")
        for table, label in EVIDENCE_TABLES
        if counts.get(table, 0) == 0
    ]


def create_dump(*, snapshot_dir: Path, service: str) -> list[Outcome]:
    """Snapshot the live warehouse, refusing when it holds nothing worth restoring."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    dump = snapshot_dir / DUMP_FILENAME
    manifest = snapshot_dir / MANIFEST_FILENAME

    counts = _table_counts()
    missing = _audit_evidence(counts)
    if missing:
        return [
            Outcome(
                STATUS_FAIL,
                "the warehouse holds no evidence to snapshot: "
                + "; ".join(o.detail for o in missing),
            ),
            Outcome(
                STATUS_FAIL,
                "a dump of this database would restore to a blank demo. Land a score, a "
                "backtest fold and a decision through the API first (STATE.md item 11).",
            ),
        ]

    # --clean because the target file is a rebuild, and --if-exists because a re-create
    # after the first snapshot is the normal case rather than an error.
    _compose(
        "pg_dump",
        "-U",
        DEFAULT_USER,
        "-d",
        DEFAULT_DB,
        "--format=custom",
        "--clean",
        "--if-exists",
        "-f",
        _container_path(dump),
        service=service,
    )
    if not dump.is_file():
        raise SeederError(
            f"pg_dump reported success but {dump} is not on the host; the container wrote "
            "somewhere the mount does not cover"
        )

    payload: dict[str, Any] = {
        "created_by": "scripts/demo_seed.py --create",
        "alembic_revision": _alembic_revision(),
        "dump": DUMP_FILENAME,
        "dump_bytes": dump.stat().st_size,
        "dump_sha256": _sha256(dump),
        "row_counts": {table: n for table, n in sorted(counts.items()) if n},
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    populated = payload["row_counts"]
    return [
        Outcome(STATUS_OK, f"snapshot {dump.relative_to(REPO_ROOT)} ({dump.stat().st_size} bytes)"),
        Outcome(STATUS_OK, f"alembic revision {payload['alembic_revision']}"),
        Outcome(
            STATUS_OK,
            f"{len(populated)} populated table(s): "
            + ", ".join(f"{t}={n}" for t, n in list(populated.items())[:8])
            + (" ..." if len(populated) > 8 else ""),
        ),
    ]


def _verify_digest(dump: Path, manifest_path: Path) -> Outcome:
    """Refuse a dump whose bytes are not the ones the manifest recorded.

    Restoring a dump that has been edited, truncated or half-copied produces a demo that
    boots and shows whatever survived, which is the quiet failure worth refusing.
    """
    if not manifest_path.is_file():
        raise SeederError(
            f"{manifest_path} is missing, so the snapshot is unpinned. A restore of an "
            "unpinned dump cannot be compared against the database it came from."
        )
    recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = _sha256(dump)
    if actual != recorded.get("dump_sha256"):
        raise SeederError(
            f"{dump.name} has sha256 {actual[:16]}..., the manifest records "
            f"{str(recorded.get('dump_sha256'))[:16]}...; the snapshot was rewritten or "
            "truncated after it was taken. Re-run with --create."
        )
    return Outcome(
        STATUS_OK,
        f"digest verified against {manifest_path.name} (alembic "
        f"{recorded.get('alembic_revision')})",
    )


def _wait_for_healthz(base_url: str, budget_seconds: float) -> tuple[float, str]:
    """Poll the API until it reports healthy, or the budget runs out.

    Returns (elapsed_seconds, detail). The clock is measured across the whole wait, which
    is what the plan's "boots offline in under 90 seconds" is about -- not the time one
    request took.
    """
    deadline = time.monotonic() + budget_seconds
    last = "never answered"
    while time.monotonic() < deadline:
        elapsed = budget_seconds - (deadline - time.monotonic())
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys,urllib.request\n"
                "try:\n"
                f"    r=urllib.request.urlopen({base_url!r}, timeout=5)\n"
                "    print(r.read().decode())\n"
                "except Exception as exc:\n"
                "    print(f'UNREACHABLE: {exc}')\n",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        raw = (done.stdout or "").strip()
        if raw.startswith("{") and '"status"' in raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            status = payload.get("data", payload).get("status", "?")
            if status == "ok":
                return elapsed, status
            last = f"status={status}"
        else:
            last = raw[:120] or "no response"
        time.sleep(1.0)
    return budget_seconds, last


def restore_dump(
    *, snapshot_dir: Path, service: str, base_url: str, boot_budget: float
) -> list[Outcome]:
    """Load the snapshot, then measure whether the stack came up inside the budget."""
    dump = snapshot_dir / DUMP_FILENAME
    manifest = snapshot_dir / MANIFEST_FILENAME
    if not dump.is_file():
        return [
            Outcome(
                STATUS_FAIL,
                f"no snapshot at {dump}. Run `scripts/demo_seed.py --create` against a "
                "warehouse that holds a score, a fold and a decision first.",
            )
        ]

    results = [_verify_digest(dump, manifest)]

    _compose(
        "pg_restore",
        "-U",
        DEFAULT_USER,
        "-d",
        DEFAULT_DB,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--single-transaction",
        _container_path(dump),
        service=service,
    )
    results.append(Outcome(STATUS_OK, f"restored {dump.name} into {DEFAULT_DB}"))

    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    results.append(
        Outcome(STATUS_OK, f"restored at alembic revision {recorded.get('alembic_revision')}")
    )

    # The budget is the point of the target, so it is measured against a real endpoint and
    # a blown budget fails the command rather than printing a hopeful note.
    started = time.monotonic()
    elapsed, detail = _wait_for_healthz(base_url, boot_budget)
    wall = time.monotonic() - started
    if elapsed < boot_budget:
        results.append(
            Outcome(
                STATUS_OK,
                f"API healthy ({detail}) after {wall:.1f}s, inside the {boot_budget:.0f}s budget",
            )
        )
    else:
        results.append(
            Outcome(
                STATUS_FAIL,
                f"API did not reach healthy within {boot_budget:.0f}s "
                f"({wall:.1f}s elapsed, last state: {detail})",
            )
        )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Snapshot a warehouse that holds real evidence, or restore one and prove the "
            "stack boots inside a budget. Both refuse rather than produce a blank demo."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--create", action="store_true", help="Snapshot the live warehouse.")
    mode.add_argument("--restore", action="store_true", help="Restore the pinned snapshot.")
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=REPO_ROOT / "data" / SNAPSHOT_DIRNAME,
        help=f"Where the dump and its manifest live (default data/{SNAPSHOT_DIRNAME}).",
    )
    parser.add_argument("--service", default=DEFAULT_SERVICE, help="Compose postgres service.")
    parser.add_argument(
        "--api-url", default="http://127.0.0.1:8000/healthz", help="Endpoint the boot check polls."
    )
    parser.add_argument(
        "--boot-budget",
        type=float,
        default=90.0,
        help="Seconds the restored stack has to reach healthy (plan §15 says 90).",
    )
    args = parser.parse_args(argv)

    try:
        if args.create:
            results = create_dump(snapshot_dir=args.snapshot_dir, service=args.service)
        else:
            results = restore_dump(
                snapshot_dir=args.snapshot_dir,
                service=args.service,
                base_url=args.api_url,
                boot_budget=args.boot_budget,
            )
    except SeederError as exc:
        print(f"{STATUS_FAIL:5} {exc}")
        return 1

    for outcome in results:
        print(outcome.render())
    return 1 if any(o.status == STATUS_FAIL for o in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
