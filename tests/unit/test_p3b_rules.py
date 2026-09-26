"""P3b rule-engine tests: each rule, its planted case, and its near-misses.

Every expected number in this file was computed on paper from the definitions in
``config/rules.yaml`` before the code was run against it, and the arithmetic is written
into the comment above each assertion. Where the golden fixture under
``tests/golden/`` already carries a hand-computed matrix, that matrix is asserted
separately in ``test_p3b_golden_matrix.py``; the cases here are smaller, exist to pin one
mechanism each, and are the ones that can be read in one screenful.

Two fixture-level choices are worth stating because they are not arbitrary:

* **No rails.** The rail threshold in ``config/pipeline.yaml`` is a 99th percentile over
  whatever corpus is loaded, so in a twenty-account fixture the busiest actor would be
  typed ``rail`` and excluded from the fan and graph rules for a reason that has nothing
  to do with the rule being tested. The helper raises the percentile out of range and
  says so, rather than padding fixtures with decoy accounts to move a percentile.
* **The hit-rate ceiling is off in the shared helper.** Several fixtures deliberately
  concentrate hits on a small population, which is exactly what the ceiling exists to
  catch; it gets its own test (``test_hit_rate_ceiling_fails_the_run_with_a_suggestion``)
  where that failure is the thing under test rather than an obstruction.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from oxbow.config import load_pipeline_config
from oxbow.contracts.canonical_v1 import CANONICAL_DTYPES
from oxbow.graph import build_graph
from oxbow.rules import (
    RULE_REGISTRY,
    HitRateCeilingError,
    RuleHit,
    RuleResult,
    Window,
    evaluate_rules,
    load_rules_settings,
)
from oxbow.rules.events import ratio_of
from oxbow.rules.hits import deduplicate_by_signature
from oxbow.rules.settings import RulesSettings
from oxbow.rules.severity import log_band_severity

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = dt.UTC
# Deployment zone per config/pipeline.yaml (Africa/Kampala, UTC+3, no DST). Fixtures
# state UTC instants and derive the local hour from this offset, so a rule that read the
# UTC hour would be visible in the results rather than hidden by an accident of input.
LOCAL_OFFSET_HOURS = 3
# Midnight, so instant(day, hour) means what it reads as. An offset base would silently
# shift every fixture hour by that offset and the odd-hour tests would be testing the
# arithmetic of the helper rather than the rule.
BASE = dt.datetime(2024, 3, 1, 0, 0, tzinfo=UTC)
HOUR = dt.timedelta(hours=1)
DAY = dt.timedelta(days=1)


def instant(day: int, hour: int = 0, minute: int = 0) -> dt.datetime:
    return BASE + dt.timedelta(days=day, hours=hour, minutes=minute)


@dataclasses.dataclass(frozen=True, slots=True)
class Row:
    """One planted transaction, in the shape the rules read."""

    txn_id: str
    at: dt.datetime
    amount: int
    src: str
    dst: str
    txn_type: str = "TRANSFER"
    currency: str = "EUR"
    channel: str = "APP"


def frame(rows: Iterable[Row], *, offset_hours: int = LOCAL_OFFSET_HOURS) -> pl.DataFrame:
    """A canonical event frame, with the local columns derived from one declared offset.

    ``offset_hours`` exists because the UTC-trap test needs two views of identical
    instants; the derivation is done here and nowhere else so no fixture can accidentally
    state a local hour that disagrees with its own timestamp.
    """
    ordered = sorted(rows, key=lambda row: (row.at, row.txn_id))
    shifted = [row.at + dt.timedelta(hours=offset_hours) for row in ordered]
    return pl.DataFrame(
        {
            "txn_id": [row.txn_id for row in ordered],
            "event_ts_utc": pl.Series([row.at for row in ordered], dtype=pl.Datetime("us", "UTC")),
            "event_date_local": [moment.date().isoformat() for moment in shifted],
            "local_hour": pl.Series(
                [moment.hour for moment in shifted], dtype=CANONICAL_DTYPES["local_hour"]
            ),
            "txn_type": [row.txn_type for row in ordered],
            "channel": [row.channel for row in ordered],
            "amount_minor": pl.Series([row.amount for row in ordered], dtype=pl.Int64),
            "currency": [row.currency for row in ordered],
            "account_from": [row.src for row in ordered],
            "account_to": [row.dst for row in ordered],
        }
    )


def settings(*, rule: str, **overrides: Any) -> RulesSettings:
    """The real config with one rule's resolved parameters replaced.

    Used where a test needs to prove a threshold is *config-sourced*: the value is
    swapped in the loaded settings, and the rule's behaviour moves with it. Renaming a key
    is not possible this way, which is part of the point.
    """
    loaded = load_rules_settings(REPO_ROOT)
    current = loaded.settings_for(rule)
    merged = {**dataclasses.asdict(current), **overrides}
    updated = dataclasses.replace(current, **merged)
    everything = dict(loaded.settings)
    everything[rule] = updated
    return dataclasses.replace(loaded, settings=everything)


def evaluate(
    events: pl.DataFrame,
    *,
    cfg: RulesSettings | None = None,
    fit_window: Window | None = None,
) -> RuleResult:
    """Build the graph and score all twelve rules over ``events``."""
    pipeline = load_pipeline_config(REPO_ROOT)
    # Rails out of range: see the module docstring. 99.9 of a twenty-node population
    # resolves above every degree present, so nothing is typed rail by accident.
    pipeline = dataclasses.replace(
        pipeline,
        raw={
            **pipeline.raw,
            "graph": {**pipeline.raw["graph"], "rail_degree_percentile": 99.9},
        },
    )
    graph = build_graph(events, pipeline)
    return evaluate_rules(
        events,
        graph,
        cfg or load_rules_settings(REPO_ROOT),
        fit_window=fit_window,
        enforce_hit_rate_ceiling=False,
    )


def accounts(result: RuleResult, rule_id: str) -> set[str]:
    return {hit.account_key for hit in result.hits if hit.rule_id == rule_id}


def only(result: RuleResult, rule_id: str, account: str) -> RuleHit:
    hits = [hit for hit in result.hits if hit.rule_id == rule_id and hit.account_key == account]
    assert len(hits) == 1, f"expected exactly one {rule_id} hit on {account}, got {len(hits)}"
    return hits[0]


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def test_registry_covers_the_config_exactly() -> None:
    loaded = load_rules_settings(REPO_ROOT)
    assert set(RULE_REGISTRY) == set(loaded.specs)
    assert [loaded.spec(rid).name for rid in sorted(RULE_REGISTRY, key=lambda r: int(r[1:]))] == [
        "RAPID_PASS_THROUGH",
        "FAN_IN",
        "FAN_OUT",
        "CYCLE_MEMBER",
        "STRUCTURING",
        "VELOCITY_SPIKE",
        "DORMANT_REACTIVATION",
        "ODD_HOUR_SHIFT",
        "AMOUNT_REGIME_SHIFT",
        "FAST_CASH_OUT",
        "NEW_COUNTERPARTY_SURGE",
        "CHAIN_MEMBER",
    ]


def test_rules_are_not_allowed_to_import_adapters_or_http() -> None:
    """Contract 1 is enforced by lint-imports; this names the forbidden modules locally.

    The value is that a developer running only the unit tests still learns which imports
    the architecture forbids, without needing the contract runner installed.
    """
    import oxbow.rules

    package_dir = Path(oxbow.rules.__file__).parent
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(package_dir.glob("*.py")))
    for forbidden in ("oxbow.adapters", "httpx", "requests", "boto3", "urllib3", "aiohttp"):
        assert forbidden not in text, forbidden
    # Money is Int64 end to end. The layer converts to float only for severity, which is
    # a rank; `scripts/no_float_money.py` owns the annotation audit, so this asserts the
    # one thing it cannot: that no amount column is ever cast on the way through.
    assert "amount_minor" in text
    assert "cast(pl.Float" not in text and "Float64" not in text


# ---------------------------------------------------------------------------
# R1 rapid pass-through
# ---------------------------------------------------------------------------


def test_r1_fires_on_planted_pair_and_ranks_by_retained_share() -> None:
    # receives 500000, sends 460000 40 minutes later.
    # ratio = 460000/500000 = 0.92 >= p 0.80 -> fires.
    # severity = (0.92 - 0.80) / (1 - 0.80) = 0.12 / 0.20 = 0.60
    # second mule: 1000000 -> 850000 in 25 min: ratio 0.85, severity 0.05/0.20 = 0.25
    result = evaluate(
        frame(
            [
                Row("t1", instant(0), 500_000, "ACC-FUND", "ACC-MULE-A"),
                Row("t2", instant(0, 0, 40), 460_000, "ACC-MULE-A", "ACC-PAY"),
                Row("t3", instant(1), 1_000_000, "ACC-FUND", "ACC-MULE-B"),
                Row("t4", instant(1, 0, 25), 850_000, "ACC-MULE-B", "ACC-PAY"),
            ]
        )
    )
    assert accounts(result, "R1") == {"ACC-MULE-A", "ACC-MULE-B"}
    assert only(result, "R1", "ACC-MULE-A").severity == pytest.approx(0.60, abs=1e-6)
    assert only(result, "R1", "ACC-MULE-B").severity == pytest.approx(0.25, abs=1e-6)
    # the mule ranks above the payer and the payee, which never handled a receipt
    assert "ACC-FUND" not in accounts(result, "R1")
    assert "ACC-PAY" not in accounts(result, "R1")


def test_r1_near_misses_ratio_amount_and_window() -> None:
    # (a) 316000/400000 = 0.79 < 0.80  -> silent
    # (b) 8500/9000  = 0.9444 >= 0.80 but the receipt is below min_amount_minor 10000
    # (c) 570000/600000 = 0.95 but the send lands 61 minutes after the receipt > delta 60
    result = evaluate(
        frame(
            [
                Row("a1", instant(0), 400_000, "ACC-W1", "ACC-NM-RATIO"),
                Row("a2", instant(0, 0, 30), 316_000, "ACC-NM-RATIO", "ACC-OUT"),
                Row("b1", instant(1), 9_000, "ACC-W2", "ACC-NM-MIN"),
                Row("b2", instant(1, 0, 20), 8_500, "ACC-NM-MIN", "ACC-OUT"),
                Row("c1", instant(2), 600_000, "ACC-W3", "ACC-NM-WINDOW"),
                Row("c2", instant(2, 1, 1), 570_000, "ACC-NM-WINDOW", "ACC-OUT"),
            ]
        )
    )
    assert accounts(result, "R1") == set()


def test_r1_does_not_pair_across_currencies() -> None:
    # EUR: 200000/500000 = 0.40 -> no.  USD: 450000/700000 = 0.643 -> no.
    # Currency-blind pairing would compute 450000/500000 = 0.90 and fire; that is the
    # implicit-FX error 01 B and section 8 forbid.
    result = evaluate(
        frame(
            [
                Row("x1", instant(0), 500_000, "ACC-IN", "ACC-XCUR", currency="EUR"),
                Row("x2", instant(0, 0, 5), 700_000, "ACC-IN", "ACC-XCUR", currency="USD"),
                Row("x3", instant(0, 0, 20), 450_000, "ACC-XCUR", "ACC-OUT", currency="USD"),
                Row("x4", instant(0, 0, 25), 200_000, "ACC-XCUR", "ACC-OUT", currency="EUR"),
            ]
        )
    )
    assert accounts(result, "R1") == set()


# ---------------------------------------------------------------------------
# R2 / R3 fan in and out
# ---------------------------------------------------------------------------


# Background block: 30 large-value transfers, so the corpus p25 that R2's tau is fitted
# from lands at 1_000_000 rather than among the nine gatherer amounts. Amounts sorted:
# 18 rows at 1000 (two gatherers x nine senders), then the large rows; n = 30 + 18 + 2,
# nearest-rank index int(0.25 * 50) = 12 -> the 13th smallest value = 1_000_000.
def _background(count: int = 30, amount: int = 1_000_000) -> list[Row]:
    return [
        Row(f"bg{i}", instant(0, i % 12, i), amount, f"ACC-BGS{i}", f"ACC-BGD{i}")
        for i in range(count)
    ]


def test_r2_fires_on_many_small_senders_and_respects_the_tau_condition() -> None:
    # nine distinct senders within one hour, k = 8 satisfied.
    # inbound amounts 1000 x 9 -> median 1000; tau (corpus p25) = 1_000_000 -> 1000 < tau -> fires.
    # severity = (9 - 8) / (2*8 - 8) = 1/8 = 0.125
    # whale gatherer: nine senders of 5_000_000 -> median 5_000_000 >= tau -> k passes, tau refuses.
    rows = (
        _background()
        + [
            Row(f"s{i}", instant(6, i // 6, (i * 7) % 60), 1_000, f"ACC-POOL{i}", "ACC-GATHER")
            for i in range(9)
        ]
        # both gatherers originate one payment, or the graph types them `external` and
        # the tau case would be decided by the external exclusion instead of by tau
        + [Row("pay", instant(6, 23), 3_000, "ACC-GATHER", "ACC-PAYEE")]
        + [
            Row(
                f"w{i}",
                instant(7, i // 6, (i * 11) % 60),
                5_000_000,
                f"ACC-WHALE{i}",
                "ACC-GATHER-W",
            )
            for i in range(9)
        ]
        + [Row("payw", instant(7, 23), 3_000, "ACC-GATHER-W", "ACC-PAYEE2")]
    )
    result = evaluate(frame(rows))
    assert accounts(result, "R2") == {"ACC-GATHER"}
    hit = only(result, "R2", "ACC-GATHER")
    assert hit.severity == pytest.approx(0.125, abs=1e-6)
    assert hit.evidence["median_inbound_amount_minor"] == 1_000
    assert hit.evidence["tau_minor"] == 1_000_000
    assert result.thresholds[0].value == 1_000_000
    # the tau condition is the second gate doing real work, not a formality
    assert "ACC-GATHER-W" not in accounts(result, "R2")


def test_r2_window_is_sliding_not_calendar() -> None:
    # nine senders, but arrivals span 26 hours: any 24-hour window holds seven.
    # k = 8 -> must stay silent. Calendar day buckets would report 2+5+2 and could fire.
    rows = _background() + [
        Row(
            f"v{i}",
            instant(9) + dt.timedelta(hours=[0, 0, 13, 13, 13, 13, 13, 26, 26][i]),
            1_000,
            f"ACC-V{i}",
            "ACC-GATHER-V",
        )
        for i in range(9)
    ]
    assert accounts(evaluate(frame(rows)), "R2") == set()


def test_r2_and_r3_exclude_self_transfers_and_count_them() -> None:
    # one account moving money to itself 45000 times the fan threshold: not a gather,
    # not a scatter, and the graph still has to report the self-transfer it dropped.
    rows = [
        Row(f"self{i}", instant(0, i // 60, i % 60), 45_000, "ACC-SELF", "ACC-SELF")
        for i in range(12)
    ]
    result = evaluate(frame(rows))
    assert result.self_transfer_count == 12
    assert accounts(result, "R2") == set()
    assert accounts(result, "R3") == set()
    assert accounts(result, "R4") == set()


def test_r3_fires_on_nine_receivers_and_stops_at_seven() -> None:
    # scatter: 9 distinct receivers inside one hour -> k = 8 satisfied.
    # severity = (9 - 8) / (16 - 8) = 0.125
    # decoy: 7 distinct receivers -> silent even though the volume is identical.
    result = evaluate(
        frame(
            [
                Row(f"o{i}", instant(0, 0, i * 5), 30_000, "ACC-SCATTER", f"ACC-RECV{i}")
                for i in range(9)
            ]
            + [
                Row(f"d{i}", instant(1, 0, i * 5), 30_000, "ACC-SCATTER-N", f"ACC-DEC{i}")
                for i in range(7)
            ]
        )
    )
    assert accounts(result, "R3") == {"ACC-SCATTER"}
    assert only(result, "R3", "ACC-SCATTER").severity == pytest.approx(0.125, abs=1e-6)


def test_rails_are_excluded_from_fan_rules_by_node_type() -> None:
    # The hub must satisfy two percentile constraints at once, which is why the fixture is
    # bigger than the arithmetic looks. Rail typing is total_degree > the nearest-rank p99
    # of the degree population, so with one busy node the threshold IS its own degree and
    # nothing is a rail: the corpus needs ~1% of nodes at the top. And tau is the p25 of
    # the amount population, so the hub's small inbound amounts have to stay under a
    # quarter of it or the median test would fail for the wrong reason. 130 background
    # pairs give 260 degree-1 nodes (p99 -> 1, hub 42 -> rail) and put tau at 2_000_000.
    gathered = [
        Row(f"g{i}", instant(0, 0, i), 500, f"ACC-POOL{i}", "ACC-HUB") for i in range(40)
    ] + [Row(f"p{i}", instant(1, 0, i), 500, "ACC-HUB", f"ACC-PAY{i}") for i in range(2)]
    background = [
        Row(f"b{i}", instant(2, 0, i % 60), 2_000_000, f"ACC-Q{i}", f"ACC-Z{i}") for i in range(130)
    ]
    events = frame(gathered + background)
    pipeline = load_pipeline_config(REPO_ROOT)
    graph = build_graph(events, pipeline)
    assert graph.stats.rail_degree_threshold == 1, graph.stats.degree_all_nodes
    assert graph.node_types["ACC-HUB"] == "rail"
    rail_scored = evaluate_rules(
        events, graph, load_rules_settings(REPO_ROOT), enforce_hit_rate_ceiling=False
    )
    assert "ACC-HUB" not in accounts(rail_scored, "R2")
    assert rail_scored.thresholds[0].value == 2_000_000

    # the same corpus with the rail typing out of range: only the exclusion changed, so
    # the silence above is the exclusion and not an incidental arithmetic miss
    assert accounts(evaluate(events), "R2") == {"ACC-HUB"}


# ---------------------------------------------------------------------------
# R4 cycle member
# ---------------------------------------------------------------------------


def test_r4_fires_on_a_three_hop_time_respecting_loop() -> None:
    # A->B 100000, B->C 100000, C->A 90000, times strictly increasing.
    # retention = min/max = 90000/100000 = 0.90 >= 0.60 -> fires on all three.
    # severity = mean( (0.90-0.60)/(1-0.60), (3-3)/(6-3) ) = mean(0.75, 0.0) = 0.375
    result = evaluate(
        frame(
            [
                Row("c1", instant(0), 100_000, "ACC-A", "ACC-B"),
                Row("c2", instant(0, 1), 100_000, "ACC-B", "ACC-C"),
                Row("c3", instant(0, 2), 90_000, "ACC-C", "ACC-A"),
            ]
        )
    )
    assert accounts(result, "R4") == {"ACC-A", "ACC-B", "ACC-C"}
    assert only(result, "R4", "ACC-A").severity == pytest.approx(0.375, abs=1e-6)


def test_r4_time_reversed_loop_returns_nothing() -> None:
    # A->B 12:00, B->C 11:00, C->A 10:00. From every start the walk must move backwards:
    # A(12:00)->B, B's only leg is 11:00 < 12:00 -> dead; B(11:00)->C, C->A is 10:00 < 11:00
    # -> dead; C(10:00)->A(12:00), A's next is 12:00 -> B, then 11:00 < 12:00 -> dead.
    # Nothing is returned, so nothing can be down-weighted or mis-reported as a near-miss.
    result = evaluate(
        frame(
            [
                Row("r1", instant(0, 12), 100_000, "ACC-A", "ACC-B"),
                Row("r2", instant(0, 11), 100_000, "ACC-B", "ACC-C"),
                Row("r3", instant(0, 10), 90_000, "ACC-C", "ACC-A"),
            ]
        )
    )
    assert accounts(result, "R4") == set()
    assert result.near_misses == ()


def test_r4_near_miss_retention_is_reported_with_its_reason() -> None:
    # 100000 -> 60000 -> 59000: retention 59000/100000 = 0.59 < 0.60 -> no hit, and the
    # loop is not silently dropped: the ledger names the setting that refused it.
    result = evaluate(
        frame(
            [
                Row("n1", instant(0), 100_000, "ACC-A", "ACC-B"),
                Row("n2", instant(0, 1), 60_000, "ACC-B", "ACC-C"),
                Row("n3", instant(0, 2), 59_000, "ACC-C", "ACC-A"),
            ]
        )
    )
    assert accounts(result, "R4") == set()
    assert [miss.reasons for miss in result.near_misses] == [("value_retention_below_floor",)]
    assert result.near_misses[0].pattern == ("ACC-A", "ACC-B", "ACC-C")


def test_r4_seven_node_loop_is_excluded_by_length_only() -> None:
    # amounts flat, times increasing, seven distinct nodes > max_length 6.
    # Near-miss reasons must name length and nothing else.
    nodes = [f"ACC-N{i}" for i in range(7)]
    rows = [Row(f"l{i}", instant(0, i), 100_000, nodes[i], nodes[(i + 1) % 7]) for i in range(7)]
    result = evaluate(frame(rows))
    assert accounts(result, "R4") == set()
    assert [miss.reasons for miss in result.near_misses] == [("length_above_max_length",)]


def test_r4_reversal_is_not_a_cycle() -> None:
    # P->Q 08:00, Q->R 09:30, R->P 11:00 typed REVERSAL. Read as a transfer the ring
    # closes at retention 1.00; a reversal is not a hop, so R4 and R12 must both be quiet.
    result = evaluate(
        frame(
            [
                Row("p1", instant(0, 8), 600_000, "ACC-P", "ACC-Q"),
                Row("p2", instant(0, 9, 30), 600_000, "ACC-Q", "ACC-R"),
                Row("p3", instant(0, 11), 600_000, "ACC-R", "ACC-P", txn_type="REVERSAL"),
            ]
        )
    )
    assert accounts(result, "R4") == set()
    assert accounts(result, "R12") == set()
    assert result.reversal_excluded == 1


def test_r4_periodic_payroll_ring_is_down_weighted_not_suppressed() -> None:
    # The same four-account ring closing weekly: lap starts 0, 7 and 14 days apart,
    # period_hours = 168 so 7 days = 168h exactly.
    # Per lap: 800000 -> 800000 -> 800000 -> 786000; retention 786000/800000 = 0.98250.
    # severity before the weight = mean((0.9825-0.6)/0.4, (4-3)/(6-3)) = mean(0.95625, 0.3333)
    #                            = 0.64479
    # after the 0.3 down-weight      = 0.19344
    rows: list[Row] = []
    for lap in range(3):
        start = instant(7 * lap, 8)
        rows += [
            Row(f"w{lap}1", start, 800_000, "ACC-EMP", "ACC-MERCH"),
            Row(f"w{lap}2", start + HOUR, 800_000, "ACC-MERCH", "ACC-SUPP"),
            Row(f"w{lap}3", start + 2 * HOUR, 800_000, "ACC-SUPP", "ACC-PAY"),
            Row(f"w{lap}4", start + 3 * HOUR, 786_000, "ACC-PAY", "ACC-EMP"),
        ]
    result = evaluate(frame(rows))
    assert accounts(result, "R4") == {"ACC-EMP", "ACC-MERCH", "ACC-SUPP", "ACC-PAY"}
    hit = only(result, "R4", "ACC-EMP")
    assert hit.severity == pytest.approx(0.19344, abs=1e-4)
    assert hit.evidence["down_weighted"] is True
    assert hit.evidence["recurrence_period_hours"] == 168.0
    assert hit.evidence["distinct_lap_starts"] == 3
    # one pattern, one hit per account, even though three laps were observed
    assert (
        sum(1 for other in result.hits if other.rule_id == "R4" and other.account_key == "ACC-EMP")
        == 1
    )


def test_r4_ignores_the_periodic_weight_when_config_turns_it_off() -> None:
    cfg = settings(rule="R4", down_weight_periodic=False)
    rows: list[Row] = []
    for lap in range(3):
        start = instant(7 * lap, 8)
        rows += [
            Row(f"w{lap}1", start, 800_000, "ACC-EMP", "ACC-MERCH"),
            Row(f"w{lap}2", start + HOUR, 800_000, "ACC-MERCH", "ACC-SUPP"),
            Row(f"w{lap}3", start + 2 * HOUR, 800_000, "ACC-SUPP", "ACC-PAY"),
            Row(f"w{lap}4", start + 3 * HOUR, 786_000, "ACC-PAY", "ACC-EMP"),
        ]
    hit = only(evaluate(frame(rows), cfg=cfg), "R4", "ACC-EMP")
    assert hit.severity == pytest.approx(0.64479, abs=1e-4)
    assert hit.evidence["down_weighted"] is False


def test_r4_cross_currency_loop_is_a_named_near_miss_by_default() -> None:
    # The shape DEV-015 measured in the corpus's own labelled cycles: every hop converts
    # and the amounts grow. Under the shipped policy the currency rule refuses it, and
    # with the non-increasing knob on that refusal is named too.
    rows = [
        Row("f1", instant(0), 58_702_10, "ACC-A", "ACC-B", currency="EUR"),
        Row("f2", instant(0, 1), 73_328_7, "ACC-B", "ACC-C", currency="CHF"),
        Row("f3", instant(0, 2), 26_443_7, "ACC-C", "ACC-A", currency="ILS"),
    ]
    result = evaluate(frame(rows))
    assert accounts(result, "R4") == set()
    miss = result.near_misses[0]
    assert "cross_currency_legs" in miss.reasons
    assert miss.measurements["currencies"] == ["CHF", "EUR", "ILS"]
    assert miss.measurements["retention_comparable_across_currencies"] is False


def test_r4_currency_and_non_increasing_knobs_are_config_sourced() -> None:
    # Same loop as the test above, with the policy knobs relaxed one at a time. Turning
    # both off makes it a hit, which is what proves the knobs are wired rather than
    # decorative: the default refuses, the alternative admits, and nothing in between is
    # assumed.
    rows = [
        Row("f1", instant(0), 5_870_210, "ACC-A", "ACC-B", currency="EUR"),
        Row("f2", instant(0, 1), 5_000_000, "ACC-B", "ACC-C", currency="CHF"),
        Row("f3", instant(0, 2), 4_500_000, "ACC-C", "ACC-A", currency="ILS"),
    ]
    strict = evaluate(frame(rows))
    assert "cross_currency_legs" in strict.near_misses[0].reasons

    relaxed = evaluate(
        frame(rows),
        cfg=settings(rule="R4", currency_policy="ignore_currency", non_increasing=False),
    )
    assert accounts(relaxed, "R4") == {"ACC-A", "ACC-B", "ACC-C"}
    hit = only(relaxed, "R4", "ACC-A")
    assert hit.evidence["currency_policy"] == "ignore_currency"


def test_r4_default_policy_agrees_with_the_graph_engine() -> None:
    """The rules-layer walk must not drift from P3a's enumerator under the shipped policy."""
    rows = [
        Row("c1", instant(0), 100_000, "ACC-A", "ACC-B"),
        Row("c2", instant(0, 1), 100_000, "ACC-B", "ACC-C"),
        Row("c3", instant(0, 2), 90_000, "ACC-C", "ACC-A"),
        Row("d1", instant(3), 200_000, "ACC-X", "ACC-Y"),
        Row("d2", instant(3, 1), 150_000, "ACC-Y", "ACC-X"),
    ]
    events = frame(rows)
    pipeline = load_pipeline_config(REPO_ROOT)
    graph = build_graph(
        events,
        dataclasses.replace(
            pipeline,
            raw={
                **pipeline.raw,
                "graph": {**pipeline.raw["graph"], "rail_degree_percentile": 99.9},
            },
        ),
    )
    assert graph.search.count == 1  # the A-B-C ring; X->Y->X is two legs, below min_length
    assert accounts(evaluate(events), "R4") == {"ACC-A", "ACC-B", "ACC-C"}


