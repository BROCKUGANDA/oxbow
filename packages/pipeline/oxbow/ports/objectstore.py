"""The ObjectStoreAdapter port: immutable artifacts by content key (02 §A, §C).

Object storage is the one external system 02 §C lists as genuinely integrated in
the demo, and it is the seam that keeps the pipeline and the API from sharing a
filesystem: the pipeline writes an artifact, hands back a reference, and the API
serves that reference without knowing which disk it lives on.

Two rules shape the interface:

* Keys are content-addressed. ``sha256`` of the body is part of the reference, so
  an artifact cannot be swapped under a run that already names it — the same
  argument as the audit chain, applied to files (01 §A rule 4: determinism before
  features).
* Reads are total or loud. A missing key raises :class:`ObjectNotFound`; it does
  not return empty bytes. An empty evidence file that verifies as empty is worse
  than a missing one (03 §A rule 2: never let an unknown become a zero).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

MIN_KEY_LENGTH: int = 1
MAX_KEY_LENGTH: int = 1024


class ObjectNotFound(FileNotFoundError):  # noqa: N818 - named to match the stdlib builtin it subclasses
    """The requested key does not exist in this store.

    Distinct from "exists and is empty", which is a real state an investigator
    must be able to see.
    """


class ObjectConflict(RuntimeError):  # noqa: N818 - published port name, in __all__; renaming is an API change, not a lint fix
    """A put would overwrite bytes that are not identical to the existing ones.

    Content-addressed keys make this an integrity event rather than a normal
    collision, so adapters raise instead of last-write-wins.
    """


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """A handle to one stored object, safe to persist in a run manifest.

    ``sha256`` is verified on read, not only trusted from the write path: MinIO
    and a local directory can both lose a byte, and the run is supposed to be
    reproducible.
    """

    key: str
    sha256: str
    size_bytes: int
    content_type: str
    uri: str
    stored_at: datetime | None = None

    @staticmethod
    def digest(body: bytes) -> str:
        """The content address of a body — the only accepted way to name it."""
        return hashlib.sha256(body).hexdigest()

    def matches(self, body: bytes) -> bool:
        """Whether a body is the object this reference names."""
        return self.sha256 == self.digest(body)


def validate_key(key: str) -> str:
    """Reject keys that cannot be stored portably.

    Path traversal is the live risk: a key of ``../../etc/passwd`` is a valid
    filename and would escape a file-backed store's root. Checked in the port
    helper so every implementation inherits it rather than re-deriving it.
    """
    if len(key) < MIN_KEY_LENGTH or len(key) > MAX_KEY_LENGTH:
        raise ValueError(f"object key must be between {MIN_KEY_LENGTH} and {MAX_KEY_LENGTH} chars")
    if key.startswith("/") or ".." in key.split("/") or "\\" in key:
        raise ValueError(f"object key is not path-safe: {key!r}")
    if any(ord(char) < 32 for char in key):
        raise ValueError(f"object key contains control characters: {key!r}")
    return key


@runtime_checkable
class ObjectStoreAdapter(Protocol):
    """Write-once object storage with content-addressed references.

    Every implementation — null, file, S3/MinIO — passes the same parametrised
    contract test (02 §H), which is what makes "we support MinIO" and "the demo
    runs offline" the same sentence rather than two claims.
    """

    @property
    def store_id(self) -> str:
        """Stable identifier for logs and problem payloads (``null``, ``s3``, ...)."""
        ...

    def put(self, key: str, body: bytes, content_type: str) -> ObjectRef:
        """Store a body under a key and return its verified reference.

        Implementations compute the digest themselves; callers do not supply one,
        because a caller-supplied digest is an assertion, not a measurement.
        """
        ...

    def get(self, key: str) -> bytes:
        """Return the bytes at ``key`` or raise :class:`ObjectNotFound`."""
        ...

    def head(self, key: str) -> ObjectRef:
        """Metadata without the body, for listing and for presigned links."""
        ...

    def list(self, prefix: str) -> Sequence[ObjectRef]:
        """Every object under ``prefix``, in lexicographic key order.

        Ordered because a run manifest built from an unordered listing is not
        reproducible across filesystems.
        """
        ...

    def delete(self, key: str) -> bool:
        """Remove an object, returning whether it existed.

        Exposed for lifecycle policy (evidence retention), never for
        correctness: no pipeline path may depend on a delete having happened.
        """
        ...


__all__ = [
    "MAX_KEY_LENGTH",
    "ObjectConflict",
    "ObjectNotFound",
    "ObjectRef",
    "ObjectStoreAdapter",
    "validate_key",
]
