"""Null source adapter: a declared source with nothing behind it (02 §D).

The honest null for the ``SourceAdapter`` port. It yields only rows the caller hands
it, and it refuses to invent a manifest, because "no manifest" is the state that
makes a batch un-acceptable — an implementation that quietly synthesised one would
turn a missing drop into a successful empty run, which is precisely the clean-run-
that-scored-nothing failure ``config/pipeline.yaml`` calls an error.

Rows are checked against the port's canonical field list and the no-float-money
rule, so the null path cannot be used to smuggle a malformed row past the contract
test that the real adapters have to pass.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from oxbow.adapters.file.source import quarantine_record_to_dict
from oxbow.adapters.io import port_dir, resolve_out_root, write_json_atomic
from oxbow.ports.source import (
    BatchManifest,
    QuarantineRecord,
    assert_canonical_row,
    assert_no_float_money,
)


class NullSourceAdapter:
    """A source that was declared but has nothing to read."""

    def __init__(
        self,
        *,
        source_id: str = "null",
        rows: Sequence[Mapping[str, Any]] = (),
        manifest: BatchManifest | None = None,
        root: Path | None = None,
    ) -> None:
        self._source_id = source_id
        self._rows = [dict(row) for row in rows]
        self._manifest = manifest
        self._quarantine_dir = port_dir(resolve_out_root(root), "sources") / source_id
        self._quarantined: list[QuarantineRecord] = []

    @property
    def source_id(self) -> str:
        return self._source_id

    def read_manifest(self) -> BatchManifest:
        if self._manifest is None:
            raise ValueError(
                f"source {self._source_id!r} has no manifest. A declared source with no "
                "manifest cannot be accepted whole, so there is nothing to accept."
            )
        self._manifest.validate()
        return self._manifest

    def iter_canonical(self) -> Iterator[dict[str, Any]]:
        for row in self._rows:
            assert_canonical_row(row)
            assert_no_float_money(row)
            yield dict(row)

    def quarantine(self, record: QuarantineRecord) -> None:
        self._quarantined.append(record)
        write_json_atomic(
            self._quarantine_dir / f"quarantine-{record.batch_id}-{len(self._quarantined)}.json",
            quarantine_record_to_dict(record),
        )

    def quarantine_count(self) -> int:
        """Counted, never dropped quietly: the UI shows this number (02 §D)."""
        return len(self._quarantined)


__all__ = ["NullSourceAdapter"]
