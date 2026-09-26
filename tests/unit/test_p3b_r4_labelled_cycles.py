"""DEV-015: what R4's settings actually exclude, measured on the corpus's own labelled cycles.

The decision entry gives a table of what R4 as specified in plan §9 excludes from the 54
cycles the IBM-AML bundle labels as cycles, and then draws a conclusion from it: 38 of the
54 convert currency on the way round, 25 shed more value than the 0.6 retention floor
tolerates, most grow along the ring, 14 are two-leg round trips, and 5 do not respect
time — "so a single-currency, non-increasing, at-least-0.6-retention definition cannot fire on
them", scoring zero on the labelled positive set. That table is a claim about a corpus,
and a claim about a corpus is checkable.

The table checks out. The conclusion drawn from it does not, and the gap is instructive:
those five counts are per-knob tallies that overlap heavily, so their union is not their
sum. Three single-currency SAR rings (3-4 hops, retention 0.82-0.94, strictly increasing
timestamps) are refused by nothing, so shipped R4 fires on three of the 54 rather than
zero. These tests assert both halves — the six per-knob counts reproduce DEV-015's
recorded numbers, and the headline is pinned to what the bytes actually do.

This module measures two ways, and the two are not redundant:

* :func:`_labelled_cycles` parses the 54 ``CYCLE`` attempt blocks out of
  ``HI-Small_Patterns.txt`` and measures each ring against the settings resolved from
  ``config/rules.yaml``, using the *same* classifier the rule uses
  (:func:`oxbow.rules.cycles.build_loop` / :func:`~oxbow.rules.cycles.loop_reasons`).
  Asserting the counts reproduces DEV-015's numbers from the bytes rather than quoting
  them.
* :func:`test_a_labelled_cycle_is_a_near_miss_with_reasons_end_to_end` feeds one of those
  rings through ``evaluate_rules`` and requires a ``NearMiss`` naming the settings, not an
  empty result. That is the part a table cannot prove: it is the difference between
  "0 hits" and "0 hits, and here is what each knob refused". A near miss can only name a
  reason for a loop the enumerator actually produced, so that test picks a ring inside
  R4's search horizon (``max_length + NEAR_MISS_DEPTH_SLACK``) — a ring longer than the
  horizon is never enumerated, so the ledger stays silent and proves nothing.

The file is optional. Without the corpus on disk the end-to-end test still runs against a
ring transcribed from DEV-015's own quoted example, so the mechanism is never covered only
by data that may not be present.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import Sequence
from pathlib import Path

import polars as pl
import pytest

from oxbow.config import load_pipeline_config
from oxbow.contracts.raw_ibm_aml import CURRENCY_ISO
from oxbow.graph import build_graph
from oxbow.rules import evaluate_rules, load_rules_settings
from oxbow.rules.cycles import NEAR_MISS_DEPTH_SLACK, Leg, build_loop, loop_reasons
from oxbow.rules.settings import CycleMemberSettings, RulesSettings

REPO_ROOT = Path(__file__).resolve().parents[2]
PATTERNS_FILE = REPO_ROOT / "data" / "raw" / "ibmaml" / "HI-Small_Patterns.txt"

# IBM-AML ships currency *names*; canonical v1 requires 2-5 uppercase letters, and the
# graph layer refuses anything else. A name outside the table is a hard failure rather
# than a silent 3-letter guess, because a wrong ISO code would put a fake currency into a
# real measurement — so the transcription is read from the contract module that the ingest
# itself uses rather than restated here, where it could drift away from the frames this
# measurement is a claim about.
CURRENCY_NAMES: dict[str, str] = CURRENCY_ISO

_BLOCK = re.compile(
    r"BEGIN LAUNDERING ATTEMPT - CYCLE.*?\n(.*?)END LAUNDERING ATTEMPT - CYCLE", re.S
)
_LEG = re.compile(
    r"^(\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}),([0-9]+),(\w+),([0-9]+),(\w+),([0-9.]+),([^,]+),",
    re.M,
)


@pytest.fixture(scope="module")
def settings() -> RulesSettings:
    return load_rules_settings(REPO_ROOT)


@pytest.fixture(scope="module")
def cycle_settings(settings: RulesSettings) -> CycleMemberSettings:
    resolved = settings.settings_for("R4")
    assert isinstance(resolved, CycleMemberSettings)
    return resolved


def _iso_currency(name: str) -> str:
    try:
        return CURRENCY_NAMES[name.strip()]
    except KeyError as exc:
        raise AssertionError(
            f"currency {name.strip()!r} is not in the transcription table; guessing an ISO "
            "code would put a fabricated currency into a measurement of the corpus"
        ) from exc


def _legs(block: str) -> tuple[Leg, ...]:
    """One labelled attempt block, as the classifier's own input type."""
    legs: list[Leg] = []
    for index, row in enumerate(_LEG.findall(block.strip())):
        stamp = dt.datetime.strptime(row[0].replace("/", "-"), "%Y-%m-%d %H:%M").replace(
            tzinfo=dt.UTC
        )
        legs.append(
            Leg(
                src=row[2],
                dst=row[4],
                ts_us=int(stamp.timestamp() * 1_000_000),
                txn_id=f"ibm-cycle-{index:02d}",
                amount_minor=int(round(float(row[5]) * 100)),
                currency=_iso_currency(row[6]),
            )
        )
    return tuple(legs)