def test_cycle_search_budget_is_respected_and_reported() -> None:
    # Four accounts in a directed ring, six parallel legs per hop: the walk has far more
    # than one visit's worth of continuations to consider. With max_visits = 1 it must
    # stop and say so rather than hang, and the flag must reach the result. Every node
    # keeps two distinct neighbours so none of them is a singleton being skipped for a
    # reason unrelated to the budget.
    nodes = ["ACC-A", "ACC-B", "ACC-C", "ACC-D"]
    rows = [
        Row(f"e{lap}{hop}", instant(lap * 7, hop, lap), 100_000, nodes[hop], nodes[(hop + 1) % 4])
        for lap in range(6)
        for hop in range(4)
    ]
    events = frame(rows)
    unbounded = evaluate(events)
    assert not unbounded.cycle_search_truncated
    assert accounts(unbounded, "R4") == set(nodes)
    bounded = evaluate(events, cfg=settings(rule="R4", max_visits=1))
    assert bounded.cycle_search_truncated is True
    assert bounded.cycle_search_reason == "max_visits"
    assert bounded.cycle_search_visits >= 1


# ---------------------------------------------------------------------------
# R5 structuring
# ---------------------------------------------------------------------------


def test_r5_fires_on_a_ladder_and_is_ranked_by_count_and_tightness() -> None:
    # T = 2_500_000 (config), band [0.80, 1.00] x T = [2_000_000, 2_500_000].
    # ladder A: 2_450_000 x 3 on three consecutive days -> all in band, 3 >= n.
    #   count term  = (3 - 3) / (6 - 3) = 0
    #   tightness   = mean(amount/T) = 0.98; (0.98 - 0.80) / (1.00 - 0.80) = 0.90
    #   severity    = mean(0, 0.90) = 0.45
    rows = [
        Row(
            f"a{i}",
            instant(i * 2, 9),
            2_450_000,
            "ACC-SMURF",
            "ACC-AGENT",
            txn_type="CASH_OUT",
            channel="AGENT",
        )
        for i in range(3)
    ]
    result = evaluate(frame(rows))
    assert accounts(result, "R5") == {"ACC-SMURF"}
    assert only(result, "R5", "ACC-SMURF").severity == pytest.approx(0.45, abs=1e-6)


