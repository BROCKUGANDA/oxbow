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

DEV-033 is what that stop cost: the 40k walk-forward re-run hit the refusal on the two folds
whose booster the GBM's own average-precision guard had refused, the fold chain unwound past the
artifact write, and the command exited 0 having written nothing — no `fold_windows`, no
`backtest_fold` rows, no demo. The guard stays exactly where it is. What the tests below now also
pin is the half that was missing: the ablation harness converts *that one* refusal into an
unavailable cell carrying the fold's own ``scoring_mode`` and skip reason, the run completes, the
artifact lands with its fold windows, and the unavailable cell is still distinguishable from a
measured one and from a zero.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from oxbow.backtest import fakes  # noqa: E402
from oxbow.backtest.ablation import (  # noqa: E402
    ABLATION_ROWS,
    ROW_IDS,
    build_ablation,
    check_leakage_control,
)
from oxbow.backtest.harness import VariantResult, variant_to_dict  # noqa: E402
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
from tests.unit.p6_fixtures import fast_config  # noqa: E402

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


# ---------------------------------------------------------------------------
# DEV-033: a fold that cannot support one model channel reports it UNAVAILABLE and the run
# still completes, still lands its artifact, and still publishes everything it measured.
#
# The refusal above is the producer's guard and stays. What the ablation harness changed is
# what happens after it: the chain used to unwind past `serialize.write_json`, and the 40k
# re-run exited 0 having written nothing — no fold_windows, so no `backtest_fill`, no demo.
# Catching the refusal and filling the cell with the full stack's number is the substitution
# DEV-027 removed, so every test below checks both halves: the run completed, AND the
# unavailable cell is distinguishable from a measured one (and from zero).
# ---------------------------------------------------------------------------

DEGRADED_MODE: str = "scorecard_and_rules_only"
GUARD_REASON: str = "gbm fit refused: average_precision disagrees on the validation slice"
CHANNELS_ON_A_FULL_FOLD: tuple[str, ...] = tuple(PROFILES.values())
CHANNELS_ON_A_DEGRADED_FOLD: tuple[str, ...] = (
    P_SCORECARD_COLUMN,
    P_FUSED_RAW_COLUMN,
    P_FUSED_COLUMN,
)
# How strongly each channel tracks the corpus's hidden signal. Different per channel because
# they ARE different models: one quality for all five would re-create the identical-column
# defect this file exists to stop, inside the fixture.
CHANNEL_QUALITY: dict[str, float] = {
    P_SCORECARD_COLUMN: 0.20,
    P_GBM_NO_GRAPH_COLUMN: 0.35,
    P_GBM_COLUMN: 0.55,
    P_FUSED_RAW_COLUMN: 0.70,
    P_FUSED_COLUMN: 0.85,
}


def _hash01(key: str, salt: str) -> float:
    """Deterministic noise in [0, 1), so the fake probabilities are not a label read."""
    digest = hashlib.sha256(f"{salt}|{key}".encode()).hexdigest()
    return int(digest[:12], 16) / float(1 << 48)


def _channel_value(account: str, channel: str, signal: float) -> float:
    quality = CHANNEL_QUALITY[channel]
    mixed = quality * signal + (1.0 - quality) * _hash01(account, channel)
    return min(0.95, max(0.05, mixed))


@dataclasses.dataclass(frozen=True)
class _FoldRunAt:
    """A ``FoldRun`` surface for fold ``index`` — the projection reads it, nothing is fitted."""

    scored: pl.DataFrame
    fold: int
    mode: str
    channel_skips: dict[str, str]
    lineage: None = None
    gbm: None = None


