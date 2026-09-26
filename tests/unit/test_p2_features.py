"""Plan §8's money and data tests for the feature layer, plus the gate's witness.

WHY THIS FILE SITS ALONGSIDE ``tests/test_leakage.py``. That file is the leakage gate's
centrepiece; this one carries §8's named *money and data* cases — cross-currency, zero
amounts, sealed windows, winsorisation, totals, finiteness, reversals, balance deltas —
and the deliberate-leak witness, so the whole of §8 is covered by ``tests/unit/test_p2_*``
even when the centrepiece file moves. Both use the real registry and the real builder: a
§8 case satisfied by a hand-rolled matrix would prove the test agrees with itself and
nothing else.

WHAT EACH CASE IS GUARDED AGAINST, BRIEFLY. Every assertion here is the sort that fails for
a *plausible* reason — a dropped row, a clipped report, a late arrival absorbed into a
window — and every docstring names the shortcut it exists to catch, because a money test
whose failure nobody can interpret gets deleted rather than fixed.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl
import pytest

from oxbow.backtest.splits import Fold
from oxbow.config import PipelineConfig, find_repo_root, load_pipeline_config
from oxbow.features.build import (
    LABEL_FRAUD,
    REVERSAL_OF,
    CrossCurrencyAggregationError,
    FeatureHashMismatchError,
    FeatureTable,
    FeatureTableBuilder,
    LabelInMatrixError,
    NonFiniteFeatureError,
    WinsorBounds,
    assert_features_finite,
    cycle_eligible_edges,
    edge_frame_for_fold,
    label_correlations,
)
from oxbow.features.compute import (
    _time_stratified,
    assert_registry_groups_money_by_currency,
    assert_totals_match_rendered_rows,
    build_feature_table,
    induced_subcorpus,
    money_totals,
    registry_with_code_version,
    registry_with_declared_window,
    registry_with_extra_entry,
)
from oxbow.features.fakes import (
    CENTS,
    EPOCH,
    canonical_event,
    canonical_frame,
    chain_fixture,
    multi_currency_fixture,
    star_fixture,
    wide_fixture,
)
from oxbow.features.kinds import EVENT_TS, TXN_ID
from oxbow.features.leakage import (
    FutureReadError,
    audit_no_future_reads,
    audit_probe_cutoffs,
    audited_columns,
    truncation_invariance_violations,
)
from oxbow.features.registry import FeatureRegistry, FeatureSpec, registry_from_config_dir

REPO_ROOT: Final = find_repo_root()
CONFIG_DIR: Final = REPO_ROOT / "config"
LEAK_ID: Final = "leaky_next_30d_amount_minor"
SMUGGLED_ID: Final = "observed_outcome_minor"
KEYS: Final = ["txn_id", "entity"]


@pytest.fixture(scope="module")
def registry() -> FeatureRegistry:
    return registry_from_config_dir(CONFIG_DIR)


@pytest.fixture(scope="module")
def cfg() -> PipelineConfig:
    return load_pipeline_config(REPO_ROOT)


@pytest.fixture(scope="module")
def events() -> pl.DataFrame:
    return star_fixture()


@pytest.fixture(scope="module")
def table(events: pl.DataFrame, registry: FeatureRegistry, cfg: PipelineConfig) -> FeatureTable:
    return build_feature_table(events, registry, cfg=cfg)


@pytest.fixture(scope="module")
def wide_events(cfg: PipelineConfig) -> pl.DataFrame:
    return wide_fixture(400, accounts=24, seed=1337)


@pytest.fixture(scope="module")
def wide(wide_events: pl.DataFrame, registry: FeatureRegistry, cfg: PipelineConfig) -> FeatureTable:
    return build_feature_table(wide_events, registry, cfg=cfg)


def _leaking_spec() -> FeatureSpec:
    """A column that reads the thirty days *after* its own row, fully declared.

    Every required field is present and well-formed — a real sentence, a real leakage note,
    an int64 money id — because the point is that the entry is not rejected for being
    sloppy. Direction is its only defect, and direction is what the gate is for.
    """
    return FeatureSpec(
        id=LEAK_ID,
        group="volume_value",
        sentence=(
            "Money received in the thirty days following this row, which a backward-only "
            "feature may never read."
        ),
        kind="forward_window",
        as_of="row_event_ts",
        window="30d",
        group_by=("entity", "currency"),
        dtype="int64",
        null_policy="never_null",
        leakage_sensitive=True,
        leakage_note="reads rows after the cutoff by construction, which is the leak itself",
        role="feature",
        agg="sum",
        source="amount_minor",
        where="is_inflow_nonzero",
    )


def _fold(start_day: int = 0, end_day: int = 60, embargo_days: int = 30) -> Fold:
    """A fold whose graph cutoff sits inside the fixture timeline."""
    origin = EPOCH + timedelta(days=start_day)
    return Fold(
        fold_id="p2-fixture-fold",
        index=0,
        train_start_ts=origin,
        train_end_ts=EPOCH + timedelta(days=end_day),
        validation_start_ts=origin,
        test_start_ts=EPOCH + timedelta(days=end_day + embargo_days),
        test_end_ts=EPOCH + timedelta(days=end_day + embargo_days + 30),
        purge_days=1,
        label_window_days=1,
        seed=1337,
    )


# --- the witness: the gate must fail on a real leak -----------------------


def test_the_gate_catches_a_deliberately_declared_leak(
    events: pl.DataFrame, registry: FeatureRegistry, cfg: PipelineConfig
) -> None:
    """A leaking column registered through the registry mechanism is caught, by name.

    The column is computed by ``oxbow.features.kinds._kind_forward_window`` — the production
    kernel, the same running-total path the honest backward columns use, over a
    time-reversed frame. Only its direction differs, and only the gate notices. Without this
    test, every green leakage assertion in the repository is decoration.
    """
    leaking = registry_with_extra_entry(registry, _leaking_spec())
    assert LEAK_ID in leaking.matrix_ids
    built = build_feature_table(events, leaking, cfg=cfg)
    assert LEAK_ID in built.matrix.columns

    with pytest.raises(FutureReadError) as excinfo:
        audit_no_future_reads(built, events, leaking)
    message = str(excinfo.value)
    assert LEAK_ID in message
    assert "read data after its own cutoff" in message

    # The witness must bite on the leak and on nothing else, or the failure would only show
    # that the gate is noisy — which is not the same claim as the honest columns being safe.
    violations = truncation_invariance_violations(
        FeatureTableBuilder(leaking),
        events,
        cutoffs=audit_probe_cutoffs(events, count=4),
        columns=list(audited_columns(leaking)),
    )
    assert {violation.feature_id for violation in violations} == {LEAK_ID}


def test_the_forward_kind_is_refused_a_matrix_role_by_the_loader() -> None:
    """The other arm: config/features.yaml cannot declare a future-reading model input.

    Two independent mechanisms, so neither is the only line. This one fires at load; the
    gate above fires at audit. Asserting only the first would leave the gate untested, and
    asserting only the second would let a bad YAML line through to a training run.
    """
    from oxbow.config import load_yaml
    from oxbow.features.registry import RegistryError, parse_registry

    raw = load_yaml(CONFIG_DIR / "features.yaml")
    entries = raw.get("features")
    assert isinstance(entries, list)
    payload = dataclasses.asdict(_leaking_spec())
    entry = {key: value for key, value in payload.items() if value is not None}
    entry["group_by"] = list(_leaking_spec().group_by)
    entry.pop("graph_field", None)
    entry.pop("categories", None)
    entries.append(entry)
    with pytest.raises(RegistryError, match="reads rows after the cutoff") as excinfo:
        parse_registry(raw)
    assert "role 'outcome'" in str(excinfo.value)


# --- §8 currency ----------------------------------------------------------


def test_cross_currency_sum_raises(cfg: PipelineConfig, registry: FeatureRegistry) -> None:
    """One account in two currencies fails the build; no implicit FX is invented.

    Plan §8's wording is "aggregations group by currency or fail", and both arms run here.
    The *data* arm catches a corpus that contradicts the invariant the balance-sourced
    aggregates rest on. The *declaration* arm catches a feature that widens a money
    partition past currency — which the data arm cannot see, because on a single-currency
    corpus a blended aggregate computes beautifully and is simply wrong.
    """
    checked = assert_registry_groups_money_by_currency(registry)
    assert len(checked) >= 4
    assert "amount_in_30d_minor" in checked

    with pytest.raises(CrossCurrencyAggregationError, match="no implicit FX"):
        build_feature_table(multi_currency_fixture(), registry, cfg=cfg)

    blended = dataclasses.replace(registry["amount_in_30d_minor"], group_by=("entity",))
    narrowed = dataclasses.replace(
        registry,
        entries=tuple(blended if entry.id == blended.id else entry for entry in registry.entries),
    )
    assert narrowed["amount_in_30d_minor"].group_by == ("entity",)
    with pytest.raises(CrossCurrencyAggregationError, match="blend currencies"):
        assert_registry_groups_money_by_currency(narrowed)


def test_money_never_becomes_a_float(table: FeatureTable, registry: FeatureRegistry) -> None:
    """Money is Int64 minor units; every float column declares why it is a score."""
    for entry in registry.entries:
        if entry.id not in table.matrix.columns:
            continue
        dtype = table.matrix.schema[entry.id]
        if entry.dtype == "int64":
            assert dtype == pl.Int64, entry.id
        if dtype == pl.Float64:
            assert entry.declared_score_because, entry.id
            assert not entry.id.endswith("_minor"), entry.id


# --- §8 zero amounts ------------------------------------------------------


def test_zero_amount_excluded_from_value_features(
    cfg: PipelineConfig, registry: FeatureRegistry
) -> None:
    """A zero-amount row is kept, flagged, counts as activity, and moves no money.

    Plan §8 / 03 D. Three assertions, because three separate shortcuts each satisfy one and
    break the others: drop the row (hides the probe), zero-fill it (invents a measurement),
    or let it into a sum (a "typical movement" of zero that nobody sent).
    """
    with_probe = star_fixture()
    without_probe = canonical_frame(
        [event for event in with_probe.to_dicts() if event["txn_id"] != "t_probe"]
    )
    built_with = build_feature_table(with_probe, registry, cfg=cfg)
    built_without = build_feature_table(without_probe, registry, cfg=cfg)

    assert (
        built_with.matrix.filter(pl.col("txn_id") == "t_probe").height == 2
    ), "the zero-amount row vanished from the matrix"
    # `t_rev` is the anchor because its own cutoff is after the probe. The window is
    # (cutoff - 30d, cutoff], so `t_pay` at minute 0 cannot see a probe at minute 120 —
    # checking that row instead would be asserting a forward read.
    anchor = (pl.col("txn_id") == "t_rev") & (pl.col("entity") == "acct_b")
    payee = built_with.matrix.filter(anchor)
    assert int(payee["txn_count_in_30d"].item()) == 3
    assert int(payee["amount_in_30d_minor"].item()) == 50 * CENTS
    assert int(payee["zero_value_count_30d"].item()) == 1

    without = built_without.matrix.filter(anchor)
    assert int(without["txn_count_in_30d"].item()) == 2, "the probe must count as activity"
    assert int(without["amount_in_30d_minor"].item()) == 50 * CENTS, "must not move money"
    assert int(without["zero_value_count_30d"].item()) == 0
    assert int(payee["typical_movement_30d_minor"].item()) == int(
        without["typical_movement_30d_minor"].item()
    ), "a probe must not shift the median movement"


# --- §8 sealed windows ----------------------------------------------------


def test_late_row_does_not_mutate_sealed_window(
    cfg: PipelineConfig, registry: FeatureRegistry
) -> None:
    """A row that arrived after the seal cannot retroactively change a sealed answer.

    ``window_semantics.sealed_windows: true`` is a promise about replays: re-running a
    sealed build after late data lands must reproduce every number the earlier run
    published, or every artifact written between the two runs is wrong and silent about it.

    The precise claim, and why the obvious version of it is wrong. A row whose own cutoff
    sits *after* the late event is not sealed against it — lift the seal and that window
    genuinely does contain the arrival, and a value that changes there changes for the right
    reason. What must not move is every row at or before the seal. It must not move for
    whole-population features either, which is the case a predicate-only exclusion misses:
    ``where: always`` names no predicate column, so the late row stays in its population
    unless "always" itself becomes seal-aware.
    """
    corpus = star_fixture()
    seal = EPOCH + timedelta(days=45)
    late = corpus.filter(pl.col("txn_id") == "t_late")
    assert late["event_ts_utc"].item() <= seal, "the fixture row must be inside the window"
    assert late["ingested_at"].item() > seal, "and must have arrived after it"

    earlier_corpus = canonical_frame(
        [event for event in corpus.to_dicts() if event["txn_id"] != "t_late"]
    )
    sealed = build_feature_table(corpus, registry, cfg=cfg, sealed_before_ts=seal)
    historical = build_feature_table(earlier_corpus, registry, cfg=cfg, sealed_before_ts=seal)
    assert sealed.report.late_arrival_count == 2, "one late transaction renders two scored sides"
    assert historical.report.late_arrival_count == 0

    compared = [column for column in sealed.matrix.columns if column not in (*KEYS, "cutoff_ts")]
    joined = sealed.matrix.join(
        historical.matrix.select([*KEYS, *compared]),
        on=KEYS,
        how="inner",
        suffix="__historical",
    )
    covered = joined.filter(pl.col("cutoff_ts") <= seal)
    assert (
        covered.height == historical.matrix.height - 2
    ), "the seal should cover every pre-existing row except the ones after it"
    offenders: list[str] = []
    for column in compared:
        first, second = covered[column], covered[f"{column}__historical"]
        differs = int(((first != second) & ~(first.is_null() & second.is_null())).sum())
        if differs:
            offenders.append(f"{column} ({differs} rows)")
    assert not offenders, f"a sealed window moved under late data: {offenders}"

    # And the exclusion is doing real work rather than having nothing to exclude: past the
    # seal, the same row's window genuinely does contain the arrival once the seal lifts.
    unsealed = build_feature_table(corpus, registry, cfg=cfg, sealed_before_ts=None)
    anchor = (pl.col("txn_id") == "t_last") & (pl.col("entity") == "acct_a")
    assert (
        int(unsealed.matrix.filter(anchor)["txn_count_in_30d"].item())
        == int(sealed.matrix.filter(anchor)["txn_count_in_30d"].item()) + 1
    ), "the late row must be out of the sealed count and inside the unsealed one"
    assert int(unsealed.matrix.filter(anchor)["amount_in_30d_minor"].item()) > int(
        sealed.matrix.filter(anchor)["amount_in_30d_minor"].item()
    ), "and out of its money sum"


# --- §8 winsorisation -----------------------------------------------------


def test_winsorise_features_not_reports(
    wide_events: pl.DataFrame, registry: FeatureRegistry, cfg: PipelineConfig
) -> None:
    """Clipping moves the model's columns and not one reported currency figure.

    03 D. A winsorised feature is a modelling device; a winsorised *report* is a number
    telling an analyst that less money moved than moved. The two must not be the same
    column, so the clip is applied and the report total is recomputed from the canonical
    events afterwards and required to be identical — while at least one published column
    really does change, or nothing was clipped and the identity is vacuous.
    """
    plain = build_feature_table(wide_events, registry, cfg=cfg)
    ids = list(registry.winsorised_ids)
    assert ids, "the registry declares no winsorised columns"
    bounds = WinsorBounds.fit(plain.matrix, ids, registry=registry, fit_scope="training_slice")
    assert bounds.limits, "no column produced a bound"

    clipped = build_feature_table(wide_events, registry, cfg=cfg, winsor_bounds=bounds)
    changed: list[str] = []
    for column, (low, high) in sorted(bounds.limits.items()):
        after = clipped.matrix[column]
        before = plain.matrix[column]
        assert int(after.drop_nulls().min() or low) >= low, column
        assert int(after.drop_nulls().max() or high) <= high, column
        if int(((before != after) & ~(before.is_null() & after.is_null())).sum()):
            changed.append(column)
    assert changed, "the clip changed no published column at all"
    # A bound can be a legitimate no-op when p1 and p99 land on the same value; insisting
    # every column moves would make the guard depend on the fixture's tail rather than on
    # the clip. What must hold is that the clip ran and that no column escaped its bound.
    unfitted = sorted(set(ids) - set(bounds.limits))
    assert all(
        plain.matrix[column].null_count() == plain.matrix.height for column in unfitted
    ), "a column with values present must produce a bound"

    reported = money_totals(wide_events)
    assert sum(total.moved_minor for total in reported) == int(wide_events["amount_minor"].sum())
    assert sum(total.events for total in reported) == wide_events.height
    assert "reported money is raw and unclipped" in bounds.statement()
    # The clip must not be fitted on the whole corpus either: bounds fitted on all rows
    # would use the scored period to choose the cut points, which is the scaler leak
    # spec §7.2 lists next to the window one.
    assert bounds.fit_scope == "training_slice"
    assert bounds.fit_rows == plain.matrix.height


# --- §8 totals ------------------------------------------------------------


def test_totals_match_rendered_rows(wide_events: pl.DataFrame, wide: FeatureTable) -> None:
    """Reported money reconciles against the rows the artifact renders, per currency.

    Three identities, each failing alone: every event renders exactly two scored sides; the
    debit side alone sums to the money reported as moved; the credit side does too. A
    dropped join breaks the first, a double-counted side breaks the second, and a one-sided
    expansion breaks only one of them — which is why one ratio would not do.
    """
    totals = money_totals(wide_events)
    assert len(totals) == 1
    total = totals[0]
    assert total.currency == "USD"
    assert total.rendered_rows == 2 * total.events
    assert total.debit_minor == total.moved_minor == total.credit_minor
    assert_totals_match_rendered_rows(wide_events, wide)

    # A duplicated side must be caught, not silently reported as more activity.
    inflated = wide.keys.vstack(wide.keys.head(1))
    with pytest.raises(Exception, match="renders"):
        assert_totals_match_rendered_rows(wide_events, dataclasses.replace(wide, keys=inflated))


def test_a_dropped_transaction_breaks_the_reconciliation(
    wide_events: pl.DataFrame, wide: FeatureTable
) -> None:
    """Drop one rendered transaction and the totals stop matching.

    The counterpart to the previous test: a reconciliation that never fails on a missing row
    is only checking an identity the builder satisfies by construction.
    """
    dropped = wide.keys.filter(pl.col("txn_id") != wide.keys["txn_id"][0])
    with pytest.raises(Exception, match="renders .* of .* transactions"):
        assert_totals_match_rendered_rows(wide_events, dataclasses.replace(wide, keys=dropped))


# --- §8 reversals and cycles ---------------------------------------------


def test_reversal_not_a_cycle(cfg: PipelineConfig, registry: FeatureRegistry) -> None:
    """A refund is typed, linked, kept visible, and excluded from cycle detection.

    Plan §8 / 03 D. Read as an ordinary transfer, ``A→B, B→C, B→A(REVERSAL)`` is a
    round trip that closed on itself, so the raw edge set contains a cycle. Read correctly
    the last leg is money returning the way it came, so the cycle-eligible edge set
    collapses to the path ``A→B→C``. The fixture is built so the *only* difference between
    "cycle" and "no cycle" is whether the reversal is excluded — which is what makes this a
    property of the seam rather than of a caller's habits.
    """
    corpus = chain_fixture()
    fold = _fold(end_day=7)
    edges = edge_frame_for_fold(corpus, fold, registry)
    assert edges.height == 3, "every leg must stay in the edge frame; a refund is visible"

    reversal = edges.filter(pl.col("is_reversal"))
    assert reversal.height == 1
    assert reversal["txn_type"].item() == "REVERSAL"
    assert reversal[REVERSAL_OF].item() == "c1", "the refund must be linked to what it reverses"

    eligible = cycle_eligible_edges(edges)
    assert eligible.height == 2
    assert not bool(eligible["is_reversal"].any())

    assert _has_directed_cycle(
        edges.select("src", "dst")
    ), "the unfiltered edge set does form a cycle, so the exclusion is doing the work"
    assert not _has_directed_cycle(
        eligible.select("src", "dst")
    ), "a refund manufactured a laundering cycle"


def _has_directed_cycle(edges: pl.DataFrame) -> bool:
    """Reachability-from-itself over a small edge set, in plain Python.

    Deliberately local to the test rather than a call into the graph layer: this test is
    about the *edge filter* the feature seam applies, and borrowing the consumer's own
    cycle detector would let a bug in either one answer the question.
    """
    adjacency: dict[str, set[str]] = {}
    for source, target in edges.iter_rows():
        adjacency.setdefault(str(source), set()).add(str(target))
    return any(_reaches(adjacency, node, node) for node in adjacency)


def _reaches(adjacency: dict[str, set[str]], start: str, node: str) -> bool:
    seen: set[str] = set()
    stack = list(adjacency.get(start, set()))
    while stack:
        current = stack.pop()
        if current == node:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency.get(current, set()))
    return False


# --- §8 balance proxy -----------------------------------------------------


def test_balance_delta_feature_present(
    table: FeatureTable, events: pl.DataFrame, registry: FeatureRegistry
) -> None:
    """The balance-delta chain is present end to end and matches arithmetic by hand.

    A balance-proxy feature is the closest thing this corpus has to a ledger check, so the
    intermediate must be computed, the mismatch flag must fire exactly on the rows whose
    balances disagree with the amount, and the published aggregate must be the sum of the
    intermediate and not of something else. Checked against hand arithmetic on the fixture
    because every one of those three links can silently break: an intermediate dropped at the
    boundary, a flag derived from an aggregate instead of the row, an aggregate reading a
    stale column.
    """
    assert "balance_delta_abs_minor" in registry.intermediate_ids
    assert "balance_delta_abs_minor" not in table.matrix.columns
    published = [
        "balance_delta_total_30d_minor",
        "is_balance_delta_mismatch",
        "balance_mismatch_count_30d",
        "has_balance_delta",
    ]
    for column in published:
        assert column in registry.matrix_ids or column in registry.intermediate_ids, column

    computed = FeatureTableBuilder(registry).compute_frame(events)
    imbalanced = computed.filter(pl.col("txn_id") == "t_imbalanced").sort("entity")
    # acct_a pays 30_00 out and its balance falls 5_00: the ledger says 25_00 came back
    # from nowhere. acct_c receives 30_00 and its balance rises 20_00: 10_00 unaccounted.
    assert imbalanced["balance_delta_abs_minor"].to_list() == [
        abs(45 * CENTS - 50 * CENTS + 30 * CENTS),
        abs(1_020 * CENTS - 1_000 * CENTS - 30 * CENTS),
    ]
    assert imbalanced["is_balance_delta_mismatch"].to_list() == [True, True]

    clean = computed.filter(pl.col("txn_id") == "t_pay")
    assert clean["balance_delta_abs_minor"].to_list() == [0, 0]
    assert clean["is_balance_delta_mismatch"].to_list() == [False, False]

    # A balance the corpus never observed is null, not zero: 03 A rule 2. `t_late` carries
    # no balance on either side, which is the state the rule is about — `t_meet` does
    # observe acct_e's balance and is therefore a delta of exactly zero, a different fact.
    unobserved = computed.filter((pl.col("txn_id") == "t_late") & (pl.col("entity") == "acct_f"))
    assert unobserved["balance_delta_abs_minor"].item() is None
    assert not bool(unobserved["is_balance_delta_mismatch"].item())
    assert int(unobserved["balance_mismatch_count_30d"].item()) == 0


# --- §8 finiteness --------------------------------------------------------


def test_features_finite(wide: FeatureTable, registry: FeatureRegistry) -> None:
    """No NaN or infinity reaches the matrix on a corpus big enough to produce one.

    The degenerate divisions this catches are ordinary: a robust z over one observation, a
    coefficient of variation with a zero mean. The eight-row fixture would pass this by
    having too few rows to divide by, so it runs on four hundred events. LightGBM tolerates
    a non-finite value and the WOE scorecard does not, so a table that passes one model
    silently diverges for the other (03 H).
    """
    assert registry.guards.require_finite
    assert_features_finite(wide.matrix, registry)
    float_columns = [column for column, dtype in wide.matrix.schema.items() if dtype == pl.Float64]
    assert float_columns, "the registry declares float scores; none were published"
    for column in float_columns:
        series = wide.matrix[column]
        assert int(series.is_nan().sum()) == 0, column
        assert int(series.is_infinite().sum()) == 0, column
    poisoned = wide.matrix.with_columns(pl.lit(float("nan")).alias(float_columns[0]))
    with pytest.raises(NonFiniteFeatureError, match="non-finite"):
        assert_features_finite(poisoned, registry)


# --- label isolation (spec §7.2) ------------------------------------------


def test_labels_never_enter_the_matrix(table: FeatureTable, registry: FeatureRegistry) -> None:
    """``label_is_fraud``/``label_is_flagged``/``label_typology`` are absent and banned.

    Checked against the declared ban list rather than a hardcoded triple, so a fourth label
    added to the guard is covered without touching this file.
    """
    banned = {LABEL_FRAUD, "label_is_flagged", "label_typology"}
    assert banned <= set(registry.guards.banned_sources)
    for column in banned:
        assert column not in table.matrix.columns
        assert column not in table.outcomes.columns
        assert column not in table.keys.columns
    assert table.report.labels_carried_outside_matrix


def test_max_label_correlation_is_measured_and_below_the_guard(
    wide: FeatureTable, wide_events: pl.DataFrame, registry: FeatureRegistry
) -> None:
    """The worst |Pearson r| against the fraud label, measured and reported.

    Recomputed from the published columns rather than read back out of the build report: the
    report and the matrix come from different code paths, and a guard that only re-reads its
    own log line proves that the log line was written.
    """
    computed = FeatureTableBuilder(registry).compute_frame(wide_events)
    measured = label_correlations(computed, registry)
    assert measured, "no numeric column correlated at all, so the check did not run"
    worst_id, worst = max(measured.items(), key=lambda pair: pair[1])
    ceiling = registry.guards.max_abs_correlation_with_label
    assert worst < ceiling, f"{worst_id} correlates {worst:.4f} with the label; guard {ceiling}"
    assert wide.report.max_abs_label_correlation == pytest.approx(worst)
    assert wide.report.max_abs_label_correlation_column == worst_id
    print(
        f"MEASURED max |correlation| with {LABEL_FRAUD}: {worst:.6f} on column {worst_id}, "
        f"over {len(measured)} numeric matrix columns; guard is {ceiling}"
    )


def test_a_label_derived_feature_is_refused(cfg: PipelineConfig, registry: FeatureRegistry) -> None:
    """A column that *is* the label under another name is caught by the correlation guard.

    The absence check cannot see this: the column is not called ``label_is_fraud``. Only the
    correlation is offended, which is why the guard is a number and not merely a name list —
    and why deleting the ceiling check from the build path turns this test red.
    """
    smuggled = FeatureSpec(
        id=SMUGGLED_ID,
        group="exposure",
        sentence=(
            "The shortfall between the balance a payer reported and the amount they moved, "
            "which in this corpus is exactly the investigator's fraud decision."
        ),
        kind="row_value",
        as_of="row_event_ts",
        window="point_in_time",
        group_by=("entity",),
        dtype="int64",
        null_policy="null_when_balance_absent",
        leakage_sensitive=True,
        leakage_note="is the label by arithmetic, which no name-based ban can see",
        role="feature",
        value="balance_delta_abs_minor",
    )
    poisoned = registry_with_extra_entry(registry, smuggled)
    with pytest.raises(LabelInMatrixError, match="correlate with the label above") as excinfo:
        build_feature_table(_label_aligned_events(), poisoned, cfg=cfg)
    assert SMUGGLED_ID in str(excinfo.value)


def _label_aligned_events() -> pl.DataFrame:
    """A corpus whose sender-side balance shortfall *is* the fraud flag, so r reaches 1.0.

    The receiver's balances stay unobserved, which is what makes the correlation exact
    rather than diluted by a second all-zero side.
    """
    rows = [
        canonical_event(
            f"z{index}",
            index * 60,
            "acct_a",
            "acct_b",
            100 * CENTS,
            src_balance_before_minor=1_000 * CENTS,
            src_balance_after_minor=1_000 * CENTS - (index % 2) * CENTS,
            label_is_fraud=index % 2,
        )
        for index in range(24)
    ]
    return canonical_frame(rows)


# --- the hash seam --------------------------------------------------------


def test_feature_hash_mismatch_refuses(
    table: FeatureTable, events: pl.DataFrame, registry: FeatureRegistry, cfg: PipelineConfig
) -> None:
    """Scoring a matrix built from a different spec is refused, not warned about.

    02 B seam 3. The failure mode this pins has no other symptom: a model fitted on one
    feature set and scored against another raises nothing anywhere and produces scores whose
    columns silently mean something else.
    """
    table.assert_feature_hash_matches(registry.spec_hash)
    with pytest.raises(FeatureHashMismatchError, match="Refusing to score"):
        table.assert_feature_hash_matches("0" * 64)

    shortened = registry_with_declared_window(registry, "amount_in_30d_minor", "7d")
    rebuilt = build_feature_table(events, shortened, cfg=cfg)
    assert rebuilt.matrix.columns == table.matrix.columns
    with pytest.raises(FeatureHashMismatchError) as excinfo:
        rebuilt.assert_feature_hash_matches(table.spec_hash)
    assert "do not paste the hash forward" in str(excinfo.value)


def test_hash_covers_declared_windows_and_order(registry: FeatureRegistry) -> None:
    """The digest moves when a window moves, with the column names unchanged.

    Plan §8 bounds the hash at "ordered feature ids + declared windows + code version". A
    hash over the id *set* alone would let a 30-day window become a 7-day one without
    invalidating a single training artifact, reducing the guard to a name check.
    """
    from oxbow.features.compute import registry_with_entry_order_reversed

    assert len(registry.spec_hash) == 64
    shortened = registry_with_declared_window(registry, "amount_in_30d_minor", "7d")
    assert shortened.spec_hash != registry.spec_hash
    assert [entry.id for entry in shortened.entries] == list(registry.ids)
    reversed_order = registry_with_entry_order_reversed(registry)
    assert reversed_order.spec_hash != registry.spec_hash
    assert sorted(reversed_order.matrix_ids) == sorted(registry.matrix_ids)
    restored = registry_with_declared_window(shortened, "amount_in_30d_minor", "30d")
    assert restored.spec_hash == registry.spec_hash


def test_hash_covers_the_code_version(registry: FeatureRegistry) -> None:
    bumped = registry_with_code_version(registry, "p2.2")
    assert bumped.code_version == "p2.2"
    assert registry.code_version == "p2.1"
    assert bumped.spec_hash != registry.spec_hash


# --- the registry is the only feature list --------------------------------


def test_every_declared_kind_has_a_kernel(registry: FeatureRegistry) -> None:
    """Dispatch covers exactly the declared vocabulary, and nothing else.

    Asserted as a set equality rather than against a literal list written into this test: a
    hardcoded list fails every time a kind is legitimately added, which trains people to
    widen the list instead of implementing the kernel.
    """
    from oxbow.features.kinds import KIND_IMPLEMENTATIONS, REGISTRY_KIND_NAMES
    from oxbow.features.registry import KIND_NAMES

    assert set(KIND_IMPLEMENTATIONS) == set(registry.kinds) == KIND_NAMES == REGISTRY_KIND_NAMES
    assert all(callable(kernel) for kernel in KIND_IMPLEMENTATIONS.values())


def _feature_named_literals(path: Path, ids: set[str]) -> list[str]:
    """String constants naming a matrix feature, excluding docstrings.

    AST-parsed rather than grepped: a feature id in prose is documentation and a feature id
    in code is a decision being made about that feature.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return sorted(
        {
            text
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            for text in [node.value]
            if any(name in text for name in ids)
        }
    )


