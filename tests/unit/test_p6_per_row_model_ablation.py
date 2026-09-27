"""Each ablation row is scored by the model its label names, and says so in its own artifact.

DEV-027 is the defect this file exists to stop. The first real walk-forward published nine
variants whose PR-AUC, AUROC and Brier were byte-identical, because one scorer answered for
every row and the rows differed only by policy ladder — while their labels ("Scorecard only",
"LightGBM with graph features") named *models*. A reader of the card took identical columns as
evidence that the graph adds nothing, the exact opposite of the thesis the build rests on.

The fix has two halves, and this file pins both:

1. **A row reads the column its own model produced.** ``PROFILES`` maps a profile to one fitted
   channel of the fold; ``WalkForwardScorer.scores_from`` reads that channel and nothing else.
   The five model rows therefore map to five different fitted objects — the WOE logistic, the
   booster on every feature, the booster refitted without the graph groups, the uncalibrated
   meta-learner, and the calibrated one — at the cost of one extra booster fit per fold rather
   than five whole stacks.
2. **The disclosure is generated from the mapping.** ``ablation_caveat`` used to be a sentence
   someone maintained by hand; it now comes out of :data:`ABLATION_PROFILES`, so the only way to
   change what the artifact admits is to change what the rows do.

The refusal is the load-bearing part. A profile whose column the fold did not publish raises by
name instead of borrowing another channel's number: that is the one move that would recreate
DEV-027 while looking like it had been fixed, so every test that expects a number here is worth
less than the tests that expect a stop.
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from oxbow.backtest.ablation import ROW_IDS  # noqa: E402
from oxbow.backtest.run import ABLATION_PROFILES, _ablation_caveat  # noqa: E402
from oxbow.models.errors import ModelLayerError  # noqa: E402
from oxbow.models.run import (  # noqa: E402
    P_FUSED_COLUMN,
    P_FUSED_RAW_COLUMN,
    P_GBM_COLUMN,
    P_GBM_NO_GRAPH_COLUMN,
    P_SCORECARD_COLUMN,
    FoldModelRunner,
    FoldRun,
)
from oxbow.models.scorer import (  # noqa: E402
    PROFILES,
    ProfileUnavailableError,
    WalkForwardScorer,
)

# Hand-set so the arithmetic is checkable on paper: five channels of one fold, five
# deliberately different probabilities, one row per account.
CHANNEL_VALUES = {
    P_SCORECARD_COLUMN: 0.10,
    P_GBM_COLUMN: 0.20,
    P_GBM_NO_GRAPH_COLUMN: 0.05,
    P_FUSED_RAW_COLUMN: 0.30,
    P_FUSED_COLUMN: 0.40,
}
ACCOUNTS = ("acct-a", "acct-b")
START = datetime(2024, 1, 1, tzinfo=UTC)


class _FoldStub:
    """The read surface ``scores_from`` uses off a ``FoldRun``, with no model fitted.

    Built from the dataclass's own field list, which the last test in this file asserts still
    covers what the projection reads — so a rename in ``models.run`` fails a test rather than
    silently widening this stub.
    """

    def __init__(
        self,
        scored: pl.DataFrame,
        *,
        mode: str = "full_model_stack",
        channel_skips: dict[str, str] | None = None,
    ) -> None:
        self.scored = scored
        self.fold = 0
        self.mode = mode
        self.channel_skips = channel_skips or {}
        self.lineage = None
        self.gbm = None


def _fold_frame(*, drop: str | None = None, values: dict[str, float] | None = None) -> pl.DataFrame:
    """One fold's scored rows: test-role accounts carrying every channel column."""
    columns = {
        "account_key": list(ACCOUNTS),
        "as_of_ts": [START for _ in ACCOUNTS],
        "role": ["test"] * len(ACCOUNTS),
        "feature_spec_hash": ["f" * 64] * len(ACCOUNTS),
    }
    for name, value in (values or CHANNEL_VALUES).items():
        columns[name] = [value] * len(ACCOUNTS)
    frame = pl.DataFrame(columns)
    if drop is not None:
        frame = frame.drop(drop)
    return frame


# ---------------------------------------------------------------------------


