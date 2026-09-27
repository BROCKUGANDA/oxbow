"""P7 — the three studio builders that count the run's own landed rows.

``band_definition``, ``scorecard_bin`` and ``scorecard_point`` are the last writers of the CLI
``warehouse`` stage that had no test at all. They differ from the artifact-shaped builders in
``test_p7_analytical_landing.py`` in one way that makes them harder to prove and easier to corrupt:
nothing is read from a published document. Every figure is a count over the run's own scored rows —
a band's population, a bin's share, a bin's bad rate — taken at the grain ``score`` itself uses,
one current row per account. A count can be wrong in three ways that all look identical on the
page: the grain can be wrong (an account scored in two folds counted twice), the denominator can be
the labelled part while the rate is presented as the whole band, or the population can be the
in-sample rows the model memorised.

Every expected number below is arithmetic done on paper against the literal rows written beside it
(§19 rule 6). Nothing here was run first and pasted after; where the shipped builder does something
other than what its own docstring promises, the test pins what ships and says so in the assertion
message.

The gates these tests exist to keep biting:

* a band with any unlabelled row refuses — presenting ``1/3`` as the rate of a 3-account band whose
  third account has no label is the substitution plan §18 calls a lie;
* a band with no declared action or no declared review effort refuses, because those two columns are
  ``NOT NULL`` and the only honest source is the config the run consumed;
* a band letter outside ``A-E`` refuses by naming ``ck_band_definition_band``;
* an account scored twice contributes once;
* an attribute the folds disagree about refuses entirely, and the accounts that needed it land no
  ``scorecard_point`` rows at all — the case page sums those rows against the band's point range, so
  a short list reads as a safer account.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import polars as pl
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from oxbow.adapters.warehouse.landing import (  # noqa: E402
    LandingError,
    band_definition_rows,
    scorecard_bin_rows,
    scorecard_point_rows,
)
from oxbow.adapters.warehouse.models import (  # noqa: E402
    BandDefinition,
    ScorecardBin,
    ScorecardPoint,
)

# --- the fixture frame --------------------------------------------------------

AS_OF_Q1: Final = datetime(2024, 1, 31, tzinfo=UTC)
AS_OF_Q3: Final = datetime(2024, 7, 31, tzinfo=UTC)
AS_OF_Q4: Final = datetime(2024, 10, 31, tzinfo=UTC)

#: The columns the three builders read out of ``out/score/<run>/scored_rows.parquet``. Declared
#: explicitly so an all-null ``band`` or ``label_is_fraud`` column keeps a dtype instead of being
#: inferred away from the row that needs it.
SCORED_SCHEMA: Final[dict[str, pl.DataType]] = {
    "account_key": pl.Utf8,
    "role": pl.Utf8,
    "fold": pl.Int64,
    "as_of_ts": pl.Datetime("us", "UTC"),
    "band": pl.Utf8,
    "score_points": pl.Int64,
    "label_is_fraud": pl.Int64,
    "points_json": pl.Utf8,
}

#: ``band_definition.action`` and ``.review_minutes`` are declared configuration, not measurement —
#: ``_frame_tables`` reads them from ``config/scorecard.yaml`` and ``config/economics.yaml``. These
#: are the same five sentences and the same five numbers, hand-written here rather than loaded, so
#: no test in this file touches the filesystem.
ACTIONS: Final[dict[str, str]] = {
    "A": "monitor",
    "B": "review",
    "C": "review",
    "D": "escalate",
    "E": "escalate",
}
MINUTES: Final[dict[str, float]] = {"A": 0.0, "B": 15.0, "C": 45.0, "D": 120.0, "E": 240.0}


def _entry(
    feature: str, bin_label: str, points: Any, woe: Any, *, kind: str = "quantile"
) -> dict[str, Any]:
    """One ``points_json`` entry, exactly as the scorecard layer serialises it.

    ``points`` and ``woe`` are deliberately typed ``Any``: the shape under test is what happens when
    the artifact states one of them in a form that is not a number.
    """
    return {
        "feature": feature,
        "bin_label": bin_label,
        "bin_kind": kind,
        "points": points,
        "woe": woe,
    }


def _row(
    account_key: str,
    band: str | None,
    points: int,
    label: int | None,
    *,
    fold: int = 0,
    as_of: datetime = AS_OF_Q1,
    role: str = "test",
    entries: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """One scored row. ``label`` is ``None`` for an account nothing labelled, ``band`` for no band."""
    return {
        "account_key": account_key,
        "role": role,
        "fold": fold,
        "as_of_ts": as_of,
        "band": band,
        "score_points": points,
        "label_is_fraud": label,
        "points_json": json.dumps(list(entries)),
    }


def _scored(rows: Sequence[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=SCORED_SCHEMA)


def _band_rows() -> list[dict[str, Any]]:
    """Five out-of-sample accounts in two bands, plus one in-sample account that must not count."""
    return [
        _row("a1", "B", 12, 0),
        _row("a2", "B", 20, 1),
        _row("a3", "B", 8, 0),
        _row("a4", "B", 30, 0),
        _row("a5", "A", 2, 0),
        _row("a6", "E", 100, 1, role="train"),
    ]


def _bin_rows() -> list[dict[str, Any]]:
    """Four test accounts over two attributes, and one train account the count must never see.

    ``velocity``: a1 and a2 fall in ``">=100"`` (a1 is the only positive), a3 and a4 in ``"[10,100)"``.
    ``night_share``: a1 and a2 in ``"[0.5,1.0)"``, a3 in ``"<0.5"``, a4 in ``"missing"`` — the last two
    both weighted 0.0 so their order is decided by the label tie-break alone.
    """
    velocity_hi = _entry("velocity", ">=100", 30, 0.3)
    night_hi = _entry("night_share", "[0.5,1.0)", 12, 0.12)
    velocity_mid = _entry("velocity", "[10,100)", 5, 0.05)
    return [
        _row("a1", "B", 42, 1, entries=[velocity_hi, night_hi]),
        _row("a2", "B", 42, 0, entries=[velocity_hi, night_hi]),
        _row("a3", "B", 5, 0, entries=[velocity_mid, _entry("night_share", "<0.5", 0, 0.0)]),
        _row("a4", "B", 5, 0, entries=[velocity_mid, _entry("night_share", "missing", 0, 0.0)]),
        # a5 is in-sample. Its weight is different from every landed bin on purpose: were a train row
        # ever counted, it would both add a third ``velocity`` bin and move every share off 0.5.
        _row("a5", "E", 99, 1, role="train", entries=[_entry("velocity", ">=1000", 90, 0.9)]),
    ]


def _conflict_rows() -> list[dict[str, Any]]:
    """Fold 0 and fold 1 weight the same ``night_share`` bin differently; ``velocity`` agrees."""
    velocity_hi = _entry("velocity", ">=100", 30, 0.3)
    return [
        _row("a1", "B", 42, 1, entries=[velocity_hi, _entry("night_share", "[0.5,1.0)", 12, 0.12)]),
        _row("a2", "B", 30, 0, entries=[velocity_hi]),
        _row(
            "a3",
            "C",
            55,
            0,
            fold=1,
            as_of=AS_OF_Q4,
            entries=[_entry("night_share", "[0.5,1.0)", 25, 0.25)],
        ),
    ]


def _table_columns(model: Any) -> set[str]:
    """The declared table's own columns, minus the two the sink stamps on every row."""
    return {str(column.name) for column in model.__table__.columns} - {"id", "run_id"}


