"""`make eval` refuses to publish a superseded backtest, rather than quietly re-quoting it.

The publisher reads two fixed paths (`out/backtest/ablation_results.json` and its model card)
while `oxbow backtest` writes a per-run directory by default. So the failure mode is not a
crash: it is the cards coming out clean, current-looking and describing the *previous* run —
which is the shape of a stale claim a reviewer has no way to disprove without opening `out/`.

The generated documents already carry artifact digests, which makes staleness detectable after
the fact (§16's detector). This is the check that fires before the document is written.

Timestamps are set with `os.utime`, never slept on: a test that waits on the clock is a coin
toss, and the one-second tolerance in the guard exists precisely so a filesystem with coarse
mtime resolution cannot make this flaky.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from oxbow.eval import _declared_provenance, _refuse_a_stale_backtest_artifact

BASE = 1_700_000_000  # a fixed epoch, so the arithmetic below is checkable on paper


def _write(root: Path, relative: str, document: dict[str, Any], *, mtime: float) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _arm(provenance: str) -> dict[str, Any]:
    return {"variants": [{"provenance": provenance, "row_id": "a"}]}


def test_a_real_run_is_never_published_behind_a_harness_self_check(tmp_path: Path) -> None:
    """Provenance, not the clock: the published file is fake while a run directory is real."""
    _write(tmp_path, "out/backtest/ablation_results.json", _arm("fake_harness"), mtime=BASE + 60)
    newer = _write(
        tmp_path,
        "out/backtest/run_real/ablation_results.json",
        _arm("real_corpus"),
        mtime=BASE,  # deliberately older on disk: only its provenance makes it the candidate
    )

    with pytest.raises(FileNotFoundError) as caught:
        _refuse_a_stale_backtest_artifact(tmp_path)

    message = str(caught.value)
    assert "out/backtest/run_real/ablation_results.json" in message
    assert "fake_harness" in message and "real_corpus" in message
    assert "--out" in message, "the refusal has to name the fix, not just the fact"
    assert newer.is_file()


def test_a_published_copy_older_than_what_it_replaced_is_refused(tmp_path: Path) -> None:
    """Same provenance, older clock: the second run's numbers are the ones being claimed."""
    _write(tmp_path, "out/backtest/ablation_results.json", _arm("real_corpus"), mtime=BASE)
    _write(
        tmp_path,
        "out/backtest/run_second/ablation_results.json",
        _arm("real_corpus"),
        mtime=BASE + 120,
    )

    with pytest.raises(FileNotFoundError, match="written after"):
        _refuse_a_stale_backtest_artifact(tmp_path)


def test_a_current_publication_and_an_absent_one_both_pass(tmp_path: Path) -> None:
    """The guard bites on supersession only; it does not invent work the run never had."""

    # Nothing published yet: ARTIFACTS reports the absence and this stays quiet.
    _refuse_a_stale_backtest_artifact(tmp_path)

    published = _write(
        tmp_path, "out/backtest/ablation_results.json", _arm("real_corpus"), mtime=BASE + 300
    )
    _write(
        tmp_path, "out/backtest/run_first/ablation_results.json", _arm("real_corpus"), mtime=BASE
    )
    _refuse_a_stale_backtest_artifact(tmp_path)  # the copy is the newest thing on disk
    assert published.is_file()


def test_provenance_is_read_off_the_arms_and_not_guessed_from_a_path() -> None:
    assert _declared_provenance(_arm("real_corpus")) == "real_corpus"
    assert _declared_provenance(_arm("fake_harness")) == "fake_harness"
    mixed = {"variants": [{"provenance": "fake_harness"}, {"provenance": "real_corpus"}]}
    assert _declared_provenance(mixed) == "real_corpus", (
        "one real arm in the document makes it a corpus result; reading the first variant "
        "instead would keep publishing real numbers under a self-check label"
    )
    assert _declared_provenance({"variants": [{"row_id": "a"}]}) == "unknown"
