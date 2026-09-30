"""The queue's ranking fast path: position for every account, selection for the few.

`policy_engine.stored_ranking` / `rank_from_stored` exist because re-pricing 43,046 accounts
per request made `GET /api/alerts` answer in ~40 s and Next's rewrite proxy reset before that,
so the queue never rendered at all. These tests pin the two properties that make the fast path
trustworthy rather than merely quick: it reproduces the allocator's own first-fit selection, and
it never lets speed invent a number the run did not record.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from oxbow.quant.allocate import _greedy_scan

REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api.policy_engine import RANKING_COLUMNS, rank_from_stored, stored_ranking  # noqa: E402


class _Source:
    """The two read-model calls the fast path makes, with the rows a test hands it.

    `economics` is the ranking read; `score` is the read that restricts it to accounts the
    run actually scored and supplies the scored count. Serving both is what lets one fake
    drive the real function rather than a shape the test wishes it had.
    """

    def __init__(self, rows: list[dict[str, Any]], *, scored: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows
        self._scored = rows if scored is None else scored

    def select(self, table: str, **kwargs: Any):
        columns = list(kwargs["columns"])
        if table == "economics":
            assert columns == list(RANKING_COLUMNS)
            return [dict(row) for row in self._rows], len(self._rows)
        assert table == "score"
        assert columns == ["account_key"]
        return [dict(row) for row in self._scored], len(self._scored)


class _ReadModel:
    def __init__(self, rows: list[dict[str, Any]], *, scored: list[dict[str, Any]] | None = None) -> None:
        self.source = _Source(rows, scored=scored)


def _ranked(rows: list[dict[str, Any]], *, scored: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The ordered rows only — the value every ranking assertion below is about."""
    result = stored_ranking(_ReadModel(rows, scored=scored), "01RUN")  # type: ignore[arg-type]
    assert result is not None
    return result[0]


def _row(key: str, density: float, ev: int, minutes: float) -> dict[str, Any]:
    return {
        "account_key": key,
        "ev_density": density,
        "expected_value_minor": ev,
        "analyst_minutes": minutes,
        "currency": "UGX",
    }


def _without(key: str, **column_overrides: Any) -> dict[str, Any]:
    """A stored row with a column blanked or swapped — the shapes the fast path must refuse."""
    return {**_row(key, 1.0, 1, 5.0), **column_overrides}


def test_the_order_is_the_allocators_own_key_not_a_second_invention() -> None:
    """Density descending, ties broken on account_key — the comparator `ev.py` uses."""
    rows = [_row("b", 10.0, 100, 5), _row("a", 10.0, 100, 5), _row("c", 99.0, 100, 5)]
    assert [row["account_key"] for row in _ranked(rows)] == ["c", "a", "b"]


def test_an_economics_row_with_no_score_row_is_never_ranked_or_funded() -> None:
    """The slow path's candidates are `score` JOIN `economics`; this path must not widen that.

    A priced account the run never scored would otherwise appear in the queue and could be
    *funded*, which is the disagreement between the screen and the backtest that the whole
    fast path exists to avoid.
    """
    rows = [_row("a", 100.0, 500, 5), _row("ghost", 90.0, 500, 5)]
    ordered = _ranked(rows, scored=[{"account_key": "a"}])
    assert [row["account_key"] for row in ordered] == ["a"]


def test_every_account_is_positioned_even_when_almost_none_of_them_pays() -> None:
    """The capacity line has to sit inside a full list (plan §11.2).

    Three profitable accounts of five, a budget that reaches two of them: the other three
    still carry ranks, and the two unfunded-but-profitable ones are *not* marked selected.
    """
    ordered = [
        _row("p1", 100.0, 500, 5),
        _row("p2", 90.0, 400, 5),
        _row("p3", 80.0, 300, 5),
        _row("n1", 1.0, -10, 5),
        _row("n2", 0.5, -20, 5),
    ]
    ranks, cutoff = rank_from_stored(ordered, capacity_minutes=10)

    assert [ranks[key]["rank"] for key in ("p1", "p2", "p3", "n1", "n2")] == [1, 2, 3, 4, 5]
    assert [ranks[key]["selected"] for key in ("p1", "p2", "p3", "n1", "n2")] == [
        True,
        True,
        False,
        False,
        False,
    ]
    assert cutoff == 2, "the line is drawn at the last funded account, not at the last rank"