# --- the seam: the rows carry the table's names, not the frame's --------------


def test_every_builder_emits_exactly_its_tables_own_columns() -> None:
    """A rename on either side of the seam has to fail here, not land a null Postgres accepts."""
    frame = _scored(_bin_rows())
    bands, _band_refused = band_definition_rows(frame, actions=ACTIONS, review_minutes=MINUTES)
    bins, _bin_refused = scorecard_bin_rows(frame)
    points, _point_refused = scorecard_point_rows(frame)

    assert bands and bins and points, "an empty return would make the key comparison below vacuous"
    assert {tuple(sorted(row)) for row in bands} == {tuple(sorted(_table_columns(BandDefinition)))}
    assert {tuple(sorted(row)) for row in bins} == {tuple(sorted(_table_columns(ScorecardBin)))}
    assert {tuple(sorted(row)) for row in points} == {tuple(sorted(_table_columns(ScorecardPoint)))}


# --- band_definition: the rate, the population, and the two declared columns --


def test_a_fully_labelled_band_lands_its_measured_rate_and_its_declared_effort() -> None:
    rows, refused = band_definition_rows(
        _scored(_band_rows()), actions=ACTIONS, review_minutes=MINUTES
    )

    # Counted by hand off _band_rows(), one row per account:
    #   band A = {a5: 2 pts, clean}  -> n 1, positives 0 -> 0/1 = 0.0,  point range 2-2
    #   band B = {a1: 12, a2: 20, a3: 8, a4: 30}, positive = a2 only
    #          -> n 4, positives 1 -> 1/4 = 0.25, point range min 8 - max 30
    # a6 is role="train": filtered before the count, so band E is not in the table at all.
    assert refused == [], f"every band here is labelled and declared: {refused}"
    assert rows == [
        {
            "band": "A",
            "lower_points": 2,
            "upper_points": 2,
            "observed_rate": 0.0,
            "n": 1,
            "action": "monitor",
            "review_minutes": 0.0,
        },
        {
            "band": "B",
            "lower_points": 8,
            "upper_points": 30,
            "observed_rate": 0.25,
            "n": 4,
            "action": "review",
            "review_minutes": 15.0,
        },
    ], rows
    assert [row["band"] for row in rows] == ["A", "B"], "ascending letter, which is the table's key"
    assert rows[0]["review_minutes"] == 0.0, (
        "a declared zero is a measurement of no effort; treating it as absent would refuse band A "
        "and drop the cheapest band off the studio page"
    )


