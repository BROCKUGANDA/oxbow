"""P7 — the policy tables, projected from the figures the backtest artifact actually publishes.

``policy`` and ``policy_summary`` are declared in the ORM, migrated in 0001, listed in
``apps/api/readmodel.py``'s ``POSTGRES_ONLY_TABLES`` and read by ``/api/dashboard`` for its four
currency tiles — and no builder for either existed anywhere in the repository, which is why the
command dashboard answers 503 on a fully landed warehouse. These mappers are that projection, and
the shape of the answer is set by one fact about the tables: **every column of both is NOT NULL**
except ``policy``'s ``degraded_reason``, ``solve_ms`` and ``optimality_gap_minor``. So a figure the
artifact does not record cannot be left unset the way a ``economics`` row has been able to leave
its Monte Carlo quantiles unset since migration 0004 — it refuses the row. That makes "an unknown
is a refusal, not a zero" load-bearing here rather than a slogan, and these tests are mostly about
it.

Fixtures are synthetic and every expected number below is arithmetic done on paper against the
literal input written beside it, never read back out of the mapper (§19 rule 6). The money figures
are copied from the shape of ``out/backtest/real40k/ablation_results.json`` so the fixture is
recognisable to anyone who has read that artifact, but nothing reads the real file except the last
test, which is skipped when ``out/`` is absent because it is gitignored.

What each refusal is for is stated in the mapper's own docstring; what is pinned here is the four
shapes that would silently corrupt the dashboard: a required column filled by arithmetic the
producer never did, an operating point chosen because it looks best, a summary bound to another
run's id, and a float in a money column.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Final

import pytest

from oxbow.adapters.warehouse.landing import (
    BACKTEST_FOLD_OWNER,
    POLICY_OWNER,
    POLICY_SUMMARY_UNPUBLISHED,
    LandingError,
    policy_rows,
    policy_summary_rows,
)
from oxbow.ports.warehouse import WarehouseTableError, assert_money_is_integer_minor
from oxbow.quant.economics import load_economics

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ECONOMICS: Final = load_economics(REPO_ROOT)

RUN_ID: Final = "01M3H8WG436R394NZT2GS1KG69"
OTHER_RUN_ID: Final = "01M3SRWV7JK89R9XPE43JH1KN4"
#: The artifact names the run whose features it walked by path, so the fixture states a path of the
#: same shape rather than a bare id.
WINDOW_SOURCE: Final = f"C:\\repo\\out\\features\\{RUN_ID}\\features_manifest.json"
DIGEST: Final = "8bd630cf3ad71ce2a56c1040c39eae233a2835888cc400e5b34299d8f4afcf4e"

#: Copied from the artifact's own ``allocator_label`` strings. These are the producer's sentences,
#: and the mapper matches them against ``AllocatorId.label`` exactly, so a fixture that paraphrased
#: one would test nothing.
CPSAT_LABEL: Final = "CP-SAT exact 0/1 knapsack (optimality proven within the deadline)"
GREEDY_LABEL: Final = (
    "greedy by EV density (fast approximation; optimal for the fractional relaxation)"
)
DEGRADED_LABEL: Final = (
    "greedy by EV density - DEGRADED: the exact CP-SAT solve passed its hard deadline, so no "
    "optimality gap is available for this result"
)
THRESHOLD_LABEL: Final = "baseline: score-threshold at the same analyst capacity"

# --- the fixture -------------------------------------------------------------


def _fold(index: int) -> dict[str, Any]:
    """One fold record: the currency and the Monte Carlo parameters are read at this grain.

    The per-fold money (``captured_value_minor``, ``review_cost_minor``, ``friction_cost_minor``,
    ``accounts_reviewed``, ``n_decisions``) is deliberately absent: the mapper reads none of it, and
    a fixture carrying it would invite a test that asserted a summed total the producer never
    published.
    """
    return {
        "fold_index": index,
        "economics": {"currency": "UGX", "mc_draws": 10_000, "mc_seed": 1337},
    }


def _owner_record(**overrides: Any) -> dict[str, Any]:
    """The ``full_calibrated`/`ev_cpsat`` record as the artifact shapes it, with the owner's money.

    Hand-computed against the keys, so each landed column can be traced to the one line below it:

    =========================  =============  =========================================
    artifact key               value          column it becomes
    =========================  =============  =========================================
    net_benefit_total_minor      258_670_182  policy_summary.net_benefit_minor
    var95_mean_minor             204_394_946  policy_summary.var95_minor
    es975_mean_minor             320_503_169  policy_summary.es975_minor
    benefit_per_analyst_hour     129_335_091  ...benefit_per_analyst_hour_minor
    max_drawdown_minor                     0  policy_summary.max_drawdown_minor
    mean_alerts_per_10k          2.29357798   ...alerts_per_10k_accounts
    risk_adjusted_ratio.value    0.4472136    ...risk_adjusted_benefit
    =========================  =============  =========================================
    """
    record: dict[str, Any] = {
        "policy": "ev_cpsat",
        "allocator_label": CPSAT_LABEL,
        "net_benefit_total_minor": 258_670_182,
        "benefit_per_analyst_hour_minor": 129_335_091,
        "max_drawdown_minor": 0,
        "zero_drawdown_labelled": True,
        "var95_mean_minor": 204_394_946,
        "es975_mean_minor": 320_503_169,
        "mean_alerts_per_10k_accounts": 2.29357798,
        "optimality_gap_minor": 0,
        "var95_reduction_vs_threshold_minor": -73_451_366,
        "es975_reduction_vs_threshold_minor": -150_088_264,
        "cumulative_benefit_minor": [0, 0, 0, 0, 258_670_182],
        "risk_adjusted_benefit_ratio": {
            "label": "Risk-adjusted benefit ratio",
            "formula": "mean(per-period net benefit) / std(per-period net benefit)",
            "is_sharpe_ratio": False,
            "not_sharpe_because": "no risk-free rate subtracted, no annualisation",
            "value": 0.4472136,
        },
        "folds": [_fold(index) for index in range(5)],
    }
    record.update(overrides)
    return record


def _baseline_record() -> dict[str, Any]:
    """The sibling ``score_threshold`` ladder, the thing the two reductions are reductions against."""
    return {
        "policy": "score_threshold",
        "allocator_label": THRESHOLD_LABEL,
        "net_benefit_total_minor": -6_199_161_801,
        "var95_mean_minor": 130_943_581,
        "es975_mean_minor": 170_414_905,
        "max_drawdown_minor": 6_199_161_801,
        "zero_drawdown_labelled": False,
        "folds": [_fold(index) for index in range(5)],
    }


def _rival_record() -> dict[str, Any]:
    """Another arm's greedy ladder, worth far more, to prove the owner rule is not a winner pick."""
    return {
        "policy": "ev_greedy",
        "allocator_label": GREEDY_LABEL,
        "net_benefit_total_minor": 999_000_000,
        "folds": [_fold(index) for index in range(5)],
    }


