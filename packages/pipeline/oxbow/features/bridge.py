"""The grain bridge: a ``txn_id x entity`` matrix in, an ``account_key x as_of_ts x fold`` frame out.

WHAT THIS MODULE EXISTS FOR. ``oxbow.features.build.FeatureTable`` publishes one row per
``(txn_id, entity)``: each event is answered from the payer's side and from the payee's
side, because "is this account behaving oddly" is a different question on each side.
``oxbow.scoring.frame.build_training_frame`` consumes one row per
``(account_key, as_of_ts, fold)`` carrying ``label_is_fraud`` and ``feature_spec_hash``.
Nothing bridged the two, so the scorecard, the GBM, the forest, the calibration, the fusion
and SHAP could not run at all: not because the maths was missing, but because no module
owned the change of grain. This is that module. The composition root hands in events, a
registry and the split plan; it does not assemble a frame by hand.

THE ONE DECISION THIS MODULE MAKES, STATED PLAINLY. **An account-grain row is a carry of
one whole matrix row, never an aggregate of several.** For an account and an instant ``T``
the bridge selects the account's own most recent scored row at or before ``T`` under the
total order ``(event_ts_utc, txn_id)`` — its *anchor event* — and carries that row's
columns unchanged. The anchor is exactly the row the feature layer computed as of ``T``:
every value on it was produced from data at or before its own ``event_ts_utc``, so the
carried row is a statement about the account at ``T`` and nothing in it needed the account's
later history. That is plan §8's legality test, and it is the reason no pooling is performed
here: pooling *across* instants — averaging a 30-day sum over the days it covers, say —
would manufacture an account-level number out of quantities that already answer the
account-level question at their own cutoffs, and the result would be a second, undocumented
definition of every feature.

WHY THE INSTANT IS THE ROW'S OWN TIMESTAMP. The matrix already scores the account at each
of its events, so the set of instants on which an account has a legal state is the set of
its own event timestamps. The bridge therefore emits one row per
``(account, distinct event_ts_utc)``: no synthetic as-of grid is invented, and no as-of
instant appears that the feature layer never evaluated.

WHERE THE TWO GRAINS GENUINELY DISAGREE, AND WHAT HAPPENS THERE. Seven published columns
are facts about the *transaction* rather than about the account: the four row-local
declarations (``is_reversal``, ``dormancy_days_before_now``, ``local_hour_code``,
``txn_type_code``) and the three whose declared partition is finer than the account
(``typical_movement_30d_minor`` and ``largest_movement_30d_minor`` split by ``direction``,
``pair_visit_count_30d`` by ``counterparty``). Read alone, none of them has an account-level
value: "how large was this account's largest movement" is unanswerable without knowing
which side of the network is being asked about, and "how many times did it visit this
counterparty" needs a counterparty. They are carried, not averaged, and they carry *with the
anchor event*: at instant ``T`` the account's largest in-and-out-of-the-same-kind movement
is its largest movement of the kind of its most recent event, and its pair count is against
that event's counterparty. The bridge therefore (a) refuses to compute any statistic over a
set of matrix rows for these columns, (b) names them in
:attr:`BridgeReport.anchor_relative_columns` so a reader of the artifact sees which columns
depend on the anchor and why, and (c) measures how many instants had more than one event to
choose between — the ambiguity is reported rather than silently resolved. A caller that
cannot accept an anchor-relative column has to change the registry, not this file: a column
the TrainingFrame contract does not carry is a column the scorecard cannot bin, so "omit it"
is not available to a frame that must publish the declared spec in full, and inventing a
pooling for it would be the failure mode the report exists to make visible.

THE LABEL CROSSES THE SEAM, THE MATRIX NEVER DOES. ``label_is_fraud`` and
``label_typology`` are joined from the canonical events on ``txn_id`` — the columns the
feature matrix is asserted free of at its own publish boundary
(:func:`oxbow.features.build.assert_labels_absent`). They land in the TrainingFrame's
contract columns, which ``oxbow.scoring.frame`` validates as *not* features, so the guard
that fired on the run — labels carried outside the matrix — is satisfied by this module
rather than silenced: nothing here can put a label into a feature column, because the
feature list comes from ``registry.matrix_ids`` and the registry refuses a label-named
feature at load. ``label_is_fraud`` is the maximum over the events an account had at that
instant rather than the anchor's own value, which is the one place a set statistic is used:
an account whose instant contains a fraudulent event was involved in fraud at that instant,
and dropping that positive because a later-sorting event at the same second was clean would
train the model on an answer the corpus has.

WINSORISATION, FIT WHERE IT IS FIT. ``guards.winsorise.fit_scope`` is
``training_slice_only``, and the builder refuses to clip at bounds it has not been handed.
The clip points here are fitted per fold on that fold's own training rows — instants at or
before ``fold.train_end_ts`` — and applied to the fold's evaluation rows afterwards. Applying
a clip after carrying a row is the same arithmetic as applying it before: the carry selects
one scalar and ``clip`` is a per-value function, so no column in the published frame differs
from the builder's own path.

MONEY. Every amount stays Int64 minor units; this module adds no arithmetic to a money
column at all — it selects rows and casts identity columns. The one quantity it computes is
``label_is_fraud``, an int8 0/1 flag, by maximum.

DETERMINISM. Rows are ordered ``(as_of_ts, account_key)`` on output, the anchor is chosen by
the declared total order ``(event_ts_utc, txn_id)``, folds are walked in plan order, and no
wall clock reaches the frame: the same events, registry, plan and providers produce
byte-identical Parquet (01 A rule 4).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl

from oxbow.backtest.splits import Fold, SplitPlan
from oxbow.config import PipelineConfig
from oxbow.dtypes import as_moment
from oxbow.features.build import (
    LABEL_FRAUD,
    LABEL_TYPOLOGY,
    FeatureTable,
    WinsorBounds,
    assert_input_contract,
)
from oxbow.features.compute import assert_totals_match_rendered_rows, build_feature_table
from oxbow.features.fold_scope import GraphFeatureProvider, RuleHitProvider
from oxbow.features.kinds import ENTITY, EVENT_TS, TXN_ID
from oxbow.features.registry import FeatureRegistry

ACCOUNT_KEY: Final = "account_key"
AS_OF_TS: Final = "as_of_ts"
FOLD: Final = "fold"
SPEC_HASH: Final = "feature_spec_hash"
ROLE_FIT: Final = "fit"
ROLE_SCORED: Final = "scored"
ONE_SECOND: Final = timedelta(seconds=1)

#: The published columns the carry-forward resolves against the anchor event rather than
#: against the account. Every entry is named, with its reason, in
#: :func:`anchor_relative_columns`; the set is asserted against the registry at build time so
#: an eighth transaction-relative declaration cannot slip through unremarked.
_ROW_LOCAL_KINDS: Final[frozenset[str]] = frozenset({"row_flag", "row_value", "event_field"})
#: Finer than the account: a direction-side statistic and a per-counterparty count.
_FINER_THAN_ACCOUNT_PARTITIONS: Final[frozenset[str]] = frozenset({"direction", "counterparty"})


class GrainBridgeError(RuntimeError):
    """The grains could not be reconciled, naming the column or the fold at fault."""


class LabelUnavailableError(GrainBridgeError):
    """The corpus carries no usable ``label_is_fraud``, so no frame can be trained on it."""


class SpecHashDisagreementError(GrainBridgeError):
    """Two feature tables built for one run disagree about the spec they were built from."""


def _moment(value: object, *, column: str, bound: str) -> str:
    """One edge of the bridged timeline as an instant, or a refusal naming the column."""
    moment = as_moment(value)
    if moment is None:
        raise GrainBridgeError(
            f"{column} {bound}: the bridged frame yielded {value!r} "
            f"({type(value).__name__}) where its temporal contract promised a timestamp; the "
            "bridge report would otherwise publish a timeline string that is not an instant"
        )
    return moment.isoformat()


@dataclass(frozen=True, slots=True)
class FoldBridgeRow:
    """What one fold contributed: rows, accounts, positives, and the fold's own horizon."""

    fold_id: str
    index: int
    rows: int
    accounts: int
    positives: int
    horizon_ts: str
    fit_rows: int
    scored_rows: int
    skipped_reason: str | None