def test_a_band_with_one_unlabelled_row_refuses_rather_than_publishing_the_labelled_part() -> None:
    frame = _scored(
        [
            _row("a1", "B", 10, 1),
            _row("a2", "B", 20, 0),
            _row("a3", "B", 30, None),
            _row("a4", "A", 3, 0),
        ]
    )
    rows, refused = band_definition_rows(frame, actions=ACTIONS, review_minutes=MINUTES)

    # ``label_is_fraud`` sums over the null and ``len()`` counts it, so the arithmetic the builder
    # would have published is positives 1 over n 3 = 0.3333... — a rate over the two labelled
    # accounts presented as the rate of a three-account band. That substitution is what §18 refuses.
    assert refused == [
        "band B: 1 of 3 row(s) carry no label, so the band's rate is not a rate over its population"
    ], refused
    assert all(
        "0.33" not in str(row["observed_rate"]) for row in rows
    ), f"the part-rate must not reach the table: {rows}"
    assert rows == [
        {
            "band": "A",
            "lower_points": 3,
            "upper_points": 3,
            "observed_rate": 0.0,
            "n": 1,
            "action": "monitor",
            "review_minutes": 0.0,
        }
    ], "the refusal is per band; the labelled band beside it is unaffected"


@pytest.mark.parametrize(
    ("actions", "minutes", "expected", "absent"),
    [
        (
            {},
            {"B": 15.0},
            ("action: no action declared for band B in config/scorecard.yaml",),
            ("review_minutes",),
        ),
        (
            {"B": "review"},
            {},
            (
                "review_minutes: no review effort declared for band B in config/economics.yaml "
                "review_minutes_by_alert_class",
            ),
            ("action:",),
        ),
        (
            {},
            {},
            (
                "action: no action declared for band B in config/scorecard.yaml",
                "review_minutes: no review effort declared for band B in config/economics.yaml "
                "review_minutes_by_alert_class",
            ),
            (),
        ),
    ],
    ids=["no-action", "no-minutes", "neither-declared"],
)
def test_a_band_nothing_declares_an_action_or_an_effort_for_refuses(
    actions: dict[str, str],
    minutes: dict[str, float],
    expected: tuple[str, ...],
    absent: tuple[str, ...],
) -> None:
    frame = _scored(
        [
            _row("a1", "B", 10, 0),
            _row("a2", "B", 20, 1),
            _row("a3", "B", 30, 0),
            _row("a4", "B", 40, 0),
        ]
    )
    rows, refused = band_definition_rows(frame, actions=actions, review_minutes=minutes)

    # The same four accounts with both figures declared land 1/4 = 0.25 (see the success test);
    # here there is no row to land at all, because action and review_minutes are NOT NULL and the
    # only honest source is the config the run consumed.
    assert rows == [], f"nothing was declared, so nothing may be written: {rows}"
    assert len(refused) == 1, refused
    assert refused[0].startswith("band B: "), refused
    for phrase in expected:
        assert phrase in refused[0], f"the refusal has to name the missing declaration: {refused}"
    for phrase in absent:
        assert phrase not in refused[0], (
            f"{phrase!r} is declared in this case; a refusal that blames the wrong config key sends "
            f"the reviewer to the wrong file: {refused}"
        )


