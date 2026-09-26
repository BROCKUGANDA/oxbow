"""Plan §8's leakage gate, and the proof that it bites.

WHAT A PASSES (clause one of §8's gate). *No feature reads any row whose timestamp exceeds
its own as-of cutoff.* The proof is truncation invariance, run over every column the
registry computes and at several cutoffs: build the table, then rebuild it with every row
after a cutoff deleted, and every value on every surviving row must be identical. A
backward-only quantity cannot tell the two builds apart; a forward-looking one is *made of*
the deleted rows, so it must change. ``oxbow.features.leakage`` is the library form of that
procedure, and this file is its test — the same code CI runs and the same code a run itself
calls, so the fixture proves the guard bites rather than proving the test bites.

WHAT B PASSES (clause two, the clause the phase is judged on). *The test is demonstrated to
bite.* A deliberately leaking feature is registered through the same loader the honest
registry uses — the same YAML mapping, the same ``parse_registry``, the same kernel
dispatch — and the gate must refuse it, naming it. Its output is pasted into the phase
report. 00 §B: "a test that does not fail when a deliberately leaking feature is
introduced is not a gate; it is decoration." Three further refusals are asserted so the
bite cannot be an artefact of the fixture's shape: the loader refuses a forward window that
declares itself a model input, the gate refuses to certify a column it was never able to
compare (an empty sweep passes as loudly as a full one otherwise), and swapping the leak for
its honest backward twin — same window, same predicate, same aggregation — makes the very
same sweep pass.

WHAT THIS FILE DOES NOT CLAIM. Truncation invariance covers the row-anchored kernels, whose
inputs all live in the frame being truncated. Graph and rule features arrive from other
layers as fold-scoped tables and are guarded structurally instead — a table fed edges past
its fold's cutoff cannot be sealed at all — which :class:`FoldScopeLeakageArm` tests in the
same file because plan §8 asks for both arms as one gate.

Money: no float amounts anywhere (DEV-005); every expected value in the fixtures is written
down before the code ran, and the assertions that matter here are equalities between two
builds rather than numbers, so there is nothing to derive from the system under test.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import polars as pl
import pytest

from oxbow.backtest.splits import Fold
from oxbow.config import find_repo_root, load_yaml
from oxbow.contracts.canonical_v1 import CANONICAL_COLUMNS
from oxbow.features.build import (
    FeatureHashMismatchError,
    FeatureTableBuilder,
    build_feature_table,
)
from oxbow.features.compute import registry_from_repo, registry_with_code_version
from oxbow.features.fold_scope import (
    ACCOUNT,
    CONTRIBUTING_TS_COLUMN,
    RULE_COLUMN,
    SEVERITY_COLUMN,
    GraphScopeError,
    require_graph_provider,
    require_rule_provider,
    seal_node_table,
)
from oxbow.features.kinds import ENTITY, EVENT_TS, TXN_ID
from oxbow.features.leakage import (
    FutureReadError,
    assert_backward_only,
    truncation_invariance_violations,
)
from oxbow.features.registry import FeatureRegistry, RegistryError, parse_registry

BASE: datetime = datetime(2024, 3, 1, tzinfo=UTC)
HOUR: timedelta = timedelta(hours=1)
DAY: timedelta = timedelta(days=1)

REPO_ROOT: Path = find_repo_root()
FEATURES_YAML: Path = REPO_ROOT / "config" / "features.yaml"

ALICE: str = "a11ce0000000"
BOB: str = "b0b000000000"
CAROL: str = "c0c500000000"
DAVE: str = "d0de00000000"
MALL: str = "ma1100000000"
# A second currency, and two accounts that only ever appear in it: plan §8's money rule
# refuses a frame where one account shows two currencies, so the partition has to be
# disjoint rather than bilingual.
KEVIN: str = "k3v100000000"
KIRA: str = "k1r400000000"

#: The deliberately leaking column. Declared below with the same loader the real registry
#: uses, so nothing about it is a test-only code path except that it reads forward.
LEAK_ID: Final = "leak_forward_mean_inflow_7d_minor"
#: The honest twin: same window, same predicate, same aggregation, backward.
TWIN_ID: Final = "honest_trailing_mean_inflow_7d_minor"

# Cutoffs chosen against the fixture, not against a build of it: the first is inside the
# 09:00/10:00/11:00 burst on day 3, the second one hour after a row that opens a new
# counterparty relationship, the third sits mid-window for every 30d feature, the fourth
# lands on the reversal, and the fifth is late in the corpus with rows still to come. Every
# one of them leaves something on both sides, because a cutoff with no future tests nothing
# and the auditor says so by skipping it.
CUTOFFS: Final[tuple[datetime, ...]] = (
    BASE + 3 * DAY + timedelta(hours=10),
    BASE + 6 * DAY + timedelta(hours=12),
    BASE + 21 * DAY,
    BASE + 33 * DAY + timedelta(hours=2),
    BASE + 44 * DAY + timedelta(hours=12),
)


def _event(
    index: int,
    src: str,
    dst: str,
    amount_minor: int,
    at: datetime,
    *,
    txn_type: str = "TRANSFER",
    currency: str = "UGX",
    fraud: int = 0,
) -> dict[str, Any]:
    """One canonical event, balances consistent by construction."""
    local = at + timedelta(hours=3)
    return {
        "txn_id": f"paysim:{index}",
        "event_ts_utc": at,
        "event_date_local": local.date(),
        "local_hour": local.hour,
        "txn_type": txn_type,
        "channel": "USSD" if local.hour < 6 else "APP",
        "amount_minor": amount_minor,
        "currency": currency,
        "account_from": src,
        "account_to": dst,
        "src_balance_before_minor": 500_000,
        "src_balance_after_minor": 500_000 - amount_minor,
        "dst_balance_before_minor": 25_000,
        "dst_balance_after_minor": 25_000 + amount_minor,
        "label_is_fraud": fraud,
        "label_is_flagged": 0,
        "label_typology": None,
        "source_dataset": "paysim",
        "ingested_at": at,
        "run_id": "01HQX2Y3Z4A5B6C7D8E9F0G1H2",
        "batch_id": "0123456789ab",
    }


def _fixture_rows() -> list[dict[str, Any]]:
    """Forty-one rows spanning forty-five days, with a burst, a reversal and a lull.

    Shaped so every declared kind has a window to be wrong in: three same-day rows for the
    distinct-day kernel, a REVERSAL for the money rules, an overnight local hour, a weekend
    local date, a 20-day lull that dormancy must notice, and a second currency partition
    that must never be summed with the first.
    """
    rows: list[dict[str, Any]] = []
    index = 0

    def add(*args: Any, **kwargs: Any) -> None:
        nonlocal index
        index += 1
        rows.append(_event(index, *args, **kwargs, ))

    # Day 0-1: Alice and Bob meet, twice on the same local day and once the next.
    add(ALICE, BOB, 10_000, BASE + HOUR)
    add(BOB, ALICE, 4_000, BASE + 5 * HOUR)
    add(ALICE, MALL, 90_000, BASE + 9 * HOUR, txn_type="CASH_IN")
    add(ALICE, BOB, 2_000, BASE + DAY + 2 * HOUR)
    # Day 3: a burst — three transfers inside three hours, the busy part a cutoff must sit in.
    add(ALICE, BOB, 7_500, BASE + 3 * DAY + timedelta(hours=9))
    add(ALICE, BOB, 7_500, BASE + 3 * DAY + timedelta(hours=10))
    add(ALICE, BOB, 7_500, BASE + 3 * DAY + timedelta(hours=11))
    # Day 4 overnight, local 03:00 = 00:00 UTC, plus a zero-amount balance probe.
    add(BOB, ALICE, 0, BASE + 4 * DAY)
    add(CAROL, ALICE, 12_000, BASE + 4 * DAY + timedelta(hours=3))
    # Day 6: Alice meets Carol properly, and a refund of an earlier payment arrives.
    add(ALICE, CAROL, 25_000, BASE + 6 * DAY + timedelta(hours=11))
    add(CAROL, ALICE, 25_000, BASE + 6 * DAY + timedelta(hours=12), txn_type="REVERSAL")
    # Days 8-21: a second currency partition of its own, and a long quiet spell for Alice.
    add(KEVIN, KIRA, 3_300, BASE + 8 * DAY, currency="KES")
    add(KIRA, KEVIN, 1_500, BASE + 12 * DAY + timedelta(hours=6), currency="KES")
    add(MALL, BOB, 220_000, BASE + 15 * DAY, txn_type="CASH_OUT")
    add(BOB, MALL, 210_000, BASE + 18 * DAY, txn_type="CASH_OUT")
    add(ALICE, DAVE, 1_100, BASE + 21 * DAY)
    # Days 22-33: a structuring ladder, and a reversal at the cutoff the gate probes.
    for step in range(6):
        add(DAVE, ALICE, 9_900 - step * 100, BASE + (22 + step) * DAY + timedelta(hours=7))
    add(ALICE, MALL, 40_000, BASE + 30 * DAY, txn_type="PAYMENT")
    add(MALL, ALICE, 40_000, BASE + 33 * DAY + timedelta(hours=1), txn_type="REVERSAL")
    add(CAROL, BOB, 61_000, BASE + 33 * DAY + timedelta(hours=2))
    # Days 35-45: dormancy ends, a weekend lands, and the corpus' last row is a cash-out.
    add(ALICE, BOB, 15_000, BASE + 35 * DAY + timedelta(hours=4))
    add(BOB, CAROL, 8_000, BASE + 38 * DAY)
    add(CAROL, BOB, 8_000, BASE + 38 * DAY + timedelta(hours=9))
    add(ALICE, DAVE, 2_500, BASE + 40 * DAY)
    add(DAVE, CAROL, 3_500, BASE + 42 * DAY + timedelta(hours=2))
    add(CAROL, DAVE, 4_500, BASE + 44 * DAY + timedelta(hours=23))
    add(ALICE, MALL, 300_000, BASE + 45 * DAY - HOUR, txn_type="CASH_OUT", fraud=1)
    return rows


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    columns: dict[str, pl.Series] = {}
    from oxbow.contracts.canonical_v1 import CANONICAL_DTYPES

    for name in CANONICAL_COLUMNS:
        columns[name] = pl.Series(name, [row[name] for row in rows], dtype=CANONICAL_DTYPES[name])
    return pl.DataFrame(columns).sort([EVENT_TS, TXN_ID])


@pytest.fixture(scope="module")
def events() -> pl.DataFrame:
    return _frame(_fixture_rows())


@pytest.fixture(scope="module")
def registry() -> FeatureRegistry:
    return registry_from_repo(REPO_ROOT)


def _entry(
    feature_id: str,
    *,
    kind: str,
    role: str,
    sentence: str,
    extra: Mapping[str, object],
) -> dict[str, object]:
    """One registry entry, written in exactly the shape ``config/features.yaml`` uses."""
    entry: dict[str, object] = {
        "id": feature_id,
        "group": "volume_value",
        "sentence": sentence,
        "kind": kind,
        "as_of": "row_event_ts",
        "window": "7d",
        "group_by": ["entity", "currency"],
        "dtype": "int64",
        "null_policy": "null_when_window_empty",
        "leakage_sensitive": True,
        "leakage_note": "trailing seven-day money mean, so its window is the whole question",
        "role": role,
    }
    entry.update(extra)
    return entry


def registry_with(leak: bool = True, twin: bool = False) -> FeatureRegistry:
    """The real registry plus a forward-looking column, loaded through the real loader.

    The entry is appended to the *parsed YAML mapping* and handed to ``parse_registry``, so
    the deliberate leak travels the same path as every honest feature: same validation, same
    hash, same dispatch table, same builder loop. The twin is declared ``intermediate``
    because plan §8 bounds the published matrix at 60-75 columns and the loader enforces it;
    an intermediate is still computed by the same kernels and is still swept by the gate, so
    the comparison below is about direction and not about role.
    """
    raw: dict[str, object] = load_yaml(FEATURES_YAML)
    features = list(raw["features"] if isinstance(raw["features"], list) else [])
    if leak:
        features.append(
            _entry(
                LEAK_ID,
                kind="forward_window",
                role="outcome",
                sentence=(
                    "Deliberate leak: the mean receipt over the seven days AFTER this "
                    "transaction, which no scorer may ever see."
                ),
                extra={
                    "agg": "mean_int",
                    "source": "amount_minor",
                    "where": "is_inflow_nonzero",
                    "leakage_note": (
                        "reads rows after the cutoff on purpose; this entry exists so the "
                        "gate can be shown to refuse it"
                    ),
                },
            )
        )
    if twin:
        features.append(
            _entry(
                TWIN_ID,
                kind="window_agg",
                role="intermediate",
                sentence=(
                    "Honest twin: the mean receipt over the seven days up to this "
                    "transaction, same predicate and window as the leak."
                ),
                extra={
                    "agg": "mean_int",
                    "source": "amount_minor",
                    "where": "is_inflow_nonzero",
                },
            )
        )
    return parse_registry({**raw, "features": features})


# --- clause one: no feature reads past its own cutoff --------------------------------


def test_no_feature_reads_any_row_after_its_own_cutoff(
    events: pl.DataFrame, registry: FeatureRegistry
) -> None:
    """§8 clause one, over every computed column and every probe cutoff."""
    builder = FeatureTableBuilder(registry)
    checked = assert_backward_only(
        builder,
        events,
        cutoffs=CUTOFFS,
        columns=registry.computed_ids,
    )
    assert checked == len(CUTOFFS) * len(registry.computed_ids)
    assert checked >= len(CUTOFFS) * 90, (
        "the sweep is supposed to cover the whole registry, not a comfortable subset"
    )


def test_every_published_column_is_swept_not_a_handful(events: pl.DataFrame) -> None:
    """A gate handed a short column list would pass by ignoring most of the registry.

    ``assert_backward_only`` refuses columns it cannot find rather than skipping them, so
    asking for a name that was never computed fails here exactly as it would in CI.
    """
    registry = registry_with(leak=False)
    builder = FeatureTableBuilder(registry)
    with pytest.raises(FutureReadError, match="never computed"):
        assert_backward_only(
            builder,
            events,
            cutoffs=CUTOFFS[:1],
            columns=[*registry.computed_ids, "a_column_nobody_declared"],
        )


def test_truncation_probe_reports_the_rows_it_compared(events: pl.DataFrame) -> None:
    """A probe that compared nothing must not read as a pass."""
    builder = FeatureTableBuilder(registry_from_repo(REPO_ROOT))
    violations = truncation_invariance_violations(
        builder,
        events,
        cutoffs=CUTOFFS,
        columns=builder.registry.matrix_ids,
    )
    assert violations == []
    truncated = events.filter(pl.col(EVENT_TS) <= CUTOFFS[0])
    assert 0 < truncated.height < events.height


def test_no_feature_reads_the_future_on_a_real_paysim_slice() -> None:
    """The same clause on real bytes rather than a shaped fixture.

    The head of PaySim's own file is its step order, so this is a deterministic time slice;
    fifty thousand events is enough to make every window non-empty for the busy accounts
    while staying a fraction of the gate's second budget.
    """
    from tests.unit.p2_fixtures import paysim_slice

    events = paysim_slice(50_000)
    stamps = events[EVENT_TS].sort()
    cutoffs = tuple(
        stamps[int(fraction * (stamps.len() - 1))].replace(tzinfo=UTC)
        for fraction in (0.25, 0.5, 0.75, 0.93)
    )
    registry = registry_from_repo(REPO_ROOT)
    checked = assert_backward_only(
        FeatureTableBuilder(registry),
        events,
        cutoffs=cutoffs,
        columns=registry.computed_ids,
    )
    assert checked == len(cutoffs) * len(registry.computed_ids)


# --- clause two: the gate is demonstrated to bite ------------------------------------


def test_the_gate_catches_a_deliberately_leaking_feature(events: pl.DataFrame) -> None:
    """§8 clause two. Remove the leak and this test fails; that is its whole purpose."""
    leaking = registry_with(leak=True)
    builder = FeatureTableBuilder(leaking)
    assert LEAK_ID in builder.compute_frame(events).columns
    with pytest.raises(FutureReadError) as caught:
        assert_backward_only(
            builder,
            events,
            cutoffs=CUTOFFS,
            columns=leaking.computed_ids,
        )
    message = str(caught.value)
    assert LEAK_ID in message
    assert "read past their own as-of cutoff" in message


def test_the_leak_is_caught_at_every_probe_not_just_one(events: pl.DataFrame) -> None:
    """One differing cutoff is a coincidence; every cutoff is a property of the kernel."""
    leaking = registry_with(leak=True)
    violations = truncation_invariance_violations(
        FeatureTableBuilder(leaking),
        events,
        cutoffs=CUTOFFS,
        columns=[LEAK_ID],
    )
    offenders = {violation.cutoff_ts for violation in violations}
    assert len(offenders) == len(CUTOFFS), offenders
    assert all(violation.feature_id == LEAK_ID for violation in violations)


def test_the_honest_twin_of_the_leak_passes_the_same_sweep(events: pl.DataFrame) -> None:
    """The bite is about direction, not about the fixture's shape.

    Same window, same predicate, same aggregation, same partition, computed by the same
    money machinery — one declaration (``forward_window`` instead of ``window_agg``) apart.
    The leak is refused and this passes, so the gate discriminates on the only axis it
    claims to discriminate on.
    """
    twin = registry_with(leak=False, twin=True)
    assert TWIN_ID in twin.computed_ids
    assert TWIN_ID not in twin.matrix_ids, (
        "the twin is an intermediate: plan §8 bounds the published matrix at 60-75 and the "
        "shipped registry already sits at the ceiling, so a synthetic column cannot join it"
    )
    checked = assert_backward_only(
        FeatureTableBuilder(twin),
        events,
        cutoffs=CUTOFFS,
        columns=twin.computed_ids,
    )
    assert checked == len(CUTOFFS) * len(twin.computed_ids)


def test_a_forward_window_may_not_declare_itself_a_model_input() -> None:
    """The structural half of the same guard: the loader refuses the role, not the value."""
    raw: dict[str, object] = load_yaml(FEATURES_YAML)
    features = list(raw["features"] if isinstance(raw["features"], list) else [])
    features.append(
        _entry(
            "forward_mean_inflow_as_a_feature_minor",
            kind="forward_window",
            role="feature",
            sentence=(
                "A forward-looking mean that someone has tried to publish as a model "
                "column, which the registry must refuse."
            ),
            extra={"agg": "mean_int", "source": "amount_minor", "where": "is_inflow_nonzero"},
        )
    )
    with pytest.raises(RegistryError, match="may only be declared with role 'outcome'"):
        parse_registry({**raw, "features": features})


def test_a_label_derived_feature_is_refused_at_load() -> None:
    """Leakage by *name*: reading the answer is refused before any value is computed."""
    raw: dict[str, object] = load_yaml(FEATURES_YAML)
    features = list(raw["features"] if isinstance(raw["features"], list) else [])
    features.append(
        {
            "id": "count_of_flagged_rows_30d",
            "group": "volume_value",
            "sentence": "How many of this account's last thirty days were flagged by the label.",
            "kind": "window_agg",
            "agg": "count",
            "source": "label_is_fraud",
            "where": "always",
            "as_of": "row_event_ts",
            "window": "30d",
            "group_by": ["entity"],
            "dtype": "int64",
            "null_policy": "never_null",
            "leakage_sensitive": False,
            "leakage_note": "not sensitive, according to whoever wrote it",
        }
    )
    with pytest.raises(RegistryError, match="banned label column"):
        parse_registry({**raw, "features": features})


# --- the feature-spec hash seam (plan §8, 02 §B seam 3) ------------------------------


def test_feature_hash_mismatch_refuses(events: pl.DataFrame, registry: FeatureRegistry) -> None:
    """A matrix built from a different spec than training used must not be scored."""
    trained = build_feature_table(events, registry)
    assert trained.spec_hash == registry.spec_hash
    # Scoring against the spec it was actually built from is allowed.
    trained.assert_feature_hash_matches(trained.spec_hash)

    raw: dict[str, object] = load_yaml(FEATURES_YAML)
    features = [dict(entry) for entry in raw["features"] if isinstance(entry, dict)]
    target = next(entry for entry in features if entry["id"] == "txn_count_in_24h")
    target["window"] = "12h"
    narrowed = parse_registry({**raw, "features": features})
    assert narrowed.spec_hash != registry.spec_hash

    scored = build_feature_table(events, narrowed)
    with pytest.raises(FeatureHashMismatchError, match="Refusing to score"):
        scored.assert_feature_hash_matches(trained.spec_hash)


def test_the_hash_covers_the_code_version_of_the_kernels(
    events: pl.DataFrame, registry: FeatureRegistry
) -> None:
    """Identical declarations and different kernel code are a different feature table.

    ``registry_with_code_version`` is the sanctioned way to say that: it re-runs the
    loader's own hash rather than pasting a digest forward.
    """
    moved = registry_with_code_version(registry, "p2.99-code-version-probe")
    assert moved.spec_hash != registry.spec_hash
    assert len(moved.spec_hash) == 64
    trained = build_feature_table(events, registry)
    scored = build_feature_table(events, moved)
    with pytest.raises(FeatureHashMismatchError):
        scored.assert_feature_hash_matches(trained.spec_hash)
    assert scored.matrix.columns == trained.matrix.columns


def test_the_hash_is_a_pure_function_of_the_declaration(registry: FeatureRegistry) -> None:
    """Two loads of one file produce one hash, or the seam is noise."""
    again = registry_from_repo(REPO_ROOT)
    assert again.spec_hash == registry.spec_hash


# --- the structural arm: fold-scoped graph and rule features -------------------------


class _NodeTable:
    """A graph table whose edges reach ``reaches`` past the fold's cutoff."""

    def __init__(self, registry: FeatureRegistry, reaches: timedelta) -> None:
        self.registry = registry
        self.reaches = reaches

    def produce(self, fold: Fold) -> tuple[pl.DataFrame, pl.DataFrame]:
        nodes = pl.DataFrame(
            {
                ACCOUNT: [ALICE, BOB],
                **{field: [0] * 2 for field in self.registry.graph_fields},
            }
        )
        edges = pl.DataFrame(
            {
                EVENT_TS: [fold.graph_as_of_ts + self.reaches, fold.graph_as_of_ts - DAY],
                "amount_minor": [10, 20],
            }
        )
        return nodes, edges