def test_a_negative_expected_value_is_never_funded_however_much_budget_is_left() -> None:
    """First-fit over the density order, but only over profitable rows — `_greedy_scan`'s rule."""
    ordered = [_row("a", 10.0, 100, 5), _row("b", 9.0, -1, 5)]
    ranks, cutoff = rank_from_stored(ordered, capacity_minutes=1_000)
    assert ranks["a"]["selected"] is True
    assert ranks["b"]["selected"] is False
    assert cutoff == 1


def test_an_account_too_large_for_the_remaining_budget_is_skipped_not_truncated() -> None:
    ordered = [_row("big", 100.0, 900, 50), _row("small", 90.0, 100, 5)]
    ranks, cutoff = rank_from_stored(ordered, capacity_minutes=10)
    assert ranks["big"]["selected"] is False
    assert ranks["small"]["selected"] is True
    assert cutoff == 2, "the line marks the deepest rank the budget reached"


def test_no_rank_at_all_is_reported_as_no_line_rather_than_a_line_at_zero() -> None:
    ranks, cutoff = rank_from_stored([_row("a", 1.0, -5, 5)], capacity_minutes=10)
    assert ranks["a"]["rank"] == 1
    assert cutoff is None


@pytest.mark.parametrize(
    ("rows", "why"),
    [
        ([], "nothing was priced"),
        ([_without("a", ev_density=None)], "a stored row with no density"),
        ([_without("a", analyst_minutes=None)], "a stored row with no minutes"),
        ([_without("a", expected_value_minor=None)], "a stored row with no expected value"),
        ([_without("a", currency=None)], "a stored row with no currency"),
        ([_row("a", 1.0, 1, 5), _without("b", currency="KES")], "two currencies in one run"),
    ],
)
def test_the_fast_path_declines_rather_than_guess(rows: list[dict[str, Any]], why: str) -> None:
    """A partial ordering would silently drop accounts and a mixed-currency first-fit would
    spend minutes against money that does not add up. Both fall back to the slow path.

    Every arm returns `None` rather than raising: this runs inside a request, so a
    `TypeError` on a null column would be a 500 on the queue instead of a fallback.
    """
    assert stored_ranking(_ReadModel(rows), "01RUN") is None, why  # type: ignore[arg-type]


class _Candidate(NamedTuple):
    """Only the fields `_greedy_scan` reads, so the differential exercises the real loop."""

    account_key: str
    review_minutes: int


def test_the_fast_path_funds_exactly_the_accounts_the_allocator_funds() -> None:
    """The claim in `rank_from_stored`'s docstring, tested against the function it copies.

    A comment saying "mirrors `_greedy_scan`" is not evidence. The candidate set is the
    composition `allocate_greedy` builds — the positive-EV rows in density order, since
    `positive_ev_rows` is what feeds the scan and the EV test is not inside it — and the
    assertion is that the two selections are the same *set*, at the same cutoff, for a
    budget that leaves one row too large to fit and a remainder that fits nothing.
    """
    rows = [
        _row("a", 100.0, 500, 12),
        _row("b", 95.0, 400, 10),
        _row("c", 90.0, 300, 25),
        _row("d", 85.0, -1, 5),
        _row("e", 80.0, 200, 6),
        _row("f", 60.0, 0, 5),
        _row("g", 10.0, 50, 3),
    ]
    ordered = _ranked(rows)
    capacity = 31

    ranks, cutoff = rank_from_stored(ordered, capacity)
    candidates = [
        _Candidate(str(row["account_key"]), int(row["analyst_minutes"]))
        for row in ordered
        if int(row["expected_value_minor"]) > 0
    ]
    chosen = _greedy_scan(candidates, capacity)

    assert {key for key, entry in ranks.items() if entry["selected"]} == {row.account_key for row in chosen}
    # a and b fit; c is too large for what is left and is skipped, not truncated; d pays
    # nothing and f pays exactly nothing, so neither is ever funded; e and g take the
    # remainder down to zero minutes.
    assert [row.account_key for row in chosen] == ["a", "b", "e", "g"]
    assert cutoff == ranks["g"]["rank"]
