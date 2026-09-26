"""Golden-fixture SELF-CHECK: validates the corpus against its own contract.

This file contains NO rule code. It cannot, because at write time none exists
and 00 SB forbids the alternative: a fixture whose expectations were produced
by the engine under test proves nothing. What is checked here instead:

  * the canonical column set, exactly and in order (task contract);
  * money is integers - dtypes, raw-cell shape, and an AST scan of the
    generator for float literals in money positions (DEV-005, 01 SB);
  * local_hour and event_date_local agree with event_ts_utc under
    Africa/Kampala for EVERY row (03 SC: an ODD_HOUR rule that reads UTC is
    a bug; a fixture whose hour column lies is a worse one);
  * txn_id uniqueness, namespaced format (DEV-004), and the pipeline total
    order (event_ts_utc, txn_id) as file order;
  * the planted singletons of structure: exactly one self-transfer, exactly
    two zero-amount probes, exactly two reversals, exactly three
    balance-delta-inconsistent rows, exactly two USD rows - each equal to
    the list expected.yaml CLAIMS, not just internally consistent;
  * scenario bookkeeping: every row belongs to exactly one scenario, counts
    in expected.yaml match the built file, and the planted totals match;
  * the degree structure the rail-typing formula (committed P3a,
    percentile_nearest_rank at graph.rail_degree_percentile) needs to keep
    every expected-hit account un-typed while the rail node stays typed;
  * determinism: rebuilding produces byte-identical output (seed 1337).
"""

from __future__ import annotations

import ast
import csv
import hashlib
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import build_fixture
import polars as pl
import pytest
import yaml
from loader import GOLDEN_COLUMNS, MONEY_COLUMNS

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = Path(__file__).resolve().parent
KAMPALA = ZoneInfo("Africa/Kampala")

pytestmark = pytest.mark.golden


# ---------------------------------------------------------------------------
# schema and money typing
# ---------------------------------------------------------------------------


def test_canonical_column_set_matches_exactly(golden: pl.DataFrame) -> None:
    assert tuple(golden.columns) == GOLDEN_COLUMNS


def test_money_columns_are_integers(golden: pl.DataFrame) -> None:
    for col in MONEY_COLUMNS:
        assert golden.schema[col] == pl.Int64, f"{col} is {golden.schema[col]}"
    for col in ("local_hour", "label_is_fraud", "label_is_flagged"):
        assert golden.schema[col] == pl.Int64
    assert (golden.select(MONEY_COLUMNS).min_horizontal() >= 0).all(), "negative money"


def test_money_cells_have_no_decimal_marks() -> None:
    with (GOLDEN_DIR / "transactions.csv").open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        money_idx = [GOLDEN_COLUMNS.index(c) for c in MONEY_COLUMNS]
        for row_no, record in enumerate(reader, start=2):
            for i in money_idx:
                cell = list(record.values())[i]
                assert re.fullmatch(r"-?\d+", cell), f"row {row_no}: money cell {cell!r}"


def test_no_float_literals_in_money_positions_of_generator() -> None:
    """DEV-005 at the source: the builder must not type money as float."""
    tree = ast.parse((GOLDEN_DIR / "build_fixture.py").read_text(encoding="utf-8"))
    offenders: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "event"
        ):
            amount = node.args[3] if len(node.args) > 3 else None
            if isinstance(amount, ast.Constant) and isinstance(amount.value, float):
                offenders.append(node.lineno)
            for kw in node.keywords:
                if (
                    ("amount" in kw.arg or "balance" in kw.arg)
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, float)
                ):
                    offenders.append(node.lineno)
    assert not offenders, f"float money literals at lines {offenders}"


# ---------------------------------------------------------------------------
# time columns
# ---------------------------------------------------------------------------


