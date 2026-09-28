"""The warehouse landing shapes rows the API can read — and labels the ones it cannot measure.

Nothing in this repository had ever written the `score` or `account` handoff tables, in either
sink, so the live read model had no rows to serve. These tests cover the two halves of the rule
that matters: a value that exists is landed with the table's own column names, and a value that
was never measured is landed as *not measured*, with the producer's reason beside it.

The second half changed shape in revision 0003, and the change is the point of this file. A zero
in `calibration_n` used to be refused because it would have turned "we could not measure this"
into "we measured nothing" — right reasoning, wrong remedy, because refusing the whole row hid
every scored account behind an empty queue. `ck_score_calibration_pairing` now makes the pairing
the invariant: a calibrated row carries its rate, its band and a positive n with no note, an
uncalibrated row carries none of them and a note, and neither can be half-populated. What this
file still refuses, and what the tests below still prove, is a frame that contradicts itself —
a flag that disagrees with the population it reports, where the loader cannot tell which of the
two is the lie.

One test reads the landed run from disk when it exists, because the interesting fact about the
40k slice is that every row there is uncalibrated: `band_n` is 0 across it, since calibration was
refused for lack of positives (DEV-024).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import polars as pl
import pytest

from oxbow.adapters.warehouse.landing import (
    ACCOUNT_COLUMNS,
    SCORE_COLUMNS,
    LandingError,
    account_rows,
    rule_hit_rows,
    score_rows,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
T0: Final = datetime(2015, 3, 1, 12, 0, tzinfo=UTC)
DAY: Final = timedelta(days=1)


def _events() -> pl.DataFrame:
    """Three accounts, four events: A pays B and C twice, B pays C.

    Hand-computed, not derived from the code under test (§19 rule 6):

    ======  ==========  ==========  ==========  ==============  ==============  =========
    acct    txn_count   n_outbound  n_inbound   n_counterparties  funding_minor   age_days
    ======  ==========  ==========  ==========  ==============  ==============  =========
    A           3           3           0            2 (B,C)         0              3
    B           2           1           1            2 (A,C)       500_000          1
    C           3           0           3            2 (A,B)     1_400_000          2
    ======  ==========  ==========  ==========  ==============  ==============  =========
    """
    return pl.DataFrame(
        {
            "account_from": ["A", "A", "B", "A"],
            "account_to": ["B", "C", "C", "C"],
            "amount_minor": [500_000, 400_000, 500_000, 500_000],
            "event_ts_utc": [T0, T0 + 2 * DAY, T0 + DAY, T0 + 3 * DAY],
            "currency": ["UGX"] * 4,
            "source_dataset": ["paysim"] * 4,
        }
    )


def test_account_rows_are_a_measurement_not_a_judgement() -> None:
    rows = {(row["account_key"]): row for row in account_rows(_events())}

    assert set(rows) == {"A", "B", "C"}
    # A sends on all three of its events and receives none.
    assert rows["A"]["txn_count"] == 3
    assert rows["A"]["n_outbound"] == 3 and rows["A"]["n_inbound"] == 0
    assert rows["A"]["n_counterparties"] == 2
    assert rows["A"]["funding_minor"] == 0, "funding is money received, not money moved"
    assert rows["A"]["age_days"] == 3, "first seen T0, last seen T0+3d"

    assert rows["B"]["txn_count"] == 2
    assert rows["B"]["n_outbound"] == 1 and rows["B"]["n_inbound"] == 1
    assert rows["B"]["n_counterparties"] == 2, "paid by A, paid into C"
    assert rows["B"]["funding_minor"] == 500_000, "B received one 5000.00 UGX transfer from A"
    assert rows["B"]["age_days"] == 1

    assert rows["C"]["txn_count"] == 3
    assert rows["C"]["n_inbound"] == 3 and rows["C"]["n_outbound"] == 0
    assert rows["C"]["n_counterparties"] == 2
    assert rows["C"]["funding_minor"] == 1_400_000, "4000.00 + 5000.00 from A and 5000.00 from B"
    assert rows["C"]["first_seen_at"].replace(tzinfo=UTC) == T0 + DAY, "B pays C at T0+1d"
    assert rows["C"]["last_seen_at"].replace(tzinfo=UTC) == T0 + 3 * DAY
    assert rows["C"]["age_days"] == 2, "first seen day 1, last seen day 3"
    assert all(set(row) == set(ACCOUNT_COLUMNS) for row in rows.values())


def test_a_direction_column_missing_from_the_frame_is_refused() -> None:
    with pytest.raises(LandingError, match="account_from"):
        account_rows(pl.DataFrame({"amount_minor": [1]}))


def _scored(**overrides: Any) -> pl.DataFrame:
    """One calibrated test row, plus the columns the loader reads to decide anything else.

    `calibrated` and `uncalibrated_reason` are here because `models/run.py:annotate_rows`
    puts them on every real scored row: the flag is the fold's own decision about its
    calibration and the reason is its sentence about that decision. A fixture without them
    would test the loader against a frame the pipeline never emits, and the branch that
    arbitrates a flag against a measurement would go unexercised.
    """
    base: dict[str, Any] = {
        "account_key": "0123456789abcdef01234567",
        "role": "test",
        "fold": 0,
        "as_of_ts": T0,
        "band": "D",
        "p_fused": 0.625,
        "score_points": 712,
        "p_scorecard": 0.40,
        "p_gbm": 0.70,
        "anomaly_norm": 0.25,
        "calibrated": True,
        "band_observed_rate": 0.31,
        "band_n": 97,
        "confidence_label": "calibrated_band",
        "label_typology": "funnel",
        "reason_codes": ["weekend share in its top band: plus 12 points"],
        "model_version": "p4b-1",
        "rule_r4_cycle_severity": 3,
        "rule_r2_fan_in_severity": 0,
        "rule_r9_regime_shift_severity": 0,
        "uncalibrated_reason": None,
    }
    base.update(overrides)
    return pl.DataFrame([base])


# The fold's own sentence for a refused calibration, quoted from the landed 40k artifact
# (`out/score/01M3H8WG436R394NZT2GS1KG69/scored_rows.parquet`, the fold that measured 6
# validation positives — one of the three counts DEV-024 names). Copied as text rather than
# built from `UNCALIBRATED_UI_TEXT` because the loader lands whatever the frame carries and
# must not depend on the wording, and because a fixture that paraphrases the producer cannot
# catch the loader mangling it.
_UNCALIBRATED_REASON: Final = (
    "probabilities are uncalibrated: 6 validation positives is below "
    "calibration.min_positives_for_calibration=50; an observed rate measured on that few "
    "rows is noise, and Module C multiplies it by money. "
    "below_floor_action='refuse_and_label_uncalibrated': probabilities are labelled "
    "uncalibrated and no calibrated figure is emitted"
)


def _uncalibrated(**overrides: Any) -> pl.DataFrame:
    """The same row with calibration refused: no population, no rate, and the fold's reason."""
    base: dict[str, Any] = {
        "calibrated": False,
        "band_n": 0,
        "band_observed_rate": float("nan"),
        "confidence_label": "uncalibrated",
        "uncalibrated_reason": _UNCALIBRATED_REASON,
    }
    # Merged rather than splatted: a caller that overrides band_n itself used to hit
    # "got multiple values for keyword argument", so the two tests that vary the
    # population could not be written at all.
    base.update(overrides)
    return _scored(**base)


