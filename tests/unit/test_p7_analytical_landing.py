"""P7 — landing the analytical tables from the artifacts that exist (DEV-027's neighbours).

`WAREHOUSE_TABLES` declares 22 handoff tables. Three (`account`, `score`, `rule_hit`) already
had mappers and were being written by the `warehouse` stage; the analytical tables the `/model`
panes read had no writer at all, which is why those routes answer 200 over nothing.

These fixtures are synthetic and every expected value below is arithmetic done on paper against
the literal input written beside it — not the landed artifact, and not a number read back out of
the mapper (§19 rule 6). The real `out/backtest/real40k/*` documents are not used as fixtures
because `out/` is gitignored: a test that passes only on the machine that ran the pipeline proves
nothing to a reviewer, and a fixture copied from an artifact and then asserted against the code
that read it is the same circularity in a different costume.

What each family is here to refuse is stated in the mapper's own docstring; these tests pin the
three shapes that would silently corrupt the page if they ever changed: a figure two arms
disagree about (DEV-026's failure, moved from the ledger to the read model), a required column
with no measurement, and a bucket the artifact itself marks unmeasurable.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any, Final

import pytest

from oxbow.adapters.warehouse.landing import (
    LandingError,
    ablation_rows,
    backtest_fold_rows,
    fairness_rows,
    perturbation_rows,
    validation_metric_rows,
)

CI_METHOD: Final = "percentile_bootstrap"


def _ablation_document(*, ci_note: str = CI_METHOD, resamples: int | None = 40) -> dict[str, Any]:
    """Two arms, one policy ladder each, one shared CI block."""
    ci: dict[str, Any] = {"confidence_note": ci_note}
    if resamples is not None:
        ci["resamples"] = resamples
    policy = {"pr_auc_ci": ci, "folds": []}
    return {
        "corpus": "fixture-corpus",
        "seed": 1337,
        "variants": [
            {
                "row_id": "arm_a",
                "label": "Arm A",
                "corpus": "fixture-corpus",
                "base_rate": 0.002,
                "n_total_rows": 1000,
                "n_total_positive": 2,
                "fold_count": 5,
                "embargo_days": 30,
                "policies": {"score_threshold": copy.deepcopy(policy)},
            },
            {
                "row_id": "arm_b",
                "label": "Arm B",
                "corpus": "fixture-corpus",
                "base_rate": 0.002,
                "n_total_rows": 1000,
                "n_total_positive": 2,
                "fold_count": 5,
                "embargo_days": 30,
                "policies": {"score_threshold": copy.deepcopy(policy)},
            },
        ],
        "overfitting_controls": {"configs_evaluated": 3, "test_fold_touched_once": True},
        "corpus_feasibility": {
            "corpus_span_days": 700.5,
            "folds_supplied": 5,
            "max_full_embargo_gaps_in_span": 23,
            "supports_literal_walk_forward": True,
        },
        "corpus_fold_column_check": {"agreement_share": 1.0, "rows_compared": 100},
        "honest_ablation_rows": 2,
        "honest_model_fits": 1,
        "leakage_control": {"detected": True},
    }


_CARD: Final[dict[str, Any]] = {
    "ablation_table": [
        {
            "row_id": "arm_a",
            "label": "Arm A",
            "question": "Does the second arm earn its complexity?",
            "corpus": "fixture-corpus",
            "pr_auc": 0.25,
            "pr_auc_ci_low": 0.05,
            "pr_auc_ci_high": 0.45,
            "net_benefit_total_minor": 2500,
            "currency": "UGX",
        },
        {
            "row_id": "arm_b",
            "label": "Arm B",
            "question": "Does the second arm earn its complexity?",
            "corpus": "fixture-corpus",
            "pr_auc": 0.2,
            "pr_auc_ci_low": 0.04,
            "pr_auc_ci_high": 0.4,
            "net_benefit_total_minor": -100,
            "currency": "UGX",
        },
    ],
}


# --- ablation_row: the published table itself --------------------------------


def test_each_arm_lands_one_row_under_the_tables_own_names() -> None:
    rows, refused = ablation_rows(_CARD, _ablation_document(), declared_resamples=60)

    assert refused == [], f"every figure the fixture records is present: {refused}"
    assert [row["variant"] for row in rows] == ["arm_a", "arm_b"]
    # The producer calls it `net_benefit_total_minor`; the column is `net_benefit_minor`.
    assert rows[0]["net_benefit_minor"] == 2500
    assert rows[1]["net_benefit_minor"] == -100, "a loss is a number, not a clamp to zero"
    assert all(
        isinstance(row["net_benefit_minor"], int) for row in rows
    ), "money is an integer count of minor units; a float here is DEV-005 at the read model"
    assert rows[0]["pr_auc"] == 0.25 and rows[0]["ci_low"] == 0.05
    assert rows[0]["ci_method"] == CI_METHOD
    assert rows[0]["seed"] == 1337, "the seed comes from the run, not from the row's own label"


def test_the_resample_count_the_run_never_serialised_is_declared_or_refused() -> None:
    """`n_resamples` is NOT NULL and the artifact omits it: config supplies it, or the row refuses."""
    document = _ablation_document(resamples=None)

    from_config, refused = ablation_rows(_CARD, document, declared_resamples=99)
    assert refused == [] and {row["n_resamples"] for row in from_config} == {99}

    nothing, refused = ablation_rows(_CARD, document, declared_resamples=None)
    assert nothing == [], "a zero written here would claim a bootstrap that never ran"
    assert len(refused) == 2 and all("n_resamples" in line for line in refused), refused


def test_two_arms_naming_different_ci_methods_refuse_rather_than_pick_one() -> None:
    document = _ablation_document()
    document["variants"][1]["policies"]["score_threshold"]["pr_auc_ci"]["confidence_note"] = (
        "normal_approximation"
    )

    _rows, refused = ablation_rows(_CARD, document, declared_resamples=60)
    assert any("ci_method" in line for line in refused), refused
    assert any("Arm B/score_threshold" in line for line in refused), (
        "the refusal has to name the diverging source, or a reader cannot tell which arm moved: "
        f"{refused}"
    )


# --- validation_metric: one row per named figure, collapsed across the arms --


def _metric(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [row for row in rows if row["name"] == name]
    assert len(matches) == 1, f"expected exactly one {name!r} row, got {len(matches)}"
    return matches[0]


def test_a_figure_two_arms_agree_on_lands_once_with_its_n() -> None:
    rows, refused = validation_metric_rows(_ablation_document())

    prevalence = _metric(rows, "label_prevalence")
    assert prevalence["value"] == 0.002 and prevalence["unit"] == "share"
    assert prevalence["n"] == 1000, "the denominator is what makes a rate a measurement"
    assert prevalence["corpus"] == "fixture-corpus"
    assert _metric(rows, "corpus_rows")["value"] == 1000.0
    assert _metric(rows, "configurations_evaluated")["value"] == 3.0
    assert _metric(rows, "corpus_fold_column_agreement")["n"] == 100
    assert not refused or all("typology_recall" in line for line in refused), refused


def test_a_recorded_boolean_lands_as_a_flag_beside_its_own_words() -> None:
    rows, _refused = validation_metric_rows(_ablation_document())

    detected = _metric(rows, "leakage_control_detected")
    assert detected["value"] == 1.0, "a truthy control is a 1.0 with the note beside it, not True"
    assert "lookahead" in str(detected["note"]).lower()


def test_a_figure_two_arms_disagree_on_refuses_by_name() -> None:
    document = _ablation_document()
    document["variants"][1]["base_rate"] = 0.004

    _rows, refused = validation_metric_rows(document)
    assert any("label_prevalence" in line for line in refused), refused
    assert any(
        "Arm B" in line for line in refused
    ), f"the refusal must name the disagreeing arm, or it is a count and not a diagnosis: {refused}"


def test_a_metric_nothing_measured_is_absent_rather_than_zero() -> None:
    document = _ablation_document()
    for variant in document["variants"]:
        del variant["n_total_positive"]
        variant.pop("label_typology_recall", None)
    document.pop("leakage_control", None)

    rows, _refused = validation_metric_rows(document)
    names = {row["name"] for row in rows}
    assert "leakage_control_detected" not in names, (
        "a flag with no recorded value landing as 0.0 would report a leakage control that "
        "did not run as a control that did not detect"
    )
    assert (
        "labelled_positive_rows" not in names
    ), "its value was removed from both arms; a zero would be a measured count of none"
    assert (
        _metric(rows, "label_prevalence")["n"] == 1000
    ), "the metrics the fixture still records must land unchanged by a neighbour's absence"


# --- fairness_row: the card's published axes, not the per-arm blocks ---------


def test_an_axis_the_artifact_marks_unavailable_refuses_with_its_reason() -> None:
    card = {
        "fairness": {
            "protected_attributes_note": "proxy axes, not protected attributes",
            "axes": [
                {
                    "axis": "amount_band",
                    "available": True,
                    "buckets": [
                        {"bucket": "high", "false_positive_rate": 0.2, "n_accounts": 5},
                        {"bucket": "low", "false_positive_rate": 0.1, "n_accounts": 20},
                    ],
                },
                {
                    "axis": "activity_volume",
                    "available": False,
                    "reason": "fewer than 4 non-null values in this corpus",
                },
            ],
        }
    }

    rows, refused = fairness_rows(card)

    assert [(row["axis"], row["bucket"]) for row in rows] == [
        ("amount_band", "high"),
        ("amount_band", "low"),
    ], "rows are ordered by (axis, bucket), which is the table's unique key"
    assert rows[0]["axis_rationale"] == "proxy axes, not protected attributes"
    assert rows[0]["fp_rate"] == 0.2 and rows[0]["n"] == 5
    assert len(refused) == 1 and "activity_volume" in refused[0]
    assert "fewer than 4 non-null values" in refused[0], (
        "the artifact's own reason is the sentence that tells a reviewer the axis is thin, "
        f"not the mapper: {refused}"
    )


def test_a_card_with_no_fairness_block_is_an_error_not_an_empty_page() -> None:
    """§18: a broad exception becoming an empty list is what renders a bug as an empty state."""
    with pytest.raises(LandingError, match="model_card"):
        fairness_rows({})


# --- backtest_fold: honest about the columns nothing measures ----------------


def _fold_document(*, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    fold: dict[str, Any] = {
        "fold_index": 0,
        "embargo_days": 30,
        "n_fit_rows": 100,
        "n_scored_rows": 40,
        "pr_auc": 0.25,
        "precision": 0.5,
        "recall": 0.5,
        "corpus": "fixture-corpus",
        "n_test_positive": 2,
        "skipped_reason": None,
        "alerts_per_10k_accounts": 250.0,
        "typology_recall": {},
        "economics": {"net_benefit_minor": 2500, "currency": "UGX"},
    }
    if extra:
        fold.update(extra)
    return {
        "variants": [
            {
                "row_id": "arm_a",
                "label": "Arm A",
                "corpus": "fixture-corpus",
                "policies": {"score_threshold": {"folds": [fold]}},
            }
        ]
    }


def test_a_fold_the_producer_never_describes_refuses_naming_the_missing_figures() -> None:
    """`backtest_fold` declares its columns NOT NULL; a zero-filled fold would be a claim."""
    rows, refused = backtest_fold_rows(_fold_document())

    assert rows == [], "nine required figures have no producer; a zero-filled fold is a claim"
    assert len(refused) == 1 and "fold 0" in refused[0]
    assert "declared owner" in refused[0], refused


def _owner_fold_document(
    *,
    drop: tuple[str, ...] = (),
    without_windows: bool = False,
    alerts: int = 10,
) -> dict[str, Any]:
    """A complete `full_calibrated` x `ev_cpsat` fold record plus the run's window section.

    Every figure is hand-set so the landed row is checkable on paper: 100 fit rows and 40 scored
    rows, 10 accounts reviewed, 5000 captured minus 2500 cost = 2500 net, precision 5/10 = 0.5,
    recall 5/10 positives = 0.5.
    """
    fold: dict[str, Any] = {
        "fold_index": 2,
        "embargo_days": 30,
        "n_fit_rows": 100,
        "n_scored_rows": 40,
        "n_test_positive": 10,
        "pr_auc": 0.25,
        "auroc": 0.6,
        "brier": 0.00125,
        "max_drawdown_minor": 750,
        "entity_disjoint": False,
        "precision": 0.5,
        "recall": 0.5,
        "corpus": "fixture-corpus",
        "economics": {
            "accounts_reviewed": alerts,
            "captured_value_minor": 5000,
            "cost_minor": 2500,
            "net_benefit_minor": 2500,
            "var95_minor": 3000,
            "es975_minor": 3500,
            "mc_draws": 200,
            "mc_seed": 1337,
            "currency": "UGX",
        },
    }
    for column in drop:
        del fold[column]
    document: dict[str, Any] = {
        "variants": [
            {
                "row_id": "full_calibrated",
                "label": "Full system, calibrated",
                "corpus": "fixture-corpus",
                "policies": {"ev_cpsat": {"folds": [fold]}},
            },
            {
                "row_id": "scorecard_only",
                "label": "Scorecard only (WOE logistic)",
                "corpus": "fixture-corpus",
                # Its OWN record: two arms sharing one dict would let a test mutate the owner it is
                # trying to leave alone, which is exactly the confusion this table exists to remove.
                "policies": {"ev_cpsat": {"folds": [copy.deepcopy(fold)]}},
            },
        ]
    }
    if not without_windows:
        document["fold_windows"] = [
            {
                "fold_index": 2,
                "train_start": "2014-01-02T01:30:57.117619+00:00",
                "train_end": "2015-06-30T23:59:59+00:00",
                "validation_start": "2015-01-04T00:00:00+00:00",
                "embargo_end": "2015-07-30T23:59:59+00:00",
                "test_start": "2015-07-31T00:00:00+00:00",
                "test_end": "2015-12-20T16:45:37.523020+00:00",
            }
        ]
    return document


def test_a_whole_fold_record_lands_one_row_with_the_window_it_was_cut_on() -> None:
    """The fold table's owner, its dates, and the figures the harness reduced over one fold."""
    rows, refused = backtest_fold_rows(_owner_fold_document())

    assert refused == [], refused
    assert rows == [
        {
            "fold_index": 2,
            "corpus": "fixture-corpus",
            "train_start": date(2014, 1, 2),
            "train_end": date(2015, 6, 30),
            "embargo_days": 30,
            "embargo_end": date(2015, 7, 30),
            "test_start": date(2015, 7, 31),
            "test_end": date(2015, 12, 20),
            "n_train": 100,
            "n_test": 40,
            "pr_auc": 0.25,
            "auroc": 0.6,
            "brier": 0.00125,
            "precision_at_budget": 0.5,
            "recall_at_budget": 0.5,
            "precision_undefined": False,
            "alerts": 10,
            "captured_value_minor": 5000,
            "cost_minor": 2500,
            "net_benefit_minor": 2500,
            "max_drawdown_minor": 750,
            "var95_minor": 3000,
            "es975_minor": 3500,
            "mc_runs": 200,
            "mc_seed": 1337,
            "currency": "UGX",
            "entity_disjoint": False,
        }
    ], rows