@pytest.mark.parametrize("raw", ["F", "AB", " ", None], ids=["F", "two-letters", "blank", "null"])
def test_a_band_letter_the_check_constraint_rejects_refuses_naming_the_constraint(raw: Any) -> None:
    frame = _scored([_row("a1", "A", 2, 0), _row("a2", raw, 40, 1)])
    rows, refused = band_definition_rows(frame, actions=ACTIONS, review_minutes=MINUTES)

    # ``band_definition.band`` is CHAR(1) with ck_band_definition_band IN ('A','B','C','D','E'), so a
    # sixth letter — or a two-letter code, a blank, or no band at all — has no row it could hold.
    assert (
        len(rows) == 1 and rows[0]["band"] == "A"
    ), f"one bad letter must not empty the table: {rows}"
    assert len(refused) == 1, refused
    line = refused[0]
    assert "not one of A-E" in line and "ck_band_definition_band" in line, line
    assert (
        repr(raw) in line
    ), f"the refusal has to echo the value the artifact actually carried: {line}"
    assert sum(row["n"] for row in rows) == 1, "the refused account is counted nowhere, not moved"


# --- band_definition: the grain, which is the account and not the account-instant


def test_an_account_scored_in_two_folds_enters_the_band_once_at_its_latest_pass() -> None:
    frame = _scored(
        [
            _row("a1", "B", 10, 0, as_of=AS_OF_Q1),
            _row("a1", "C", 40, 1, as_of=AS_OF_Q3),
            _row("a1", "C", 60, 1, fold=1, as_of=AS_OF_Q4),
            _row("a2", "C", 20, 0, fold=1, as_of=AS_OF_Q4),
        ]
    )
    rows, refused = band_definition_rows(frame, actions=ACTIONS, review_minutes=MINUTES)

    # Three rows carry a1 and one carries a2; only two accounts exist. The surviving rows are
    # a1's fold-1 October pass (60 pts, positive) and a2's (20 pts, clean):
    #   band C = {60 bad, 20 clean} -> n 2, positives 1 -> 1/2 = 0.5, range min 20 - max 60.
    # Uncollapsed, this frame would report band B n 1 rate 0.0 and band C n 3 rate 2/3 = 0.6667,
    # i.e. a population the queue cannot page through.
    assert refused == [], refused
    assert rows == [
        {
            "band": "C",
            "lower_points": 20,
            "upper_points": 60,
            "observed_rate": 0.5,
            "n": 2,
            "action": "review",
            "review_minutes": 45.0,
        }
    ], rows
    assert not any(
        row["band"] == "B" for row in rows
    ), "band B only exists in the pass a1 has already moved on from"


