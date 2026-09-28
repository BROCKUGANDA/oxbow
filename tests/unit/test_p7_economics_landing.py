"""`economics` rows are priced from what the run measured, and nothing else.

`apps/api/schemas/catalog.py`'s ``AlertRow`` declares ``exposure`` and ``expected_value`` as
required, so the alert queue cannot render an account this table has no row for. These tests pin the
two halves of that job: the arithmetic is the §11 formula evaluated on integer minor units, and every
term is a column the scored frame recorded or a key ``config/economics.yaml`` declares.

Every expected figure below is computed by hand in the assertion's own comment, from the numbers the
shipped config declares, and none of it is read back out of the code under test (§19 rule 6: a
fixture whose expected value came from the code it tests proves nothing). The arithmetic being
checked against :func:`oxbow.quant.ev.price_account` is deliberate — this layer is supposed to call
it rather than restate it — but the *figure* is verified by the longhand product, so a change to the
formula, to the micro-ratio conversion, or to the configured price would have to break one of these
lines on purpose.

The refusals are the other half of the file, and they are the interesting half: an unknown that
becomes a zero is the failure that lies most convincingly, so an unpriced exposure term and an absent
inflow cap each have to arrive as a named refusal instead of as a number. DEV-031 is the one place
that rule had to be sharpened rather than relaxed, and the file pins both sides of it: a null
`downstream_outflow_24h_minor` beside a `graph_out_degree_30d` of 0 or null is a *measured empty set*
— no leg to sum, so the subject's own outflow is the whole of `E_i` and the row says so — while the
same null beside a degree above 0 is the unknown and still refuses, naming the degree. A column the
frame already carries separates the two, so the branch is a discriminator and not a coalesce.

The unsimulated Monte Carlo interval is the third case, and it moved the other way: it was a refusal
while `economics.mc_p05_minor`, `mc_p50_minor` and `mc_p95_minor` were NOT NULL, because the only
alternatives to refusing were a zero and the config's `runs: 10000`, either of which claims a result
this run never produced. Migration 0004 made the three columns nullable, so the absence is now a
stored fact and the account lands priced. What the file insists on instead is that the absence stay
*legible*: the quantiles are null together or not at all, `mc_runs` is the zero draws actually taken,
the row's `assumptions` name the producer that would fill them, and the money either way is the same
money — a distribution nobody sampled was never part of `EV_i`, so no figure here moves because one
went missing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import polars as pl
import pytest

from oxbow.adapters.warehouse.landing import (
    ECONOMICS_DOWNSTREAM_DEGREE_SOURCE,
    ECONOMICS_EXPOSURE_SOURCE,
    ECONOMICS_INFLOW_CAP_SOURCE,
    ECONOMICS_SUBJECT_OUTFLOW_SOURCE,
    ECONOMICS_UNMEASURED_COLUMNS,
    ECONOMICS_UNRUN_MC_DRAWS,
    LandingError,
    economics_rows,
)
from oxbow.adapters.warehouse.models import Base
from oxbow.ports.case_sink import MonteCarloInterval
from oxbow.ports.warehouse import assert_money_is_integer_minor
from oxbow.quant.economics import (
    AnalystAssumptions,
    CapacityAssumptions,
    CapacitySweep,
    Economics,
    ExposureAssumptions,
    FourEyesPolicy,
    MonteCarloAssumptions,
    RecoveryAssumptions,
    SolverAssumptions,
    TailRiskLevels,
    load_economics,
)
from oxbow.quant.ev import PricingError
from oxbow.quant.money import CurrencyMismatchError, Money

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
T0: Final = datetime(2015, 3, 1, 12, 0, tzinfo=UTC)

#: The account the single-account tests price. `economics.account_key` is CHAR(12).
ACCT: Final = "0123456789"
OTHER: Final = "aaaaaaaaaaaa"
THIRD: Final = "zzzzzzzzzzzz"

# --- the configured terms, written out rather than imported -----------------
# These six numbers are `config/economics.yaml`'s own, and `test_the_hand_stated_figures_are_the
# _shipped_file_declares` pins them against the loader so a config edit has to be mirrored here
# before any of the arithmetic below can still pass.
COST_PER_MINUTE: Final = 15_000
FRICTION_MINOR: Final = 2_500_000
RECOVERY_RATE: Final = 0.35
MIN_REVIEW_MINUTES: Final = 5
MINUTES_BY_BAND: Final = {"A": 5, "B": 12, "C": 25, "D": 60, "E": 120}


def _config(**overrides: Any) -> Economics:
    """`config/economics.yaml`'s declared values as the validated object `ev` prices against.

    Built here rather than loaded so a test can move one term (the review-minute floor, say) without
    editing a shipped configuration file to reach a branch.
    """
    kwargs: dict[str, Any] = {
        "source_path": REPO_ROOT / "config" / "economics.yaml",
        "currency": "UGX",
        "minor_units_per_major": 100,
        "recovery": RecoveryAssumptions(
            rate=RECOVERY_RATE,
            band=(0.20, 0.35, 0.50),
            lower_exclusive=0.0,
            upper_exclusive=1.0,
        ),
        "analyst": AnalystAssumptions(
            cost_per_hour_minor=900_000,
            cost_per_minute_minor=COST_PER_MINUTE,
            hours_per_period=40,
            min_review_minutes=MIN_REVIEW_MINUTES,
        ),
        "friction_cost": Money(FRICTION_MINOR, "UGX"),
        "review_minutes_by_alert_class": dict(MINUTES_BY_BAND),
        "exposure": ExposureAssumptions(window_hours=24, downstream_hops=1),
        "capacity": CapacityAssumptions(
            review_minutes_per_period=12_000,
            default_alerts_reviewed=200,
            sweep=CapacitySweep(min_minutes=0, max_minutes=30_000, points=31),
        ),
        "solver": SolverAssumptions(
            greedy_budget_ms=200,
            cpsat_deadline_ms=5_000,
            cpsat_workers=1,
            agreement_tolerance_ratio=0.02,
        ),
        "monte_carlo": MonteCarloAssumptions(
            runs=10_000, max_depth=4, seed=1337, lower_quantile=0.05, upper_quantile=0.95
        ),
        "four_eyes": FourEyesPolicy(threshold_exposure_minor=50_000_000),
        "tail_risk": TailRiskLevels(var_alpha=0.95, es_alpha=0.975),
        "seed": 1337,
        "order_columns": ("as_of_ts", "txn_id"),
    }
    kwargs.update(overrides)
    return Economics(**kwargs)


def _interval(account: str = ACCT, *, spread: int = 2_000_000) -> MonteCarloInterval:
    """A measured propagation interval, as `quant/monte_carlo` would hand one over.

    Stated as p50 = the exposure these tests price, with a symmetric bracket, so the three columns
    are visibly the caller's numbers and not this module's arithmetic.
    """
    return MonteCarloInterval(
        runs=10_000,
        seed=1337,
        p05_minor=10_000_000 - spread,
        p50_minor=10_000_000,
        p95_minor=10_000_000 + spread,
        interval=(0.05, 0.95),
    )


def _intervals(*accounts: str) -> dict[str, MonteCarloInterval]:
    return {account: _interval(account) for account in accounts or (ACCT,)}


def _scored(**overrides: Any) -> pl.DataFrame:
    """One calibrated test row, priced off a measured 10,000,000-minor exposure.

    The columns `score_rows` needs are here because the economics row's account set, band,
    probability and calibration label are read off *its* output rather than re-derived; the exposure
    pair is here because that is what §3.2 measures.

    The subject's own outflow and its 1-hop degree are here for the same reason DEV-031 put them in
    the frame: they are the two readings that separate a downstream null which is an empty set from a
    downstream null which is a gap. They are deliberately figures the default path does not use —
    6,000,000 of the subject's own against 10,000,000 measured downstream (the account plus the legs
    it pays, so the pair is coherent) — because a test that has to say *which* column priced is the
    test that catches a substitution.
    """
    base: dict[str, Any] = {
        "account_key": ACCT,
        "role": "test",
        "fold": 0,
        "as_of_ts": T0,
        "band": "A",
        "p_fused": 0.5,
        "score_points": 120,
        "p_scorecard": 0.40,
        "p_gbm": 0.60,
        "anomaly_norm": 0.25,
        "calibrated": True,
        "band_observed_rate": 0.31,
        "band_n": 97,
        "label_typology": "funnel",
        "reason_codes": ["overnight share in its top band: plus 12 points"],
        "model_version": "p4b-1",
        "uncalibrated_reason": None,
        ECONOMICS_EXPOSURE_SOURCE: 10_000_000,
        ECONOMICS_INFLOW_CAP_SOURCE: 20_000_000,
        ECONOMICS_SUBJECT_OUTFLOW_SOURCE: 6_000_000,
        ECONOMICS_DOWNSTREAM_DEGREE_SOURCE: 2,
    }
    base.update(overrides)
    return pl.DataFrame([base])


_UNCALIBRATED_REASON: Final = (
    "probabilities are uncalibrated: 6 positives in the validation split against "
    "min_positives_for_calibration=50"
)


def _uncalibrated(**overrides: Any) -> pl.DataFrame:
    """The landed 40k run's shape: the fold refused calibration, so there is no population."""
    merged: dict[str, Any] = {
        "calibrated": False,
        "band_n": 0,
        "band_observed_rate": float("nan"),
        "uncalibrated_reason": _UNCALIBRATED_REASON,
    }
    merged.update(overrides)
    return _scored(**merged)


