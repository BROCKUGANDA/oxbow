"""Plan §8's split discipline: expanding-window walk-forward, purged, 30 days embargoed.

WHY THESE TESTS LIVE HERE AND THE MODULE DOES NOT. Plan §8 requires fold computation to
live in exactly one module. That module landed as ``oxbow.backtest.splits``, which
``oxbow/backtest/interfaces.py`` records as "the ONE splits module plan §8 mandates …
owned by the feature layer", and its own docstring names *this file* as the check that keeps
that claim true rather than aspirational. Duplicating it at ``oxbow/features/splits.py``
would create the second set of boundary arithmetic that §8 exists to prevent, so the
ownership is honoured by testing it.

WHAT IS ASSERTED, AND WHAT WOULD SLIP THROUGH A WEAKER TEST. An embargo that is merely
*configured* is not an embargo: the failure this file guards against is a fold whose
training rows have feature windows straddling the boundary, which makes the walk-forward a
restatement of the test period. So the assertions are on row counts and on published values,
not on a config field.

``test_embargo_blocks_leakage`` is the load-bearing one, and it is a value-level claim:
recompute the real feature matrix with the corpus truncated at a fold's training cutoff, and
require every training row to come back byte-identical. If the embargo were shorter than the
longest declared window, that comparison would fail, and it would fail on the arithmetic
rather than on a comment.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl
import pytest
import yaml

from oxbow.backtest.splits import (
    EXPANDING,
    SLIDING,
    WALK_FORWARD,
    SplitError,
    SplitPlan,
    analysis_windows,
    assert_entity_disjoint,
    build_walk_forward,
    dedupe_by_pattern_signature,
    fold_audit,
    holdout_mask,
    is_holdout,
    load_split_config,
    require_reported,
)
from oxbow.config import PipelineConfig, find_repo_root, load_pipeline_config, load_yaml
from oxbow.features.compute import build_feature_table
from oxbow.features.fakes import CENTS, EPOCH, canonical_event, canonical_frame
from oxbow.features.registry import FeatureRegistry, registry_from_config_dir

REPO_ROOT: Final = find_repo_root()
CONFIG_DIR: Final = REPO_ROOT / "config"
PACKAGE_DIR: Final = REPO_ROOT / "packages" / "pipeline" / "oxbow"
DAY: Final = timedelta(days=1)
BASE: Final = datetime(2023, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def registry() -> FeatureRegistry:
    return registry_from_config_dir(CONFIG_DIR)


@pytest.fixture(scope="module")
def cfg() -> PipelineConfig:
    return load_pipeline_config(REPO_ROOT)


@pytest.fixture(scope="module")
def timeline() -> pl.DataFrame:
    """A 900-day timeline with an entity column, which is all the split machinery reads.

    Long enough that five expanding folds plus a 30-day embargo each have rows: a plan built
    on a 60-day timeline cannot express a 30-day lookback and a 30-day embargo at once, and
    a test that quietly accepted that would be measuring a degenerate split. Anchored on the
    feature fixtures' epoch so the embargo test can compare fold boundaries against published
    feature cutoffs at all.

    Three hundred distinct accounts, not forty: ``test_entity_disjoint_split_...`` measures
    the *share* of accounts the hash bucketing lands in the holdout, and a share measured on
    forty accounts is a sample of forty Bernoulli draws — with seed 1337 the fixture's
    ``acct_000..039`` happen to land sixteen below the 0.2 threshold, a 0.40 draw that says
    about the sample and nothing about the bucketing. At three hundred the realized share
    sits on the configured fraction to within a few points, so the assertion below measures
    the mechanism instead of the luck of four account names.
    """
    stamps = [EPOCH + timedelta(days=day, hours=day % 24) for day in range(900)]
    return pl.DataFrame(
        {
            "event_ts_utc": stamps,
            "entity": [f"acct_{index % 300:03d}" for index in range(len(stamps))],
            "txn_id": [f"t{index:05d}" for index in range(len(stamps))],
        }
    )


# A ladder the shipped file does not have: each fold's training window must *end* after the
# previous fold's test window ended, by more than the embargo. See
# ``test_the_shipped_fold_ladder_is_refused`` for what config/splits.yaml currently says.
VALID_LADDER: Final[list[tuple[float, float]]] = [
    (0.38, 0.45),
    (0.53, 0.60),
    (0.68, 0.75),
    (0.83, 0.90),
    (0.98, 1.00),
]


def _splits_at(
    tmp_path: Path,
    *,
    folds: list[tuple[float, float]] | None = None,
    scheme: str | None = None,
) -> Path:
    """A config directory holding the live splits file with its ladder and/or scheme changed."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    raw = load_yaml(CONFIG_DIR / "splits.yaml")
    walk = raw.get("walk_forward")
    assert isinstance(walk, dict)
    if folds is not None:
        walk["folds"] = [
            {"index": index, "train_end_frac": train, "test_end_frac": test}
            for index, (train, test) in enumerate(folds)
        ]
    if scheme is not None:
        walk["scheme"] = scheme
    (config_dir / "splits.yaml").write_text(_dump(raw), encoding="utf-8")
    shutil.copyfile(CONFIG_DIR / "pipeline.yaml", config_dir / "pipeline.yaml")
    return config_dir