def test_fold_zero_lands_like_any_other_fold() -> None:
    """The first fold is the one an index lookup is most likely to lose.

    Found while wiring these columns into the real schema: the window join keyed its lookup with
    ``_integer(...) or -1``, which turns a legitimate fold 0 into a miss and quietly refuses the
    fold the run cares most about. A fixture that only ever used index 2 could not see it.
    """
    document = _owner_fold_document()
    for variant in document["variants"]:
        for record in variant["policies"]["ev_cpsat"]["folds"]:
            record["fold_index"] = 0
        document["fold_windows"][0]["fold_index"] = 0

    rows, refused = backtest_fold_rows(document)
    assert refused == [], refused
    assert [row["fold_index"] for row in rows] == [0], rows
    assert rows[0]["train_start"] == date(2014, 1, 2), rows[0]


def test_a_fold_is_owned_by_one_arm_and_policy_rather_than_collapsed_across_both() -> None:
    """Nine arms x three ladders cannot average into one row per fold.

    The EV ladders book different money for the same fold and, since DEV-027, the arms differ by
    model — so averaging them would state a figure no arm produced. The row is owned by the final
    calibrated system under the constrained-optimal queue; everything else belongs to
    ``ablation_row``, which is keyed by variant.
    """
    document = _owner_fold_document()
    # A second arm reporting a DIFFERENT pr_auc for the same fold: a mapper that collapsed sources
    # would have to refuse on disagreement, and one that averaged them would invent a number.
    document["variants"][1]["policies"]["ev_cpsat"]["folds"][0]["pr_auc"] = 0.11

    rows, refused = backtest_fold_rows(document)
    assert refused == [], refused
    assert len(rows) == 1 and rows[0]["pr_auc"] == 0.25, rows
    assert rows[0]["fold_index"] == 2, "one row per fold, not one per arm per ladder"