def test_one_account_priced_end_to_end_and_checkable_by_hand() -> None:
    """EV = p*E*r - c - (1-p)*f, in integer minor units, for band A.

    p = 0.5, E = 10,000,000 minor (the cap does not bind: inflow 20,000,000), r = 0.35,
    m = 5 min (band A) so c = 5 x 15,000 = 75,000, f = 2,500,000.

        p*E*r = 0.5 * 10,000,000 * 0.35            =  1,750,000
        (1-p)*f = 0.5 * 2,500,000                  =  1,250,000
        EV      = 1,750,000 - 75,000 - 1,250,000   =    425,000
        density = 425,000 / 5 minutes              = 85,000.0 minor per analyst-minute
    """
    rows, refused = economics_rows(_scored(), config=_config(), intervals=_intervals(ACCT))

    assert refused == [], refused
    assert len(rows) == 1
    row = rows[0]
    assert row["account_key"] == ACCT
    assert row["currency"] == "UGX"
    assert row["exposure_minor"] == 10_000_000
    assert row["loss_avoided_minor"] == 1_750_000, "gross p*E*r, not netted of either cost"
    assert row["analyst_cost_minor"] == 75_000
    assert row["friction_cost_minor"] == 1_250_000
    assert row["expected_value_minor"] == 425_000
    assert row["analyst_minutes"] == 5
    assert row["recovery_rate"] == RECOVERY_RATE
    assert row["ev_density"] == 85_000.0
    # The interval is the caller's measurement, carried through without arithmetic.
    assert row["mc_p50_minor"] == 10_000_000
    assert row["mc_p05_minor"] == 8_000_000
    assert row["mc_p95_minor"] == 12_000_000
    assert row["mc_runs"] == 10_000 and row["mc_seed"] == 1337
    assert row["mc_interval"] == [0.05, 0.95]
    # Every money column is an integer, the way both sinks demand it.
    assert_money_is_integer_minor("economics", row)


