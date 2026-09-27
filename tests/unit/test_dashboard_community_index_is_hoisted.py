"""`_high_risk_networks` must build the community index once, not once per community.

The audit's finding: the set of stored `community.canonical_index` values was being
reconstructed *inside* the generator expression that iterates the communities, so the
landing route was O(communities²) — on runs with 6,150 and 23,646 communities that is
38 million and 559 million set insertions for one KPI tile.

The gate is not a timer. `communities` is handed to the read as a list subclass that
counts how many times it is iterated, and the assertion is that it is iterated **once**.
The set comprehension re-added to the loop turns that into one iteration per risky
community, which is the shape of the bug rather than a symptom of it.

Correctness is asserted in the same breath, because hoisting a set is only safe if the
answer is the same: communities are counted when they hold at least one band-D/E account,
and a membership whose `community_id` was never written to `community` is not a community.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api.readmodel import ReadModel  # noqa: E402
from api.routers.dashboard import _high_risk_networks  # noqa: E402

RUN_ID = "01READSHAPEDASHBOARD000000000"
N_COMMUNITIES = 600


class _CountedList(list):
    """A list that reports how often it was walked."""

    def __init__(self, rows: Sequence[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.iterations = 0

    def __iter__(self) -> Any:
        self.iterations += 1
        return super().__iter__()


class _RecordingSource:
    """The one read primitive `_high_risk_networks` uses: `select` by table name."""

    backend = "test"

    def __init__(self, tables: Mapping[str, list[dict[str, Any]]]) -> None:
        self.tables = {name: _CountedList(rows) for name, rows in tables.items()}
        self.calls: list[tuple[str, Any]] = []

    def select(
        self,
        table: str,
        *,
        where: Mapping[str, Any] | None = None,
        columns: Sequence[str] | None = None,
        order: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
        allow_missing: bool = False,
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls.append((table, where))
        rows = self._filtered(table, where)
        total = len(rows)
        # The unfiltered case hands back the counted list itself: a plain copy would hide
        # every iteration from the instrument, and this test is about how often the
        # community rows are walked.
        return rows if limit is None else list(rows)[offset : offset + limit], total

    def _filtered(self, table: str, where: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        rows = self.tables[table]
        band = (where or {}).get("band")
        if band is None:
            # Every fixture row carries RUN_ID, so the run predicate selects them all.
            return rows
        wanted = {band} if isinstance(band, str) else set(band)
        return [row for row in rows if row.get("band") in wanted]

    def counted(self, table: str) -> _CountedList:
        return self.tables[table]


def _fixture() -> _RecordingSource:
    """Every community holds exactly two accounts, one of them band D or E.

    One membership row points at a `community_id` that has no `community` row, which is
    the case the index set exists to exclude.
    """
    communities = [
        {"run_id": RUN_ID, "canonical_index": index, "size": 2}
        for index in range(N_COMMUNITIES)
    ]
    memberships: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    for index in range(N_COMMUNITIES):
        risky_key = f"RISKY{index:06d}"
        quiet_key = f"QUIET{index:06d}"
        memberships.append(
            {"run_id": RUN_ID, "community_id": index, "account_key": risky_key, "degree": 1}
        )
        memberships.append(
            {"run_id": RUN_ID, "community_id": index, "account_key": quiet_key, "degree": 1}
        )
        scores.append({"run_id": RUN_ID, "account_key": risky_key, "band": "E" if index % 2 else "D"})
        scores.append({"run_id": RUN_ID, "account_key": quiet_key, "band": "B"})
    # An orphan: a community id nobody wrote a `community` row for.
    orphan_key = "ORPHAN000001"
    memberships.append({"run_id": RUN_ID, "community_id": N_COMMUNITIES + 7, "account_key": orphan_key})
    scores.append({"run_id": RUN_ID, "account_key": orphan_key, "band": "D"})
    return _RecordingSource(
        {"community": communities, "account_membership": memberships, "score": scores}
    )


def test_the_community_index_is_built_once_not_once_per_community() -> None:
    source = _fixture()
    _high_risk_networks(ReadModel(source), RUN_ID)  # type: ignore[arg-type]
    walked = source.counted("community").iterations
    assert walked == 1, (
        f"`community` was walked {walked} times for {N_COMMUNITIES} communities: the "
        "canonical-index set is being rebuilt inside the loop that iterates the "
        "communities, which makes the dashboard O(communities²)"
    )


def test_the_count_is_the_communities_that_hold_a_high_band_account() -> None:
    source = _fixture()
    result = _high_risk_networks(ReadModel(source), RUN_ID)  # type: ignore[arg-type]
    assert result["count"] == N_COMMUNITIES, (
        f"counted {result['count']}, expected one per community carrying a band-D/E account"
    )
    assert result["basis"].startswith("communities (stored Leiden output)")


def test_a_membership_without_a_community_row_is_not_counted() -> None:
    """The hoisted set still excludes orphans; a set built once is not a set built wrong."""
    source = _fixture()
    # Drop one stored community: its two memberships must stop counting.
    remaining = [
        row for row in source.counted("community") if int(row["canonical_index"]) != 3
    ]
    source.tables["community"] = _CountedList(remaining)
    result = _high_risk_networks(ReadModel(source), RUN_ID)  # type: ignore[arg-type]
    assert result["count"] == N_COMMUNITIES - 1, (
        f"a community row was deleted and the count still says {result['count']}: the "
        "membership id is being trusted as if it were a stored community"
    )


def test_a_community_of_quiet_accounts_is_not_counted() -> None:
    """No re-clustering and no guessing: a band-B-only network is not a high-risk one."""
    source = _fixture()
    source.tables["score"] = _CountedList(
        [row for row in source.counted("score") if str(row["account_key"]).startswith("QUIET")]
    )
    result = _high_risk_networks(ReadModel(source), RUN_ID)  # type: ignore[arg-type]
    assert result["count"] == 0, f"every band-D/E score is gone but the count is {result['count']}"


def test_the_three_reads_are_three_statements() -> None:
    """Hoisting must not turn a set rebuild into extra warehouse round trips."""
    source = _fixture()
    _high_risk_networks(ReadModel(source), RUN_ID)  # type: ignore[arg-type]
    assert [table for table, _ in source.calls] == [
        "community",
        "account_membership",
        "score",
    ], f"the tile now reads {source.calls}"