def _document(
    record: dict[str, Any] | None = None, *, include_rival: bool = True
) -> dict[str, Any]:
    """A document shaped like ``ablation_results.json``: one owner arm, one richer rival, one control."""
    owner = _owner_record() if record is None else record
    variants: list[dict[str, Any]] = [
        {
            "row_id": "rules_only",
            "label": "Rules only",
            "corpus": "real-corpus",
            "provenance": "real_corpus",
            "is_control": False,
            "policies": {"ev_greedy": _rival_record()},
        },
        {
            "row_id": "full_calibrated",
            "label": "Full system, calibrated",
            "corpus": "real-corpus",
            "provenance": "real_corpus",
            "is_control": False,
            "policies": {
                "ev_cpsat": owner,
                "score_threshold": _baseline_record(),
            },
        },
        {
            "row_id": "leakage_control",
            "label": "CONTROL - lookahead, not a shippable configuration",
            "corpus": "real-corpus",
            "provenance": "real_corpus",
            "is_control": True,
            "policies": {"ev_cpsat": copy.deepcopy(owner)},
        },
    ]
    if not include_rival:
        variants.pop(0)
    return {
        "artifact": "oxbow-backtest-v1",
        "content_sha256": DIGEST,
        "corpus": "real-corpus",
        "provenance_note": "provenance=real_corpus",
        "seed": 1337,
        "fold_plan_window": {"folds": 5, "window_source": WINDOW_SOURCE},
        "variants": variants,
    }