@dataclass(frozen=True, slots=True)
class BridgeReport:
    """What the bridge measured. Written beside the frame; contains no wall clock."""

    spec_hash: str
    rows: int
    accounts: int
    positives: int
    base_rate: float
    folds: int
    max_lookback_days: int
    timeline_start: str
    timeline_end: str
    per_fold: tuple[FoldBridgeRow, ...]
    anchor_relative_columns: tuple[str, ...]
    anchor_ambiguous_instants: int
    matrix_feature_count: int
    winsorisation: str
    labels_carried_outside_matrix: bool
    distinct_currencies: tuple[str, ...]

    def sentence(self) -> str:
        return (
            f"account-grain frame: {self.rows} rows x {self.matrix_feature_count} features "
            f"across {self.accounts} accounts and {self.folds} folds, "
            f"{self.positives} positives (base rate {self.base_rate:.6f}), "
            f"{self.anchor_ambiguous_instants} instant(s) had more than one event to choose an "
            f"anchor between; {len(self.anchor_relative_columns)} column(s) are anchor-relative"
        )


@dataclass(frozen=True, slots=True)
class AccountGrainFrame:
    """The bridged frame plus the report that makes it auditable.

    ``frame`` is the raw contract-shaped frame: it has not been through
    :func:`oxbow.scoring.frame.build_training_frame`, because that validator belongs to the
    scoring layer and deciding the provenance label, the categorical set and the validation
    fraction is the caller's contract with its own config. What this type guarantees is
    narrower and checkable: the columns, dtypes, uniqueness, label shape and single spec
    hash that validator will go on to enforce.
    """

    frame: pl.DataFrame
    report: BridgeReport
    registry: FeatureRegistry
    tables: Mapping[str, FeatureTable] = field(default_factory=dict, repr=False, compare=False)

    @property
    def spec_hash(self) -> str:
        return self.report.spec_hash

    def write(self, directory: Path) -> dict[str, Path]:
        """Land the frame and its sidecar; return the paths written."""
        directory.mkdir(parents=True, exist_ok=True)
        frame_path = directory / "account_frame.parquet"
        sidecar_path = directory / "account_frame_manifest.json"
        self.frame.write_parquet(frame_path, statistics=False)
        payload = {
            "report": asdict(self.report),
            "folds": [asdict(row) for row in self.report.per_fold],
            "dtypes": {name: str(dtype) for name, dtype in sorted(self.frame.schema.items())},
            "spec_hash": self.spec_hash,
            "tables": {
                fold_id: {
                    "fold_id": table.report.fold_id,
                    "rows": table.matrix.height,
                    "graph_supplied": table.report.graph_supplied,
                    "rules_supplied": table.report.rules_supplied,
                    "null_counts": dict(sorted(table.report.null_counts.items())),
                }
                for fold_id, table in sorted(self.tables.items())
            },
        }
        sidecar_path.write_text(
            json.dumps(payload, sort_keys=True, indent=2, default=str), encoding="utf-8"
        )
        return {"frame": frame_path, "manifest": sidecar_path}


