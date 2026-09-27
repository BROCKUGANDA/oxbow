"""The warehouse landing shapes rows the API can read — and refuses the ones it cannot measure.

Nothing in this repository had ever written the `score` or `account` handoff tables, in either
sink, so the live read model had no rows to serve. These tests cover both halves of the rule that
matters: a value that exists is landed with the table's own column names, and a value that does
not is refused with a reason that names the missing measurement, rather than being written as
zero. A zero in `calibration_n` would survive the NOT NULL constraint and turn "we could not
measure this" into "we measured nothing", which the queue then ranks on.

One test reads the landed run from disk when it exists, because the interesting fact about the
40k slice is that it refuses: `band_n` is 0 for every row there, since calibration was refused
for lack of positives (DEV-024).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import polars as pl
import pytest

from oxbow.adapters.warehouse.landing import (
    ACCOUNT_COLUMNS,
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
    """One calibrated test row, plus the columns the loader reads to decide anything else."""
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
    # Only the rule with severity > 0 counts as fired, and R9's zero must not sneak in.
    assert row["rule_ids"] == ["R4"], row["rule_ids"]
    assert "band_observed_rate" not in row, "the frame's name leaked into the table's row"


def test_a_refused_calibration_lands_nothing_and_says_why() -> None:
    """The 40k case: `band_n` 0 means there is no population, so there is no rate to store."""
    rows, refused = score_rows(_scored(band_n=0, band_observed_rate=float("nan")))

    assert rows == [], "a zero written into calibration_n would be a measured absence of a number"
    assert len(refused) == 1
    assert "calibration_n" in refused[0] and "refused" in refused[0].lower()


def test_a_nan_rate_alongside_a_real_n_is_still_a_refusal() -> None:
    rows, refused = score_rows(_scored(band_observed_rate=float("nan"), band_n=97))
    assert rows == [] and "NaN" in refused[0]


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
    assert by_rule["R2"]["fired"] is False, (
        "a non-firing rule is dropped from the table, so the case rail cannot show the margin"
    )
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
        ]
    )
    rows = rule_hit_rows(two_folds)
    r4 = [row for row in rows if row["rule_id"] == "R4"]

    assert len(r4) == 1, f"one (account, rule) landed {len(r4)} rows — a duplicate-key crash"
    assert r4[0]["detail"]["severity"] == 7.0
    assert r4[0]["detail"]["folds"] == 3
    assert r4[0]["detail"]["folds_fired"] == 3
    assert r4[0]["fired"] is True

    none_fired = rule_hit_rows(
        pl.concat([_scored(fold=0, rule_r4_cycle_severity=0), _scored(fold=1, rule_r4_cycle_severity=0)])
    )
    r4_quiet = next(row for row in none_fired if row["rule_id"] == "R4")
    assert r4_quiet["fired"] is False and r4_quiet["detail"]["folds_fired"] == 0


@pytest.mark.skipif(
    not (REPO_ROOT / "out" / "score").is_dir(),
    reason="no landed score run (out/ is gitignored); run `uv run oxbow score` first",
)
def test_the_landed_40k_run_refuses_every_row_for_the_same_named_reason() -> None:
    """Not a unit test — the measured state of the real artifact, so the refusal is not theory."""
    runs = sorted((REPO_ROOT / "out" / "score").glob("*/scored_rows.parquet"))
    if not runs:
        pytest.skip("no scored_rows.parquet landed yet")
    scored = pl.read_parquet(runs[-1])
    rows, refused = score_rows(scored)

    assert rows == [], (
        "the landed run should refuse: calibration was refused on every fold (DEV-024), so "
        f"no row has a calibration population — yet {len(rows)} rows passed the loader"
    )
    assert refused, "no row and no refusal means the loader looked at nothing"
    assert "calibration_n" in refused[0]
