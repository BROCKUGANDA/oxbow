"""A backtest that produced no artifact must not exit successfully (the exit-code gate).

THE MEASURED FAILURE THIS PINS
------------------------------
Run ``01M3GJXASSCDAG1DBDH6JEG7BF``: ``oxbow backtest --corpus <landed corpus>`` got past the
30-day embargo refusal and then died inside the fold chain with ``LightGBMError: bad
allocation``, printing a traceback, and left ``out/backtest/01M3GJXASSCDAG1DBDH6JEG7BF/``
**empty**. The stage had already created the output directory (``main`` mkdirs before it
works), and the exit status the operator saw was green, so an empty directory sat behind a
successful run. Failure is a state, never a silence, and a phase gate that cannot fail is not
a gate.

WHAT IS NOW CHECKED, IN TWO PLACES
----------------------------------
1. :func:`oxbow.backtest.run.main` catches a fold chain that raises for any reason other than
   the splits module's embargo refusal, prints the traceback, names the empty directory, and
   RETURNS 1 instead of letting the exception's exit status depend on how the caller piped
   stderr.
2. :func:`oxbow.backtest.run._artifacts_written_code` re-reads the filesystem: every path the
   run declares it wrote must exist as a non-empty file, or the run returns 1. A return value
   is a claim; the file on disk is the measurement.

DETERMINISM: no wall clock, no set ordering; the corpus is the repository's own deterministic
fake and the failure is injected through the scorer seam the harness already calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest
from lightgbm.basic import LightGBMError

from oxbow.backtest import fakes, run as backtest_run

ABLATION_ARTIFACT = "ablation_results.json"
MODEL_CARD_ARTIFACT = "model_card.json"


def _corpus_path(tmp_path: Path) -> Path:
    corpus = fakes.make_corpus(n_accounts=500, span_days=500, with_typology=True)
    assert isinstance(corpus, pl.DataFrame)
    path = tmp_path / "corpus.parquet"
    corpus.write_parquet(path)
    return path


def test_a_fold_chain_that_raises_exits_non_zero_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The exact historical case: the fold fit dies, so the command must not be green.

    Removing the ``except Exception`` from ``main`` makes this test fail by letting the
    exception escape -- which is the defect, since the escape is what made the status depend
    on the caller rather than on the run.
    """
    from oxbow.models.scorer import WalkForwardScorer

    corpus_path = _corpus_path(tmp_path)
    out_dir = tmp_path / "backtest_out"

    def _die(self: Any, **kwargs: Any) -> Any:
        del self, kwargs
        raise LightGBMError("bad allocation")

    monkeypatch.setattr(WalkForwardScorer, "score", _die, raising=True)

    code = backtest_run.main(["--corpus", str(corpus_path), "--out", str(out_dir)])

    captured = capfd.readouterr()
    assert code != 0, (
        f"a fold chain that raised exited {code} while writing nothing to {out_dir}; "
        "an empty artifact directory behind a green exit is the failure this gate exists to stop"
    )
    assert "bad allocation" in captured.err, (
        "the traceback must reach the operator: the exit code alone re-silences the failure"
    )
    assert "wrote NOTHING" in captured.err, captured.err
    assert not (out_dir / ABLATION_ARTIFACT).is_file(), (
        "the run failed and still landed an ablation artifact; the artifact would be a lie"
    )


def test_declared_artifacts_absent_or_empty_on_disk_exit_non_zero(tmp_path: Path) -> None:
    """The postcondition is measured against the filesystem, not against the return value."""
    written = tmp_path / "real.json"
    written.write_text('{"folds": [0]}', encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")

    assert backtest_run._artifacts_written_code({"ablation": written}) == 0  # noqa: SLF001

    missing_path = tmp_path / "never_written.json"
    assert backtest_run._artifacts_written_code({"ablation": missing_path}) != 0, (  # noqa: SLF001
        "a declared artifact that is not on disk must not read as a successful run"
    )
    assert backtest_run._artifacts_written_code({"ablation": empty}) != 0, (  # noqa: SLF001
        "a zero-byte artifact is an empty directory wearing a filename"
    )


def test_a_run_that_writes_its_artifacts_still_exits_zero(tmp_path: Path) -> None:
    """The gate can pass: it fails on absence, not on principle.

    Without this the exit-code change could be made trivially green by returning 1 from every
    path, and the mutation of the real guard would go unnoticed.
    """
    out_dir = tmp_path / "demo_out"
    code = backtest_run.main(["--demo-fakes", "--out", str(out_dir)])
    assert code == 0, "the fake-harness demonstration must stay green"
    payload = out_dir / ABLATION_ARTIFACT
    assert payload.is_file() and payload.stat().st_size > 0
    assert (out_dir / MODEL_CARD_ARTIFACT).is_file()


def test_the_embargo_refusal_still_exits_non_zero_via_systemexit(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The splits module's refusal stays a named refusal, and it is still a non-green exit.

    A 4,000-row head of the landed corpus cannot hold a 30-day embargo across five folds;
    that is DEV-013's measured fact and it must surface as ``SystemExit`` with the arithmetic,
    not as a swallowed return code.
    """
    corpus = fakes.make_corpus(n_accounts=400, span_days=12, with_typology=False)
    path = tmp_path / "short.parquet"
    corpus.write_parquet(path)
    out_dir = tmp_path / "short_out"

    with pytest.raises(SystemExit) as exit_raised:
        backtest_run.main(["--corpus", str(path), "--out", str(out_dir)])

    assert exit_raised.value.code not in (None, 0, "")
    message = str(exit_raised.value.code)
    assert "embargo" in message.lower(), message
    assert not (out_dir / ABLATION_ARTIFACT).is_file()