_CARD: Final[dict[str, Any]] = {"currency": "UGX", "corpus": "real-corpus"}

#: The six totals the artifact does not publish today. Supplying them is what lets a summary row
#: land, which is why they are a fixture rather than a mock: the mapper looks them up by name and
#: the test proves the lookup, the types and the trace all at once.
_TOTALS: Final[dict[str, Any]] = {
    "captured_value_total_minor": 260_470_182,
    "review_cost_total_minor": 1_800_000,
    "friction_cost_total_minor": 0,
    "selected_count_total": 1,
    "candidate_count_total": 43_405,
    "frontier_points": [
        {"capacity_minutes": 0, "net_benefit_minor": 0},
        {"capacity_minutes": 12_000, "net_benefit_minor": 258_670_182},
    ],
}


def _landed_summary(record: dict[str, Any]) -> dict[str, Any]:
    """The owner's summary row, with the document's identity pointed at ``RUN_ID``."""
    rows, refused = policy_summary_rows(_document(record), _CARD, ECONOMICS, run_id=RUN_ID)
    assert rows, f"the totals are all published here, so the row must land: {refused}"
    assert refused == []
    (row,) = rows
    return row


# --- the active-policy rule, asserted rather than assumed --------------------


def test_the_policy_owner_is_the_same_operating_point_the_fold_table_already_declares() -> None:
    """One declared owner, shared by three tables, so a page cannot show two configurations.

    ``backtest_fold`` needed the same decision for the same reason: ``(run_id, fold_index)`` holds
    one row while the artifact records a fold once per arm per ladder. If these two constants ever
    drift apart the dashboard's money and the fold curve describe different runs of the allocator,
    and nothing on screen would say so.
    """
    assert POLICY_OWNER == BACKTEST_FOLD_OWNER
    assert POLICY_OWNER == ("full_calibrated", "ev_cpsat")


def test_the_owner_is_not_chosen_because_it_has_the_best_figure() -> None:
    """A rival arm worth 999,000,000 minor to the owner's 258,670,182 still does not become active.

    Choosing by net benefit would make the loader pick a benchmark winner and present it as the
    operating point, which is the exact move this rule exists to refuse.
    """
    rows, refused = policy_rows(_document(), _CARD, ECONOMICS)

    assert refused == []
    (row,) = rows
    assert row["policy_id"] == "ev_cpsat"
    assert row["active"] is True, "the declared owner, not the arm with the bigger number"


def test_exactly_one_policy_row_is_emitted_and_it_is_the_owner() -> None:
    rows, refused = policy_rows(_document(), _CARD, ECONOMICS)

    assert refused == []
    assert len(rows) == 1, f"models.py says exactly one policy is active: {rows}"
    assert sum(1 for row in rows if row["active"]) == 1


def test_a_ladder_key_and_its_echo_disagreeing_refuses_both_tables() -> None:
    """The artifact records the ladder twice; a loader that picked one half would be guessing."""
    record = _owner_record(policy="ev_greedy")
    document = _document(record)

    prow, pref = policy_rows(document, _CARD, ECONOMICS)
    srow, sref = policy_summary_rows(document, _CARD, ECONOMICS, run_id=RUN_ID)

    assert prow == [] and srow == []
    assert any("names itself" in line for line in pref), pref
    assert any("names itself" in line for line in sref), sref


