"""The P3b gate, asserted against the hand-computed golden matrix.

``tests/golden/expected.yaml`` was written before any rule code existed: 59 planted
scenarios with their arithmetic, an expected-hit matrix, severity orderings with their
justifications, and a list of accounts that must stay clean across all twelve rules. That
file is the authority this test reads, so the assertion is "does the engine agree with
the paper", never "does the engine agree with itself".

Scope note for the reader: the golden corpus is EUR apart from two declared USD rows, so
a currency-crossing case is out of scope here and is covered with hand-built frames in
``test_p3b_rules.py``. ``tests/golden/`` itself is owned by another workstream and is
never written to by anything in this file.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from oxbow.config import load_pipeline_config
from oxbow.graph import build_graph
from oxbow.rules import evaluate_rules, load_rules_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "tests" / "golden") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests" / "golden"))

from loader import load_golden_transactions  # noqa: E402  (path bootstrap precedes import)

RULE_IDS = [f"R{index}" for index in range(1, 13)]
# The four accounts of the weekly payroll ring. Derived from the fixture's own
# cycle_expectations stanza rather than restated here, so this file has exactly one
# source for what the corpus plants.
PERIODIC_NAME = "PAYROLL"


def matrix_of(expected: dict, rule_id: str) -> set[str]:
    """One rule's declared hit accounts, with the fixture's annotations stripped."""
    declared: set[str] = set()
    for entry in _flatten_expected(expected["expected_hits"][rule_id]):
        account = entry.split("@")[-1].strip()
        for part in account.replace(")", "").split(","):
            token = part.strip()
            if token.startswith("ACC-"):
                declared.add(token)
    return declared


def periodic_accounts(expected: dict) -> set[str]:
    for cycle in expected["cycle_expectations"]:
        if cycle["name"] == PERIODIC_NAME:
            return {str(node) for node in cycle["nodes"]}
    raise AssertionError("the fixture no longer declares the payroll ring")


@pytest.fixture(scope="module")
def golden_events() -> pl.DataFrame:
    return load_golden_transactions()


@pytest.fixture(scope="module")
def golden_expected() -> dict[str, Any]:
    import yaml

    document = yaml.safe_load((REPO_ROOT / "tests" / "golden" / "expected.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


@pytest.fixture(scope="module")
def golden_result(golden_events: pl.DataFrame) -> Any:
    pipeline = load_pipeline_config(REPO_ROOT)
    graph = build_graph(golden_events, pipeline)
    return evaluate_rules(golden_events, graph, load_rules_settings(REPO_ROOT))


def accounts_of(result: Any, rule_id: str) -> set[str]:
    return {hit.account_key for hit in result.hits if hit.rule_id == rule_id}


def severity_of(result: Any, rule_id: str, account: str) -> float:
    hits = [hit for hit in result.hits if hit.rule_id == rule_id and hit.account_key == account]
    assert hits, f"{rule_id} did not fire on {account}"
    return max(hit.severity for hit in hits)


def test_every_rule_fires_on_exactly_its_planted_case(golden_result: Any, golden_expected: dict) -> None:
    for rule_id in RULE_IDS:
        assert accounts_of(golden_result, rule_id) == matrix_of(golden_expected, rule_id), rule_id


def test_no_planted_near_miss_fires_any_rule(golden_result: Any, golden_expected: dict) -> None:
    """The other half of the gate: silence on every near-miss, not just absence of extras."""
    allowed = {account for rule_id in RULE_IDS for account in matrix_of(golden_expected, rule_id)}
    fired = {hit.account_key for hit in golden_result.hits}
    assert fired <= allowed, f"accounts on the must-stay-clean list fired: {sorted(fired - allowed)}"
    # the matrix says every account outside the hit list is clean, so the count is pinned
    assert golden_expected["counts"]["distinct_accounts"] == 319
    assert len(fired) == len(allowed)


@pytest.mark.parametrize(
    ("rule_id", "expected_order"),
    [
        ("R1", ["ACC-MULE-01", "ACC-MULE-07", "ACC-MULE-02"]),
        ("R2", ["ACC-GATHER-BIG", "ACC-GATHER-01"]),
        ("R3", ["ACC-SCATTER-BIG", "ACC-SCATTER-01"]),
        ("R5", ["ACC-SMURF-A", "ACC-SMURF-B"]),
        ("R7", ["ACC-SLEEP-B", "ACC-SLEEP-01"]),
        ("R10", ["ACC-MULE-07", "ACC-CASH-01"]),
    ],
)
def test_severity_keeps_two_accounts_rankable(
    golden_result: Any, rule_id: str, expected_order: list[str]
) -> None:
    # Hand-derived from the config's saturating functions, e.g. R1: retained share
    # 0.92 > 0.88 > 0.85, each normalised as (share - 0.80) / (1 - 0.80).
    scored = [severity_of(golden_result, rule_id, account) for account in expected_order]
    assert scored == sorted(scored, reverse=True), rule_id
    assert all(first >= second for first, second in zip(scored, scored[1:], strict=False))


def test_declared_severity_tie_is_a_tie_not_an_ordering_bug(golden_result: Any) -> None:
    # VEL-01 and VEL-B1 observe the same 27 paired events from opposite ends, so equal
    # severity is the expected result rather than a broken comparison.
    assert severity_of(golden_result, "R6", "ACC-VEL-01") == pytest.approx(
        severity_of(golden_result, "R6", "ACC-VEL-B1")
    )


def test_periodic_cycle_is_down_weighted_below_the_one_shot_loops(
    golden_result: Any, golden_expected: dict
) -> None:
    payroll = min(
        severity_of(golden_result, "R4", account) for account in periodic_accounts(golden_expected)
    )
    one_shot = max(
        severity_of(golden_result, "R4", account)
        for account in ("ACC-CYCA-1", "ACC-CYCB-1")
    )
    assert payroll < one_shot, "the weekly ring outranks a real loop: the weight did not apply"
    hit = next(
        hit for hit in golden_result.hits if hit.rule_id == "R4" and hit.account_key == "ACC-EMP-01"
    )
    assert hit.evidence["down_weighted"] is True
    assert hit.evidence["recurrence_period_hours"] == pytest.approx(168.0, abs=1.0)
    # three laps, one pattern: signature dedup rather than three hits per account
    assert len([h for h in golden_result.hits if h.rule_id == "R4" and h.account_key == "ACC-EMP-01"]) == 1


def test_extraction_pair_collapses_into_one_unit_of_evidence(golden_result: Any) -> None:
    # ACC-MULE-07 fires R1 (0.88 passed on in 35 min) and R10 (0.88 cashed out in 6h) on
    # the same receive-then-extract pair: one extraction group, counted once.
    groups = [group for group in golden_result.evidence_groups if group.account_key == "ACC-MULE-07"]
    assert [group.group for group in groups] == ["extraction"]
    assert groups[0].rule_ids == ("R1", "R10")
    assert golden_result.evidence_units("ACC-MULE-07") == 1


def test_aggregation_pair_collapses_and_fan_out_does_not(golden_result: Any) -> None:
    # R2 and R11 describe one gather: one aggregation unit. R3 is in no group at all, so
    # a scatter keeps two separate units - the grouping is config-driven, not a dedupe.
    assert golden_result.evidence_units("ACC-GATHER-01") == 1
    assert golden_result.evidence_units("ACC-SCATTER-01") == 2


def test_cycle_near_misses_are_recorded_with_reasons_not_silence(golden_result: Any) -> None:
    """DEV-015's mechanism, on the fixture's own near-miss loops."""
    by_pattern = {frozenset(miss.pattern): miss for miss in golden_result.near_misses}
    shrinking = frozenset(f"ACC-CYCN-{i}" for i in range(1, 4))
    seven_nodes = frozenset(f"ACC-CYCL7-{i}" for i in range(1, 8))
    assert shrinking in by_pattern, sorted(by_pattern)
    assert by_pattern[shrinking].reasons == ("value_retention_below_floor",)
    assert seven_nodes in by_pattern
    assert by_pattern[seven_nodes].reasons == ("length_above_max_length",)
    # and neither of them scored
    assert accounts_of(golden_result, "R4").isdisjoint(shrinking | seven_nodes)


def test_time_reversed_and_reversal_loops_produce_nothing_at_all(golden_result: Any) -> None:
    # S23 is the gate's own clause: A->B 12:00, B->C 11:00, C->A 10:00 fails from every
    # rotation, so neither a hit nor a near-miss may exist for those accounts.
    reversed_ring = {"ACC-CYCT-1", "ACC-CYCT-2", "ACC-CYCT-3"}
    reversal_ring = {"ACC-RV-P", "ACC-RV-Q", "ACC-RV-R"}
    fired = {hit.account_key for hit in golden_result.hits}
    assert fired.isdisjoint(reversed_ring | reversal_ring)
    patterns = [frozenset(miss.pattern) for miss in golden_result.near_misses]
    assert reversed_ring not in patterns
    assert reversal_ring not in patterns
    assert golden_result.reversal_excluded == 2
    assert golden_result.self_transfer_count == 1


def test_structuring_hit_names_its_synthetic_threshold_and_fit_window(golden_result: Any) -> None:
    hit = next(hit for hit in golden_result.hits if hit.rule_id == "R5" and hit.account_key == "ACC-SMURF-A")
    assert hit.evidence["structuring_threshold_minor"] == 2_500_000
    assert hit.evidence["threshold_is_synthetic"] is True
    assert "SYNTHETIC" in str(hit.evidence["threshold_label"])
    fit = next(fit for fit in golden_result.thresholds if fit.name == "tau_minor")
    # golden pins the corpus p25 at 22000 minor and brackets it 8250 < tau < 120000
    assert fit.value == 22_000
    assert fit.sample_size == 532  # 534 rows less the two zero-value probes
    assert fit.window.label == "run"


def test_hit_rate_report_is_complete_and_under_the_ceiling(golden_result: Any) -> None:
    assert [row.rule_id for row in golden_result.hit_rates] == RULE_IDS
    assert all(row.hit_rate <= 0.33 for row in golden_result.hit_rates), golden_result.hit_rate_table()
    assert golden_result.scored_accounts == 319
    assert all(row.accounts_hit > 0 for row in golden_result.hit_rates), golden_result.hit_rate_table()
    assert golden_result.removed_rules == ()


def test_parallel_edges_are_forty_transfers_not_one(golden_result: Any) -> None:
    # S53: 40 transfers of 30000 in 120 minutes. No rule may fire on it, and the count
    # has to survive as 40: a burst of 40 is not one transfer of 1.2M.
    fired = {hit.account_key for hit in golden_result.hits}
    assert fired.isdisjoint({"ACC-BURST-A", "ACC-BURST-B"})
    graph = build_graph(load_golden_transactions(), load_pipeline_config(REPO_ROOT))
    totals = graph.pair_totals("ACC-BURST-A", "ACC-BURST-B")
    assert totals == {"EUR": 1_200_000}
    assert graph.stats.event_count == 534


def test_search_budgets_were_not_exhausted_on_the_golden_corpus(golden_result: Any) -> None:
    assert golden_result.cycle_search_truncated is False
    assert golden_result.chain_search_truncated is False


def test_settings_layer_refuses_a_missing_knob_rather_than_defaulting(tmp_path: Path) -> None:
    from oxbow.rules.errors import RuleConfigError

    source = (REPO_ROOT / "config" / "rules.yaml").read_text(encoding="utf-8")
    stripped = source.replace("params: { p: 0.80, delta_minutes: 60, min_amount_minor: 10_000 }", "params: {}")
    assert stripped != source
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "rules.yaml").write_text(stripped, encoding="utf-8")
    with pytest.raises(RuleConfigError, match="R1.params.p is missing"):
        load_rules_settings(tmp_path)


def test_rule_settings_defaults_are_what_plan_9_pins() -> None:
    """The §9 stanzas themselves, asserted so a config edit cannot silently redefine a typology."""
    loaded = load_rules_settings(REPO_ROOT)
    one, four, five, twelve = (loaded.settings_for(rule) for rule in ("R1", "R4", "R5", "R12"))
    assert (one.p, one.delta_minutes, one.min_amount_minor) == (0.80, 60, 10_000)
    # DEV-015: the §9 retention floor and 3-6 length stay the shipped defaults
    assert (four.retention, four.min_length, four.max_length) == (0.60, 3, 6)
    assert (four.down_weight_periodic, four.periodic_period_hours, four.periodic_down_weight) == (
        True, 168, 0.3,
    )
    assert (five.n, five.window_days, five.threshold_band_low, five.threshold_band_high) == (
        3, 7, 0.80, 1.00,
    )
    assert five.synthetic is True
    assert (twelve.min_length, twelve.delta_hours, twelve.decay, twelve.component_budget) == (
        3, 24, 0.95, 5000,
    )


def test_result_serialises_for_scoring_and_backtest(golden_result: Any) -> None:
    payload = golden_result.as_json_dict()
    assert set(payload) >= {
        "hits", "hit_rates", "evidence_groups", "near_misses", "removed_rules",
        "thresholds", "window", "fit_window", "cycle_search_truncated",
        "cycle_search_reason", "cycle_search_visits", "chain_search_truncated",
        "chain_search_reason", "self_transfer_count", "zero_value_excluded",
        "reversal_excluded", "currencies", "scored_accounts", "settings_fingerprint",
        "local_offset_hours",
    }
    hit = payload["hits"][0]
    assert set(hit) == {
        "rule_id", "rule_name", "account_key", "severity", "evidence", "window",
        "hit_signature", "overlap_group",
    }
    assert json_roundtrip(payload) == json_roundtrip(
        dataclasses.replace(golden_result).as_json_dict()
    )


def json_roundtrip(payload: object) -> str:
    import json

    return json.dumps(payload, sort_keys=True, default=str)


def _flatten_expected(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise AssertionError(f"unexpected expected_hits shape: {value!r}")