def anchor_relative_columns(registry: FeatureRegistry) -> tuple[str, ...]:
    """Published columns whose account-grain meaning is inherited from the anchor event.

    Derived from the declarations rather than hard-coded, so the list cannot drift away from
    ``config/features.yaml``: a row-local kind or a partition finer than the account is
    exactly the property that makes the column transaction-relative, and both are stated in
    the registry.
    """
    out: list[str] = []
    for entry in registry.matrix_entries:
        if entry.kind in _ROW_LOCAL_KINDS or set(entry.group_by) & _FINER_THAN_ACCOUNT_PARTITIONS:
            out.append(entry.id)
    return tuple(out)


def fold_membership(plan: SplitPlan, moment: datetime) -> Fold:
    """The fold a row is *scored* in, by the plan's own boundaries.

    ``SplitPlan.fold_for`` is the sanctioned lookup and is used unchanged, so this module
    derives no boundary of its own. The one case it does not cover is an instant at or
    before the first fold's test start: that history belongs to no evaluation window, and
    it is the material every later fold fits on, so it is filed under the first fold. The
    choice is reported (``fit_rows`` per fold) rather than left for a reader to notice.
    """
    found = plan.fold_for(moment)
    if found is not None:
        return found
    first = plan.folds[0]
    if moment > first.test_start_ts:
        raise GrainBridgeError(
            f"instant {moment.isoformat()} falls in no fold's test window and after fold "
            f"{first.fold_id!r}'s test start; the plan has a gap and the bridge will not guess "
            "which side of it a row belongs to"
        )
    return first