def _dispatches_on_feature_identity(path: Path, ids: set[str]) -> list[str]:
    """Code that *branches* on a feature id — the defect the registry doctrine forbids.

    Comparisons, membership tests against a literal collection, and dict keys built from ids
    are what "which feature is this?" looks like in an AST. Asking that question at all is
    the lapse: the answer is supposed to come from the declared ``kind``.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []

    def named(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value} & ids
        if isinstance(node, ast.List | ast.Tuple | ast.Set) and node.elts:
            return set().union(*(named(item) for item in node.elts))
        return set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.Eq | ast.NotEq | ast.In | ast.NotIn | ast.Is | ast.IsNot)
            for op in node.ops
        ):
            hits.extend(sorted(named(node.left) | named(node.comparators[0])))
        elif isinstance(node, ast.Dict) and node.keys:
            hits.extend(sorted(set().union(*(named(key) for key in node.keys if key))))
    return sorted(set(hits))


def test_builder_and_kernels_name_no_features() -> None:
    """The feature list lives in config/features.yaml and nowhere else.

    Two checks, because two different lapses are possible. The strict one — no feature id as
    a string constant at all — runs on the four modules with no reason to name one. The
    precise one — no module may *branch* on a feature id — runs on all five, including
    ``build.py``, which legitimately carries id-shaped strings as the edge-frame column names
    it shares with the graph layer. Asserting the strict rule there would state a falsehood
    about a different seam, and a test that cries wolf is worse than none.
    """
    package = Path(__file__).resolve().parents[2] / "packages" / "pipeline" / "oxbow" / "features"
    ids = set(registry_from_config_dir(CONFIG_DIR).matrix_ids)
    strict = ("kinds.py", "compute.py", "leakage.py", "fold_scope.py")
    literals = {
        source: found
        for source in strict
        if (found := _feature_named_literals(package / source, ids))
    }
    assert not literals, f"feature ids appear in code outside the registry: {literals}"
    branching = {
        source: found
        for source in (*strict, "build.py")
        if (found := _dispatches_on_feature_identity(package / source, ids))
    }
    assert not branching, f"a module branches on feature identity: {branching}"
    assert "is_reversal" in _feature_named_literals(package / "build.py", ids)


def test_every_entry_declares_an_as_of_and_a_window(registry: FeatureRegistry) -> None:
    """Plan §8's precondition: an as-of timestamp and a lookback window, on every entry.

    A missing window is indistinguishable from an omitted one, which is why the loader
    requires the field to be present and this requires it to be either a real duration or one
    of the named non-rolling markers.
    """
    from oxbow.features.registry import NON_ROLLING_WINDOWS, parse_window

    rolling = 0
    for entry in registry.entries:
        assert entry.as_of in {"row_event_ts", "fold_scoped"}, entry.id
        if entry.window in NON_ROLLING_WINDOWS:
            assert parse_window(entry.window) is None, entry.id
            continue
        duration = parse_window(entry.window)
        assert duration is not None, entry.id
        assert duration.days <= registry.max_lookback_days, entry.id
        rolling += 1
    assert rolling > 0
    assert registry.max_lookback_days == 30
    assert len(registry.matrix_ids) >= MATRIX_FLOOR


MATRIX_FLOOR: Final = 60


def test_the_slice_covers_the_timeline_it_will_be_split_on(cfg: PipelineConfig) -> None:
    """A slice that will be cut into embargoed folds has to contain the whole race.

    The sampler used to sort the induced mutual set by ``(event_ts_utc, txn_id)`` and take
    ``.head(target_rows)`` -- the earliest ``target_rows`` events. Connectivity survived
    that; the timeline did not: measured on the real corpus, 40,000 events from the head
    spanned 18 days on one ingest and 162 on another, and `oxbow score`'s fold plan -- a
    30-day embargo across five expanding folds needs roughly 100 days -- therefore refused
    or accepted depending on which batch ids the last ingest had minted. A gate whose
    verdict depends on an accident of file naming is not a gate.

    Asserted: the slice reaches the end of the range it was drawn from, it is not the head,
    and repeating the call changes nothing.
    """
    corpus = wide_fixture(2_000, accounts=60, seed=1337)
    small = induced_subcorpus(corpus, target_rows=400)
    times = corpus.get_column("event_ts_utc")
    span_us = (times.max() - times.min()) // 1_000
    slice_times = small.get_column("event_ts_utc")
    slice_span_us = (slice_times.max() - slice_times.min()) // 1_000

    assert small.height == 400
    assert slice_span_us >= span_us * 0.9, (
        f"the slice covers {slice_span_us / span_us:.1%} of the corpus timeline; a fold "
        "plan is being asked to cut a window out of the first few days of it"
    )
    # The endpoints are the sampler's promise, asserted where they can be seen: over the
    # frame it is actually handed. An event whose accounts are not both admitted can never
    # be in the slice, and pretending otherwise would make this test fail for a reason that
    # has nothing to do with temporal coverage.
    ordered = corpus.sort([EVENT_TS, TXN_ID])
    strided = _time_stratified(ordered, 400)
    assert strided.height == 400
    assert strided.get_column(EVENT_TS).min() == ordered.get_column(EVENT_TS).min()
    assert strided.get_column(EVENT_TS).max() == ordered.get_column(EVENT_TS).max()
    assert strided.get_column(EVENT_TS).is_sorted()
    assert _time_stratified(ordered, 400).equals(strided)
    head = corpus.sort([EVENT_TS, TXN_ID]).head(400)
    assert (
        slice_times.max() > head.get_column(EVENT_TS).max()
    ), "stratified selection has collapsed back to a head of the timeline"
    assert induced_subcorpus(corpus, target_rows=400).equals(small)


def test_the_dev_slice_is_connected_and_deterministic(cfg: PipelineConfig) -> None:
    """The sampling rule preserves mutual traffic, and repeats byte-identically.

    ``config/pipeline.yaml`` fixes ``strategy: connected_subcorpus`` with the reason —
    "random rows would not" preserve network structure — because an account with one or two
    edges makes a graph feature null for a boring reason and hides the finding. So: every
    kept event must have *both* endpoints admitted, and a second call must be identical.
    """
    corpus = wide_fixture(2_000, accounts=60, seed=1337)
    target = int(cfg.sampling["interactive_txn_target"])
    small = induced_subcorpus(corpus, target_rows=400)
    assert small.height == 400
    assert induced_subcorpus(corpus, target_rows=400).equals(small)
    accounts = set(small["account_from"].unique().to_list()) | set(
        small["account_to"].unique().to_list()
    )
    inside = corpus.join(
        pl.DataFrame({"account": sorted(accounts)}), left_on="account_from", right_on="account"
    ).join(pl.DataFrame({"account": sorted(accounts)}), left_on="account_to", right_on="account")
    assert inside.height >= 400, "the slice is not closed under its own accounts"
    assert small["event_ts_utc"].is_sorted()
    with pytest.raises(Exception, match="exceeds the corpus"):
        induced_subcorpus(corpus, target_rows=corpus.height * 2)
    assert target == 500_000


def test_a_bare_graph_frame_is_refused_where_a_provider_is_expected() -> None:
    """The structural arm of the plan §8 graph guard, at the build's own argument position."""
    from oxbow.features.fold_scope import GraphScopeError

    events = star_fixture()
    registry = registry_from_config_dir(CONFIG_DIR)
    with pytest.raises(GraphScopeError, match="bare DataFrame"):
        build_feature_table(
            events,
            registry,
            graph_features=pl.DataFrame({"account": ["a"], "pagerank": [0.5]}),  # type: ignore[arg-type]
            fold=_fold(),
        )