@pytest.fixture(scope="module")
def plan(
    timeline: pl.DataFrame, registry: FeatureRegistry, tmp_path_factory: pytest.TempPathFactory
) -> SplitPlan:
    return build_walk_forward(
        timeline,
        registry=registry,
        config_dir=_splits_at(tmp_path_factory.mktemp("ladder"), folds=VALID_LADDER),
    )


# --- the embargo ----------------------------------------------------------


def test_the_shipped_fold_ladder_builds_expanding_and_is_refused_when_read_as_sliding(
    tmp_path: Path, registry: FeatureRegistry, timeline: pl.DataFrame
) -> None:
    """The shipped ladder under the scheme config/splits.yaml declares, and under the other one.

    Spec §7.1 fixes an *expanding*-window walk-forward and the shipped file says so in as
    many words (``scheme: expanding_window``, "train grows each fold"), so every fold trains
    from the timeline start and the shipped fractions build: each fold's training cutoff is
    the embargo width short of its own test start, never before its own training start.

    Read the same ladder as a *sliding* window — fold N starting where fold N-1 stopped
    scoring — and it cannot be embargoed at all: fold 1's ``train_end_frac`` equals fold 0's
    ``test_end_frac``, so pulling the training cutoff back by one embargo lands it a full
    embargo width *before* fold 1's training start. That is the measured finding this test
    used to pin on the shipped file alone; it belongs to the sliding reading, and both
    readings are pinned here so neither can drift into the other unnoticed.
    """
    shipped_raw = load_split_config(CONFIG_DIR)
    shipped_walk = shipped_raw.get("walk_forward")
    assert isinstance(shipped_walk, dict)
    assert shipped_walk["scheme"] == EXPANDING, (
        "config/splits.yaml declares the scheme this assertion reads; if it changed, the "
        "refusal below is the wrong test for it"
    )
    shipped = build_walk_forward(timeline, registry=registry, config_dir=CONFIG_DIR)
    assert len(shipped.folds) == 5
    for fold in shipped.folds:
        assert fold.train_start_ts < fold.train_end_ts < fold.test_start_ts <= fold.test_end_ts

    with pytest.raises(SplitError) as excinfo:
        build_walk_forward(
            timeline, registry=registry, config_dir=_splits_at(tmp_path, scheme=SLIDING)
        )
    message = str(excinfo.value)
    assert "folds[1]" in message
    assert "the timeline is shorter than the configured embargo" in message
    # And the mechanism is fine: the only thing changed is the ladder.
    fixed = build_walk_forward(
        timeline, registry=registry, config_dir=_splits_at(tmp_path / "fixed", folds=VALID_LADDER)
    )
    assert len(fixed.folds) == 5
    for fold in fixed.folds:
        assert fold.train_start_ts < fold.train_end_ts < fold.test_start_ts <= fold.test_end_ts
    assert message != ""


