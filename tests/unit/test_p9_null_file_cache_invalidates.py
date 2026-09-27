"""The null-file warehouse answered every read after its first from memory, forever.

`FileWarehouseSource._table_rows` cached by `(table, run_id)` with nothing to invalidate on,
and the null-file warehouse is what `make demo` and the offline boot run against. The SSE
stream polls `stage_event` once a second: the first poll read the file and populated the
dict, and every poll after it was served from that dict while the pipeline went on appending
to the very file that had been cached. The visible result was a run that was measurably
advancing looking dead on the screen the demo is filmed from — and `scripts/demo_seed.py`
refusing to snapshot a warehouse whose rows it could not see grow.

The fix is a signature (name, size, mtime of the files the rows came from) checked before the
cached rows are trusted. These tests hold both halves of that, because either half alone is a
defect:

* a cache that never goes stale is the freeze this file is about, and
* a read that never hits the cache is a rewrite of the parquet parse the cache exists to
  avoid, which the last test measures rather than assumes.

Written against the composed object — `select(...)` the way `stage_event_rows` calls it — so a
fix that only patched the helper would not pass.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Served as `uvicorn main:app --app-dir apps/api`, so `api.*` needs `apps` and its own
# directory on the path; the tests import the package the way the process runs it.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api", REPO_ROOT / "packages" / "pipeline"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api import readmodel  # noqa: E402
from api.readmodel import FileWarehouseSource, Gt  # noqa: E402

RUN = "01M3GVJ10K5FHPJ82BH1DYCJC9"


def _part_path(root: Path, table: str, run_id: str = RUN) -> Path:
    directory = root / "warehouse" / table
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"run={run_id}.jsonl"


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _event(seq: int) -> dict[str, Any]:
    return {
        "id": seq,
        "run_id": RUN,
        "stage": "score",
        "state": "running" if seq < 5 else "done",
        "detail": f"pass {seq}",
    }


def _poll(source: FileWarehouseSource, after_id: int = 0) -> list[dict[str, Any]]:
    """The read the stream actually performs, cursor and all."""
    rows, _ = source.select(
        "stage_event",
        where={"run_id": RUN, "id": Gt(after_id)},
        order="id",
        with_count=False,
    )
    return rows


def test_a_row_appended_after_the_first_poll_is_visible_on_the_next(tmp_path: Path) -> None:
    """The defect, stated as the smallest observable that shows it."""
    part = _part_path(tmp_path, "stage_event")
    _write_rows(part, [_event(1)])
    source = FileWarehouseSource(root=tmp_path)

    assert [row["id"] for row in _poll(source)] == [1]

    _write_rows(part, [_event(1), _event(2), _event(3)])

    ids = [row["id"] for row in _poll(source)]
    assert ids == [1, 2, 3], "the cache outlived the file it was read from"


def test_the_stream_cursor_serves_only_what_arrived_between_polls(tmp_path: Path) -> None:
    """A cursor into a growing ledger is the whole point of the poll loop."""
    part = _part_path(tmp_path, "stage_event")
    _write_rows(part, [_event(1), _event(2)])
    source = FileWarehouseSource(root=tmp_path)
    assert [row["id"] for row in _poll(source, after_id=0)] == [1, 2]

    _write_rows(part, [_event(1), _event(2), _event(3)])

    assert [row["id"] for row in _poll(source, after_id=2)] == [3]


def test_the_run_ledger_grows_with_the_run(tmp_path: Path) -> None:
    """`run` reads one fixed file rather than a run-scoped part, so it needs signing too."""
    runs = tmp_path / "warehouse" / "runs.jsonl"
    runs.parent.mkdir(parents=True, exist_ok=True)
    _write_rows(runs, [{"run_id": RUN, "state": "running"}])
    source = FileWarehouseSource(root=tmp_path)

    first, _ = source.select("run", where={"run_id": RUN})
    assert [row["state"] for row in first] == ["running"]

    _write_rows(runs, [{"run_id": RUN, "state": "running"}, {"run_id": "OTHER", "state": "done"}])

    # A second run row is not this run's row, so read the table the way the list route does.
    everything, _ = source.select("run")
    assert {row["run_id"] for row in everything} == {RUN, "OTHER"}


def test_a_run_that_appears_later_is_in_the_next_aggregate_read(tmp_path: Path) -> None:
    """The scope with no run id has to notice a file nobody had asked for yet."""
    stage = tmp_path / "warehouse" / "stage_event"
    stage.mkdir(parents=True, exist_ok=True)
    _write_rows(stage / f"run={RUN}.jsonl", [_event(1)])
    (stage / f"run={RUN}.manifest.json").write_text("{}", encoding="utf-8")

    source = FileWarehouseSource(root=tmp_path)
    assert len(_poll(source)) == 1

    second = "01M3SECONDRUN"
    _write_rows(
        stage / f"run={second}.jsonl",
        [{"id": 9, "run_id": second, "stage": "graph", "state": "done", "detail": "x"}],
    )
    (stage / f"run={second}.manifest.json").write_text("{}", encoding="utf-8")

    # No `run_id` in the predicate means the aggregate scope, which walks the manifests.
    rows, _ = source.select("stage_event", where={"state": "done"})
    assert {row["run_id"] for row in rows} == {second}


def test_an_absent_part_that_lands_later_is_not_missed(tmp_path: Path) -> None:
    """Polling a run before its first row exists must not cache the emptiness."""
    _part_path(tmp_path, "stage_event")
    source = FileWarehouseSource(root=tmp_path)
    assert _poll(source) == []

    _write_rows(tmp_path / "warehouse" / "stage_event" / f"run={RUN}.jsonl", [_event(1)])

    assert [row["id"] for row in _poll(source)] == [1]


def test_the_cache_still_caches_when_nothing_moved(tmp_path: Path, monkeypatch) -> None:
    """The guard against the lazy fix: signing must not quietly delete the cache.

    A `return` before the read would pass every test above and cost a parse per poll, which on
    a parquet part is the cost the cache was written to avoid. So the number of reads is the
    assertion, not a comment about performance.
    """
    part = _part_path(tmp_path, "stage_event")
    _write_rows(part, [_event(1), _event(2)])
    source = FileWarehouseSource(root=tmp_path)

    calls: list[str] = []
    real_read = readmodel.read_jsonl

    def counting_read(path: Path) -> list[dict[str, Any]]:
        calls.append(path.name)
        return real_read(path)

    monkeypatch.setattr(readmodel, "read_jsonl", counting_read)

    for _ in range(5):
        assert [row["id"] for row in _poll(source)] == [1, 2]

    assert calls == [f"run={RUN}.jsonl"], f"the signature check turned into a re-read: {calls}"


@pytest.mark.parametrize("table", ["decision", "review_case"])
def test_a_postgres_only_table_still_refuses_loudly(tmp_path: Path, table: str) -> None:
    """Invalidating must not turn "this deployment has no decision store" into an empty list."""
    from api.readmodel import WarehouseUnavailable

    _part_path(tmp_path, table)
    source = FileWarehouseSource(root=tmp_path)
    with pytest.raises(WarehouseUnavailable, match="does not exist in the null-file warehouse"):
        source.select(table, where={"run_id": RUN})