def test_local_hour_and_date_consistent_for_every_row(golden: pl.DataFrame) -> None:
    ts_ = golden["event_ts_utc"].to_list()
    assert len(ts_) == golden.height
    for i, (when, hour, date_local) in enumerate(
        zip(ts_, golden["local_hour"].to_list(), golden["event_date_local"].to_list(), strict=True)
    ):
        local = when.astimezone(KAMPALA)
        assert local.hour == hour, f"row {i}: local_hour {hour} != Kampala {local.hour}"
        assert local.strftime("%Y-%m-%d") == date_local, f"row {i}: event_date_local wrong"
        assert 0 <= hour <= 23


def test_timestamps_are_iso8601_utc_with_microseconds() -> None:
    with (GOLDEN_DIR / "transactions.csv").open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")
        for record in reader:
            assert pattern.match(record["event_ts_utc"]), record["event_ts_utc"]
        assert record["ingested_at"] == "2026-09-26T00:00:00.000000+00:00"


def test_event_dates_within_declared_window(golden: pl.DataFrame) -> None:
    lo, hi = golden["event_ts_utc"].min(), golden["event_ts_utc"].max()
    assert lo >= datetime(2024, 1, 1, tzinfo=ZoneInfo("UTC"))
    assert hi <= datetime(2024, 5, 1, tzinfo=ZoneInfo("UTC"))
    assert hi < datetime(2026, 9, 26, tzinfo=ZoneInfo("UTC")), "future vs ingested_at"


# ---------------------------------------------------------------------------
# ids, order, membership
# ---------------------------------------------------------------------------


def test_txn_ids_unique_namespaced_and_total_ordered(golden: pl.DataFrame) -> None:
    ids = golden["txn_id"].to_list()
    assert len(set(ids)) == len(ids)
    for i, tid in enumerate(ids, start=1):
        assert re.fullmatch(r"golden:\d{6}", tid), tid
        assert tid == f"golden:{i:06d}", "ids must be 1..N in file order"
    pairs = list(zip(golden["event_ts_utc"].to_list(), ids, strict=True))
    assert pairs == sorted(pairs), "file order must be the (event_ts_utc, txn_id) total order"


def test_single_corpus_identity(golden: pl.DataFrame) -> None:
    assert set(golden["source_dataset"]) == {"golden"}
    assert set(golden["run_id"]) == {build_fixture.RUN_ID}
    assert set(golden["batch_id"]) == {build_fixture.BATCH_ID}
    assert set(golden["currency"]) == {"EUR", "USD"}
    assert set(golden["txn_type"]) <= set(build_fixture.TXN_TYPES)
    assert set(golden["label_is_fraud"]) <= {0, 1}
    assert set(golden["label_is_flagged"]) <= {0, 1}


# ---------------------------------------------------------------------------
# planted structural singles (expected.yaml claims vs corpus reality)
# ---------------------------------------------------------------------------


def _ordinals_to_ids(ordinals: list[int]) -> set[str]:
    return {f"golden:{o:06d}" for o in ordinals}


def test_self_transfers_only_the_planted_case(golden: pl.DataFrame, expected: dict) -> None:
    got = set(golden.filter(pl.col("account_from") == pl.col("account_to"))["txn_id"])
    want = _ordinals_to_ids(expected["structural_inventory"]["self_transfer_rows"])
    assert got == want


def test_zero_amount_rows_are_exactly_declared(golden: pl.DataFrame, expected: dict) -> None:
    got = set(golden.filter(pl.col("amount_minor") == 0)["txn_id"])
    want = _ordinals_to_ids(expected["structural_inventory"]["zero_amount_rows"])
    assert got == want


def test_usd_rows_are_exactly_declared(golden: pl.DataFrame, expected: dict) -> None:
    got = set(golden.filter(pl.col("currency") == "USD")["txn_id"])
    want = _ordinals_to_ids(expected["structural_inventory"]["usd_rows"])
    assert got == want
    assert golden.filter(pl.col("currency") == "EUR").height == golden.height - 2


def test_reversal_rows_are_exactly_declared(golden: pl.DataFrame, expected: dict) -> None:
    got = set(golden.filter(pl.col("txn_type") == "REVERSAL")["txn_id"])
    want = _ordinals_to_ids(expected["structural_inventory"]["reversal_rows"])
    assert got == want