def test_a_control_arm_cannot_be_the_operating_point() -> None:
    document = _document()
    owner_arm = next(v for v in document["variants"] if v["row_id"] == "full_calibrated")
    owner_arm["is_control"] = True

    rows, refused = policy_rows(document, _CARD, ECONOMICS)

    assert rows == []
    assert any("labelled a control" in line for line in refused), refused


def test_a_document_without_the_owner_arm_refuses_and_names_the_arms_present() -> None:
    document = _document()
    document["variants"] = [document["variants"][0]]

    rows, refused = policy_rows(document, _CARD, ECONOMICS)

    assert rows == []
    assert any(
        "no variant in the document has row_id 'full_calibrated'" in line for line in refused
    )
    assert any("rules_only" in line for line in refused), refused


# --- policy: every column has a source, so the row lands --------------------


def test_the_policy_row_is_every_column_traced_to_its_source() -> None:
    """The landed row, checked column by column against the fixture and ``config/economics.yaml``.

    Economic parameters are the declared assumption set (config is their only source, and the
    artifact never records the knobs that priced its money); identity columns come from the record.
    Both halves are hand-computed in the assertions below.
    """
    rows, refused = policy_rows(_document(), _CARD, ECONOMICS)

    assert refused == []
    (row,) = rows
    assert row["policy_id"] == "ev_cpsat", "the recorded ladder name, not a minted id"
    assert row["name"] == CPSAT_LABEL
    assert row["solver"] == "cpsat_exact"
    assert row["active"] is True
    assert row["degraded"] is False
    assert row["currency"] == "UGX"
    # config/economics.yaml, read as declared.
    assert row["capacity_minutes"] == 12_000
    assert row["recovery_rate"] == 0.35
    assert row["analyst_cost_per_hour_minor"] == 900_000
    assert row["min_review_minutes"] == 5.0
    assert row["friction_cost_minor"] == 2_500_000
    assert row["four_eyes_threshold_minor"] == 50_000_000
    assert row["review_minutes_by_band"] == {"A": 5, "B": 12, "C": 25, "D": 60, "E": 120}
    assert row["optimality_gap_minor"] == 0
    # The port's own money gate, run on the row the sink would receive.
    assert_money_is_integer_minor("policy", row)


def test_the_r_band_is_stored_beside_the_rate_and_never_alone() -> None:
    """DEV §3.2: no currency figure reaches a reader without its recovery-rate sensitivity band.

    ``policy.recovery_sensitivity_band`` is the table's only home for it, and ``policy_summary``
    declares no band column at all, so the band is also echoed into the summary's ``assumptions``;
    a summary whose band could be moved by editing config after the run is the single money figure
    the plan forbids.
    """
    rows, _refused = policy_rows(_document(), _CARD, ECONOMICS)

    assert rows[0]["recovery_sensitivity_band"] == [0.20, 0.35, 0.50]
    assert rows[0]["recovery_rate"] in rows[0]["recovery_sensitivity_band"]

    summary = _landed_summary(_owner_record(**_TOTALS))
    assert summary["assumptions"]["recovery.sensitivity_band"] == [0.20, 0.35, 0.50]
    assert summary["assumptions"]["recovery.rate"] == 0.35


def test_nullable_columns_absent_from_the_record_land_the_row_anything_else_can() -> None:
    """The one place "lands what it can" is literally true for these tables.

    ``solve_ms``, ``degraded_reason`` and ``optimality_gap_minor`` are nullable, so an unrecorded
    solve time leaves the column unset and the rest of the row still lands. A zero in
    ``optimality_gap_minor`` would claim a proven-optimal solve the artifact never reported.
    """
    record = _owner_record()
    del record["optimality_gap_minor"]
    rows, refused = policy_rows(_document(record), _CARD, ECONOMICS)

    assert refused == []
    (row,) = rows
    assert "optimality_gap_minor" not in row
    assert "solve_ms" not in row, "no solve time is recorded, so none is claimed"
    assert row["solver"] == "cpsat_exact"