def test_a_calibrated_row_lands_with_the_tables_own_names() -> None:
    rows, refused = score_rows(_scored())

    assert refused == []
    assert len(rows) == 1
    row = rows[0]
    assert row["calibrated_probability"] == 0.625 and row["fused_score"] == 0.625
    assert row["scorecard_points"] == 712
    assert row["observed_rate"] == 0.31 and row["calibration_n"] == 97
    assert row["band"] == "D" and row["predicted_typology"] == "funnel"
    assert row["reason_codes"] == ["weekend share in its top band: plus 12 points"]
    # A measured row is its own kind, and it carries no refusal note: that is the first
    # half of `ck_score_calibration_pairing`, and a note arriving beside a rate would tell
    # the reader two contradictory things about the same number.
    assert row["calibration_kind"] == "calibrated_band"
    assert row["calibration_note"] is None
    # Only the rule with severity > 0 counts as fired, and R9's zero must not sneak in.
    assert row["rule_ids"] == ["R4"], row["rule_ids"]
    assert "band_observed_rate" not in row, "the frame's name leaked into the table's row"


def test_a_refused_calibration_lands_labelled_uncalibrated_and_says_why() -> None:
    """The 40k case — and the reason it is a labelled row now rather than a refusal.

    `band_n` 0 with a NaN rate means the fold measured no population. The old loader
    refused the row for it, so a run whose every fold sat below
    `min_positives_for_calibration` landed nothing at all and the analyst's queue came up
    empty — the empty queue that is indistinguishable from "no account was scored", which
    03 §A rule 2 forbids and 03 §H answers: "calibration is refused and the UI says
    probabilities are uncalibrated". The score is the real output of a real fold; the
    confidence figure was never measured, so the four measurement columns go empty
    *together* and the fold's own sentence travels with the row. What this does NOT do is
    lower the floor: the probability is still not written into a column that calls itself
    calibrated.
    """
    rows, refused = score_rows(_uncalibrated())

    assert refused == [], f"a refused calibration is a labelled state, not a dropped row: {refused}"
    assert len(rows) == 1
    row = rows[0]
    assert row["calibration_kind"] == "uncalibrated"
    assert row["calibration_note"] == _UNCALIBRATED_REASON
    # The account is still findable, still ranked, still in its band.
    assert row["fused_score"] == 0.625 and row["band"] == "D"
    assert row["scorecard_points"] == 712 and row["rule_ids"] == ["R4"]
    for measurement in (
        "calibrated_probability",
        "calibration_band",
        "observed_rate",
        "calibration_n",
    ):
        assert row[measurement] is None, (
            f"{measurement}={row[measurement]!r} on an uncalibrated row: a placeholder "
            "here is the zero that reads as a measurement"
        )
    # One column list for both kinds, so the sink inserts either without branching.
    assert set(row) == set(SCORE_COLUMNS)


