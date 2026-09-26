"""The grain bridge and the two fold-scoped providers: the seams the score stage was missing.

WHAT IS UNDER TEST, AND WHY IT WAS NOT TESTABLE BEFORE. ``oxbow.features.build.FeatureTable``
publishes one row per ``(txn_id, entity)``; ``oxbow.scoring.frame.build_training_frame``
consumes one row per ``(account_key, as_of_ts, fold)``. Nothing bridged them, so the
scorecard had no input, and the two fold-scoped providers that the registry's graph and rule
columns are declared against had no implementation, so all thirty-two of those columns were
null by construction. ``tests/test_leakage.py`` covers the *guard* -- a table fed future edges
cannot be sealed -- on a hand-written frame. This file covers the two things that had to
exist for that guard to be exercised on a real build: providers that compute a fold's own
graph and its own rule hits, and a bridge that turns a fold's matrix into account-grain rows.

THE CLAIMS, EACH WITH ITS FAILURE MODE.

1. **Providers compute per fold.** A sealed node table carries every declared field, some
   carry values on a corpus with real topology, and a table sealed for one fold is
   unreachable from another. Separately: two folds of one corpus must *disagree* about a
   node's structure, because the later fold has more history. A provider that graphed the
   corpus once and handed the same table to every fold would pass every other test here and
   still leak; that is the case claim 1's difference test covers.
2. **The bridge keeps the leakage doctrine at the new grain.** An account row is carried from
   that account's own most recent event at or before its as-of instant, so every value on it
   was computable then. Tested the way the rest of the feature layer tests it: truncate the
   corpus, rebuild, and every surviving row must be identical.
3. **The frame crosses the scoring contract unchanged.** ``build_training_frame`` accepts it,
   its digest is the registry's own (02 §B seam 3, now that both layers compute one hash),
   the labels arrive as contract columns and never as features, and every money column is
   still Int64 minor units.
4. **It is deterministic.** Two builds -- one over the corpus in reverse input order -- produce
   one content digest.

Money: no float amount appears in this file; the only quantity computed over a set of rows is
a 0/1 label.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl
import pytest

from oxbow.backtest.splits import Fold, SplitPlan, build_walk_forward
from oxbow.config import find_repo_root, load_pipeline_config
from oxbow.features.bridge import (
    ACCOUNT_KEY,
    AS_OF_TS,
    FOLD,
    AccountGrainFrame,
    GrainBridgeError,
    LabelUnavailableError,
    anchor_relative_columns,
    build_account_frame,
    fold_membership,
)
from oxbow.features.build import LABEL_FRAUD, LABEL_TYPOLOGY, entity_event_frame
from oxbow.features.compute import registry_from_repo
from oxbow.features.fakes import canonical_event, canonical_frame
from oxbow.features.fold_providers import (
    FoldGraphSource,
    FoldRuleHitProvider,
    fold_providers,
    provider_report,
    to_micros,
)
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
from oxbow.features.kinds import ENTITY, EVENT_TS, TXN_ID
from oxbow.features.registry import FeatureRegistry
from oxbow.graph import load_graph_settings
from oxbow.scoring.config import load_feature_registry, load_scorecard_config
from oxbow.scoring.frame import PROVENANCE_REAL, build_training_frame, frame_content_hash

REPO_ROOT: Path = find_repo_root()
EPOCH: Final = datetime(2023, 1, 1, tzinfo=UTC)
DAY: Final = timedelta(days=1)
SPAN_DAYS: Final = 420
ACCOUNTS: Final = 160
EVENTS: Final = 1400
SEED: Final = 1337
GRAPH_FIELDS: Final = 16
N_FOLDS: Final = 5


def _fixture(events: int = EVENTS, accounts: int = ACCOUNTS, seed: int = SEED) -> pl.DataFrame:
    """A sparse, star-leaning network over fourteen months, labelled on ~2 % of rows.

    Sparse on purpose: the rules layer refuses a run in which one rule flags more than a third
    of the accounts, and a dense random graph does exactly that (R4 fires on everybody). A
    corpus shaped like the ones the pipeline reads -- most accounts with a handful of
    counterparties, a few busy ones, zero-amount balance probes, a reversal type -- is what the
    providers and the bridge have to be able to work on.
    """
    modulus = 2**31 - 1
    state = seed % modulus
    keys = [f"acct_{index:04d}" for index in range(accounts)]
    rows: list[dict[str, object]] = []
    for index in range(events):
        state = (state * 48_271) % modulus
        second = (state * 48_271) % modulus
        third = (second * 48_271) % modulus
        # A popularity skew, so the graph has rails, members and externals rather than one
        # uniform blob: the square-root bucket of a uniform draw, times the account count.
        sender = keys[min(int((state % 100) ** 0.5 * accounts / 10), accounts - 1)]
        receiver = keys[(second % (accounts - 1) + 1 + state % accounts) % accounts]
        if sender == receiver:
            receiver = keys[(index + 1) % accounts]
        minute = (third % (SPAN_DAYS * 24 * 60)) + index
        amount = 0 if index % 23 == 5 else 5_000 + (state % 900) * 100
        rows.append(
            canonical_event(
                f"g{index:05d}",
                minute,
                sender,
                receiver,
                amount,
                txn_type="REVERSAL" if index % 97 == 11 else "PAYMENT",
                label_is_fraud=1 if index % 53 == 7 else 0,
                label_typology="pass_through" if index % 53 == 7 else None,
                src_balance_before_minor=500_000,
                dst_balance_before_minor=120_000,
                source_dataset="p2_grain_bridge",
            )
        )
    return canonical_frame(rows)


def _plan(events: pl.DataFrame, registry: FeatureRegistry) -> SplitPlan:
    """The real purged ladder over a corpus's own timeline.

    ``entity_event_frame`` supplies the ``entity`` column the entity-disjoint sizing needs, so
    the plan and the bridge's account grain describe the same population.
    """
    timeline = entity_event_frame(events).select([EVENT_TS, ENTITY]).sort([EVENT_TS, ENTITY])
    return build_walk_forward(timeline, registry=registry, config_dir=REPO_ROOT / "config")


@pytest.fixture(scope="module")
def events() -> pl.DataFrame:
    return _fixture()


@pytest.fixture(scope="module")
def registry() -> FeatureRegistry:
    return registry_from_repo(REPO_ROOT)


@pytest.fixture(scope="module")
def plan(events: pl.DataFrame, registry: FeatureRegistry) -> SplitPlan:
    return _plan(events, registry)


@pytest.fixture(scope="module")
def bridged(
    events: pl.DataFrame, registry: FeatureRegistry, plan: SplitPlan
) -> AccountGrainFrame:
    """The frame the score stage would hand the scoring layer: built once for the module."""
    cfg = load_pipeline_config(REPO_ROOT)
    graph, rules = fold_providers(events, registry, cfg)
    return build_account_frame(events, registry, plan, graph=graph, rules=rules, cfg=cfg)


# --- the providers --------------------------------------------------------


def test_both_providers_satisfy_the_protocols_they_claim() -> None:
    """The seam is a type, and a class that drifted off it fails at import, not at run."""
    small = _fixture(events=120, accounts=40, seed=7)
    reg = registry_from_repo(REPO_ROOT)
    cfg = load_pipeline_config(REPO_ROOT)
    graph, rules = fold_providers(small, reg, cfg)
    assert isinstance(graph, GraphFeatureProvider)
    assert isinstance(rules, RuleHitProvider)
    with pytest.raises(GraphScopeError, match="bare DataFrame"):
        require_graph_provider(small)
    with pytest.raises(GraphScopeError, match="bare DataFrame"):
        require_rule_provider(small)


def test_a_fold_graph_carries_every_declared_field_and_not_only_nulls(
    events: pl.DataFrame, registry: FeatureRegistry, plan: SplitPlan
) -> None:
    """The 100 %-null finding, tested against the thing that replaced it.

    Before this module existed every one of the sixteen graph columns was null by
    construction, which made the graph-features ablation row measure nothing. Here every
    declared field arrives, and on a corpus with real topology most of them carry a value --
    which is the difference between a measurement and a no-op.
    """
    cfg = load_pipeline_config(REPO_ROOT)
    graph, _rules = fold_providers(events, registry, cfg)
    fold = plan.folds[-1]
    sealed = graph.fold_graph(fold)
    assert sealed is not None
    rows = table_rows(sealed, fold)
    assert set(rows.columns) == {ACCOUNT, *registry.graph_fields}
    assert rows.height > 0
    populated = [
        field for field in registry.graph_fields if int(rows[field].null_count()) < rows.height
    ]
    assert len(populated) >= GRAPH_FIELDS - 3, (
        f"only {populated} carry a value; on a corpus this connected the remainder is a "
        "provider bug rather than DEV-011's star-shaped finding"
    )
    assert int(rows.filter(pl.col("in_degree") > 0).height) > 0, "no inbound edge in the fold"
    assert isinstance(rows["downstream_outflow_24h_minor"].dtype, pl.Int64), (
        "money crossed a provider as a non-Int64 column (DEV-005)"
    )
    assert int(rows["local_density_bps"].drop_nulls().max() or 0) <= 10_000, (
        "a basis-point share above 10 000 is not a share"
    )


def test_two_folds_do_not_share_one_graph(
    events: pl.DataFrame, registry: FeatureRegistry, plan: SplitPlan
) -> None:
    """The doctrine, tested as a difference rather than asserted in a comment.

    Each fold's graph is built inside its own cutoff, so the later fold -- which has more
    edges to read -- must disagree with the earlier one about at least one node.
    """
    cfg = load_pipeline_config(REPO_ROOT)
    graph, _rules = fold_providers(events, registry, cfg)
    early, late = plan.folds[0], plan.folds[-1]
    assert early.graph_as_of_ts < late.graph_as_of_ts
    early_rows = table_rows(graph.fold_graph(early), early)
    late_rows = table_rows(graph.fold_graph(late), late)
    joined = early_rows.join(late_rows, on=ACCOUNT, suffix="__late")
    assert joined.height
    differs = joined.filter(
        (pl.col("in_degree") != pl.col("in_degree__late"))
        | (pl.col("pagerank") != pl.col("pagerank__late"))
        | (pl.col("cycle_participation_count") != pl.col("cycle_participation_count__late"))
    )
    assert differs.height, (
        f"fold {early.fold_id!r} and fold {late.fold_id!r} produced identical node "
        "attributes: one graph is being reused across folds"
    )


def test_a_table_sealed_for_one_fold_is_unreachable_from_another(
    events: pl.DataFrame, registry: FeatureRegistry, plan: SplitPlan
) -> None:
    """Re-asserted on a *real* table; the leakage suite uses a hand-written frame."""
    cfg = load_pipeline_config(REPO_ROOT)
    graph, _rules = fold_providers(events, registry, cfg)
    early, late = plan.folds[0], plan.folds[1]
    sealed = graph.fold_graph(early)
    assert sealed is not None
    with pytest.raises(GraphScopeError, match="do not cross folds"):
        table_rows(sealed, late)


def test_the_rule_provider_returns_only_hits_made_inside_the_fold(
    events: pl.DataFrame, registry: FeatureRegistry, plan: SplitPlan
) -> None:
    """Each hit names the instant that produced it, and the seal checks it against the cutoff.

    A fold with no hits still yields a table rather than None: "the rules ran and nothing
    fired" and "nobody computed the rules" are different claims, and the artifact has to be
    able to tell them apart.
    """
    cfg = load_pipeline_config(REPO_ROOT)
    _graph, rules = fold_providers(events, registry, cfg)
    fold = plan.folds[-1]
    sealed = rules.fold_rule_hits(fold)
    assert sealed is not None
    frame = table_rows(sealed, fold)
    assert {ACCOUNT, "rule_id", "severity", "last_contributing_ts_utc"} <= set(frame.columns)
    if frame.height:
        latest = frame["last_contributing_ts_utc"].max()
        assert latest is not None and latest <= fold.graph_as_of_ts
    matrix = rule_matrix(sealed, fold, registry)
    assert set(matrix.columns) == {ACCOUNT, *registry.rule_ids}, (
        "a rule that fired on nobody in this fold must still be a column, null rather than "
        "absent: the null policy is declared per rule, not per corpus"
    )


def test_a_fresh_source_reports_nothing_built_yet(registry: FeatureRegistry) -> None:
    """The report reads the source's own bookkeeping, so it cannot report work it skipped."""
    small = _fixture(events=120, accounts=40, seed=17)
    cfg = load_pipeline_config(REPO_ROOT)
    source = FoldGraphSource(
        events=small,
        registry=registry,
        cfg=cfg,
        settings=load_graph_settings(cfg),
        seed=cfg.seed,
    )
    report = provider_report(source, FoldRuleHitProvider(source, registry), [])
    assert report.folds_seen == 0
    assert report.folds_with_graph == 0
    assert report.node_rows == 0 and report.rule_hit_rows == 0
    assert report.sentence().startswith("fold-scoped providers:")