def test_a_degraded_solver_id_cannot_be_landed_and_says_so_by_column() -> None:
    """A finding about the schema, pinned: ``policy.solver`` is VARCHAR(16).

    Both ``AllocatorId`` labels that carry the producer's own DEGRADED word resolve to
    ``greedy_ev_density_after_cp_sat_deadline`` (35 characters) and
    ``..._after_cp_sat_error`` (33), and the greedy ladder's id is 17. So the degraded state the
    UI is *required* to banner (models.py: "degraded, not broken") has no solver id that fits its
    own column. Truncating would land an id naming an allocation nobody ran, so the row refuses and
    names the column.
    """
    rows, refused = policy_rows(
        _document(_owner_record(allocator_label=DEGRADED_LABEL)), _CARD, ECONOMICS
    )

    assert rows == []
    assert any("VARCHAR(16)" in line and "policy.solver" in line for line in refused), refused


def test_an_allocator_label_no_enum_owns_refuses_the_row() -> None:
    rows, refused = policy_rows(
        _document(_owner_record(allocator_label="baseline: score-threshold at the same capacity")),
        _CARD,
        ECONOMICS,
    )

    assert rows == []
    assert any("matches no AllocatorId.label" in line for line in refused), refused


# --- policy_summary: the published figures, traced --------------------------


def test_the_summary_row_lands_every_published_figure_with_each_one_traced() -> None:
    """Each figure below is copied from the fixture line naming its artifact key, by hand."""
    row = _landed_summary(_owner_record(**_TOTALS))

    assert row["net_benefit_minor"] == 258_670_182
    assert row["benefit_per_analyst_hour_minor"] == 129_335_091
    assert row["max_drawdown_minor"] == 0
    assert row["var95_minor"] == 204_394_946
    assert row["es975_minor"] == 320_503_169
    assert row["loss_avoided_minor"] == 260_470_182
    assert row["analyst_cost_minor"] == 1_800_000
    assert row["friction_cost_minor"] == 0
    assert row["selected_count"] == 1
    assert row["candidate_count"] == 43_405
    assert row["alerts_per_10k_accounts"] == 2.29357798
    assert row["risk_adjusted_benefit"] == 0.4472136
    assert row["mc_runs"] == 10_000
    assert row["mc_seed"] == 1337
    assert row["capacity_minutes"] == 12_000
    assert row["var_alpha"] == 0.95
    assert row["es_alpha"] == 0.975
    assert row["zero_drawdown"] is True
    assert row["currency"] == "UGX"
    assert row["policy_id"] == "ev_cpsat"

    assert row["risk_adjusted_benefit_note"] == (
        "Risk-adjusted benefit ratio = mean(per-period net benefit) / std(per-period net "
        "benefit); no risk-free rate subtracted, no annualisation"
    ), "the not-Sharpe label travels with the ratio (plan §12)"

    # The curve is the published list, zipped onto the published fold indices. Nothing computed.
    assert row["cumulative_curve"] == [
        {"period": 0, "cumulative_benefit_minor": 0},
        {"period": 1, "cumulative_benefit_minor": 0},
        {"period": 2, "cumulative_benefit_minor": 0},
        {"period": 3, "cumulative_benefit_minor": 0},
        {"period": 4, "cumulative_benefit_minor": 258_670_182},
    ]

    # The baselines echo the recorded reductions and the sibling ladder's own published totals.
    assert row["baselines"]["reduction_vs_threshold_minor"] == {
        "es975_reduction_vs_threshold_minor": -150_088_264,
        "var95_reduction_vs_threshold_minor": -73_451_366,
    }
    assert row["baselines"]["baseline_net_benefit_total_minor"] == -6_199_161_801
    assert row["baselines"]["baseline_allocator_label"] == THRESHOLD_LABEL

    assert row["frontier"] == _TOTALS["frontier_points"]
    assert row["assumptions"]["content_sha256"] == DIGEST
    assert row["assumptions"]["owner_arm"] == "full_calibrated"
    assert row["assumptions"]["owner_ladder"] == "ev_cpsat"
    assert row["assumptions"]["minor_units_per_major"] == 100, (
        "the BASE, never the exponent: a `decimals` field carrying 100 divides every rendered "
        "figure by 10^100 (DEV-024)"
    )
    assert_money_is_integer_minor("policy_summary", row)


