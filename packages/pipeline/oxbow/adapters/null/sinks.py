"""Null sinks: the file sinks pointed at ``out/<port>/`` (plan §13).

The demo default for ``case_sink``, ``notify`` and ``report``. Each is one line of
path plus a ``sink_id``, and that thinness is the point: the null and the file
implementation share their code because they share their contract, and the contract
test runs against both so "the null adapter behaves like the real one" is checked
rather than asserted (02 §H).
"""

from __future__ import annotations

from pathlib import Path

from oxbow.adapters.file.sinks import FileCaseSink, FileNotifySink, FileReportSink
from oxbow.adapters.io import resolve_out_root


class NullCaseSink(FileCaseSink):
    """Writes decided case bundles to ``out/case_sink/<idempotency_key>.json``."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(resolve_out_root(root) / "case_sink", sink_id="null")


class NullNotifySink(FileNotifySink):
    """Writes notifications to ``out/notify/<notification_id>.json``."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(resolve_out_root(root) / "notify", sink_id="null")


class NullReportSink(FileReportSink):
    """Writes reports to ``out/report/<report_id>.json``."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(resolve_out_root(root) / "report", sink_id="null")


__all__ = ["NullCaseSink", "NullNotifySink", "NullReportSink"]
