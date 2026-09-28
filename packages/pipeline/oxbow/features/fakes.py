"""Canonical-event fixtures for the feature layer's tests.

WHY IT SHIPS WITH THE PACKAGE. ``oxbow/backtest/fakes.py`` set the precedent: a fixture
that encodes a data contract is worth more than three copies of it in three test files,
because the copy that drifts is the one that stops proving anything. Every frame here is
a *complete* canonical event v1 row — the same twenty-one columns
``oxbow.contracts.canonical_v1`` declares, with the same dtypes — so a test that builds on
it exercises ``assert_input_contract`` rather than routing around it.

WHAT THE FIXTURES ARE SHAPED LIKE, AND WHY THAT SHAPE.

* :func:`star_fixture` is the honest small case: a handful of accounts, seventy days of
  history, a zero-amount probe, a linked reversal, a balance-delta mismatch, and one
  labelled-fraud event. Small enough that every published value in it can be checked by
  hand, structured enough that a window, a recency and a distinct-count all have something
  to answer about.
* :func:`multi_currency_fixture` exists to be *refused*: one account transacting in two
  currencies, which is the state plan §8 says must fail the build rather than pick an
  exchange rate.
* :func:`chain_fixture` is a time-respecting A->B->C->A loop with a refund leg, for the
  reversal-is-not-a-cycle claim.

DETERMINISM. Timestamps are offsets from a fixed epoch, the local timezone is the
deployment timezone from ``config/pipeline.yaml`` (Africa/Kampala, UTC+3 with no DST), and
nothing reads the wall clock. ``ingested_at`` defaults to the event's own timestamp, which
is what makes the late-arrival fixture a single deliberate difference rather than noise.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from typing import Final

import polars as pl

from oxbow.contracts.canonical_v1 import CANONICAL_COLUMNS
from oxbow.dtypes import PolarsDtype

EPOCH: Final = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)
# Africa/Kampala: UTC+3 year-round, so the local hour is a pure offset and a test can
# state the hour it expects without a zone database in the way.
DEPLOYMENT_TZ: Final = dt.timezone(dt.timedelta(hours=3), name="Africa/Kampala")
MINUTE: Final = dt.timedelta(minutes=1)

# Amounts are minor units (DEV-005): one dollar is 100, and nothing here is a float.
CENTS: Final = 100

CANONICAL_SCHEMA: Final[dict[str, PolarsDtype]] = {
    "txn_id": pl.String,
    "event_ts_utc": pl.Datetime("us", "UTC"),
    "event_date_local": pl.Date,
    "local_hour": pl.Int8,
    "txn_type": pl.String,
    "channel": pl.String,
    "amount_minor": pl.Int64,
    "currency": pl.String,
    "account_from": pl.String,
    "account_to": pl.String,
    "src_balance_before_minor": pl.Int64,
    "src_balance_after_minor": pl.Int64,
    "dst_balance_before_minor": pl.Int64,
    "dst_balance_after_minor": pl.Int64,
    "label_is_fraud": pl.Int8,
    "label_is_flagged": pl.Int8,
    "label_typology": pl.String,
    "source_dataset": pl.String,
    "ingested_at": pl.Datetime("us", "UTC"),
    "run_id": pl.String,
    "batch_id": pl.String,
}

REVERSAL_TYPE: Final = "REVERSAL"


def canonical_event(
    txn_id: str,
    minute_offset: int,
    account_from: str,
    account_to: str,
    amount_minor: int,
    *,
    txn_type: str = "PAYMENT",
    channel: str = "APP",
    currency: str = "USD",
    src_balance_before_minor: int | None = None,
    dst_balance_before_minor: int | None = None,
    src_balance_after_minor: int | None = None,
    dst_balance_after_minor: int | None = None,
    label_is_fraud: int = 0,
    label_is_flagged: int = 0,
    label_typology: str | None = None,
    source_dataset: str = "p2_fixture",
    ingested_at: dt.datetime | None = None,
    batch_id: str = "batch00000001",
) -> dict[str, object]:
    """One complete canonical event v1 row.

    Balances are optional per side: ``None`` means "the corpus does not observe this
    account's balance", which is a different fact from a zero balance and is exactly the
    state that makes ``null_when_balance_absent`` a load-bearing policy rather than a
    comment. The after-balance defaults to ``before + amount`` on the receiving side and
    ``before - amount`` on the sending side, so a hand-written row is internally
    consistent unless a test deliberately says otherwise.
    """
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise TypeError(f"amount_minor must be integer minor units, got {amount_minor!r}")
    if amount_minor < 0:
        raise ValueError(
            f"amount_minor is {amount_minor}; a canonical amount is a magnitude, and the "
            "direction is carried by which account is the sender (03 D)"
        )
    event_ts = EPOCH + minute_offset * MINUTE
    local = event_ts.astimezone(DEPLOYMENT_TZ)
    sender_after = src_balance_after_minor
    if sender_after is None and src_balance_before_minor is not None:
        sender_after = src_balance_before_minor - amount_minor
    receiver_after = dst_balance_after_minor
    if receiver_after is None and dst_balance_before_minor is not None:
        receiver_after = dst_balance_before_minor + amount_minor
    return {
        "txn_id": txn_id,
        "event_ts_utc": event_ts,
        "event_date_local": local.date(),
        "local_hour": local.hour,
        "txn_type": txn_type,
        "channel": channel,
        "amount_minor": amount_minor,
        "currency": currency,
        "account_from": account_from,
        "account_to": account_to,
        "src_balance_before_minor": src_balance_before_minor,
        "src_balance_after_minor": sender_after,
        "dst_balance_before_minor": dst_balance_before_minor,
        "dst_balance_after_minor": receiver_after,
        "label_is_fraud": label_is_fraud,
        "label_is_flagged": label_is_flagged,
        "label_typology": label_typology,
        "source_dataset": source_dataset,
        "ingested_at": ingested_at if ingested_at is not None else event_ts,
        "run_id": "p2fixturerun",
        "batch_id": batch_id,
    }


def canonical_frame(rows: Iterable[dict[str, object]]) -> pl.DataFrame:
    """Typed, column-ordered assembly of hand-written rows into canonical event v1.

    The explicit schema is not ceremony: without it Polars infers a bare ``None`` balance
    column as ``Null`` and every downstream dtype check stops meaning anything.
    """
    materialised = list(rows)
    if not materialised:
        raise ValueError("an empty fixture proves nothing; build at least one event")
    return pl.DataFrame(materialised, schema=CANONICAL_SCHEMA).select(list(CANONICAL_COLUMNS))


def star_fixture(*, extra: Sequence[dict[str, object]] = ()) -> pl.DataFrame:
    """Six accounts, seventy days, one of everything a window kernel has to answer about.

    The shape is chosen so that each guard in plan §8 has a case that would fail if the
    guard were removed: ``t_probe`` is a zero-amount balance check that must be kept and
    flagged; ``t_rev`` refunds ``t_pay`` and must not manufacture a cycle; ``t_imbalanced``
    moves money without moving the balance by that amount; ``t_late`` is a normal row whose
    ``ingested_at`` lands a day after the seal so the late-arrival exclusion has something
    to exclude.
    """
    rows: list[dict[str, object]] = [
        # Day 0, 09:00 UTC -> 12:00 local. A paying into B, consistent balances.
        canonical_event(
            "t_pay",
            0,
            "acct_a",
            "acct_b",
            50 * CENTS,
            src_balance_before_minor=100 * CENTS,
            dst_balance_before_minor=10 * CENTS,
        ),
        # Day 0, a zero-amount probe: kept, flagged, and invisible to value features.
        canonical_event(
            "t_probe",
            120,
            "acct_a",
            "acct_b",
            0,
            txn_type="CASH_IN",
            src_balance_before_minor=50 * CENTS,
            dst_balance_before_minor=60 * CENTS,
        ),
        # Day 2: the refund of t_pay. Same pair, opposite direction, reversal type.
        canonical_event(
            "t_rev",
            2 * 24 * 60,
            "acct_b",
            "acct_a",
            50 * CENTS,
            txn_type=REVERSAL_TYPE,
            src_balance_before_minor=60 * CENTS,
            dst_balance_before_minor=0,
        ),
        # Day 5: money moves but the balances do not, so the delta is nonzero.
        canonical_event(
            "t_imbalanced",
            5 * 24 * 60,
            "acct_a",
            "acct_c",
            30 * CENTS,
            src_balance_before_minor=50 * CENTS,
            src_balance_after_minor=45 * CENTS,
            dst_balance_before_minor=1_000 * CENTS,
            dst_balance_after_minor=1_020 * CENTS,
        ),
        # Day 12: a fourth counterparty, and the only labelled-fraud event in the fixture.
        canonical_event(
            "t_fraud",
            12 * 24 * 60,
            "acct_c",
            "acct_d",
            90 * CENTS,
            txn_type="CASH_OUT",
            label_is_fraud=1,
            label_is_flagged=1,
            label_typology="pass_through",
            src_balance_before_minor=1_030 * CENTS,
        ),
        # Day 40: two accounts that meet for the first time, late in the timeline.
        canonical_event(
            "t_meet",
            40 * 24 * 60,
            "acct_e",
            "acct_a",
            20 * CENTS,
            src_balance_before_minor=200 * CENTS,
            dst_balance_before_minor=20 * CENTS,
        ),
        # Day 69: the last event, so a 30-day window at the end has history behind it.
        canonical_event(
            "t_last",
            69 * 24 * 60,
            "acct_a",
            "acct_e",
            15 * CENTS,
            src_balance_before_minor=40 * CENTS,
            dst_balance_before_minor=180 * CENTS,
        ),
        # A row that arrived two days after its own event: the late-arrival case. Its
        # amount is large so that including it in an earlier window would be visible.
        canonical_event(
            "t_late",
            44 * 24 * 60,
            "acct_f",
            "acct_a",
            500 * CENTS,
            ingested_at=EPOCH + dt.timedelta(days=46),
        ),
    ]
    rows.extend(extra)
    return canonical_frame(rows)


def multi_currency_fixture() -> pl.DataFrame:
    """One account, two currencies: the state plan §8 requires the build to refuse."""
    return canonical_frame(
        [
            canonical_event("m1", 0, "acct_a", "acct_b", 50 * CENTS, currency="USD"),
            canonical_event("m2", 60, "acct_a", "acct_c", 50 * CENTS, currency="EUR"),
            canonical_event("m3", 120, "acct_a", "acct_b", 50 * CENTS, currency="USD"),
        ]
    )


def chain_fixture() -> pl.DataFrame:
    """A time-respecting A->B->C path closed by a refund back along the first leg.

    ``A->B, B->C, B->A(REVERSAL)``: read as ordinary transfers the last leg closes the
    round trip A->B->A, so the raw edge set contains a cycle. Read correctly the last leg
    is a refund of the first - money came back the way it came - so the cycle-eligible
    edge set must shrink to the path A->B->C. That is plan 8's ``test_reversal_not_a_cycle``,
    and the fixture is built so the only difference between the two answers is whether the
    reversal is excluded.

    The refund is deliberately B->A rather than a third-party C->A: a reversal is matched
    to the original it undoes by its opposite-direction pair, so an edge with no opposite
    pair would be a *new* payment and ``reversal_of`` would correctly come back null.
    """
    return canonical_frame(
        [
            canonical_event("c1", 0, "acct_a", "acct_b", 100 * CENTS),
            canonical_event("c2", 60, "acct_b", "acct_c", 100 * CENTS),
            canonical_event("c3", 120, "acct_b", "acct_a", 100 * CENTS, txn_type=REVERSAL_TYPE),
        ]
    )


def wide_fixture(events: int = 400, *, accounts: int = 24, seed: int = 1337) -> pl.DataFrame:
    """A larger deterministic corpus, for the guards that need more than eight rows.

    A linear congruential generator rather than ``random``: the sequence is a pure function
    of the seed, so the same fixture reappears in a failing CI log without a seed being
    printed and re-run, and ``seed`` is threaded in rather than read from a global.
    """
    if events <= 0 or accounts < 2:
        raise ValueError(
            f"events must be positive and accounts at least 2, got {events}, {accounts}"
        )
    modulus = 2**31 - 1
    state = seed % modulus
    rows: list[dict[str, object]] = []
    keys = [f"acct_{index:03d}" for index in range(accounts)]
    for index in range(events):
        state = (state * 48_271) % modulus
        state2 = (state * 48_271) % modulus
        state3 = (state2 * 48_271) % modulus
        sender = keys[state % accounts]
        receiver = keys[(state2 % (accounts - 1) + 1 + state % accounts) % accounts]
        minute = (state3 % (100 * 24 * 60)) + index
        amount = 1 + (state % 500) * CENTS
        rows.append(
            canonical_event(
                f"w{index:05d}",
                minute,
                sender,
                receiver,
                amount,
                txn_type="REVERSAL" if state % 41 == 0 else "PAYMENT",
                # Every 37th row is labelled, so the corpus has ~1 % positives like the
                # corpora it stands in for — and, critically, has *two label values* at
                # all. A single-valued label makes every correlation undefined, and a
                # guard that silently measures nothing looks exactly like a guard that
                # passed.
                label_is_fraud=1 if index % 37 == 0 else 0,
            )
        )
    return canonical_frame(rows)


__all__ = [
    "CANONICAL_SCHEMA",
    "CENTS",
    "DEPLOYMENT_TZ",
    "EPOCH",
    "REVERSAL_TYPE",
    "canonical_event",
    "canonical_frame",
    "chain_fixture",
    "multi_currency_fixture",
    "star_fixture",
    "wide_fixture",
]