def test_the_later_fold_wins_even_when_its_as_of_stamp_is_the_earlier_one() -> None:
    frame = _scored(
        [_row("a1", "B", 10, 0, fold=1, as_of=AS_OF_Q1), _row("a1", "E", 90, 1, as_of=AS_OF_Q4)]
    )
    rows, refused = band_definition_rows(frame, actions=ACTIONS, review_minutes=MINUTES)

    # ``_current_score_per_account`` sorts [account_key, fold, as_of_ts, row] (landing.py:253-255), so
    # the fold number is the primary key and the stamp only breaks ties inside a fold. A walk-forward
    # run always stamps later folds later, so this agrees with "the latest as-of" in production — and
    # disagrees with it here, which is why it is pinned rather than assumed.
    assert refused == [], refused
    assert rows == [
        {
            "band": "B",
            "lower_points": 10,
            "upper_points": 10,
            "observed_rate": 0.0,
            "n": 1,
            "action": "review",
            "review_minutes": 15.0,
        }
    ], rows


# --- scorecard_bin: the order is declared here because the artifact does not carry it


def test_bins_come_out_by_attribute_then_descending_woe_with_the_label_breaking_ties() -> None:
    rows, refused = scorecard_bin_rows(_scored(_bin_rows()))

    # Hand-counted over the four test rows (a5 is train, so it contributes nothing):
    #   night_share, 4 contributions -> "[0.5,1.0)" = a1(bad)+a2   : n 2, share 2/4 = 0.5, bad 1/2 = 0.5
    #                                  "<0.5"        = a3          : n 1, share 1/4 = 0.25, bad 0/1 = 0.0
    #                                  "missing"     = a4          : n 1, share 0.25,        bad 0.0
    #   velocity,    4 contributions -> ">=100"       = a1(bad)+a2  : n 2, share 0.5,         bad 0.5
    #                                  "[10,100)"    = a3+a4       : n 2, share 0.5,         bad 0/2 = 0.0
    # Order is (attribute ascending, bin_index), and bin_index is WOE descending, ties by label
    # ascending: night_share's two 0.0 bins keep "<0.5" first because "<" (0x3C) sorts before
    # "m" (0x6D).
    assert refused == [], f"nothing here disagrees across folds: {refused}"
    assert rows == pytest.approx(
        [
            {
                "attribute": "night_share",
                "bin_index": 0,
                "label": "[0.5,1.0)",
                "woe": 0.12,
                "points": 12,
                "population_share": 0.5,
                "bad_rate": 0.5,
                "n": 2,
            },
            {
                "attribute": "night_share",
                "bin_index": 1,
                "label": "<0.5",
                "woe": 0.0,
                "points": 0,
                "population_share": 0.25,
                "bad_rate": 0.0,
                "n": 1,
            },
            {
                "attribute": "night_share",
                "bin_index": 2,
                "label": "missing",
                "woe": 0.0,
                "points": 0,
                "population_share": 0.25,
                "bad_rate": 0.0,
                "n": 1,
            },
            {
                "attribute": "velocity",
                "bin_index": 0,
                "label": ">=100",
                "woe": 0.3,
                "points": 30,
                "population_share": 0.5,
                "bad_rate": 0.5,
                "n": 2,
            },
            {
                "attribute": "velocity",
                "bin_index": 1,
                "label": "[10,100)",
                "woe": 0.05,
                "points": 5,
                "population_share": 0.5,
                "bad_rate": 0.0,
                "n": 2,
            },
        ]
    )
    assert [(row["attribute"], row["bin_index"]) for row in rows] == [
        ("night_share", 0),
        ("night_share", 1),
        ("night_share", 2),
        ("velocity", 0),
        ("velocity", 1),
    ], "uq_scorecard_bin is (run_id, attribute, bin_index); a gap or a repeat dies on insert"
    for attribute in ("night_share", "velocity"):
        own = [row for row in rows if row["attribute"] == attribute]
        assert [row["woe"] for row in own] == sorted(
            (row["woe"] for row in own), reverse=True
        ), f"{attribute} bins are not WOE-descending: {own}"
        assert [row["bin_index"] for row in own] == list(range(len(own))), (
            f"{attribute} indexes must be 0..n-1 with no hole, or the counterfactual range lookup "
            "misses a bin"
        )
    assert sum(row["n"] for row in rows if row["attribute"] == "velocity") == 4
    assert sum(
        row["population_share"] for row in rows if row["attribute"] == "velocity"
    ) == pytest.approx(
        1.0
    ), "the shares of one attribute cover the population exactly once, or the page lies about it"


