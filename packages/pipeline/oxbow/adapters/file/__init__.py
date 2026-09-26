"""File-backed adapters: a durable local artifact with no service to depend on.

The step between "write to out/" and "write to MinIO" that is still worth having:
these are the adapters used when an operator wants the evidence bundle on a
mounted volume, and they are what proves the S3 adapter's key scheme is not
accidentally the local filesystem's.
"""

from __future__ import annotations

__all__: list[str] = []