def _labelled_cycles() -> list[tuple[tuple[Leg, ...], bool]]:
    """Every CYCLE block, as (legs, closes_back_to_the_first_account)."""
    text = PATTERNS_FILE.read_text(encoding="utf-8", errors="replace")
    parsed: list[tuple[tuple[Leg, ...], bool]] = []
    for block in _BLOCK.findall(text):
        legs = _legs(block)
        if len(legs) < 2:
            continue
        parsed.append((legs, legs[-1].dst == legs[0].src))
    return parsed


def _reasons_for(legs: Sequence[Leg], settings: CycleMemberSettings) -> tuple[str, ...]:
    """The labelled ring, measured and refused by exactly the code path R4 uses."""
    return loop_reasons(
        build_loop([leg.src for leg in legs], legs),
        min_length=settings.min_length,
        max_length=settings.max_length,
        retention_floor=settings.retention,
        require_non_increasing=settings.non_increasing,
        same_currency=settings.currency_policy == "same_currency",
        require_strict_time=settings.require_strict_time_increase,
    )


def test_labelled_cycle_reality_matches_the_dev_015_measurement(
    cycle_settings: CycleMemberSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    """DEV-015's table, recomputed from the corpus bytes.

    Claimed: 54 labelled cycles, all closing rings; 38 cross-currency; 25 under the 0.6
    retention floor; 14 shorter than three hops; 5 not time-increasing; amounts grow in
    most. Reconstructed here rather than quoted, because a rule definition argued from a
    remembered number is still an argument from memory.

    The table is measured against plan §9 as the decision re-specified it, which includes
    the non-increasing amounts the shipped config leaves optional; without that knob on,
    the "amounts grow in most" row has no reason code to count and the row is absent
    rather than zero. The headline is then measured against the shipped settings, where
    the knob is off, because that is the configuration a run actually uses.
    """
    if not PATTERNS_FILE.is_file():
        pytest.skip(f"{PATTERNS_FILE} is not on disk; DEV-015's table cannot be recomputed")
    cycles = _labelled_cycles()
    assert len(cycles) == 54, len(cycles)
    assert all(closes for _legs, closes in cycles), "a labelled cycle that does not close"

    specified = dataclasses.replace(cycle_settings, non_increasing=True)
    counts: dict[str, int] = {}
    for legs, _closed in cycles:
        for reason in _reasons_for(legs, specified):
            counts[reason] = counts.get(reason, 0) + 1
    lengths = [len(legs) for legs, _ in cycles]
    report = "\n".join(
        [
            "R4 against the corpus's own 54 labelled cycles",
            f"  settings: retention>={specified.retention}  length "
            f"{specified.min_length}-{specified.max_length}  "
            f"non_increasing={specified.non_increasing}  "
            f"currency={specified.currency_policy}  "
            f"strict_time={specified.require_strict_time_increase}",
            f"  rings: {len(cycles)}  hops min/median/max: "
            f"{min(lengths)}/{sorted(lengths)[len(lengths) // 2]}/{max(lengths)}",
            "  how many labelled cycles each setting excludes:",
            *[f"    {reason:<34} {counts.get(reason, 0):>3} / {len(cycles)}" for reason in sorted(counts)],
            f"  excluded by at least one setting: "
            f"{sum(1 for legs, _ in cycles if _reasons_for(legs, specified))} / {len(cycles)}",
            f"  admitted by the shipped settings (non_increasing="
            f"{cycle_settings.non_increasing}): "
            f"{sum(1 for legs, _ in cycles if not _reasons_for(legs, cycle_settings))} / {len(cycles)}",
        ]
    )
    with capsys.disabled():
        print(report)

    assert counts.get("cross_currency_legs") == 38, counts
    assert counts.get("value_retention_below_floor") == 25, counts
    assert counts.get("length_below_min_length") == 14, counts
    assert counts.get("timestamps_not_increasing") == 5, counts
    assert counts.get("amount_increases_along_loop") == 45, counts
    assert counts.get("length_above_max_length") == 20, counts

    # The headline DEV-015 states is the *union* of those six columns being all 54, and
    # the union is 51, not 54: the columns overlap heavily and 38 + 25 + 14 + 5 looks
    # conclusive only if you add it. The three rings that survive every knob are
    # single-currency Saudi Riyal loops of 3-4 hops that keep 0.82-0.94 of their value
    # and respect time, so shipped R4 fires on three of the 54 labelled cycles. DEV-015's
    # decision is unaffected -- it re-specifies the knobs rather than assuming a zero --
    # but its "score zero on the labelled positive set" sentence is refuted, and pinning
    # the measurement here is what keeps the next reader from repeating it.
    admitted = [legs for legs, _ in cycles if not _reasons_for(legs, cycle_settings)]
    assert len(admitted) == 3, f"measured {len(admitted)} admitted, DEV-015 claims 0"
    assert {leg.currency for legs in admitted for leg in legs} == {"SAR"}
    assert all(cycle_settings.min_length <= len(legs) <= cycle_settings.max_length for legs in admitted)


def test_relaxing_one_setting_admits_different_cycles_not_the_same_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Which knobs overlap, and which are independent.

    DEV-015's decision says the retention floor is a choice about the corpus and the
    currency rule is a separate one; the numbers below are what makes that sentence
    checkable, and they are printed so the next reader can re-run the argument. Lifting
    the currency rule alone admits 7 rings the floor would otherwise let through; lifting
    the floor alone admits none, because every sub-floor ring in this corpus is also
    cross-currency. Lifting both admits 19, so the floor is not redundant -- it refuses
    9 of the 19 once the currency rule is out of the way.
    """
    if not PATTERNS_FILE.is_file():
        pytest.skip(f"{PATTERNS_FILE} is not on disk")
    cycles = _labelled_cycles()
    base = _cycle_settings(retention=0.60, non_increasing=False, same_currency=True)
    no_retention = _cycle_settings(retention=0.0, non_increasing=False, same_currency=True)
    no_currency = _cycle_settings(retention=0.60, non_increasing=False, same_currency=False)
    both_free = _cycle_settings(retention=0.0, non_increasing=False, same_currency=False)
    admits = lambda s: sum(1 for legs, _ in cycles if not _reasons_for(legs, s))  # noqa: E731
    retained_by_floor, freed_by_currency = admits(no_retention) - admits(base), admits(no_currency) - admits(base)
    with capsys.disabled():
        print(
            "knob independence (labelled cycles admitted, 3-6 hops, time-respecting):\n"
            f"  as specified                                  {admits(base):>2} / 54\n"
            f"  retention floor removed                       {admits(no_retention):>2} / 54"
            f"   (+{retained_by_floor})\n"
            f"  currency rule removed                         {admits(no_currency):>2} / 54"
            f"   (+{freed_by_currency})\n"
            f"  both removed                                  {admits(both_free):>2} / 54"
            f"   (+{admits(both_free) - admits(base)})"
        )
    # The two knobs are not the same knob: the currency rule admits cycles the floor
    # would admit anyway, and the floor refuses 9 of them once currency is out of the way.
    assert admits(base) == 3
    assert (retained_by_floor, freed_by_currency) == (0, 7), (retained_by_floor, freed_by_currency)
    assert admits(both_free) == 19
    assert admits(both_free) - admits(no_currency) == 9


def _cycle_settings(
    retention: float, non_increasing: bool, same_currency: bool
) -> CycleMemberSettings:
    return CycleMemberSettings(
        retention=retention,
        min_length=3,
        max_length=6,
        require_strict_time_increase=True,
        down_weight_periodic=True,
        periodic_period_hours=168,
        periodic_down_weight=0.3,
        periodic_tolerance_ratio=0.05,
        non_increasing=non_increasing,
        currency_policy="same_currency" if same_currency else "ignore_currency",
        max_visits=200_000,
        timeout_ms=25_000,
        emit_near_misses=True,
    )


def _ring_frame(legs: Sequence[Leg]) -> pl.DataFrame:
    """Labelled legs as canonical events, with the local columns the rules demand."""
    stamps = [dt.datetime.fromtimestamp(leg.ts_us / 1_000_000, tz=dt.UTC) for leg in legs]
    return pl.DataFrame(
        {
            "txn_id": [f"ibm:{leg.txn_id}" for leg in legs],
            "event_ts_utc": pl.Series(stamps, dtype=pl.Datetime("us", "UTC")),
            "event_date_local": [stamp.date().isoformat() for stamp in stamps],
            "local_hour": pl.Series([stamp.hour for stamp in stamps], dtype=pl.Int8),
            "txn_type": ["TRANSFER"] * len(legs),
            "channel": ["ACH"] * len(legs),
            "amount_minor": pl.Series([leg.amount_minor for leg in legs], dtype=pl.Int64),
            "currency": [leg.currency for leg in legs],
            "account_from": [leg.src for leg in legs],
            "account_to": [leg.dst for leg in legs],
        }
    )


def _first_enumerable_refusal(
    cycles: Sequence[tuple[tuple[Leg, ...], bool]], settings: CycleMemberSettings
) -> tuple[Leg, ...]:
    """The corpus's first ring the search can enumerate and the policy still refuses.

    A near miss can only name a reason for a loop the enumerator produced, and the
    search horizon is ``max_length + NEAR_MISS_DEPTH_SLACK``. The corpus's first labelled
    attempt is 10 hops, outside that horizon, so it never becomes a loop and the ledger
    stays silent -- an empty result that reads like "nothing refused it" and is really
    "nothing looked at it". This picks the first ring inside the horizon refused on both
    value retention and currency, the two knobs a near miss exists to name.
    """
    horizon = settings.max_length + NEAR_MISS_DEPTH_SLACK
    for legs, _closed in cycles:
        if not settings.min_length <= len(legs) <= horizon:
            continue
        reasons = _reasons_for(legs, settings)
        if "value_retention_below_floor" in reasons and "cross_currency_legs" in reasons:
            return legs
    raise AssertionError(
        f"no labelled ring is refused on both retention and currency within {horizon} hops; "
        "the corpus this file measures against has changed shape"
    )


def test_a_labelled_cycle_is_a_near_miss_with_reasons_end_to_end(
    cycle_settings: CycleMemberSettings,
) -> None:
    """The mechanism DEV-015 asked for, through ``evaluate_rules`` rather than the classifier.

    A cross-currency, value-shrinking ring must score nothing and must say why, in the
    same pass. The ring is chosen inside the search horizon so the ledger has something
    to speak about; without the corpus the same shape is transcribed from the decision
    entry's own example.
    """
    if PATTERNS_FILE.is_file():
        legs = _first_enumerable_refusal(_labelled_cycles(), cycle_settings)
    else:
        legs = _quoted_ring_within_horizon(cycle_settings)
    events = _ring_frame(legs)
    pipeline = load_pipeline_config(REPO_ROOT)
    graph = build_graph(events, pipeline)
    result = evaluate_rules(events, graph, load_rules_settings(REPO_ROOT))
    assert result.near_misses, "the ring vanished: the near-miss ledger is the point of DEV-015"
    reasons = set(result.near_misses[0].reasons)
    assert "value_retention_below_floor" in reasons, reasons
    assert "cross_currency_legs" in reasons, reasons
    assert not any(hit.rule_id == "R4" for hit in result.hits)
    assert set(result.near_misses[0].measurements["currencies"]) == {
        leg.currency for leg in legs
    }
    assert (
        result.near_misses[0].measurements["retention_comparable_across_currencies"] is False
    )


def test_quoted_dev_015_ring_scores_zero_hits_but_names_every_refusal(
    cycle_settings: CycleMemberSettings,
) -> None:
    """DEV-015's own worked example, transcribed, so the test does not need the corpus.

    Yuan 58,702.10 -> Swiss Franc 7,332.87 -> Shekel 26,443.70 -> Canadian Dollar
    10,621.24 -> Rupee 637,140.60 -> Rupee 621,578.18 -> Euro 7,222.58 -> Yen 892,031.21
    -> Australian Dollar 11,364.12 -> back to the first account. Retention against the
    peak is 722258/89203121 = 0.0081, eight distinct currencies appear on consecutive
    legs, and the amounts grow and shrink several times, so every §9 knob fires.

    "Every §9 knob" is measured with the non-increasing requirement on, which is how
    plan §9 states it; the shipped config makes it optional, so the reason it accounts
    for is the one the config would not name, and that difference is asserted rather than
    assumed.
    """
    legs = _dev015_quoted_ring()
    specified = dataclasses.replace(cycle_settings, non_increasing=True)
    reasons = _reasons_for(legs, specified)
    assert "cross_currency_legs" in reasons
    assert "value_retention_below_floor" in reasons
    assert "length_above_max_length" in reasons
    assert "amount_increases_along_loop" in reasons
    # Switching off the one optional knob drops exactly its own reason and nothing else,
    # so the other three refusals are properties of the ring, not of the config.
    assert set(reasons) - set(_reasons_for(legs, cycle_settings)) == {"amount_increases_along_loop"}
    ring = build_loop([leg.dst for leg in legs], legs)
    assert ring.value_retention == pytest.approx(722_258 / 892_031_21, abs=1e-6)
    assert ring.length == 9
    # Amounts that grow between consecutive legs: 733,287 after 5,870,210; 63,714,060
    # after 1,062,124; and 89,203,121 after 722,258. The wrap from the last leg back to
    # the first is not counted -- those two hops are minutes apart in the wrong
    # direction, so comparing them would be comparing a ring's start against its end,
    # not measuring value moving along it.
    assert ring.non_increasing_breaches == 3

    # with every refusal lifted, the same ring is a hit: proof that the reasons are the
    # settings and not an unfindable shape
    lifted = dataclasses.replace(_cycle_settings(0.0, False, False), max_length=9)
    assert _reasons_for(legs, lifted) == ()


def _quoted_ring_within_horizon(settings: CycleMemberSettings) -> tuple[Leg, ...]:
    """DEV-015's quoted example, folded to a ring the search will actually enumerate.

    The quoted ring is 9 hops, past ``max_length + NEAR_MISS_DEPTH_SLACK``, so a run over
    it produces no loop and therefore no near miss. Four of its legs are kept and the
    hop back to the first account carries the quoted Canadian Dollar amount, which leaves
    the shape the corpus ring has -- cross-currency on the way round, value retention
    733,287 / 5,870,210 = 0.12 -- while staying inside the length window.

    The legs are a day apart rather than the quoted three hours because the fixture is
    four accounts wide: an R12 chain walk over three hours of a single ring flags most of
    them and trips the corpus-level hit-rate ceiling, which would refuse the run before
    R4's ledger is ever read. The gap makes the two rules ask different questions, which
    is what the corpus does anyway.
    """
    quoted = _dev015_quoted_ring()
    if len(quoted) < settings.min_length:
        raise AssertionError(f"the quoted example has fewer than {settings.min_length} legs")
    kept = quoted[:3]
    day = 24 * 3_600_000_000
    return (
        *(dataclasses.replace(leg, ts_us=leg.ts_us + index * day) for index, leg in enumerate(kept)),
        Leg(
            src=kept[-1].dst,
            dst=kept[0].src,
            ts_us=kept[-1].ts_us + 3 * day,
            txn_id="quoted-closing",
            amount_minor=1_062_124,
            currency="CAD",
        ),
    )


def _dev015_quoted_ring() -> tuple[Leg, ...]:
    """The example ring, quoted verbatim from DECISIONS.md DEV-015."""
    amounts: list[tuple[int, str]] = [
        (5_870_210, "CNY"),
        (733_287, "CHF"),
        (2_644_370, "ILS"),
        (1_062_124, "CAD"),
        (63_714_060, "INR"),
        (62_157_818, "INR"),
        (722_258, "EUR"),
        (89_203_121, "JPY"),
        (1_136_412, "AUD"),
    ]
    accounts = [f"8013C4{index:03d}" for index in range(len(amounts))]
    stamps = dt.datetime(2022, 9, 1, tzinfo=dt.UTC) + dt.timedelta(hours=2)
    return tuple(
        Leg(
            src=accounts[index],
            dst=accounts[(index + 1) % len(amounts)],
            ts_us=int((stamps + dt.timedelta(hours=index * 3)).timestamp() * 1_000_000),
            txn_id=f"quoted-{index:02d}",
            amount_minor=amounts[index][0],
            currency=amounts[index][1],
        )
        for index in range(len(amounts))
    )