def test_the_provider_report_counts_what_it_built(
    events: pl.DataFrame, registry: FeatureRegistry, plan: SplitPlan
) -> None:
    """Every fold in the ladder gets a graph on a corpus this connected, and the counts add up."""
    cfg = load_pipeline_config(REPO_ROOT)
    graph, rules = fold_providers(events, registry, cfg)
    for fold in plan.folds:
        assert graph.fold_graph(fold) is not None
        assert rules.fold_rule_hits(fold) is not None
    report = provider_report(graph._source, rules, plan.folds)
    assert report.folds_seen == len(plan.folds) == report.folds_with_graph
    assert report.node_rows > 0 and report.edge_rows > 0
    assert report.graph_fields_populated > 0
    assert set(report.hit_rule_ids) <= set(registry.rule_ids)


# --- the bridge -----------------------------------------------------------


def test_the_frame_is_account_grain_and_unique_per_instant(bridged: AccountGrainFrame) -> None:
    """One row per ``(account_key, as_of_ts)``, five folds, and the contract's dtypes."""
    frame = bridged.frame
    assert frame.height > 0
    duplicates = (
        frame.group_by([ACCOUNT_KEY, AS_OF_TS])
        .agg(pl.len().alias("rows"))
        .filter(pl.col("rows") > 1)
    )
    assert duplicates.height == 0, f"duplicated account-instant rows: {duplicates.head(3).rows()}"
    assert sorted(frame[FOLD].unique().to_list()) == list(range(N_FOLDS)), (
        "the ladder has five folds and every one of them must be present in the frame"
    )
    assert frame.schema[AS_OF_TS] == pl.Datetime("us", UTC)
    assert frame.schema[ACCOUNT_KEY] == pl.String
    assert frame[FOLD].null_count() == 0


