"""The feature-table builder: canonical events in, as-of-correct matrix out.

WHAT THIS MODULE OWNS. The input contract (canonical event v1, checked at the boundary
rather than assumed), the entity-event expansion every kernel reads, the declared-order
evaluation loop, the fold-scoped joins, the money guards, winsorisation, the finite and
label checks at the publish boundary, and the feature-spec hash that travels with the
table. What it does not own is any feature: that list is config/features.yaml, and this
file contains no feature name.

GRAIN. The matrix has one row per ``(txn_id, entity)``: each event is scored from the
payer's side and from the payee's side, because "is this account behaving oddly" is a
different question on each side and the same event has to be answered twice. That is also
why every entry declares a ``group_by`` — the partition is the entity (plus currency for
money, plus counterparty for pair counts), never the corpus.

THE MONEY GUARDS, AND WHY THEY ARE HERE RATHER THAN IN A REVIEW NOTE. Currency is part of
the partition key of every money aggregate, so ``amount_in_30d_minor`` cannot blend two
currencies by construction; the build *still* refuses a frame where one account shows two
currencies, because a partitioned sum that silently splits one account's history into two
partitions is a different wrong answer, and the honest response to unexpected data is to
stop and name it (plan §8, 03 D). Zero-amount rows are kept and flagged, and the value
features exclude them by declaring a ``where`` predicate — which is what makes the
exclusion visible in the registry instead of baked into a kernel.

DETERMINISM. Computed rows are ordered ``(entity, event_ts_utc, txn_id)``; published rows
``(event_ts_utc, txn_id, entity)``. No wall-clock value reaches an artifact, so the same
input with the same registry and the same RUN_SALT produces byte-identical Parquet
(01 A rule 4).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl

from oxbow.backtest.splits import Fold
from oxbow.contracts.canonical_v1 import CANONICAL_COLUMNS, MONEY_COLUMNS
from oxbow.features.fold_scope import (
    ACCOUNT,
    GraphFeatureProvider,
    GraphScopeError,
    RuleHitProvider,
    require_graph_provider,
    require_rule_provider,
    rule_matrix,
    table_rows,
)
from oxbow.features.kinds import (
    ENTITY,
    EVENT_TS,
    TXN_ID,
    KernelCache,
    KernelContext,
    KernelError,
    POSITION,
    PREDICATE_PREFIX,
    ROW_UNIT,
    ROW_VALUE_PREFIX,
    dispatch,
    fold_column,
    predicate_column,
    row_value_column,
)
from oxbow.features.registry import FeatureRegistry

LABEL_FRAUD: Final = "label_is_fraud"
LABEL_FLAGGED: Final = "label_is_flagged"
LABEL_TYPOLOGY: Final = "label_typology"
INGESTED_AT: Final = "ingested_at"
BATCH_ID: Final = "batch_id"
SOURCE_DATASET: Final = "source_dataset"
CURRENCY: Final = "currency"
DIRECTION_SIGN: Final = "direction_sign"
COUNTERPARTY: Final = "counterparty"
BALANCE_BEFORE: Final = "balance_before_minor"
BALANCE_AFTER: Final = "balance_after_minor"
SELF_TRANSFER_ROW: Final = "is_self_transfer_row"
REVERSAL_OF: Final = "reversal_of_txn_id"
CUTOFF_TS: Final = "cutoff_ts"

PUBLISHED_KEYS: Final = ("txn_id", "entity", EVENT_TS, CUTOFF_TS)
ONE_DAY_SECONDS: Final = 86_400


class FeatureTableError(RuntimeError):
    """Base class for build failures that must stop the run."""


class InputContractError(FeatureTableError):
    """The frame handed to the builder is not canonical event v1."""


class CrossCurrencyAggregationError(FeatureTableError):
    """One account shows more than one currency, so no money aggregate can be summed.

    Plan §8: "currency is part of every amount; aggregations group by currency or fail,
    no implicit FX." Failing is the only option that does not invent an exchange rate.
    """


class NonFiniteFeatureError(FeatureTableError):
    """A NaN or infinity reached the matrix boundary, naming the column.

    LightGBM tolerates a non-finite value and the WOE scorecard does not, so a table that
    passes one silently diverges from the other (03 H). The run stops here instead.
    """


class LabelInMatrixError(FeatureTableError):
    """A label column, or something correlated with it past the guard, reached the matrix."""


class FeatureHashMismatchError(FeatureTableError):
    """The matrix being scored was built from a different feature spec than training used."""


@dataclass(frozen=True, slots=True)
class WinsorBounds:
    """Per-column clipping limits, fitted on a training slice and nothing else.

    Fitting the clip points on the full corpus would use test-period values to choose
    them, which is the scaler-fitting leak spec §7.2 lists alongside the window one. The
    bounds travel with the table: "features are winsorised" is *stated* in the artifact
    rather than being inferable from a column that looks like money but is not (03 D).
    """

    limits: Mapping[str, tuple[int, int]]
    lower_percentile: float
    upper_percentile: float
    fit_scope: str
    fit_rows: int

    @classmethod
    def fit(
        cls,
        frame: pl.DataFrame,
        ids: Sequence[str],
        *,
        registry: FeatureRegistry,
        fit_scope: str = "training_slice",
    ) -> WinsorBounds:
        """Percentile clip points from exactly the rows handed in, which must be training rows."""
        lower = registry.guards.winsorise_lower
        upper = registry.guards.winsorise_upper
        limits: dict[str, tuple[int, int]] = {}
        for column in ids:
            if column not in frame.columns:
                raise FeatureTableError(f"winsorisation: {column!r} is not a computed column")
            series = frame[column].cast(pl.Int64)
            low = series.quantile(lower, interpolation="lower")
            high = series.quantile(upper, interpolation="lower")
            if low is None or high is None:
                continue
            limits[column] = (int(low), int(high))
        return cls(
            limits=limits,
            lower_percentile=lower,
            upper_percentile=upper,
            fit_scope=fit_scope,
            fit_rows=frame.height,
        )

    def apply(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Clip the fitted columns. Everything else is left untouched."""
        expressions = [
            pl.col(column).clip(low, high).alias(column)
            for column, (low, high) in sorted(self.limits.items())
            if column in frame.columns
        ]
        return frame.with_columns(expressions) if expressions else frame

    def statement(self) -> str:
        """The sentence printed beside any winsorised figure."""
        if not self.limits:
            return "winsorisation: fitted on the training slice; no column reached a bound"
        return (
            f"features winsorised at p{self.lower_percentile:g}/p{self.upper_percentile:g} fitted "
            f"on {self.fit_scope} ({self.fit_rows} rows); {len(self.limits)} column(s) clipped; "
            "reported money is raw and unclipped"
        )


