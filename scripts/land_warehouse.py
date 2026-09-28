"""Land one existing run's artifacts into the Postgres warehouse through the queue's own stage loop.

Why this file exists, in one paragraph. ``jobs.PIPELINE_STAGES`` (``apps/api/jobs.py:52``)
declares four stages — ``ingest, graph, score, warehouse`` — and ``warehouse`` is the one that
writes the read model the API serves. It is not a CLI verb: ``oxbow.cli.STAGES`` is
``ingest, graph, score, backtest``, and ``pipeline_cmd`` (``cli.py:2414``) composes exactly
those four, so ``oxbow pipeline --run-id <id>`` never reaches ``warehouse`` at all — and on a
run whose ledger holds only ``score``, resuming it would re-run ``ingest`` and ``graph`` over
``data/interim`` first. 01 §D fixes the CLI at those verbs ("no more, no fewer", asserted by
the P0 gate), which is why ``land_warehouse_rows`` (``cli.py:1910``) deliberately has no verb
of its own. The only caller of that function in the tree is the queue's dispatch,
``apps/api/worker.py:655``.

So the supported way to run the ``warehouse`` stage for a run that already has artifacts on
disk is ``api.worker.run_stages(run_id=..., stages=["warehouse"], kind="pipeline")`` — the
exact callable RQ invokes — and this script calls it and nothing else. It is not a new stage
implementation, it does not start the RQ worker, Redis or any other daemon, and it adds no
gate: the immutability refusal (``worker._refuse_immutable_run``), the sink's own
``IMMUTABLE_RUN_STATES`` check, the per-table builders' refusals and migration 0002's
``trg_*_run_mutable`` all still fire, and their words are printed unedited.
``JobLedger.open`` already accounts for a hand-run job ("this job was started by hand rather
than through the API"), so a run landed this way is recorded in ``run`` and ``stage_event`` and
deliberately has no ``job_run`` row of its own.

Authorisation: plan 01 §D (the verb set this respects), §13 ("pipeline and backtest runs as
jobs with live progress" — the job this script runs is that job, run by hand), and P9, whose
other operator entry points ``scripts/demo_seed.py`` and ``scripts/verify.py`` are the same
kind of file. Run it with a ``DATABASE_URL`` for the warehouse to be written; without one the
stage prints its own ``[warehouse] DATABASE_URL is unset`` line and lands nothing.

There is deliberately no ``--dry-run`` here. ``open_stage_run`` honours ``ctx.dry_run`` by
skipping its own run-open and immutability guard, but ``land_warehouse_rows`` never reads
``ctx.dry_run``: it builds the payload and writes it either way. Measured through this script
on 2026-09-28, a ``--dry-run`` warehouse stage reached ``_read_canonical_for_ids`` and did real
work behind the flag. A flag that promises to write nothing and writes the warehouse is not a
dry run, so the flag is absent rather than advisory.

Usage:
    uv run python scripts/land_warehouse.py --run-id <ULID>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
APPS_DIR: Final = REPO_ROOT / "apps"

# Same bootstrap as apps/api/worker.py and apps/api/main.py: one import style (``api.*``),
# and it has to happen before ``api.worker`` is reachable.
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))

# Import order is load-bearing, not stylistic. ``api.worker`` imports ``osqp`` at its own top
# (worker.py:78) before anything reaches pyarrow, because loading cvxpy's OSQP probe after
# pyarrow's bundled OpenMP/BLAS runtime segfaults the process — DEV-021, exit 139, no
# traceback. Importing ``oxbow.cli`` or polars above this line would re-create that fault.
from api.worker import run_stages  # noqa: E402

from oxbow.ports.warehouse import WarehouseTableError  # noqa: E402

STAGE: Final = "warehouse"
KIND: Final = "pipeline"


def _report(summary: dict[str, Any]) -> None:
    """Print the stage ledger the run produced, as the warehouse stored it."""
    print("[land] summary:")
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the declared `warehouse` stage for one existing run through the queue's own "
            "dispatch, without starting a worker."
        )
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="The run whose artifacts are on disk under out/score/<id> and out/features/<id>.",
    )
    args = parser.parse_args(argv)

    run_id = args.run_id.strip().upper()
    print(f"[land] stage={STAGE} kind={KIND} run_id={run_id}")
    print(f"[land] repo root: {REPO_ROOT}")
    print(
        "[land] DATABASE_URL: "
        + ("set (value not printed)" if os.environ.get("DATABASE_URL", "").strip() else "UNSET")
    )
    try:
        summary = run_stages(run_id=run_id, stages=[STAGE], kind=KIND)
    except WarehouseTableError as exc:
        # The sink's own refusal, printed in its own words: an operator reading this has to
        # be able to tell "the loader refused the rows" from "the database refuses this run".
        print(f"[land] REFUSED by the warehouse: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # The traceback, not just the exception name: a stage that dies inside the landing
        # has to be locatable to a file and a line by whoever is told about it.
        print(f"[land] {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    _report(summary)
    failures = list(summary.get("failures") or [])
    if failures:
        print(f"[land] FAILED: {'; '.join(str(line) for line in failures)}", file=sys.stderr)
        return 1
    print(f"[land] run state: {summary.get('run_state')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