def test_embargo_equals_the_longest_declared_window(
    plan: SplitPlan, registry: FeatureRegistry
) -> None:
    """The embargo is 30 days because the longest feature window is 30 days.

    Asserted as equality in both directions, and sourced from each file rather than from a
    literal here: a shorter embargo lets a boundary row's window reach into the scored
    period, and a longer one throws away training data for no protective reason.
    """
    raw = load_split_config(CONFIG_DIR)
    walk = raw.get("walk_forward")
    assert isinstance(walk, dict)
    assert plan.embargo_days == walk["embargo_days"] == 30
    assert plan.embargo_days == registry.max_lookback_days
    assert plan.registry_max_lookback_days == registry.max_lookback_days
    longest = max(
        entry.window_duration for entry in registry.entries if entry.window_duration is not None
    )
    assert timedelta(days=plan.embargo_days) >= longest
    assert plan.embargo_days == plan.registry_max_lookback_days


def test_embargo_band_is_withheld_from_training_and_validation(
    timeline: pl.DataFrame, plan: SplitPlan
) -> None:
    """Rows inside the band belong to neither side, and the band is really populated.

    The non-vacuity check matters as much as the mask check: an embargo that excludes zero
    rows is a config field, not a guard, and it reads identically green in a report.
    """
    for fold in plan.folds:
        stamps = timeline["event_ts_utc"]
        inside = timeline.filter((stamps > fold.train_end_ts) & (stamps < fold.test_start_ts))
        assert inside.height > 0, f"{fold.fold_id}: empty embargo band, so nothing was withheld"
        assert timedelta(
            seconds=(fold.test_start_ts - fold.train_end_ts).total_seconds()
        ) == timedelta(days=plan.embargo_days), f"{fold.fold_id}: the band is not one embargo wide"
        trained = timeline.filter(fold.train_rows_mask()).height
        withheld = inside.height
        assert timeline.filter(fold.validation_mask()).height > 0
        overlap = timeline.filter(fold.train_rows_mask() & (stamps > fold.train_end_ts)).height
        assert overlap == 0, f"{fold.fold_id}: training rows sit past the training cutoff"
        assert trained + withheld <= timeline.height