@dataclass(frozen=True, slots=True)
class BuildOptions:
    """Per-build inputs the registry cannot express: the fold, the seal, the fits."""

    fold: Fold | None = None
    graph: GraphFeatureProvider | None = None
    rules: RuleHitProvider | None = None
    sealed_before_ts: datetime | None = None
    winsor_bounds: WinsorBounds | None = None


@dataclass(frozen=True, slots=True)
class BuildReport:
    """What the build measured. Written beside the table; contains no wall clock."""

    spec_hash: str
    feature_count: int
    intermediate_count: int
    outcome_count: int
    rows_in: int
    entity_rows: int
    late_arrival_count: int
    fold_id: str
    graph_supplied: bool
    rules_supplied: bool
    null_counts: Mapping[str, int]
    winsorisation: str
    labels_carried_outside_matrix: bool
    distinct_currencies: tuple[str, ...]
    timeline_start: str
    timeline_end: str
    max_lookback_days: int
    label_column: str = LABEL_FRAUD
    max_abs_label_correlation: float | None = None
    max_abs_label_correlation_column: str = ""
    label_correlation_note: str = ""


@dataclass(frozen=True, slots=True)
class FeatureTable:
    """A published feature matrix plus the provenance that makes it checkable."""

    matrix: pl.DataFrame
    outcomes: pl.DataFrame
    keys: pl.DataFrame
    report: BuildReport
    registry: FeatureRegistry

    @property
    def spec_hash(self) -> str:
        """The feature-spec hash for this exact column set and order."""
        return self.report.spec_hash

    def assert_feature_hash_matches(self, training_hash: str) -> None:
        """Refuse to score a matrix built from a different spec than training used.

        02 B seam 3. This is the check that stops "trained on one feature set, scored
        with another" — a failure mode that raises nothing anywhere and quietly produces
        scores whose columns mean something else.
        """
        if self.report.spec_hash != training_hash:
            raise FeatureHashMismatchError(
                f"feature spec hash mismatch: this matrix is {self.report.spec_hash}, the "
                f"training artifact recorded {training_hash}. Refusing to score. Rebuild the "
                "training side from the current registry or check out the spec the model was "
                "fitted on; do not paste the hash forward."
            )

    def sentence(self, feature_id: str) -> str:
        """The plain-English label for a column: the SHAP panel and scorecard text."""
        return self.registry[feature_id].sentence

    def write(self, directory: Path) -> dict[str, Path]:
        """Write the matrix, the outcomes and the sidecar; return the paths written."""
        directory.mkdir(parents=True, exist_ok=True)
        matrix_path = directory / "features.parquet"
        outcomes_path = directory / "outcomes.parquet"
        sidecar_path = directory / "features_manifest.json"
        self.matrix.write_parquet(matrix_path, statistics=False)
        self.outcomes.write_parquet(outcomes_path, statistics=False)
        payload = {
            "report": asdict(self.report),
            "sentences": self.registry.sentences(),
            "dtypes": {
                key: str(value) for key, value in sorted(self.registry.matrix_dtypes().items())
            },
            "null_policies": {
                entry.id: entry.null_policy for entry in self.registry.matrix_entries
            },
            "groups": {entry.id: entry.group for entry in self.registry.matrix_entries},
        }
        sidecar_path.write_text(
            json.dumps(payload, sort_keys=True, indent=2, default=str), encoding="utf-8"
        )
        return {"matrix": matrix_path, "outcomes": outcomes_path, "manifest": sidecar_path}


