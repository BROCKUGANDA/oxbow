"""Object-storage zone source: 02 §D pattern 2, gated on a ``_SUCCESS`` marker.

A drop zone is the most dangerous inbound pattern, because a prefix with files in it
looks finished whether it is or not. The rule from ``ports/source.py`` is one line —
never read a prefix without its ``_SUCCESS`` marker — and its whole value is that it
turns "the SFTP job was still uploading" into an error instead of a quiet short run.

It composes over the ObjectStoreAdapter *port*, not over boto3, so the same class
reads MinIO in production and ``out/objectstore/`` in the demo. That is the port
system paying for itself rather than being architecture for its own sake.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

from oxbow.ports.objectstore import ObjectNotFound, ObjectStoreAdapter
from oxbow.ports.source import (
    BatchManifest,
    QuarantineRecord,
    assert_canonical_row,
    assert_no_float_money,
)

SUCCESS_MARKER = "_SUCCESS"


class MissingSuccessMarker(RuntimeError):  # noqa: N818 - exported adapter name; describes the missing marker, not the failure kind
    """The prefix has data but no completion marker, so it is not a batch yet."""


class ObjectStoreZoneSource:
    """Reads one prefix of JSON-lines objects as a canonical batch."""

    def __init__(
        self,
        *,
        store: ObjectStoreAdapter,
        prefix: str,
        source_id: str,
        batch_id: str,
        window_start: datetime,
        window_end: datetime,
        expected_row_count: int | None = None,
        manifest_object: str | None = None,
    ) -> None:
        self._store = store
        self._prefix = prefix.rstrip("/")
        self._source_id = source_id
        self._batch_id = batch_id
        self._window_start = window_start
        self._window_end = window_end
        self._expected_row_count = expected_row_count
        self._manifest_object = manifest_object
        self._quarantined: list[QuarantineRecord] = []

    @property
    def source_id(self) -> str:
        return self._source_id

    def read_manifest(self) -> BatchManifest:
        """Return the manifest, refusing an unmarked prefix before counting anything."""
        if not self._has_marker():
            raise MissingSuccessMarker(
                f"{self._prefix}/ has no {SUCCESS_MARKER}. A prefix that is still being written "
                "is not a batch: reading it would produce a short run that looks like a quiet "
                "period (02 §D pattern 2)."
            )
        if self._manifest_object:
            raw = self._store.get(f"{self._prefix}/{self._manifest_object}")
            declared = json.loads(raw)
            if not isinstance(declared, Mapping):
                raise ValueError(f"{self._manifest_object} is not a JSON object")
            return BatchManifest(
                batch_id=str(declared["batch_id"]),
                row_count=int(declared["row_count"]),
                sha256=str(declared["sha256"]),
                window_start=datetime.fromisoformat(str(declared["window_start"])),
                window_end=datetime.fromisoformat(str(declared["window_end"])),
                source_system=str(declared.get("source_system", self._source_id)),
            )
        rows = self._count_rows()
        return BatchManifest(
            batch_id=self._batch_id,
            row_count=self._expected_row_count if self._expected_row_count is not None else rows,
            sha256=self._prefix_digest(),
            window_start=self._window_start,
            window_end=self._window_end,
            source_system=self._source_id,
        )

    def iter_canonical(self) -> Iterator[dict[str, Any]]:
        """Yield canonical rows from every object under the prefix, sorted by key.

        Key order is the object-store equivalent of ``ORDER BY (ts_utc, txn_id)``: a
        manifest read in a different order would produce a different digest for the
        same bytes on the wire.
        """
        if not self._has_marker():
            raise MissingSuccessMarker(f"{self._prefix}/ has no {SUCCESS_MARKER}")
        for ref in sorted(self._store.list(f"{self._prefix}/"), key=lambda item: item.key):
            if ref.key.endswith(SUCCESS_MARKER) or ref.key.endswith(".manifest.json"):
                continue
            for line_number, line in enumerate(
                self._store.get(ref.key).decode("utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    row: dict[str, Any] = dict(json.loads(line))
                    assert_canonical_row(row)
                    assert_no_float_money(row)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self.quarantine(
                        QuarantineRecord(
                            batch_id=self._batch_id,
                            source_dataset=self._source_id,
                            original_row={"line": line[:500]},
                            failing_constraint="canonical_event_v1",
                            detail=f"{ref.key}:{line_number}: {exc}",
                        )
                    )
                    continue
                yield row

    def quarantine(self, record: QuarantineRecord) -> None:
        self._quarantined.append(record)

    def quarantine_count(self) -> int:
        return len(self._quarantined)

    def _has_marker(self) -> bool:
        try:
            self._store.head(f"{self._prefix}/{SUCCESS_MARKER}")
        except ObjectNotFound:
            return False
        return True

    def _count_rows(self) -> int:
        total = 0
        for ref in self._store.list(f"{self._prefix}/"):
            if ref.key.endswith(SUCCESS_MARKER) or ref.key.endswith(".manifest.json"):
                continue
            total += sum(1 for line in self._store.get(ref.key).decode("utf-8").splitlines() if line.strip())
        return total

    def _prefix_digest(self) -> str:
        """A digest over the sorted object digests, so a rename is detectable."""
        import hashlib

        parts = [
            f"{ref.key}:{ref.sha256}"
            for ref in sorted(self._store.list(f"{self._prefix}/"), key=lambda item: item.key)
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


__all__ = ["SUCCESS_MARKER", "MissingSuccessMarker", "ObjectStoreZoneSource"]
