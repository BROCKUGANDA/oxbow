"""Session-wide test setup: one import ordering fix, and nothing else.

WHY THIS FILE EXISTS. On this platform the process segfaults -- no traceback, exit 139 --
when ``pyarrow`` is imported before ``cvxpy``. ``cvxpy`` arrives through ``optbinning``,
which ``oxbow.scoring.binning`` imports, and ``pyarrow`` arrives through ``pandera``
(``oxbow.contracts.canonical_v1``) and through the graph layer's Parquet path. The minimal
reproduction is two lines::

    import pyarrow          # fine
    import cvxpy            # segfault

reversed it is fine, and no environment variable changes it
(``KMP_DUPLICATE_LIB_OK``, ``MKL_THREADING_LAYER``, ``OMP_NUM_THREADS`` were each tried and
each still segfaulted). Nothing in the code is wrong; two wheels ship overlapping native
runtimes and the load order decides whether the process survives.

That matters for the test session because pytest runs every module in one process, and the
collection order happens to put the feature-layer tests -- which import ``oxbow.contracts``
and therefore ``pyarrow`` -- before the ones that cross into the scoring layer. Without this
file those later tests do not fail, they kill the session, which is the worst possible test
outcome: no line number, no assertion, no report.

The fix is to make the fragile import happen *first*, where it cannot be preempted, rather
than hoping each module gets its ordering right. It is a test-infrastructure concern, not
product behaviour: the pipeline's own composition root does the same thing explicitly in
``oxbow.cli`` because it has the same two libraries in one process.

If this ever stops being needed -- a pinned upgrade to ``pyarrow``, ``cvxpy`` or
``optbinning`` -- deleting it costs nothing: the imports below are cached no-ops for every
test that would have loaded them anyway.
"""

from __future__ import annotations

import importlib
import sys

import pytest

# ``optbinning`` before anything that can pull ``pyarrow``. Imported once, at collection
# time, so no test module's own import order can put the two in the wrong sequence.
if not any(name.startswith("optbinning") for name in sys.modules):
    importlib.import_module("optbinning")


@pytest.fixture(scope="session", autouse=True)
def _native_import_order() -> None:
    """Assert the ordering held, so a future crash names the cause instead of nothing.

    A silent segfault is the failure mode this file exists to prevent; a message is the next
    best thing when the prevention stops working.
    """
    if "optbinning" not in sys.modules:  # pragma: no cover - defensive
        raise RuntimeError(
            "optbinning was not imported before the test session started; pyarrow arriving "
            "first segfaults the process on this platform (see tests/conftest.py)"
        )