def test_the_labels_cross_the_seam_as_contract_columns_and_never_as_features(
    bridged: AccountGrainFrame, registry: FeatureRegistry
) -> None:
    """The guard that was firing on the live run is satisfied here, not silenced."""
    frame = bridged.frame
    assert not (set(registry.guards.banned_sources) & set(registry.matrix_ids))
    assert LABEL_FRAUD in frame.columns and LABEL_FRAUD not in registry.matrix_ids
    assert set(frame[LABEL_FRAUD].unique().to_list()) <= {0, 1}
    assert frame[LABEL_FRAUD].null_count() == 0
    assert frame[LABEL_TYPOLOGY].dtype == pl.String
    assert bridged.report.labels_carried_outside_matrix
    positives = int(frame[LABEL_FRAUD].sum())
    assert 0 < positives < frame.height, "a frame with no positives cannot train anything"
    assert bridged.report.positives == positives


def test_an_unlabelled_corpus_is_refused_rather_than_zero_filled(
    events: pl.DataFrame, registry: FeatureRegistry, plan: SplitPlan
) -> None:
    """03 A rule 2 at the seam: an unknown label is not a zero, so no frame is produced.

    Both shapes of "unknown" are checked, because they are caught by different guards
    and only the second one is the bridge's own work. A missing column never reaches
    the bridge at all -- the canonical input contract refuses it -- while a column
    that is present and null is exactly the case a pipeline would be tempted to
    ``fill_null(0)`` and train on, so the bridge has to refuse it by name and count.
    """
    cfg = load_pipeline_config(REPO_ROOT)
    unlabelled = events.with_columns(pl.lit(None, dtype=pl.Int64).alias(LABEL_FRAUD))
    assert unlabelled.get_column(LABEL_FRAUD).null_count() == unlabelled.height
    graph, rules = fold_providers(unlabelled, registry, cfg)
    with pytest.raises(LabelUnavailableError, match=r"have no label for their txn_id"):
        build_account_frame(unlabelled, registry, plan, graph=graph, rules=rules, cfg=cfg)

    absent = events.drop(LABEL_FRAUD)
    with pytest.raises(Exception, match="missing"):
        fold_providers(absent, registry, cfg)