def test_the_model_rows_name_five_different_channels() -> None:
    """The gate DEV-027 needed: five model-shaped rows, five distinct fitted objects.

    Asserted against the row ids the plan fixes, not against a count, so a merge that renames
    or drops a row cannot quietly satisfy it by leaving two rows on one column.
    """
    model_rows = (
        "scorecard_only",
        "gbm_no_graph",
        "gbm_with_graph",
        "plus_ifusion",
        "full_calibrated",
    )
    columns = [PROFILES[ABLATION_PROFILES[row]] for row in model_rows]
    assert columns == [
        P_SCORECARD_COLUMN,
        P_GBM_NO_GRAPH_COLUMN,
        P_GBM_COLUMN,
        P_FUSED_RAW_COLUMN,
        P_FUSED_COLUMN,
    ]
    assert len(set(columns)) == len(
        columns
    ), f"two model rows read the same channel, so their metrics are one measurement: {columns}"
    assert set(ABLATION_PROFILES) == set(ROW_IDS), "every plan §12 row declares a profile"


def test_one_fold_answers_six_rows_with_six_different_numbers() -> None:
    """The imitation this replaces: identical rows under different labels."""
    fold = _FoldStub(_fold_frame())
    seen = {
        profile: WalkForwardScorer.scores_from(fold, profile=profile).p_of("acct-a")
        for profile in PROFILES
    }
    assert seen == {
        "scorecard": 0.10,
        "gbm": 0.20,
        "gbm_no_graph": 0.05,
        "fused_uncalibrated": 0.30,
        "calibrated": 0.40,
    }
    assert len(set(seen.values())) == len(
        seen
    ), "five channels that produce one number is the DEV-027 table, not an ablation"


def test_a_channel_the_fold_did_not_fit_refuses_instead_of_borrowing() -> None:
    """The load-bearing refusal: a missing column is reported, not replaced.

    The with-graph row falling back to the calibrated column would reproduce the original
    defect exactly — a model-shaped label quoting another model's number — and it would do so
    only on a fold that degraded, which is to say in production and not in the fixture.
    """
    fold = _FoldStub(
        _fold_frame(drop=P_GBM_NO_GRAPH_COLUMN),
        mode="scorecard_and_rules_only",
        channel_skips={"gbm_no_graph": "gbm fit refused: too few positives"},
    )
    with pytest.raises(ProfileUnavailableError) as caught:
        WalkForwardScorer.scores_from(fold, profile="gbm_no_graph")

    message = str(caught.value)
    assert "gbm_no_graph" in message and P_GBM_NO_GRAPH_COLUMN in message
    assert "scorecard_and_rules_only" in message, "the refusal has to name the fold's mode"
    assert "too few positives" in message, "and the skip the fold already recorded"

    # Every other channel still resolves on the same fold: the refusal is scoped to the row.
    assert WalkForwardScorer.scores_from(fold, profile="calibrated").p_of("acct-a") == 0.40


@pytest.mark.parametrize("bad", [1.5, -0.2, float("nan"), float("inf")])
def test_a_column_that_is_not_a_probability_refuses_rather_than_prices_the_queue(
    bad: float,
) -> None:
    """The economics multiply p by exposure, so an out-of-range channel is not a rounding issue."""
    values = dict(CHANNEL_VALUES)
    values[P_GBM_COLUMN] = bad
    fold = _FoldStub(_fold_frame(values=values))
    with pytest.raises(ProfileUnavailableError, match="outside \\[0, 1\\]|null"):
        WalkForwardScorer.scores_from(fold, profile="gbm")


def test_an_unknown_profile_is_refused_before_any_column_is_read() -> None:
    """A typo in the row map must not become 'whichever channel happened to be there'."""
    fold = _FoldStub(_fold_frame())
    with pytest.raises(ProfileUnavailableError, match="unknown ablation profile"):
        WalkForwardScorer.scores_from(fold, profile="with_graph")


def test_the_projection_never_consults_a_second_channel() -> None:
    """A fallback is a fallback even when it picks a *better* number.

    ``p_fused`` used to be tried first and ``p_scorecard`` second. Here the fold's calibrated
    channel is intact and its scorecard channel is the one asked for: reading 0.40 would be
    defensible-looking and wrong.
    """
    fold = _FoldStub(_fold_frame())
    assert WalkForwardScorer.scores_from(fold, profile="scorecard").p_of("acct-b") == 0.10


def test_a_fold_whose_rows_disagree_on_the_spec_hash_is_refused() -> None:
    """One row fitted on spec A and another on spec B is not one measurement of anything."""
    frame = _fold_frame().with_columns(pl.Series("feature_spec_hash", ["a" * 64, "b" * 64]))
    with pytest.raises(Exception, match="feature spec|feature_spec_hash"):
        WalkForwardScorer.scores_from(_FoldStub(frame), profile="calibrated")


# ---------------------------------------------------------------------------
# The runner's half: the graph-free booster is a second fit, not a re-labelled first one.


