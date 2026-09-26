"""The SourceAdapter port: inbound transaction data.

02 D, the ingest contract, stated once: a source adapter must yield rows that
satisfy the canonical Pandera schema, or raise. It may not repair data silently.
Anything it cannot map goes to a quarantine table with the original row, the
failing constraint and the batch id, and quarantine is visible in the UI with a
count, because silently dropped rows are how fraud systems develop blind spots
that nobody can see.

Four inbound patterns exist, in increasing order of operational cost (02 D):

  1. Batch file drop      - IMPLEMENTED. A batch is rejected whole if the
                            checksum or row count disagrees. Partial batches
                            never enter.
  2. Object-storage zone  - IMPLEMENTED against MinIO. Never read a prefix
                            without its _SUCCESS marker.
  3. Change data capture  - DESIGNED ONLY. Debezium to Kafka, keyed by txn id.
  4. Push API             - route exists behind a feature flag, disabled in demo.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# The canonical event v1. Both corpora normalise into exactly this shape, and
# additive columns only: a breaking change bumps canonical_v2 and both exist
# during the migration (02 B seam 2).
CANONICAL_EVENT_FIELDS: tuple[str, ...] = (
    "txn_id",
    "ts_utc",
    "src_account",
    "dst_account",
    "amount_minor",
    "currency",
    "channel",
    "txn_type",
    "src_balance_before",
    "src_balance_after",
    "dst_balance_before",
    "dst_balance_after",
    "label_fraud",
    "label_typology",
    "source_dataset",
    "ingested_at",
    "run_id",
)


@dataclass(frozen=True, slots=True)
class BatchManifest:
    """Proof that a batch is complete and untampered.

    A batch is accepted whole or rejected whole. Half a day of transactions looks
    like a quiet day, which looks like normal behaviour, which is the specific
    failure this manifest exists to prevent (03 B).
    """

    batch_id: str
    row_count: int
    sha256: str
    window_start: datetime
    window_end: datetime
    source_system: str

    def validate(self) -> None:
        """Raise if the manifest is internally inconsistent.

        03 B: ``test_manifest_mismatch_rejects_batch``.
        """
        if self.row_count < 0:
            raise ValueError(f"batch {self.batch_id}: row_count cannot be negative")
        if self.window_end < self.window_start:
            raise ValueError(f"batch {self.batch_id}: window_end precedes window_start")
        if len(self.sha256) != 64:
            raise ValueError(f"batch {self.batch_id}: sha256 must be 64 hex characters")


@dataclass(slots=True)
class QuarantineRecord:
    """A row that could not be mapped, kept with the reason it failed.

    Never dropped silently. The count is surfaced in the UI, because a fraud
    system that quietly loses rows has a blind spot nobody can see (02 D).
    """

    batch_id: str
    source_dataset: str
    original_row: dict[str, Any]
    failing_constraint: str
    detail: str
    quarantined_at: datetime = field(default_factory=lambda: datetime.now().astimezone())


@runtime_checkable
class SourceAdapter(Protocol):
    """Reads canonical transactions from one declared source.

    Every implementation, null or real, must pass the same contract test, so the
    two are behaviourally interchangeable (02 H: port conformance parametrised
    over every adapter implementing a port).
    """

    @property
    def source_id(self) -> str:
        """The declared source id from ``config/sources.yaml``."""
        ...

    def read_manifest(self) -> BatchManifest:
        """Return the manifest for the batch, or raise if it disagrees.

        03 B: a manifest mismatch rejects the whole batch. There is no partial
        commit path.
        """
        ...

    def iter_canonical(self) -> Iterator[dict[str, Any]]:
        """Yield canonical rows satisfying the Pandera schema, or raise.

        Implementations may not coerce, repair, or silently drop. Unmappable rows
        go to :meth:`quarantine` and the row count still reflects the loss.
        """
        ...

    def quarantine(self, record: QuarantineRecord) -> None:
        """Record an unmappable row with the constraint it failed."""
        ...

    def quarantine_count(self) -> int:
        """How many rows this adapter quarantined. Shown in the UI."""
        ...


@runtime_checkable
class StreamSourceAdapter(Protocol):
    """Near-real-time inbound transactions.

    02 C: the interface is defined and DELIBERATELY NOT BUILT. The detection
    thesis is network-level and windowed; a streaming path would force incremental
    graph maintenance, which is a quarter of work, not a week. Batch windows are
    the honest architecture for this problem (02 G).
    """

    def subscribe(self, topic: str, from_timestamp: datetime) -> Iterator[dict[str, Any]]:
        """Yield events from ``topic`` strictly after ``from_timestamp``."""
        ...


def assert_canonical_row(row: dict[str, Any]) -> None:
    """Fail loud if a row is missing a canonical field.

    Deliberately not a full schema check: Pandera owns dtype and coercion rules in
    ``contracts/``. This is the cheap boundary guard that catches a field rename
    at the point it happens rather than three stages later.
    """
    missing = [field_name for field_name in CANONICAL_EVENT_FIELDS if field_name not in row]
    if missing:
        raise ValueError(f"canonical row is missing required fields: {missing}")


def assert_no_float_money(row: dict[str, Any]) -> None:
    """Fail loud if money arrived as a float.

    01 B: float arithmetic on money is a defect, not a style choice. Pandera
    rejects floats and a lint rule bans float money inside the pipeline package;
    this is the runtime counterpart (03 D: ``test_no_float_money_types``).
    """
    for key in ("amount_minor", "src_balance_before", "src_balance_after"):
        value = row.get(key)
        if isinstance(value, float):
            raise TypeError(
                f"{key} arrived as {value!r} (float). Money is integer minor units "
                "(01 B). Convert at the adapter boundary, never downstream."
            )


def null_source_path(source_id: str, root: Path) -> Path:
    """Where a null adapter writes for ``source_id``.

    The null adapters write to ``out/<port>/`` as JSON or Parquet, and that file
    is the demo's proof that the boundary was crossed for real.
    """
    return root / "out" / "sources" / source_id


def batch_paths(root: Path, source_id: str, batch_id: str) -> Sequence[Path]:
    """Candidate Parquet paths for a batch, in the order they are tried on resume.

    03 J: every stage is resumable from its checkpoint, so a partial batch failure
    records where it stopped rather than starting over.
    """
    base = root / "data" / "interim" / source_id
    return (base / f"{batch_id}.parquet", base / f"{batch_id}.partial.parquet")


__all__ = [
    "CANONICAL_EVENT_FIELDS",
    "BatchManifest",
    "QuarantineRecord",
    "SourceAdapter",
    "StreamSourceAdapter",
    "assert_canonical_row",
    "assert_no_float_money",
    "batch_paths",
    "null_source_path",
]