def test_a_fold_with_no_owner_record_refuses_naming_what_was_seen() -> None:
    """Not every run fits the owner: a fold the headline configuration never scored is refused."""
    document = _owner_fold_document()
    document["variants"] = [
        variant for variant in document["variants"] if variant["row_id"] != "full_calibrated"
    ]

    rows, refused = backtest_fold_rows(document)
    assert rows == []
    assert len(refused) == 1 and "declared owner" in refused[0], refused
    assert "full_calibrated" in refused[0] and "ev_cpsat" in refused[0], refused


def test_a_fold_without_the_serialised_window_is_refused_rather_than_redated() -> None:
    """The boundaries come from the splits module or not at all (DEV-013)."""
    rows, refused = backtest_fold_rows(_owner_fold_document(without_windows=True))

    assert rows == []
    joined = " ".join(refused)
    for column in ("train_start", "train_end", "embargo_end", "test_start", "test_end"):
        assert column in joined, f"{column} should be named: {refused}"


@pytest.mark.parametrize(
    "column",
    ["auroc", "brier", "max_drawdown_minor", "entity_disjoint"],
)
def test_a_fold_figure_the_harness_never_reduced_refuses_by_name(column: str) -> None:
    """Each of the four figures `backtest_fold` needs per fold, and nothing else can supply it.

    An absent per-fold AUROC cannot be filled with the policy aggregate's: five folds sharing one
    run-level number is DEV-027's shape at a different grain.
    """
    rows, refused = backtest_fold_rows(_owner_fold_document(drop=(column,)))

    assert rows == []
    assert any(column in line for line in refused), refused