def test_a_published_figure_that_is_absent_refuses_that_figure_by_name() -> None:
    """Not a blanket error: the refusal names the one key, so a reviewer goes to the artifact."""
    record = _owner_record(**_TOTALS)
    del record["es975_mean_minor"]

    rows, refused = policy_summary_rows(_document(record), _CARD, ECONOMICS, run_id=RUN_ID)

    assert rows == [], "every column is NOT NULL, so a row with a hole cannot land"
    assert any("policy_summary.es975_minor" in line for line in refused), refused
    assert not any(
        "policy_summary.var95_minor" in line for line in refused
    ), f"var95 is present and must not be refused: {refused}"
    assert not any(
        "loss_avoided_minor" in line for line in refused
    ), f"the total is published in this fixture, so it must not be refused: {refused}"


def test_an_unpublished_required_column_refuses_with_the_key_that_would_fill_it() -> None:
    """The finding the four dashboard tiles are waiting on, stated as a lookup rather than a shrug.

    The real artifact records this money per fold inside ``folds[].economics`` and publishes no
    run-level total, and it records no frontier at all. The mapper reads the total key and refuses
    naming it, so the producer change is a one-line addition on the harness side and this file does
    not have to be re-reasoned. ``friction_cost_total_minor: 0`` in ``_TOTALS`` shows the other
    half: a *published* zero lands, an *absent* figure never does.
    """
    rows, refused = policy_summary_rows(_document(), _CARD, ECONOMICS, run_id=RUN_ID)

    assert rows == []
    for column in sorted(POLICY_SUMMARY_UNPUBLISHED):
        assert any(
            f"policy_summary.{column}" in line for line in refused
        ), f"{column} is required and unpublished, so it must be named: {refused}"
    for column, (total_key, _why) in POLICY_SUMMARY_UNPUBLISHED.items():
        assert any(total_key in line for line in refused), f"{column}: {refused}"
    headline = refused[0]
    assert "no row landed" in headline
    assert "6 required column(s) have no published figure" in headline, headline


def test_zero_is_never_substituted_for_a_missing_money_figure() -> None:
    """DEV §19: an unknown left a zero on the dashboard would read as a measured break-even."""
    record = _owner_record(**_TOTALS)
    record["net_benefit_total_minor"] = None

    rows, refused = policy_summary_rows(_document(record), _CARD, ECONOMICS, run_id=RUN_ID)

    assert rows == []
    assert any("policy_summary.net_benefit_minor" in line for line in refused), refused


def test_a_float_in_a_money_column_is_refused_before_the_sink_is_reached() -> None:
    record = _owner_record(**_TOTALS)
    record["es975_mean_minor"] = 320_503_169.5

    rows, refused = policy_summary_rows(_document(record), _CARD, ECONOMICS, run_id=RUN_ID)

    assert rows == []
    assert any("policy_summary.es975_minor" in line for line in refused), refused
    assert not any(line.endswith("320503169.5") for line in refused)


def test_a_count_that_is_not_an_integer_is_refused() -> None:
    record = _owner_record(**_TOTALS)
    record["selected_count_total"] = 1.0

    rows, refused = policy_summary_rows(_document(record), _CARD, ECONOMICS, run_id=RUN_ID)

    assert rows == []
    assert any(
        "policy_summary.selected_count" in line and "integer count" in line for line in refused
    ), refused