def assert_input_contract(events: pl.DataFrame) -> None:
    """Check the frame is canonical event v1: every column present, none extra, money int64.

    The column list is imported from the contract module rather than restated here,
    because a second copy of a contract is a second thing that can drift.
    """
    missing = [column for column in CANONICAL_COLUMNS if column not in events.columns]
    if missing:
        raise InputContractError(
            f"the frame is not canonical event v1: missing {missing}. The contract is "
            f"{list(CANONICAL_COLUMNS)}."
        )
    unexpected = [column for column in events.columns if column not in CANONICAL_COLUMNS]
    if unexpected:
        raise InputContractError(
            f"the frame carries columns outside canonical event v1: {unexpected}. Either they "
            "are internals that must not reach features, or the contract changed and this "
            "layer has not caught up."
        )
    for column in MONEY_COLUMNS:
        dtype = events.schema[column]
        if dtype != pl.Int64:
            raise InputContractError(f"{column} must be Int64 minor units, got {dtype} (DEV-005)")
    if not isinstance(events.schema[EVENT_TS], pl.Datetime):
        raise InputContractError(f"{EVENT_TS} must be a datetime, got {events.schema[EVENT_TS]}")
    if not isinstance(events.schema[INGESTED_AT], pl.Datetime):
        raise InputContractError(
            f"{INGESTED_AT} must be a datetime; the seal comparison depends on it"
        )
    if not events.schema["local_hour"].is_integer():
        raise InputContractError(f"local_hour must be an integer hour, got {events.schema['local_hour']}")
    if events.filter(pl.col(CURRENCY).is_null()).height:
        raise InputContractError("currency is part of every amount (plan §8); null is not a currency")


def assert_single_currency_per_account(events: pl.DataFrame) -> None:
    """Fail when one account participates in more than one currency.

    Money aggregates already partition by currency, so this is not the guard that prevents
    a mixed sum. It prevents the other failure: one account's history split across two
    partitions, each reporting a plausible-looking fraction of the truth.
    """
    stacked = events.select(pl.col("account_from").alias(ACCOUNT), pl.col(CURRENCY)).vstack(
        events.select(pl.col("account_to").alias(ACCOUNT), pl.col(CURRENCY))
    )
    offenders = (
        stacked.group_by(ACCOUNT)
        .agg(
            pl.col(CURRENCY).n_unique().alias("currencies"),
            pl.col(CURRENCY).unique().sort().alias("which"),
        )
        .filter(pl.col("currencies") > 1)
        .sort(ACCOUNT)
    )
    if offenders.height:
        sample = offenders.head(5).select([ACCOUNT, "which"]).rows()
        raise CrossCurrencyAggregationError(
            f"{offenders.height} account(s) show more than one currency, e.g. {sample}. Money "
            "aggregations group by currency or fail; there is no implicit FX (plan §8)."
        )


def entity_event_frame(events: pl.DataFrame) -> pl.DataFrame:
    """Explode each canonical event into one row per participating account.

    ``direction_sign`` is +1 on the receiving side and -1 on the sending side, which lets
    one predicate vocabulary answer "inflow" and "outflow" without carrying two frames.
    Balances become the *scoring entity's own* before/after pair, so a balance-proxy
    feature never has to know which side of the transaction it was written on.
    """
    debit = events.select(
        pl.col(TXN_ID),
        pl.col(EVENT_TS),
        pl.col("event_date_local"),
        pl.col("local_hour"),
        pl.col("txn_type"),
        pl.col("channel"),
        pl.col("amount_minor"),
        pl.col(CURRENCY),
        pl.col("account_from").alias(ENTITY),
        pl.col("account_to").alias(COUNTERPARTY),
        pl.col("src_balance_before_minor").alias(BALANCE_BEFORE),
        pl.col("src_balance_after_minor").alias(BALANCE_AFTER),
        pl.lit(-1, dtype=pl.Int8).alias(DIRECTION_SIGN),
        pl.col(LABEL_FRAUD),
        pl.col(LABEL_FLAGGED),
        pl.col(SOURCE_DATASET),
        pl.col(BATCH_ID),
        pl.col(INGESTED_AT),
    )
    credit = events.select(
        pl.col(TXN_ID),
        pl.col(EVENT_TS),
        pl.col("event_date_local"),
        pl.col("local_hour"),
        pl.col("txn_type"),
        pl.col("channel"),
        pl.col("amount_minor"),
        pl.col(CURRENCY),
        pl.col("account_to").alias(ENTITY),
        pl.col("account_from").alias(COUNTERPARTY),
        pl.col("dst_balance_before_minor").alias(BALANCE_BEFORE),
        pl.col("dst_balance_after_minor").alias(BALANCE_AFTER),
        pl.lit(1, dtype=pl.Int8).alias(DIRECTION_SIGN),
        pl.col(LABEL_FRAUD),
        pl.col(LABEL_FLAGGED),
        pl.col(SOURCE_DATASET),
        pl.col(BATCH_ID),
        pl.col(INGESTED_AT),
    )
    stacked = debit.vstack(credit).sort([ENTITY, EVENT_TS, TXN_ID])
    return stacked.with_columns(
        pl.lit(1, dtype=pl.Int64).alias(ROW_UNIT),
        pl.int_range(0, stacked.height, dtype=pl.Int64).alias(POSITION),
        (pl.col(ENTITY) == pl.col(COUNTERPARTY)).alias(SELF_TRANSFER_ROW),
        pl.col(EVENT_TS).alias(CUTOFF_TS),
    )