def test_money_columns_arrive_as_integer_minor_units(
    bridged: AccountGrainFrame, registry: FeatureRegistry
) -> None:
    """DEV-005 across a grain change: the bridge adds no arithmetic, so it adds no float."""
    for entry in registry.matrix_entries:
        if entry.id.endswith("_minor") or entry.id.endswith("_bps"):
            assert str(bridged.frame[entry.id].dtype) in {"Int64", "Int32"}, (
                f"{entry.id} crossed the bridge as {bridged.frame[entry.id].dtype}"
            )


def test_an_account_row_never_reads_past_its_own_instant(
    events: pl.DataFrame, registry: FeatureRegistry, plan: SplitPlan
) -> None:
    """Truncation invariance, one grain up.

    Delete the future, rebuild, and every surviving account row must be identical. A pooling
    that averaged across instants, or a carry that looked forward, fails here -- the same
    clause ``tests/test_leakage.py`` asserts one grain below this one, now asserted over the
    frame a model would actually train on.
    """
    cfg = load_pipeline_config(REPO_ROOT)
    full_graph, full_rules = fold_providers(events, registry, cfg)
    full = build_account_frame(events, registry, plan, graph=full_graph, rules=full_rules, cfg=cfg)
    for cutoff in (plan.folds[2].train_end_ts, plan.folds[3].validation_start_ts):
        kept = events.filter(pl.col(EVENT_TS) <= cutoff)
        assert 0 < kept.height < events.height, "the cutoff left nothing to test"
        graph, rules = fold_providers(kept, registry, cfg)
        truncated = build_account_frame(kept, registry, plan, graph=graph, rules=rules, cfg=cfg)
        shared = truncated.frame.filter(pl.col(AS_OF_TS) <= cutoff)
        base = full.frame.filter(pl.col(AS_OF_TS) <= cutoff)
        joined = shared.join(base, on=[ACCOUNT_KEY, AS_OF_TS], how="inner", suffix="__full")
        assert joined.height == shared.height == base.height, (
            "an account row appeared or vanished when the future was deleted"
        )
        for entry in registry.matrix_entries:
            differs = (joined[entry.id] != joined[f"{entry.id}__full"]) & ~(
                joined[entry.id].is_null() & joined[f"{entry.id}__full"].is_null()
            )
            count = int(differs.sum())
            assert count == 0, (
                f"feature {entry.id} moved on {count} row(s) when rows after "
                f"{cutoff.isoformat()} were deleted: the carry reads the future"
            )