def test_a_flag_contradicting_the_figure_it_reports_refuses_the_row() -> None:
    """The loader does not arbitrate a producer that disagrees with itself by picking a value."""
    record = _owner_record(**_TOTALS, max_drawdown_minor=5_000)

    rows, refused = policy_summary_rows(_document(record), _CARD, ECONOMICS, run_id=RUN_ID)

    assert rows == []
    assert any("policy_summary.zero_drawdown" in line for line in refused), refused


def test_a_curve_that_does_not_line_up_with_the_folds_refuses_instead_of_guessing() -> None:
    record = _owner_record(**_TOTALS, cumulative_benefit_minor=[0, 0, 258_670_182])

    rows, refused = policy_summary_rows(_document(record), _CARD, ECONOMICS, run_id=RUN_ID)

    assert rows == []
    assert any(
        "3 period(s) of cumulative benefit and 5 fold(s)" in line for line in refused
    ), refused


def test_two_recorded_currencies_that_disagree_refuse_the_row() -> None:
    """Fold economics and the model card are two records of one fact; both must be believed."""
    document = _document(_owner_record(**_TOTALS))
    document["variants"][1]["policies"]["ev_cpsat"]["folds"][2]["economics"]["currency"] = "EUR"

    rows, refused = policy_summary_rows(document, _CARD, ECONOMICS, run_id=RUN_ID)

    assert rows == []
    assert any("policy currency" in line for line in refused), refused


def test_monte_carlo_draw_counts_that_differ_across_folds_refuse_the_row() -> None:
    record = _owner_record(**_TOTALS)
    record["folds"][1]["economics"]["mc_draws"] = 5_000

    rows, refused = policy_summary_rows(_document(record), _CARD, ECONOMICS, run_id=RUN_ID)

    assert rows == []
    assert any("policy_summary.mc_runs" in line for line in refused), refused


# --- the run identity, checked before any money is bound to it --------------


def test_an_artifact_whose_fold_plan_names_another_run_is_refused() -> None:
    """The partition key is the whole reason two runs' economics cannot share a summary row."""
    rows, refused = policy_summary_rows(
        _document(_owner_record(**_TOTALS)), _CARD, ECONOMICS, run_id=OTHER_RUN_ID
    )

    assert rows == []
    (line,) = refused
    assert OTHER_RUN_ID in line and RUN_ID in line, line
    assert "fold_plan_window.window_source" in line


def test_an_artifact_without_a_digest_is_refused() -> None:
    """The row names the artifact version its figures came from, or it names nothing."""
    document = _document(_owner_record(**_TOTALS))
    document["content_sha256"] = "not-a-digest"

    rows, refused = policy_summary_rows(document, _CARD, ECONOMICS, run_id=RUN_ID)

    assert rows == []
    assert any("content_sha256" in line for line in refused), refused


def test_an_artifact_naming_no_corpus_or_provenance_is_refused() -> None:
    document = _document(_owner_record(**_TOTALS))
    document["provenance_note"] = ""

    rows, refused = policy_summary_rows(document, _CARD, ECONOMICS, run_id=RUN_ID)
    assert rows == []
    assert any("provenance_note" in line for line in refused), refused

    document = _document(_owner_record(**_TOTALS))
    document["corpus"] = None
    rows, refused = policy_summary_rows(document, _CARD, ECONOMICS, run_id=RUN_ID)
    assert rows == []
    assert any("no corpus" in line for line in refused), refused


def test_a_run_id_that_is_not_a_ulid_is_refused_before_anything_is_read() -> None:
    with pytest.raises(LandingError):
        policy_summary_rows(
            _document(_owner_record(**_TOTALS)),
            _CARD,
            ECONOMICS,
            run_id="not-a-run-id",
        )


