"""Shared adapter plumbing: output root, atomic writes, JSON and Parquet.

Two rules from the governing documents shape what is otherwise trivial IO:

* **Atomic replace, never append-in-place.** ``write_bytes_atomic`` writes to a
  sibling ``*.partial`` file and renames. A reader that sees a path can assume the
  bytes are complete, which is the same guarantee the batch manifest gives ingest
  for whole batches (03 §B): half a file that looks like a file is worse than no
  file, because it is indistinguishable from real data right up to the moment
  someone totals it.
* **Null adapters must work with nothing else running** (plan §13). So everything
  here is stdlib plus pyarrow, which is already pinned, and nothing here opens a
  socket.

The output root is ``out/``, which ``.gitignore`` excludes: these files are
evidence that a boundary was crossed, not artifacts to commit.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from oxbow.ports.case_sink import iso_z

OUT_DIRNAME: Final = "out"
ENV_OUT_ROOT: Final = "OXBOW_OUT_ROOT"
PARTIAL_SUFFIX: Final = ".partial"


def default_repo_root(start: Path | None = None) -> Path:
    """The directory holding ``config/pipeline.yaml``, or the cwd's ancestors.

    Duplicated from :mod:`oxbow.config` deliberately? No: importing that module
    pulls YAML parsing into every adapter, and the null adapters are the ones path
    that has to keep working when the config is the thing being tested. This
    version is five lines and has no failure modes of its own.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "pipeline.yaml").is_file():
            return candidate
    return here


def resolve_out_root(explicit: Path | None = None) -> Path:
    """Where null adapters write. Argument, then env, then ``<repo>/out``."""
    if explicit is not None:
        return Path(explicit).resolve()
    from_env = os.environ.get(ENV_OUT_ROOT, "").strip()
    if from_env:
        return Path(from_env).resolve()
    return default_repo_root() / OUT_DIRNAME


def port_dir(root: Path, port: str) -> Path:
    """``out/<port>/``, created. The plan's one-line spec for a null adapter."""
    path = root / port
    path.mkdir(parents=True, exist_ok=True)
    return path


def _json_default(value: Any) -> str:
    """Serialise datetimes exactly as the wire format does — one function, no drift.

    Adapters must not invent their own timestamp spelling: the audit chain digests
    these bytes, so a different format between the null sink and the webhook sink
    would mean two digests for the same row.
    """
    if isinstance(value, datetime | date):
        return iso_z(value) if isinstance(value, datetime) else value.isoformat()
    raise TypeError(f"cannot serialise {type(value).__name__}; encode it at the call site")


def dumps(value: Any) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace, UTF-8 as-is.

    Sorted keys are not cosmetic here — the audit chain digests this exact
    encoding, so two writes of the same mapping must produce the same bytes.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def pretty(value: Any) -> str:
    """The human-readable form, for the file a null adapter leaves behind."""
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, default=_json_default)


def write_bytes_atomic(path: Path, body: bytes) -> Path:
    """Write bytes so a concurrent reader either sees nothing or sees all of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + PARTIAL_SUFFIX)
    with partial.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(path)
    return path


def write_json_atomic(path: Path, value: Any, *, indent: bool = True) -> Path:
    """Write one JSON document atomically."""
    text = pretty(value) if indent else dumps(value)
    return write_bytes_atomic(path, (text + "\n").encode("utf-8"))


def append_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    """Append records as JSON lines and return how many were written.

    Append-only by design: this is how the null audit sink stores a chain, and a
    chain file that got rewritten is a chain that lost its history. ``fsync`` after
    the batch so a crash leaves whole lines, never a half-record that the verifier
    would read as a broken link.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(dumps(record))
            handle.write("\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, refusing a malformed line rather than skipping it.

    A skipped line in an audit file is a deleted row that verifies, which is the
    exact failure the chain exists to catch (03 §A rule 1).
    """
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_number} is not valid JSON: {exc}. Refusing to read past a "
                    "malformed audit line; skipping it would delete a row invisibly."
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            records.append(parsed)
    return records


def write_rows_tabular(directory: Path, stem: str, rows: Sequence[Mapping[str, Any]]) -> Path:
    """Write rows as Parquet, or as zero-line JSON when there are no rows.

    Parquet is what the real pipeline hands over, so the null warehouse writes the
    format the demo claims to support — same zstd compression as
    ``config/pipeline.yaml`` asks for, so an artifact from the demo can be diffed
    against an artifact from a real run. An empty row list gets an empty JSONL file
    because an empty Parquet file has no schema to declare, and a "0 rows" artifact
    that silently drops its column set is a lie about the shape of the data.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if not rows:
        return write_bytes_atomic(directory / f"{stem}.jsonl", b"")
    parquet_path = directory / f"{stem}.parquet"
    table = pa.Table.from_pylist([dict(row) for row in rows])
    tmp = directory / f"{stem}.{uuid.uuid4().hex}.tmp.parquet"
    pq.write_table(table, tmp, compression="zstd", use_dictionary=False)
    tmp.replace(parquet_path)
    return parquet_path


__all__ = [
    "ENV_OUT_ROOT",
    "OUT_DIRNAME",
    "append_jsonl",
    "default_repo_root",
    "dumps",
    "port_dir",
    "pretty",
    "read_jsonl",
    "resolve_out_root",
    "write_bytes_atomic",
    "write_json_atomic",
    "write_rows_tabular",
]