def _run_start(column: str) -> pl.Expr:
    """True on the first row of a run of `column`, the frame being run-sorted.

    ``fill_null`` is load-bearing: the shifted comparison is null on row zero, and a null
    condition leaves the first partition without a start value, which silently nulls out
    every cumulative quantity for that partition.
    """
    return (pl.col(column) != pl.col(column).shift(1)).fill_null(True)


def materialise_row_values(frame: pl.DataFrame) -> pl.DataFrame:
    """The declared row-derived quantities, all strictly backward or strictly local."""
    with_previous = frame.with_columns(
        pl.when(_run_start(ENTITY))
        .then(None)
        .otherwise(pl.col(EVENT_TS).shift(1))
        .alias("_previous_event_ts")
    )
    gaps = with_previous.select(
        (pl.col(EVENT_TS) - pl.col("_previous_event_ts")).dt.total_seconds().alias("_gap_seconds")
    )["_gap_seconds"]
    return with_previous.with_columns(
        gaps.round(0).cast(pl.Int64).alias(row_value_column("gap_since_previous_event_s")),
        (gaps / ONE_DAY_SECONDS).floor().cast(pl.Int64).alias(
            row_value_column("gap_since_previous_event_days")
        ),
        (
            pl.col(BALANCE_AFTER) - pl.col(BALANCE_BEFORE) - pl.col(DIRECTION_SIGN) * pl.col("amount_minor")
        )
        .abs()
        .cast(pl.Int64)
        .alias(row_value_column("balance_delta_abs_minor")),
    )


def materialised_predicates(
    registry: FeatureRegistry, sealed_before: datetime | None
) -> dict[str, pl.Expr]:
    """The predicate vocabulary, materialised as `_pred_*` columns before any kernel runs.

    Expressions rather than python lambdas, so Polars folds them into its own pass, and
    named exactly as the registry names them, so a ``where`` in YAML resolves to a column
    without an intermediate lookup table that could fall out of step.
    """
    overnight = list(registry.guards.overnight_local_hours)
    reversal_types = list(registry.guards.reversal_txn_types)
    if sealed_before is None:
        late: pl.Expr = pl.lit(False)
    else:
        seal = pl.lit(sealed_before, dtype=pl.Datetime("us", UTC))
        late = (pl.col(EVENT_TS) <= seal) & (pl.col(INGESTED_AT) > seal)
    gap = pl.col(row_value_column("gap_since_previous_event_s"))
    delta = pl.col(row_value_column("balance_delta_abs_minor"))
    predicates: dict[str, pl.Expr] = {
        "zero_amount": pl.col("amount_minor") == 0,
        "is_nonzero": pl.col("amount_minor") != 0,
        "is_inflow": pl.col(DIRECTION_SIGN) > 0,
        "is_outflow": pl.col(DIRECTION_SIGN) < 0,
        "is_inflow_nonzero": (pl.col(DIRECTION_SIGN) > 0) & (pl.col("amount_minor") != 0),
        "is_outflow_nonzero": (pl.col(DIRECTION_SIGN) < 0) & (pl.col("amount_minor") != 0),
        "is_inflow_not_self": (pl.col(DIRECTION_SIGN) > 0) & ~pl.col(SELF_TRANSFER_ROW),
        "is_outflow_not_self": (pl.col(DIRECTION_SIGN) < 0) & ~pl.col(SELF_TRANSFER_ROW),
        "self_transfer": pl.col(SELF_TRANSFER_ROW),
        "not_self_transfer": ~pl.col(SELF_TRANSFER_ROW),
        "reversal_txn": pl.col("txn_type").is_in(reversal_types),
        "has_prior_event": gap.is_not_null(),
        "has_balance": pl.col(BALANCE_AFTER).is_not_null(),
        "has_balance_delta": delta.is_not_null(),
        "balance_delta_mismatch": delta.is_not_null() & (delta > 0),
        "zero_balance_after": pl.col(BALANCE_AFTER).is_not_null() & (pl.col(BALANCE_AFTER) == 0),
        # 03 C: a human-hours question reads local_hour. Nothing here derives an hour from
        # event_ts_utc, which is the defect that would flag an East African morning.
        "overnight_local_hour": pl.col("local_hour").is_in(overnight),
        "weekend_local_date": pl.col("event_date_local").dt.weekday() >= 6,
        "late_arrival": late,
    }
    if sealed_before is not None:
        # Materialise `always` only under a seal. Unsealed, "always" is every row and
        # `_resolve_where` correctly returns no filter at all; under a seal it means
        # "every row this seal admits", which is the one place a whole-population feature
        # (`txn_count_in_24h`) would otherwise let a late arrival move a sealed number.
        predicates["always"] = pl.lit(True)
    return predicates


