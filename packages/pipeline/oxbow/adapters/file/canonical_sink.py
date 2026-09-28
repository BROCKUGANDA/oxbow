"""Landing the canonical table: Parquet, a quarantine artifact, a run manifest, DuckDB views.

Plan §6 asks ingest for four artifacts and this module is the only place any of them
is written:

* **Parquet under ``data/interim/``**, one file per batch, at the path
  :func:`oxbow.ports.source.batch_paths` already declares as the resume location — so
  a resumed run finds the file it wrote rather than inventing a second layout.
* **A run manifest** carrying the per-batch ``row_count``, ``sha256``, window and
  quarantine count, plus DEV-012's two sidecar columns (``run_id``,
  ``ingested_at``). The manifest is *named* ``run_manifest.json``, which is the marker
  ``scripts/verify_determinism.py`` uses to exclude a file from the byte comparison:
  the sidecar values legitimately differ between runs, and that is exactly why they
  are not in the Parquet.
* **A quarantine artifact**, one row per rejected source row, carrying the original
  row, the failing constraint and the batch id (02 §D). Without an artifact the count
  is a claim nobody can audit.
* **DuckDB views** over the Parquet, so the graph and feature layers query the
  canonical table by name. The database file lives under ``out/`` because a DuckDB
  file is a query surface, not the data: it holds views over the Parquet, so it can be
  deleted and rebuilt, and its own bytes are not a determinism artifact.

WHY THE SORT HAPPENS HERE AND NOT ONLY IN THE ADAPTER. ``ingest_paysim`` sorts each
batch internally and concatenates, so the aggregate frame is ordered *within* each
batch and unordered *between* them. A file whose row order depends on batch arrival
has a digest that depends on batch arrival, which would make
``make verify-determinism`` fail on a corpus that never changed. The writer therefore
re-sorts on the contract's own total order (:data:`CANONICAL_TOTAL_ORDER`) before it
writes, and the contract's uniqueness check is what makes that sort *total* rather
than merely stable.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import duckdb
import polars as pl

from oxbow.adapters.io import dumps, write_bytes_atomic
from oxbow.contracts.canonical_v1 import (
    CANONICAL_TOTAL_ORDER,
    PERSISTED_CANONICAL_COLUMNS,
    assert_canonical_frame,
)
from oxbow.dtypes import PolarsDtype
from oxbow.ingest.paysim import QuarantineRecord

INTERIM_DIRNAME: Final = "interim"
DATA_DIRNAME: Final = "data"
OUT_DIRNAME: Final = "out"
WAREHOUSE_DIRNAME: Final = "warehouse"
RUN_MANIFEST_FILENAME: Final = "run_manifest.json"
DUCKDB_EXTENSION: Final = ".duckdb"
CANONICAL_VIEW: Final = "canonical_events"
QUARANTINE_VIEW: Final = "quarantine_records"
PARQUET_SUFFIX: Final = ".parquet"

# The codecs this writer will run, named exactly as polars accepts them, mirroring
# ``graph/persist.py``. The config key is a plain string and an unknown codec has to
# fail here rather than be swapped for a default: a differently compressed artifact
# is a different checksum, and verify-determinism compares checksums.
PARQUET_CODECS: Final[Mapping[str, str]] = {"zstd": "zstd", "snappy": "snappy", "gzip": "gzip"}

QUARANTINE_COLUMNS: Final[dict[str, PolarsDtype]] = {
    "batch_id": pl.String(),
    "source_dataset": pl.String(),
    "row_index": pl.Int64(),
    "reason": pl.String(),
    "detail": pl.String(),
    "payload_json": pl.String(),
}


class SinkConfigError(RuntimeError):
    """Raised when the determinism block cannot be honoured as written."""


def _repo_root_for(interim_root: Path) -> Path:
    """The directory an ``interim_root`` batch path is recorded relative to.

    The recorded path is ``<root>/<...>/<source>/<batch>.parquet`` and the reader joins it
    against the root, so the root has to be the directory that contains the per-source
    subdirectories. That is not always one level up: callers build the sink both ways --
    ``for_repo`` (root is ``<repo>``, interim is ``<repo>/data/interim``) and directly as
    ``CanonicalSink(interim_root=tmp_path / "interim")`` -- so the answer depends on the
    layout, and guessing wrong in either direction records a path that resolves one
    directory off from the real file.

    The two shapes the tree actually uses are recognised by name. Anything else is
    treated as "interim sits directly under the root", which is the direct-construction
    case, and is the only guess available without the caller saying so.
    """
    resolved = Path(interim_root).resolve()
    if resolved.name == INTERIM_DIRNAME and resolved.parent.name == DATA_DIRNAME:
        # <repo>/data/interim -- the layout for_repo builds.
        return resolved.parent.parent
    # <root>/interim -- the shape a direct CanonicalSink(interim_root=...) caller uses.
    return resolved.parent


def _relative_to_root(path: Path, root: Path) -> str:
    """``path`` as a POSIX string relative to ``root``, falling back to the absolute form.

    The fallback is the honest behaviour when the artifact genuinely lives outside the
    repo (an ``--out`` directory elsewhere on disk): recording it absolutely still lets
    the next reader open it on this machine, whereas a fabricated relative path would
    not open anywhere.
    """
    target = Path(path)
    base = Path(root)
    try:
        return target.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return target.as_posix()


@dataclass(frozen=True, slots=True)
class BatchArtifact:
    """One Parquet file the sink actually wrote, with the digest of its bytes."""

    batch_id: str
    source_dataset: str
    path: str
    rows: int
    sha256: str
    window_start: datetime | None
    window_end: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "source_dataset": self.source_dataset,
            "path": self.path,
            "rows": self.rows,
            "sha256": self.sha256,
            "window_start": _text(self.window_start),
            "window_end": _text(self.window_end),
        }


@dataclass(frozen=True, slots=True)
class ViewRegistration:
    """A DuckDB view, and the row count read back out of it.

    The count is queried rather than passed in: registering a view is not the same as
    being able to read it, and a printed number that came from a ``SELECT`` is the
    difference between an artifact and a claim about an artifact.
    """

    view: str
    files: int
    rows: int

    def as_dict(self) -> dict[str, Any]:
        return {"view": self.view, "files": self.files, "rows": self.rows}


def _text(value: datetime | date | None) -> str | None:
    """ISO text for the manifest, or null for a window that never opened."""
    return value.isoformat() if value is not None else None


def sha256_of_file(path: Path) -> str:
    """Digest a landed artifact in chunks; a corpus file is far too large to read whole."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parquet_settings(determinism: Mapping[str, Any]) -> dict[str, Any]:
    """Translate ``config/pipeline.yaml``'s determinism block into writer arguments.

    No defaults are substituted. Every one of these values is a byte-level decision,
    so a missing key is a configuration failure rather than an invitation to pick one.
    """
    missing = [
        key
        for key in ("parquet_row_group_size", "parquet_compression", "parquet_compression_level")
        if key not in determinism
    ]
    if missing:
        raise SinkConfigError(
            f"determinism is missing {missing}; this writer substitutes no default, because "
            "a substituted row-group size or codec changes every checksum"
        )
    codec = PARQUET_CODECS.get(str(determinism["parquet_compression"]))
    if codec is None:
        raise SinkConfigError(
            f"determinism.parquet_compression is {determinism['parquet_compression']!r}; this "
            f"writer implements {sorted(PARQUET_CODECS)}. Add the codec deliberately rather "
            "than letting it be ignored."
        )
    return {
        "row_group_size": int(determinism["parquet_row_group_size"]),
        "compression": codec,
        "compression_level": int(determinism["parquet_compression_level"]),
    }


