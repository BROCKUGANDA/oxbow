"""`make verify-audit` — walk every decision and audit hash chain, print OK or the break.

Plan §13 defines this command's observable: *walks the chain, prints OK or the first
broken link naming the sequence number*. It is deliberately a thin script over the same
pure arithmetic the packet uses on export (:func:`oxbow.audit.chain.verify_chain`), so
the two can never disagree about what "verified" means — a check that passes in one
place and fails in the other is two checks, and neither is trustworthy.

Three chains are walked when they exist:

* the file audit chain, ``out/audit/audit.jsonl`` — the null-adapter path the offline
  demo runs on, and the one a packet export carries;
* ``audit_event`` in Postgres — the system audit stream, when ``DATABASE_URL`` resolves;
* ``decision`` in Postgres — the decision chain, whose row carries its own
  ``chain_seq``, ``prev_hash`` and ``row_hash``, and whose digest covers the reviewer's
  free-text reason.

A chain that cannot be reached is reported as SKIPPED with the reason, never as a pass.
``scripts/verify.py`` uses the same vocabulary for the same reason: a green tick that
covers nothing is how a gate starts lying.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]

from oxbow.adapters.file.audit import AUDIT_FILENAME, FileAuditSink
from oxbow.audit.chain import ChainRow, render_verification, verify_chain

DEFAULT_AUDIT_DIR: Final = REPO_ROOT / "out" / "audit"
DECISION_SUBJECT_PREFIX: Final = "case:"

STATUS_OK = "OK"
STATUS_SKIP = "SKIPPED"
STATUS_FAIL = "FAIL"


def _file_rows(sink: FileAuditSink) -> Sequence[ChainRow]:
    return sink.load()


def _parse_instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(
            f"audit timestamp {value!r} has no UTC offset; the digest covers an instant, "
            "and a naive one is an unlogged assumption about who wrote it and where"
        )
    return parsed


def _postgres_engine(url: str) -> Any:
    from sqlalchemy import create_engine  # noqa: PLC0415 - only needed on the DB path

    return create_engine(url)


def _rows_from_table(engine: Any, table: str) -> list[ChainRow]:
    """Rebuild a stored chain in sequence order.

    The field mapping mirrors ``apps/api/decisions.py`` exactly — same subject prefix,
    same ``decision:`` action form — because the digest is over those strings, and a
    verifier that reconstructed them differently would report a valid chain as broken.
    """
    from sqlalchemy import text  # noqa: PLC0415 - DB path only

    columns = _table_columns(engine, table)
    if columns is None:
        raise LookupError(f"table {table!r} does not exist in this database")
    subject_expression = (
        f"concat('{DECISION_SUBJECT_PREFIX}', case_id) AS subject"
        if table == "decision"
        else "subject"
    )
    query = text(
        f"SELECT chain_seq, occurred_at, actor_id, {subject_expression} AS subject, "
        f"action, payload, prev_hash, row_hash FROM {table} ORDER BY chain_seq ASC"
    )
    rows: list[ChainRow] = []
    with engine.connect() as connection:
        for record in connection.execute(query).mappings().all():
            payload = record["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            action = str(record["action"])
            if table == "decision" and not action.startswith("decision:"):
                action = f"decision:{action}"
            rows.append(
                ChainRow(
                    seq=int(record["chain_seq"]),
                    occurred_at=_parse_instant(record["occurred_at"]),
                    actor_id=str(record["actor_id"]),
                    subject=str(record["subject"]),
                    action=action,
                    payload=dict(payload or {}),
                    prev_hash=str(record["prev_hash"]),
                    row_hash=str(record["row_hash"]),
                )
            )
    return rows


def _table_columns(engine: Any, table: str) -> Sequence[str] | None:
    from sqlalchemy import inspect  # noqa: PLC0415 - DB path only

    names = {str(column["name"]) for column in inspect(engine).get_columns(table)}
    required = {"chain_seq", "occurred_at", "actor_id", "action", "payload", "prev_hash", "row_hash"}
    if not required.issubset(names):
        return None
    return sorted(names)


def verify_file_chain(directory: Path) -> tuple[str, str]:
    """Walk ``<directory>/audit.jsonl``; SKIPPED when the file is not there."""
    path = directory / AUDIT_FILENAME
    if not path.is_file():
        return (
            STATUS_SKIP,
            f"{path} does not exist yet — nothing has been decided through the file audit "
            "sink on this host (stage P7). Only a reviewer decision writes it -- "
            "`oxbow pipeline` runs ingest, graph, score and backtest, and none of those "
            "decide anything, so a pipeline run will never produce this file. The writer "
            "is apps/api/decisions.py through oxbow.ports.case_sink.",
        )
    verification = verify_chain(list(_file_rows(FileAuditSink(directory))))
    return (STATUS_OK if verification.ok else STATUS_FAIL), render_verification(
        verification, label=f"file:{path.relative_to(REPO_ROOT)}"
    )


def verify_database_chain(url: str, table: str) -> tuple[str, str]:
    engine = _postgres_engine(url)
    try:
        rows = _rows_from_table(engine, table)
    except Exception as exc:  # noqa: BLE001 - reported as a named skip, never as a pass
        return STATUS_SKIP, f"{table}: not reachable ({type(exc).__name__}: {exc})"
    if not rows:
        return (
            STATUS_SKIP,
            f"{table}: chain is empty — no decision has been written through this database",
        )
    verification = verify_chain(rows)
    return (STATUS_OK if verification.ok else STATUS_FAIL), render_verification(
        verification, label=f"postgres:{table}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Walk every reachable hash chain and report it. Exit 1 if any link is broken."""
    parser = argparse.ArgumentParser(
        description="Walk the decision and audit hash chains; print OK or the first "
        "broken link naming its sequence number."
    )
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=DEFAULT_AUDIT_DIR,
        help=f"Directory holding {AUDIT_FILENAME} (default out/audit).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres URL. Defaults to $DATABASE_URL; unset or unreachable is SKIPPED, "
        "never passed.",
    )
    parser.add_argument(
        "--no-database", action="store_true", help="Verify the file chain only."
    )
    args = parser.parse_args(argv)

    results: list[tuple[str, str]] = [verify_file_chain(args.audit_dir)]
    url = args.database_url
    if url is None and not args.no_database:
        import os

        url = os.environ.get("DATABASE_URL", "")
    if url and not args.no_database:
        for table in ("audit_event", "decision"):
            results.append(verify_database_chain(str(url), table))
    elif not args.no_database:
        results.append(
            (
                STATUS_SKIP,
                "postgres: DATABASE_URL is not set, so the warehouse chains were not walked",
            )
        )

    width = max(len(status) for status, _ in results)
    failures = 0
    for status, detail in results:
        print(f"{status:<{width}} {detail}")
        failures += status == STATUS_FAIL
    print()
    print(f"chains walked: {len(results)}  failed: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
