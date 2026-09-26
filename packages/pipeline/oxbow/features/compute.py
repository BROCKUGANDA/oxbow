"""The registry, executed: canonical events in, as-of-correct feature table out.

WHY THIS FILE EXISTS ALONGSIDE ``build.py``. ``build.py`` owns the mechanics — the input
contract, the entity/event expansion, the declared-order evaluation loop, the fold-scoped
joins and the publish-boundary assertions. This module owns the *one call every other layer
makes*: :func:`build_feature_table`. Scoring, backtest and the CLI are told to hand in
events, a registry and a scoring horizon, and nothing else; the seam is narrow on purpose,
because a wide seam is a seam somebody configures wrongly at 2 a.m. during a rerun.

NO FEATURE LOGIC LIVES HERE. Plan §8: the feature list is ``config/features.yaml``, and
``oxbow.features.kinds`` dispatches on the declared ``kind``. This file names no feature,
no window and no aggregation. If a column is not declared in the registry it is not
computed, and if it is declared it cannot be skipped: the evaluation loop walks
``registry.entries`` and the leakage gate walks the same list.

WHAT ``as_of`` DOES AND DOES NOT MEAN. ``as_of`` is the scoring horizon: the last instant
whose events exist for this build. It is not a per-row cutoff — every published value is
computed against its *own* row's ``event_ts_utc`` as cutoff, over the declared lookback,
on rows sorted by ``(entity, event_ts_utc, txn_id)``. That is what "never over the whole
corpus" means operationally, and ``leakage.audit_no_future_reads`` is the proof: delete
every row after a row's own cutoff, recompute, and byte-identical values must come back.

MONEY. Currency is part of the partition key of every money aggregate the registry
declares, and the build additionally refuses a frame where one account shows two
currencies (``assert_single_currency_per_account``), so a cross-currency sum is
structurally unavailable *and* loudly impossible. Amounts stay Int64 minor units end to
end; the only floats are the scores the registry marks ``declared_score_because``.

DETERMINISM. No wall clock enters any artifact. The row order is
``(event_ts_utc, txn_id, entity)`` for published rows and ``(entity, event_ts_utc,
txn_id)`` inside the kernels, both total orders over the canonical contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl

from oxbow.backtest.splits import Fold
from oxbow.config import (
    CONFIG_DIRNAME,
    ConfigError,
    PipelineConfig,
    find_repo_root,
    load_pipeline_config,
)
from oxbow.contracts.canonical_v1 import CANONICAL_COLUMNS
from oxbow.features.build import (
    DIRECTION_SIGN,
    LABEL_FRAUD,
    BuildOptions,
    CrossCurrencyAggregationError,
    FeatureTable,
    FeatureTableBuilder,
    FeatureTableError,
    WinsorBounds,
    assert_input_contract,
    entity_event_frame,
)
from oxbow.features.fold_scope import (
    GraphFeatureProvider,
    RuleHitProvider,
)
from oxbow.features.registry import (
    FeatureRegistry,
    FeatureSpec,
    RegistryError,
    hash_registry,
    parse_window,
    registry_from_config_dir,
)

AMOUNT_MINOR: Final = "amount_minor"
CURRENCY: Final = "currency"
EVENT_TS: Final = "event_ts_utc"
TXN_ID: Final = "txn_id"
ENTITY_KEY: Final = "entity"
ENTITY_ACCOUNTS: Final = ("account_from", "account_to")


class BuildConfigError(ConfigError):
    """The configuration handed to the builder contradicts the registry it must serve."""


def registry_from_repo(root: str | Path | None = None) -> FeatureRegistry:
    """Load ``config/features.yaml`` through the sanctioned config loader.

    Exposed here so a caller never hand-assembles a path: ``find_repo_root`` is the one
    function that knows where configuration lives. ``root`` is a **repository** root,
    matching the name -- the previous body accepted it as the config directory while
    the default branch appended ``config``, so the CLI, which passed a repo root, was
    looking for ``<repo>/features.yaml`` and failing, while the tests, which happened
    to pass ``<repo>/config``, passed. One parameter with two meanings is how a stage
    breaks in production and stays green in CI.
    """
    base = Path(root) if root is not None else find_repo_root()
    return registry_from_config_dir(base / CONFIG_DIRNAME)


# --- the one seam ---------------------------------------------------------


def build_feature_table(
    events: pl.DataFrame,
    registry: FeatureRegistry,
    *,
    graph_features: GraphFeatureProvider | None = None,
    as_of: datetime | None = None,
    cfg: PipelineConfig | None = None,
    fold: Fold | None = None,
    rules: RuleHitProvider | None = None,
    sealed_before_ts: datetime | None = None,
    winsor_bounds: WinsorBounds | None = None,
    label_column: str = LABEL_FRAUD,
) -> FeatureTable:
    """Compute the registry's declared columns over ``events`` and publish the matrix.

    ``graph_features`` and ``rules`` are *providers*, not frames, and passing a bare
    ``pl.DataFrame`` is refused on purpose: a whole-corpus table cannot be shown to
    contain only this fold's edges, which is the property plan §8 requires of every graph
    feature. Build the seam with ``oxbow.features.fold_scope.graph_provider_from_callable``
    around the P3a table and the fold's own edges.

    ``as_of`` truncates the corpus to the scoring horizon before anything is computed, so
    a backtest cannot read a row that had not happened yet. ``sealed_before_ts`` is the
    later of "the instant this build's windows were sealed" and enables the late-arrival
    exclusion; a row that arrived after the seal stays visible and flagged but stops
    entering other rows' windows.
    """
    assert_input_contract(events)
    assert_public_contract(registry)
    assert_registry_groups_money_by_currency(registry)
    chosen = _config_or_default(cfg)
    horizon = _apply_horizon(events, as_of, cfg=chosen)
    builder = FeatureTableBuilder(registry)
    return builder.build(
        horizon,
        options=BuildOptions(
            fold=fold,
            graph=graph_features,
            rules=rules,
            sealed_before_ts=sealed_before_ts,
            winsor_bounds=winsor_bounds,
        ),
        label_column=label_column,
    )


def _config_or_default(cfg: PipelineConfig | None) -> PipelineConfig:
    if cfg is not None:
        return cfg
    return load_pipeline_config()


def _apply_horizon(
    events: pl.DataFrame, as_of: datetime | None, *, cfg: PipelineConfig
) -> pl.DataFrame:
    """Cut the corpus at the scoring horizon, and refuse a horizon the seal cannot support."""
    tolerance = _future_tolerance(cfg)
    observed = events["event_ts_utc"].max()
    if as_of is None:
        return events
    if observed is not None and as_of > observed + tolerance:
        raise FeatureTableError(
            f"as_of {as_of.isoformat()} is more than {tolerance.total_seconds() / 3600:g}h past "
            f"the latest event in the frame ({observed.isoformat()}). A scoring horizon beyond "
            "the data proves nothing was withheld from it; pass the fold's own cutoff or ingest "
            "further (03 B)."
        )
    return events.filter(pl.col("event_ts_utc") <= as_of)


def _future_tolerance(cfg: PipelineConfig) -> timedelta:
    """The configured future-timestamp tolerance, as a duration.

    Read from ``config/pipeline.yaml`` rather than restated: 00 G forbids a tunable in
    code, and the ingest guard and this one must not be able to disagree.
    """
    ingest = cfg.raw.get("ingest")
    if not isinstance(ingest, dict):
        raise BuildConfigError(
            "config/pipeline.yaml has no ingest block to read the tolerance from"
        )
    hours = ingest.get("future_timestamp_tolerance_hours")
    if not isinstance(hours, int) or isinstance(hours, bool) or hours < 0:
        raise BuildConfigError(
            f"ingest.future_timestamp_tolerance_hours must be a non-negative int, got {hours!r}"
        )
    return timedelta(hours=hours)


# --- the hash seam --------------------------------------------------------


def assert_feature_hash_matches(table: FeatureTable, training_hash: str) -> None:
    """Module-level form of :meth:`FeatureTable.assert_feature_hash_matches`.

    Both spellings exist because the scoring agent's call site reads better one way and
    the backtest harness's the other; the refusal is the same code, so the two cannot
    drift into different tolerances.
    """
    table.assert_feature_hash_matches(training_hash)


def _variant_registry(
    registry: FeatureRegistry,
    entries: Sequence[FeatureSpec] | None = None,
    *,
    code_version: str | None = None,
) -> FeatureRegistry:
    """A registry differing from ``registry`` in one declared respect, hash recomputed.

    The hash is re-derived through :func:`~oxbow.features.registry.hash_registry` rather
    than copied or invented, so a variant can be used to prove the mismatch guard bites: if
    the recomputation were skipped, the variant would carry its parent's digest and the
    refusal this exists to test would never fire.
    """
    moved = replace(
        registry,
        entries=tuple(entries) if entries is not None else registry.entries,
        code_version=code_version if code_version is not None else registry.code_version,
    )
    return replace(
        moved,
        spec_hash=hash_registry(
            moved.entries,
            spec_version=moved.spec_version,
            code_version=moved.code_version,
            semantics=moved.semantics,
        ),
    )


def registry_with_code_version(registry: FeatureRegistry, code_version: str) -> FeatureRegistry:
    """The same registry under a different ``code_version``, with its hash recomputed."""
    return _variant_registry(registry, code_version=code_version)


def registry_with_declared_window(
    registry: FeatureRegistry, feature_id: str, window: str
) -> FeatureRegistry:
    """The same registry with one declared lookback shortened, hash recomputed.

    The variant exists to be *scored against*: plan §8's hash covers the declared windows,
    and the only way to show that is to change a window and watch the digest move while the
    column names stay identical. A guard that only covered the id list would accept this
    table, and a 30-day aggregate would be read as a 7-day one.
    """
    entry = registry[feature_id]
    replaced = replace(entry, window=window)
    parse_window(replaced.window)
    return _variant_registry(
        registry, [replaced if item.id == feature_id else item for item in registry.entries]
    )


def registry_with_entry_order_reversed(registry: FeatureRegistry) -> FeatureRegistry:
    """The same entries in the opposite declared order, hash recomputed.

    Hash-only by design: the builder evaluates in declared order, and a real registry has
    entries that reference earlier ones, so this variant is a witness about the digest and
    deliberately not something to compute.
    """
    return _variant_registry(registry, list(reversed(registry.entries)))


def registry_with_extra_entry(registry: FeatureRegistry, extra: FeatureSpec) -> FeatureRegistry:
    """The live entries plus one more, hash recomputed.

    The sanctioned route for a deliberately leaking column: it goes through the same
    dispatch table, the same evaluation loop and the same publish boundary as an honest
    one, which is what makes the gate's failure evidence about the gate.
    """
    if extra.id in set(registry.ids):
        raise RegistryError(f"feature {extra.id!r} is already declared")
    return _variant_registry(registry, [*registry.entries, extra])


# --- money reconciliation -------------------------------------------------


@dataclass(frozen=True, slots=True)
class MoneyTotal:
    """One currency's worth of reported money, and the rendered rows behind it.

    A report that quotes ``moved_minor`` without tying it to the rows it rendered is the
    defect plan §8's ``test_totals_match_rendered_rows`` names: a dropped join or a
    double-counted side changes one number and not the other, and the discrepancy is
    invisible in the rendered table.
    """

    currency: str
    events: int
    rendered_rows: int
    moved_minor: int
    debit_minor: int
    credit_minor: int

    def reconciles(self) -> bool:
        """True when the reported total equals what the rendered rows add up to.

        Three separate identities, because each fails alone: every canonical event renders
        exactly two scored sides; the debit side alone sums to the money reported as moved;
        and the credit side does too. A dropped join breaks the first, a double-counted
        side breaks the second and third, and a one-sided expansion breaks only one of
        them — which is why all three are checked rather than a single ratio.
        """
        return (
            self.rendered_rows == 2 * self.events
            and self.debit_minor == self.moved_minor
            and self.credit_minor == self.moved_minor
        )

    def describe(self) -> str:
        return (
            f"{self.currency}: {self.events} events reporting {self.moved_minor} minor moved / "
            f"{self.rendered_rows} rendered rows, debits {self.debit_minor}, credits "
            f"{self.credit_minor}"
        )


def money_totals(events: pl.DataFrame) -> tuple[MoneyTotal, ...]:
    """Per-currency totals of the corpus and of the entity-expanded rows the build renders.

    The two sides are derived independently and on purpose: the reported total comes from
    the canonical events, the rendered total from the same
    :func:`~oxbow.features.build.entity_event_frame` the builder feeds its kernels. If the
    builder's expansion and the corpus ever disagree about how much money exists, this is
    where it shows — which is the only way the check means anything.
    """
    assert_input_contract(events)
    reported = events.group_by(CURRENCY).agg(
        pl.len().alias("events"),
        pl.col(AMOUNT_MINOR).sum().alias("moved_minor"),
    )
    expanded = entity_event_frame(events)
    signed = expanded.with_columns((pl.col(DIRECTION_SIGN) * pl.col(AMOUNT_MINOR)).alias("_signed"))
    rendered = signed.group_by(CURRENCY).agg(
        pl.len().alias("rendered_rows"),
        (-pl.col("_signed").filter(pl.col(DIRECTION_SIGN) < 0)).sum().alias("debit_minor"),
        pl.col("_signed").filter(pl.col(DIRECTION_SIGN) > 0).sum().alias("credit_minor"),
    )
    joined = reported.join(rendered, on=CURRENCY, how="inner", maintain_order="left").sort(CURRENCY)
    return tuple(
        MoneyTotal(
            currency=str(row[CURRENCY]),
            events=int(row["events"]),
            rendered_rows=int(row["rendered_rows"]),
            moved_minor=int(row["moved_minor"]),
            debit_minor=int(row["debit_minor"]),
            credit_minor=int(row["credit_minor"]),
        )
        for row in joined.iter_rows(named=True)
    )


def assert_totals_match_rendered_rows(events: pl.DataFrame, table: FeatureTable) -> None:
    """Fail when reported money and rendered rows disagree, naming the currency.

    Two independent comparisons, because they fail for different reasons. The per-currency
    identities are computed from the canonical events against the entity expansion, so a
    broken expansion is caught even if the published artifact looks self-consistent. The
    rendered-row count is checked against the *published* keys frame, so a join that
    duplicated or dropped a scored side is caught on the artifact actually written rather
    than on the frame that would have been. Checking only distinct transaction ids would let
    a duplicated side through, and a duplicated side is the likelier of the two mistakes.
    """
    totals = money_totals(events)
    if not totals:
        raise FeatureTableError(
            "an empty corpus renders no totals; refusing to report a zero that reads as a "
            "reconciled one (03 A rule 2)"
        )
    offenders = [total for total in totals if not total.reconciles()]
    if offenders:
        raise FeatureTableError(
            "the reported money does not match the rendered rows: "
            + "; ".join(total.describe() for total in offenders)
        )
    rendered_keys = table.keys["txn_id"].n_unique()
    corpus_keys = events["txn_id"].n_unique()
    if rendered_keys != corpus_keys:
        raise FeatureTableError(
            f"the published matrix renders {rendered_keys} of {corpus_keys} transactions; a "
            "silently dropped transaction changes every downstream total without changing a "
            "column name"
        )
    expected_rows = 2 * events.height
    if table.keys.height != expected_rows:
        raise FeatureTableError(
            f"the published keys frame renders {table.keys.height} rows for {events.height} "
            f"events, which must be exactly {expected_rows}: one scored side per participating "
            "account. A duplicated row here doubles the money in every downstream total while "
            "leaving the transaction count untouched."
        )


# --- currency guard -------------------------------------------------------


def assert_registry_groups_money_by_currency(registry: FeatureRegistry) -> tuple[str, ...]:
    """Every *additive* aggregate of `amount_minor` must partition by currency.

    Plan §8: "currency is part of every amount; aggregations group by currency or fail, no
    implicit FX." Additive aggregations are the ones this can fail on, because a sum, a
    mean and a variance actually combine the rows they read; a max or a median picks one
    row, which is unit-confused in a way the registry resolves by partitioning on
    ``direction`` instead (its loader refuses a filtered order statistic for the same
    reason, and the comment on the entry says so).

    Balance aggregates are the deliberate exception, and the reason is a property of the
    data rather than of the declaration: an account's own balance sequence cannot hold two
    currencies, because ``assert_single_currency_per_account`` — which
    :func:`build_feature_table` runs on every build, before a single window is evaluated —
    refuses the corpus that does. So a balance aggregate partitioned on ``entity`` alone
    is a single-currency sum by construction. That is why the check here is on the
    partition being *named* and *no wider* than the entity, and why it reports the ids it
    cleared rather than returning a boolean nobody can audit.
    """
    additive: frozenset[str] = frozenset({"sum", "mean_int", "std"})
    offenders: list[str] = []
    checked: list[str] = []
    for entry in registry.entries:
        if entry.kind not in {"window_agg", "cumulative", "forward_window"}:
            continue
        source = entry.source or ""
        is_money = source == AMOUNT_MINOR or source.endswith("_minor")
        if not is_money or entry.agg not in additive:
            continue
        checked.append(entry.id)
        if "currency" in entry.group_by:
            continue
        if source == AMOUNT_MINOR:
            offenders.append(entry.id)
        elif ENTITY_KEY not in entry.group_by:
            # A balance aggregate wider than the entity is no longer an entity's own
            # sequence, and the account-currency invariant stops covering it.
            offenders.append(entry.id)
    if offenders:
        raise CrossCurrencyAggregationError(
            f"{len(offenders)} additive money aggregate(s) blend currencies: "
            f"{sorted(offenders)[:5]}. Either add `currency` to group_by or narrow the "
            "partition to the entity whose currency the corpus pins; summing across "
            "currencies invents an exchange rate (plan §8)."
        )
    return tuple(checked)


# --- the dev slice --------------------------------------------------------


def induced_subcorpus(events: pl.DataFrame, *, target_rows: int) -> pl.DataFrame:
    """A deterministic, connected-ish slice: keep the busiest accounts' mutual traffic.

    ``config/pipeline.yaml`` fixes the sampling strategy at ``connected_subcorpus`` and
    says why — "random rows would not" preserve network structure. A random head of file
    would leave most accounts with one or two edges, which is exactly the condition under
    which a graph feature is null for a boring reason. So: rank accounts by participation
    count (ties broken by account key, both sorted, so the ranking is a pure function of
    the data), grow the account set until the edges *between* admitted accounts reach
    ``target_rows``, then cut at the total order ``(event_ts_utc, txn_id)``.

    No seed reaches the selection: it is deterministic by construction, and a seed nobody
    needed would only invite the question of what it perturbs.
    """
    if target_rows <= 0:
        raise FeatureTableError(f"target_rows must be positive, got {target_rows}")
    if target_rows > events.height:
        raise FeatureTableError(
            f"target_rows {target_rows} exceeds the corpus of {events.height} events; the "
            "slice would silently become the whole corpus"
        )
    stacked = events.select(pl.col(ENTITY_ACCOUNTS[0]).alias("account")).vstack(
        events.select(pl.col(ENTITY_ACCOUNTS[1]).alias("account"))
    )
    ranked = (
        stacked.group_by("account")
        .agg(pl.len().alias("degree"))
        .sort(["degree", "account"], descending=[True, False])["account"]
        .to_list()
    )
    if not ranked:
        raise FeatureTableError("the corpus has no accounts to induce a subgraph from")

    chosen = _grow_to_cover(events, ranked, target_rows)
    admitted = pl.DataFrame({"account": sorted(chosen)})
    from_side = events.join(admitted, left_on="account_from", right_on="account", how="semi")
    mutual = from_side.join(admitted, left_on="account_to", right_on="account", how="semi")
    if mutual.height < target_rows:
        raise FeatureTableError(
            f"only {mutual.height} mutually-admitted events exist across all {len(ranked)} "
            f"accounts, short of the {target_rows} requested. Widen the account budget "
            "deliberately rather than accepting a shorter slice as if it were the configured "
            "one."
        )
    return mutual.sort([EVENT_TS, TXN_ID]).head(target_rows)


def _grow_to_cover(events: pl.DataFrame, ranked: Sequence[str], target_rows: int) -> set[str]:
    """Admit accounts in degree order until the induced edge set covers the target.

    Doubling the candidate pool per round keeps this O(log n) join passes over the corpus;
    a per-account loop would be one join per account, which at this size is the difference
    between seconds and the rest of the afternoon.
    """
    total = len(ranked)
    take = max(2, min(total, total // 200))
    while take < total:
        candidate = set(ranked[:take])
        if _induced_height(events, candidate) >= target_rows:
            return candidate
        take = min(total, take * 2)
    return set(ranked)


def _induced_height(events: pl.DataFrame, accounts: set[str]) -> int:
    admitted = pl.DataFrame({"account": sorted(accounts)})
    from_side = events.join(admitted, left_on="account_from", right_on="account", how="semi")
    return from_side.join(admitted, left_on="account_to", right_on="account", how="semi").height


# --- graph-feature null reporting ----------------------------------------


def graph_null_rate(table: FeatureTable) -> tuple[float, tuple[str, ...]]:
    """Measured null share of the fold-scoped graph columns, and which columns it covers.

    DEV-011 measured PaySim as star-shaped, so a graph column being mostly null is a
    *finding about the corpus*. It is reported here as a number rather than left for a
    reader to discover, because the same 90 % null rate on a corpus with real structure
    would be a build defect and the two must be tellable apart from the artifact alone.
    """
    ids = [entry.id for entry in table.registry.entries if entry.kind == "graph_node"]
    present = [feature_id for feature_id in ids if feature_id in table.matrix.columns]
    if not present:
        return 0.0, ()
    rows = table.matrix.height
    if rows == 0:
        return 0.0, tuple(present)
    nulls = sum(int(table.matrix[feature_id].null_count()) for feature_id in present)
    return nulls / (rows * len(present)), tuple(present)


def assert_public_contract(registry: FeatureRegistry) -> None:
    """Cross-check the registry against the canonical contract before any work is done.

    The two vocabularies are declared in different files and read by different people; a
    feature naming a row field the contract does not emit is a build that fails at the
    first fold, several minutes in, with a message about a missing column.
    """
    columns = set(CANONICAL_COLUMNS)
    carry = {
        "row_unit",
        "counterparty",
        "direction",
        "event_date_local",
        "balance_before_minor",
        "balance_after_minor",
    }
    declared = set(registry.ids)
    for entry in registry.entries:
        for ref in (entry.source, entry.subject):
            # A source may name a canonical column, one of the keys the builder itself
            # materialises, or an *earlier declared feature*: `counterparties_first_seen_30d`
            # reads `is_counterparty_first_ever`, which is a column like any other by the
            # time the second kernel runs. registry.py validates the ordering; this checks
            # the vocabulary.
            if ref is None or ref in columns or ref in carry or ref in declared:
                continue
            raise RegistryError(
                f"feature {entry.id!r} reads {ref!r}, which is neither a canonical event "
                f"column, nor a builder-materialised key, nor a declared feature"
            )
    semantics = registry.semantics.sort_order
    missing = [name for name in semantics if name not in columns]
    if missing:
        raise RegistryError(
            f"window_semantics.sort_order names {missing}, which canonical event v1 does not "
            "emit; the declared total order would be unexecutable"
        )


def utc_bound(seconds: int) -> datetime:
    """A UTC instant from epoch seconds — the shape tests and folds both need."""
    return datetime.fromtimestamp(seconds, UTC)


__all__ = [
    "BuildConfigError",
    "CrossCurrencyAggregationError",
    "MoneyTotal",
    "assert_feature_hash_matches",
    "assert_public_contract",
    "assert_registry_groups_money_by_currency",
    "assert_totals_match_rendered_rows",
    "build_feature_table",
    "graph_null_rate",
    "induced_subcorpus",
    "money_totals",
    "registry_from_repo",
    "registry_with_code_version",
    "registry_with_declared_window",
    "registry_with_entry_order_reversed",
    "registry_with_extra_entry",
    "utc_bound",
]