def _runner_for_groups(groups: tuple[str, ...]) -> FoldModelRunner:
    from oxbow.models.config import load_model_config, load_split_config
    from oxbow.scoring.config import load_feature_registry, load_scorecard_config

    registry = load_feature_registry(REPO_ROOT)
    return FoldModelRunner(
        model_cfg=load_model_config(REPO_ROOT),
        scorecard_cfg=load_scorecard_config(REPO_ROOT),
        split_cfg=load_split_config(REPO_ROOT),
        feature_registry=registry,
        rules_provider=_NoRules(),
        provenance="test",
        trial_budget=1,
        explain=False,
        root=REPO_ROOT,
        ablate_feature_groups=groups,
    )


class _NoRules:
    def rule_hits(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}


def test_the_ablated_groups_come_from_the_registry_and_not_a_name_match() -> None:
    """'graph features' means the groups P2 declares, so the row cannot silently ablate zero."""
    runner = _runner_for_groups(("graph_local", "graph_global", "graph_typology"))
    kept, removed = runner.ablated_feature_names()

    assert removed, "the config names graph groups and they must publish features"
    assert set(removed) & set(kept) == set()
    survivors = {name for name in kept if name.startswith("graph_")}
    assert not survivors, f"a graph feature survived the ablation: {sorted(survivors)}"
    assert len(kept) < len(runner.feature_registry.names)


def test_an_unknown_group_is_refused_at_construction_not_at_the_row() -> None:
    """A group that does not exist would ablate nothing and still print a row."""
    with pytest.raises(ModelLayerError, match="does not define"):
        _runner_for_groups(("graph_whoops",))


def test_a_group_that_publishes_no_feature_is_refused_at_the_fit() -> None:
    """An empty group passes construction and would re-fit the identical model under a new name."""
    with pytest.raises(ModelLayerError, match="declares no features"):
        _runner_with_groups({"empty_group": ()}).ablated_feature_names()


def _runner_with_groups(extra: dict[str, tuple[str, ...]]) -> FoldModelRunner:
    from oxbow.models.config import load_model_config, load_split_config
    from oxbow.scoring.config import load_feature_registry, load_scorecard_config

    registry = load_feature_registry(REPO_ROOT)
    widened = dataclasses.replace(registry, groups={**registry.groups, **extra})
    return FoldModelRunner(
        model_cfg=load_model_config(REPO_ROOT),
        scorecard_cfg=load_scorecard_config(REPO_ROOT),
        split_cfg=load_split_config(REPO_ROOT),
        feature_registry=widened,
        rules_provider=_NoRules(),
        provenance="test",
        trial_budget=1,
        explain=False,
        root=REPO_ROOT,
        ablate_feature_groups=tuple(extra),
    )


# ---------------------------------------------------------------------------
# The artifact's own sentence, generated rather than maintained.


def test_the_caveat_states_the_rows_it_came_from_and_not_the_old_admission() -> None:
    """The old caveat said the rows did NOT differ by model. That sentence is now false.

    Generated text goes stale silently, so the check is on the specific lie as well as the
    specific truth: every row id with its column, and no claim that feature subsetting is
    still outstanding.
    """
    text = _ablation_caveat()
    for row in ROW_IDS:
        assert f"{row}={ABLATION_PROFILES[row]}" in text, text
    for stale in ("not yet by feature subset", "named here rather than faked"):
        assert stale not in text, f"the caveat still carries the superseded admission: {stale!r}"
    assert "config/splits.yaml" in text, "it has to name where the graph groups come from"
    assert "was NOT run on the IBM-AML corpus" in text, (
        "the row labelled as a cross-corpus transfer check is scored on the same corpus as "
        "the rest of the table, and the reader can only learn that from this sentence"
    )


def test_the_caveat_names_the_rows_that_share_a_column() -> None:
    """rules_only, threshold_vs_ev and full_calibrated are one channel; the sentence says so."""
    text = _ablation_caveat()
    for row in ("rules_only", "threshold_vs_ev", "full_calibrated"):
        assert row in text
    assert "Rows sharing one probability column:" in text
    tail = text.split("Rows sharing one probability column:", 1)[1]
    head = tail.split(";", 1)[0]
    for row in ("rules_only", "threshold_vs_ev", "full_calibrated"):
        assert row in head, f"{row} shares the calibrated column and is not admitted: {head}"
    for row in ("scorecard_only", "gbm_no_graph", "gbm_with_graph", "plus_ifusion"):
        assert row not in head


def test_fold_run_still_carries_every_field_the_projection_reads() -> None:
    """The stub above is only honest while the real type still has these fields."""
    fields = {field.name for field in dataclasses.fields(FoldRun)}
    assert {"scored", "fold", "mode", "channel_skips", "lineage", "gbm"} <= fields