def test_a_flag_that_disagrees_with_its_population_is_refused() -> None:
    """The contradiction has no arbiter, so the row is refused by name rather than guessed.

    Either the flag or the measurement is wrong, and the loader cannot tell which. Landing
    it as calibrated would store an unmeasured rate; landing it as uncalibrated would throw
    away a rate somebody measured. Both are inventions, and the refusal names both values
    so the run that produced them is diagnosable from the message alone.
    """
    rows, refused = score_rows(_uncalibrated(band_n=97, band_observed_rate=0.31))
    assert rows == [], "a rate the fold said it did not measure cannot be stored as measured"
    assert len(refused) == 1
    assert "calibrated=False" in refused[0] and "band_n=97" in refused[0]

    rows, refused = score_rows(_scored(band_n=0, band_observed_rate=float("nan")))
    assert rows == [], "a flag claiming calibration over an empty population cannot be kept"
    assert len(refused) == 1
    assert "calibrated=True" in refused[0] and "band_n=0" in refused[0]


def test_a_nan_rate_alongside_a_real_n_without_the_flag_is_still_a_refusal() -> None:
    """No `calibrated` column means a frame that predates the flag, so the measurement rules.

    `band_n=97` with a NaN rate is not a measurement over a population — one of the two is
    wrong and there is no flag left to arbitrate, so the row is refused for want of an
    arbiter rather than labelled either way.
    """
    frame = _scored(band_observed_rate=float("nan")).drop("calibrated")
    rows, refused = score_rows(frame)

    assert rows == [] and refused, "a NaN rate over a claimed population cannot be landed"
    assert "band_n=97" in refused[0] and "no `calibrated` flag" in refused[0], refused[0]


def test_a_band_outside_the_check_constraint_is_refused_not_clamped() -> None:
    """`ck_score_band` allows A-E; landing 'F' would fail at the database instead of here."""
    rows, refused = score_rows(_scored(band="F"))
    assert rows == [] and "band=" in refused[0]


def test_only_the_out_of_sample_role_is_considered() -> None:
    frames = pl.concat(
        [_scored(role="test"), _scored(role="train", account_key="0000000000000000000000ff")]
    )
    rows, _ = score_rows(frames, role="test")
    assert [row["account_key"] for row in rows] == ["0123456789abcdef01234567"]

    with pytest.raises(LandingError, match="role"):
        score_rows(frames.drop("role"))