def build_account_frame(
    events: pl.DataFrame,
    registry: FeatureRegistry,
    plan: SplitPlan,
    *,
    graph: GraphFeatureProvider,
    rules: RuleHitProvider,
    cfg: PipelineConfig | None = None,
    winsorise: bool = True,
) -> AccountGrainFrame:
    """Bridge the declared feature matrix to the account-grain TrainingFrame shape.

    One feature-table build per fold, each with that fold's own sealed graph and rule-hit
    tables and each truncated at that fold's own horizon, then one carry per
    ``(account, instant)`` in the fold that scores it. The fold's horizon is
    ``fold.feature_as_of_ts``: no fold's build reads a row after its own scoring cutoff, so
    an earlier fold's rows are computed from strictly less history than a later fold's,
    which is what the expanding-window ladder claims.

    Raises :class:`GrainBridgeError` rather than emitting a frame it cannot defend: a label
    the corpus does not carry, a matrix column that went missing, a duplicated
    ``(account_key, as_of_ts)``, or two folds built from different specs.
    """
    assert_input_contract(events)
    if LABEL_FRAUD not in events.columns:
        raise LabelUnavailableError(
            f"{LABEL_FRAUD!r} is not on the input frame: the bridge cannot produce a training "
            "frame without labels, and filling the gap with zeros would train a model on an "
            "answer nobody observed (03 A rule 2)"
        )
    if not plan.folds:
        raise GrainBridgeError("the split plan carries no folds, so there is nothing to bridge")
    labels = _event_labels(events)
    parts: list[pl.DataFrame] = []
    fold_rows: list[FoldBridgeRow] = []
    statements: list[str] = []
    tables: dict[str, FeatureTable] = {}
    currencies: set[str] = set()
    ambiguous = 0
    anchor_columns = anchor_relative_columns(registry)

    for fold in plan.folds:
        horizon = fold.feature_as_of_ts
        fold_events = events.filter(pl.col(EVENT_TS) <= horizon)
        if fold_events.height == 0:
            fold_rows.append(
                FoldBridgeRow(
                    fold_id=fold.fold_id,
                    index=fold.index,
                    rows=0,
                    accounts=0,
                    positives=0,
                    horizon_ts=horizon.isoformat(),
                    fit_rows=0,
                    scored_rows=0,
                    skipped_reason=(
                        f"no event at or before {horizon.isoformat()}: the fold's history is "
                        "empty, so it contributes no rows rather than a row of nulls"
                    ),
                )
            )
            continue
        table = build_feature_table(
            fold_events,
            registry,
            fold=fold,
            graph_features=graph,
            rules=rules,
            cfg=cfg,
            label_column=LABEL_FRAUD,
        )
        # The builder's own money identity, run per fold: a fold that dropped or duplicated a
        # scored side would change every downstream total without changing a column name.
        assert_totals_match_rendered_rows(fold_events, table)
        if table.spec_hash != registry.spec_hash:
            raise SpecHashDisagreementError(
                f"fold {fold.fold_id!r} published spec hash {table.spec_hash}, the registry "
                f"declares {registry.spec_hash}. The table was not built from the registry it "
                "claims to be scored against."
            )
        tables[fold.fold_id] = table
        accounts, ambiguity = _collapse(table, labels, fold, registry)
        ambiguous += ambiguity
        chosen = _fold_rows(accounts, plan, fold)
        bounds: WinsorBounds | None = None
        if winsorise and registry.guards.winsorise_enabled:
            fit_ids = [column for column in registry.winsorised_ids if column in chosen.columns]
            fit_slice = chosen.filter(pl.col(AS_OF_TS) <= fold.train_end_ts)
            if fit_ids and fit_slice.height:
                bounds = WinsorBounds.fit(
                    fit_slice, fit_ids, registry=registry, fit_scope=f"fold {fold.fold_id} train"
                )
                chosen = bounds.apply(chosen)
                statements.append(bounds.statement())
        else:
            statements.append(
                "winsorisation: "
                + (
                    "disabled in the registry"
                    if not registry.guards.winsorise_enabled
                    else "not fitted (no training rows at or before the fold cutoff)"
                )
            )
        chosen = chosen.with_columns(
            pl.lit(fold.index, dtype=pl.Int32).alias(FOLD),
            pl.lit(table.spec_hash, dtype=pl.String).alias(SPEC_HASH),
        )
        parts.append(chosen)
        fold_rows.append(
            FoldBridgeRow(
                fold_id=fold.fold_id,
                index=fold.index,
                rows=chosen.height,
                accounts=int(chosen[ACCOUNT_KEY].n_unique()),
                positives=int(chosen[LABEL_FRAUD].sum()),
                horizon_ts=horizon.isoformat(),
                fit_rows=int((chosen[AS_OF_TS] <= fold.train_end_ts).sum()),
                scored_rows=int((chosen[AS_OF_TS] > fold.train_end_ts).sum()),
                skipped_reason=None,
            )
        )
        currencies.update(str(item) for item in table.report.distinct_currencies)
    if not parts:
        raise GrainBridgeError(
            "no fold contributed a row: every fold's history was empty. A frame built out of "
            "nothing would validate and train, which is the worst possible outcome."
        )
    frame = pl.concat(parts, how="vertical_relaxed").sort([AS_OF_TS, ACCOUNT_KEY])
    frame = _cast_contract(frame, registry)
    _assert_frame(frame, registry, anchor_columns)
    stamps = frame[AS_OF_TS]
    # The union polars answers a reduction with is the same one every other consumer of a
    # temporal contract faces: `_cast_contract` has already made this column a Datetime, so a
    # non-timestamp here means the cast and the read disagree about the frame, and the bridge
    # report's timeline would then carry `b'2014-01-02'` — a string that reads like an instant.
    timeline_start = _moment(stamps.min(), column=AS_OF_TS, bound="minimum")
    timeline_end = _moment(stamps.max(), column=AS_OF_TS, bound="maximum")
    report = BridgeReport(
        spec_hash=registry.spec_hash,
        rows=frame.height,
        accounts=int(frame[ACCOUNT_KEY].n_unique()),
        positives=int(frame[LABEL_FRAUD].sum()),
        base_rate=float(frame[LABEL_FRAUD].sum()) / frame.height,
        folds=len({row.index for row in fold_rows if row.rows}),
        max_lookback_days=registry.max_lookback_days,
        timeline_start=timeline_start,
        timeline_end=timeline_end,
        per_fold=tuple(fold_rows),
        anchor_relative_columns=anchor_columns,
        anchor_ambiguous_instants=ambiguous,
        matrix_feature_count=len(registry.matrix_ids),
        winsorisation="; ".join(dict.fromkeys(statements)),
        labels_carried_outside_matrix=all(
            column not in registry.matrix_ids for column in (LABEL_FRAUD, LABEL_TYPOLOGY)
        ),
        distinct_currencies=tuple(sorted(currencies)),
    )
    return AccountGrainFrame(frame=frame, report=report, registry=registry, tables=tables)


