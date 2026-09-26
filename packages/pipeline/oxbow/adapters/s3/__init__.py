"""S3-protocol adapters, pointed at MinIO in Compose (02 §C).

Object storage is the one external system the demo genuinely integrates rather
than simulates. ``boto3`` is already a pinned dependency (pyproject), so no new
third-party import enters the tree here — 01 §A rule 6 is satisfied by the
existing pin, not by a fresh one.
"""

from __future__ import annotations

__all__: list[str] = []