class TestFoldScopeLeakageArm:
    """Graph and rule features are recomputed per fold from that fold's edges only."""

    def test_a_graph_table_fed_future_edges_cannot_be_sealed(
        self, registry: FeatureRegistry
    ) -> None:
        fold = _probe_fold()
        producer = _NodeTable(registry, reaches=HOUR).produce
        provider = _provider(producer, registry)
        with pytest.raises(GraphScopeError, match="whole-corpus graph"):
            provider.fold_graph(fold)

    def test_a_graph_table_from_the_fold_own_edges_seals_and_is_readable(
        self, registry: FeatureRegistry
    ) -> None:
        fold = _probe_fold()
        producer = _NodeTable(registry, reaches=-DAY).produce
        table = _provider(producer, registry).fold_graph(fold)
        assert table is not None
        assert table.height == 2

    def test_a_bare_dataframe_is_refused_at_the_provider_position(
        self, registry: FeatureRegistry
    ) -> None:
        nodes = pl.DataFrame(
            {ACCOUNT: [ALICE], **{field: [0] for field in registry.graph_fields}}
        )
        with pytest.raises(GraphScopeError, match="bare DataFrame"):
            require_graph_provider(nodes)
        with pytest.raises(GraphScopeError, match="bare DataFrame"):
            require_rule_provider(nodes)

    def test_a_table_sealed_for_one_fold_is_unreachable_from_another(
        self, registry: FeatureRegistry
    ) -> None:
        fold = _probe_fold()
        other = _probe_fold(index=3, fold_id="wf-3")
        producer = _NodeTable(registry, reaches=-DAY).produce
        table = _provider(producer, registry).fold_graph(fold)
        assert table is not None
        from oxbow.features.fold_scope import table_rows

        with pytest.raises(GraphScopeError, match="do not cross folds"):
            table_rows(table, other)

    def test_the_builder_refuses_a_graph_source_that_is_not_a_provider(
        self, events: pl.DataFrame, registry: FeatureRegistry
    ) -> None:
        nodes = pl.DataFrame(
            {ACCOUNT: [ALICE], **{field: [0] for field in registry.graph_fields}}
        )
        with pytest.raises(GraphScopeError, match="bare DataFrame"):
            build_feature_table(events, registry, graph_features=nodes)  # type: ignore[arg-type]