def link_reversals(frame: pl.DataFrame, registry: FeatureRegistry) -> pl.DataFrame:
    """Type and link reversals to the event they reverse, keeping both visible.

    A reversal is matched to the most recent non-reversal event between the same pair in
    the opposite direction. The link is what makes ``test_reversal_not_a_cycle``
    meaningful: without it a refund reads as a fresh payment back to the originator, and a
    two-edge cycle is manufactured out of a bookkeeping entry (plan §8, 03 D).
    """
    reversal_types = list(registry.guards.reversal_txn_types)
    is_reversal = pl.col("txn_type").is_in(reversal_types)
    originals = frame.filter(~is_reversal).select(
        pl.col(ENTITY).alias("_o_dst"),
        pl.col(COUNTERPARTY).alias("_o_src"),
        pl.col(EVENT_TS).alias("_o_ts"),
        pl.col(TXN_ID).alias(REVERSAL_OF),
    )
    probes = frame.filter(is_reversal).select(
        pl.col(TXN_ID),
        pl.col(ENTITY).alias("_o_dst"),
        pl.col(COUNTERPARTY).alias("_o_src"),
        pl.col(EVENT_TS).alias("_p_ts"),
    )
    if probes.height == 0:
        return frame.with_columns(pl.lit(None, dtype=pl.String).alias(REVERSAL_OF))
    joined = probes.join(originals, on=["_o_dst", "_o_src"], how="inner").filter(
        pl.col("_o_ts") < pl.col("_p_ts")
    )
    if joined.height == 0:
        return frame.with_columns(pl.lit(None, dtype=pl.String).alias(REVERSAL_OF))
    best = (
        joined.sort([TXN_ID, "_o_ts", REVERSAL_OF])
        .unique(subset=[TXN_ID], keep="last", maintain_order=True)
        .select([TXN_ID, REVERSAL_OF])
    )
    return frame.join(best, on=TXN_ID, how="left", maintain_order="left")


def fold_scoped_columns(
    frame: pl.DataFrame,
    registry: FeatureRegistry,
    *,
    fold: Fold | None,
    graph: GraphFeatureProvider | None,
    rules: RuleHitProvider | None,
) -> pl.DataFrame:
    """Attach the fold's graph and rule attributes as pre-joined `_fold_*` columns.

    A null here means "this fold's graph carries no such node" or "the rule did not fire
    inside the fold", both of which the registry states as a ``null_reason``. A *missing
    field* fails instead, because "this account has no edges" and "nobody computed this"
    are different claims and only one of them is a finding.
    """
    entries = [entry for entry in registry.entries if entry.as_of == "fold_scoped"]
    if not entries:
        return frame
    if fold is None:
        return frame.with_columns(
            [pl.lit(None, dtype=entry.polars_dtype).alias(fold_column(entry.id)) for entry in entries]
        )
    work = frame
    graph_entries = [entry for entry in entries if entry.kind == "graph_node"]
    rule_entries = [entry for entry in entries if entry.kind == "rule_field"]
    if graph_entries:
        table = None if graph is None else graph.fold_graph(fold)
        if table is None:
            work = work.with_columns(
                [
                    pl.lit(None, dtype=entry.polars_dtype).alias(fold_column(entry.id))
                    for entry in graph_entries
                ]
            )
        else:
            nodes = table_rows(table, fold).rename({ACCOUNT: ENTITY})
            renames = {
                entry.graph_field: fold_column(entry.id)
                for entry in graph_entries
                if entry.graph_field
            }
            work = work.join(
                nodes.select([ENTITY, *renames]).rename(renames),
                on=ENTITY,
                how="left",
                maintain_order="left",
            )
    if rule_entries:
        hits = None if rules is None else rules.fold_rule_hits(fold)
        if hits is None:
            work = work.with_columns(
                [
                    pl.lit(None, dtype=entry.polars_dtype).alias(fold_column(entry.id))
                    for entry in rule_entries
                ]
            )
        else:
            matrix = rule_matrix(hits, fold, registry).rename({ACCOUNT: ENTITY})
            renames = {entry.rule_id: fold_column(entry.id) for entry in rule_entries if entry.rule_id}
            work = work.join(
                matrix.select([ENTITY, *renames]).rename(renames),
                on=ENTITY,
                how="left",
                maintain_order="left",
            )
    return work


def assert_labels_absent(matrix: pl.DataFrame, registry: FeatureRegistry) -> None:
    """No label column, and nothing declared as one, may reach the matrix."""
    banned = set(registry.guards.banned_sources)
    found = sorted(banned.intersection(matrix.columns))
    if found:
        raise LabelInMatrixError(
            f"label column(s) {found} reached the feature matrix; spec §7.2 forbids it, and a "
            "model trained on the answer scores perfectly and means nothing."
        )


def assert_features_finite(matrix: pl.DataFrame, registry: FeatureRegistry) -> None:
    """Fail the run, naming the column, on a non-finite value at the boundary (03 H)."""
    if not registry.guards.require_finite:
        return
    for column in matrix.columns:
        if not isinstance(matrix.schema[column], pl.Float64):
            continue
        series = matrix[column]
        bad = int(series.is_nan().sum() + series.is_infinite().sum())
        if bad:
            raise NonFiniteFeatureError(
                f"feature column {column!r} carries {bad} non-finite value(s) at the feature-"
                "table boundary. LightGBM would tolerate them and the scorecard would not, and "
                "the two models would then disagree in silence."
            )