def test_r5_near_misses_band_and_window() -> None:
    # (a) two in band and one at 1_975_000 = 79.0% of T, below the 80% edge -> 2 < n.
    # (b) three in band but on days 0, 5 and 10 -> any 7-day window holds two.
    rows = [
        Row("a1", instant(0, 9), 2_100_000, "ACC-SM-A", "ACC-AGENT", txn_type="CASH_OUT"),
        Row("a2", instant(1, 9), 2_480_000, "ACC-SM-A", "ACC-AGENT", txn_type="CASH_OUT"),
        Row("a3", instant(2, 9), 1_975_000, "ACC-SM-A", "ACC-AGENT", txn_type="CASH_OUT"),
        Row("b1", instant(20, 9), 2_100_000, "ACC-SM-B", "ACC-AGENT", txn_type="CASH_OUT"),
        Row("b2", instant(25, 9), 2_100_000, "ACC-SM-B", "ACC-AGENT", txn_type="CASH_OUT"),
        Row("b3", instant(30, 9), 2_100_000, "ACC-SM-B", "ACC-AGENT", txn_type="CASH_OUT"),
    ]
    assert accounts(evaluate(frame(rows)), "R5") == set()


def test_structuring_threshold_is_config_sourced_and_labelled_synthetic() -> None:
    # Moving the configured T moves the band, which is the only way to show the number is
    # read from config rather than recognised by the rule. At T = 2_000_000 the same three
    # transactions sit at 1.05 x T, i.e. outside [0.80, 1.00] x T, so the rule goes quiet.
    rows = [
        Row(f"c{i}", instant(i * 2, 9), 2_100_000, "ACC-LAD", "ACC-AGENT", txn_type="CASH_OUT")
        for i in range(3)
    ]
    loaded = evaluate(frame(rows))
    assert accounts(loaded, "R5") == {"ACC-LAD"}
    hit = only(loaded, "R5", "ACC-LAD")
    assert hit.evidence["structuring_threshold_minor"] == 2_500_000
    assert hit.evidence["threshold_is_synthetic"] is True
    assert "SYNTHETIC" in str(hit.evidence["threshold_label"])
    assert "config-pinned" in str(hit.evidence["threshold_source"])

    moved = evaluate(frame(rows), cfg=settings(rule="R5", threshold_minor=2_000_000))
    assert accounts(moved, "R5") == set()