def test_a_missing_model_column_fails_loudly_instead_of_landing_a_null() -> None:
    with pytest.raises(LandingError, match="cannot populate"):
        score_rows(_scored().drop("p_fused"))


def test_rule_hits_keep_the_rules_that_did_not_fire() -> None:
    rows = rule_hit_rows(_scored())
    by_rule = {row["rule_id"]: row for row in rows}

    assert set(by_rule) == {"R4", "R2", "R9"}
    assert by_rule["R4"]["fired"] is True
    assert (
        by_rule["R2"]["fired"] is False
    ), "a non-firing rule is dropped from the table, so the case rail cannot show the margin"
    assert by_rule["R2"]["detail"]["severity"] == 0.0


def test_an_account_scored_in_two_folds_collapses_to_one_hit_row() -> None:
    """`uq_rule_hit` is (run_id, account_key, rule_id); an account recurs across folds.

    The landing failed on its own duplicate key before this existed. The stored severity is
    the maximum across folds with the fold counts alongside, so "one fold said 3" and "four
    folds said 3" remain different claims rather than one row silently overwriting another.
    """
    two_folds = pl.concat(
        [
            _scored(fold=0, rule_r4_cycle_severity=1),
            _scored(fold=1, rule_r4_cycle_severity=7),
            _scored(fold=2, rule_r4_cycle_severity=3),
        ],
        how="vertical_relaxed",
    )
    rows = rule_hit_rows(two_folds)
    r4 = [row for row in rows if row["rule_id"] == "R4"]

    assert len(r4) == 1, f"one (account, rule) landed {len(r4)} rows — a duplicate-key crash"
    assert r4[0]["detail"]["severity"] == 7.0
    assert r4[0]["detail"]["folds"] == 3
    assert r4[0]["detail"]["folds_fired"] == 3
    assert r4[0]["fired"] is True

    none_fired = rule_hit_rows(
        pl.concat(
            [_scored(fold=0, rule_r4_cycle_severity=0), _scored(fold=1, rule_r4_cycle_severity=0)]
        )
    )
    r4_quiet = next(row for row in none_fired if row["rule_id"] == "R4")
    assert r4_quiet["fired"] is False and r4_quiet["detail"]["folds_fired"] == 0


@pytest.mark.skipif(
    not (REPO_ROOT / "out" / "score").is_dir(),
    reason="no landed score run (out/ is gitignored); run `uv run oxbow score` first",
)
def test_the_landed_40k_run_lands_every_row_and_labels_every_one_uncalibrated() -> None:
    """Not a unit test — the measured state of the real artifact, so the fix is not theory.

    This test asserted the opposite until revision 0003. It used to prove that the landed
    40k run *refused* every row, and the message said that making it refuse was the
    unblocking step for the queue, the packet and the demo snapshot. The refusal was the
    bug: 43,046 scored accounts reached an empty queue, which is indistinguishable from an
    account nobody scored. What is unblocked now is the landing; what is unchanged is the
    calibration floor itself — every row on this run still says "probabilities are
    uncalibrated", because every fold really did fall under
    `min_positives_for_calibration` (DEV-024), and no number in this test says otherwise.
    """
    runs = sorted((REPO_ROOT / "out" / "score").glob("*/scored_rows.parquet"))
    if not runs:
        pytest.skip("no scored_rows.parquet landed yet")
    scored = pl.read_parquet(runs[-1])
    rows, refused = score_rows(scored)

    assert rows, (
        "the landed 40k run produced no score rows at all, which is the empty-queue "
        "failure revision 0003 exists to end"
    )
    assert (
        refused == []
    ), f"every row on this run is scoreable; refusals mean a regression: {refused}"
    kinds = {row["calibration_kind"] for row in rows}
    assert kinds == {"uncalibrated"}, (
        f"the landed slice calibrated ({kinds}), which contradicts DEV-024's measured "
        "108 positives against a floor of 50 per fold"
    )
    assert all(
        row["calibration_note"] for row in rows
    ), "an uncalibrated row with no note is the unexplained gap the pairing forbids"
    assert all(row["observed_rate"] is None for row in rows)
    assert all(row["calibration_n"] is None for row in rows)
    assert all(row["fused_score"] is not None for row in rows), "a landed score, always measured"