# --- internals ------------------------------------------------------------


def _event_labels(events: pl.DataFrame) -> pl.DataFrame:
    """``txn_id -> (label_is_fraud, label_typology)``, one row per transaction.

    Read from the canonical events, never from the matrix: the matrix is where the label
    guard says the label must not be, and this is the seam the guard exists to protect. A
    repeated ``txn_id`` is refused rather than resolved by ``keep='first'`` — two events with
    one id would attach somebody else's answer to a row.
    """
    columns = [LABEL_FRAUD] + ([LABEL_TYPOLOGY] if LABEL_TYPOLOGY in events.columns else [])
    distinct = events.select([TXN_ID, *columns]).unique(maintain_order=True, subset=[TXN_ID])
    if distinct.height != events.height:
        raise GrainBridgeError(
            f"{events.height - distinct.height} duplicated {TXN_ID} value(s) on the input frame. "
            "The label is joined on that key, and a transaction with two answers is not a "
            "detail: it is the join picking one at random."
        )
    return distinct.with_columns(
        pl.col(LABEL_FRAUD).cast(pl.Int8, strict=True),
        *(
            [pl.col(LABEL_TYPOLOGY).cast(pl.String)]
            if LABEL_TYPOLOGY in columns
            else [pl.lit(None, dtype=pl.String).alias(LABEL_TYPOLOGY)]
        ),
    )