def test_an_attribute_the_folds_disagree_on_refuses_whole_and_names_both_folds() -> None:
    rows, refused = scorecard_bin_rows(_scored(_conflict_rows()))

    # night_share "[0.5,1.0)" is 0.12/12 in fold 0 (a1) and 0.25/25 in fold 1 (a3). One row of the
    # table could not say which fold's weight it holds, so the attribute loses every one of its bins.
    # velocity agrees everywhere: 2 contributions, both in ">=100" -> share 2/2 = 1.0, bad 1/2 = 0.5.
    assert len(refused) == 1, refused
    line = refused[0]
    assert line.startswith("attribute night_share:") and "1 bin(s) disagree across folds" in line
    assert "across fold(s) [0, 1]" in line, f"the refusal has to name both folds: {line}"
    assert (
        "first seen 0.12/12" in line
    ), f"the first pair is the one a reader can diff against: {line}"
    assert rows == [
        pytest.approx(
            {
                "attribute": "velocity",
                "bin_index": 0,
                "label": ">=100",
                "woe": 0.3,
                "points": 30,
                "population_share": 1.0,
                "bad_rate": 0.5,
                "n": 2,
            }
        )
    ], rows
    assert not any(
        row["attribute"] == "night_share" for row in rows
    ), "a partial bin table would show population_share summing under one with nothing saying why"


# --- scorecard_point: whole account or nothing


def test_an_account_missing_any_attribute_lands_no_rows_at_all() -> None:
    rows, refused = scorecard_point_rows(_scored(_conflict_rows()))

    # a1 = velocity + night_share, a2 = velocity, a3 = night_share only. night_share has no bin table
    # (refused above), so a1 and a3 each have an attribute with nowhere to land and are dropped whole;
    # a2 keeps its single contribution, joined to the bin it sits in: share 2/2 = 1.0, bad 1/2 = 0.5.
    assert rows == [
        {
            "account_key": "a2",
            "attribute": "velocity",
            "bin_label": ">=100",
            "points": 30,
            "woe": 0.3,
            "reason_code": "velocity",
            "population_share": 1.0,
            "bad_rate": 0.5,
        }
    ], rows
    assert not any(row["account_key"] in {"a1", "a3"} for row in rows), (
        "a partial attribute list is a smaller summed score, which the case rail reads as a safer "
        "account in a safer band"
    )
    assert len(refused) == 2, refused
    assert refused[-1].startswith("scorecard_point: 2 account(s) refused"), refused
    assert "1 attribute(s) refused above" in refused[-1], refused
    assert "lands whole or not at all" in refused[-1], refused


def test_a_row_that_names_one_attribute_twice_refuses_as_a_unit() -> None:
    velocity_hi = _entry("velocity", ">=100", 30, 0.3)
    frame = _scored(
        [
            _row("a1", "B", 42, 1, entries=[velocity_hi, _entry("velocity", "[10,100)", 5, 0.05)]),
            _row("a2", "B", 30, 0, entries=[velocity_hi]),
        ]
    )
    rows, refused = scorecard_point_rows(frame)

    # uq_scorecard_point is (run_id, account_key, attribute): a1's two velocity entries are the
    # duplicate-key crash DEV-026 found at the ledger, arriving here instead. The row is refused as a
    # unit, so only velocity's single surviving contribution (a2) is counted: share 1/1 = 1.0,
    # bad 0/1 = 0.0.
    assert [row["account_key"] for row in rows] == ["a2"], rows
    assert len(refused) == 1, refused
    assert "account a1" in refused[0], refused
    assert "attribute velocity appears twice in one row" in refused[0], refused


