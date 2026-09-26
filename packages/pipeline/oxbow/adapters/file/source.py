"""File-backed source adapter: 02 §D pattern 1, a batch file drop.

The SourceAdapter protocol in :mod:`oxbow.ports.source` is the port P1b owns; these
are the two implementations the demo and the contract suite need, and they follow
the port's own docstring rather than inventing a third shape:

* The null counterpart lives in :mod:`oxbow.adapters.null.source`; it exists so the
  contract test can run a source with nothing declared behind it.
* ``CsvSourceAdapter`` implements pattern 1 — a batch file drop, accepted whole or
  rejected whole against its manifest. A manifest disagreement raises before a
  single row is yielded, because half a day of transactions looks like a quiet day
  and a quiet day looks normal (03 §B).
* :mod:`oxbow.adapters.s3.source` implements pattern 2 — an object-storage zone that
  refuses a prefix without its ``_SUCCESS`` marker.

Column names follow ``CANONICAL_EVENT_FIELDS`` from the port, which is the
documented inbound shape and the one the contract test asserts. Where
``ingest/canonical.py`` names the persisted frame differently (DEV-012), the
translation is explicit at the call site and never silent.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oxbow.adapters.io import port_dir, resolve_out_root, write_json_atomic
from oxbow.ports.source import (
    CANONICAL_EVENT_FIELDS,
    BatchManifest,
    QuarantineRecord,
    assert_no_float_money,
)


def manifest_for_file(path: Path, *, source_id: str, batch_id: str) -> BatchManifest:
    """Derive a manifest from bytes on disk, so the "declared" side is measured.

    A manifest typed by hand is a wish. This reads the file, counts the data rows
    and hashes them, which is the only way the mismatch check downstream can fail
    for a real reason.
    """
    body = path.read_bytes()
    lines = [line for line in body.decode("utf-8").splitlines() if line.strip()]
    data_rows = max(len(lines) - 1, 0)
    stamps = _timestamp_column(path)
    return BatchManifest(
        batch_id=batch_id,
        row_count=data_rows,
        sha256=hashlib.sha256(body).hexdigest(),
        window_start=min(stamps) if stamps else datetime.now(UTC),
        window_end=max(stamps) if stamps else datetime.now(UTC),
        source_system=source_id,
    )


def _timestamp_column(path: Path) -> list[datetime]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        stamps: list[datetime] = []
        for row in reader:
            raw = (row.get("ts_utc") or "").strip()
            if raw:
                stamps.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    return stamps


class CsvSourceAdapter:
    """Pattern 1: a batch file drop, checked against its manifest before row one."""

    def __init__(
        self,
        *,
        source_id: str,
        path: Path,
        run_id: str,
        manifest: BatchManifest | None = None,
        root: Path | None = None,
    ) -> None:
        self._source_id = source_id
        self._path = Path(path)
        self._manifest = manifest or manifest_for_file(self._path, source_id=source_id, batch_id=source_id)
        self._run_id = run_id
        self._quarantined: list[QuarantineRecord] = []
        self._quarantine_dir = port_dir(resolve_out_root(root), "sources") / source_id

    @property
    def source_id(self) -> str:
        return self._source_id

    def read_manifest(self) -> BatchManifest:
        self._manifest.validate()
        return self._manifest

    def iter_canonical(self) -> Iterator[dict[str, Any]]:
        """Yield canonical rows, or raise before the first one on a mismatch.

        The row-count check is against the *declared* manifest, not against a count
        taken from the same file: the failure being prevented is a truncated drop,
        and a self-consistent truncated file would otherwise pass.
        """
        if not self._path.is_file():
            raise FileNotFoundError(f"source {self._source_id!r}: batch file is missing: {self._path}")
        with self._path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            yielded = 0
            for index, raw in enumerate(reader, start=2):
                try:
                    row = self._canonicalise(raw, index)
                except ValueError as exc:
                    self.quarantine(
                        QuarantineRecord(
                            batch_id=self._manifest.batch_id,
                            source_dataset=self._source_id,
                            original_row=dict(raw),
                            failing_constraint="canonical_event_v1",
                            detail=str(exc),
                        )
                    )
                    continue
                yielded += 1
                yield row
            if yielded != self._manifest.row_count:
                raise ValueError(
                    f"batch {self._manifest.batch_id}: manifest declares "
                    f"{self._manifest.row_count} rows, {yielded} were usable. The batch is "
                    "rejected whole; a partial batch enters as a quiet period (03 §B)."
                )

    def _canonicalise(self, raw: Mapping[str, str | None], line_number: int) -> dict[str, Any]:
        missing = [field for field in CANONICAL_EVENT_FIELDS if field not in raw]
        if missing:
            raise ValueError(f"line {line_number}: missing fields {missing}")
        row: dict[str, Any] = dict(raw)
        amount = row.get("amount_minor")
        row["amount_minor"] = _strict_int(amount, "amount_minor", line_number)
        for key in (
            "src_balance_before",
            "src_balance_after",
            "dst_balance_before",
            "dst_balance_after",
        ):
            row[key] = None if row[key] in ("", None) else _strict_int(row[key], key, line_number)
        label = row.get("label_fraud")
        row["label_fraud"] = None if label in ("", None) else str(label).lower() in ("1", "true")
        row["ts_utc"] = datetime.fromisoformat(str(row["ts_utc"]).replace("Z", "+00:00"))
        row["ingested_at"] = datetime.now(UTC)
        row["run_id"] = self._run_id
        row["source_dataset"] = self._source_id
        assert_no_float_money(row)
        return row

    def quarantine(self, record: QuarantineRecord) -> None:
        self._quarantined.append(record)
        write_json_atomic(
            self._quarantine_dir / f"quarantine-{record.batch_id}-{len(self._quarantined)}.json",
            quarantine_record_to_dict(record),
        )

    def quarantine_count(self) -> int:
        return len(self._quarantined)


def _strict_int(value: Any, field: str, line_number: int) -> int:
    """Parse an integer minor-unit amount, refusing anything else.

    A float here is not rounded, it is quarantined: silently truncating a currency
    value at the boundary is the coercion ``config/pipeline.yaml`` sets
    ``allow_silent_coercion: false`` to prevent.
    """
    text = str(value).strip()
    if not text:
        raise ValueError(f"line {line_number}: {field} is empty")
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(
            f"line {line_number}: {field}={value!r} is not an integer minor-unit amount"
        ) from exc


def quarantine_record_to_dict(record: QuarantineRecord) -> dict[str, Any]:
    """The stored form of a quarantined row: the row, the constraint, the reason."""
    return {
        "batch_id": record.batch_id,
        "source_dataset": record.source_dataset,
        "original_row": record.original_row,
        "failing_constraint": record.failing_constraint,
        "detail": record.detail,
        "quarantined_at": record.quarantined_at.isoformat(),
    }


__all__ = ["CsvSourceAdapter", "manifest_for_file", "quarantine_record_to_dict"]