def test_balance_delta_inconsistency_is_exactly_the_planted_case(
    golden: pl.DataFrame, expected: dict
) -> None:
    ok = (
        pl.col("src_balance_after_minor")
        == pl.col("src_balance_before_minor") - pl.col("amount_minor")
    ) & (
        pl.col("dst_balance_after_minor")
        == pl.col("dst_balance_before_minor") + pl.col("amount_minor")
    )
    broken = set(golden.filter(~ok)["txn_id"])
    want = _ordinals_to_ids(expected["structural_inventory"]["balance_inconsistent_rows"])
    assert broken == want, "the balance-delta feature must be true on exactly S58's three rows"


# ---------------------------------------------------------------------------
# scenario bookkeeping: planted counts vs expected.yaml vs the built file
# ---------------------------------------------------------------------------


def test_total_planted_count_matches_expected_yaml(golden: pl.DataFrame, expected: dict) -> None:
    assert expected["counts"]["rows"] == golden.height
    assert expected["counts"]["scenarios"] == len(expected["scenarios"])
    accounts = set(golden["account_from"]) | set(golden["account_to"])
    assert expected["counts"]["distinct_accounts"] == len(accounts)
    assert expected["counts"]["usd_rows"] == golden.filter(pl.col("currency") == "USD").height


def test_every_row_belongs_to_exactly_one_scenario(
    build_manifest: dict, golden: pl.DataFrame
) -> None:
    owners: dict[int, list[str]] = defaultdict(list)
    for tag, ordinals in build_manifest["scenarios"].items():
        for o in ordinals:
            owners[o].append(tag)
    assert all(len(v) == 1 for v in owners.values()), "a row is in two scenarios"
    assert len(owners) == golden.height == build_manifest["rows"], "orphan or phantom rows"


def test_expected_yaml_scenario_rows_match_manifest(expected: dict, build_manifest: dict) -> None:
    for scenario in expected["scenarios"]:
        sid = scenario["id"]
        assert sid in build_manifest["scenarios"], f"{sid} missing from manifest"
        assert sorted(scenario["rows"]) == build_manifest["scenarios"][sid], sid


def test_fraud_labels_only_on_planted_positives(golden: pl.DataFrame, build_manifest: dict) -> None:
    positive = {
        "S01",
        "S02",
        "S03",
        "S07",
        "S11",
        "S12",
        "S16",
        "S17",
        "S19",
        "S20",
        "S28",
        "S33",
        "S37",
        "S40",
        "S41",
        "S44",
        "S50",
    }
    allowed: set[str] = set()
    for tag, ordinals in build_manifest["scenarios"].items():
        if tag.split("_", 1)[0] in positive:
            allowed |= {f"golden:{o:06d}" for o in ordinals}
    flagged = set(golden.filter(pl.col("label_is_fraud") == 1)["txn_id"])
    assert flagged <= allowed, "a near-miss or structural row carries a fraud label"


# ---------------------------------------------------------------------------
# arithmetic-relation invariants the plants DEPEND on (design-time claims,
# re-checked here so a future row edit cannot silently break an expectation)
# ---------------------------------------------------------------------------


def test_tau_bracket_holds(golden: pl.DataFrame) -> None:
    tau = golden["amount_minor"].quantile(0.25)
    assert 8250 < tau < 120_000, f"fixture p25 {tau} left the design bracket"


def test_no_noise_row_in_the_structuring_band(golden: pl.DataFrame, build_manifest: dict) -> None:
    smurf_rows = set()
    for tag in (
        "S33_r5_structuring_a",
        "S34_r5_structuring_b",
        "S35_r5_near_miss_count",
        "S36_r5_near_miss_window",
    ):
        smurf_rows |= {f"golden:{o:06d}" for o in build_manifest["scenarios"][tag]}
    t = 2_500_000
    band_low = t * 8 // 10
    in_band = golden.filter((pl.col("amount_minor") >= band_low) & (pl.col("amount_minor") <= t))[
        "txn_id"
    ].to_list()
    assert set(in_band) <= smurf_rows, "a foreign row landed inside 80-100% of T"


