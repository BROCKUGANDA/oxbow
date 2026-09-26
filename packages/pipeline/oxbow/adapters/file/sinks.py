"""File-backed sinks for cases, notifications and reports (02 §A).

One class, three ports, because all three do the same thing at the same boundary:
validate a self-describing payload and land it on disk under a name that is already
an idempotency key. Splitting them into three files would be three copies of the
dedupe check, which is three chances to get one of them subtly wrong.

The filename *is* the idempotency key (``<key>.json``), so a second delivery of the
same decision is a write to the same path with the same bytes. That makes the
at-least-once rule in 02 §E observable from a directory listing rather than from a
log: ``ls out/case_sink | wc -l`` is the number of distinct deliveries, not the
number of attempts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oxbow.adapters.io import write_json_atomic
from oxbow.ports.case_sink import (
    CaseBundle,
    CaseSink,
    SinkReceipt,
    assert_self_describing,
    build_idempotency_key,
)
from oxbow.ports.notify import Notification, NotifySink
from oxbow.ports.report import ReportSink, ReportSubmission


class _DiskSink:
    """Shared landing logic: validate, then write one JSON document per key."""

    def __init__(self, directory: Path, *, sink_id: str) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._sink_id = sink_id

    @property
    def sink_id(self) -> str:
        return self._sink_id

    @property
    def base_dir(self) -> Path:
        return self._directory

    def delivered_keys(self) -> set[str]:
        """Keys already on disk, read from the filenames. No sidecar index."""
        return {
            path.name[: -len(".json")]
            for path in self._directory.glob("*.json")
            if path.name != "index.json"
        }

    def has_delivered(self, idempotency_key: str) -> bool:
        return idempotency_key in self.delivered_keys()

    def _land(self, key: str, payload: dict[str, Any]) -> SinkReceipt:
        write_json_atomic(self._directory / f"{key}.json", payload)
        return SinkReceipt(
            accepted=True,
            idempotency_key=key,
            consumer=self._sink_id,
            accepted_at=datetime.now(UTC),
        )


class FileCaseSink(_DiskSink, CaseSink):
    """Writes each decided case bundle to ``<directory>/<idempotency_key>.json``."""

    def __init__(self, directory: Path, *, sink_id: str = "file") -> None:
        super().__init__(directory, sink_id=sink_id)

    def emit(self, bundle: CaseBundle) -> SinkReceipt:
        payload = bundle.to_payload()
        assert_self_describing(payload)
        return self._land(bundle.idempotency_key, payload)


class FileNotifySink(_DiskSink, NotifySink):
    """Writes each notification to ``<directory>/<notification_id>.json``."""

    def __init__(self, directory: Path, *, sink_id: str = "file") -> None:
        super().__init__(directory, sink_id=sink_id)

    def notify(self, notification: Notification) -> SinkReceipt:
        payload = notification.validated_payload()
        return self._land(notification.notification_id, payload)


class FileReportSink(_DiskSink, ReportSink):
    """Writes each report to ``<directory>/<report_id>.json``, totals checked."""

    def __init__(self, directory: Path, *, sink_id: str = "file") -> None:
        super().__init__(directory, sink_id=sink_id)

    def submit(self, report: ReportSubmission) -> SinkReceipt:
        payload = report.validated_payload()
        return self._land(report.report_id, payload)

    def read(self, key: str) -> dict[str, Any]:
        """Read a landed document back. Used by the contract test's round trip."""
        path = self._directory / f"{key}.json"
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError(f"{path} is not a JSON object")
        return parsed


__all__ = [
    "FileCaseSink",
    "FileNotifySink",
    "FileReportSink",
    "build_idempotency_key",
]