def _collapse(
    table: FeatureTable,
    labels: pl.DataFrame,
    fold: Fold,
    registry: FeatureRegistry,
) -> tuple[pl.DataFrame, int]:
    """One row per ``(account, instant)``, carried from that account's anchor event.

    Returns the collapsed frame and the number of instants that had more than one event to
    choose between — the ambiguity the carry resolves by the declared total order, measured
    rather than mentioned.
    """
    matrix = table.matrix
    if matrix.height == 0:
        raise GrainBridgeError(
            f"fold {fold.fold_id!r} published an empty matrix from "
            f"{table.report.entity_rows} entity rows; the build dropped every row"
        )
    joined = matrix.join(labels, on=TXN_ID, how="left")
    if joined[LABEL_FRAUD].null_count():
        # The subclass, not the base: this is the "unknown is not a zero" refusal the
        # module already has a name for, and a caller catching GrainBridgeError still
        # gets it.
        raise LabelUnavailableError(
            f"{joined[LABEL_FRAUD].null_count()} matrix row(s) in fold {fold.fold_id!r} have no "
            f"label for their {TXN_ID}; the event the feature layer scored is not the event "
            "the labels came from"
        )
    ordered = joined.sort([ENTITY, EVENT_TS, TXN_ID])
    per_instant = ordered.group_by([ENTITY, EVENT_TS], maintain_order=True).agg(
        pl.len().alias("_events"),
        pl.col(LABEL_FRAUD).max().alias(LABEL_FRAUD),
        (pl.col(LABEL_TYPOLOGY).filter(pl.col(LABEL_FRAUD) == 1).last().alias(LABEL_TYPOLOGY)),
    )
    ambiguous = int(per_instant.filter(pl.col("_events") > 1).height)
    anchors = ordered.unique(subset=[ENTITY, EVENT_TS], keep="last", maintain_order=True).select(
        [ENTITY, EVENT_TS, *registry.matrix_ids]
    )
    collapsed = anchors.join(
        per_instant.select([ENTITY, EVENT_TS, LABEL_FRAUD, LABEL_TYPOLOGY]),
        on=[ENTITY, EVENT_TS],
        how="inner",
    )
    if collapsed.height != anchors.height:
        raise GrainBridgeError(
            f"the label join lost {anchors.height - collapsed.height} anchor row(s) in fold "
            f"{fold.fold_id!r}; an account-instant pair present in the matrix went missing at "
            "the seam"
        )
    return collapsed, ambiguous


def _fold_rows(collapsed: pl.DataFrame, plan: SplitPlan, fold: Fold) -> pl.DataFrame:
    """Keep the rows this fold scores, renamed to the TrainingFrame's contract keys.

    Each fold's build carries every instant up to its own horizon, so the filter is what
    makes one instant belong to exactly one fold: the plan's membership test, not a
    boundary this module invented.
    """
    stamps = collapsed[EVENT_TS].to_list()
    owners = [fold_membership(plan, moment) for moment in stamps]
    keep = pl.Series("keep", [owner.fold_id == fold.fold_id for owner in owners], pl.Boolean)
    return collapsed.filter(keep).rename({ENTITY: ACCOUNT_KEY, EVENT_TS: AS_OF_TS}, strict=True)


def _cast_contract(frame: pl.DataFrame, registry: FeatureRegistry) -> pl.DataFrame:
    """The identity columns as the scoring contract declares them, features untouched."""
    return frame.select(
        pl.col(ACCOUNT_KEY).cast(pl.String),
        pl.col(AS_OF_TS).cast(pl.Datetime("us", UTC)),
        pl.col(FOLD).cast(pl.Int32),
        pl.col(LABEL_FRAUD).cast(pl.Int8),
        pl.col(LABEL_TYPOLOGY).cast(pl.String),
        *[pl.col(entry.id) for entry in registry.matrix_entries],
        pl.col(SPEC_HASH).cast(pl.String),
    )