def test_the_fold_a_row_belongs_to_is_decided_by_the_plan_alone() -> None:
    """Membership is ``SplitPlan.fold_for``; the bridge derives no boundary of its own."""
    plan = _tiny_plan()
    first = plan.folds[0]
    assert fold_membership(plan, first.test_start_ts + timedelta(minutes=5)).fold_id == "wf-0"
    assert fold_membership(plan, first.train_start_ts + timedelta(minutes=1)).fold_id == "wf-0", (
        "pre-ladder history is the material later folds fit on, and it is filed under the "
        "first fold rather than silently dropped"
    )
    with pytest.raises(GrainBridgeError, match="gap"):
        fold_membership(plan, plan.folds[1].train_end_ts + timedelta(minutes=1))


def test_the_bridge_output_satisfies_the_scoring_contract(
    bridged: AccountGrainFrame, registry: FeatureRegistry
) -> None:
    """The point of the module: P4's validator accepts what P2's matrix became.

    This is where the two digest halves meet. The frame's stamp is the registry's, so the
    recomputation inside ``build_training_frame`` *passes* where it used to refuse every real
    frame while two hashes disagreed.
    """
    view = load_feature_registry(REPO_ROOT)
    scorecard = load_scorecard_config(REPO_ROOT)
    categorical = tuple(name for name in scorecard.binning.categorical_features)
    training = build_training_frame(
        bridged.frame,
        view,
        categorical,
        PROVENANCE_REAL,
        0.15,
        N_FOLDS,
    )
    assert training.feature_spec_hash == registry.spec_hash == bridged.spec_hash
    assert training.n_rows == bridged.frame.height
    assert set(training.feature_names) == set(registry.matrix_ids)
    assert training.provenance == PROVENANCE_REAL
    assert training.folds == tuple(range(N_FOLDS))


