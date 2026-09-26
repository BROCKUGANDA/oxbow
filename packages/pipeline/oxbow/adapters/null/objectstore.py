"""Null object store: writes under ``out/objectstore/`` and needs nothing.

The demo default for the ObjectStoreAdapter port (plan §13: null adapters must run
with nothing else up). It is not a pretend store — content addressing, the
digest-on-read check and the no-overwrite rule are all enforced here, so swapping
to MinIO changes the destination and not the behaviour. That is the whole argument
for having a null adapter: the contract test runs against both, and a port that
only its real adapter passes is not a port.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oxbow.adapters.io import dumps, resolve_out_root, write_bytes_atomic
from oxbow.ports.objectstore import ObjectConflict, ObjectNotFound, ObjectRef, validate_key

PORT_DIRNAME = "objectstore"
META_SUFFIX = ".meta.json"


class NullObjectStore:
    """A filesystem object store rooted at ``out/objectstore/``."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (resolve_out_root(root) / PORT_DIRNAME).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def store_id(self) -> str:
        return "null"

    @property
    def base_dir(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise ValueError(
                f"object key escapes the store root: {key!r}. validate_key catches '..' in a "
                "path segment; this catches the rest."
            )
        return path

    def _meta_path(self, path: Path) -> Path:
        return path.with_name(path.name + META_SUFFIX)

    def put(self, key: str, body: bytes, content_type: str) -> ObjectRef:
        """Store bytes; refuse to overwrite existing bytes with different ones."""
        path = self._path(key)
        digest = ObjectRef.digest(body)
        if path.exists():
            existing = path.read_bytes()
            if ObjectRef.digest(existing) != digest:
                raise ObjectConflict(
                    f"object {key!r} already holds different bytes. A content-addressed store "
                    "does not overwrite: the reference a run already made would name the old "
                    "content and return the new."
                )
            stored_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        else:
            write_bytes_atomic(path, body)
            stored_at = datetime.now(UTC)
            write_bytes_atomic(
                self._meta_path(path),
                dumps(
                    {
                        "key": key,
                        "sha256": digest,
                        "size_bytes": len(body),
                        "content_type": content_type,
                        "stored_at": stored_at.isoformat(),
                    }
                ).encode("utf-8"),
            )
        return ObjectRef(
            key=key,
            sha256=digest,
            size_bytes=len(body),
            content_type=content_type,
            uri=path.as_uri(),
            stored_at=stored_at,
        )

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFound(f"no object at key {key!r} in the null store")
        body = path.read_bytes()
        meta = self._meta(key)
        if meta is not None and meta.get("sha256") != ObjectRef.digest(body):
            raise ValueError(
                f"object {key!r} does not match its recorded digest: the stored bytes were "
                "modified after the reference was written"
            )
        return body

    def head(self, key: str) -> ObjectRef:
        meta = self._meta(key)
        if meta is None:
            raise ObjectNotFound(f"no object at key {key!r} in the null store")
        return ObjectRef(
            key=key,
            sha256=str(meta["sha256"]),
            size_bytes=int(meta["size_bytes"]),
            content_type=str(meta["content_type"]),
            uri=self._path(key).as_uri(),
            stored_at=None,
        )

    def list(self, prefix: str) -> Sequence[ObjectRef]:
        """Every object whose key starts with ``prefix``, sorted by key."""
        keys = sorted(
            str(path.relative_to(self._root)).replace("\\", "/")
            for path in self._root.rglob("*")
            if path.is_file() and not path.name.endswith(META_SUFFIX)
        )
        return [self.head(key) for key in keys if key.startswith(prefix)]

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.is_file():
            return False
        path.unlink()
        self._meta_path(path).unlink(missing_ok=True)
        return True

    def _meta(self, key: str) -> dict[str, Any] | None:
        meta_path = self._meta_path(self._path(key))
        if not meta_path.is_file():
            return None
        parsed: Any = json.loads(meta_path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None


__all__ = ["META_SUFFIX", "PORT_DIRNAME", "NullObjectStore"]