def test_the_row_covers_every_column_the_table_declares_not_null() -> None:
    """`sink.write` inserts by column name, so a missing NOT NULL column is a failed landing.

    Checked against the ORM's own table rather than a list written here, because a column added to
    `models.py` has to break this line rather than break a run in Postgres at 3am.

    Run for BOTH interval shapes, because that is the whole point of migration 0004: the measured row
    and the row that stores an absence are each insertable as they stand, and neither needs a column
    filled in by something other than this mapper.
    """
    table = Base.metadata.tables["economics"]
    required = {name for name, column in table.columns.items() if not column.nullable}
    measured = economics_rows(_scored(), config=_config(), intervals=_intervals(ACCT))[0][0]
    absent = economics_rows(_scored(), config=_config())[0][0]

    for row in (measured, absent):
        # `run_id` is injected by the sink and `id` is generated by the database.
        assert required - {"id", "run_id"} <= set(
            row
        ), "the table gained a column this row cannot fill"
        for name in required - {"id", "run_id"}:
            assert row[name] is not None, f"{name} is NOT NULL and this row leaves it null"

    # The trio left the required set — that is the change — and nothing else did.
    assert required.isdisjoint(ECONOMICS_UNMEASURED_COLUMNS), "a quantile went back to NOT NULL"
    assert {"mc_runs", "mc_seed", "mc_interval"} <= required
    # Nothing lands into a column the table does not have: the sink inserts by name and Postgres
    # rejects the whole batch, which would roll back every other table in the run.
    assert set(absent) - {"ev_density"} <= set(
        table.columns.keys()
    ), "the row names a column not in DDL"


def test_the_quantiles_are_null_together_and_never_half_a_distribution() -> None:
    """The pairing `models.py` installs is a fact about the shape, so the mapper honours it first.

    A reader who finds `mc_p50_minor` null has to be able to conclude "no simulation", and that
    conclusion is only sound if the other two are null too. `ck_economics_mc_interval_pairing` rejects
    a half-populated row in the database; this pins the same rule on both shapes before it gets there.
    """
    absent = economics_rows(_scored(), config=_config())[0][0]
    measured = economics_rows(_scored(), config=_config(), intervals=_intervals(ACCT))[0][0]

    assert [absent[column] for column in ECONOMICS_UNMEASURED_COLUMNS] == [None, None, None]
    assert absent["mc_runs"] == ECONOMICS_UNRUN_MC_DRAWS
    assert all(measured[column] is not None for column in ECONOMICS_UNMEASURED_COLUMNS)
    assert measured["mc_runs"] >= 1, "a served distribution claims draws it did not take"


def test_the_inflow_cap_actually_binds() -> None:
    """E_i = min(outflow, inflow): 10,000,000 leaving against 4,000,000 received is 4,000,000.

    p*E*r = 0.5 * 4,000,000 * 0.35             =    700,000
    EV      = 700,000 - 75,000 - 1,250,000     =   -625,000
    """
    rows, refused = economics_rows(
        _scored(**{ECONOMICS_INFLOW_CAP_SOURCE: 4_000_000}),
        config=_config(),
        intervals=_intervals(ACCT),
    )

    assert refused == [], refused
    assert rows[0]["exposure_minor"] == 4_000_000, "the cap is part of E_i, not a footnote"
    assert rows[0]["loss_avoided_minor"] == 700_000
    assert rows[0]["expected_value_minor"] == -625_000, "a capped account can price below zero"
    assert rows[0]["assumptions"]["exposure_capped_by_inflow"] is True
    assert rows[0]["assumptions"]["exposure_before_cap_minor"] == 10_000_000


def test_a_null_downstream_with_a_zero_degree_lands_on_the_subjects_own_capped_outflow() -> None:
    """DEV-031's measured empty set: degree 0 is the account paying nobody, so nothing is missing.

    §3.2's exposure is the subject plus its 1-hop downstream; with no downstream leg the sum has one
    term, so E_i = min(`amount_out_24h_minor`, `amount_in_24h_minor`) = min(12,000,000, 20,000,000)
    = 12,000,000, and the cap does not bind. Band A is 5 minutes, so:

        p*E*r   = 0.5 * 12,000,000 * 0.35           =  2,100,000
        c       = 5 min x 15,000/minute             =     75,000
        (1-p)*f = 0.5 * 2,500,000                   =  1,250,000
        EV      = 2,100,000 - 75,000 - 1,250,000    =    775,000
        density = 775,000 / 5 minutes               = 155,000.0 minor per analyst-minute
    """
    rows, refused = economics_rows(
        _scored(
            **{
                ECONOMICS_EXPOSURE_SOURCE: None,
                ECONOMICS_DOWNSTREAM_DEGREE_SOURCE: 0,
                ECONOMICS_SUBJECT_OUTFLOW_SOURCE: 12_000_000,
            }
        ),
        config=_config(),
        intervals=_intervals(ACCT),
    )

    assert refused == [], refused
    assert len(rows) == 1, "the degree-proved empty set has to land or the queue stays unpriced"
    row = rows[0]
    assert row["exposure_minor"] == 12_000_000, "the subject's own outflow, uncapped at this inflow"
    assert row["loss_avoided_minor"] == 2_100_000
    assert row["analyst_cost_minor"] == 75_000
    assert row["friction_cost_minor"] == 1_250_000
    assert row["expected_value_minor"] == 775_000
    assert row["ev_density"] == 155_000.0
    assert_money_is_integer_minor("economics", row)


def test_a_null_downstream_with_a_null_degree_lands_the_same_way() -> None:
    """The landed 40k run's own shape: all 43,511 null-downstream test rows carry a null degree too.

    A recorded null and a recorded 0 are one branch because the frame records no outgoing edge either
    way (`config/features.yaml` states out-degree is undefined for a node that originates nothing).
    Same 12,000,000 out of 20,000,000 in as the test above, so the money is identical: EV 775,000.
    """
    rows, refused = economics_rows(
        _scored(
            **{
                ECONOMICS_EXPOSURE_SOURCE: None,
                ECONOMICS_DOWNSTREAM_DEGREE_SOURCE: None,
                ECONOMICS_SUBJECT_OUTFLOW_SOURCE: 12_000_000,
            }
        ),
        config=_config(),
        intervals=_intervals(ACCT),
    )

    assert refused == [], refused
    assert rows[0]["exposure_minor"] == 12_000_000
    assert rows[0]["expected_value_minor"] == 775_000
    assert rows[0]["assumptions"]["exposure_downstream_basis"].startswith(
        f"`{ECONOMICS_DOWNSTREAM_DEGREE_SOURCE}` is null"
    ), rows[0]["assumptions"]["exposure_downstream_basis"]


