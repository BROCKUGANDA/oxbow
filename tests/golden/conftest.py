"""Fixtures for tests/golden - the golden corpus as data.

Import strategy, stated because it matters to the P3b rules agent: this
directory is deliberately NOT a package on sys.path; loader.py is imported
below by file location. Anything outside tests/golden that wants the corpus
either runs pytest (these fixtures arrive ready) or copy-adds the same two
lines:

    sys.path.insert(0, str(Path("tests/golden").resolve()))
    from loader import load_golden_transactions, load_golden_expectations

Keeping the path bootstrap in exactly one place stops an import error from
being mistaken for a fixture bug.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import polars as pl
import pytest

GOLDEN_DIR = Path(__file__).resolve().parent
if str(GOLDEN_DIR) not in sys.path:
    sys.path.insert(0, str(GOLDEN_DIR))

from loader import (  # noqa: E402  (path bootstrap must run before the import)
    load_build_manifest,
    load_golden_expectations,
    load_golden_transactions,
)


@pytest.fixture(scope="session")
def golden() -> pl.DataFrame:
    """The 534-row canonical corpus, total order (event_ts_utc, txn_id)."""
    return load_golden_transactions()


@pytest.fixture(scope="session")
def expected() -> dict[str, Any]:
    """expected.yaml as a dict: scenario -> planted rows + hand expectations."""
    return load_golden_expectations()


@pytest.fixture(scope="session")
def build_manifest() -> dict[str, Any]:
    """build_manifest.json as a dict: seed, rows, sha256, scenario -> ordinals."""
    return load_build_manifest()