def test_an_unlabelled_account_refuses_its_own_contributions_before_any_bin_is_counted() -> None:
    velocity_hi = _entry("velocity", ">=100", 30, 0.3)
    frame = _scored(
        [
            _row("a1", "B", 42, None, entries=[velocity_hi]),
            _row("a2", "B", 30, 0, entries=[velocity_hi]),
        ]
    )
    rows, refused = scorecard_point_rows(frame)

    # a1 has no label, so counting it inside a bin would put an unmeasured row into a bad-rate
    # denominator. It is refused by account, and a2's bin keeps n 1 -> share 1/1 = 1.0, bad 0/1 = 0.0.
    assert [row["account_key"] for row in rows] == ["a2"], rows
    assert len(refused) == 1, refused
    assert "account a1" in refused[0] and "label_is_fraud=None" in refused[0], refused
    assert "unlabelled row in a bad rate" in refused[0], refused


def test_a_contribution_the_artifact_cannot_state_drops_that_one_attribute_only() -> None:
    velocity_hi = _entry("velocity", ">=100", 30, 0.3)
    frame = _scored(
        [
            _row(
                "a1",
                "B",
                42,
                1,
                entries=[velocity_hi, _entry("night_share", "[0.5,1.0)", 1.5, 0.12)],
            ),
            _row(
                "a2",
                "B",
                30,
                0,
                entries=[velocity_hi, _entry("night_share", "[0.5,1.0)", 12, 0.12)],
            ),
        ]
    )
    rows, refused = scorecard_point_rows(frame)

    # a1's night_share points arrive as 1.5, and a fractional point is not the integer the scorecard
    # issues, so that one entry is dropped and counted per attribute (a refusal printed per account
    # would be noise at 43k accounts).
    assert any(
        line.startswith("attribute night_share: 1 contribution(s) refused") for line in refused
    ), refused
    assert any("points=1.5/woe=0.12 is not a number" in line for line in refused), refused
    assert any("score.scorecard_points" in line for line in refused), refused
    per_account = {
        key: [row["attribute"] for row in rows if row["account_key"] == key] for key in ("a1", "a2")
    }
    assert per_account == {"a1": [], "a2": ["night_share", "velocity"]}, (
        "a1 was scored on two attributes and one arrived unreadable, so a1 lands NOTHING: the "
        "case page sums these rows against band_definition's point ranges, and a one-attribute "
        "list reads as a safer account than the two-attribute truth. a2 keeps both, because the "
        "loss is a1's row, not the run's attribute."
    )
    assert any(
        line.startswith("scorecard_point: 1 account(s) refused") for line in refused
    ), f"the short list must be counted, not just the dropped entry: {refused}"
    assert any("1 account(s) affected" in line for line in refused), refused
    bins, _bin_refused = scorecard_bin_rows(frame)
    assert [(row["attribute"], row["n"], row["population_share"]) for row in bins] == [
        ("night_share", 1, 1.0),
        ("velocity", 2, 1.0),
    ], (
        "the dropped contribution is missing from the bin's numerator AND its denominator, so "
        "night_share claims to cover the whole population from one account"
    )


LONG_ATTRIBUTE: Final = "night_share_velocity_" + "x" * 49  # 21 + 49 = 70 characters


def test_an_attribute_wider_than_the_reason_code_column_refuses_the_account() -> None:
    """`reason_code` copies `attribute` into the narrower of the two columns.

    A 70-character feature key fits ``attribute`` (128), so the only check that can catch it is
    here: truncating the code would mint a reason the scorecard never issued, and letting it
    through would move the failure to a Postgres insert error that names neither the account nor
    the column. The account is refused whole, and the bin table still lands the attribute — bins
    carry no code.
    """
    frame = _scored(
        [
            _row("a1", "C", 55, 1, entries=[_entry(LONG_ATTRIBUTE, ">=1", 7, 0.2)]),
            _row("a2", "C", 12, 0, entries=[_entry("velocity", ">=1", 7, 0.2)]),
        ]
    )
    assert len(LONG_ATTRIBUTE) == 70

    rows, refused = scorecard_point_rows(frame)
    assert [row["account_key"] for row in rows] == ["a2"], rows
    assert not any(row["attribute"] == LONG_ATTRIBUTE for row in rows), rows
    assert any(LONG_ATTRIBUTE in line and "reason_code is 64" in line for line in refused), refused
    assert "cut mid-word" in " ".join(refused), refused
    assert "70 characters" in " ".join(refused), refused

    bins, bin_refusals = scorecard_bin_rows(frame)
    assert [row["attribute"] for row in bins] == [LONG_ATTRIBUTE, "velocity"], bins
    assert bin_refusals == [], bin_refusals