def test_r5_threshold_can_be_derived_from_the_histogram_mode() -> None:
    # With no pinned T the fitter searches the empirical support: the only cluster of three
    # or more originated amounts inside a [0.8T, 1.0T] band is the ladder itself, so the
    # derived value has to land on 2_450_000 (the largest member, which contains all four).
    rows = [
        Row(f"d{i}", instant(i * 2, 9), amount, "ACC-HIST", "ACC-AGENT", txn_type="CASH_OUT")
        for i, amount in enumerate([2_450_000, 2_300_000, 2_100_000, 2_455_000])
    ]
    result = evaluate(frame(rows), cfg=settings(rule="R5", threshold_minor=None))
    assert accounts(result, "R5") == {"ACC-HIST"}
    fit = next(f for f in result.thresholds if f.name == "structuring_threshold_minor")
    assert fit.value == 2_455_000
    assert "histogram mode" in fit.derivation


def test_threshold_fit_is_pinned_to_the_window_that_produced_it() -> None:
    # tau and T are both fitted, and both must carry the window they were fitted on: a
    # threshold with no provenance is indistinguishable from one tuned on the test set.
    events = frame(
        [Row(f"g{i}", instant(i), 1_000_000 + i, f"ACC-S{i}", f"ACC-D{i}") for i in range(8)]
    )
    fit_window = Window(start_us=0, end_us=10**18, label="train")
    result = evaluate(events, fit_window=fit_window)
    assert [fit.window.label for fit in result.thresholds] == ["train", "train"]
    assert result.fit_window == fit_window
    assert result.window.label == "run"
    assert result.window.end_us < fit_window.end_us
    # the fit is recorded in the fingerprint-bearing payload scoring will consume
    payload = result.as_json_dict()
    assert payload["fit_window"]["label"] == "train"
    assert {item["name"] for item in payload["thresholds"]} == {
        "tau_minor",
        "structuring_threshold_minor",
    }


