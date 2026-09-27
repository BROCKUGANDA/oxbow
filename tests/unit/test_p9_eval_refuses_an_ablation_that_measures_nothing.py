"""DEV-027 as a gate instead of an incident: a table that claims channels and reports one number.

The first real walk-forward published nine model-shaped rows whose PR-AUC, AUROC and Brier were
byte-identical, because one scorer answered for all of them. The per-row scorer that replaced it
(``PROFILES`` + ``SharedFoldRuns``) can fail in the same shape for a different reason — a fold
cache that keys on the wrong identity would hand two rows one fitted channel — and every number
would still be arithmetically correct. Nobody reading a card would see it; the only reason it was
seen the first time is that a human compared columns.

So the publisher compares them. :func:`_refuse_an_ablation_table_whose_model_rows_do_not_differ`
runs before any document is written, and only for artifacts that declare a channel per row: an
artifact from before per-row profiling declares none, and its limitation is the sentence the
packet already carries rather than a reason to stop the build.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from oxbow.eval import _refuse_an_ablation_table_whose_model_rows_do_not_differ  # noqa: E402

# Five channels, so the check applies at all: the gate is about a model ablation, and an
# artifact that declares fewer than five has never claimed to be one.
PROFILES = {
    "scorecard_only": "scorecard",
    "gbm_no_graph": "gbm_no_graph",
    "gbm_with_graph": "gbm",
    "plus_ifusion": "fused_uncalibrated",
    "full_calibrated": "calibrated",
    "threshold_vs_ev": "calibrated",
}
_MEASURES = ("pr_auc", "brier", "auroc_comparability_only")


def _variant(row_id: str, *, pr_auc: float, is_control: bool = False) -> dict[str, Any]:
    return {
        "row_id": row_id,
        "is_control": is_control,
        "policies": {
            "score_threshold": {
                "pr_auc": pr_auc,
                "brier": 0.001,
                "auroc_comparability_only": 0.61,
            }
        },
    }


def _document(
    variants: list[dict[str, Any]], *, profiles: dict[str, str] | None = PROFILES
) -> dict[str, Any]:
    return {"ablation_profiles": dict(profiles or {}), "variants": variants}


# ---------------------------------------------------------------------------


def test_two_rows_on_different_channels_that_report_one_measurement_are_refused() -> None:
    """The exact DEV-027 shape, now caught by the publisher rather than by a reader."""
    document = _document(
        [
            _variant("scorecard_only", pr_auc=0.059115),
            _variant("gbm_with_graph", pr_auc=0.059115),
        ]
    )

    with pytest.raises(FileNotFoundError) as caught:
        _refuse_an_ablation_table_whose_model_rows_do_not_differ(document)

    message = str(caught.value)
    for fragment in ("scorecard_only", "gbm_with_graph", "profile scorecard", "profile gbm"):
        assert fragment in message, message
    # All three columns are named: one matching column is a coincidence, three is a shared fit.
    for column in _MEASURES:
        assert column in message, message


def test_rows_that_differ_are_published() -> None:
    """The gate must not be an equality detector: it fires on identity, not on unlikeliness."""
    _refuse_an_ablation_table_whose_model_rows_do_not_differ(
        _document(
            [
                _variant("scorecard_only", pr_auc=0.0591),
                _variant("gbm_with_graph", pr_auc=0.0612),
                _variant("gbm_no_graph", pr_auc=0.0588),
            ]
        )
    )


def test_rows_that_declare_the_same_channel_are_allowed_to_agree() -> None:
    """threshold-vs-EV and full-calibrated are one model under two policy ladders, by design.

    Their discrimination columns are the same measurement and the generated caveat says so; a
    gate that refused them would refuse every honest run and teach the operator to delete it.
    """
    _refuse_an_ablation_table_whose_model_rows_do_not_differ(
        _document(
            [
                _variant("full_calibrated", pr_auc=0.059115),
                _variant("threshold_vs_ev", pr_auc=0.059115),
            ]
        )
    )


def test_an_artifact_that_declares_no_channels_is_left_to_its_own_limitation() -> None:
    """The published 40k table predates per-row scoring and must still be publishable."""
    _refuse_an_ablation_table_whose_model_rows_do_not_differ(
        _document(
            [
                _variant("scorecard_only", pr_auc=0.059115),
                _variant("gbm_with_graph", pr_auc=0.059115),
            ],
            profiles=None,
        )
    )


def test_an_artifact_declaring_fewer_than_five_channels_is_not_a_model_ablation() -> None:
    """Three channels cannot be judged for model distinctness; refusing would police the wrong thing."""
    _refuse_an_ablation_table_whose_model_rows_do_not_differ(
        _document(
            [
                _variant("scorecard_only", pr_auc=0.059115),
                _variant("gbm_with_graph", pr_auc=0.059115),
            ],
            profiles={"scorecard_only": "scorecard", "gbm_with_graph": "scorecard"},
        )
    )


def test_the_control_arm_is_never_part_of_the_comparison() -> None:
    """The lookahead control is a different scorer on purpose; matching it proves nothing."""
    _refuse_an_ablation_table_whose_model_rows_do_not_differ(
        _document(
            [
                _variant("scorecard_only", pr_auc=0.059115),
                _variant("leakage_control", pr_auc=0.059115, is_control=True),
            ]
        )
    )


def test_a_missing_discrimination_column_does_not_count_as_agreement() -> None:
    """Two rows both reporting None did not measure the same thing; they measured nothing."""
    first = _variant("scorecard_only", pr_auc=0.059115)
    second = _variant("gbm_with_graph", pr_auc=0.059115)
    for agg in (first["policies"]["score_threshold"], second["policies"]["score_threshold"]):
        agg["brier"] = None
        agg["auroc_comparability_only"] = None
    _refuse_an_ablation_table_whose_model_rows_do_not_differ(_document([first, second]))
