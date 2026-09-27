"""Folds do not carry the same channels, and the persist step has to survive that.

On 2026-09-27 a `oxbow score --max-events 40000` run scored all five folds — two degraded to
`scorecard_and_rules_only` because the GBM fit was refused, three ran the full stack — and then
died at the last step writing the artifact:

    ComputeError: schema names differ: got p_gbm, expected p_fused_raw

`write_run_artifacts` concatenated the per-fold frames with `how="vertical_relaxed"`, which
aligns by position and refuses a name mismatch. A degraded fold has no `p_gbm` and no
`anomaly_norm`; a full fold has no `drift_banner` or `drift_score_psi`. So the frames are the
same *kind* of thing with different columns, and the run that produced the most complete fold
coverage to date was the one that could not write it down. That is the worst possible place for
a guard to fire: every model was fitted, every fold was defensible, and the numbers still did
not exist on disk.

The persist step now aligns on column names, and the declared row contract
(`SCORED_ROW_COLUMNS` — documented since P4b as "the columns every persisted scored row
carries", previously enforced by nothing) is asserted on the union: a channel one fold lacked
is a null on its rows with `scoring_mode` saying why, but a contract column no fold produced is
a build defect and the write refuses.

Note the stand-in fold objects below. `write_run_artifacts` reads exactly one thing from a fold
— its `scored` frame — and a test that built a whole `FoldRun` would be asserting about the
models it had to invent rather than about the concat.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from oxbow.models.config import ReportingConfig
from oxbow.models.run import SCORED_ROW_COLUMNS, write_run_artifacts

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The two channels the full stack writes itself, and the two only the degraded path writes.
#: Each mode swaps two columns for two others, so a run holding both kinds of fold ends up
#: with frames of the SAME WIDTH and DIFFERENT NAMES — which is exactly the error observed:
#: `vertical_relaxed` aligns by position and reported "schema names differ: got p_gbm,
#: expected p_fused_raw" rather than complaining about a length.
_FULL_CHANNELS = ("p_gbm", "anomaly_norm")
_DEGRADED_CHANNELS = ("drift_banner", "drift_score_psi")


def _fold_frame(*, full_stack: bool, drop: tuple[str, ...] = ()) -> pl.DataFrame:
    """A scored frame carrying the contract minus whichever channel this mode did not produce."""
    produced = _FULL_CHANNELS if full_stack else _DEGRADED_CHANNELS
    others = tuple(c for c in (*_FULL_CHANNELS, *_DEGRADED_CHANNELS) if c not in produced)
    columns: dict[str, Any] = {
        name: [None, None]
        for name in SCORED_ROW_COLUMNS
        if name not in (*others, *drop)
    }
    columns["account_key"] = ["ACC-A", "ACC-B"]
    columns["fold"] = [0, 0]
    columns["scoring_mode"] = ["full_model_stack" if full_stack else "scorecard_and_rules_only"] * 2
    columns["p_fused_raw"] = [0.4, 0.7]
    columns["p_fused"] = [0.4, 0.7]
    columns["p_scorecard"] = [0.35, 0.66]
    if full_stack:
        columns["p_gbm"] = [0.55, 0.81]
        columns["anomaly_norm"] = [0.11, 0.19]
    else:
        columns["drift_banner"] = ["drift action taken", "drift action taken"]
        columns["drift_score_psi"] = [0.31, 0.31]
    frame = pl.DataFrame(columns)
    # Dropped last: a fixture that removed a column and then set its value would hand back the
    # column it was supposed to be missing, which is the mistake this line used to be.
    return frame.select([c for c in sorted(frame.columns) if c not in drop])


def _reporting(tmp_path: Path) -> ReportingConfig:
    return ReportingConfig(
        artifact_dir=str(tmp_path / "artifacts"),
        hash_artifacts=True,
        scored_output_filename="scored_rows.parquet",
        tie_break_score_column="p_fused",
    )


class _Fold:
    """Only `.scored` is read by the persist step; everything else would be set dressing."""

    def __init__(self, scored: pl.DataFrame) -> None:
        self.scored = scored


def test_divergent_folds_persist_as_one_artifact(tmp_path: Path) -> None:
    """The defect: all five folds scored, and the artifact could not be written."""
    full = _fold_frame(full_stack=True)
    degraded = _fold_frame(full_stack=False)
    # `x in y is False` would chain into two comparisons and read as always-False; spell it out.
    assert "p_gbm" not in degraded.columns
    assert "drift_banner" not in full.columns

    written = write_run_artifacts(
        [_Fold(full), _Fold(degraded)],  # type: ignore[arg-type]
        reporting=_reporting(tmp_path),
        model_card={"seed": 1337},
        root=tmp_path,
    )
    assert written["files"], "nothing was recorded as written"

    path = tmp_path / "artifacts" / "scored_rows.parquet"
    assert path.is_file(), f"the run reported {written} but left no {path}"
    back = pl.read_parquet(path)
    assert back.height == 4, "both folds' rows must be in the artifact, not one of them"

    # The union: each fold keeps its own channels and nulls where the other had them.
    assert "p_gbm" in back.columns and "drift_banner" in back.columns
    full_rows = back.filter(pl.col("scoring_mode") == "full_model_stack")
    degraded_rows = back.filter(pl.col("scoring_mode") == "scorecard_and_rules_only")
    assert full_rows.get_column("p_gbm").to_list() == [0.55, 0.81]
    assert degraded_rows.get_column("p_gbm").to_list() == [None, None], (
        "a degraded fold's missing channel must be null, never a number the model did not produce"
    )
    assert degraded_rows.get_column("drift_banner").to_list() == [
        "drift action taken",
        "drift action taken",
    ]
    assert full_rows.get_column("drift_banner").to_list() == [None, None]

    # No row loses its identity in the alignment.
    assert sorted(back.get_column("account_key").to_list()) == ["ACC-A", "ACC-A", "ACC-B", "ACC-B"]


def test_a_contract_column_no_fold_produced_refuses_the_write(tmp_path: Path) -> None:
    """The guard has to be capable of failing, or the alignment is a silent widening.

    Null-filling a channel one fold lacked is correct. Null-filling a column that NO fold ever
    produced would hand P8's UI a parquet that merely looks like the contract.
    """
    from oxbow.models.errors import ModelLayerError

    left = _fold_frame(full_stack=True, drop=("p_fused",))
    right = _fold_frame(full_stack=False, drop=("p_fused",))

    with pytest.raises(ModelLayerError, match="no fold produced"):
        write_run_artifacts(
            [_Fold(left), _Fold(right)],  # type: ignore[arg-type]
            reporting=_reporting(tmp_path),
            model_card={"seed": 1337},
            root=tmp_path,
        )


def test_a_run_whose_every_fold_degraded_refuses_rather_than_nulls_the_gbm_column(
    tmp_path: Path,
) -> None:
    """The case the alignment must NOT swallow: no fold in the run has a GBM.

    A run that degraded on every fold is a finding about the corpus, and it should say so. The
    artifact it would produce has a `p_gbm` of nothing but nulls, which the read model would
    print as a channel that exists and is empty — the same absent-value-as-zero shape this
    repository keeps refusing. So the contract check fires on the union, not per fold.
    """
    from oxbow.models.errors import ModelLayerError

    with pytest.raises(ModelLayerError, match="p_gbm"):
        write_run_artifacts(
            [_Fold(_fold_frame(full_stack=False))],  # type: ignore[arg-type]
            reporting=_reporting(tmp_path),
            model_card={"seed": 1337},
            root=tmp_path,
        )


def test_the_artifact_records_what_it_wrote(tmp_path: Path) -> None:
    """A digest table the docs quote has to name the file it hashed, with the right row count."""
    written = write_run_artifacts(
        [_Fold(_fold_frame(full_stack=True))],  # type: ignore[arg-type]
        reporting=_reporting(tmp_path),
        model_card={"seed": 1337},
        root=tmp_path,
    )
    record = written["files"]["scored_rows.parquet"]
    assert isinstance(record, dict) and record["rows"] == 2
    assert len(str(record["sha256"])) == 64
    assert json.dumps(written, default=str), "the record has to be serialisable for the docs"


def test_an_empty_fold_list_writes_no_parquet(tmp_path: Path) -> None:
    """No folds is not an empty artifact — it is no artifact, and the docs say `absent`."""
    written = write_run_artifacts(
        [],
        reporting=_reporting(tmp_path),
        model_card={"seed": 1337},
        root=tmp_path,
    )
    assert "scored_rows.parquet" not in written["files"]
    assert not (tmp_path / "artifacts" / "scored_rows.parquet").exists()


def test_the_live_score_stage_stacks_through_the_shared_helper() -> None:
    """The stage that runs is `cli.py`, not `write_run_artifacts`.

    `write_run_artifacts` is exported, tested, and has no production caller: the score stage
    assembles and lands its own frames. So a fix that only changed the writer would stay green in
    this file while the run still crashed — which is exactly what happened here. This reads the
    live source and refuses if the fold stack has drifted back to a positional concat.
    """
    cli = (_REPO_ROOT / "packages" / "pipeline" / "oxbow" / "cli.py").read_text(encoding="utf-8")

    assert "stack_scored_frames(scored_frames)" in cli, (
        "the score stage no longer stacks folds through the shared helper, so neither the "
        "schema alignment nor the SCORED_ROW_COLUMNS refusal is on the path that runs"
    )
    # Inside the function that uses it: `oxbow.cli` refuses to load when pyarrow is already
    # resident (DEV-021), which is why this module imports the model stack lazily.
    assert "from oxbow.models.run import FoldModelRunner, stack_scored_frames" in cli

    stage = cli.split("def _score_models_and_land", 1)[1].split("\ndef ", 1)[0]
    assert 'pl.concat(scored_frames, how="vertical_relaxed")' not in stage, (
        "a positional fold concat is back inside the score stage. Degraded and full-stack folds "
        "have the same width and different names, so it raises ComputeError only after every "
        "fold has already been fitted."
    )
    assert "stack_scored_frames(scored_frames)" in stage


def test_the_shared_helper_refuses_an_empty_frame_list() -> None:
    """`concat([])` would be a silently empty artifact; the caller means "nothing landed"."""
    from oxbow.models.errors import ModelLayerError
    from oxbow.models.run import stack_scored_frames

    with pytest.raises(ModelLayerError, match="no fold frames"):
        stack_scored_frames([])


def test_the_shared_helper_aligns_the_divergent_folds_it_is_called_with() -> None:
    """The helper itself, not only the writer that now delegates to it."""
    from oxbow.models.run import stack_scored_frames

    stacked = stack_scored_frames([_fold_frame(full_stack=True), _fold_frame(full_stack=False)])
    assert stacked.height == 4
    assert {"p_gbm", "drift_banner"}.issubset(set(stacked.columns))
    degraded = stacked.filter(pl.col("scoring_mode") == "scorecard_and_rules_only")
    assert degraded.get_column("p_gbm").to_list() == [None, None]