# --- LandingError: a frame that cannot be counted is an error, not an empty table


@pytest.mark.parametrize("column", ["band", "score_points", "label_is_fraud", "account_key"])
def test_a_scored_frame_missing_a_column_the_band_map_names_raises(column: str) -> None:
    frame = _scored(_bin_rows()).drop(column)

    # BAND_SOURCES reads band, score_points (twice: min and max) and label_is_fraud and account_key.
    # A missing one is a renamed producer column, and an empty band table would render as "no bands".
    with pytest.raises(LandingError) as excinfo:
        band_definition_rows(frame, actions=ACTIONS, review_minutes=MINUTES)
    message = str(excinfo.value)
    assert repr(column) in message, message
    assert "band table cannot be counted" in message, message


@pytest.mark.parametrize("dropped", [["fold"], ["as_of_ts"], ["fold", "as_of_ts"]])
def test_a_frame_that_cannot_say_which_pass_is_current_raises_for_all_three_tables(
    dropped: list[str],
) -> None:
    frame = _scored(_bin_rows()).drop(dropped)

    # `score` is UNIQUE (run_id, account_key) and carries no fold column, so the collapse needs
    # fold + as_of_ts. Without them one account would land as several current scores — DEV-026's grain
    # finding at the warehouse boundary — and no builder here may guess instead.
    for call in (
        lambda: band_definition_rows(frame, actions=ACTIONS, review_minutes=MINUTES),
        lambda: scorecard_bin_rows(frame),
        lambda: scorecard_point_rows(frame),
    ):
        with pytest.raises(LandingError) as excinfo:
            call()
        message = str(excinfo.value)
        assert str(dropped) in message, f"the refusal has to name the missing columns: {message}"
        assert "column(s)" in message and "cannot be resolved" in message, message


@pytest.mark.parametrize(
    ("column", "phrase"),
    [
        ("points_json", "no attribute contribution exists to land"),
        ("label_is_fraud", "bad rate cannot be counted"),
    ],
    ids=["no-payload", "no-labels"],
)
def test_a_scored_frame_missing_a_column_the_scorecard_needs_raises(
    column: str, phrase: str
) -> None:
    frame = _scored(_bin_rows()).drop(column)

    for builder in (scorecard_bin_rows, scorecard_point_rows):
        with pytest.raises(LandingError) as excinfo:
            builder(frame)
        message = str(excinfo.value)
        assert column in message, message
        assert phrase in message, message


def test_a_run_with_no_out_of_sample_rows_refuses_rather_than_landing_blank_tables() -> None:
    frame = _scored([_row("a1", "E", 99, 1, role="train")])

    # `score_rows` has always raised on exactly this input (landing.py:275-276); these three now
    # refuse the same way instead of returning two empty lists, because a green warehouse stage
    # beside a blank band table, a blank scorecard and a blank case rail cannot be told apart from
    # a run that legitimately had no accounts at all.
    for builder, table in (
        (
            lambda: band_definition_rows(frame, actions=ACTIONS, review_minutes=MINUTES),
            "band_definition",
        ),
        (lambda: scorecard_bin_rows(frame), "scorecard_bin"),
        (lambda: scorecard_point_rows(frame), "scorecard_point"),
    ):
        with pytest.raises(LandingError) as caught:
            builder()
        message = str(caught.value)
        assert table in message, message
        assert "out-of-sample" in message and "'test'" in message, message
        assert "out/score/<run>/scored_rows.parquet" in message, message