def _probe_fold(*, index: int = 2, fold_id: str | None = None) -> Fold:
    """A fold with hand-written boundaries; only its cutoffs matter to this arm."""
    return Fold(
        fold_id=fold_id or f"wf-{index}",
        index=index,
        train_start_ts=BASE,
        train_end_ts=BASE + 30 * DAY,
        validation_start_ts=BASE + 25 * DAY,
        test_start_ts=BASE + 60 * DAY,
        test_end_ts=BASE + 75 * DAY,
        purge_days=1,
        label_window_days=1,
        seed=1337,
    )


def _provider(producer: Any, registry: FeatureRegistry) -> Any:
    from oxbow.features.fold_scope import graph_provider_from_callable

    return graph_provider_from_callable(producer, registry)


def test_a_rule_hit_table_produced_by_future_events_is_refused(
    registry: FeatureRegistry,
) -> None:
    """The rule arm of the same structural guard (plan §8: fold-scoped, per fold)."""
    from oxbow.features.fold_scope import seal_rule_hit_table

    fold = _probe_fold()
    hits = pl.DataFrame(
        {
            ACCOUNT: [ALICE, BOB],
            RULE_COLUMN: ["R5", "R6"],
            SEVERITY_COLUMN: [0.4, 0.9],
            CONTRIBUTING_TS_COLUMN: [
                fold.graph_as_of_ts + HOUR,
                fold.graph_as_of_ts - DAY,
            ],
        }
    )
    with pytest.raises(GraphScopeError, match="at or after"):
        seal_rule_hit_table(fold=fold, hits=hits, registry=registry)


def test_the_gate_reads_the_shipped_registry_not_a_test_copy(registry: FeatureRegistry) -> None:
    """A guard run against a fixture registry would certify a fixture, not the build."""
    on_disk = registry_from_repo(REPO_ROOT)
    assert on_disk.matrix_ids == registry.matrix_ids
    assert on_disk.spec_hash == registry.spec_hash
    assert len(on_disk.matrix_ids) >= 60
