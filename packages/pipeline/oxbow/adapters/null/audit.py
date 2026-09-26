"""Null audit sink: the file chain, pointed at ``out/audit/`` (plan §13).

Deliberately a two-line subclass of :class:`~oxbow.adapters.file.audit.FileAuditSink`
rather than a second implementation. A null adapter that re-derives the hashing is a
second chain format, and the point of the family is that the demo path and the
production path differ only in destination.
"""

from __future__ import annotations

from pathlib import Path

from oxbow.adapters.file.audit import FileAuditSink
from oxbow.adapters.io import resolve_out_root


class NullAuditSink(FileAuditSink):
    """Writes ``out/audit/decision.jsonl`` and ``out/audit/audit.jsonl``."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(resolve_out_root(root) / "audit", name="null")


__all__ = ["NullAuditSink"]