def test_an_account_scored_in_two_folds_lands_one_current_score() -> None:
    """`score` is UNIQUE (run_id, account_key) with no fold column, so the table has one row.

    Hand-computed: the same account scored in fold 0 at band D / 712 points and again in fold 2
    (30 days later) at band C / 800 points. The later pass is the current score, so one row
    lands and it carries C and 800. A loader that emitted both would have the write rejected by
    the database — measured on the landed 40k run, where 43,720 test rows cover 43,046 accounts
    and 674 rows collide (DEV-026, at the warehouse boundary).
    """
    frames = pl.concat(
        [
            _scored(fold=0, as_of_ts=T0),
            _scored(fold=2, as_of_ts=T0 + 30 * DAY, band="C", score_points=800),
            _scored(account_key="ffffffffffffffffffff0000", fold=1, as_of_ts=T0 + DAY),
        ],
        # vertical_relaxed: the older pass carries a Null refusal reason and the current one
        # a String sentence; strict vertical concat refuses that union, and the pair this
        # test is about is exactly that pair.
        how="vertical_relaxed",
    )
    rows, refused = score_rows(frames)

    assert refused == [], f"every row here is calibrated: {refused}"
    assert len(rows) == 2, f"expected one current score per account, got {len(rows)}"
    by_account = {row["account_key"]: row for row in rows}
    assert len(by_account) == 2, "the two accounts must not collapse into one another"
    scored_twice = by_account["0123456789abcdef01234567"]
    assert scored_twice["band"] == "C" and scored_twice["scorecard_points"] == 800, (
        "the fold-0 reading is the older information; landing it would put a stale score in "
        "the queue and the reader would have no way to tell"
    )


def test_the_latest_pass_beats_an_earlier_one_regardless_of_row_order() -> None:
    """The rule names the latest scoring pass, not the last row the frame happened to hold."""
    frames = pl.concat(
        [
            _scored(fold=2, as_of_ts=T0 + 30 * DAY, band="C", score_points=800),
            _scored(fold=0, band="D"),
        ],
        how="vertical_relaxed",
    )
    rows, _refused = score_rows(frames)

    assert len(rows) == 1 and rows[0]["band"] == "C" and rows[0]["scorecard_points"] == 800


def test_an_account_whose_current_row_is_uncalibrated_is_not_rescued_by_an_older_fold() -> None:
    """Falling back to a stale calibrated score would be the silent substitution this file
    exists to refuse; the account states its current condition instead.

    The doctrine is unchanged from the revision that wrote it — an account's *current* score
    is the latest pass's, and nothing about an older fold may stand in for it. What changed
    is the shape of the answer: the current row used to be refused outright, which proved
    the point by deleting the account from the queue. It now lands, labelled uncalibrated,
    and still carries no number from the fold before it. If the loader reached backwards,
    `observed_rate` would be 0.31 over n=97 and the band would read D.
    """
    frames = pl.concat(
        [
            # vertical_relaxed: the older pass carries no refusal reason (Null) and the
            # current one carries the fold sentence (String); a strict vertical concat
            # refuses that union, and the row this test is about is exactly the pair.
            _scored(fold=0, as_of_ts=T0),
            _uncalibrated(fold=2, as_of_ts=T0 + 30 * DAY, band="C", score_points=800),
        ]
    )
    rows, refused = score_rows(frames)

    assert refused == [], f"the current row is scoreable; it is only unmeasured: {refused}"
    assert len(rows) == 1
    (row,) = rows
    assert (
        row["band"] == "C" and row["scorecard_points"] == 800
    ), "the latest pass is the current one"
    assert row["calibration_kind"] == "uncalibrated"
    assert (
        row["observed_rate"] is None and row["calibration_n"] is None
    ), "the older fold's measured rate is not this account's current confidence"
    assert row["calibration_note"] == _UNCALIBRATED_REASON


def test_a_scored_frame_without_a_fold_or_as_of_cannot_name_a_current_score() -> None:
    """Refuse rather than land one account as several 'current' scores and hope the reader notices."""
    frames = _scored().drop("as_of_ts")

    with pytest.raises(LandingError, match="as_of_ts"):
        score_rows(frames)