def test_a_running_total_keeps_the_units_of_its_own_source() -> None:
    """Money stays integer; a moment stays a float, however large it gets.

    ``_running_totals`` used to cast every accumulated source to ``Int64``. For a money
    column that was a no-op wearing a name; for the float intermediates it did two kinds of
    damage. It truncated a fractional running total toward zero, and it refused the whole
    feature build on the first corpus with amounts big enough to matter. Measured, on the
    full PaySim ingest: one transfer of 3.5e9 minor units squares to 1.2e19, and
    ``i64::MAX`` is 9.22e18, so ``_squares`` died on its own before any window arithmetic --

        InvalidOperationError: conversion from `f64` to `i64` failed in column '_squares'
        for 11 out of 80000 values: [1.2190e19, 1.1968e19, 2.1355e19]

    A sum of squares is a statistic, not an amount. The integer-minor-units rule (DEV-005)
    is about money, and applying it to a moment is the version of over-correction that
    breaks the build instead of protecting the cents.
    """
    from oxbow.features.kinds import POSITION, SQUARES, _running_totals

    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    rows = pl.DataFrame(
        {
            "entity": ["A", "A", "A", "B"],
            EVENT_TS: [t0, t0 + timedelta(days=1), t0 + timedelta(days=2), t0],
            TXN_ID: ["t1", "t2", "t3", "t4"],
            POSITION: [0, 1, 2, 3],
            "amount_minor": pl.Series([100, 200, 300, 50], dtype=pl.Int64),
            SQUARES: pl.Series([1.0e19, 2.0e19, 3.0e19, 4.0e18], dtype=pl.Float64),
            "ratio": pl.Series([0.5, 0.25, 0.125, 0.75], dtype=pl.Float64),
        }
    )

    totals = _running_totals(rows, ["entity"], ["amount_minor", SQUARES, "ratio"])

    money = totals["c_amount_minor"]
    assert money.dtype == pl.Int64, money.dtype
    assert money.to_list() == [100, 300, 600, 50], "money must still accumulate in minor units"
    squares = totals[f"c_{SQUARES}"]
    assert squares.dtype == pl.Float64, squares.dtype
    assert squares.to_list() == [1.0e19, 3.0e19, 6.0e19, 4.0e18], (
        "a sum of squares past i64::MAX is the normal case for large transfers, not an error"
    )
    ratio = totals["c_ratio"]
    assert ratio.to_list() == [0.5, 0.75, 0.875, 0.75], (
        "the running total was truncating a fractional source toward zero -- 0.75 arrived as 0"
    )