def label_correlations(
    computed: pl.DataFrame,
    registry: FeatureRegistry,
    *,
    label_column: str = LABEL_FRAUD,
) -> Mapping[str, float]:
    """Absolute Pearson correlation of every published column with the fraud label.

    Only rows where both sides are observed contribute, and a column with no overlap or no
    spread reports nothing rather than a fabricated 0.0.
    """
    if label_column not in computed.columns:
        raise FeatureTableError(f"{label_column!r} is not on the computed frame to correlate against")
    out: dict[str, float] = {}
    target = computed[label_column].cast(pl.Float64)
    if target.n_unique() < 2:
        return out
    for column in registry.matrix_ids:
        series = computed[column]
        if not (series.dtype.is_integer() or isinstance(series.dtype, pl.Float64)):
            continue
        both = computed.select(
            series.cast(pl.Float64).alias("_x"), target.alias("_y")
        ).drop_nulls()
        if both.height < 2 or both["_x"].n_unique() < 2:
            continue
        # `pl.corr` as an expression rather than `Series.corr`: the latter was removed from
        # the 1.x Series API, and a call that raises AttributeError inside a guard is a
        # guard that silently never ran.
        value = both.select(pl.corr("_x", "_y", method="pearson").alias("_r"))["_r"].item()
        if value is not None and value == value:
            out[column] = abs(float(value))
    return out


def assert_no_label_leakage(
    computed: pl.DataFrame, matrix: pl.DataFrame, registry: FeatureRegistry
) -> Mapping[str, float]:
    """Assert the label columns are absent *and* nothing correlates past the guard."""
    assert_labels_absent(matrix, registry)
    correlations = label_correlations(computed, registry)
    ceiling = registry.guards.max_abs_correlation_with_label
    offenders = {column: value for column, value in correlations.items() if value > ceiling}
    if offenders:
        worst = max(offenders.items(), key=lambda pair: pair[1])
        raise LabelInMatrixError(
            f"{len(offenders)} feature(s) correlate with the label above {ceiling}; the worst is "
            f"{worst[0]} at {worst[1]:.4f}. Either it is derived from the label or it encodes "
            "the labelling rule itself (spec §7.2)."
        )
    return correlations