def test_the_inflow_cap_binds_on_the_degree_proved_branch_too() -> None:
    """The cap is part of E_i's definition on this branch exactly as it is on the measured one.

    12,000,000 leaving against 9,000,000 received is 9,000,000, and the pre-cap figure the row
    reports is the subject's own outflow — there was no downstream term to have added to it.

        p*E*r = 0.5 * 9,000,000 * 0.35              =  1,575,000
        EV      = 1,575,000 - 75,000 - 1,250,000     =    250,000
        density = 250,000 / 5 minutes               =   50,000.0
    """
    rows, refused = economics_rows(
        _scored(
            **{
                ECONOMICS_EXPOSURE_SOURCE: None,
                ECONOMICS_DOWNSTREAM_DEGREE_SOURCE: 0,
                ECONOMICS_SUBJECT_OUTFLOW_SOURCE: 12_000_000,
                ECONOMICS_INFLOW_CAP_SOURCE: 9_000_000,
            }
        ),
        config=_config(),
        intervals=_intervals(ACCT),
    )

    assert refused == [], refused
    assert rows[0]["exposure_minor"] == 9_000_000, "the cap is part of E_i, not a footnote"
    assert rows[0]["loss_avoided_minor"] == 1_575_000
    assert rows[0]["expected_value_minor"] == 250_000
    assert rows[0]["ev_density"] == 50_000.0
    assumptions = rows[0]["assumptions"]
    assert assumptions["exposure_capped_by_inflow"] is True
    assert assumptions["exposure_before_cap_minor"] == 12_000_000


def test_the_landed_row_says_the_downstream_set_was_empty_and_claims_no_downstream_figure() -> None:
    """The label is structural, not decorative: term, evidence and grain, or it means nothing.

    The same account as the degree-0 test above, so the money is unchanged at EV 775,000 — what is
    checked here is the sentence sitting beside it in ``assumptions``. It has to say the set was
    measured empty and never report an amount for the downstream term (that is the unknown-turned-into
    -a-zero DEV-031 rejects), and the row has to attribute E_i to the subject's own column rather than
    to a downstream measurement it does not have.
    """
    rows, _ = economics_rows(
        _scored(
            **{
                ECONOMICS_EXPOSURE_SOURCE: None,
                ECONOMICS_DOWNSTREAM_DEGREE_SOURCE: 0,
                ECONOMICS_SUBJECT_OUTFLOW_SOURCE: 12_000_000,
            }
        ),
        config=_config(),
        intervals=_intervals(ACCT),
    )
    assumptions = rows[0]["assumptions"]

    assert assumptions["exposure_is_partial"] is True
    basis = assumptions["exposure_downstream_basis"]
    assert "measured EMPTY" in basis, basis
    assert "no downstream amount was measured" in basis, basis
    assert ECONOMICS_DOWNSTREAM_DEGREE_SOURCE in basis, basis
    assert ECONOMICS_EXPOSURE_SOURCE in basis, basis
    # Worded as an empty set, never as a zero: the word would report an observation this row lacks.
    assert "zero" not in basis.lower(), basis
    # E_i is credited to the column it was actually read from, so the row cannot read as a cluster.
    assert assumptions["exposure_source_column"] == ECONOMICS_SUBJECT_OUTFLOW_SOURCE
    assert assumptions["exposure_before_cap_minor"] == 12_000_000
    assert (
        assumptions["exposure.downstream_hops"] == 1
    ), "the definition did not change; the set did"
    assert (
        rows[0]["expected_value_minor"] == 775_000
    ), "the label qualified the figure, not moved it"


def test_a_measured_downstream_term_prices_that_column_and_nothing_else() -> None:
    """DEV-031 changed what a null means, not what a number means: the measured path is untouched.

    The fixture measures 10,000,000 of downstream money for an account whose own outflow is 6,000,000
    and whose degree is 3, so only the §3.2 column can produce the figures below. E_i =
    min(10,000,000, 20,000,000) = 10,000,000:

        p*E*r = 0.5 * 10,000,000 * 0.35             =  1,750,000
        EV      = 1,750,000 - 75,000 - 1,250,000    =    425,000
        density = 425,000 / 5 minutes               =   85,000.0

    A row that measured its downstream legs carries no partial-exposure key at all: nothing about its
    E_i is partial, and stamping one would dilute the rows that genuinely need it.
    """
    rows, refused = economics_rows(
        _scored(**{ECONOMICS_DOWNSTREAM_DEGREE_SOURCE: 3}),
        config=_config(),
        intervals=_intervals(ACCT),
    )

    assert refused == [], refused
    row = rows[0]
    assert row["exposure_minor"] == 10_000_000, "the narrower subject-only 6,000,000 was not used"
    assert row["expected_value_minor"] == 425_000
    assert row["ev_density"] == 85_000.0
    assumptions = row["assumptions"]
    assert assumptions["exposure_source_column"] == ECONOMICS_EXPOSURE_SOURCE
    assert assumptions["exposure_before_cap_minor"] == 10_000_000
    assert "exposure_is_partial" not in assumptions, assumptions
    assert "exposure_downstream_basis" not in assumptions, assumptions

    # A downstream term measured AT zero is a measurement, not an empty set: the 209 rows this run
    # does have are priced at zero exposure and labelled as measured, so the branch keys on null and
    # never on "falsy". p*E*r = 0, EV = 0 - 75,000 - 1,250,000 = -1,325,000.
    measured, gaps = economics_rows(
        _scored(**{ECONOMICS_EXPOSURE_SOURCE: 0}), config=_config(), intervals=_intervals(ACCT)
    )
    assert gaps == [], gaps
    assert measured[0]["exposure_minor"] == 0
    assert measured[0]["expected_value_minor"] == -1_325_000
    assert "exposure_is_partial" not in measured[0]["assumptions"], measured[0]["assumptions"]