# ---------------------------------------------------------------------------
# R6 velocity
# ---------------------------------------------------------------------------


def _velocity_rows(spike_count: int, baseline_days: int = 5) -> list[Row]:
    """One account, one counterparty, daily counts 3,4,5,6,7 then a spike day.

    Baseline median of [3,4,5,6,7,spike] and the MAD of its deviations decide z. With
    spike 13: median 5.5, deviations [2.5,1.5,.5,.5,1.5,7.5] -> MAD 1.5,
    sigma = 1.4826 * 1.5 = 2.2239, z = (13 - 5.5)/2.2239 = 3.3724 >= 3.0.
    severity = (3.3724 - 3)/(6 - 3) = 0.1241.
    """
    rows: list[Row] = []
    for day, count in enumerate([3, 4, 5, 6, 7][:baseline_days]):
        rows += [
            Row(f"b{day}{i}", instant(day * 3, 1, i * 3), 50_000, "ACC-VEL", "ACC-CP")
            for i in range(count)
        ]
    rows += [
        Row(f"s{i}", instant(20, 1 + i // 6, (i * 7) % 60), 50_000, "ACC-VEL", "ACC-CP")
        for i in range(spike_count)
    ]
    return rows


def test_r6_fires_on_a_robust_z_spike() -> None:
    result = evaluate(frame(_velocity_rows(13)))
    # Both ends of one burst observe the same 27 paired events, so both fire at equal z.
    # That is the declared tie the golden fixture records for VEL-01/VEL-B1, not a leak.
    assert accounts(result, "R6") == {"ACC-VEL", "ACC-CP"}
    hit = only(result, "R6", "ACC-VEL")
    assert float(hit.evidence["observation"]) == pytest.approx(3.3724, abs=1e-3)
    assert hit.severity == pytest.approx(0.1241, abs=1e-3)
    assert hit.evidence["baseline_active_days"] == 5
    assert hit.evidence["robust_scale"] == "mad"


def test_r6_near_miss_below_z_and_flat_baseline() -> None:
    # spike 8 -> z = (8 - 5.5)/2.2239 = 1.1243 < 3.0
    assert accounts(evaluate(frame(_velocity_rows(8))), "R6") == set()
    # a flat baseline has no measured variability: MAD 0 is reported as not significant
    # rather than as an infinite z-score.
    flat = [
        Row(f"fl{i}{j}", instant(i * 2, 1, j), 50_000, "ACC-FLAT", "ACC-CFP")
        for i in range(6)
        for j in range(4)
    ]
    assert accounts(evaluate(frame(flat)), "R6") == set()


def test_r6_short_baseline_is_not_scored() -> None:
    # spike 13 would score z = 3.37 but only three active baseline days exist and
    # min_baseline_days is 5, so the account is never evaluated at all.
    assert accounts(evaluate(frame(_velocity_rows(13, baseline_days=3))), "R6") == set()


# ---------------------------------------------------------------------------
# R7 dormancy
# ---------------------------------------------------------------------------


def test_r7_fires_after_a_calendar_gap_and_a_burst() -> None:
    # last activity day 0, next activity day 35 -> gap 35 calendar days >= 30.
    # six transactions inside three hours >= k 5.
    # severity = mean( (6-5)/(10-5), (35-30)/30 ) = mean(0.2, 0.16667) = 0.18333
    rows = [Row("x0", instant(0, 5), 60_000, "ACC-SLEEP", "ACC-P")] + [
        Row(f"y{i}", instant(35, 4, i * 30), 60_000, "ACC-SLEEP", "ACC-P") for i in range(6)
    ]
    result = evaluate(frame(rows))
    # the counterparty sees the identical silence-then-burst from its own side, so it
    # scores the same way: dormancy is a property of the relationship, not of one end
    assert accounts(result, "R7") == {"ACC-SLEEP", "ACC-P"}
    hit = only(result, "R7", "ACC-SLEEP")
    assert hit.severity == pytest.approx(0.18333, abs=1e-5)
    assert hit.evidence["dormant_calendar_days"] == 35


def test_r7_near_misses_gap_29d_and_burst_of_four() -> None:
    silent_gap = [Row("z0", instant(0, 5), 60_000, "ACC-GAP", "ACC-P")] + [
        Row(f"zg{i}", instant(29, 4, i * 20), 60_000, "ACC-GAP", "ACC-P") for i in range(6)
    ]
    short_burst = [Row("w0", instant(0, 5), 60_000, "ACC-BURST", "ACC-P")] + [
        Row(f"wb{i}", instant(40, 4, i * 30), 60_000, "ACC-BURST", "ACC-P") for i in range(4)
    ]
    assert accounts(evaluate(frame(silent_gap + short_burst)), "R7") == set()


# ---------------------------------------------------------------------------
# R8 odd hours
# ---------------------------------------------------------------------------


def _odd_hour_rows(night_count: int) -> list[Row]:
    """Ten baseline transactions in local hour 10, then ten recent ones.

    Quiet hours are the two lowest-volume baseline hours ranked by (volume, hour). The
    baseline sits entirely in local hour 10, so the quiet set is {0, 1}. With
    ``night_count`` of the ten recent transactions in those hours the jump is
    ``night_count/10 - 0``, and at 8 that is 0.80 >= q 0.40 with severity
    (0.80 - 0.40) / (1 - 0.40) = 0.66667.
    """
    rows = [Row(f"day{i}", instant(i, 7), 25_000, "ACC-NIGHT", "ACC-NP") for i in range(10)]
    for i in range(10):
        # UTC 21:00/22:00 is local 00:00/01:00 at the deployment offset; UTC 07:00 is
        # local 10:00, so the non-night rows stay in a busy hour.
        at = instant(40, 21 + (i % 2), (i * 7) % 60) if i < night_count else instant(40, 7, i * 3)
        rows.append(Row(f"n{i}", at, 25_000, "ACC-NIGHT", "ACC-NP"))
    return rows


def test_r8_fires_on_a_shift_into_quiet_local_hours() -> None:
    result = evaluate(frame(_odd_hour_rows(8)))
    # the payee sits inside the same twenty-event window and its own hour distribution is
    # the mirror image, so it scores the same shift; both ends firing is correct, not a leak
    assert accounts(result, "R8") == {"ACC-NIGHT", "ACC-NP"}
    hit = only(result, "R8", "ACC-NIGHT")
    assert hit.severity == pytest.approx(0.66667, abs=1e-4)
    assert hit.evidence["quiet_hours"] == [0, 1]


def test_r8_near_misses_share_and_minimum_volume() -> None:
    # three recent transactions in quiet hours: jump 0.30 < q 0.40
    assert accounts(evaluate(frame(_odd_hour_rows(3))), "R8") == set()
    # and fewer than min_transactions 10 recent rows at all, whatever the share
    thin = [Row(f"b{i}", instant(i, 7), 25_000, "ACC-FEW", "ACC-FP") for i in range(10)] + [
        Row(f"n{i}", instant(40, 21 + (i % 2)), 25_000, "ACC-FEW", "ACC-FP") for i in range(5)
    ]
    assert accounts(evaluate(frame(thin)), "R8") == set()


def test_odd_hour_uses_local_hour_and_not_the_utc_instant() -> None:
    # Two views of one set of UTC instants, differing only in the local columns: the same
    # 21:00Z/22:00Z cluster is 00:00/01:00 in a UTC+3 deployment (a night shift) and
    # 13:00/14:00 in a UTC-8 one (mid-afternoon). The 07:00Z baseline moves the other way.
    # R8 flips its verdict while the instants stay byte-identical, which no
    # event_ts_utc-reading implementation can do (03 C test_odd_hour_uses_local_hour).
    rows = _odd_hour_rows(8)
    east = frame(rows, offset_hours=3)
    west = frame(rows, offset_hours=-8)
    assert east.get_column("event_ts_utc").to_list() == west.get_column("event_ts_utc").to_list()
    assert accounts(evaluate(east), "R8") == {"ACC-NIGHT", "ACC-NP"}
    assert accounts(evaluate(west), "R8") == set()


# ---------------------------------------------------------------------------
# R9 amount regime
# ---------------------------------------------------------------------------


def test_r9_fires_outside_the_multiplicative_band() -> None:
    # baseline median 100000, recent median 480000 -> ratio 4.8 > m 4.
    # severity = log(4.8/4)/log(4) = 0.18232/1.38629 = 0.13152
    rows = [Row(f"p{i}", instant(i * 3), 100_000, "ACC-REG", "ACC-RP") for i in range(7)] + [
        Row(f"q{i}", instant(40 + i, 1), 480_000, "ACC-REG", "ACC-RQ") for i in range(5)
    ]
    result = evaluate(frame(rows))
    assert accounts(result, "R9") == {"ACC-REG"}
    assert only(result, "R9", "ACC-REG").severity == pytest.approx(0.13152, abs=1e-4)


def test_r9_near_miss_inside_the_band_and_collapse_side() -> None:
    inside = [Row(f"p{i}", instant(i * 3), 100_000, "ACC-REG-N", "ACC-RP") for i in range(7)] + [
        Row(f"q{i}", instant(40 + i, 1), 390_000, "ACC-REG-N", "ACC-RQ") for i in range(5)
    ]
    assert accounts(evaluate(frame(inside)), "R9") == set()
    # the low side of the band is a real condition, not decoration: ratio 0.20 < 1/4
    collapsed = [
        Row(f"c{i}", instant(i * 3), 1_000_000, "ACC-REG-L", "ACC-RP") for i in range(7)
    ] + [Row(f"e{i}", instant(40 + i, 1), 200_000, "ACC-REG-L", "ACC-RQ") for i in range(5)]
    result = evaluate(frame(collapsed))
    assert accounts(result, "R9") == {"ACC-REG-L"}
    assert float(only(result, "R9", "ACC-REG-L").evidence["observation"]) == pytest.approx(
        0.2, abs=1e-9
    )


def test_log_band_severity_is_symmetric_around_one() -> None:
    assert log_band_severity(4.8, 4.0) == pytest.approx(log_band_severity(1 / 4.8, 4.0), abs=1e-9)


# ---------------------------------------------------------------------------
# R10 fast cash-out
# ---------------------------------------------------------------------------


def test_r10_fires_on_the_episode_total_not_a_single_withdrawal() -> None:
    # inflow 800000 at 06:00; CASH_OUT 320000 at 08:00 and 300000 at 10:00.
    # Neither withdrawal alone clears s (0.40, 0.375); the episode total is 620000,
    # share 620000/800000 = 0.775 >= 0.70 -> fires.
    # severity = (0.775 - 0.70)/(1 - 0.70) = 0.075/0.30 = 0.25
    rows = [
        Row("i1", instant(0, 6), 800_000, "ACC-IN", "ACC-CASH"),
        Row(
            "o1",
            instant(0, 8),
            320_000,
            "ACC-CASH",
            "ACC-ATM",
            txn_type="CASH_OUT",
            channel="AGENT",
        ),
        Row(
            "o2",
            instant(0, 10),
            300_000,
            "ACC-CASH",
            "ACC-ATM",
            txn_type="CASH_OUT",
            channel="AGENT",
        ),
    ]
    result = evaluate(frame(rows))
    assert accounts(result, "R10") == {"ACC-CASH"}
    assert only(result, "R10", "ACC-CASH").severity == pytest.approx(0.25, abs=1e-6)


def test_r10_near_misses_share_and_holding_time() -> None:
    slow = [
        Row("h1", instant(0, 6), 1_000_000, "ACC-IN", "ACC-CASH-S"),
        Row("h2", instant(0, 20), 900_000, "ACC-CASH-S", "ACC-ATM", txn_type="CASH_OUT"),
    ]
    thin = [
        Row("s1", instant(2, 6), 1_000_000, "ACC-IN", "ACC-CASH-T"),
        Row("s2", instant(2, 9), 690_000, "ACC-CASH-T", "ACC-ATM", txn_type="CASH_OUT"),
    ]
    assert accounts(evaluate(frame(slow + thin)), "R10") == set()


# ---------------------------------------------------------------------------
# R11 new counterparties
# ---------------------------------------------------------------------------


def test_r11_fires_on_a_first_time_surge() -> None:
    # eight counterparties, none of which ACC-NEW has ever transacted with:
    # fresh 8, active 8 -> share 1.0 >= g 0.80 and count 8 >= c 6.
    # severity = (8 - 6)/(12 - 6) = 0.3333
    rows = [Row(f"k{i}", instant(0, 0, i * 5), 40_000, "ACC-NEW", f"ACC-F{i}") for i in range(8)]
    result = evaluate(frame(rows))
    assert accounts(result, "R11") == {"ACC-NEW"}
    assert only(result, "R11", "ACC-NEW").severity == pytest.approx(0.33333, abs=1e-5)


def test_r11_near_misses_share_and_count() -> None:
    # six counterparties inside the window, two of which are returning from an earlier
    # encounter -> share 4/6 = 0.667 < g 0.80 even though the count clears c.
    returning = (
        [Row(f"r{i}", instant(0, 0, i), 40_000, "ACC-SURGE-A", f"ACC-OLD{i}") for i in range(2)]
        + [Row(f"m{i}", instant(30, 0, i), 40_000, "ACC-SURGE-A", f"ACC-OLD{i}") for i in range(2)]
        + [
            Row(f"n{i}", instant(30, 0, 10 + i), 40_000, "ACC-SURGE-A", f"ACC-NEW{i}")
            for i in range(4)
        ]
    )
    # five fresh counterparties in one window: share 1.0, count 5 < c 6.
    few = [
        Row(f"f{i}", instant(60, 0, i * 4), 40_000, "ACC-SURGE-B", f"ACC-FRESH{i}")
        for i in range(5)
    ]
    assert accounts(evaluate(frame(returning + few)), "R11") == set()


def test_r11_reads_pair_history_from_either_side() -> None:
    # Eight counterparties meet ACC-HUB for the first time, four of them after a day-0
    # encounter: the pair (peer, hub) is one relationship whichever side sent, so a
    # returning peer must not read as new merely because the account being scored is the
    # one that received. Fresh 4 of 8 -> share 0.50 < g 0.80 -> silent.
    met_before = [
        Row(f"p{i}", instant(0, 0, i), 40_000, f"ACC-OLD{i}", "ACC-HUB") for i in range(4)
    ] + [Row(f"r{i}", instant(30, 0, 40 + i), 40_000, f"ACC-OLD{i}", "ACC-HUB") for i in range(4)]
    brand_new = [
        Row(f"n{i}", instant(30, 0, i), 40_000, f"ACC-NEW{i}", "ACC-HUB") for i in range(4)
    ]
    result = evaluate(frame(met_before + brand_new))
    assert "ACC-HUB" not in accounts(result, "R11")
    # the same eight relationships scored from the peer side are one counterparty each, so
    # the count floor is what keeps a single-relationship account off the panel however
    # new that relationship is
    assert "ACC-NEW0" not in accounts(result, "R11")


# ---------------------------------------------------------------------------
# R12 chain
# ---------------------------------------------------------------------------


def test_r12_fires_on_a_decay_exact_chain_once_per_account() -> None:
    # 1000000 -> 950000 -> 902500 -> 857375, every hop 6h: 4 hops >= min_length 3,
    # non-increasing, 0.95 exactly per hop so fidelity = 1.0.
    # severity = mean( (4-3)/(6-3), 1.0 ) = mean(0.3333, 1.0) = 0.6667
    amounts = [1_000_000, 950_000, 902_500, 857_375]
    nodes = [f"ACC-CHN-{i}" for i in range(5)]
    rows = [
        Row(f"e{i}", instant(0, i * 6), amounts[i], nodes[i], nodes[i + 1])
        for i in range(len(amounts))
    ]
    result = evaluate(frame(rows))
    assert accounts(result, "R12") == set(nodes)
    assert only(result, "R12", nodes[0]).severity == pytest.approx(0.66667, abs=1e-5)
    # the maximal chain is one pattern: the three- and four-hop views of it are not
    # separate evidence
    assert len([h for h in result.hits if h.rule_id == "R12" and h.account_key == nodes[0]]) == 1


def test_r12_near_misses_increasing_slow_and_short() -> None:
    rising = [
        Row("q1", instant(0), 500_000, "ACC-UP-A", "ACC-UP-B"),
        Row("q2", instant(0, 2), 600_000, "ACC-UP-B", "ACC-UP-C"),
        Row("q3", instant(0, 4), 400_000, "ACC-UP-C", "ACC-UP-D"),
    ]
    slow = [
        Row("s1", instant(0), 500_000, "ACC-SL-A", "ACC-SL-B"),
        Row("s2", instant(2), 475_000, "ACC-SL-B", "ACC-SL-C"),
        Row("s3", instant(4), 451_250, "ACC-SL-C", "ACC-SL-D"),
    ]
    short = [
        Row("h1", instant(6), 500_000, "ACC-SH-A", "ACC-SH-B"),
        Row("h2", instant(6, 3), 475_000, "ACC-SH-B", "ACC-SH-C"),
    ]
    assert accounts(evaluate(frame(rising + slow + short)), "R12") == set()


def test_chain_search_records_truncation_instead_of_hanging() -> None:
    amounts = [1_000_000, 950_000, 902_500, 857_375, 814_506]
    nodes = [f"ACC-CX-{i}" for i in range(6)]
    rows = [
        Row(f"e{i}", instant(0, 0, i), amounts[i], nodes[i], nodes[i + 1])
        for i in range(len(amounts))
    ] + [Row(f"x{i}", instant(1, 0, i), amounts[0], nodes[0], nodes[(i % 5) + 1]) for i in range(6)]
    events = frame(rows)
    assert accounts(evaluate(events), "R12") != set()
    tight = evaluate(events, cfg=settings(rule="R12", component_budget=1))
    assert tight.chain_search_truncated is True
    assert tight.chain_search_reason == "max_visits"


def test_r12_reversal_does_not_extend_a_chain() -> None:
    rows = [
        Row("m1", instant(0), 1_000_000, "ACC-RV-1", "ACC-RV-2"),
        Row("m2", instant(0, 3), 950_000, "ACC-RV-2", "ACC-RV-3"),
        Row("m3", instant(0, 6), 902_500, "ACC-RV-3", "ACC-RV-4"),
        Row("m4", instant(0, 9), 857_375, "ACC-RV-4", "ACC-RV-5"),
    ]
    baseline = evaluate(frame(rows))
    assert len(accounts(baseline, "R12")) == 5
    with_refund = evaluate(
        frame(
            [*rows, Row("m5", instant(0, 12), 814506, "ACC-RV-5", "ACC-RV-1", txn_type="REVERSAL")]
        )
    )
    # the refund is not a hop: the chain stays five nodes long rather than closing a ring
    assert len(accounts(with_refund, "R12")) == 5
    assert accounts(with_refund, "R4") == set()


# ---------------------------------------------------------------------------
# guards: overlap groups, dedup, ceiling, dead rules
# ---------------------------------------------------------------------------


def test_overlapping_rules_are_counted_once_per_account() -> None:
    # one receipt and one cash-out 35 minutes later: R1 sees 0.88 of a 1000000 receipt
    # passed on (severity (0.88-0.80)/0.20 = 0.40) and R10 sees 880000/1000000 = 0.88
    # (severity (0.88-0.70)/0.30 = 0.60). Both are the extraction typology on one pair.
    rows = [
        Row("e1", instant(0, 6), 1_000_000, "ACC-FUND", "ACC-MULE7"),
        Row("e2", instant(0, 6, 35), 880_000, "ACC-MULE7", "ACC-ATM", txn_type="CASH_OUT"),
    ]
    result = evaluate(frame(rows))
    assert accounts(result, "R1") == {"ACC-MULE7"}
    assert accounts(result, "R10") == {"ACC-MULE7"}
    assert only(result, "R1", "ACC-MULE7").overlap_group == "extraction"
    assert only(result, "R10", "ACC-MULE7").overlap_group == "extraction"
    groups = [group for group in result.evidence_groups if group.account_key == "ACC-MULE7"]
    assert len(groups) == 1
    assert groups[0].rule_ids == ("R1", "R10")
    assert groups[0].severity == pytest.approx(0.60, abs=1e-6)
    # the score sees one unit of extraction evidence; R5's absence keeps the total at one
    assert result.evidence_units("ACC-MULE7") == 1


def test_overlap_grouping_is_config_driven_not_a_blanket_dedupe() -> None:
    # FAN_OUT is in no group, so a scatter account that also surges keeps two units.
    rows = [
        Row(f"p{i}", instant(0, 0, i * 4), 30_000, "ACC-SCATTER-X", f"ACC-T{i}") for i in range(9)
    ]
    result = evaluate(frame(rows))
    assert accounts(result, "R3") == {"ACC-SCATTER-X"}
    assert accounts(result, "R11") == {"ACC-SCATTER-X"}
    assert only(result, "R3", "ACC-SCATTER-X").overlap_group is None
    assert only(result, "R11", "ACC-SCATTER-X").overlap_group == "aggregation"
    assert result.evidence_units("ACC-SCATTER-X") == 2


def test_pattern_signature_dedup_collapses_one_pattern_seen_twice() -> None:
    first = RuleHit(
        rule_id="R5",
        rule_name="STRUCTURING",
        account_key="ACC-X",
        severity=0.30,
        evidence={
            "observation": 3.0,
            "threshold_param": "n",
            "threshold_value": 3,
            "txn_ids": ["t1", "t2", "t3"],
        },
        window=Window(start_us=0, end_us=10, label="w1"),
        hit_signature="same",
    )
    second = RuleHit(
        rule_id="R5",
        rule_name="STRUCTURING",
        account_key="ACC-X",
        severity=0.55,
        evidence={
            "observation": 3.0,
            "threshold_param": "n",
            "threshold_value": 3,
            "txn_ids": ["t1", "t2", "t3", "t4"],
        },
        window=Window(start_us=5, end_us=15, label="w2"),
        hit_signature="same",
    )
    merged = deduplicate_by_signature([first, second])
    assert len(merged) == 1
    assert merged[0].severity == pytest.approx(0.55)
    assert merged[0].evidence["windows_observed"] == 2
    assert merged[0].evidence["txn_ids"] == ["t1", "t2", "t3", "t4"]


def test_a_ladder_across_midnight_is_detected_once() -> None:
    # three in-band withdrawals at 23:00, 23:50 and 00:40 the next day: the pattern
    # straddles the day boundary, and one ladder has to be reported.
    rows = [
        Row(
            f"m{i}",
            instant(0, hour, minute),
            2_400_000,
            "ACC-MID",
            "ACC-AGENT",
            txn_type="CASH_OUT",
        )
        for i, (hour, minute) in enumerate([(23, 0), (23, 50), (24, 40)])
    ]
    result = evaluate(frame(rows))
    hits = [hit for hit in result.hits if hit.rule_id == "R5"]
    assert len(hits) == 1
    assert len(hits[0].evidence["txn_ids"]) == 3


def test_hit_rate_ceiling_fails_the_run_with_a_suggestion() -> None:
    # Two mules sharing one funder and one payee: R1 fires on 2 of 4 accounts = 0.50,
    # above the 0.33 ceiling. The suggestion is the observation at the rank the ceiling
    # allows, taken upwards: both mules pass 0.92, so the next admissible p is 0.93.
    rows = [
        Row("1", instant(0), 500_000, "ACC-FUND", "ACC-MULE-A"),
        Row("2", instant(0, 0, 40), 460_000, "ACC-MULE-A", "ACC-PAY"),
        Row("3", instant(1), 500_000, "ACC-FUND", "ACC-MULE-B"),
        Row("4", instant(1, 0, 40), 460_000, "ACC-MULE-B", "ACC-PAY"),
    ]
    events = frame(rows)
    pipeline = load_pipeline_config(REPO_ROOT)
    graph = build_graph(
        events,
        dataclasses.replace(
            pipeline,
            raw={
                **pipeline.raw,
                "graph": {**pipeline.raw["graph"], "rail_degree_percentile": 99.9},
            },
        ),
    )
    with pytest.raises(HitRateCeilingError) as raised:
        evaluate_rules(events, graph, load_rules_settings(REPO_ROOT))
    error = raised.value
    assert error.rule_id == "R1"
    assert error.hit_rate == pytest.approx(0.5)
    assert error.suggested_params == {"p": 0.93}
    # the same run with enforcement off reports the breach instead of raising
    reported = evaluate(events, cfg=load_rules_settings(REPO_ROOT))
    row = next(item for item in reported.hit_rates if item.rule_id == "R1")
    assert row.status == "above_ceiling"
    assert row.suggestion is not None
    assert float(row.suggestion.suggested) == pytest.approx(0.93)


def test_dead_rule_is_removed_with_a_reason_and_excluded_from_scoring() -> None:
    # No loop, no chain, no cash-out and no ladder: the graph rules have nothing to see,
    # and DEV-011 says that is a measurement. They are removed from what the score
    # consumes rather than displayed as capability the run did not have.
    rows = [Row(f"n{i}", instant(i), 60_000, f"ACC-A{i}", "ACC-B") for i in range(6)]
    result = evaluate(frame(rows))
    removed = {rule.rule_id: rule for rule in result.removed_rules}
    assert "R4" in removed and "R12" in removed
    assert "DEV-011" in removed["R4"].reason
    assert "time-respecting cycles" in removed["R4"].reason
    assert "R4" not in result.scored_rule_ids()
    assert "R4" in {row.rule_id for row in result.hit_rates}


def test_dead_rule_removal_can_be_turned_off_by_config() -> None:
    loaded = load_rules_settings(REPO_ROOT)
    strict = dataclasses.replace(
        loaded, engine=dataclasses.replace(loaded.engine, remove_dead_rules=False)
    )
    rows = [Row(f"n{i}", instant(i), 60_000, f"ACC-A{i}", "ACC-B") for i in range(6)]
    result = evaluate(frame(rows), cfg=strict)
    # removal is a display decision, not a data decision: the row stays and still says
    # it scored nothing, because a rule nobody can see cannot be reviewed either.
    assert result.removed_rules == ()
    dead = next(row for row in result.hit_rates if row.rule_id == "R4")
    assert dead.status == "below_floor"
    assert dead.accounts_hit == 0


def test_no_rule_fires_on_more_than_a_third_of_accounts() -> None:
    """The day-5 rejection trigger, asserted over the golden corpus itself."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "tests" / "golden"))
    from loader import load_golden_transactions

    result = evaluate(load_golden_transactions())
    ceiling = load_rules_settings(REPO_ROOT).hit_rate_ceiling
    assert result.hits
    assert all(row.hit_rate <= ceiling for row in result.hit_rates), result.hit_rate_table()
    assert result.scored_accounts == 319


def test_money_stays_integer_across_the_whole_layer() -> None:
    rows = [
        Row("c1", instant(0), 100_000, "ACC-A", "ACC-B"),
        Row("c2", instant(0, 1), 100_000, "ACC-B", "ACC-C"),
        Row("c3", instant(0, 2), 90_000, "ACC-C", "ACC-A"),
    ]
    result = evaluate(frame(rows))
    for hit in result.hits:
        for value in hit.evidence.values():
            if isinstance(value, list):
                assert all(not isinstance(item, float) for item in value), hit.evidence
    amounts = [hit.evidence.get("amounts_minor") for hit in result.hits]
    assert any(amounts), "no amount evidence was produced at all"
    for entry in amounts:
        if entry:
            assert all(isinstance(value, int) for value in entry)


def test_result_ordering_is_deterministic_and_tie_broken() -> None:
    rows = [
        Row(f"o{i}", instant(0, 0, i * 3), 30_000, "ACC-SCATTER", f"ACC-RECV{i}") for i in range(9)
    ]
    events = frame(rows)
    first, second = evaluate(events), evaluate(events)
    assert [hit.sort_key() for hit in first.hits] == sorted(hit.sort_key() for hit in first.hits)
    assert first.as_json_dict() == second.as_json_dict()


def test_zero_value_rows_do_not_move_a_value_rule() -> None:
    # probes are counts, not value: a zero receipt cannot be a pass-through base and it
    # cannot supply the denominator of a cash-out share.
    rows = [
        Row("z1", instant(0), 0, "ACC-FUND", "ACC-PROBE"),
        Row("z2", instant(0, 0, 30), 45_000, "ACC-PROBE", "ACC-OUT"),
        Row("z3", instant(1), 0, "ACC-FUND", "ACC-PROBE2"),
    ]
    result = evaluate(frame(rows))
    assert accounts(result, "R1") == set()
    assert result.zero_value_excluded == 2


def test_ratio_helper_is_exact_at_the_threshold() -> None:
    # 0.80 * 400000 = 320000 exactly: a float comparison at the boundary is what decides
    # a hit, so the boundary case is asserted rather than assumed.
    assert ratio_of(316_000, 400_000) == pytest.approx(0.79)
    from oxbow.rules.events import ratio_at_least

    assert ratio_at_least(320_000, 400_000, 0.80) is True
    assert ratio_at_least(319_999, 400_000, 0.80) is False
    assert ratio_at_least(1, 0, 0.80) is False


def test_rule_errors_are_named_and_catchable_as_one_type() -> None:
    from oxbow.rules.errors import RuleContractError, RuleError

    with pytest.raises(RuleError) as raised:
        evaluate_rules(pl.DataFrame({"txn_id": []}), None, load_rules_settings(REPO_ROOT))  # type: ignore[arg-type]
    assert isinstance(raised.value, RuleContractError)


def test_settings_refuse_an_unknown_knob_rather_than_ignoring_it(tmp_path: Path) -> None:
    source = (REPO_ROOT / "config" / "rules.yaml").read_text(encoding="utf-8")
    mutated = source.replace(
        "params: { p: 0.80, delta_minutes: 60, min_amount_minor: 10_000 }",
        "params: { p: 0.80, delta_minutes: 60, min_amount_minor: 10_000, p_plus: 0.9 }",
    )
    assert mutated != source
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "rules.yaml").write_text(mutated, encoding="utf-8")
    from oxbow.rules.errors import RuleConfigError

    with pytest.raises(RuleConfigError, match="does not read"):
        load_rules_settings(tmp_path)


def test_rule_id_list_is_the_twelve_the_plan_names() -> None:
    assert len(RULE_REGISTRY) == 12
    assert set(RULE_REGISTRY) == {f"R{index}" for index in range(1, 13)}
