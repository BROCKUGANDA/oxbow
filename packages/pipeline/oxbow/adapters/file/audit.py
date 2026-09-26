"""File-backed audit sink: a hash chain in JSON Lines (02 §A, §F).

One line per row, and the line *is* the row's stored digest material, so
``cat out/audit/audit.jsonl`` is a meaningful command and a packet export can carry
the chain without a database.

Single-writer by design. Two processes appending to one file cannot both be the tip
of a chain without coordination, and pretending otherwise would produce a forked
chain that verifies right up to the point it matters. The Postgres sink
(``oxbow.adapters.audit.postgres``) is the concurrent path: it has a unique
``chain_seq``, a trigger that refuses UPDATE and DELETE, and a transaction to lose.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from oxbow.adapters.io import append_jsonl, read_jsonl
from oxbow.audit.chain import (
    GENESIS_HASH,
    ChainRow,
    ChainVerification,
    PendingChainRow,
    compute_row_hash,
    verify_chain,
)
from oxbow.ports.audit import AuditAppendError

AUDIT_FILENAME = "audit.jsonl"


def _to_row(record: dict[str, Any]) -> ChainRow:
    """Rebuild a chain row from its stored line."""
    return ChainRow(
        seq=int(record["seq"]),
        occurred_at=datetime.fromisoformat(str(record["occurred_at"])),
        actor_id=str(record["actor_id"]),
        subject=str(record["subject"]),
        action=str(record["action"]),
        payload=dict(record["payload"]),
        prev_hash=str(record["prev_hash"]),
        row_hash=str(record["row_hash"]),
    )


class FileAuditSink:
    """Append-only audit chain stored as ``<directory>/audit.jsonl``."""

    def __init__(self, directory: Path, *, name: str = "file") -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._name = name

    @property
    def sink_id(self) -> str:
        return self._name

    @property
    def base_dir(self) -> Path:
        return self._directory

    @property
    def path(self) -> Path:
        return self._directory / AUDIT_FILENAME

    def tip(self) -> ChainRow | None:
        rows = self.load()
        return rows[-1] if rows else None

    def append(self, row: PendingChainRow) -> ChainRow:
        """Append one row, refusing a write that would fork the chain.

        Every check here is the file equivalent of a database constraint, and each
        names the thing that went wrong: a wrong predecessor means a concurrent
        append, a wrong sequence means a hole, and a digest that does not match its
        own row means the caller built the row from different bytes than it wrote.
        """
        current = self.tip()
        expected_prev = GENESIS_HASH if current is None else current.row_hash
        if row.prev_hash != expected_prev:
            raise AuditAppendError(
                f"audit: row claims prev_hash {row.prev_hash!r} but the stored tip is "
                f"{expected_prev!r}. Something appended between reading the tip and writing."
            )
        expected_seq = 1 if current is None else current.seq + 1
        if row.seq != expected_seq:
            raise AuditAppendError(
                f"audit: row has seq {row.seq} but the next sequence is {expected_seq}"
            )
        if compute_row_hash(row) != row.row_hash:
            raise AuditAppendError(
                "audit: row_hash does not match its own contents; refusing to write a row that "
                "could never verify"
            )
        record: dict[str, Any] = {
            "seq": row.seq,
            "occurred_at": row.occurred_at.isoformat(),
            "actor_id": row.actor_id,
            "subject": row.subject,
            "action": row.action,
            "payload": dict(row.payload),
            "prev_hash": row.prev_hash,
            "row_hash": row.row_hash,
        }
        append_jsonl(self.path, (record,))
        return _to_row(record)

    def load(self, *, from_seq: int = 1) -> Sequence[ChainRow]:
        return [
            _to_row(record) for record in read_jsonl(self.path) if int(record["seq"]) >= from_seq
        ]

    def verify(self) -> ChainVerification:
        return verify_chain(self.load())


__all__ = ["AUDIT_FILENAME", "FileAuditSink"]