def test_a_null_downstream_with_a_positive_degree_is_refused_and_names_the_degree() -> None:
    """DEV-031's second reading, kept as code rather than as a comment.

    An account with 3 legs and no downstream measurement has an E_i this frame cannot know, so the
    refusal stands and now says which way the discriminator pointed: `graph_out_degree_30d=3`. Zero
    rows of this shape exist on the landed run, which is exactly why the guard stays — the day a fold
    covers an account and fails to price its downstream, that row must not land.
    """
    rows, refused = economics_rows(
        _scored(
            **{
                ECONOMICS_EXPOSURE_SOURCE: None,
                ECONOMICS_DOWNSTREAM_DEGREE_SOURCE: 3,
                ECONOMICS_SUBJECT_OUTFLOW_SOURCE: 12_000_000,
            }
        ),
        config=_config(),
        intervals=_intervals(ACCT),
    )

    assert rows == [], f"an unmeasured downstream with real legs landed anyway: {rows}"
    assert len(refused) == 1, refused
    gap = refused[0]
    assert ACCT in gap and ECONOMICS_EXPOSURE_SOURCE in gap, gap
    # The degree is named with its value, so this refusal is distinguishable from the empty-set one.
    assert f"{ECONOMICS_DOWNSTREAM_DEGREE_SOURCE}=3" in gap, gap
    assert "3 1-hop" in gap, gap
    # The narrower column that WOULD have filled the gap is still named as declined, not used.
    assert "not substituted" in gap and ECONOMICS_SUBJECT_OUTFLOW_SOURCE in gap, gap


def test_a_frame_without_the_degree_column_cannot_prove_an_empty_set_and_still_refuses() -> None:
    """Absence of the discriminator is not evidence of emptiness.

    A frame from a run whose feature set pre-dates `graph_out_degree_30d` records no fact about the
    account's legs, so the null has no reading and the row stays unpriced — the branch is proved by a
    column, not by the column being missing.
    """
    frame = _scored(
        **{ECONOMICS_EXPOSURE_SOURCE: None, ECONOMICS_SUBJECT_OUTFLOW_SOURCE: 12_000_000}
    ).drop(ECONOMICS_DOWNSTREAM_DEGREE_SOURCE)
    rows, refused = economics_rows(frame, config=_config(), intervals=_intervals(ACCT))

    assert rows == []
    assert ECONOMICS_DOWNSTREAM_DEGREE_SOURCE in refused[0], refused[0]
    assert "not a column of this frame" in refused[0], refused[0]


def test_a_missing_inflow_cap_refuses_the_row_because_the_cap_has_no_other_source() -> None:
    """An uncapped exposure is a bigger number than §3.2 licenses, so the row does not exist."""
    rows, refused = economics_rows(
        _scored(**{ECONOMICS_INFLOW_CAP_SOURCE: None}), config=_config(), intervals=_intervals(ACCT)
    )

    assert rows == []
    assert ECONOMICS_INFLOW_CAP_SOURCE in refused[0] and "definition" in refused[0], refused[0]


def test_a_negative_cluster_outflow_is_refused_rather_than_made_positive() -> None:
    rows, refused = economics_rows(
        _scored(**{ECONOMICS_EXPOSURE_SOURCE: -1}), config=_config(), intervals=_intervals(ACCT)
    )

    assert rows == [] and "negative" in refused[0], refused


def test_a_foreign_exposure_raises_instead_of_being_converted() -> None:
    """No FX rate is written down anywhere, so there is nothing to price a EUR leg against.

    `ev.price_account`'s raising arm is the right contract for a landing that committed to one
    currency: a per-row label would still leave a EUR exposure in a UGX-denominated total.
    """
    frame = _scored(currency="EUR")

    with pytest.raises(CurrencyMismatchError, match="EUR"):
        economics_rows(frame, config=_config(), intervals=_intervals(ACCT))

    # The same frame in the configured money prices, and the currency is taken from the frame.
    rows, _ = economics_rows(_scored(currency="UGX"), config=_config(), intervals=_intervals(ACCT))
    assert rows[0]["currency"] == "UGX"
    assert rows[0]["assumptions"]["currency_is_frame_measurement"] is True


def test_a_review_time_below_the_configured_floor_raises_rather_than_being_clamped() -> None:
    """m_i is the density denominator, so a sub-floor band cannot be rounded up to be tolerated.

    Reached by configuring it that way (band A at 3 minutes against a 5-minute floor) because the
    shipped file's loader refuses the combination at load, which is the point.
    """
    cheap = _config(
        review_minutes_by_alert_class={**MINUTES_BY_BAND, "A": 3},
        analyst=AnalystAssumptions(
            cost_per_hour_minor=900_000,
            cost_per_minute_minor=COST_PER_MINUTE,
            hours_per_period=40,
            min_review_minutes=MIN_REVIEW_MINUTES,
        ),
    )

    with pytest.raises(PricingError, match="below the floor"):
        economics_rows(_scored(band="A"), config=cheap, intervals=_intervals(ACCT))


def test_one_row_per_account_not_one_per_scored_row() -> None:
    """DEV-026's grain, at the money table: `economics` is UNIQUE (run_id, account_key).

    Fold 0 measured a 10,000,000 exposure and fold 2 (30 days later) measured 20,000,000. The current
    fold is the one priced, and it is priced once:

        p*E*r = 0.5 * 20,000,000 * 0.35            =  3,500,000
        EV      = 3,500,000 - 75,000 - 1,250,000   =  2,175,000
        density = 2,175,000 / 5                    =  435,000.0
    """
    later = T0.replace(year=2015, month=4, day=1)
    frames = pl.concat(
        [
            _scored(fold=0, as_of_ts=T0),
            _scored(fold=2, as_of_ts=later, **{ECONOMICS_EXPOSURE_SOURCE: 20_000_000}),
            _scored(account_key=OTHER, fold=1, as_of_ts=T0),
        ]
    )
    rows, refused = economics_rows(frames, config=_config(), intervals=_intervals(ACCT, OTHER))

    assert refused == [], refused
    assert len(rows) == 2, f"expected one priced row per account, got {len(rows)}"
    by_account = {row["account_key"]: row for row in rows}
    assert by_account[ACCT]["exposure_minor"] == 20_000_000, "the older fold is not current"
    assert by_account[ACCT]["expected_value_minor"] == 2_175_000
    assert by_account[ACCT]["ev_density"] == 435_000.0
    # The account scored once is priced from its one measurement, unchanged.
    assert by_account[OTHER]["exposure_minor"] == 10_000_000


