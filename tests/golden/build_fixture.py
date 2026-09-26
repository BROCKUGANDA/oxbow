#!/usr/bin/env python
"""Deterministic golden fixture generator for the OXBOW P3b rules R1-R12.

Plan SS9 builds this fixture BEFORE the rule code: ~500 hand-built rows with
planted cycles, fan-ins, a structuring ladder, a dormant wake, a pass-through
chain, plus deliberate near-misses that must not fire. The scenario definitions
are hand-authored; randomness (seed 1337) is only cosmetic filler between fresh
one-shot noise accounts, so two runs produce byte-identical output.

Money is int64 minor units everywhere (DECISIONS DEV-005): no float literal
appears in a money position in this file, and every emitted CSV money cell is
plain digits. Timestamps are real UTC instants; local_hour is derived under
Africa/Kampala (UTC+3, no DST, config/pipeline.yaml deployment_timezone) and is
never hand-typed. Account keys are readable planted names because this corpus
is synthetic by construction (DEV-011: PaySim turned out star-shaped, so THIS
fixture is the evidence that the network typologies exist and are detectable).

Design invariant that keeps the near-misses clean: every account lives inside
one amount scale (max/min across its rows below 4x), sends-only or receives-
only outside planted patterns, and quiet-hour rows reach only dedicated
low-count receivers. An account can therefore never fire R8/R9/R6 accidentally
from cross-scenario reuse - the only hits are the ones expected.yaml lists.

Rule parameters mirrored here for plant design only - the engine reads
config/rules.yaml, and the self-check test cross-reads that file to assert this
header has not drifted:

    R1 p=0.80 dt=60min minA=10_000      R7 dormant=30d k=5 win=24h
    R2 k=8 win=24h tau=corpus p25       R8 q=0.40 min_txn=10
    R3 k=8 win=24h                      R9 m=4 recent=7d base=30d
    R4 r=0.6 len 3..6 strict-time       R10 s=0.70 hold=6h
    R5 n=3 win=7d band 0.80..1.00 T     R11 g=0.80 c=6 win=48h
    R6 z=3.0 baseline=30d min_days=5    R12 len>=3 hop<24h decay=0.95

Fixture-level synthetic assumptions (NOT present in config/rules.yaml, stated
in expected.yaml and README rather than silently invented): structuring T is
pinned at 2_500_000 minor for this golden corpus; R6's baseline is the daily
txn count over ACTIVE days within the lookback, a zero MAD means not
significant, and accounts with fewer than min_baseline_days active days are
never scored; R11's "count" is distinct counterparties in W and the first-time
share's denominator is distinct counterparties too; R12's gate is
non-increasing amounts (decay enters severity only); a reversal is typed by
txn_type == REVERSAL.

Usage:  uv run python tests/golden/build_fixture.py
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SEED: int = 1337
CURRENCY = "EUR"
ALT_CURRENCY = "USD"
SOURCE_DATASET = "golden"
RUN_ID = "01JDGGT3NF1X700000000000GN"
BATCH_ID = "301d2f1c1337"
INGESTED_AT = "2026-09-26T00:00:00.000000+00:00"

KAMPALA = ZoneInfo("Africa/Kampala")

COLUMNS: tuple[str, ...] = (
    "txn_id",
    "event_ts_utc",
    "event_date_local",
    "local_hour",
    "txn_type",
    "channel",
    "amount_minor",
    "currency",
    "account_from",
    "account_to",
    "src_balance_before_minor",
    "src_balance_after_minor",
    "dst_balance_before_minor",
    "dst_balance_after_minor",
    "label_is_fraud",
    "label_is_flagged",
    "label_typology",
    "source_dataset",
    "ingested_at",
    "run_id",
    "batch_id",
)

TXN_TYPES: frozenset[str] = frozenset(
    {"CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER", "REVERSAL"}
)
CHANNELS: tuple[str, ...] = ("USSD", "APP", "AGENT", "WEB")

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "transactions.csv"
MANIFEST_PATH = HERE / "build_manifest.json"

H = timedelta(hours=1)
D = timedelta(days=1)
M = timedelta(minutes=1)


def ts(year: int, month: int, day: int, hour_utc: int, minute: int = 0) -> datetime:
    """A UTC instant with zero microseconds. hour_utc is the UTC hour."""
    return datetime(year, month, day, hour_utc, minute, tzinfo=UTC)


def event(
    when: datetime,
    txn_type: str,
    channel: str,
    amount_minor: int,
    account_from: str,
    account_to: str,
    typology: str = "none",
    currency: str = CURRENCY,
    fraud: int = 0,
    flagged: int = 0,
    src_before: int | None = None,
    src_after: int | None = None,
    dst_before: int | None = None,
    dst_after: int | None = None,
) -> dict:
    """One canonical row. Balances default to a delta-CONSISTENT movement.

    The single planted exception (S58) passes all four balance fields
    explicitly, so the balance-delta inconsistency flag is true on exactly
    those rows and false everywhere else.
    """
    if src_before is None:
        src_before = amount_minor + 100_000
        src_after = 100_000
    if dst_before is None:
        dst_before = 50_000
        dst_after = 50_000 + amount_minor
    local = when.astimezone(KAMPALA)
    return {
        "txn_id": "",  # assigned after the global sort
        "event_ts_utc": when.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
        "event_date_local": local.strftime("%Y-%m-%d"),
        "local_hour": local.hour,
        "txn_type": txn_type,
        "channel": channel,
        "amount_minor": amount_minor,
        "currency": currency,
        "account_from": account_from,
        "account_to": account_to,
        "src_balance_before_minor": src_before,
        "src_balance_after_minor": src_after,
        "dst_balance_before_minor": dst_before,
        "dst_balance_after_minor": dst_after,
        "label_is_fraud": fraud,
        "label_is_flagged": flagged,
        "label_typology": typology,
        "source_dataset": SOURCE_DATASET,
        "ingested_at": INGESTED_AT,
        "run_id": RUN_ID,
        "batch_id": BATCH_ID,
    }


# --- planted account families ------------------------------------------------
POOL: tuple[str, ...] = tuple(f"ACC-SMALL-{i:02d}" for i in range(1, 12))  # send 1.5k-6.5k only
RP: tuple[str, ...] = tuple(f"ACC-RECV-{i:02d}" for i in range(1, 13))  # receive 20k-65k only
RPT: tuple[str, ...] = ("ACC-RECV-T01", "ACC-RECV-T02")  # receive 2k-3k payouts
FUND: tuple[str, ...] = tuple(f"ACC-FUND-{i:02d}" for i in range(1, 15))  # one large send each
PAY: tuple[str, ...] = tuple(f"ACC-PAYOUT-{i:02d}" for i in range(1, 8))  # one large receive
EXT_ONLY = "ACC-EXT-001"  # designated external-only counterparty (2 large receives)
WHALE: tuple[str, ...] = tuple(f"ACC-WHALE-{i:02d}" for i in range(1, 10))  # send 150k-280k
RS: tuple[str, ...] = tuple(f"ACC-RSEED-{i:02d}" for i in range(1, 31))  # rail-spread senders
MK: tuple[str, ...] = tuple(f"ACC-MKSEND-{i:02d}" for i in range(1, 21))  # market senders


def s01() -> list[dict]:
    # R1 positive, ratio 0.92: in 500000 @07:00 local (04:00Z), out 460000
    # @07:40 local. 460000 >= 0.80*500000 = 400000, dt 40min <= 60min.
    return [
        event(
            ts(2024, 2, 1, 4, 0),
            "TRANSFER",
            "APP",
            500_000,
            FUND[0],
            "ACC-MULE-01",
            "mule",
            fraud=1,
        ),
        event(
            ts(2024, 2, 1, 4, 40),
            "TRANSFER",
            "APP",
            460_000,
            "ACC-MULE-01",
            EXT_ONLY,
            "mule",
            fraud=1,
        ),
    ]


def s02() -> list[dict]:
    # R1 ratio 0.88 AND R10 cash share 0.88 on the SAME event pair: the
    # planted extraction overlap group (test_overlapping_rules_counted_once).
    return [
        event(
            ts(2024, 2, 1, 6, 0),
            "TRANSFER",
            "USSD",
            1_000_000,
            FUND[1],
            "ACC-MULE-07",
            "mule",
            fraud=1,
        ),
        event(
            ts(2024, 2, 1, 6, 35),
            "CASH_OUT",
            "AGENT",
            880_000,
            "ACC-MULE-07",
            EXT_ONLY,
            "mule",
            fraud=1,
        ),
    ]


def s03() -> list[dict]:
    # R1 ratio 0.85.
    return [
        event(
            ts(2024, 2, 1, 8, 0),
            "TRANSFER",
            "APP",
            300_000,
            FUND[2],
            "ACC-MULE-02",
            "mule",
            fraud=1,
        ),
        event(
            ts(2024, 2, 1, 8, 25),
            "TRANSFER",
            "APP",
            255_000,
            "ACC-MULE-02",
            PAY[0],
            "mule",
            fraud=1,
        ),
    ]


def s04() -> list[dict]:
    # R1 near-miss: 316000/400000 = 0.79 < p=0.80.
    return [
        event(ts(2024, 2, 2, 4, 0), "TRANSFER", "APP", 400_000, FUND[3], "ACC-MULE-04", "none"),
        event(ts(2024, 2, 2, 4, 30), "TRANSFER", "APP", 316_000, "ACC-MULE-04", PAY[1], "none"),
    ]


def s05() -> list[dict]:
    # R1 near-miss: received A=9000 < min_amount_minor=10000 (the 100-EUR floor).
    return [
        event(ts(2024, 2, 2, 5, 0), "TRANSFER", "USSD", 9_000, POOL[0], "ACC-MULE-05", "none"),
        event(ts(2024, 2, 2, 5, 20), "TRANSFER", "USSD", 8_500, "ACC-MULE-05", RPT[1], "none"),
    ]


def s06() -> list[dict]:
    # R1 near-miss: ratio 0.95 but the send is 61min after receipt > dt=60.
    return [
        event(ts(2024, 2, 2, 6, 0), "TRANSFER", "APP", 600_000, FUND[4], "ACC-MULE-06", "none"),
        event(ts(2024, 2, 2, 7, 1), "TRANSFER", "APP", 570_000, "ACC-MULE-06", PAY[2], "none"),
    ]


def s07() -> list[dict]:
    # R10 positive: cash-out 320000+300000=620000 vs inflow 800000 -> share
    # 0.775 >= s=0.70, max hold 4h <= h=6h. First exit is +120min after
    # receipt, so R1 (window 60min) must stay off this account.
    return [
        event(
            ts(2024, 2, 3, 6, 0),
            "TRANSFER",
            "APP",
            800_000,
            FUND[5],
            "ACC-CASH-01",
            "extraction",
            fraud=1,
        ),
        event(
            ts(2024, 2, 3, 8, 0),
            "CASH_OUT",
            "AGENT",
            320_000,
            "ACC-CASH-01",
            PAY[3],
            "extraction",
            fraud=1,
        ),
        event(
            ts(2024, 2, 3, 10, 0),
            "CASH_OUT",
            "AGENT",
            300_000,
            "ACC-CASH-01",
            PAY[4],
            "extraction",
            fraud=1,
        ),
    ]


def s08() -> list[dict]:
    # R10 near-miss: 690000/1000000 = 0.69 < s=0.70.
    return [
        event(ts(2024, 2, 3, 12, 0), "TRANSFER", "APP", 1_000_000, FUND[6], "ACC-CASH-02", "none"),
        event(ts(2024, 2, 3, 15, 0), "CASH_OUT", "AGENT", 690_000, "ACC-CASH-02", PAY[5], "none"),
    ]


def s09() -> list[dict]:
    # R10 near-miss: share 0.90 but held 7h10m > h=6h.
    return [
        event(ts(2024, 2, 3, 20, 0), "TRANSFER", "APP", 1_000_000, FUND[7], "ACC-CASH-03", "none"),
        event(ts(2024, 2, 4, 3, 10), "CASH_OUT", "AGENT", 900_000, "ACC-CASH-03", PAY[6], "none"),
    ]


def s10() -> list[dict]:
    # Cross-currency pair. Correct per-currency pass-through ratios:
    # EUR 200000/500000 = 0.40, USD 450000/700000 = 0.643 -> R1 must NOT fire.
    # A currency-blind pairing (USD out against EUR in) would wrongly see
    # 450000/500000 = 0.90. The USD rows are the fixture's only non-EUR money.
    return [
        event(ts(2024, 2, 5, 7, 0), "TRANSFER", "WEB", 500_000, FUND[8], "ACC-XCUR-01", "none"),
        event(
            ts(2024, 2, 5, 7, 5),
            "TRANSFER",
            "WEB",
            700_000,
            FUND[9],
            "ACC-XCUR-01",
            "none",
            currency=ALT_CURRENCY,
        ),
        event(
            ts(2024, 2, 5, 7, 20),
            "TRANSFER",
            "WEB",
            450_000,
            "ACC-XCUR-01",
            "ACC-XPAY-01",
            "none",
            currency=ALT_CURRENCY,
        ),
        event(
            ts(2024, 2, 5, 7, 25), "TRANSFER", "WEB", 200_000, "ACC-XCUR-01", "ACC-XPAY-02", "none"
        ),
    ]


def s11() -> list[dict]:
    # R2 positive: 9 distinct senders within 24h (>= k=8); amounts
    # 1500..5500, sorted median 3500 < tau. The 20:00 payout to the tiny
    # pool keeps the hub an originator (not external-typed); it is 5h+ after
    # any receipt and 3000 < 0.8*3500, so R1 stays clean on both ends.
    rows = [
        event(
            ts(2024, 2, 10, 5, 30) + H * i + M * (7 * i),
            "CASH_IN",
            "USSD",
            1_500 + 500 * i,
            POOL[i],
            "ACC-GATHER-01",
            "gather",
            fraud=1,
        )
        for i in range(9)
    ]
    rows.append(
        event(
            ts(2024, 2, 10, 20, 0),
            "TRANSFER",
            "APP",
            3_000,
            "ACC-GATHER-01",
            RPT[1],
            "gather",
            fraud=1,
        )
    )
    return rows


def s12() -> list[dict]:
    # R2 severity peer: 11 distinct senders (median 4000) -> same rule,
    # higher severity than S11.
    rows = [
        event(
            ts(2024, 2, 11, 4, 0) + H * i,
            "CASH_IN",
            "USSD",
            1_500 + 500 * i,
            POOL[i],
            "ACC-GATHER-BIG",
            "gather",
            fraud=1,
        )
        for i in range(11)
    ]
    rows.append(
        event(
            ts(2024, 2, 11, 18, 0),
            "TRANSFER",
            "APP",
            3_000,
            "ACC-GATHER-BIG",
            RPT[1],
            "gather",
            fraud=1,
        )
    )
    return rows


def s13() -> list[dict]:
    # R2 near-miss: 7 distinct senders < k=8. Six are returning customers
    # (history spread Jan 10-20, at most 2 new pairings per 48h so the
    # history itself is not an R11 surge) - window fresh share 1/7.
    base = ts(2024, 1, 10, 6, 0)
    rows = [
        event(
            base + 2 * D * i, "CASH_IN", "USSD", 2_500 + 300 * i, POOL[i], "ACC-GATHER-NM7", "none"
        )
        for i in range(6)
    ]
    win = ts(2024, 2, 12, 5, 0)
    rows += [
        event(win + H * i, "CASH_IN", "USSD", 1_500 + 500 * i, POOL[i], "ACC-GATHER-NM7", "none")
        for i in range(7)
    ]
    rows.append(
        event(ts(2024, 2, 12, 13, 0), "TRANSFER", "APP", 2_000, "ACC-GATHER-NM7", RPT[0], "none")
    )
    return rows


def s14() -> list[dict]:
    # R2 near-miss: 9 distinct senders exist but every 24h window holds at
    # most 7 (2 @h0, 5 @h13, 2 @h26). Four returning senders keep the R11
    # first-time share at 5/9 < g=0.80.
    base = ts(2024, 1, 21, 7, 0)
    rows = [
        event(base + H * i, "CASH_IN", "USSD", 3_000 + 400 * i, POOL[i], "ACC-GATHER-NMW", "none")
        for i in range(4)
    ]
    win = ts(2024, 2, 13, 6, 0)
    rows += [
        event(win, "CASH_IN", "USSD", 2_000, POOL[0], "ACC-GATHER-NMW", "none"),
        event(win + 30 * M, "CASH_IN", "USSD", 2_200, POOL[1], "ACC-GATHER-NMW", "none"),
    ]
    mid = win + 13 * H
    rows += [
        event(mid + i * M, "CASH_IN", "USSD", 2_400 + 100 * i, POOL[i], "ACC-GATHER-NMW", "none")
        for i in range(2, 7)
    ]
    late = win + 26 * H
    rows += [
        event(late, "CASH_IN", "USSD", 2_900, POOL[7], "ACC-GATHER-NMW", "none"),
        event(late + 20 * M, "CASH_IN", "USSD", 3_100, POOL[8], "ACC-GATHER-NMW", "none"),
    ]
    rows.append(
        event(ts(2024, 2, 14, 12, 0), "TRANSFER", "APP", 2_000, "ACC-GATHER-NMW", RPT[1], "none")
    )
    return rows


def s15() -> list[dict]:
    # R2 near-miss: 9 distinct senders inside 24h but median 240000 >= tau.
    # All nine whales transacted with this hub before (history spread
    # Jan 10-26, <=2 new pairings per 48h), so the R11 share is 0 on the
    # window day.
    hist = ts(2024, 1, 10, 8, 0)
    rows = [
        event(
            hist + 2 * D * i,
            "TRANSFER",
            "WEB",
            150_000 + 2_000 * i,
            WHALE[i],
            "ACC-GATHER-NMT",
            "none",
        )
        for i in range(9)
    ]
    rows += [
        event(
            ts(2024, 2, 14, 6, 0) + H * i,
            "TRANSFER",
            "WEB",
            200_000 + 10_000 * i,
            WHALE[i],
            "ACC-GATHER-NMT",
            "none",
        )
        for i in range(9)
    ]
    rows.append(
        event(ts(2024, 2, 15, 9, 0), "TRANSFER", "APP", 45_000, "ACC-GATHER-NMT", RP[2], "none")
    )
    return rows


def s16() -> list[dict]:
    # R3 positive: 9 distinct receivers inside 24h (>= k=8). Receivers are
    # mid-scale RP accounts (also scatter targets elsewhere - one txn per
    # day keeps their own velocity/fan arithmetic inert). The hub sends only,
    # so it is an originator and cannot be external-typed.
    return [
        event(
            ts(2024, 2, 20, 8, 0) + H * i + M * (11 * i),
            "TRANSFER",
            "APP",
            30_000 + 2_000 * i,
            "ACC-SCATTER-01",
            RP[i],
            "scatter",
            fraud=1,
        )
        for i in range(9)
    ]


def s17() -> list[dict]:
    # R3 severity peer: 11 distinct receivers -> higher severity than S16.
    return [
        event(
            ts(2024, 2, 21, 8, 0) + H * i,
            "PAYMENT",
            "USSD",
            25_000 + 1_000 * i,
            "ACC-SCATTER-BIG",
            RP[i],
            "scatter",
            fraud=1,
        )
        for i in range(11)
    ]


def s18() -> list[dict]:
    # R3 near-miss: 7 distinct receivers < k=8, six returning (history
    # Jan 28-Feb 6, <=2 new pairings per 48h; also keeps the R7 gap at 16
    # days).
    hist = ts(2024, 1, 28, 5, 0)
    rows = [
        event(
            hist + 36 * H * i,
            "PAYMENT",
            "WEB",
            40_000 + 1_500 * i,
            "ACC-SCATTER-NM7",
            RP[i],
            "none",
        )
        for i in range(6)
    ]
    win = ts(2024, 2, 22, 6, 0)
    rows += [
        event(win + H * i, "PAYMENT", "WEB", 40_000 + 1_500 * i, "ACC-SCATTER-NM7", RP[i], "none")
        for i in range(7)
    ]
    return rows


def s19() -> list[dict]:
    # R4 positive, 4-node time-respecting cycle. Strictly increasing stamps
    # (0/70/140/210 min). Retention 850000/1000000 = 0.85 >= r=0.6 (and
    # min/max = 850000/1050000 = 0.81, also >= 0.6). Length 4 in [3,6].
    # Amounts bump twice (1020000 > 1000000, 1050000 > 1020000) so NO
    # rotation contains three consecutive non-increasing edges: the loop is
    # an R4 hit and an R12 non-hit, cleanly. Every hop is 70min > dt=60min,
    # so R1 must not fire either.
    a = ts(2024, 3, 1, 9, 0)
    return [
        event(a, "TRANSFER", "APP", 1_000_000, "ACC-CYCA-1", "ACC-CYCA-2", "cycle", fraud=1),
        event(
            a + 70 * M, "TRANSFER", "APP", 1_020_000, "ACC-CYCA-2", "ACC-CYCA-3", "cycle", fraud=1
        ),
        event(
            a + 140 * M, "TRANSFER", "APP", 1_050_000, "ACC-CYCA-3", "ACC-CYCA-4", "cycle", fraud=1
        ),
        event(
            a + 210 * M, "TRANSFER", "APP", 850_000, "ACC-CYCA-4", "ACC-CYCA-1", "cycle", fraud=1
        ),
    ]


def s20() -> list[dict]:
    # R4 positive, 3-node cycle. Retention 670000/700000 = 0.957 (min/max =
    # 670000/720000 = 0.931). The 720000 hop blocks every >=3-hop
    # non-increasing subchain (700 then 720 increases; 720, 670 then 700
    # increases); 2h hops block R1.
    a = ts(2024, 3, 3, 10, 0)
    return [
        event(a, "TRANSFER", "APP", 700_000, "ACC-CYCB-1", "ACC-CYCB-2", "cycle", fraud=1),
        event(a + 2 * H, "TRANSFER", "APP", 720_000, "ACC-CYCB-2", "ACC-CYCB-3", "cycle", fraud=1),
        event(a + 4 * H, "TRANSFER", "APP", 670_000, "ACC-CYCB-3", "ACC-CYCB-1", "cycle", fraud=1),
    ]


def s21() -> list[dict]:
    # R4 near-miss: retention 590000/1000000 = 0.59 < r=0.60 (min/max =
    # 590000/1200000 = 0.49, also below). 2h hops keep R1 out; every
    # sub-chain dies on the 1200000 bump or the closing increase, so R12 is
    # clean too.
    a = ts(2024, 3, 4, 8, 0)
    return [
        event(a, "TRANSFER", "APP", 1_000_000, "ACC-CYCN-1", "ACC-CYCN-2", "none"),
        event(a + 2 * H, "TRANSFER", "APP", 1_200_000, "ACC-CYCN-2", "ACC-CYCN-3", "none"),
        event(a + 4 * H, "TRANSFER", "APP", 590_000, "ACC-CYCN-3", "ACC-CYCN-1", "none"),
    ]


def s22() -> list[dict]:
    # R4 near-miss: 7-node loop, retention 700000/1000000 = 0.70, strictly
    # increasing times - but length 7 > max_length=6, so R4 must not fire.
    # Amounts bump at e2, e4 and e6 (and e1 > e7 closes the wrap), so every
    # cyclic run has an increase within three edges: no rotation is a legal
    # >=3-hop non-increasing chain either. This scenario fires NOTHING.
    # 75min hops keep R1 out.
    a = ts(2024, 3, 5, 6, 0)
    amounts = [1_000_000, 1_050_000, 900_000, 950_000, 800_000, 830_000, 700_000]
    return [
        event(
            a + timedelta(minutes=75 * i),
            "TRANSFER",
            "WEB",
            amounts[i],
            f"ACC-CYCL7-{i + 1}",
            f"ACC-CYCL7-{(i + 1) % 7 + 1}",
            "none",
        )
        for i in range(7)
    ]


def s23() -> list[dict]:
    # R4 time-reversed loop: edges A->B 12:00Z, B->C 11:00Z, C->A 10:00Z.
    # Start A: 12:00 then 11:00 (dies); start B: 11:00 then 10:00 (dies);
    # start C: 10:00, 12:00, then 11:00 (dies). Time-respecting enumeration
    # must return nothing. Max 2 increasing hops also keep R12 clean; the
    # 750000->780000 pair is time-blocked for R1.
    return [
        event(
            ts(2024, 3, 6, 10, 0), "TRANSFER", "APP", 780_000, "ACC-CYCT-3", "ACC-CYCT-1", "none"
        ),
        event(
            ts(2024, 3, 6, 11, 0), "TRANSFER", "APP", 750_000, "ACC-CYCT-2", "ACC-CYCT-3", "none"
        ),
        event(
            ts(2024, 3, 6, 12, 0), "TRANSFER", "APP", 700_000, "ACC-CYCT-1", "ACC-CYCT-2", "none"
        ),
    ]


def s24() -> list[dict]:
    # Legitimate recurring cycle PAY->EMP->MERCH->SUPP->PAY, lap starts
    # exactly 168h apart (periodic_period_hours=168). Satisfies R4's raw
    # arithmetic (strictly increasing stamps, retention 786000/800000 =
    # 0.9825 >= 0.6) but must be DOWN-WEIGHTED (periodic_down_weight=0.3),
    # not suppressed. Every >=3-hop sub-chain hits an increase (the 810000
    # hop, or 786->800 across laps), so R12 stays out; intra-lap gaps >= 10h
    # keep R1 out.
    rows = []
    for t0 in (ts(2024, 3, 4, 8, 0), ts(2024, 3, 11, 8, 0), ts(2024, 3, 18, 8, 0)):
        rows += [
            event(t0, "PAYMENT", "WEB", 800_000, "ACC-PAY-01", "ACC-EMP-01", "payroll_recurring"),
            event(
                t0 + 10 * H,
                "PAYMENT",
                "WEB",
                790_000,
                "ACC-EMP-01",
                "ACC-MERCH-01",
                "payroll_recurring",
            ),
            event(
                t0 + 30 * H,
                "PAYMENT",
                "WEB",
                810_000,
                "ACC-MERCH-01",
                "ACC-SUPP-01",
                "payroll_recurring",
            ),
            event(
                t0 + 50 * H,
                "PAYMENT",
                "WEB",
                786_000,
                "ACC-SUPP-01",
                "ACC-PAY-01",
                "payroll_recurring",
            ),
        ]
    return rows


def s25() -> list[dict]:
    # The ONLY self-transfer in the corpus: excluded from R4/R2/R3 by
    # guards.exclude_self_loops_from; contributes to self_transfer_count.
    return [
        event(
            ts(2024, 3, 7, 9, 0),
            "TRANSFER",
            "APP",
            45_000,
            "ACC-SELF-01",
            "ACC-SELF-01",
            "self_transfer",
        )
    ]


def s26() -> list[dict]:
    # Reversal pair, typed REVERSAL 3h after the original. Excluded from
    # cycle/chain/pass-through by guards; the residual A->B->A loop is also
    # only 2 nodes < min_length=3. Nothing must fire.
    return [
        event(ts(2024, 3, 9, 10, 0), "TRANSFER", "APP", 400_000, "ACC-RA-1", "ACC-RA-2", "none"),
        event(
            ts(2024, 3, 9, 13, 0), "REVERSAL", "APP", 400_000, "ACC-RA-2", "ACC-RA-1", "reversal"
        ),
    ]


def s27() -> list[dict]:
    # Reversal triangle: P->Q, Q->R, R->P as REVERSAL. Read as ordinary
    # transfers that is a valid 3-cycle (retention 600000/600000 = 1.00) and
    # a legal 3-hop non-increasing chain; with the reversal exclusion
    # (guards.exclude_reversals_from covers R4/R12/R1) BOTH must return
    # nothing. 90min hops also keep plain R1 out.
    a = ts(2024, 3, 10, 8, 0)
    return [
        event(a, "TRANSFER", "WEB", 600_000, "ACC-RV-P", "ACC-RV-Q", "none"),
        event(a + 90 * M, "TRANSFER", "WEB", 600_000, "ACC-RV-Q", "ACC-RV-R", "none"),
        event(a + 3 * H, "REVERSAL", "WEB", 600_000, "ACC-RV-R", "ACC-RV-P", "reversal"),
    ]


def s28() -> list[dict]:
    # R12 positive pass-through chain: 4 hops, amounts 1000000 -> 950000 ->
    # 902500 -> 857375. Every hop ratio is exactly 0.95 (the decay bound with
    # equality), the amounts are non-increasing, hops are 6h < dt=24h, and
    # every node originates its forward edge, so all five are originators.
    # The chain fires under the non-increasing gate AND under a strict
    # decay-gate reading - interpretation-proof. R1 stays off because every
    # hop is 6h > 60min despite each send being >= 0.8 * the receipt.
    a = ts(2024, 3, 1, 6, 0)
    amounts = [1_000_000, 950_000, 902_500, 857_375]
    return [
        event(
            a + 6 * H * i,
            "TRANSFER",
            "APP",
            amounts[i],
            f"ACC-CHN-{i + 1}",
            f"ACC-CHN-{i + 2}",
            "chain",
            fraud=1,
        )
        for i in range(4)
    ]


def s29() -> list[dict]:
    # R12 near-miss: the 600000 hop breaks non-increasing. 2h hops keep R1
    # out even though 600000 >= 0.8*500000.
    a = ts(2024, 3, 2, 6, 0)
    return [
        event(a, "TRANSFER", "APP", 500_000, "ACC-CHNX-1", "ACC-CHNX-2", "none"),
        event(a + 2 * H, "TRANSFER", "APP", 600_000, "ACC-CHNX-2", "ACC-CHNX-3", "none"),
        event(a + 4 * H, "TRANSFER", "APP", 400_000, "ACC-CHNX-3", "ACC-CHNX-4", "none"),
    ]


def s30() -> list[dict]:
    # R12 near-miss: non-increasing 3 hops but every hop spans 30h > dt=24h.
    a = ts(2024, 3, 3, 6, 0)
    return [
        event(a, "TRANSFER", "APP", 400_000, "ACC-CHNS-1", "ACC-CHNS-2", "none"),
        event(a + 30 * H, "TRANSFER", "APP", 390_000, "ACC-CHNS-2", "ACC-CHNS-3", "none"),
        event(a + 60 * H, "TRANSFER", "APP", 380_000, "ACC-CHNS-3", "ACC-CHNS-4", "none"),
    ]


def s31() -> list[dict]:
    # R12 near-miss: 2 hops < min_length=3; the 0.79 ratio and 30min gap
    # are also just under R1's floor, so R1 must not fire either.
    a = ts(2024, 3, 4, 6, 0)
    return [
        event(a, "TRANSFER", "APP", 1_000_000, "ACC-CHNT-1", "ACC-CHNT-2", "none"),
        event(a + 30 * M, "TRANSFER", "APP", 790_000, "ACC-CHNT-2", "ACC-CHNT-3", "none"),
    ]


def s32() -> list[dict]:
    # Structural decoy: a clean 2-hop sub-chain 90min apart. Under the
    # non-increasing gate it never reaches min_length=3; the 90min hop also
    # keeps it outside R1's inclusive 60min window by 30 minutes.
    a = ts(2024, 3, 1, 6, 30)
    return [
        event(a, "TRANSFER", "USSD", 990_000, "ACC-CHND-1", "ACC-CHND-2", "none"),
        event(a + 90 * M, "TRANSFER", "USSD", 985_000, "ACC-CHND-2", "ACC-CHND-3", "none"),
    ]


def s33() -> list[dict]:
    # R5 positive, ladder A: 4 withdrawals in 7d inside 80-100% of
    # T=2500000 (band 2000000..2500000) -> count 4 >= n=3, fires. The same
    # -day 15:00 deposits are >6h from each withdrawal and precede the next
    # day's withdrawal by 18h, so R1 (60min) and R10 (6h holding) stay out.
    day = ts(2024, 2, 20, 9, 0)
    rows = []
    for i, amt in enumerate([2_450_000, 2_300_000, 2_100_000, 2_455_000]):
        rows.append(
            event(
                day + i * D,
                "CASH_OUT",
                "AGENT",
                amt,
                "ACC-SMURF-A",
                "ACC-AGENT-S1",
                "structuring",
                fraud=1,
            )
        )
        rows.append(
            event(
                day + i * D + 6 * H,
                "TRANSFER",
                "APP",
                3_000_000,
                FUND[10 + i],
                "ACC-SMURF-A",
                "none",
            )
        )
    return rows


def s34() -> list[dict]:
    # R5 positive, ladder B: exactly 3 in band within 7d -> fires at lower
    # severity than ladder A (count above n is the severity dimension).
    day = ts(2024, 3, 1, 9, 0)
    return [
        event(
            day + i * 48 * H,
            "CASH_OUT",
            "AGENT",
            2_200_000 + 50_000 * i,
            "ACC-SMURF-B",
            "ACC-AGENT-S1",
            "structuring",
        )
        for i in range(3)
    ]


def s35() -> list[dict]:
    # R5 near-miss: 2 in band plus one at 1975000 = 79.0% of T (below
    # band_low 0.80) -> in-band count 2 < n=3.
    day = ts(2024, 2, 26, 9, 0)
    return [
        event(day, "CASH_OUT", "AGENT", 2_050_000, "ACC-SMURF-NM1", "ACC-AGENT-S1", "none"),
        event(day + D, "CASH_OUT", "AGENT", 2_480_000, "ACC-SMURF-NM1", "ACC-AGENT-S1", "none"),
        event(day + 2 * D, "CASH_OUT", "AGENT", 1_975_000, "ACC-SMURF-NM1", "ACC-AGENT-S1", "none"),
    ]


def s36() -> list[dict]:
    # R5 near-miss: 3 in-band amounts on days 0/5/10 - every 7d window sees
    # at most 2 < n=3.
    day = ts(2024, 3, 2, 9, 0)
    return [
        event(
            day + d * D,
            "CASH_OUT",
            "AGENT",
            2_150_000 + 20_000 * i,
            "ACC-SMURF-NM2",
            "ACC-AGENT-S1",
            "none",
        )
        for i, d in enumerate([0, 5, 10])
    ]


def s37() -> list[dict]:
    # R6 positive. Active-day counts Mar 25..Apr 2: (1,2,3,2,1,3,2,1) ->
    # median 2, MAD 1, sigma = 1.4826. Spike Apr 3: 12 txns to ONE
    # counterparty -> z = (12-2)/1.4826 = 6.74 >= 3.0 fires (raw-MAD z = 10,
    # also fires). The counterparty sees the identical daily pattern from
    # the receiving side, so R6 legitimately fires on BOTH accounts -
    # expected.yaml says so. One counterparty keeps R2/R3 off (distinct 1).
    counts = [1, 2, 3, 2, 1, 3, 2, 1]
    rows = []
    for d, c in enumerate(counts):
        for j in range(c):
            rows.append(
                event(
                    ts(2024, 3, 25, 7, 0) + d * D + M * j,
                    "PAYMENT",
                    "USSD",
                    25_000 + 500 * j,
                    "ACC-VEL-01",
                    "ACC-VEL-B1",
                    "none",
                )
            )
    rows += [
        event(
            ts(2024, 4, 3, 7, 0) + M * j,
            "TRANSFER",
            "USSD",
            25_000,
            "ACC-VEL-01",
            "ACC-VEL-B1",
            "velocity",
            fraud=1,
        )
        for j in range(12)
    ]
    return rows


def s38() -> list[dict]:
    # R6 near-miss: same baseline, spike-day count 4 -> z = (4-2)/1.4826 =
    # 1.35 (raw 2.0), below 3.0 under BOTH conventions.
    counts = [1, 2, 3, 2, 1, 3, 2, 1]
    rows = []
    for d, c in enumerate(counts):
        for j in range(c):
            rows.append(
                event(
                    ts(2024, 3, 26, 8, 0) + d * D + M * j,
                    "PAYMENT",
                    "USSD",
                    26_000 + 500 * j,
                    "ACC-VEL-NM1",
                    "ACC-VEL-B2",
                    "none",
                )
            )
    rows += [
        event(
            ts(2024, 4, 3, 8, 0) + M * j,
            "TRANSFER",
            "USSD",
            26_000,
            "ACC-VEL-NM1",
            "ACC-VEL-B2",
            "none",
        )
        for j in range(4)
    ]
    return rows


def s39() -> list[dict]:
    # R6 near-miss: the spike-day count 10 would be significant if scored
    # (z = (10-1)/1.4826 = 6.07), but the account has only 3 active baseline
    # days < min_baseline_days=5 -> the rule is never evaluated.
    rows = [
        event(
            ts(2024, 3, 29, 7, 0), "PAYMENT", "USSD", 21_000, "ACC-VEL-NM2", "ACC-VEL-B3", "none"
        ),
        event(
            ts(2024, 3, 30, 7, 0), "PAYMENT", "USSD", 21_500, "ACC-VEL-NM2", "ACC-VEL-B3", "none"
        ),
        event(
            ts(2024, 3, 31, 7, 0), "PAYMENT", "USSD", 22_000, "ACC-VEL-NM2", "ACC-VEL-B3", "none"
        ),
    ]
    rows += [
        event(
            ts(2024, 4, 1, 7, 0) + M * j,
            "TRANSFER",
            "USSD",
            22_000,
            "ACC-VEL-NM2",
            "ACC-VEL-B3",
            "none",
        )
        for j in range(10)
    ]
    return rows


def s40() -> list[dict]:
    # R7 positive: last pre-txn Jan 5, first post-txn Feb 9 04:00Z.
    # Jan 5 -> Feb 9 = 35 calendar days apart, 34 full inactive days, both
    # readings >= d=30. Then 6 txns in 20h >= k=5. Two of six are returning
    # (RP[0], RP[1]) and four dedicated new receivers: fresh share 4/6 =
    # 0.667 < g=0.80 and distinct counterparties 6 -> R11 stays out;
    # receivers in one 24h window = 6 < k=8 -> R3 stays out; all 9 txns are
    # daytime (local 10-23) so R8 stays out; 3 pre-active days <
    # min_baseline_days=5 (and all three fall outside the 30-day lookback
    # opened by Feb 9) so R6 stays out.
    rows = [
        event(ts(2024, 1, 3, 7, 0), "PAYMENT", "USSD", 30_000, "ACC-SLEEP-01", RP[0], "none"),
        event(ts(2024, 1, 4, 7, 0), "PAYMENT", "USSD", 31_000, "ACC-SLEEP-01", RP[1], "none"),
        event(ts(2024, 1, 5, 7, 0), "PAYMENT", "USSD", 32_000, "ACC-SLEEP-01", RP[2], "none"),
    ]
    morning = ts(2024, 2, 9, 4, 0)
    rows += [
        event(
            morning + 3 * H * j,
            "TRANSFER",
            "USSD",
            amt,
            "ACC-SLEEP-01",
            dst,
            "dormant_wake",
            fraud=1,
        )
        for j, (dst, amt) in enumerate(
            [
                (RP[0], 25_000),
                (RP[1], 26_000),
                ("ACC-SL-N01", 27_000),
                ("ACC-SL-N02", 28_000),
                ("ACC-SL-N03", 29_000),
                ("ACC-SL-N04", 30_000),
            ]
        )
    ]
    return rows


def s41() -> list[dict]:
    # R7 positive severity peer: Jan 4 -> Apr 4 = 91 calendar days dormant
    # (90 full), same k=6 -> same rule, higher severity than S40 (dormancy
    # length is the severity dimension; k ties).
    rows = [
        event(ts(2024, 1, 3, 9, 0), "PAYMENT", "USSD", 33_000, "ACC-SLEEP-B", RP[7], "none"),
        event(ts(2024, 1, 4, 9, 0), "PAYMENT", "USSD", 34_000, "ACC-SLEEP-B", RP[8], "none"),
    ]
    morning = ts(2024, 4, 4, 5, 0)
    rows += [
        event(
            morning + 3 * H * j,
            "TRANSFER",
            "USSD",
            amt,
            "ACC-SLEEP-B",
            dst,
            "dormant_wake",
            fraud=1,
        )
        for j, (dst, amt) in enumerate(
            [
                (RP[7], 24_000),
                (RP[8], 25_000),
                ("ACC-SLB-N01", 26_000),
                ("ACC-SLB-N02", 27_000),
                ("ACC-SLB-N03", 28_000),
                ("ACC-SLB-N04", 29_000),
            ]
        )
    ]
    return rows


def s42() -> list[dict]:
    # R7 near-miss: Jan 5 -> Feb 3 = 29 calendar days (28 full) < d=30.
    # k=6 is satisfied; only the gap saves it. Fresh share 4/6 < g too.
    rows = [
        event(ts(2024, 1, 4, 6, 0), "PAYMENT", "USSD", 34_000, "ACC-SLEEP-NM29", RP[9], "none"),
        event(ts(2024, 1, 5, 6, 0), "PAYMENT", "USSD", 35_000, "ACC-SLEEP-NM29", RP[10], "none"),
    ]
    morning = ts(2024, 2, 3, 5, 0)
    rows += [
        event(morning + 3 * H * j, "TRANSFER", "USSD", amt, "ACC-SLEEP-NM29", dst, "none")
        for j, (dst, amt) in enumerate(
            [
                (RP[9], 23_000),
                (RP[10], 24_000),
                ("ACC-S29-N01", 25_000),
                ("ACC-S29-N02", 26_000),
                ("ACC-S29-N03", 27_000),
                ("ACC-S29-N04", 28_000),
            ]
        )
    ]
    return rows


def s43() -> list[dict]:
    # R7 near-miss: 39 calendar days dormant (>= d) but only 4 txns in 24h
    # < k=5.
    rows = [event(ts(2024, 1, 6, 6, 0), "PAYMENT", "USSD", 36_000, "ACC-SLEEP-NM4", RP[11], "none")]
    morning = ts(2024, 2, 15, 6, 0)
    rows += [
        event(
            morning + 3 * H * j,
            "TRANSFER",
            "USSD",
            22_000 + 1_000 * j,
            "ACC-SLEEP-NM4",
            dst,
            "none",
        )
        for j, dst in enumerate([RP[11], "ACC-S4-N01", "ACC-S4-N02", "ACC-S4-N03"])
    ]
    return rows


def s44() -> list[dict]:
    # R8 positive. Baseline Mar 1-6: 6 txns, all local hour 10 (07:00Z).
    # Recent Mar 20-24: 10 txns, 8 at local hours 0-1 (the account's own
    # zero-volume quietest hours). Quiet share 0.80 - 0.00 = 0.80 >= q=0.40,
    # recent count 10 >= min 10 -> fires. Receivers are four dedicated
    # accounts holding <= 5 txns each, so no receiver inherits an R8 hit,
    # and they are all baseline counterparties, so the R11 share is 0.
    rows = [
        event(
            ts(2024, 3, d, 7, 0),
            "PAYMENT",
            "USSD",
            21_000 + 500 * d,
            "ACC-NIGHT-01",
            f"ACC-NR-{(d % 3) + 1:02d}",
            "none",
        )
        for d in range(1, 7)
    ]
    for night in range(2):
        for j in range(4):
            rows.append(
                event(
                    ts(2024, 3, 20 + night, 21, 0) + 30 * M * j,
                    "TRANSFER",
                    "USSD",
                    20_000 + 400 * j,
                    "ACC-NIGHT-01",
                    f"ACC-NR-{(j % 4) + 1:02d}",
                    "odd_hours",
                    fraud=1,
                )
            )
    rows += [
        event(
            ts(2024, 3, 24, 7, 0), "PAYMENT", "USSD", 24_000, "ACC-NIGHT-01", "ACC-NR-01", "none"
        ),
        event(
            ts(2024, 3, 24, 7, 40), "PAYMENT", "USSD", 24_500, "ACC-NIGHT-01", "ACC-NR-02", "none"
        ),
    ]
    return rows


def s45() -> list[dict]:
    # R8 near-miss: recent quiet-hour share 3/10 = 0.30 < q=0.40.
    rows = [
        event(
            ts(2024, 3, d, 7, 30),
            "PAYMENT",
            "USSD",
            22_000 + 500 * d,
            "ACC-NIGHT-NM",
            "ACC-NNM-01",
            "none",
        )
        for d in range(1, 6)
    ]
    rows += [
        event(
            ts(2024, 3, 21, 0, 0) + 2 * H * j,
            "TRANSFER",
            "USSD",
            21_000,
            "ACC-NIGHT-NM",
            "ACC-NNM-02",
            "none",
        )
        for j in range(3)
    ]
    rows += [
        event(
            ts(2024, 3, 22, 7, 0) + 2 * H * j,
            "PAYMENT",
            "USSD",
            21_500,
            "ACC-NIGHT-NM",
            "ACC-NNM-03",
            "none",
        )
        for j in range(3)
    ]
    rows += [
        event(
            ts(2024, 3, 23, 7, 0) + 2 * H * j,
            "PAYMENT",
            "USSD",
            22_000,
            "ACC-NIGHT-NM",
            "ACC-NNM-04",
            "none",
        )
        for j in range(2)
    ]
    return rows


def s46() -> list[dict]:
    # R8 near-miss: the night jump is a full 1.0 but the account has 9
    # lifetime txns (recent 5 < min_transactions 10) -> must not fire.
    rows = [
        event(
            ts(2024, 3, d, 7, 0), "PAYMENT", "USSD", 23_000, "ACC-NIGHT-FEW", "ACC-NFW-01", "none"
        )
        for d in range(1, 5)
    ]
    rows += [
        event(
            ts(2024, 3, 20, 22, 0) + H * j,
            "TRANSFER",
            "USSD",
            23_000,
            "ACC-NIGHT-FEW",
            "ACC-NFW-02",
            "odd_hours",
        )
        for j in range(5)
    ]
    return rows


def s47() -> list[dict]:
    # Local-hour guard (test_odd_hour_uses_local_hour): 10 txns at UTC hour
    # 05 = local hour 08 in Kampala, ordinary daytime. A rule reading
    # local_hour sees nothing; one reading the UTC hour would see a "5am"
    # cluster. Must not fire.
    return [
        event(
            ts(2024, 3, d, 5, 0),
            "PAYMENT",
            "USSD",
            24_000 + 300 * d,
            "ACC-DAWN-01",
            f"ACC-DRW-{(d % 3) + 1:02d}",
            "none",
        )
        for d in range(1, 11)
    ]


def s48() -> list[dict]:
    # R9 positive: prior-30d median 58500 (Mar 1-15, eight txns 55000..
    # 62000), recent-7d median 282500 (Apr 2-7: 270000..295000): ratio 4.83
    # > m=4 -> fires. Daily counts stay at 1, so R6 sees median 1 / MAD 0 ->
    # not significant. Receivers are scale-matched dedicated families.
    rows = [
        event(
            ts(2024, 3, 1, 6, 0) + 2 * D * d,
            "PAYMENT",
            "WEB",
            55_000 + 1_000 * d,
            "ACC-REGIME-01",
            f"ACC-RGA-{(d % 4) + 1:02d}",
            "none",
        )
        for d in range(8)
    ]
    rows += [
        event(
            ts(2024, 4, 2, 6, 0) + D * d,
            "TRANSFER",
            "WEB",
            270_000 + 5_000 * d,
            "ACC-REGIME-01",
            f"ACC-RGB-{(d % 4) + 1:02d}",
            "regime_shift",
        )
        for d in range(6)
    ]
    return rows


def s49() -> list[dict]:
    # R9 near-miss: recent median 220000 vs baseline 59750 -> 3.68 < 4.
    rows = [
        event(
            ts(2024, 3, 5, 7, 0) + 2 * D * d,
            "PAYMENT",
            "WEB",
            58_000 + 500 * d,
            "ACC-REGIME-NM",
            f"ACC-RGA-{(d % 4) + 1:02d}",
            "none",
        )
        for d in range(8)
    ]
    rows += [
        event(
            ts(2024, 4, 2, 7, 0) + D * d,
            "TRANSFER",
            "WEB",
            210_000 + 5_000 * d,
            "ACC-REGIME-NM",
            f"ACC-RGN-{(d % 4) + 1:02d}",
            "none",
        )
        for d in range(5)
    ]
    return rows


def s50() -> list[dict]:
    # R11 positive: Feb 1 08:00Z .. Feb 2 14:00Z (inside 48h) eight txns to
    # eight first-time counterparties: share 8/8 = 1.0 >= g=0.80 and
    # distinct count 8 >= c=6 -> fires. 6h spacing means the busiest 24h
    # window holds 5 < k=8, so R3 stays out. Two Jan payments to RP[8] give
    # the account history and mid-scale continuity.
    rows = [
        event(ts(2024, 1, 2, 8, 0), "PAYMENT", "APP", 45_000, "ACC-NEO-01", RP[8], "none"),
        event(ts(2024, 1, 10, 8, 0), "PAYMENT", "APP", 46_000, "ACC-NEO-01", RP[8], "none"),
    ]
    rows += [
        event(
            ts(2024, 2, 1, 8, 0) + 6 * H * j,
            "TRANSFER",
            "APP",
            40_000 + 500 * j,
            "ACC-NEO-01",
            f"ACC-NEO-N{j + 1:02d}",
            "new_counterparty",
            fraud=1,
        )
        for j in range(8)
    ]
    return rows


def s51() -> list[dict]:
    # R11 near-miss: SIX distinct counterparties inside 48h (count >= c
    # satisfied) but only four first-time: share 4/6 = 0.667 < g=0.80 -> no
    # fire. The busiest 24h window holds 5 distinct receivers < k=8, so R3
    # stays out. Five payments go to the two returning accounts.
    rows = [
        event(
            ts(2024, 1, 5, 9, 0) + D * i,
            "PAYMENT",
            "USSD",
            30_000,
            "ACC-NEO-02",
            RP[8 + (i % 2)],
            "none",
        )
        for i in range(4)
    ]
    win = ts(2024, 2, 5, 6, 0)
    rows += [
        event(
            win + timedelta(hours=7 * j),
            "TRANSFER",
            "USSD",
            31_000,
            "ACC-NEO-02",
            RP[8 + (j % 2)],
            "none",
        )
        for j in range(5)
    ]
    rows += [
        event(
            win + timedelta(hours=3 + 7 * j),
            "TRANSFER",
            "USSD",
            32_000,
            "ACC-NEO-02",
            f"ACC-NEO-M{j + 1:02d}",
            "none",
        )
        for j in range(4)
    ]
    return rows


def s52() -> list[dict]:
    # R11 near-miss: five first-time counterparties in 44h - share 1.0 >= g
    # but distinct count 5 < c=6 -> no fire.
    return [
        event(
            ts(2024, 2, 7, 7, 0) + 10 * H * j,
            "TRANSFER",
            "APP",
            33_000,
            "ACC-NEO-03",
            f"ACC-NEO-P{j + 1:02d}",
            "none",
        )
        for j in range(5)
    ]


def s53() -> list[dict]:
    # Structural: 40 transfers between ONE pair in 2h
    # (test_parallel_edges_preserved: forty parallel edges, not one). The
    # pair has one-off history through OTHER dedicated accounts (Jan 20-31)
    # so the burst counterparty is returning - R11 fresh share 0 - while
    # each side keeps fewer than min_baseline_days=5 active days inside its
    # own 30-day baseline before its busiest day, so R6 is never scored.
    # 25-day and 20-day gaps are below d=30, so R7 stays out. No cash-out
    # and no post-burst reverse leg, so R1/R10 stay out; one distinct
    # receiver in the burst window, so R3 stays out (7 < 8 receivers ever).
    rows = [
        event(ts(2024, 1, d, 7, 0), "PAYMENT", "USSD", 20_000, "ACC-BURST-A", "ACC-BUR-H01", "none")
        for d in (20, 22, 24, 26)
    ]
    rows += [
        event(
            ts(2024, 1, d, 7, 0), "TRANSFER", "USSD", 25_000, "ACC-BUR-S01", "ACC-BURST-B", "none"
        )
        for d in (28, 29, 30, 31)
    ]
    rows += [
        event(
            ts(2024, 2, 20, 4, 0) + 3 * M * j,
            "TRANSFER",
            "APP",
            30_000,
            "ACC-BURST-A",
            "ACC-BURST-B",
            "burst_parallel",
        )
        for j in range(40)
    ]
    return rows


def s54() -> list[dict]:
    # Rail/supernode in miniature (DEV-011 is exactly why this must be
    # hand-built: the real PaySim corpus maxes at sender degree 3).
    # ACC-RAIL-AGENT carries the highest total_degree in the fixture (48
    # non-self edges) and is typed rail by config/pipeline.yaml
    # graph.rail_degree_percentile=99.0. R2 must exclude it despite a
    # planted 24h cluster of 10 distinct senders with median 4125 < tau -
    # a naive R2 would fire here. Six of the ten cluster senders are
    # returning (history spread Jan 25-Feb 2, <=2 new pairings per 48h) so
    # the cluster-day R11 share is 4/10 = 0.40 < g. Both disbursements are
    # 4000 (scale-matched, >=13h after any inflow), so R1 and R9 stay out;
    # no CASH_OUT by the agent, so R10 stays out.
    rows = [
        event(
            ts(2024, 1, 25, 6, 0) + 44 * H * i,
            "CASH_IN",
            "USSD",
            4_000 + 200 * i,
            POOL[i],
            "ACC-RAIL-AGENT",
            "none",
        )
        for i in range(6)
    ]
    rows += [
        event(
            ts(2024, 2, 15, 4, 30) + H * i,
            "CASH_IN",
            "USSD",
            3_000 + 250 * i,
            POOL[i],
            "ACC-RAIL-AGENT",
            "none",
        )
        for i in range(10)
    ]
    rows += [
        event(
            ts(2024, 2, 20, 5, 0) + i * D + 11 * M * i,
            "CASH_IN",
            "USSD",
            5_000 + 300 * i,
            sender,
            "ACC-RAIL-AGENT",
            "none",
        )
        for i, sender in enumerate(RS)
    ]
    rows += [
        event(
            ts(2024, 2, 16, 3, 0),
            "TRANSFER",
            "AGENT",
            4_000,
            "ACC-RAIL-AGENT",
            "ACC-RAIL-BANK",
            "none",
        ),
        event(
            ts(2024, 2, 22, 3, 0),
            "TRANSFER",
            "AGENT",
            4_000,
            "ACC-RAIL-AGENT",
            "ACC-RAIL-BANK",
            "none",
        ),
    ]
    return rows


def s55() -> list[dict]:
    # Second high-degree rail candidate: ACC-RAIL-MARKET, 20 distinct
    # counterparties over 20 days, never more than one inside any 24h
    # window, receive-only. Its degree (20) plus the two 44-ties (BURST
    # pair) build the gap the percentile threshold lands in: the rail set
    # can only ever come out of {48, 44, 44, 27, 20}, and the fan-rule
    # hubs (<= 13) sit safely below it at any account count in [101, 390].
    # The self-check recomputes the P3a formula at the real N and fails the
    # build if that ever stops holding.
    return [
        event(
            ts(2024, 2, 1, 9, 0) + i * D,
            "PAYMENT",
            "WEB",
            60_000 + 1_000 * i,
            sender,
            "ACC-RAIL-MARKET",
            "none",
        )
        for i, sender in enumerate(MK)
    ]


def s56() -> list[dict]:
    # Singleton edge: both accounts have exactly one incident non-self edge
    # -> excluded from graph aggregates (test_singletons_excluded_from_-
    # graph_stats), still eligible for tabular-only scoring.
    return [
        event(
            ts(2024, 3, 15, 6, 0),
            "PAYMENT",
            "USSD",
            25_000,
            "ACC-LONER",
            "ACC-EXT-002",
            "singleton",
        )
    ]


def s57() -> list[dict]:
    # Zero-amount balance probes (test_zero_amount_excluded_from_value_-
    # features): kept, flagged is_zero_value, excluded from value rules.
    # ZERO-02 receives 3 rows from ONE sender -> no fan arithmetic.
    return [
        event(
            ts(2024, 3, 16, 5, 0), "TRANSFER", "APP", 0, "ACC-ZERO-01", "ACC-ZERO-02", "zero_value"
        ),
        event(
            ts(2024, 3, 16, 10, 0), "TRANSFER", "APP", 0, "ACC-ZERO-01", "ACC-ZERO-02", "zero_value"
        ),
        event(
            ts(2024, 3, 17, 6, 0), "PAYMENT", "APP", 12_000, "ACC-ZERO-01", "ACC-ZERO-02", "none"
        ),
    ]


def s58() -> list[dict]:
    # THE planted balance-delta case (test_balance_delta_feature_present):
    # every row here violates src_after = src_before - amount and/or
    # dst_after = dst_before + amount. These three txn_ids are the ONLY
    # delta-inconsistent rows in the corpus; the feature is exactly true on
    # them and false everywhere else. No rule fires on these accounts:
    # IMBAL-02's one send is 25h after any receipt (window-blocked for R1).
    return [
        event(
            ts(2024, 2, 25, 7, 0),
            "PAYMENT",
            "USSD",
            50_000,
            "ACC-IMBAL-01",
            "ACC-IMBAL-02",
            "balance_inconsistent",
            src_before=500_000,
            src_after=460_000,
            dst_before=100_000,
            dst_after=100_000,
        ),
        event(
            ts(2024, 2, 26, 7, 0),
            "PAYMENT",
            "USSD",
            80_000,
            "ACC-IMBAL-01",
            "ACC-IMBAL-02",
            "balance_inconsistent",
            src_before=460_000,
            src_after=400_000,
            dst_before=100_000,
            dst_after=140_000,
        ),
        event(
            ts(2024, 2, 27, 7, 0),
            "TRANSFER",
            "USSD",
            40_000,
            "ACC-IMBAL-02",
            "ACC-IMBAL-01",
            "balance_inconsistent",
            src_before=140_000,
            src_after=95_000,
            dst_before=400_000,
            dst_after=440_000,
        ),
    ]


def s59(rng: random.Random) -> list[dict]:
    # Background traffic: 25 one-shot star edges between fresh accounts,
    # PaySim-like type mix, lognormal-ish amounts clamped to [20000,
    # 1500000] minor (200 .. 15000 EUR), daytime local hours only.
    # Structurally inert BY DESIGN: every noise account appears exactly
    # once, so no pass-through, cycle, chain, fan or surge arithmetic can
    # complete on them. The clamp holds the corpus amount p25 near 22000:
    # above every planted gather median (3500/4000/4125) and below the
    # R2-tau near-miss median (240000).
    rows = []
    for i in range(25):
        when = ts(2024, 1, 1, 7, 0) + timedelta(
            days=rng.randrange(115), hours=rng.randrange(14), minutes=rng.randrange(60)
        )
        txn_type = rng.choices(
            ["PAYMENT", "TRANSFER", "CASH_IN", "CASH_OUT", "DEBIT"],
            weights=[38, 26, 15, 11, 10],
            k=1,
        )[0]
        channel = CHANNELS[rng.randrange(len(CHANNELS))]
        amount = min(max(int(rng.lognormvariate(11.0, 0.9)), 20_000), 1_500_000)
        rows.append(
            event(
                when,
                txn_type,
                channel,
                amount,
                f"ACC-N{2 * i + 1:03d}",
                f"ACC-N{2 * i + 2:03d}",
                "none",
            )
        )
    return rows


BUILDERS: list[tuple[str, Callable[..., list[dict]]]] = [
    ("S01_r1_pass_through_092", s01),
    ("S02_r1_pass_through_088_overlap_r10", s02),
    ("S03_r1_pass_through_085", s03),
    ("S04_r1_near_miss_ratio_079", s04),
    ("S05_r1_near_miss_min_amount", s05),
    ("S06_r1_near_miss_window_61min", s06),
    ("S07_r10_fast_cash_out_0775", s07),
    ("S08_r10_near_miss_share_069", s08),
    ("S09_r10_near_miss_holding_7h", s09),
    ("S10_cross_currency_no_sum", s10),
    ("S11_r2_fan_in_positive", s11),
    ("S12_r2_fan_in_severity", s12),
    ("S13_r2_near_miss_k7", s13),
    ("S14_r2_near_miss_window", s14),
    ("S15_r2_near_miss_tau", s15),
    ("S16_r3_fan_out_positive", s16),
    ("S17_r3_fan_out_severity", s17),
    ("S18_r3_near_miss_k7", s18),
    ("S19_r4_cycle_4", s19),
    ("S20_r4_cycle_3", s20),
    ("S21_r4_near_miss_retention_059", s21),
    ("S22_r4_near_miss_length_7", s22),
    ("S23_r4_time_reversed", s23),
    ("S24_r4_periodic_payroll", s24),
    ("S25_r4_self_transfer", s25),
    ("S26_reversal_pair", s26),
    ("S27_reversal_triangle", s27),
    ("S28_chain_positive", s28),
    ("S29_chain_near_miss_increasing", s29),
    ("S30_chain_near_miss_slow", s30),
    ("S31_chain_near_miss_short", s31),
    ("S32_chain_decoy_2hop", s32),
    ("S33_r5_structuring_a", s33),
    ("S34_r5_structuring_b", s34),
    ("S35_r5_near_miss_count", s35),
    ("S36_r5_near_miss_window", s36),
    ("S37_r6_velocity_positive", s37),
    ("S38_r6_near_miss_z", s38),
    ("S39_r6_near_miss_baseline", s39),
    ("S40_r7_dormant_positive", s40),
    ("S41_r7_dormant_severity", s41),
    ("S42_r7_near_miss_gap_29d", s42),
    ("S43_r7_near_miss_k4", s43),
    ("S44_r8_odd_hour_positive", s44),
    ("S45_r8_near_miss_share", s45),
    ("S46_r8_near_miss_min_txn", s46),
    ("S47_r8_local_hour_guard", s47),
    ("S48_r9_regime_positive", s48),
    ("S49_r9_near_miss", s49),
    ("S50_r11_positive", s50),
    ("S51_r11_near_miss_share", s51),
    ("S52_r11_near_miss_count", s52),
    ("S53_burst_parallel_edges", s53),
    ("S54_rail_supernode", s54),
    ("S55_rail_second", s55),
    ("S56_singleton", s56),
    ("S57_zero_amount", s57),
    ("S58_balance_inconsistency", s58),
    ("S59_background_noise", s59),
]


def main() -> int:
    rng = random.Random(SEED)
    all_rows: list[dict] = []
    scenario_rows: dict[str, list[dict]] = {}
    for tag, builder in BUILDERS:
        made = builder(rng) if tag == "S59_background_noise" else builder()
        scenario_rows[tag] = made
        all_rows.extend(made)

    # The pipeline's total order is (event_ts_utc, txn_id); ids are assigned
    # in sorted order, so file order == id order == total order. The tie
    # breakers below only ever fire on identical timestamps, keeping ids a
    # deterministic function of the row content.
    all_rows.sort(
        key=lambda r: (r["event_ts_utc"], r["account_from"], r["account_to"], r["amount_minor"])
    )
    for i, row in enumerate(all_rows, start=1):
        row["txn_id"] = f"golden:{i:06d}"

    lines = [",".join(COLUMNS)]
    for row in all_rows:
        lines.append(",".join(str(row[col]) for col in COLUMNS))
    CSV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    sha = hashlib.sha256(CSV_PATH.read_bytes()).hexdigest()
    manifest = {
        "seed": SEED,
        "rows": len(all_rows),
        "sha256": sha,
        "scenarios": {
            tag: sorted(int(r["txn_id"].rsplit(":", 1)[1]) for r in made)
            for tag, made in scenario_rows.items()
        },
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, sort_keys=True, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    accounts = {row["account_from"] for row in all_rows} | {row["account_to"] for row in all_rows}
    print(f"built {len(all_rows)} rows, {len(accounts)} accounts -> {CSV_PATH.name}")
    print(f"sha256={sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