# --- the wiring, which is a gate and not a switch ---------------------------


def test_the_port_declares_the_policy_tables_exactly_as_the_gate_believes() -> None:
    """The CLI withholds these tables when the port will not accept them, and only then.

    This asserts the *couple*, not today's answer: if ``WAREHOUSE_TABLES`` gains ``policy`` the
    gate opens in the same commit, and a test that pinned the absence would fail for the wrong
    reason the moment the work is unblocked.
    """
    from oxbow.ports.warehouse import WAREHOUSE_TABLES, assert_writable_table

    for name in ("policy", "policy_summary"):
        if name in WAREHOUSE_TABLES:
            assert assert_writable_table(name) == name
        else:
            with pytest.raises(WarehouseTableError, match=name):
                assert_writable_table(name)


def test_the_report_order_writes_the_parent_before_the_row_that_needs_it() -> None:
    """``policy_summary.policy_id`` is a NOT NULL foreign key, so insert order is not cosmetic.

    The landing writes in ``LANDING_REPORT_ORDER``, so ``policy`` has to precede ``policy_summary``
    or every summary row dies on the constraint.
    """
    from oxbow.cli import LANDING_REPORT_ORDER

    assert LANDING_REPORT_ORDER.index("policy") < LANDING_REPORT_ORDER.index("policy_summary")
    # Both are reported with the backtest tables they are shaped from, not as a mystery stage.
    assert LANDING_REPORT_ORDER.index("policy_summary") < LANDING_REPORT_ORDER.index("ablation_row")


def test_a_table_that_lands_nothing_still_prints_its_reason() -> None:
    """The reporting change this task needed: a silent refusal is indistinguishable from no work.

    Before it, a table absent from the write payload was skipped by the report loop, so an operator
    staring at an empty ``policy_summary`` had one line to grep and it was not there.
    """
    import inspect

    from oxbow import cli

    source = inspect.getsource(cli.land_warehouse_rows)
    assert "0 row(s) landed under run" in source, (
        "the report loop went back to skipping tables that wrote nothing, which hides the "
        "refusal this finding depends on"
    )


# --- the measured state of the real artifact --------------------------------


@pytest.mark.skipif(
    not (REPO_ROOT / "out" / "backtest" / "real40k" / "ablation_results.json").is_file(),
    reason="no real backtest artifact (out/ is gitignored); run `uv run oxbow backtest` first",
)
def test_the_real_artifact_lands_one_policy_and_refuses_the_summary_by_name() -> None:
    """Not a unit test — the measured state of ``out/backtest/real40k``, so the finding is not theory.

    ``policy`` lands complete from the config the run consumed plus the record's own identity.
    ``policy_summary`` lands nothing, and the reason is the six required columns the harness never
    totals — which is why ``/api/dashboard`` still answers 503 after this change and what
    ``oxbow backtest`` would have to publish to fix it.
    """
    import json

    base = REPO_ROOT / "out" / "backtest" / "real40k"
    ablation = json.loads((base / "ablation_results.json").read_text(encoding="utf-8"))
    card = json.loads((base / "model_card.json").read_text(encoding="utf-8"))

    prows, pref = policy_rows(ablation, card, ECONOMICS)
    assert len(prows) == 1, pref
    (prow,) = prows
    assert prow["policy_id"] == "ev_cpsat"
    assert prow["solver"] == "cpsat_exact"
    assert prow["currency"] == "UGX"
    assert prow["recovery_sensitivity_band"] == [0.20, 0.35, 0.50]

    srows, sref = policy_summary_rows(ablation, card, ECONOMICS, run_id=RUN_ID)
    assert srows == (
        []
    ), "the real artifact publishes no run-level totals, so nothing may land here"
    for column in POLICY_SUMMARY_UNPUBLISHED:
        assert any(f"policy_summary.{column}" in line for line in sref), sref
