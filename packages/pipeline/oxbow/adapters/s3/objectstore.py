"""S3-protocol object store, pointed at MinIO in Compose (02 §C).

The one external system the demo genuinely integrates rather than simulates.
``boto3`` is already pinned in ``pyproject.toml``, so nothing new enters the tree —
01 §A rule 6 is satisfied by the existing pin rather than by a fresh one.

Two MinIO-specific notes, because "it works against AWS" and "it works against
MinIO" are different claims:

* the digest a reference carries is **SHA-256 in object metadata**, not the ETag.
  An ETag is MD5 for single-part uploads and something else entirely for multipart,
  so a content-addressed store that trusted it would be trusting a different
  function depending on how the upload happened.
* addressing is path-style, which is what a self-hosted endpoint with no DNS
  wildcards needs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from oxbow.ports.objectstore import ObjectNotFound, ObjectRef, validate_key

DIGEST_METADATA = "oxbow-sha256"
_S3_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket", "404 Not Found"}


class S3ObjectStore:
    """An S3-protocol bucket used as a content-addressed artifact store."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str,
        secret_key: str,
        store_id: str = "s3",
    ) -> None:
        if not access_key or not secret_key:
            raise ValueError(
                "S3 credentials must come from the environment and neither is set. Refusing "
                "an anonymous write to a bucket that holds evidence."
            )
        self._bucket = bucket
        self._endpoint_url = endpoint_url
        self._store_id = store_id
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
        )

    @property
    def store_id(self) -> str:
        return self._store_id

    @property
    def bucket(self) -> str:
        return self._bucket

    def ensure_bucket(self) -> None:
        """Create the bucket if it does not exist. MinIO-init already does in Compose.

        Kept separate from ``__init__`` so constructing a store never has a side
        effect: the contract test constructs against a bucket that may not exist yet
        and must see a clean failure from the first write, not a silent mkdir.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    def put(self, key: str, body: bytes, content_type: str) -> ObjectRef:
        validate_key(key)
        digest = ObjectRef.digest(body)
        if self._exists(key):
            existing = self.head(key)
            if existing.sha256 != digest:
                raise ValueError(
                    f"s3 object {key!r} already holds different bytes; a content-addressed "
                    "store does not overwrite"
                )
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            Metadata={DIGEST_METADATA: digest},
        )
        return ObjectRef(
            key=key,
            sha256=digest,
            size_bytes=len(body),
            content_type=content_type,
            uri=self._uri(key),
            stored_at=datetime.now(UTC),
        )

    def get(self, key: str) -> bytes:
        validate_key(key)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise self._not_found_or_reraise(key, exc) from exc
        body: bytes = response["Body"].read()
        claimed = (response.get("Metadata") or {}).get(DIGEST_METADATA)
        if claimed and claimed != ObjectRef.digest(body):
            raise ValueError(
                f"s3 object {key!r} does not match its recorded digest ({claimed}): the bytes "
                "stored are not the bytes the reference names"
            )
        return body

    def head(self, key: str) -> ObjectRef:
        validate_key(key)
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise self._not_found_or_reraise(key, exc) from exc
        metadata: dict[str, Any] = response.get("Metadata") or {}
        return ObjectRef(
            key=key,
            sha256=str(metadata.get(DIGEST_METADATA, "")),
            size_bytes=int(response.get("ContentLength", 0)),
            content_type=str(response.get("ContentType", "application/octet-stream")),
            uri=self._uri(key),
            stored_at=None,
        )

    def list(self, prefix: str) -> Sequence[ObjectRef]:
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            keys.extend(str(item["Key"]) for item in page.get("Contents", []))
        return [self.head(key) for key in sorted(keys)]

    def delete(self, key: str) -> bool:
        validate_key(key)
        existed = self._exists(key)
        if existed:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        return existed

    def _exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in _S3_NOT_FOUND_CODES:
                return False
            raise
        return True

    def _not_found_or_reraise(self, key: str, exc: ClientError) -> Exception:
        if _error_code(exc) in _S3_NOT_FOUND_CODES:
            return ObjectNotFound(f"no object at s3://{self._bucket}/{key}")
        return exc

    def _uri(self, key: str) -> str:
        base = (self._endpoint_url or "https://s3.amazonaws.com").rstrip("/")
        return f"{base}/{self._bucket}/{key}"


def _error_code(exc: ClientError) -> str:
    error: dict[str, Any] = exc.response.get("Error", {})
    return str(error.get("Code") or error.get("code") or "")


__all__ = ["DIGEST_METADATA", "S3ObjectStore"]
