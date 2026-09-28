"""A corpus whose as-of column is not timestamps cannot resolve a fold plan.

`_resolve_fold_plan` decides which walk-forward the run is walked on, and every boundary it
reports comes out of `stamps.min()` / `stamps.max()`. polars answers those reductions with a
union over every type a column can hold, so the type checker could not tell whether
`corpus_window` was a date or `b"2014-01-02"` — and the runtime answer is whichever the frame
happens to carry. This is the same rule the embargo guard runs on: fold arithmetic is decided in
exactly one module, so the one place that reads the corpus's own window has to refuse a column
that is not a window rather than walk on a string.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from oxbow.backtest.interfaces import FoldError  # noqa: E402
from oxbow.backtest.run import _as_moment  # noqa: E402

MOMENT = datetime(2014, 1, 2, 1, 30, 57, tzinfo=UTC)


def test_a_timestamp_passes_through_untouched() -> None:
    assert _as_moment(MOMENT, where="as_of_ts minimum") is MOMENT


@pytest.mark.parametrize(
    "value",
    [
        None,
        b"2014-01-02",
        "2014-01-02T01:30:57+00:00",
        1_388_645_457,
        float("nan"),
    ],
)
def test_anything_else_refuses_naming_the_column_and_what_it_found(value: Any) -> None:
    """The bytes case is the one that used to be silent.

    `f"{b'2014-01-02'}"` renders as `b'2014-01-02'`, so a corpus stored as strings or bytes
    would print a fold window that is not a window and still exit zero. The refusal is the only
    place that shape can be caught: by the time it reaches the artifact, it is prose.
    """
    with pytest.raises(FoldError, match="as_of_ts minimum") as caught:
        _as_moment(value, where="as_of_ts minimum")

    message = str(caught.value)
    assert repr(value) in message, message
    assert "not a timestamp" in message, message