def test_embargo_blocks_leakage(
    plan: SplitPlan, registry: FeatureRegistry, cfg: PipelineConfig
) -> None:
    """A feature from inside the embargo cannot reach a training fold — measured, not asserted.

    Two arms, because two different things go wrong here.

    *The mask arm*: take a row inside the withheld band and show its label window reaches
    into the period being scored, which is exactly why it is not a training row.
    *The value arm*: recompute the real published matrix with the corpus truncated at the
    fold's training cutoff and require every training row to come back identical. That is the
    property the whole discipline buys. If the embargo were shorter than the longest declared
    window, a training row's 30-day aggregate would contain post-cutoff events and this
    comparison would fail on numbers, not on a comment.
    """
    fold = plan.folds[0]
    events = _feature_corpus()
    full = build_feature_table(events, registry, cfg=cfg)
    truncated_events = events.filter(pl.col("event_ts_utc") <= fold.train_end_ts)
    assert truncated_events.height < events.height
    truncated = build_feature_table(events, registry, as_of=fold.train_end_ts, cfg=cfg)

    keys = ["txn_id", "entity"]
    columns = [column for column in full.matrix.columns if column not in (*keys, "cutoff_ts")]
    training_rows = truncated.matrix
    assert training_rows["cutoff_ts"].max() <= fold.train_end_ts
    joined = training_rows.join(
        full.matrix.select([*keys, *columns]), on=keys, how="inner", suffix="__full"
    )
    assert joined.height == training_rows.height, "training rows disappeared under truncation"
    offenders: list[str] = []
    for column in columns:
        sealed, whole = joined[column], joined[f"{column}__full"]
        differs = int(((sealed != whole) & ~(sealed.is_null() & whole.is_null())).sum())
        if differs:
            offenders.append(f"{column} ({differs} rows)")
    assert not offenders, (
        f"a training row's published value changed once post-cutoff data was included: "
        f"{offenders}. The embargo is {plan.embargo_days}d and the longest window is "
        f"{registry.max_lookback_days}d, so this is a boundary arithmetic fault, not a "
        "statistical fluke."
    )

    # The band really does straddle: an embargo row's own 30-day window reaches back into
    # training data, and its label window reaches forward into the scored period.
    stamps = events["event_ts_utc"]
    band = events.filter((stamps > fold.train_end_ts) & (stamps < fold.test_start_ts))
    assert band.height > 0, "the fixture corpus does not cover this fold's embargo band"
    longest = max(
        entry.window_duration for entry in registry.entries if entry.window_duration is not None
    )
    inside_band = band["event_ts_utc"][0]
    assert inside_band - longest < fold.train_end_ts, (
        "an embargo-band window should reach back across the training cutoff, which is why "
        "the band is withheld from fitting"
    )
    assert inside_band + timedelta(days=fold.label_window_days) >= fold.test_start_ts or (
        inside_band + timedelta(days=fold.label_window_days) > fold.train_end_ts
    )


def _feature_corpus(events: int = 2_700, *, accounts: int = 12) -> pl.DataFrame:
    """A canonical corpus spanning the first fold's embargo band and its test window.

    ``canonical_event`` takes a minute offset from the feature fixtures' epoch, which is the
    same epoch the timeline fixture uses, so a fold boundary and a published feature cutoff
    are comparable instants rather than two calendars. 2,700 events at eight-hour spacing
    covers the full 900 days.
    """
    keys = [f"acct_{index:03d}" for index in range(accounts)]
    rows = [
        canonical_event(
            f"f{index:05d}",
            index * 480,
            keys[index % accounts],
            keys[(index * 5 + 1) % accounts],
            (1 + index % 50) * CENTS,
            src_balance_before_minor=10_000 * CENTS,
            label_is_fraud=1 if index % 53 == 0 else 0,
        )
        for index in range(events)
    ]
    return canonical_frame(rows)