def test_a_fold_with_no_alerts_reports_undefined_precision_rather_than_zero() -> None:
    rows, refused = backtest_fold_rows(_owner_fold_document(alerts=0))

    assert len(rows) == 1, refused
    assert rows[0]["alerts"] == 0
    assert rows[0]["precision_undefined"] is True, "no denominator, so no rate"
    assert rows[0]["precision_at_budget"] is None and rows[0]["recall_at_budget"] is None


def test_a_document_without_fold_records_is_refused_as_a_missing_artifact() -> None:
    with pytest.raises(LandingError, match="ablation_results.json"):
        backtest_fold_rows({"variants": []})


# --- perturbation_row: the check's own figures, and no family guessed at -----


def _perturbation_card() -> dict[str, Any]:
    return {
        "seed": 7,
        "perturbations": {
            "amount_shift_minus10": {
                "shift_ratio": -0.1,
                "spearman": 0.99,
                "reordered_at_cutoff": False,
            },
            "edge_drop_typology_recall": {
                "drop_ratio": 0.1,
                "max_abs_shift": 0.0,
                "seed": 11,
                "caveat": "proxy",
            },
            "unmapped_kind": {"whatever": 1},
        },
    }


def test_each_ran_check_lands_and_an_undocumented_family_refuses() -> None:
    rows, refused = perturbation_rows(_perturbation_card())

    assert [(row["kind"], row["magnitude"], row["result"]) for row in rows] == [
        ("amount_shift_minus10", -0.1, 0.99),
        ("edge_drop_typology_recall", 0.1, 0.0),
    ], "sorted by kind, and a zero shift is a measured result rather than a missing one"
    assert rows[0]["unit"] == "spearman rho" and rows[1]["unit"] == "recall share"
    assert rows[0]["seed"] == 7, "the run's recorded seed, stated once in the document"
    assert rows[1]["seed"] == 11, "a check that carries its own seed does not inherit the run's"
    assert rows[0]["note"] == "reordered_at_cutoff=False"
    assert (
        refused
        == [reason for reason in refused if "unmapped_kind" in reason and "source map" in reason]
        and len(refused) == 1
    ), f"a family with no declared mapping cannot say which of its figures is the magnitude: {refused}"


def test_a_card_with_no_perturbations_block_is_an_error_not_an_empty_table() -> None:
    with pytest.raises(LandingError, match="model_card"):
        perturbation_rows({})
