"""Audit sinks: a hash chain in Postgres, and the same chain as a JSONL file.

Both are append-only and both can walk themselves; the file sink is what lets a
demo run produce a chain that ``make verify-audit`` verifies without a server.
"""

from __future__ import annotations

__all__: list[str] = []