def test_the_anchor_rule_is_stated_and_its_ambiguity_measured(
    bridged: AccountGrainFrame, registry: FeatureRegistry
) -> None:
    """Which columns depend on the anchor event, and how often the anchor had to be chosen."""
    named = anchor_relative_columns(registry)
    assert bridged.report.anchor_relative_columns == named
    assert set(named) <= set(registry.matrix_ids)
    # Four row-local declarations plus three whose declared partition is finer than the
    # account: the columns whose account-grain meaning is inherited, not pooled.
    assert len(named) == 7, named
    assert bridged.report.anchor_ambiguous_instants >= 0
    assert bridged.report.rows > 0
    assert bridged.report.matrix_feature_count == len(registry.matrix_ids)


def test_two_builds_of_one_corpus_produce_one_digest(
    events: pl.DataFrame, registry: FeatureRegistry, plan: SplitPlan
) -> None:
    """Byte determinism at the new grain: no wall clock, no input-order dependence."""
    cfg = load_pipeline_config(REPO_ROOT)
    first_graph, first_rules = fold_providers(events, registry, cfg)
    first = build_account_frame(
        events, registry, plan, graph=first_graph, rules=first_rules, cfg=cfg
    )
    second_graph, second_rules = fold_providers(events, registry, cfg)
    second = build_account_frame(
        events.sort([EVENT_TS, TXN_ID], descending=True),
        registry,
        plan,
        graph=second_graph,
        rules=second_rules,
        cfg=cfg,
    )
    assert frame_content_hash(first.frame) == frame_content_hash(second.frame)
    assert first.report.sentence() == second.report.sentence()


def test_to_micros_is_exact_and_refuses_a_naive_instant() -> None:
    """Window arithmetic is integer, so a boundary row cannot land on either side by luck."""
    from oxbow.features.fold_providers import FoldProviderError

    moment = datetime(2024, 5, 4, 3, 2, 1, 654_321, tzinfo=UTC)
    assert to_micros(moment) == 1_714_791_721_654_321
    with pytest.raises(FoldProviderError, match="naive"):
        to_micros(datetime(2024, 5, 4, 3, 2, 1))


# --- fixtures for the membership test -------------------------------------


def _tiny_plan() -> SplitPlan:
    """A two-fold plan with a deliberate gap: enough to test membership, nothing else."""
    folds = (
        Fold(
            fold_id="wf-0",
            index=0,
            train_start_ts=EPOCH,
            train_end_ts=EPOCH + 30 * DAY,
            validation_start_ts=EPOCH + 25 * DAY,
            test_start_ts=EPOCH + 40 * DAY,
            test_end_ts=EPOCH + 50 * DAY,
            purge_days=1,
            label_window_days=1,
            seed=SEED,
        ),
        Fold(
            fold_id="wf-1",
            index=1,
            train_start_ts=EPOCH,
            train_end_ts=EPOCH + 100 * DAY,
            validation_start_ts=EPOCH + 90 * DAY,
            test_start_ts=EPOCH + 130 * DAY,
            test_end_ts=EPOCH + 140 * DAY,
            purge_days=1,
            label_window_days=1,
            seed=SEED,
        ),
    )
    return SplitPlan(
        seed=SEED,
        embargo_days=30,
        folds=folds,
        entity_split=None,
        optimised_on="walk_forward",
        timeline_start=EPOCH,
        timeline_end=EPOCH + 140 * DAY,
        registry_max_lookback_days=30,
        spec_hash="0" * 64,
    )