class FeatureTableBuilder:
    """Evaluate the registry against canonical events, honouring the fold and the seal."""

    def __init__(self, registry: FeatureRegistry) -> None:
        self.registry = registry

    def prepare(
        self,
        events: pl.DataFrame,
        *,
        fold: Fold | None = None,
        graph: GraphFeatureProvider | None = None,
        rules: RuleHitProvider | None = None,
        sealed_before_ts: datetime | None = None,
    ) -> pl.DataFrame:
        """The work frame every kernel reads: contract-checked, expanded, sealed, joined."""
        assert_input_contract(events)
        assert_single_currency_per_account(events)
        chosen_graph = None if graph is None else require_graph_provider(graph)
        chosen_rules = None if rules is None else require_rule_provider(rules)
        frame = entity_event_frame(events)
        frame = link_reversals(frame, self.registry)
        frame = fold_scoped_columns(
            frame, self.registry, fold=fold, graph=chosen_graph, rules=chosen_rules
        )
        frame = materialise_row_values(frame)
        predicates = materialised_predicates(self.registry, sealed_before_ts)
        return frame.with_columns(
            [expression.alias(predicate_column(name)) for name, expression in predicates.items()]
        )

    def compute_frame(
        self,
        events: pl.DataFrame,
        *,
        fold: Fold | None = None,
        graph: GraphFeatureProvider | None = None,
        rules: RuleHitProvider | None = None,
        sealed_before_ts: datetime | None = None,
        drop_late_from_windows: bool = True,
    ) -> pl.DataFrame:
        """Evaluate every declared column, in declared order, keyed by ``(txn_id, entity)``.

        Exposed apart from :meth:`build` because the leakage gate compares *all* computed
        columns — matrix and outcome — and because late rows must be withheld from window
        inputs before any window is evaluated, not corrected afterwards.

        ``drop_late_from_windows`` is the sealed-window rule: a row that arrived after its
        own window was sealed is excluded from window inputs, so a published value cannot
        change under a later arrival. It stays in the frame, flagged, and is admitted by
        the next build with a later seal.
        """
        frame = self.prepare(
            events,
            fold=fold,
            graph=graph,
            rules=rules,
            sealed_before_ts=sealed_before_ts,
        )
        late_column = predicate_column("late_arrival")
        window_filter_names = {
            entry.where for entry in self.registry.entries if entry.where and entry.where != "always"
        }
        if drop_late_from_windows and sealed_before_ts is not None:
            for name in list(frame.columns):
                if name.startswith(PREDICATE_PREFIX) and name != late_column:
                    frame = frame.with_columns(
                        pl.when(pl.col(late_column)).then(False).otherwise(pl.col(name)).alias(name)
                    )
        context = KernelContext(registry=self.registry, cache=KernelCache())
        for entry in self.registry.entries:
            values = dispatch(entry.kind)(frame, entry, context)
            if values.len() != frame.height:
                raise KernelError(
                    f"feature {entry.id} produced {values.len()} values for {frame.height} rows"
                )
            frame = frame.with_columns(values.alias(entry.id))
            # A `where` may name a declared bool feature rather than a primitive predicate
            # (`is_zero_value`, `is_reversal`). Those columns are created inside this loop,
            # so the seal is applied here: publish the row-local fact unguarded, and hand
            # the window kernels a late-guarded alias of the same name. `_resolve_where`
            # prefers the alias, which is what keeps a sealed window sealed.
            if (
                drop_late_from_windows
                and sealed_before_ts is not None
                and entry.dtype == "bool"
                and entry.id in window_filter_names
            ):
                frame = frame.with_columns(
                    pl.when(pl.col(late_column))
                    .then(pl.lit(False))
                    .otherwise(values)
                    .cast(pl.Boolean)
                    .alias(predicate_column(entry.id))
                )
        return frame

    def build(
        self,
        events: pl.DataFrame,
        *,
        options: BuildOptions | None = None,
        label_column: str = LABEL_FRAUD,
    ) -> FeatureTable:
        """Produce the published matrix, its outcomes and its build report."""
        build_options = options or BuildOptions()
        computed = self.compute_frame(
            events,
            fold=build_options.fold,
            graph=build_options.graph,
            rules=build_options.rules,
            sealed_before_ts=build_options.sealed_before_ts,
        )
        registry = self.registry
        matrix_ids = list(registry.matrix_ids)
        late_count = int(computed.filter(pl.col(predicate_column("late_arrival"))).height)
        bounds = build_options.winsor_bounds
        if bounds is not None:
            computed = bounds.apply(computed)
            winsorisation = bounds.statement()
        elif registry.guards.winsorise_enabled:
            winsorisation = (
                "winsorisation: enabled in the registry but not applied — no training-slice "
                "bounds were supplied, and clipping at a bound fitted on the test period "
                "would be the leak this guard exists to prevent"
            )
        else:
            winsorisation = "winsorisation: disabled in the registry"
        order = [EVENT_TS, TXN_ID, ENTITY]
        matrix = computed.select([*PUBLISHED_KEYS, *matrix_ids]).sort(order)
        outcomes = computed.select([*PUBLISHED_KEYS, *registry.outcome_ids]).sort(order)
        keys = computed.select(
            [*PUBLISHED_KEYS, CURRENCY, SOURCE_DATASET, BATCH_ID]
        ).sort(order)
        for column, dtype in registry.matrix_dtypes().items():
            if matrix.schema[column] != dtype:
                raise FeatureTableError(
                    f"feature {column} was published as {matrix.schema[column]}; the registry "
                    f"declares {dtype}"
                )
        assert_features_finite(matrix, registry)
        assert_labels_absent(matrix, registry)
        correlations: Mapping[str, float] = {}
        note: str
        if label_column not in computed.columns:
            # Absence of the label column is the guard that matters, and it already ran. A
            # corpus without labels (a scoring run, an unlabelled slice) reports that no
            # correlation was measured rather than reporting a reassuring zero.
            note = (
                f"label correlation: not measured — {label_column!r} is not on the input frame. "
                "The matrix was still checked for the presence of every banned label column."
            )
        else:
            correlations = assert_no_label_leakage(computed, matrix, registry)
            ceiling = registry.guards.max_abs_correlation_with_label
            note = (
                f"label correlation: max |pearson r| against {label_column} over "
                f"{len(correlations)} numeric column(s), guard at {ceiling}"
            )
        worst = max(correlations.items(), key=lambda pair: pair[1], default=("", None))
        stamps = computed[EVENT_TS]
        report = BuildReport(
            spec_hash=registry.spec_hash,
            feature_count=len(matrix_ids),
            intermediate_count=len(registry.intermediate_ids),
            outcome_count=len(registry.outcome_ids),
            rows_in=events.height,
            entity_rows=computed.height,
            late_arrival_count=late_count,
            fold_id=build_options.fold.fold_id if build_options.fold else "unsegmented",
            graph_supplied=build_options.graph is not None,
            rules_supplied=build_options.rules is not None,
            null_counts={column: int(matrix[column].null_count()) for column in matrix_ids},
            winsorisation=winsorisation,
            labels_carried_outside_matrix=all(
                column in computed.columns and column not in matrix.columns
                for column in (LABEL_FRAUD, LABEL_FLAGGED)
            ),
            distinct_currencies=tuple(sorted(computed[CURRENCY].unique().to_list())),
            timeline_start=(
                min(stamps).isoformat() if computed.height else ""  # type: ignore[union-attr]
            ),
            timeline_end=(
                max(stamps).isoformat() if computed.height else ""  # type: ignore[union-attr]
            ),
            max_lookback_days=registry.max_lookback_days,
            label_column=label_column,
            max_abs_label_correlation=None if worst[1] is None else float(worst[1]),
            max_abs_label_correlation_column=str(worst[0]),
            label_correlation_note=note,
        )
        return FeatureTable(
            matrix=matrix, outcomes=outcomes, keys=keys, report=report, registry=registry
        )