class _DegradedFoldScorer:
    """Stand-in for ``WalkForwardScorer.fold_run`` on a corpus whose folds degrade.

    It publishes exactly what ``oxbow.models.run`` publishes: every channel on a
    ``full_model_stack`` fold, and on a ``scorecard_and_rules_only`` fold only the columns
    ``with_degraded_channels`` writes — the graph-free booster is simply absent, because the
    GBM's own average-precision guard refused the fold. The projection that reads these runs
    is the real one, so the refusal this produces is the production refusal.
    """

    def __init__(
        self,
        *,
        degraded: set[int],
        fold_starts: dict[datetime, int],
        signal_by_account: dict[str, float],
    ) -> None:
        self._degraded = set(degraded)
        self._fold_starts = fold_starts
        self._signal = signal_by_account

    def fold_run(
        self,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> _FoldRunAt:
        del train, validation, seed, feature_spec_hash
        fold = self._fold_starts[scored.get_column("as_of_ts").min()]
        degraded = fold in self._degraded
        accounts = [str(row["account_key"]) for row in scored.to_dicts()]
        columns: dict[str, list[object]] = {
            "account_key": accounts,
            "as_of_ts": scored.get_column("as_of_ts").to_list(),
            "role": ["test"] * len(accounts),
            "feature_spec_hash": [fakes.FAKE_SPEC_HASH] * len(accounts),
        }
        channels = CHANNELS_ON_A_DEGRADED_FOLD if degraded else CHANNELS_ON_A_FULL_FOLD
        for channel in channels:
            columns[channel] = [
                _channel_value(account, channel, self._signal[account]) for account in accounts
            ]
        frame = pl.DataFrame(columns)
        if degraded:
            return _FoldRunAt(
                scored=frame,
                fold=fold,
                mode=DEGRADED_MODE,
                channel_skips={
                    "gbm": GUARD_REASON,
                    P_GBM_NO_GRAPH_COLUMN: f"{P_GBM_NO_GRAPH_COLUMN} fit refused: no gbm channel",
                    "fusion": "unavailable without a gbm channel",
                    "calibration": "unavailable without a fused score",
                },
            )
        return _FoldRunAt(scored=frame, fold=fold, mode="full_model_stack", channel_skips={})


@dataclasses.dataclass(frozen=True)
class _StubFold:
    """The window bounds ``fold_windows`` reads off ``oxbow.backtest.splits``' Fold."""

    index: int

    @property
    def train_start_ts(self) -> datetime:
        return START + timedelta(days=365 * self.index)

    @property
    def train_end_ts(self) -> datetime:
        return START + timedelta(days=365 * self.index + 200)

    @property
    def validation_start_ts(self) -> datetime:
        return START + timedelta(days=365 * self.index + 215)

    @property
    def embargo_band(self) -> tuple[datetime, datetime]:
        low = START + timedelta(days=365 * self.index + 230)
        return (low, low + timedelta(days=30))

    @property
    def test_start_ts(self) -> datetime:
        return START + timedelta(days=365 * self.index + 261)

    @property
    def test_end_ts(self) -> datetime:
        return START + timedelta(days=365 * self.index + 364)

    @property
    def purge_days(self) -> int:
        return 5

    @property
    def label_window_days(self) -> int:
        return 90


@dataclasses.dataclass(frozen=True)
class _StubPlan:
    """A five-fold plan: the shape ``fold_windows`` and the printed header read."""

    folds: tuple[_StubFold, ...]
    embargo_days: int = 30

    def as_report_line(self) -> str:
        return "which_split_was_optimised_on: temporal_walk_forward (stub plan)"


def _degraded_ablation(degraded: set[int]) -> list[VariantResult]:
    """Run the real eight-row ablation over a five-fold plan whose `degraded` folds refuse the GBM.

    Nothing here re-implements the harness or the projection: the fold scorer is stubbed (no
    LightGBM is fitted), and :class:`SharedFoldRuns`, :class:`ProfileScorer`,
    ``WalkForwardScorer.scores_from``, ``run_variant`` and ``build_ablation`` are the production
    objects, so the completion path under test is the one that wrote nothing on 2026-09-28.
    """
    from oxbow.backtest.ablation import AblationSpec
    from oxbow.backtest.run import _REAL_POLICIES_BY_ROW, ProfileScorer, SharedFoldRuns

    config = fast_config()
    corpus = fakes.make_corpus(n_accounts=500, span_days=500, with_typology=True)
    masks = fakes.make_fold_masks(
        height=corpus.height, n_folds=config.n_folds, embargo_days=config.embargo_days
    )
    fold_starts = {
        corpus.filter(pl.Series(fold.test_mask)).get_column("as_of_ts").min(): fold.index
        for fold in masks
    }
    signal_by_account = {str(row["account_key"]): float(row["signal"]) for row in corpus.to_dicts()}
    runs = SharedFoldRuns(
        _DegradedFoldScorer(
            degraded=degraded, fold_starts=fold_starts, signal_by_account=signal_by_account
        )
    )
    specs = [
        AblationSpec(
            row_id=row_id,
            label=label,
            question=question,
            scorer=ProfileScorer(runs, ABLATION_PROFILES[row_id]),
            policies=_REAL_POLICIES_BY_ROW[row_id],
            corpus_name="real-corpus",
        )
        for row_id, label, question in ABLATION_ROWS
    ]
    return build_ablation(
        specs,
        corpus=corpus,
        corpus_name="real-corpus",
        provenance="real_corpus",
        fold_provider=fakes.FakeFoldProvider(masks, embargo_days=config.embargo_days),
        config=config,
        allocator=fakes.GreedyAllocator(),
        rule_hits=fakes.FakeRuleHits(),
    )


def _row(variants: list[VariantResult], row_id: str) -> VariantResult:
    return next(variant for variant in variants if variant.row_id == row_id)


def test_a_degraded_fold_marks_that_channel_unavailable_and_the_run_does_not_raise() -> None:
    """The DEV-033 case: folds 2 and 3 refused the booster, so the row says so and moves on.

    Before this change the same fold raised ``ProfileUnavailableError`` up through ``run_real``
    and the command exited 0 with ``wrote NOTHING``. The refusal is still produced by the guard;
    what is new is that the harness converts it into a state on the row.
    """
    variants = _degraded_ablation({2, 3})
    assert {variant.row_id for variant in variants} == set(
        ROW_IDS
    ), "every plan §12 row is still published: an unavailable channel narrows a cell, not a table"

    availability = _row(variants, "gbm_no_graph").availability
    assert availability["status"] == "partial"
    assert availability["profile"] == "gbm_no_graph"
    assert availability["folds_measured"] == [0, 1, 4]
    assert availability["measured_count"] == 3
    assert availability["unavailable_count"] == 2
    refused = availability["folds_unavailable"]
    assert [entry["fold_index"] for entry in refused] == [2, 3]
    for entry in refused:
        assert entry["scoring_mode"] == DEGRADED_MODE, "the cell names the fold's own mode"
        assert GUARD_REASON in str(entry["reason"]), "and the guard that refused the fold"
        assert "p_gbm_no_graph" in entry["reason"]


def test_the_degraded_fold_still_publishes_its_measurable_rows_with_their_real_numbers() -> None:
    """The run publishes everything it can measure: the other channels are untouched."""
    variants = _degraded_ablation({2, 3})
    for row_id in ("rules_only", "scorecard_only", "plus_ifusion", "full_calibrated"):
        variant = _row(variants, row_id)
        assert variant.availability["status"] == "measured", variant.availability
        assert variant.availability["folds_measured"] == [0, 1, 2, 3, 4]
        for aggregate in variant.policies.values():
            assert len(aggregate.folds) == 5, f"{row_id}/{aggregate.policy} lost a fold it measured"
            assert aggregate.pr_auc is not None
    # The rows whose channel two folds lacked are still measured where the model exists — over
    # three folds, with the count beside the number rather than hidden under a five-fold label.
    partial = _row(variants, "gbm_no_graph").policies["ev_greedy"]
    assert len(partial.folds) == 3
    assert partial.pr_auc is not None


def test_an_unavailable_channel_is_never_equal_to_another_rows_number() -> None:
    """DEV-027's distinctness, kept: no cell is another channel's measurement wearing a label.

    Two GBM rows on a corpus where both degrade report nothing; the measured rows report five
    different numbers. Neither case may be confused with the table this file exists to stop.
    """
    variants = _degraded_ablation({2, 3})
    measured_columns = {
        row_id: _row(variants, row_id).policies["score_threshold"].pr_auc
        for row_id in ("scorecard_only", "gbm_no_graph", "gbm_with_graph", "plus_ifusion")
    }
    assert all(value is not None for value in measured_columns.values()), measured_columns
    assert len(set(measured_columns.values())) == len(measured_columns), (
        "two model rows reporting one PR-AUC is the DEV-027 table: " f"{measured_columns}"
    )

    everything = _degraded_ablation({0, 1, 2, 3, 4})
    dead = _row(everything, "gbm_no_graph")
    assert dead.policies == {}, "a row with no measurable channel publishes no aggregate at all"
    assert dead.availability["status"] == "unavailable"
    assert dead.availability["folds_measured"] == []
    assert dead.availability["unavailable_count"] == 5
    assert (
        _policy_numbers(dead) == []
    ), "an unavailable row publishes no numbers, so it equals nothing"
    survivor = _row(everything, "scorecard_only").policies["score_threshold"].pr_auc
    assert survivor is not None and survivor != 0.0
    assert survivor not in _policy_numbers(dead)


def _policy_numbers(variant: VariantResult) -> list[Any]:
    """Every discrimination figure the row published, for the not-a-zero assertions."""
    out: list[Any] = []
    for aggregate in variant.policies.values():
        out.extend([aggregate.pr_auc, aggregate.auroc, aggregate.brier])
    return out


def test_a_row_that_measured_nothing_is_undefined_with_its_count_and_never_a_zero() -> None:
    """`undefined`, counted — the shape `precision_undefined` already uses for an empty fold."""
    variants = _degraded_ablation({0, 1, 2, 3, 4})
    dead = _row(variants, "gbm_no_graph")
    assert dead.fold_count == 5, "the plan is still five folds; only this channel is missing"
    assert dead.availability["note"] == (
        "row 'gbm_no_graph': 0 of 5 folds measured — no metric on this row is a measurement, "
        "and none is reported as zero"
    )
    rendered = variant_to_dict(dead)
    assert rendered["policies"] == {}
    assert rendered["channel_availability"]["status"] == "unavailable"
    assert len(rendered["channel_availability"]["folds_unavailable"]) == 5
    # The measurable rows in the same run keep their numbers, so the refusal is scoped to the row.
    assert _row(variants, "full_calibrated").policies["ev_cpsat"].pr_auc is not None


def test_the_conversion_catches_only_a_fold_refusal_and_never_a_contract_break() -> None:
    """No catch-and-fill: anything other than 'this fold never fitted that channel' still aborts.

    A null in a published column, or a profile the run never declared, is a build fault. Silently
    turning those into unavailable cells would look like this fix and be the lie again.
    """
    from oxbow.backtest.run import ProfileScorer, SharedFoldRuns

    runs = SharedFoldRuns(_DegradedFoldScorer(degraded={2}, fold_starts={}, signal_by_account={}))

    class _NullChannel:
        """A fold that DID publish `p_gbm_no_graph` and put nothing in it."""

        def fold_run(self, **kwargs: Any) -> _FoldRunAt:
            del kwargs
            frame = _fold_frame(values={**CHANNEL_VALUES, P_GBM_NO_GRAPH_COLUMN: 0.20}).drop(
                P_GBM_NO_GRAPH_COLUMN
            )
            frame = frame.with_columns(pl.Series(P_GBM_NO_GRAPH_COLUMN, [None, None]))
            return _FoldRunAt(scored=frame, fold=2, mode="full_model_stack", channel_skips={})

    scorer = ProfileScorer(SharedFoldRuns(_NullChannel()), "gbm_no_graph")
    with pytest.raises(ProfileUnavailableError, match="is null"):
        scorer.score(
            train=pl.DataFrame(),
            validation=pl.DataFrame(),
            scored=pl.DataFrame(),
            feature_spec_hash="f" * 64,
            seed=1,
        )
    assert runs.fits == 0

    # An unknown profile is a typo in the row map, not a fact about the corpus: it aborts.
    class _Fine:
        def fold_run(self, **kwargs: Any) -> _FoldStub:
            del kwargs
            return _FoldStub(_fold_frame())

    typo = ProfileScorer(SharedFoldRuns(_Fine()), "with_graph")
    with pytest.raises(ProfileUnavailableError, match="unknown ablation profile"):
        typo.score(
            train=pl.DataFrame(),
            validation=pl.DataFrame(),
            scored=pl.DataFrame(),
            feature_spec_hash="f" * 64,
            seed=1,
        )


def test_the_written_artifact_reloads_with_the_unavailability_intact_and_its_fold_windows(
    tmp_path: Path,
) -> None:
    """The completion path, in one assertion: the artifact lands, and it says what it could not."""
    from oxbow.backtest.harness import assemble_run
    from oxbow.backtest.model_card import build_model_card_payload
    from oxbow.backtest.run import build_real_payload
    from oxbow.backtest.serialize import write_json

    config = fast_config()
    corpus = fakes.make_corpus(n_accounts=500, span_days=500, with_typology=True)
    variants = _degraded_ablation({2, 3})
    plan = _StubPlan(folds=tuple(_StubFold(index) for index in range(5)))
    control = check_leakage_control(variants, expect_outperforms=False)
    payload = build_real_payload(
        run=assemble_run(
            corpus,
            config,
            variants,
            corpus_name="real-corpus",
            split_report_line=plan.as_report_line(),
            mlflow_uri=None,
        ),
        variants=variants,
        control=control,
        plan=plan,
        plan_report={"window_source": "test"},
        fold_column_report={"available": False, "note": "stub plan"},
        fits=5,
    )

    # fold_windows on the completion path: five folds in, five dated windows out, so
    # `backtest_fold`'s five NOT NULL bounds have a producer and demo_seed stops refusing.
    assert len(payload["fold_windows"]) == 5
    assert [entry["fold_index"] for entry in payload["fold_windows"]] == [0, 1, 2, 3, 4]
    assert {"train_start", "train_end", "embargo_end", "test_start", "test_end"} <= set(
        payload["fold_windows"][0]
    )

    tally = payload["ablation_channel_availability"]
    assert "gbm_no_graph" in tally["partial_rows"]
    assert tally["unavailable_rows"] == []
    assert "PARTIALLY MEASURED" in payload["ablation_caveat"]
    assert "gbm_no_graph" in payload["ablation_caveat"]

    path = tmp_path / "ablation_results.json"
    write_json(payload, path)
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    row = next(v for v in reloaded["variants"] if v["row_id"] == "gbm_no_graph")
    assert row["channel_availability"]["status"] == "partial"
    assert [entry["fold_index"] for entry in row["channel_availability"]["folds_unavailable"]] == [
        2,
        3,
    ]
    assert row["channel_availability"]["folds_unavailable"][0]["scoring_mode"] == DEGRADED_MODE
    assert row["policies"]["ev_greedy"]["folds_reported"] == 3
    assert row["policies"]["ev_greedy"]["folds_expected"] == 5
    assert "3 of 5 folds" in row["policies"]["ev_greedy"]["coverage_note"]
    assert len(reloaded["fold_windows"]) == 5

    card = build_model_card_payload(
        assemble_run(
            corpus,
            config,
            variants,
            corpus_name="real-corpus",
            split_report_line=plan.as_report_line(),
            mlflow_uri=None,
        )
    )
    card_rows = {entry["row_id"]: entry for entry in card["ablation_table"]}
    assert set(card_rows) == set(ROW_IDS), "the never-cut table kept every row"
    assert card_rows["gbm_no_graph"]["folds_reported"] == 3
    assert card_rows["gbm_no_graph"]["channel_status"] == "partial"
    assert card["ablation_row_availability"]["rows_unavailable"] == []
    assert "gbm_no_graph" in card["ablation_row_availability"]["rows_partially_measured"]


def test_a_card_row_that_measured_nothing_is_published_as_absent_not_as_zero() -> None:
    """The card cannot drop the row and cannot invent a number for it."""
    from oxbow.backtest.harness import assemble_run
    from oxbow.backtest.model_card import build_model_card_payload

    config = fast_config()
    corpus = fakes.make_corpus(n_accounts=500, span_days=500, with_typology=True)
    variants = _degraded_ablation({0, 1, 2, 3, 4})
    plan = _StubPlan(folds=tuple(_StubFold(index) for index in range(5)))
    card = build_model_card_payload(
        assemble_run(
            corpus,
            config,
            variants,
            corpus_name="real-corpus",
            split_report_line=plan.as_report_line(),
            mlflow_uri=None,
        )
    )
    entry = next(r for r in card["ablation_table"] if r["row_id"] == "gbm_no_graph")
    assert entry["channel_status"] == "unavailable"
    assert entry["pr_auc"] is None
    assert entry["net_benefit_total_minor"] is None, "absent, not 0 minor units"
    assert entry["folds_reported"] == 0
    assert "gbm_no_graph" in card["ablation_row_availability"]["rows_unavailable"]
    # The headline moves to a configuration that measured something, rather than crashing or
    # publishing a null as the run's figure.
    assert card["headline"]["pr_auc"] is not None


def test_the_generated_caveat_names_the_rows_the_run_could_not_measure() -> None:
    """Disclosure is generated from the run, so a partial row cannot be published silently."""
    variants = _degraded_ablation({2, 3})
    text = _ablation_caveat(variants)
    assert "PARTIALLY MEASURED" in text
    assert "gbm_no_graph" in text and "gbm_with_graph" in text
    assert "NOT MEASURED AT ALL" not in text

    quiet = _degraded_ablation(set())
    assert "PARTIALLY MEASURED" not in _ablation_caveat(quiet)
    assert all(v.availability["status"] == "measured" for v in quiet)


def test_the_console_table_prints_which_rows_were_measured_and_which_were_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The third surface plan §12 requires: an operator reading stdout sees the gap too."""
    from oxbow.backtest.run import _print_real

    config = fast_config()
    variants = _degraded_ablation({2, 3})
    plan = _StubPlan(folds=tuple(_StubFold(index) for index in range(5)))
    control = check_leakage_control(variants, expect_outperforms=False)
    _print_real(
        variants,
        control,
        config,
        plan,
        {
            "window_source": "stub",
            "recorded_window": None,
            "corpus_window": None,
            "folds_whose_boundaries_would_differ": 0,
            "folds": 5,
        },
        {"available": False, "note": "stub plan"},
    )
    printed = capsys.readouterr().out
    assert "ROW AVAILABILITY" in printed
    assert "gbm_no_graph [partial]" in printed
    assert DEGRADED_MODE in printed
    assert "fold 2" in printed and "fold 3" in printed
    assert "3/5" in printed, "the fold-coverage column rides beside the PR-AUC it describes"