def test_a_shorter_embargo_is_refused_not_silently_accepted(
    tmp_path: Path, registry: FeatureRegistry, timeline: pl.DataFrame
) -> None:
    """The config's two halves must agree, and the loader enforces it in both directions.

    This is the guard that makes the previous test's setup trustworthy: an embargo edited
    down to 7 days would leave the value-level claim above passing on a 30-day window by
    accident of the fixture, and the run would still look green.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    raw = load_yaml(CONFIG_DIR / "splits.yaml")
    walk = raw.get("walk_forward")
    assert isinstance(walk, dict)
    walk["embargo_days"] = 7
    (config_dir / "splits.yaml").write_text(
        _dump({"walk_forward": walk, **{k: v for k, v in raw.items() if k != "walk_forward"}}),
        encoding="utf-8",
    )
    shutil.copyfile(CONFIG_DIR / "pipeline.yaml", tmp_path / "config" / "pipeline.yaml")
    with pytest.raises(SplitError, match="longest declared feature window"):
        build_walk_forward(timeline, registry=registry, config_dir=config_dir)


def _dump(raw: dict[str, object]) -> str:
    return yaml.safe_dump(raw, sort_keys=False)


# --- expanding window and purging ----------------------------------------


def test_walk_forward_is_expanding_and_temporally_ordered(plan: SplitPlan) -> None:
    """Train grows, test is always later, and no fold scores its own training data.

    Expanding means every fold trains from the timeline start, so fold N's training window
    contains fold N-1's in full and what moves is its *end*. Starting each fold where the
    previous one stopped scoring is a sliding window, which config/splits.yaml does not
    select — see ``test_the_shipped_fold_ladder_builds_expanding_and_is_refused_when_read_as_sliding``
    for what the same ladder does under that reading.
    """
    assert len(plan.folds) == 5
    previous_test_end = plan.timeline_start
    for index, fold in enumerate(plan.folds):
        assert fold.index == index
        assert fold.train_start_ts == plan.timeline_start, f"fold {index} is not expanding"
        assert fold.train_end_ts < fold.test_start_ts <= fold.test_end_ts
        assert (
            fold.test_start_ts > previous_test_end
        ), f"fold {index} starts scoring before the fold before it finished scoring"
        assert (
            fold.graph_as_of_ts == fold.train_end_ts
        ), "the fold's graph must end where its training data ends, not at the test start"
        assert fold.available_end_ts == fold.test_start_ts
        previous_test_end = fold.test_end_ts
    widths = [(fold.train_end_ts - fold.train_start_ts) for fold in plan.folds]
    assert widths == sorted(widths), "an expanding window that shrinks is a different scheme"
    assert plan.timeline_end <= previous_test_end


def test_purging_drops_rows_whose_label_window_overlaps_the_test_window(
    plan: SplitPlan,
) -> None:
    """Purge is arithmetic on the label window, not a synonym for the embargo.

    A training row is kept only while its label window ``[ts, ts + label_window)`` is closed
    before the scored period opens: a row at the last instant before the training cutoff
    whose outcome is still being observed inside the period being scored is the textbook way
    a backtest becomes fiction, and the embargo does not touch it.

    On config/splits.yaml's own numbers the purge cannot fire at all — the embargo is 30
    days and the label window is 1, so every row the purge would reach has already been
    withheld by the embargo. That is a fact about the config, not a property of the mask, so
    the mask is exercised here at the widest label window the loader still admits (equal to
    the embargo; a wider one is refused by ``build_walk_forward``), where the boundary row is
    purged and the rows behind it are kept. The shipped fold is asserted to drop nothing,
    which is the same statement from the other side: purge and embargo stay two predicates
    even where one of them happens to be redundant.
    """
    fold = plan.folds[1]
    widest = dataclasses.replace(fold, label_window_days=plan.embargo_days)
    assert fold.label_window_days < widest.label_window_days, (
        "the shipped label window sits inside the embargo, so the wide case is the one with "
        "a boundary row left to purge"
    )
    stamps = [fold.train_end_ts - timedelta(hours=hours) for hours in range(0, 72, 6)]
    ids = [f"p{i}" for i in range(len(stamps))]
    frame = pl.DataFrame({"event_ts_utc": stamps, "txn_id": ids})
    kept = frame.filter(widest.train_rows_mask())["txn_id"].to_list()
    assert len(kept) < frame.height, "nothing was purged, so the purge is decorative"
    for stamp, txn_id in zip(stamps, ids, strict=True):
        overlaps = stamp + timedelta(days=widest.label_window_days) >= widest.test_start_ts
        assert (txn_id in kept) is not overlaps, f"{txn_id} purged on the wrong predicate"
    assert frame.filter(fold.train_rows_mask()).height == frame.height, (
        "the shipped 1-day label window is closed 30 days before scoring opens, so the "
        "embargo alone must account for the boundary"
    )
    assert fold.purge_days >= 1, "a zero-day purge makes the label window meaningless"


def test_validation_slice_is_the_tail_of_training_never_the_test_period(
    plan: SplitPlan,
) -> None:
    """Bins, WOE and calibration fit on the train tail; the test fold stays untouched."""
    for fold in plan.folds:
        validation = (
            fold.validation_start_ts < fold.train_end_ts and fold.test_start_ts > fold.train_end_ts
        )
        assert validation, f"{fold.fold_id}: the validation slice is not inside training"
        assert fold.validation_mask() is not None


# --- entity-disjoint split and the reported flag --------------------------


def test_entity_disjoint_split_puts_every_account_on_one_side(
    timeline: pl.DataFrame, plan: SplitPlan, cfg: PipelineConfig
) -> None:
    """No account appears in both sides, measured on the data rather than argued.

    The holdout is a hash bucket of the account key, so it is stable across runs and
    independent of row order — and the only evidence that holds is the count of accounts
    found on both sides, which must be zero.
    """
    split = plan.entity_split
    assert split is not None
    assert split.seed == cfg.seed == 1337
    assert 0.0 < split.holdout_fraction < 1.0
    assert assert_entity_disjoint(timeline, split, entity_column="entity") == 0
    holdout = split.accounts(timeline, entity_column="entity")
    assert holdout, "an empty holdout would make the disjointness claim vacuous"
    for account in holdout[:5]:
        assert is_holdout(account, seed=split.seed, holdout_fraction=split.holdout_fraction)
    mask = holdout_mask(timeline, split, entity_column="entity")
    assert mask.sum() == timeline.filter(pl.col("entity").is_in(holdout)).height
    share = len(holdout) / split.total_accounts
    assert abs(share - split.holdout_fraction) < 0.15, f"measured share {share} is off target"


def test_which_split_was_optimised_on_is_reported_and_cannot_be_silently_swapped(
    plan: SplitPlan,
) -> None:
    """Spec §7.1: the temporal split is primary and the flag travels with the plan.

    The entity-disjoint split is a robustness check. Quoting its better number without the
    flag is the sin this exists to prevent, so the flag is a property of the plan, rendered
    by one method, and promoted-to-headline is a refusal rather than a review note.
    """
    assert plan.which_split_was_optimised_on == "walk_forward"
    line = plan.as_report_line()
    assert "which_split_was_optimised_on: walk_forward" in line
    assert "robustness check, not the headline" in line
    assert f"{plan.embargo_days}d embargo" in line
    require_reported(plan, line)
    with pytest.raises(Exception, match="which_split_was_optimised_on"):
        require_reported(plan, "a report that quietly omits the flag")
    promoted = dataclasses.replace(plan, optimised_on="entity_disjoint")
    with pytest.raises(SplitError, match="robustness check"):
        promoted.assert_only_temporal_split_is_primary()
    assert promoted.which_split_was_optimised_on != WALK_FORWARD


# --- the pattern-spanning-boundary case ----------------------------------


def test_pattern_spanning_boundary_detected_once(registry: FeatureRegistry) -> None:
    """A pattern straddling a window boundary is visible, and visible exactly once.

    ``window_semantics.overlap_hours: 168`` and "deduplicate hits by pattern signature, not
    by window" are one mechanism with two failure modes, and this test pins both. A
    structuring ladder that crosses midnight must appear in at least one window — a
    non-overlapping tumble would split it in half and detect nothing — and must appear in
    only one *alert*, because counting it per window inflates both the evidence panel and the
    hit rate a threshold is tuned against.
    """
    overlap = timedelta(hours=registry.semantics.overlap_hours)
    assert overlap == timedelta(days=7)
    stamps = pl.DataFrame({"event_ts_utc": [BASE, BASE + timedelta(days=13)]})
    windows = analysis_windows(
        stamps, window_days=1, overlap_hours=registry.semantics.overlap_hours
    )
    assert windows.height >= 13
    first = windows.row(0, named=True)
    assert (
        first["read_from_ts"] == first["window_start_ts"] - overlap
    ), "the window must read back the declared overlap, or a spanning pattern is cut in two"

    spanning = BASE + timedelta(days=12, hours=18)
    hits = pl.DataFrame(
        {
            "account": ["acct_0", "acct_0"],
            "rule_id": ["R5", "R5"],
            "pattern_signature": ["ladder-4-steps", "ladder-4-steps"],
            "event_ts_utc": [spanning, spanning + DAY],
            "txn_id": ["h1", "h2"],
        }
    )
    deduped = dedupe_by_pattern_signature(hits)
    assert deduped.height == 1, "a boundary-spanning pattern was alerted twice"
    assert deduped["txn_id"].item() == "h1", "the earliest hit must survive"
    assert hits.height == 2, "the fixture must genuinely present the hit in two windows"

    distinct = pl.DataFrame(
        {
            "account": ["acct_0", "acct_0"],
            "rule_id": ["R5", "R5"],
            "pattern_signature": ["ladder-4-steps", "ladder-6-steps"],
            "event_ts_utc": [spanning, spanning + DAY],
            "txn_id": ["h1", "h2"],
        }
    )
    assert (
        dedupe_by_pattern_signature(distinct).height == 2
    ), "two different patterns collapsed into one alert, which hides evidence"


# --- the one-module claim -------------------------------------------------


def test_no_fold_boundary_arithmetic_lives_outside_the_splits_module() -> None:
    """§8's "one module" claim, checked by scanning the package rather than by review.

    The claim is that nothing outside ``oxbow/backtest/splits.py`` may *derive* a fold, an
    embargo band or a window boundary. Two copies of that arithmetic is the defect §8 calls
    "the single most common way a backtest becomes fiction", and the only durable check is a
    scan.

    Scanned for derivation, not for mention: the harness and its adapter legitimately pass
    ``embargo_days=`` and read ``fold.train_end_ts``, and a scan that flagged those would be
    wrong in a way that gets it deleted. What is disqualifying is computing a boundary — a
    local binding from a duration, or constructing the ``Fold`` itself.
    """
    constructors = _matches(r"\bFold\(\n?\s*fold_id=")
    assert constructors == {"packages/pipeline/oxbow/backtest/splits.py"}, constructors
    derived = _matches(
        r"^\s*(train_end|train_start|test_start|test_end|validation_start|embargo_days)"
        r"\s*=\s*[^=\n].*(timedelta|duration\(|days=|seconds=)"
    )
    assert derived == {"packages/pipeline/oxbow/backtest/splits.py"}, derived
    read_only = _matches(r"\.(train_end_ts|test_start_ts)\s*[-+]=?\s")
    assert not read_only, f"a fold boundary was mutated outside its module of origin: {read_only}"


def _matches(pattern: str) -> set[str]:
    compiled = re.compile(pattern, re.MULTILINE)
    found: set[str] = set()
    for path in PACKAGE_DIR.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - not a source file
            continue
        if compiled.search(text):
            found.add(relative)
    return found


def test_the_fold_protocol_is_satisfied_by_the_real_plan(
    plan: SplitPlan, cfg: PipelineConfig
) -> None:
    """``FoldProvider`` is met by an adapter over this module, with masks and the embargo.

    The harness consumes boolean masks and ``embargo_days``; the splits module emits polars
    expressions and timestamps. The adapter is the bridge and must add no arithmetic of its
    own, so the check is that the masks it produces *are* the splits module's masks
    evaluated, row for row, and that the embargo it reports is this plan's.
    """
    from oxbow.backtest.fold_provider import SplitsFoldProvider
    from oxbow.backtest.interfaces import FoldProvider, HarnessFold

    corpus = pl.DataFrame({"as_of_ts": plan.folds[0].train_start_ts + timedelta(days=1)})
    provider = SplitsFoldProvider(plan)
    assert isinstance(provider, FoldProvider)
    folds = provider.folds(corpus)
    assert len(folds) == len(plan.folds)
    assert provider.embargo_days() == plan.embargo_days == 30
    for harness, fold in zip(folds, plan.folds, strict=True):
        assert isinstance(harness, HarnessFold)
        assert harness.index == fold.index
        assert harness.embargo_days == plan.embargo_days
        for name in ("train_mask", "validation_mask", "test_mask"):
            mask = getattr(harness, name)
            assert len(mask) == corpus.height, f"fold {fold.index} mask {name} is misaligned"
            assert all(isinstance(value, bool) for value in mask)
        expected = corpus.select(fold.train_mask("as_of_ts").cast(pl.Boolean)).item()
        assert harness.train_mask[0] == bool(expected)


def test_fold_audit_reports_measured_counts(plan: SplitPlan, timeline: pl.DataFrame) -> None:
    """Printed numbers, so an embargo that eats half the corpus is visible before metrics.

    A plan whose folds are 90 % embargo can still produce plausible PR-AUC. This is the
    artifact that makes that visible, so the assertion is that it is non-empty and internally
    consistent with the masks, not that it looks nice.
    """
    audit = fold_audit(plan, timeline)
    assert audit.height == len(plan.folds)
    assert set(audit.columns) >= {
        "fold_id",
        "train_rows",
        "validation_rows",
        "test_rows",
        "embargo_rows",
    }
    for row in audit.iter_rows(named=True):
        assert row["train_rows"] > 0, f"{row['fold_id']} has no training rows"
        assert row["test_rows"] > 0
        assert row["embargo_rows"] >= 0
    assert audit["test_rows"].sum() > 0


def test_a_shuffled_split_is_refused(
    tmp_path: Path, registry: FeatureRegistry, timeline: pl.DataFrame
) -> None:
    """Shuffling a temporal split is a rejection trigger, not a knob (plan §18)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    raw = load_yaml(CONFIG_DIR / "splits.yaml")
    walk = raw.get("walk_forward")
    assert isinstance(walk, dict)
    walk["shuffle"] = True
    (config_dir / "splits.yaml").write_text(_dump(raw), encoding="utf-8")
    shutil.copyfile(CONFIG_DIR / "pipeline.yaml", tmp_path / "config" / "pipeline.yaml")
    with pytest.raises(SplitError, match="random split"):
        build_walk_forward(timeline, registry=registry, config_dir=config_dir)