def training_hash_of(table: FeatureTable) -> str:
    """The hash a training artifact records, so scoring can compare against it."""
    return table.spec_hash


def edge_frame_for_fold(
    events: pl.DataFrame, fold: Fold, registry: FeatureRegistry
) -> pl.DataFrame:
    """The fold's edges — reversals typed and linked, self-transfers and zero values kept.

    This is the frame the P3a layer is asked to build a graph from. The exclusion that
    keeps reversals out of cycle detection is applied by :func:`cycle_eligible_edges`,
    which is the frame the cycle fields must come from.
    """
    frame = link_reversals(entity_event_frame(events), registry)
    inside = frame.filter(
        (pl.col(EVENT_TS) >= fold.train_start_ts - timedelta(days=fold.label_window_days))
        & (pl.col(EVENT_TS) <= fold.graph_as_of_ts)
        & (pl.col(DIRECTION_SIGN) < 0)
    )
    return inside.select(
        pl.col(ENTITY).alias("src"),
        pl.col(COUNTERPARTY).alias("dst"),
        pl.col(TXN_ID),
        pl.col(EVENT_TS),
        pl.col("amount_minor"),
        pl.col(CURRENCY),
        pl.col("txn_type"),
        pl.col("txn_type").is_in(list(registry.guards.reversal_txn_types)).alias("is_reversal"),
        pl.col(REVERSAL_OF),
        (pl.col("amount_minor") == 0).alias("is_zero_value"),
        # Spelled from the source columns rather than from the `src`/`dst` aliases above:
        # expressions inside one `select` cannot see that select's own output names, so the
        # alias form raises ColumnNotFoundError the first time anything calls this.
        (pl.col(ENTITY) == pl.col(COUNTERPARTY)).alias("is_self_transfer"),
    ).sort([EVENT_TS, TXN_ID, "src"])


def cycle_eligible_edges(edges: pl.DataFrame) -> pl.DataFrame:
    """The edge set a cycle enumeration may read: no reversals, no zero values, no loops.

    Plan §7 and §8 together: a self-loop trivially satisfies a cycle, a zero-value edge
    cannot retain value, and a reversal is a refund rather than a lap of a loop. Keeping
    the filter here — rather than trusting each caller to remember it — is what makes
    ``test_reversal_not_a_cycle`` a property of the seam instead of a habit.
    """
    return edges.filter(~pl.col("is_reversal") & ~pl.col("is_zero_value") & ~pl.col("is_self_transfer"))


def assert_edges_within_fold(edges: pl.DataFrame, fold: Fold) -> None:
    """Refuse an edge frame that reaches past the fold's graph cutoff."""
    if not edges.height:
        return
    stamps = edges[EVENT_TS]
    latest = stamps.max()
    if latest is not None and latest > fold.graph_as_of_ts:
        beyond = int((stamps > fold.graph_as_of_ts).sum())
        raise GraphScopeError(
            f"{beyond} edge(s) at or after {fold.graph_as_of_ts.isoformat()} were handed to fold "
            f"{fold.fold_id!r}; the graph a fold reads must come from that fold's edges only"
        )


def __getattr__(name: str) -> object:
    """Re-export :func:`oxbow.features.compute.build_feature_table` lazily.

    ``compute.py`` imports this module, so a top-level ``from oxbow.features.compute import
    build_feature_table`` here would be an import cycle. PEP 562 resolves it at attribute
    time instead, which lets the one seam be reached by either spelling
    (``features.build.build_feature_table`` or ``features.compute.build_feature_table``) with
    a single implementation and no duplicated logic. The home of the function is
    ``compute.py``; this is an alias for callers that think of the builder and its seam as
    one module, which is a reasonable thing to think.
    """
    if name == "build_feature_table":
        from oxbow.features.compute import build_feature_table

        return build_feature_table
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BALANCE_AFTER",
    "BALANCE_BEFORE",
    "BuildOptions",
    "BuildReport",
    "COUNTERPARTY",
    "CUTOFF_TS",
    "CrossCurrencyAggregationError",
    "DIRECTION_SIGN",
    "FeatureHashMismatchError",
    "FeatureTable",
    "FeatureTableBuilder",
    "FeatureTableError",
    "InputContractError",
    "LABEL_FRAUD",
    "LabelInMatrixError",
    "NonFiniteFeatureError",
    "PUBLISHED_KEYS",
    "REVERSAL_OF",
    "SELF_TRANSFER_ROW",
    "WinsorBounds",
    "assert_edges_within_fold",
    "assert_features_finite",
    "assert_input_contract",
    "assert_labels_absent",
    "assert_no_label_leakage",
    "assert_single_currency_per_account",
    "build_feature_table",
    "cycle_eligible_edges",
    "edge_frame_for_fold",
    "entity_event_frame",
    "label_correlations",
    "link_reversals",
    "materialise_row_values",
    "materialised_predicates",
    "training_hash_of",
]