def _assert_frame(
    frame: pl.DataFrame, registry: FeatureRegistry, anchor_columns: Sequence[str]
) -> None:
    """Every property the scoring validator will check, checked here first.

    Not a duplicate of ``oxbow.scoring.frame._validate``: that validator is the boundary a
    caller must still cross, and this one says *which* grain property failed rather than
    leaving the failure to a frame-level message three layers away.
    """
    expected = [
        ACCOUNT_KEY,
        AS_OF_TS,
        FOLD,
        LABEL_FRAUD,
        LABEL_TYPOLOGY,
        *registry.matrix_ids,
        SPEC_HASH,
    ]
    missing = sorted(set(expected) - set(frame.columns))
    extra = sorted(set(frame.columns) - set(expected))
    if missing or extra:
        raise GrainBridgeError(
            f"the bridged frame does not match the contract: missing {missing}, extra {extra}"
        )
    if frame.schema[AS_OF_TS] != pl.Datetime("us", UTC):
        raise GrainBridgeError(
            f"{AS_OF_TS} must be Datetime[us, UTC], got {frame.schema[AS_OF_TS]}"
        )
    if frame[ACCOUNT_KEY].null_count() or frame[AS_OF_TS].null_count():
        raise GrainBridgeError("an account row without an account or an as-of instant is not a row")
    duplicates = (
        frame.group_by([ACCOUNT_KEY, AS_OF_TS])
        .agg(pl.len().alias("rows"))
        .filter(pl.col("rows") > 1)
    )
    if duplicates.height:
        raise GrainBridgeError(
            f"{duplicates.height} (account_key, as_of_ts) pair(s) carry more than one row, e.g. "
            f"{duplicates.head(3).rows()}: one fold must own each instant, and two folds "
            "claiming one row is the grain contract failing rather than a detail"
        )
    hashes = set(frame[SPEC_HASH].unique().to_list())
    if hashes != {registry.spec_hash}:
        raise SpecHashDisagreementError(
            f"the bridged frame carries spec hashes {sorted(hashes)} but the registry declares "
            f"{registry.spec_hash!r}; one frame is one feature spec"
        )
    labels = set(frame[LABEL_FRAUD].unique().to_list())
    if not labels <= {0, 1}:
        raise GrainBridgeError(f"{LABEL_FRAUD} must be 0/1, found {sorted(labels)}")
    banned = set(registry.guards.banned_sources) & set(registry.matrix_ids)
    if banned:
        raise GrainBridgeError(
            f"label column(s) {sorted(banned)} are published features; the matrix builder "
            "refuses that, so reaching here means a registry that was never loaded"
        )
    # The anchor-relative set is the bridge's disclosure, so it is checked rather than
    # trusted: every name must be a published column, and the derivation must still find
    # the transaction-relative declarations it names. An empty list would mean the registry
    # had been narrowed under this module and the report would be silently claiming that
    # nothing in the frame depends on the anchor event.
    unresolved = sorted(set(anchor_columns) - set(registry.matrix_ids))
    if unresolved:
        raise GrainBridgeError(
            f"anchor-relative columns {unresolved} are not published matrix entries"
        )
    if not anchor_columns:
        raise GrainBridgeError(
            "no published column was found to be row-local or finer-than-account; either the "
            "registry changed under the bridge or the derivation no longer reads it"
        )
    for entry in registry.matrix_entries:
        declared = entry.polars_dtype
        actual = frame.schema[entry.id]
        if actual != declared:
            raise GrainBridgeError(
                f"feature {entry.id} crossed the bridge as {actual}; the registry declares "
                f"{declared}. A money column that became a float is DEV-005, not a rounding."
            )


__all__ = [
    "ACCOUNT_KEY",
    "AS_OF_TS",
    "FOLD",
    "SPEC_HASH",
    "AccountGrainFrame",
    "BridgeReport",
    "GrainBridgeError",
    "LabelUnavailableError",
    "SpecHashDisagreementError",
    "anchor_relative_columns",
    "build_account_frame",
    "fold_membership",
]
