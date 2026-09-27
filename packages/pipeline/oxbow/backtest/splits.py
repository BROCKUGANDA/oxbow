"""The one module that computes folds: expanding-window walk-forward, purged and embargoed.

WHY ONE MODULE. Plan §8: "Walk-forward splits with purging and 30-day embargo live in
one module... Purge/embargo misapplication is the single most common way a backtest
becomes fiction." Two places computing boundaries is two places that can disagree, and
the disagreement shows up as a backtest number nobody can reproduce. Nothing outside
this file may derive a fold, an embargo band or a window boundary;
``tests/unit/test_p2_splits.py`` greps the package for that arithmetic to keep the claim
checkable rather than aspirational.

WHAT THE EMBARGO IS FOR, EXACTLY. A feature window is ``(cutoff - window, cutoff]`` and
the longest declared window is ``max_lookback_days``. The embargo withholds the band
between the training cutoff and the test start from *both* sides, so no fitted
statistic — bins, WOE, scalers, thresholds, and the fold's graph — is estimated from
rows whose feature windows straddle the boundary in a way the test rows will never see
again in production. The module asserts the embargo equals the registry's longest
window instead of trusting that config/splits.yaml and config/features.yaml were edited
together.

PURGE AND EMBARGO ARE TWO PREDICATES, NOT ONE. The embargo withholds a *band* of time; the
purge drops *rows* whose outcome window ``[ts, ts + label_window_days]`` reaches the test
start. A row can only be purged if its outcome window is at least as wide as the band that
already separates it from the scored period, so on config/splits.yaml's numbers — a 30-day
embargo and a 1-day declared window — the purge removes nothing and the embargo alone is
doing the work. That is reported, not smoothed over: :func:`fold_audit` prints the measured
``purged_rows`` per fold, and the loader still refuses an outcome window *wider* than the
band, which is the case where the boundary genuinely would not be withheld. Keeping the two
arithmetic separate is what makes the mask right on a corpus whose labels do arrive late.

ENTITY-DISJOINT SIZING IS MEASURED, NOT ASSUMED. Membership is a bucket of a seeded hash, so
the count that lands in the holdout is a draw, and a draw on a small account population can
sit far off the requested fraction while every per-account test still passes. The bucket is
therefore built from a bias-free 64-bit mapping, :func:`build_entity_disjoint` refuses a
draw no fair sampling on that population explains or one that leaves a side empty, and
``SplitPlan.as_report_line`` prints the measured share next to the requested fraction.

WHAT ``which_split_was_optimised_on`` IS FOR. Spec §7.1: the entity-disjoint split is a
robustness check, not the headline. A temporal split can put the same account on both
sides, which flatters account-level features; an entity-disjoint split cannot be
overfitted that way but breaks the temporal ordering the product is deployed against.
Reporting the better number without saying which split produced it is the sin this flag
exists to prevent, so it travels with the plan and reporting renders
:meth:`SplitPlan.as_report_line` verbatim.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl

from oxbow.config import ConfigError, find_repo_root, load_pipeline_config, load_yaml
from oxbow.features.registry import FeatureRegistry

SPLITS_FILENAME: Final = "splits.yaml"
TS_COLUMN: Final = "event_ts_utc"
ENTITY_COLUMN: Final = "entity"
TXN_ID_COLUMN: Final = "txn_id"
ACCOUNT_COLUMN: Final = "account"

WALK_FORWARD: Final = "walk_forward"
ENTITY_DISJOINT: Final = "entity_disjoint"

# The two fold-window schemes config/splits.yaml may declare. `expanding_window` is plan
# §8's scheme and the shipped default: every fold trains from the beginning of history, so
# training data only ever grows. `sliding_window` starts each fold where the previous one
# stopped scoring, which is a different scheme with a different failure mode and has to be
# named to be used.
EXPANDING: Final = "expanding_window"
SLIDING: Final = "sliding_window"

# The fold's last usable instant is one microsecond of slack below the boundary, so
# "at or after the cutoff" is excluded by the same rule everywhere.
ONE_SECOND: Final = timedelta(seconds=1)
HOLDOUT_BUCKETS: Final = 1_000_000


class SplitError(ConfigError):
    """Raised when the split configuration or the observed timeline cannot produce folds.

    A fold that would be empty, inverted, or shorter than its own embargo is a
    configuration fault. It stops the run rather than reporting metrics from a
    degenerate split, because a degenerate split's metrics look plausible.
    """


@dataclass(frozen=True, slots=True)
class Fold:
    """One walk-forward fold, with the timestamps every consumer has to agree on.

    ``graph_as_of_ts`` is the only graph cutoff that matters downstream: edges at or
    after it may not enter this fold, and ``features.fold_scope`` refuses any table that
    does. It equals ``train_end_ts`` deliberately — the model is fitted on training
    data, so the structure it sees ends where the training data ends, not at the test
    start.
    """

    fold_id: str
    index: int
    train_start_ts: datetime
    train_end_ts: datetime
    validation_start_ts: datetime
    test_start_ts: datetime
    test_end_ts: datetime
    purge_days: int
    label_window_days: int
    seed: int

    @property
    def graph_as_of_ts(self) -> datetime:
        """The last instant whose edges may feed this fold's graph."""
        return self.train_end_ts

    @property
    def feature_as_of_ts(self) -> datetime:
        """The scoring cutoff for this fold: the last instant inside its test window."""
        return self.test_end_ts - ONE_SECOND

    @property
    def embargo_band(self) -> tuple[datetime, datetime]:
        """``(train cutoff, test start)``: withheld from training and validation."""
        return (self.train_end_ts, self.test_start_ts)

    @property
    def available_end_ts(self) -> datetime:
        """The end of the history this fold may read at all, which is the test start."""
        return self.test_start_ts

    def inside_embargo(self, moment: datetime) -> bool:
        """True when an instant falls in the withheld band between train and test."""
        start, end = self.embargo_band
        return start < moment < end

    def train_mask(self, ts_column: str = TS_COLUMN) -> pl.Expr:
        """Rows available to the fold: at or before the training cutoff."""
        return (pl.col(ts_column) >= self.train_start_ts) & (pl.col(ts_column) <= self.train_end_ts)

    def train_rows_mask(self, ts_column: str = TS_COLUMN) -> pl.Expr:
        """Fitting rows: available history, minus the embargo band and the purge overlap.

        The two exclusions are different arithmetic. The embargo withholds a *band* of
        time, so a row survives it purely by sitting before the training cutoff. The purge
        looks at the row's own outcome window ``[ts, ts + label_window_days]`` and drops
        it when that window reaches the test start — the row's label is then partly
        determined inside the period being scored, which is the textbook way a backtest
        becomes fiction. The boundary row at ``ts == train_end`` is the case only the purge
        catches when the outcome window is as wide as the band, and the rows the purge
        removes are exactly ``label_window_days - embargo_days`` days wide otherwise.
        """
        label_window_end = pl.col(ts_column) + pl.duration(days=self.label_window_days)
        overlaps_test_window = label_window_end >= self.test_start_ts
        return self.train_mask(ts_column) & ~overlaps_test_window

    def outcome_window_reaches_test(self, moment: datetime) -> bool:
        """Whether one instant's outcome window reaches into this fold's scored period.

        The scalar form of the purge predicate, so an audit or a report can name the row it
        dropped instead of restating the mask: a guard nobody can point at a row for is a
        config field, not a guard.
        """
        return moment + timedelta(days=self.label_window_days) >= self.test_start_ts

    def validation_mask(self, ts_column: str = TS_COLUMN) -> pl.Expr:
        """The last slice of the training period: bins, WOE, calibration, selection."""
        return (pl.col(ts_column) > self.validation_start_ts) & (
            pl.col(ts_column) <= self.train_end_ts
        )

    def test_mask(self, ts_column: str = TS_COLUMN) -> pl.Expr:
        """The scored period: after ``test_start_ts``, up to and including ``test_end_ts``.

        THE WINDOW OPENS AT THE TEST START, NOT AT THE TRAINING CUTOFF. config/splits.yaml
        states the ladder as "train, then an embargo gap equal to the longest feature
        lookback (30 days), then test", and ``embargo_band`` names ``(train_end_ts,
        test_start_ts)`` the band "withheld from training and validation" — i.e. belonging
        to neither side. Opening the scored window at ``train_end_ts`` instead scored that
        band, which is what collapsed the fold's honest gap to nothing: on the 2026-09-27
        corpus it put 5,556 embargo-band accounts into fold 0's scored set (28,588 rows
        where the embargo-respecting window holds 23,032), and the first "test" row then sat
        0.0029 d after the last fit row instead of 30 d. The harness's
        ``assert_fold_discipline`` caught exactly that and refused; this predicate is the
        fix on this side of the boundary.

        The boundary instant itself goes to the EARLIER fold, which is what
        :meth:`SplitPlan.fold_for` already decides (``test_start < moment <= test_end``) and
        what :meth:`SplitPlan.fold_of_row` stamps onto the feature table, so the two masks
        and the per-row fold id cannot disagree.
        """
        return (pl.col(ts_column) > self.test_start_ts) & (pl.col(ts_column) <= self.test_end_ts)

    def as_dict(self) -> dict[str, object]:
        """JSON-safe description for the run's split_def record. No wall clock."""
        return {
            "fold_id": self.fold_id,
            "index": self.index,
            "train_start_ts": self.train_start_ts.isoformat(),
            "train_end_ts": self.train_end_ts.isoformat(),
            "validation_start_ts": self.validation_start_ts.isoformat(),
            "test_start_ts": self.test_start_ts.isoformat(),
            "test_end_ts": self.test_end_ts.isoformat(),
            "graph_as_of_ts": self.graph_as_of_ts.isoformat(),
            "purge_days": self.purge_days,
            "label_window_days": self.label_window_days,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class EntitySplit:
    """Account-level holdout: an account is on one side, never both.

    ``holdout_fraction`` is the fraction config requested *and* the bucket cut
    :func:`is_holdout` applies — one value, so the membership test and the resolved account
    list cannot disagree. ``holdout_accounts`` over ``total_accounts`` is what that cut
    actually landed on this corpus, and it is the number reporting prints: a bucket draw is a
    random variable, and on a small account population it can sit far off the requested
    fraction while every per-account test still passes.
    """

    holdout_fraction: float
    holdout_accounts: int
    total_accounts: int
    seed: int

    @property
    def holdout_share(self) -> float:
        """The measured share, which is what the hash bucketing approximates."""
        return self.holdout_accounts / self.total_accounts if self.total_accounts else 0.0

    @property
    def share_error(self) -> float:
        """Measured share minus requested fraction. Printed by the report line, never assumed."""
        return self.holdout_share - self.holdout_fraction

    def mask(self, frame: pl.DataFrame, entity_column: str = ENTITY_COLUMN) -> pl.Series:
        """Holdout membership per row, from the same hash :func:`is_holdout` uses.

        Materialised rather than returned as an expression because the hash is a python
        function: one implementation of the rule, so a mask and a membership test cannot
        disagree.
        """
        return holdout_mask(frame, self, entity_column)

    def accounts(self, frame: pl.DataFrame, entity_column: str = ENTITY_COLUMN) -> list[str]:
        """The holdout's account keys, sorted for a stable artifact order."""
        threshold = _holdout_threshold(self.holdout_fraction)
        distinct = frame[entity_column].unique(maintain_order=True).sort().to_list()
        return sorted(
            account for account in distinct if _entity_bucket(str(account), self.seed) < threshold
        )


@dataclass(frozen=True, slots=True)
class SplitPlan:
    """Everything the backtest is allowed to know about how the data was divided."""

    seed: int
    embargo_days: int
    folds: tuple[Fold, ...]
    entity_split: EntitySplit | None
    optimised_on: str
    timeline_start: datetime
    timeline_end: datetime
    registry_max_lookback_days: int
    spec_hash: str

    @property
    def which_split_was_optimised_on(self) -> str:
        """The split selection and tuning actually happened on. Reporting must print it."""
        return self.optimised_on

    def as_report_line(self) -> str:
        """The sentence a validation page renders, so the flag cannot go missing quietly."""
        folds = "; ".join(
            f"fold {fold.index}: train <= {fold.train_end_ts.isoformat()} | test "
            f"{fold.test_start_ts.isoformat()}..{fold.test_end_ts.isoformat()}"
            for fold in self.folds
        )
        label_window = self.folds[0].label_window_days if self.folds else 0
        entity = (
            "entity_disjoint holdout not configured"
            if self.entity_split is None
            else (
                f"entity_disjoint holdout {self.entity_split.holdout_accounts} of "
                f"{self.entity_split.total_accounts} accounts = measured "
                f"{self.entity_split.holdout_share:.3f} against requested "
                f"{self.entity_split.holdout_fraction:.3f}"
            )
        )
        return (
            f"which_split_was_optimised_on: {self.optimised_on} | expanding-window walk-forward, "
            f"purged on a {label_window}d outcome window, {self.embargo_days}d embargo over "
            f"{self.timeline_start.isoformat()}..{self.timeline_end.isoformat()} | {folds} | "
            f"{entity} | "
            "entity_disjoint is a robustness check, not the headline (spec §7.1)"
        )

    def fold_for(self, moment: datetime) -> Fold | None:
        """The fold whose test window contains ``moment``, or None."""
        return next(
            (fold for fold in self.folds if fold.test_start_ts < moment <= fold.test_end_ts), None
        )

    def fold_of_row(self, stamps: pl.Series) -> pl.Series:
        """Fold id per row: the fold the row is scored in, or the empty string."""
        return pl.Series(
            "fold_id",
            [
                fold.fold_id if (moment is not None and (fold := self.fold_for(moment))) else ""
                for moment in stamps.to_list()
            ],
            dtype=pl.String,
        )

    def assert_only_temporal_split_is_primary(self) -> None:
        """A robustness check may not be quietly promoted to the headline."""
        if self.optimised_on != WALK_FORWARD:
            raise SplitError(
                f"the plan was optimised on {self.optimised_on!r}; spec §7.1 fixes the temporal "
                "walk-forward as primary and the entity-disjoint split as the robustness check"
            )


def _entity_bucket(entity: str, seed: int) -> int:
    """Deterministic 0..999_999 bucket for an entity, salted by the run seed.

    Hashed rather than drawn from a shuffled index: the holdout must be stable across
    runs and independent of row order, and a permutation recomputed per partition is
    neither.

    The mapping is ``floor(2**64 * bucket / 2**64)``-style multiply-shift, not ``%``: a
    32-bit prefix reduced modulo 1_000_000 makes buckets ``0..967_295`` about 1/4294 more
    likely than the rest, which is small but is a *known* bias in the one number that
    decides which accounts the robustness split never sees. Multiply-shift over the full
    64-bit prefix is exactly uniform for any bucket count, so the measured holdout share
    is sampling noise and nothing else.
    """
    prefix = int.from_bytes(
        hashlib.sha256(f"{seed}|{entity}".encode()).digest()[:8], "big", signed=False
    )
    return (prefix * HOLDOUT_BUCKETS) >> 64


def _holdout_threshold(holdout_fraction: float) -> int:
    """The bucket cut a fraction means, computed the one way every caller must use.

    ``round`` rather than ``int`` because the fraction is a float that has just been divided
    out of ``HOLDOUT_BUCKETS``: truncating ``0.195341 * 1_000_000 = 195340.99999`` would put
    the boundary account on the training side of the very split that selected it, which is a
    membership disagreement between :func:`is_holdout` and the resolved list.
    """
    cut = round(holdout_fraction * HOLDOUT_BUCKETS)
    return max(0, min(HOLDOUT_BUCKETS, cut))


def is_holdout(entity: str, *, seed: int, holdout_fraction: float) -> bool:
    """Whether an account belongs to the entity-disjoint holdout."""
    return _entity_bucket(entity, seed) < _holdout_threshold(holdout_fraction)


def holdout_mask(
    frame: pl.DataFrame, split: EntitySplit, entity_column: str = ENTITY_COLUMN
) -> pl.Series:
    """Holdout membership per row, from the same hash :func:`is_holdout` uses."""
    threshold = _holdout_threshold(split.holdout_fraction)
    entities = frame[entity_column].cast(pl.String).to_list()
    return pl.Series(
        "is_holdout",
        [_entity_bucket(entity, split.seed) < threshold for entity in entities],
        dtype=pl.Boolean,
    )


def _require_int(
    mapping: dict[str, object], key: str, where: str, default: int | None = None
) -> int:
    value = mapping.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SplitError(f"{where}: {key!r} must be an integer, got {value!r}")
    return value


def _require_str(mapping: dict[str, object], key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SplitError(f"{where}: {key!r} must be a non-empty string, got {value!r}")
    return value


def _require_float(mapping: dict[str, object], key: str, where: str) -> float:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SplitError(f"{where}: {key!r} must be a number, got {value!r}")
    return float(value)


def _require_bool(mapping: dict[str, object], key: str, where: str, default: bool) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise SplitError(f"{where}: {key!r} must be true or false, got {value!r}")
    return value


def load_split_config(config_dir: Path) -> dict[str, object]:
    """Read config/splits.yaml and refuse the settings that make a backtest meaningless."""
    path = config_dir / SPLITS_FILENAME
    raw: dict[str, object] = load_yaml(path)
    walk_forward = raw.get("walk_forward")
    if not isinstance(walk_forward, dict):
        raise SplitError(f"{path}: the walk_forward block is missing")
    if _require_bool(walk_forward, "shuffle", f"{path}: walk_forward", False):
        # A shuffled temporal split is plan §18's rejection trigger, not a knob.
        raise SplitError(
            f"{path}: shuffle must be false — a random split on transactions is leakage"
        )
    if _require_bool(walk_forward, "purge", f"{path}: walk_forward", True) is False:
        raise SplitError(f"{path}: purge: false is not supported; plan §8's split is purged")
    return raw


def build_walk_forward(
    timeline: pl.DataFrame,
    *,
    registry: FeatureRegistry,
    config_dir: Path | None = None,
    ts_column: str = TS_COLUMN,
    observed_window: tuple[datetime, datetime] | None = None,
) -> SplitPlan:
    """Resolve the configured fractional fold boundaries against the observed timeline.

    ``timeline`` needs only a timestamp column — the canonical event frame or the
    feature frame both work, because the split is temporal.

    ``observed_window`` is the timeline the fractions are resolved *against*, stated by the
    caller rather than inferred from the frame handed in. It exists because the fractions in
    config/splits.yaml are fractions of a specific observed window, and a downstream stage
    that re-derives that window from a narrower frame silently moves every boundary. Measured
    on 2026-09-27: the score stage resolved them against the event window
    ``2014-01-02T01:30:57.117619Z..2016-01-01T01:24:02.347054Z`` and printed fold 0 as
    ``train <= 2014-07-09T18:16:52.686449Z``, while the account-grain corpus it landed ends at
    ``2015-12-20T16:45:37.523020Z`` — re-deriving from that put fold 0's cutoff at
    ``2014-07-06T08:29:21Z``, three days earlier, and no fold boundary matched the run that
    produced the bytes. Passing the recorded window reproduces the printed plan to the
    microsecond. This is a *wiring* argument, not a knob: it supplies the observation, and
    every boundary is still derived here. The window must contain the frame's own span, and a
    frame with rows outside it is a named refusal rather than rows that fall in no fold.
    """
    root = config_dir if config_dir is not None else find_repo_root() / "config"
    raw = load_split_config(root)
    pipeline = load_pipeline_config(root.parent)
    walk_forward = raw.get("walk_forward")
    assert isinstance(walk_forward, dict)
    embargo_days = _require_int(walk_forward, "embargo_days", "walk_forward")
    # `scheme` was declared in config/splits.yaml and read by nobody, which made
    # `expanding_window` a comment: the loop below started each fold's training window at the
    # previous fold's test end, which is a *sliding* window. The two are not equivalent, and
    # the difference is not cosmetic — under a sliding window fold N's training cutoff lands
    # one embargo width before its own training start for any ladder whose boundaries touch,
    # so the shipped five-fold ladder raised on every fold after the first.
    scheme = _require_str(walk_forward, "scheme", "walk_forward")
    if scheme not in {EXPANDING, SLIDING}:
        raise SplitError(f"walk_forward.scheme is {scheme!r}; supported: {EXPANDING}, {SLIDING}")
    expanding = scheme == EXPANDING
    purge_days = _require_int(walk_forward, "purge_days", "walk_forward", 0)
    # The outcome window the purge reads. config/splits.yaml declares no
    # `label_window_days`, so this falls back to `purge_days` = 1 day against a 30-day
    # withheld band — which means the purge removes nothing on the shipped numbers, because
    # every row it could reach the embargo has already withheld. That is a fact about the
    # configuration and `fold_audit` prints the measured `purged_rows` per fold so it is
    # visible in the artifact rather than argued from a comment; it is not a reason to let
    # the two predicates collapse into one, because the mask must still be right on the
    # corpora where the outcome window is as wide as the band.
    label_window_days = _require_int(walk_forward, "label_window_days", "walk_forward", purge_days)
    if embargo_days != registry.max_lookback_days:
        raise SplitError(
            f"walk_forward.embargo_days is {embargo_days} but the longest declared feature window "
            f"is {registry.max_lookback_days}d. A shorter embargo lets a boundary row's feature "
            "window reach across it; config/features.yaml and config/splits.yaml must agree "
            "(plan §8)."
        )
    if purge_days < 0 or label_window_days < 0:
        raise SplitError("purge_days and label_window_days cannot be negative")
    if embargo_days < max(purge_days, label_window_days):
        raise SplitError(
            f"embargo {embargo_days}d is shorter than the purge/label window "
            f"{max(purge_days, label_window_days)}d; the boundary would not be withheld"
        )

    fold_specs = walk_forward.get("folds")
    if not isinstance(fold_specs, list) or not fold_specs:
        raise SplitError("walk_forward.folds must list the fold boundaries")
    validation = raw.get("validation")
    if not isinstance(validation, dict):
        raise SplitError("the validation block is missing")
    validation_fraction = _require_float(validation, "fraction_of_train", "validation")
    if not 0.0 < validation_fraction < 1.0:
        raise SplitError("validation.fraction_of_train must be inside (0, 1)")

    bounds = timeline.select(
        pl.col(ts_column).min().alias("start"), pl.col(ts_column).max().alias("end")
    ).row(0)
    start, end = bounds[0], bounds[1]
    if observed_window is not None:
        recorded_start, recorded_end = observed_window
        if recorded_start is None or recorded_end is None:
            raise SplitError("observed_window must give both ends of the recorded timeline")
        if start is not None and recorded_start > start:
            raise SplitError(
                f"the recorded timeline starts at {recorded_start}, before the frame's own "
                f"{start}; the frame holds rows outside the window the fractions resolve "
                "against, which would leave them in no fold"
            )
        if end is not None and recorded_end < end:
            raise SplitError(
                f"the recorded timeline ends at {recorded_end}, before the frame's own {end}; "
                "the frame holds rows outside the window the fractions resolve against, "
                "which would leave them in no fold"
            )
        start, end = recorded_start, recorded_end
    if start is None or end is None or not start < end:
        raise SplitError(f"the timeline {start}..{end} cannot be split into folds")
    span_seconds = (end - start).total_seconds()

    folds: list[Fold] = []
    previous_test_end = start
    for position, spec in enumerate(fold_specs):
        if not isinstance(spec, dict):
            raise SplitError(f"walk_forward.folds[{position}] must be a mapping")
        typed: dict[str, object] = dict(spec)
        train_end_frac = _require_float(typed, "train_end_frac", f"folds[{position}]")
        test_end_frac = _require_float(typed, "test_end_frac", f"folds[{position}]")
        index = _require_int(typed, "index", f"folds[{position}]")
        if not 0.0 < train_end_frac < test_end_frac <= 1.0:
            raise SplitError(
                f"folds[{position}]: need 0 < train_end_frac < test_end_frac <= 1, got "
                f"{train_end_frac}, {test_end_frac}"
            )
        test_end = start + timedelta(seconds=test_end_frac * span_seconds)
        # train_end_frac names the end of the history available to this fold. The
        # embargo pulls the *training cutoff* back from it, and the withheld band
        # between the two belongs to neither side.
        test_start = start + timedelta(seconds=train_end_frac * span_seconds)
        train_end = test_start - timedelta(days=embargo_days)
        # Expanding: every fold sees all history from the timeline start. Sliding:
        # each fold starts where the previous one stopped scoring, which is a smaller
        # training set by design and must be chosen, not defaulted into.
        train_start = start if expanding else (previous_test_end if position else start)
        if train_start > train_end:
            raise SplitError(
                f"folds[{position}]: the embargo puts the training cutoff ({train_end}) before the "
                "training start; the timeline is shorter than the configured embargo"
            )
        if test_start >= test_end:
            raise SplitError(f"folds[{position}]: empty test window {test_start}..{test_end}")
        validation_start = train_end - timedelta(
            seconds=validation_fraction * (train_end - train_start).total_seconds()
        )
        folds.append(
            Fold(
                fold_id=f"wf-{index}",
                index=index,
                train_start_ts=train_start,
                train_end_ts=train_end,
                validation_start_ts=validation_start,
                test_start_ts=test_start,
                test_end_ts=test_end,
                purge_days=purge_days,
                label_window_days=label_window_days,
                seed=pipeline.seed,
            )
        )
        previous_test_end = test_end

    return SplitPlan(
        seed=pipeline.seed,
        embargo_days=embargo_days,
        folds=tuple(folds),
        entity_split=build_entity_disjoint(timeline, raw=raw, seed=pipeline.seed),
        optimised_on=WALK_FORWARD,
        timeline_start=start,
        timeline_end=end,
        registry_max_lookback_days=registry.max_lookback_days,
        spec_hash=registry.spec_hash,
    )


def build_entity_disjoint(
    timeline: pl.DataFrame,
    *,
    raw: dict[str, object],
    seed: int,
    entity_column: str = ENTITY_COLUMN,
) -> EntitySplit | None:
    """Account-level holdout, sized by the hash bucketing of the entity key.

    The size is *measured* here and refused if it cannot be sampling noise. Bucket
    membership is a per-account draw, so on a small account population the realized share
    can sit far off the requested fraction while every account still lands on exactly one
    side — the disjointness tests stay green and the robustness number means something else
    than its label claims. The bound is the binomial spread of the draw itself: 8 standard
    deviations, floored at one account, is a deviation no fair hash produces by luck, so a
    threshold or hash that is off scale fails the build instead of the model card.
    """
    block = raw.get("entity_disjoint")
    if not isinstance(block, dict) or block.get("enabled") is not True:
        return None
    fraction = _require_float(block, "holdout_fraction", "entity_disjoint")
    if not 0.0 < fraction < 1.0:
        raise SplitError("entity_disjoint.holdout_fraction must be inside (0, 1)")
    if entity_column not in timeline.columns:
        raise SplitError(
            f"entity_disjoint needs the {entity_column!r} column; the feature frame provides it"
        )
    distinct = timeline[entity_column].cast(pl.String).unique(maintain_order=True).sort().to_list()
    total = len(distinct)
    if total < 2:
        raise SplitError(
            f"entity_disjoint needs at least two distinct accounts; the timeline has {total}, "
            "so a holdout would leave no training side to compare against"
        )
    threshold = _holdout_threshold(fraction)
    holdout = [account for account in distinct if _entity_bucket(account, seed) < threshold]
    measured = len(holdout) / total
    if not holdout or len(holdout) == total:
        raise SplitError(
            f"entity_disjoint.holdout_fraction={fraction} buckets "
            f"{len(holdout)} of {total} accounts, leaving "
            f"{'no holdout' if not holdout else 'no training side'}: the robustness check "
            "would be vacuous. Widen the fraction or seed, or run the temporal split alone."
        )
    # The deviation a fair draw on this population cannot explain: a normal band on the
    # binomial count at z = 3.9 (about a 1-in-1000 two-sided event), plus the half-account
    # continuity correction that keeps the bound usable at all on a small population.
    # This is deliberately silent on a few dozen accounts, where a threshold off by 2x is
    # statistically indistinguishable from noise — which is exactly why the measured share
    # is published on the plan and printed by `SplitPlan.as_report_line` rather than being
    # represented by the requested fraction.
    spread = math.sqrt(fraction * (1.0 - fraction) / total)
    tolerance = 3.9 * spread + 0.5 / total
    if abs(measured - fraction) > tolerance:
        raise SplitError(
            f"entity_disjoint requested {fraction:.3f} of {total} accounts and the hash "
            f"bucketing put {len(holdout)} ({measured:.3f}) in the holdout — a deviation of "
            f"{abs(measured - fraction):.3f}, more than the {tolerance:.3f} a fair draw on "
            "this population explains. The split labelled 'entity-disjoint' is not the split "
            "the configuration asked for."
        )
    return EntitySplit(
        holdout_fraction=fraction,
        holdout_accounts=len(holdout),
        total_accounts=total,
        seed=seed,
    )


def assert_entity_disjoint(
    frame: pl.DataFrame, split: EntitySplit, entity_column: str = ENTITY_COLUMN
) -> int:
    """Measured count of entities that appear on both sides. Must be zero.

    Asserted against the data rather than argued from the construction: the whole point
    of the split is that an account cannot be in training and holdout at once, and the
    only evidence that holds is the number.
    """
    masked = holdout_mask(frame, split, entity_column)
    both = (
        frame.with_columns(masked.alias("_holdout"))
        .group_by(entity_column)
        .agg(pl.col("_holdout").n_unique().alias("_sides"))
        .filter(pl.col("_sides") > 1)
    )
    return both.height


def analysis_windows(
    timeline: pl.DataFrame,
    *,
    window_days: int,
    overlap_hours: int,
    ts_column: str = TS_COLUMN,
) -> pl.DataFrame:
    """Tumbling analysis windows, each reading back ``overlap_hours`` before its start.

    Plan §8: "windows overlap by the longest rule lookback; deduplicate hits by pattern
    signature, not by window." A structuring ladder that spans midnight has to be visible
    in at least one window, and visible once — which is the pair this function and
    :func:`dedupe_by_pattern_signature` implement together.
    """
    if window_days <= 0 or overlap_hours < 0:
        raise SplitError("window_days must be positive and overlap_hours non-negative")
    bounds = timeline.select(
        pl.col(ts_column).min().alias("start"), pl.col(ts_column).max().alias("end")
    ).row(0)
    start, end = bounds[0], bounds[1]
    if start is None or end is None:
        raise SplitError("no timeline, so no analysis windows")
    step = timedelta(days=window_days)
    overlap = timedelta(hours=overlap_hours)
    rows: list[tuple[str, datetime, datetime, datetime]] = []
    cursor = start
    index = 0
    while cursor <= end:
        window_end = cursor + step
        rows.append((f"w{index}", cursor - overlap, cursor, window_end))
        cursor = window_end
        index += 1
    return pl.DataFrame(
        rows,
        schema=["window_id", "read_from_ts", "window_start_ts", "window_end_ts"],
        orient="row",
    )


def dedupe_by_pattern_signature(
    hits: pl.DataFrame,
    *,
    signature_column: str = "pattern_signature",
    order: Sequence[str] = (TS_COLUMN, TXN_ID_COLUMN),
) -> pl.DataFrame:
    """One row per (account, rule, signature): the earliest hit survives.

    The signature is produced by the rule layer over the event set that caused the hit,
    so the same ladder seen from two overlapping windows carries the same signature and
    collapses to one alert. Deduplicating by *window* instead would report a
    boundary-spanning pattern once per window it touches, which inflates both the
    evidence panel and the hit rate a threshold is tuned against.
    """
    for column in (signature_column, *order):
        if column not in hits.columns:
            raise SplitError(f"the hit frame is missing {column!r}, which deduplication needs")
    partition = [column for column in (ACCOUNT_COLUMN, "rule_id") if column in hits.columns]
    if not partition:
        raise SplitError("the hit frame must carry at least one of 'account' or 'rule_id'")
    return hits.sort([*partition, signature_column, *order]).unique(
        subset=[*partition, signature_column], keep="first", maintain_order=True
    )


def fold_audit(plan: SplitPlan, frame: pl.DataFrame, *, ts_column: str = TS_COLUMN) -> pl.DataFrame:
    """Measured per-fold row counts on each side of each boundary.

    Printed rather than assumed: an embargo that silently withholds 40 % of the corpus,
    or a fold that ends up with no positives, is visible here and nowhere else before the
    metrics start being quoted.

    ``purged_rows`` is the count the *purge* alone removed — available history that the
    embargo left in place and the outcome window took out. It is separated from
    ``embargo_rows`` because a purge that removes nothing is indistinguishable, in a report
    that only counts the withheld band, from a purge that was never wired up: a zero here is
    the honest statement that the embargo is doing all the work at this label window, and a
    plan whose purged count is large is one whose labels arrive late.
    """
    rows: list[tuple[str, int, int, int, int, int, int]] = []
    for fold in plan.folds:
        stamps = frame[ts_column]
        embargo_rows = frame.filter(
            (stamps > fold.train_end_ts) & (stamps < fold.test_start_ts)
        ).height
        available = frame.filter(fold.train_mask(ts_column)).height
        rows.append(
            (
                fold.fold_id,
                frame.filter(fold.train_rows_mask(ts_column)).height,
                frame.filter(fold.validation_mask(ts_column)).height,
                frame.filter(fold.test_mask(ts_column)).height,
                embargo_rows,
                frame.filter(stamps < fold.train_start_ts).height,
                available - frame.filter(fold.train_rows_mask(ts_column)).height,
            )
        )
    return pl.DataFrame(
        rows,
        schema=[
            "fold_id",
            "train_rows",
            "validation_rows",
            "test_rows",
            "embargo_rows",
            "before_split_rows",
            "purged_rows",
        ],
        orient="row",
    )


def require_reported(plan: SplitPlan, rendered: str) -> None:
    """Fail when a rendered report does not carry the split flag.

    Plan §8 asks for a flag "that downstream reporting must print". A flag nobody prints
    is a flag that can be ignored, so the check runs on the rendered text rather than on
    the plan object.
    """
    line = plan.as_report_line()
    if plan.which_split_was_optimised_on not in rendered or line.split(" | ")[0] not in rendered:
        raise SplitError(
            "the report does not state which split was optimised on "
            "(which_split_was_optimised_on); render SplitPlan.as_report_line() (spec §7.1)"
        )


__all__ = [
    "ACCOUNT_COLUMN",
    "ENTITY_COLUMN",
    "ENTITY_DISJOINT",
    "EXPANDING",
    "SLIDING",
    "SPLITS_FILENAME",
    "TS_COLUMN",
    "WALK_FORWARD",
    "EntitySplit",
    "Fold",
    "SplitError",
    "SplitPlan",
    "analysis_windows",
    "assert_entity_disjoint",
    "build_entity_disjoint",
    "build_walk_forward",
    "dedupe_by_pattern_signature",
    "fold_audit",
    "holdout_mask",
    "is_holdout",
    "load_split_config",
    "require_reported",
]