def test_the_landed_order_is_the_allocator_order_and_not_the_input_order() -> None:
    """(-density, account_key): the same key `positive_ev_rows` and `price_exposures` rank on.

    Hand-computed at m = 5 minutes (band A), c = 75,000, friction = 1,250,000:

        p=0.5, E=10,000,000 -> EV 425,000  -> density 85,000.0
        p=0.5, E=30,000,000 -> 5,250,000 - 75,000 - 1,250,000 = 3,925,000 -> 785,000.0
        p=0.2, E=10,000,000 -> 700,000 - 75,000 - 2,000,000 = -1,375,000 -> -275,000.0

    The middle account gets a matching 40,000,000 inflow so the cap does not bind it: at the
    fixture's default 20,000,000 E becomes 20,000,000, which is the previous test's subject.
    """
    frames = pl.concat(
        [
            _scored(account_key=ACCT, fold=0, p_fused=0.2),
            _scored(
                account_key=OTHER,
                fold=0,
                **{ECONOMICS_EXPOSURE_SOURCE: 30_000_000, ECONOMICS_INFLOW_CAP_SOURCE: 40_000_000},
            ),
            _scored(account_key=THIRD, fold=0),
        ]
    )
    forward, _ = economics_rows(frames, config=_config(), intervals=_intervals(ACCT, OTHER, THIRD))
    reversed_, _ = economics_rows(
        frames[::-1], config=_config(), intervals=_intervals(ACCT, OTHER, THIRD)
    )

    assert [row["account_key"] for row in forward] == [OTHER, THIRD, ACCT]
    assert forward == reversed_, "row order leaked the frame's own order"
    assert [row["ev_density"] for row in forward] == [785_000.0, 85_000.0, -275_000.0]


def test_an_uncalibrated_account_is_priced_and_cannot_escape_its_label() -> None:
    """Option (a), and the repo already chose it on the read side (DEV-024).

    The landed 40k run is 100 % uncalibrated, so refusing here would leave the queue unrankable —
    the empty screen from the other direction. So the row prices off `p_fused` and carries the
    pipeline's own words for what that probability is not. Same figures as the first test:
    EV 425,000, density 85,000.0.
    """
    rows, refused = economics_rows(_uncalibrated(), config=_config(), intervals=_intervals(ACCT))

    assert refused == [], f"a refused calibration is a labelled state, not a dropped row: {refused}"
    assert len(rows) == 1
    row = rows[0]
    assert row["expected_value_minor"] == 425_000
    assumptions = row["assumptions"]
    assert assumptions["calibration_kind"] == "uncalibrated"
    assert assumptions["probability_is_uncalibrated"] is True
    assert assumptions["probability_source_column"] == "p_fused"
    label = assumptions["confidence_label"]
    assert "probabilities are uncalibrated" in label, label
    # The label states the absence rather than dressing it up: no rate, no population.
    assert "observed rate in this band" not in label, label
    assert assumptions["disclaimer"], "money left the row without its assumption line"


def test_a_calibrated_account_is_priced_from_the_same_number_and_says_it_was_measured() -> None:
    rows, _ = economics_rows(_scored(), config=_config(), intervals=_intervals(ACCT))

    assumptions = rows[0]["assumptions"]
    assert assumptions["calibration_kind"] == "calibrated_band"
    assert assumptions["probability_is_uncalibrated"] is False
    assert "observed rate in this band: 31%, n=97" in assumptions["confidence_label"]


def test_an_account_with_no_landed_score_row_is_not_priced() -> None:
    """The money table prices exactly the accounts the queue can show.

    Band F is outside `ck_score_band`, so `score_rows` refuses it; pricing it anyway would put a
    figure on a card the score table has no row for.
    """
    rows, refused = economics_rows(_scored(band="F"), config=_config(), intervals=_intervals(ACCT))

    assert rows == []
    assert any("band=" in line and "'F'" in line for line in refused), refused


def test_an_account_nobody_propagated_lands_with_null_quantiles_and_names_the_producer() -> None:
    """Migration 0004's whole purpose: the absence of a distribution stops erasing the account.

    The fixture is the shipped default shape — `downstream_outflow_24h_minor` measured at 10,000,000
    against 20,000,000 of inflow, so the cap does not bind and E_i = 10,000,000. Band A is 5 minutes:

        p*E*r   = 0.5 * 10,000,000 * 0.35            =  1,750,000
        c       = 5 min x 15,000/minute              =     75,000
        (1-p)*f = 0.5 * 2,500,000                    =  1,250,000
        EV      = 1,750,000 - 75,000 - 1,250,000     =    425,000
        density = 425,000 / 5 minutes                =   85,000.0

    Nothing above needs a Monte Carlo, and the row says so: the trio is null, `mc_runs` is the zero
    draws taken, and the assumption line names the producer that would fill them and the edge list it
    would read. A zero in a quantile column instead would have reported a distribution concentrated at
    nothing, and `config/economics.yaml`'s runs would have reported 10,000 draws that never happened.
    """
    rows, refused = economics_rows(_scored(), config=_config())

    assert (
        refused == []
    ), f"an unpropagated account is a labelled state now, not a dropped row: {refused}"
    assert len(rows) == 1
    row = rows[0]
    assert row["exposure_minor"] == 10_000_000
    assert row["loss_avoided_minor"] == 1_750_000
    assert row["analyst_cost_minor"] == 75_000
    assert row["friction_cost_minor"] == 1_250_000
    assert row["expected_value_minor"] == 425_000
    assert row["ev_density"] == 85_000.0

    assert [row[column] for column in ECONOMICS_UNMEASURED_COLUMNS] == [None, None, None]
    assert row["mc_runs"] == ECONOMICS_UNRUN_MC_DRAWS
    # Declared parameters, not results: the seed and the nominal coverage the config names, with no
    # draw count attached to them. See `economics.mc_runs`' column comment in migration 0004.
    assert row["mc_seed"] == 1337
    assert row["mc_interval"] == [0.05, 0.95]

    assumptions = row["assumptions"]
    assert assumptions["monte_carlo_propagated"] is False
    basis = assumptions["exposure_interval_basis"]
    assert "no simulated exposure interval" in basis, basis
    for column in ECONOMICS_UNMEASURED_COLUMNS:
        assert column in basis, f"{column} not named in the row's own explanation: {basis}"
    # Checkable rather than reassuring: a reader can go and run the named producer over the named
    # artifact. These two substrings are the promise.
    assert "simulate_exposure_interval" in basis, basis
    assert "out/graph/<run>/pairs.parquet" in basis, basis
    assert "not a record of it running" in basis, basis
    # The sink's own money gate still passes: null is absent, and an absent amount is not a float.
    assert_money_is_integer_minor("economics", row)