class CanonicalSink:
    """Writes canonical artifacts and registers the DuckDB views over them."""

    def __init__(
        self,
        *,
        interim_root: Path,
        duckdb_path: Path,
        determinism: Mapping[str, Any],
        repo_root: Path | None = None,
    ) -> None:
        self.interim_root = Path(interim_root)
        self.duckdb_path = Path(duckdb_path)
        self.settings = parquet_settings(determinism)
        # The repo root, kept only to relativise recorded artifact paths so a manifest
        # written inside a container (/srv) is still readable from the host. Derived from
        # interim_root when not given, but that derivation is only valid for the default
        # layout -- `for_repo` passes the real root because --out relocates interim_root.
        self.repo_root = (
            Path(repo_root) if repo_root is not None else _repo_root_for(self.interim_root)
        )

    @classmethod
    def for_repo(
        cls,
        root: Path,
        *,
        out_override: Path | None,
        determinism: Mapping[str, Any],
    ) -> CanonicalSink:
        """Build the sink for one run. Default root is ``<repo>/data/interim``.

        ``--out`` relocates the Parquet and the manifests; the DuckDB file stays under
        ``<repo>/out/warehouse``, because it is disposable query state rebuilt from the
        Parquet list on every run rather than an artifact a test is asking to keep.
        :func:`oxbow.ports.source.batch_paths` names the same default, so a resumed run
        finds the files its predecessor wrote.
        """
        interim = (
            out_override
            if out_override is not None
            else Path(root) / DATA_DIRNAME / INTERIM_DIRNAME
        )
        return cls(
            interim_root=interim,
            duckdb_path=Path(root) / OUT_DIRNAME / WAREHOUSE_DIRNAME / f"oxbow{DUCKDB_EXTENSION}",
            determinism=determinism,
            repo_root=Path(root),
        )

    def source_dir(self, source_dataset: str) -> Path:
        directory = self.interim_root / source_dataset
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def resolve(self, recorded: str) -> Path:
        """A recorded batch path as something openable on this machine.

        `BatchArtifact.path` is repo-relative so the manifest survives being written by a
        container at /srv and read by a host checkout. That makes it useless on its own to
        a caller that just wants to open the file -- `pl.read_parquet(artifact["path"])`
        and a DuckDB `CREATE VIEW` both resolve against the process working directory, not
        the repo root. They resolve the record first, so no caller has to remember that
        the string in the manifest is a root-relative path and not a path.
        """
        candidate = Path(recorded)
        return candidate if candidate.is_absolute() else self.repo_root / candidate

    def batch_path(self, source_dataset: str, batch_id: str) -> Path:
        """``data/interim/<source>/<batch_id>.parquet`` — the port's own resume path."""
        return self.source_dir(source_dataset) / f"{batch_id}{PARQUET_SUFFIX}"

    def write_batch(
        self,
        source_dataset: str,
        batch_id: str,
        events: pl.DataFrame,
    ) -> dict[str, object]:
        """Validate, order, write and digest one batch's canonical events.

        The contract check is the gate, not a courtesy: this is the last point at which
        a row can be refused before it becomes an artifact every downstream stage
        trusts. The return is a plain dict so ``ingest/`` can carry it into a manifest
        without importing this module (02 §A).
        """
        validated = assert_canonical_frame(events, persisted=True)
        ordered = validated.sort(list(CANONICAL_TOTAL_ORDER)).select(
            list(PERSISTED_CANONICAL_COLUMNS)
        )
        target = self.batch_path(source_dataset, batch_id)
        buffer = io.BytesIO()
        ordered.write_parquet(buffer, **self.settings)
        write_bytes_atomic(target, buffer.getvalue())
        window = ordered.select(
            pl.col(CANONICAL_TOTAL_ORDER[0]).min().alias("start"),
            pl.col(CANONICAL_TOTAL_ORDER[0]).max().alias("end"),
        ).row(0, named=True)
        return BatchArtifact(
            batch_id=batch_id,
            source_dataset=source_dataset,
            # Repo-relative, not absolute. The reader (cli.py:594) opens `batch.path`
            # verbatim, so an absolute path makes the manifest non-portable across the two
            # places this tree legitimately runs: a container writes /srv/data/interim/...
            # and the host then reads that manifest and looks for a literal /srv path it
            # does not have, failing with "is declared by paysim's run manifest but is
            # absent on disk". Same convention as eval.Artifact (relpath + root / relpath).
            path=_relative_to_root(target, self.repo_root),
            rows=ordered.height,
            sha256=sha256_of_file(target),
            window_start=_as_datetime(window["start"]),
            window_end=_as_datetime(window["end"]),
        ).as_dict()

    def write_quarantine(
        self,
        source_dataset: str,
        batch_id: str,
        records: Sequence[QuarantineRecord],
    ) -> str:
        """Land the quarantine table for one batch: row, constraint, reason, batch id."""
        frame = pl.DataFrame(
            {
                "batch_id": [batch_id] * len(records),
                "source_dataset": [record.source_dataset for record in records],
                "row_index": [int(record.row_index) for record in records],
                "reason": [record.reason for record in records],
                "detail": [record.detail for record in records],
                "payload_json": [dumps(record.payload) for record in records],
            },
            schema=QUARANTINE_COLUMNS,
        )
        target = self.source_dir(source_dataset) / f"quarantine-{batch_id}{PARQUET_SUFFIX}"
        buffer = io.BytesIO()
        # ``statistics=False``: the payload is arbitrary JSON text, and min/max
        # dictionary entries over a free-text column are exactly the kind of incidental
        # byte that makes two identical quarantine tables differ. Row groups are pinned
        # to the configured size for the same reason they are pinned for the events.
        frame.sort(["row_index", "reason"]).write_parquet(
            buffer,
            compression=self.settings["compression"],
            compression_level=self.settings["compression_level"],
            row_group_size=self.settings["row_group_size"],
            statistics=False,
        )
        return write_bytes_atomic(target, buffer.getvalue()).as_posix()

    def write_run_manifest(
        self,
        source_dataset: str,
        payload: Mapping[str, object],
    ) -> str:
        """One manifest per source: the sidecar home for ``run_id`` and ``ingested_at``."""
        target = self.source_dir(source_dataset) / RUN_MANIFEST_FILENAME
        return write_bytes_atomic(target, (dumps(payload) + "\n").encode("utf-8")).as_posix()

    def register_views(
        self,
        *,
        canonical_files: Mapping[str, Sequence[str]],
        quarantine_files: Sequence[str],
    ) -> list[dict[str, object]]:
        """Create or replace the DuckDB views and read each one back.

        ``canonical_files`` is per source so a per-corpus query (plan §6's "we report
        metrics per corpus and never average them") has a view to point at, and the
        union view exists for the corpus-agnostic stages. Reading ``count(*)`` back is
        what proves the view resolves against the files rather than merely being
        recorded in the catalog.
        """
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        registrations: list[dict[str, object]] = []
        with duckdb.connect(str(self.duckdb_path)) as connection:
            all_files: list[str] = []
            for source_dataset in sorted(canonical_files):
                files = sorted(canonical_files[source_dataset])
                if not files:
                    continue
                all_files.extend(files)
                view = f"{CANONICAL_VIEW}_{source_dataset}"
                connection.execute(_create_view(view, files))
                registrations.append(
                    ViewRegistration(view, len(files), _read_count(connection, view)).as_dict()
                )
            if all_files:
                connection.execute(_create_view(CANONICAL_VIEW, all_files))
                registrations.append(
                    ViewRegistration(
                        CANONICAL_VIEW, len(all_files), _read_count(connection, CANONICAL_VIEW)
                    ).as_dict()
                )
            if quarantine_files:
                connection.execute(_create_view(QUARANTINE_VIEW, sorted(quarantine_files)))
                registrations.append(
                    ViewRegistration(
                        QUARANTINE_VIEW,
                        len(quarantine_files),
                        _read_count(connection, QUARANTINE_VIEW),
                    ).as_dict()
                )
        return registrations