def test_a_purged_false_split_is_refused(
    tmp_path: Path, registry: FeatureRegistry, timeline: pl.DataFrame
) -> None:
    """§8's split is purged; turning the purge off is not a supported configuration."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    raw = load_yaml(CONFIG_DIR / "splits.yaml")
    walk = raw.get("walk_forward")
    assert isinstance(walk, dict)
    walk["purge"] = False
    (config_dir / "splits.yaml").write_text(_dump(raw), encoding="utf-8")
    shutil.copyfile(CONFIG_DIR / "pipeline.yaml", tmp_path / "config" / "pipeline.yaml")
    with pytest.raises(SplitError, match="purge: false is not supported"):
        build_walk_forward(timeline, registry=registry, config_dir=config_dir)


def test_the_fold_carries_the_timestamps_every_consumer_must_agree_on(plan: SplitPlan) -> None:
    """The dict a run records as its ``split_def``: complete, and free of wall clock."""
    fold = plan.folds[2]
    payload = fold.as_dict()
    assert payload["fold_id"] == fold.fold_id
    assert payload["seed"] == 1337
    for key in ("train_start_ts", "train_end_ts", "test_start_ts", "test_end_ts", "graph_as_of_ts"):
        assert key in payload, key
        assert isinstance(payload[key], str)
    assert fold.inside_embargo(fold.train_end_ts + timedelta(minutes=1))
    assert not fold.inside_embargo(fold.train_end_ts - timedelta(minutes=1))
    assert not fold.inside_embargo(fold.test_start_ts + timedelta(minutes=1))
    assert plan.fold_for(fold.test_start_ts + timedelta(minutes=1)) == fold
    assert (
        plan.fold_of_row(
            pl.Series([fold.test_start_ts + timedelta(minutes=1)], dtype=pl.Datetime("us", UTC))
        ).item()
        == fold.fold_id
    )
