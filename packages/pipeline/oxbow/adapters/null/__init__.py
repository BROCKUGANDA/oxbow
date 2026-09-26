"""Null adapters: write a file, and the demo needs nothing else running.

02 §A: every port has a null adapter that writes to ``out/<port>/`` as JSON or
Parquet. That is what makes "integration-ready" testable rather than rhetorical
— the real adapter and the null one pass the same contract test, so swapping them
changes the destination and not the behaviour (plan §13: the demo default must work
with nothing else up).
"""

from __future__ import annotations

__all__: list[str] = []