def test_a_missing_interval_changes_no_money_figure_because_it_entered_no_term() -> None:
    """The interval is a distributional claim about E_i, not an input to it, so the money is equal.

    Same account, same frame, same config — supplied versus absent. Hand-computed once:
    E = 10,000,000 (cap not binding on 20,000,000 of inflow), p = 0.5, r = 0.35, m = 5 min so
    c = 75,000 and (1-p)*f = 1,250,000, giving p*E*r = 1,750,000 and EV = 1,750,000 - 75,000 -
    1,250,000 = 425,000 at a density of 85,000.0 minor per analyst-minute.

    This is the guard against the other failure: if landing an interval-free row moved a number, the
    row would be pricing a distribution it says it does not have.
    """
    absent, _ = economics_rows(_scored(), config=_config())
    measured, _ = economics_rows(_scored(), config=_config(), intervals=_intervals(ACCT))

    money_columns = (
        "exposure_minor",
        "expected_value_minor",
        "loss_avoided_minor",
        "analyst_cost_minor",
        "friction_cost_minor",
        "analyst_minutes",
        "ev_density",
    )
    assert {column: absent[0][column] for column in money_columns} == {
        column: measured[0][column] for column in money_columns
    }
    assert absent[0]["expected_value_minor"] == measured[0]["expected_value_minor"] == 425_000
    # What DOES differ is the distribution and the flag describing it.
    assert absent[0]["mc_p50_minor"] is None and measured[0]["mc_p50_minor"] == 10_000_000
    assert absent[0]["assumptions"]["monte_carlo_propagated"] is False
    assert measured[0]["assumptions"]["monte_carlo_propagated"] is True
    assert "exposure_interval_basis" not in measured[0]["assumptions"], (
        "a measured row still " "explains an absence it does not have"
    )


def test_a_partially_supplied_interval_set_measures_only_what_it_was_given() -> None:
    """An account nobody propagated is not evidence that its neighbour's distribution applies to it.

    Both land, because 0004 made the absence representable; only ACCT carries figures. The money is
    the fixture's own in both cases — E = 10,000,000, p = 0.5, r = 0.35, m = 5 min, so
    p*E*r = 1,750,000 and EV = 1,750,000 - 75,000 - 1,250,000 = 425,000 each.
    """
    frames = pl.concat([_scored(account_key=ACCT), _scored(account_key=OTHER)])

    rows, refused = economics_rows(frames, config=_config(), intervals=_intervals(ACCT))

    assert refused == [], refused
    by_account = {row["account_key"]: row for row in rows}
    assert sorted(by_account) == [ACCT, OTHER]
    assert by_account[ACCT]["mc_p50_minor"] == 10_000_000
    assert by_account[OTHER]["mc_p50_minor"] is None
    assert by_account[OTHER]["mc_runs"] == ECONOMICS_UNRUN_MC_DRAWS
    assert by_account[OTHER]["assumptions"]["exposure_interval_basis"]
    assert all(by_account[account]["expected_value_minor"] == 425_000 for account in by_account)


def test_the_landed_run_shape_prices_a_degree_zero_account_with_no_interval_at_all() -> None:
    """DEV-031's branch and 0004's absence together: this is the 43,511-row case, unsimulated.

    `downstream_outflow_24h_minor` null with `graph_out_degree_30d` at 0 AND at null — the two shapes
    DEV-031 measured across all 43,511 of them, one branch because neither records a leg to sum — so
    E_i is the subject's own 12,000,000 outflow against 20,000,000 of inflow (the cap does not bind),
    and no caller ran the propagation. Band A is 5 minutes:

        p*E*r   = 0.5 * 12,000,000 * 0.35            =  2,100,000
        EV      = 2,100,000 - 75,000 - 1,250,000     =    775,000
        density = 775,000 / 5 minutes                = 155,000.0

    Two independent absences, two separate statements: the downstream term contributed nothing because
    the set was measured empty, and the distribution is missing because nothing sampled it. Neither
    may be collapsed into the other — a reader who ran the propagation would still have no downstream.
    """
    for degree in (0, None):
        rows, refused = economics_rows(
            _scored(
                **{
                    ECONOMICS_EXPOSURE_SOURCE: None,
                    ECONOMICS_DOWNSTREAM_DEGREE_SOURCE: degree,
                    ECONOMICS_SUBJECT_OUTFLOW_SOURCE: 12_000_000,
                }
            ),
            config=_config(),
        )

        assert refused == [], f"degree={degree}: {refused}"
        row = rows[0]
        assert row["exposure_minor"] == 12_000_000
        assert row["expected_value_minor"] == 775_000
        assert row["ev_density"] == 155_000.0
        assert row["mc_p05_minor"] is None and row["mc_p95_minor"] is None

        assumptions = row["assumptions"]
        assert assumptions["exposure_is_partial"] is True
        assert "measured EMPTY" in assumptions["exposure_downstream_basis"]
        assert assumptions["monte_carlo_propagated"] is False
        assert "simulate_exposure_interval" in assumptions["exposure_interval_basis"]