def _create_view(view: str, files: Sequence[str]) -> str:
    """The CREATE OR REPLACE statement, with every path quoted as a SQL literal."""
    listed = ", ".join(_sql_literal(path) for path in files)
    return f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet([{listed}])"


def _sql_literal(path: str) -> str:
    """A single-quoted SQL string with embedded quotes doubled."""
    return "'" + path.replace("'", "''") + "'"


def _read_count(connection: duckdb.DuckDBPyConnection, view: str) -> int:
    """Query the view. A view that cannot be read is a failed artifact, not a zero."""
    row = connection.execute(f"SELECT count(*) FROM {view}").fetchone()
    if row is None:
        raise RuntimeError(f"duckdb returned no row for count(*) over view {view}")
    return int(row[0])


def _as_datetime(value: Any) -> datetime | None:
    """Normalise whatever polars hands back for a min/max instant into a datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    raise TypeError(f"unexpected window boundary of type {type(value).__name__}")


def empty_quarantine_frame() -> pl.DataFrame:
    """The quarantine shape with its columns declared and zero rows.

    A green run lands nothing, and "nothing" still needs a shape: the
    ``quarantine_records`` view is created only over files that exist, so the empty
    case is expressed by this frame rather than by a bare ``pl.DataFrame()`` whose
    missing columns would read as a schema change.
    """
    return pl.DataFrame(
        {name: pl.Series(name, [], dtype=dtype) for name, dtype in QUARANTINE_COLUMNS.items()}
    )


__all__ = [
    "CANONICAL_VIEW",
    "PARQUET_CODECS",
    "QUARANTINE_COLUMNS",
    "QUARANTINE_VIEW",
    "RUN_MANIFEST_FILENAME",
    "BatchArtifact",
    "CanonicalSink",
    "SinkConfigError",
    "ViewRegistration",
    "empty_quarantine_frame",
    "parquet_settings",
    "sha256_of_file",
]