def test_rail_degree_gap_survives_the_p3a_percentile_formula(
    golden: pl.DataFrame, expected: dict
) -> None:
    """The committed P3a typing: is_rail = total_degree > sorted_deg[int(0.99*N)].

    Every account that must FIRE a fan or pattern rule has to sit at or below
    the threshold, and the declared supernode above it, under the current N.
    """
    edges = golden.filter(pl.col("account_from") != pl.col("account_to"))
    deg: dict[str, int] = defaultdict(int)
    for f, t in zip(edges["account_from"], edges["account_to"], strict=True):
        deg[f] += 1
        deg[t] += 1
    n_accounts = len(set(golden["account_from"]) | set(golden["account_to"]))
    pipeline_cfg = yaml.safe_load(
        (REPO_ROOT / "config" / "pipeline.yaml").read_text(encoding="utf-8")
    )
    pct = pipeline_cfg["graph"]["rail_degree_percentile"] / 100.0
    ordered = sorted(deg[a] for a in deg)
    threshold = ordered[min(max(int(pct * n_accounts), 0), len(ordered) - 1)]
    assert deg["ACC-RAIL-AGENT"] > threshold
    hit_accounts = {a for rule in expected["expected_hits"].values() for a in rule}
    for a in hit_accounts:
        assert deg[a] <= threshold, f"{a} would be typed rail (degree {deg[a]} > {threshold})"
    assert (
        n_accounts >= 101
    ), "below 101 accounts the p99 index selects the max and nothing is a rail"


def test_expected_hits_are_a_small_fraction_of_accounts(
    golden: pl.DataFrame, expected: dict
) -> None:
    n_accounts = len(set(golden["account_from"]) | set(golden["account_to"]))
    for rule, accounts in expected["expected_hits"].items():
        assert len(accounts) <= n_accounts / 3, f"{rule} above the 1/3 rejection trigger (01 SG)"


def test_every_referenced_account_exists(golden: pl.DataFrame, expected: dict) -> None:
    universe = set(golden["account_from"]) | set(golden["account_to"])
    referenced: set[str] = {a for accounts in expected["expected_hits"].values() for a in accounts}
    inventory = expected["structural_inventory"]
    for key in (
        "external_only_accounts",
        "singleton_accounts",
        "supernode_accounts",
        "second_high_degree",
    ):
        referenced |= set(inventory[key])
    for a in referenced:
        assert a in universe, a


def test_external_only_accounts_never_originate(golden: pl.DataFrame, expected: dict) -> None:
    for a in expected["structural_inventory"]["external_only_accounts"]:
        assert golden.filter(pl.col("account_from") == a).is_empty(), a


# ---------------------------------------------------------------------------
# config and determinism
# ---------------------------------------------------------------------------


def test_rule_params_mirror_has_not_drifted_from_config(expected: dict) -> None:
    cfg = yaml.safe_load((REPO_ROOT / "config" / "rules.yaml").read_text(encoding="utf-8"))
    by_id = {r["id"]: r["params"] for r in cfg["rules"]}
    for rule_id, mirror in expected["rule_params"].items():
        params = by_id[rule_id]
        for key, value in mirror.items():
            assert key in params, f"{rule_id}.{key} vanished from config/rules.yaml"
            assert params[key] == value, f"{rule_id}.{key}: config {params[key]} != mirror {value}"


def test_fixture_is_reproducible_byte_identical(build_manifest: dict) -> None:
    before = hashlib.sha256((GOLDEN_DIR / "transactions.csv").read_bytes()).hexdigest()
    assert before == build_manifest["sha256"]
    build_fixture.main()  # rewrites transactions.csv + manifest from seed 1337
    after = hashlib.sha256((GOLDEN_DIR / "transactions.csv").read_bytes()).hexdigest()
    assert after == before, "double build produced different bytes"


def test_seed_is_1337(build_manifest: dict) -> None:
    assert build_manifest["seed"] == 1337