def test_a_positive_degree_with_a_null_downstream_refuses_even_when_no_interval_was_supplied() -> (
    None
):
    """The DEV-031 guard is about the money, not about the Monte Carlo sitting behind it.

    Removing the interval refusal must not quietly turn the downstream guard into a refusal that only
    fires in the presence of `intervals=`. Degree 3 with no downstream measurement is an unknown E_i
    whatever the caller passed, and an unknown E_i is not priceable at all: no row, and the reason
    names the degree.
    """
    rows, refused = economics_rows(
        _scored(
            **{
                ECONOMICS_EXPOSURE_SOURCE: None,
                ECONOMICS_DOWNSTREAM_DEGREE_SOURCE: 3,
                ECONOMICS_SUBJECT_OUTFLOW_SOURCE: 12_000_000,
            }
        ),
        config=_config(),
    )

    assert rows == [], f"an unmeasured E_i landed because the interval gate came down: {rows}"
    assert len(refused) == 1, refused
    assert f"{ECONOMICS_DOWNSTREAM_DEGREE_SOURCE}=3" in refused[0], refused[0]
    assert "1-hop downstream leg" in refused[0], refused[0]


def test_the_row_refuses_an_absent_inflow_cap_even_with_no_interval_supplied() -> None:
    """Refusals still mean an unpriceable amount. E_i's cap has no other source, interval or not.

    The fixture's 10,000,000 of measured downstream is priceable only against a measured inflow,
    and the row that lacks one does not exist: an uncapped exposure is a larger number than §3.2
    licenses, and 0004 made a distribution absent, not the cap.
    """
    rows, refused = economics_rows(_scored(**{ECONOMICS_INFLOW_CAP_SOURCE: None}), config=_config())

    assert rows == []
    assert ECONOMICS_INFLOW_CAP_SOURCE in refused[0] and "definition" in refused[0], refused[0]


def test_the_schema_and_the_migration_agree_that_the_quantiles_are_optional() -> None:
    """The mapper's nulls, the ORM's columns and revision 0004 are one claim or they are broken.

    `landing.py` writes a null trio because `models.py` says the columns are nullable and because
    `0004_mc_interval_optional.py` dropped the NOT NULLs on the live database. Any one of the three
    can fall out of step on its own — a mapper writing nulls into a NOT NULL column fails at 3am on a
    run nobody is watching, and a migration applied to a schema the ORM no longer describes makes the
    read side guess. So the three are pinned against each other here, in the only place that holds all
    of them without a database.

    The run-scoped list `0002_integrity_triggers.py` maintains is checked too: `economics` is already
    in it, and the immutability triggers cover a table this revision alters rather than creates, so
    nothing about adding a revision makes that list fall behind the metadata.
    """
    table = Base.metadata.tables["economics"]
    for column in ECONOMICS_UNMEASURED_COLUMNS:
        assert table.columns[column].nullable is True, f"{column} went back to NOT NULL"
    assert table.columns["mc_runs"].nullable is False, "the draw count is never absent"
    assert "ck_economics_mc_interval_pairing" in {
        constraint.name for constraint in table.constraints if constraint.name
    }, "the pairing that makes a null trio mean no-simulation is not declared"

    migrations = REPO_ROOT / "apps" / "api" / "alembic" / "versions"
    revisions = sorted(migrations.glob("0004_*.py"))
    assert [path.name for path in revisions] == ["0004_mc_interval_optional.py"], revisions
    migration = revisions[0].read_text(encoding="utf-8")
    # Revised from 0003, and it is the NOT NULLs that go, not the columns.
    assert 'down_revision: str | None = "0003_calibration_kind"' in migration, migration[:200]
    assert "nullable=True" in migration
    assert "ck_economics_mc_interval_pairing" in migration
    for column in ECONOMICS_UNMEASURED_COLUMNS:
        assert column in migration, f"{column} not carried by the revision that makes it optional"

    integrity = (migrations / "0002_integrity_triggers.py").read_text(encoding="utf-8")
    assert '"economics",' in integrity, "the money table left the run-scoped trigger list"


def test_a_frame_missing_its_exposure_columns_fails_loudly_rather_than_landing_a_null() -> None:
    with pytest.raises(LandingError, match=ECONOMICS_EXPOSURE_SOURCE):
        economics_rows(_scored().drop(ECONOMICS_EXPOSURE_SOURCE), config=_config())


def test_a_frame_with_no_out_of_sample_rows_refuses_the_table_rather_than_posting_it_empty() -> (
    None
):
    """`score_rows` is asked first, so `economics` inherits its out-of-sample refusal verbatim."""
    with pytest.raises(LandingError, match="no scored rows with role="):
        economics_rows(_scored(role="train"), config=_config(), intervals=_intervals(ACCT))


def test_the_hand_stated_figures_are_the_shipped_file_declares() -> None:
    """The arithmetic above is only a cross-check if the terms are the product's real ones."""
    shipped = load_economics(REPO_ROOT)
    mine = _config()

    assert shipped.currency == mine.currency == "UGX"
    assert shipped.minor_units_per_major == mine.minor_units_per_major == 100
    assert shipped.recovery.rate == mine.recovery.rate == RECOVERY_RATE
    assert shipped.analyst.cost_per_minute_minor == COST_PER_MINUTE
    assert shipped.analyst.min_review_minutes == MIN_REVIEW_MINUTES
    assert shipped.friction_cost.minor == FRICTION_MINOR
    assert dict(shipped.review_minutes_by_alert_class) == MINUTES_BY_BAND
    assert shipped.monte_carlo.runs == mine.monte_carlo.runs
    assert shipped.monte_carlo.seed == mine.monte_carlo.seed
