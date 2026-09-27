"""P7 — the two builders that turn the canonical event frame into warehouse rows (DEV-027's case rail).

``transaction_rows`` shapes the ``transaction`` table (the evidence every case rests on) and
``evidence_event_rows`` shapes ``evidence_event`` (one row of one account's timeline). Both return
``(rows, refused)``, both are shipped, and neither had a test.

Every expected value below is arithmetic done on paper against the literal input written beside it,
never a figure read back out of the builder (§19 rule 6). Money is INTEGER minor units: the fixture
writes ``123_456``, which is 1,234.56 of the major unit, and DEV-005 is why a float there is a
refusal rather than a rounding. No clock, no filesystem, no database — ``datetime(2024, 3, …,
tzinfo=UTC)`` instants only.

What each family is here to refuse:

* **money that is not an integer** — the refusal quotes the offending value, so a reader can see
  the precision the source carried instead of guessing whether the port rounded it.
* **a repeated primary key** — ``txn_id`` is ``transaction``'s PK, so the second occurrence must
  refuse while the first lands.
* **a required column that was never measured** — a null in a NOT NULL column refuses the row with
  the producer's name in it; an absent column fails the whole call, because a frame with no
  ``amount_minor`` cannot describe a transaction at all.
* **the two sides of a transfer** — one event is one payment seen from two accounts, so the row
  keyed on ``account_from`` must name ``account_to`` as its counterparty, and back. Reading that
  pair the wrong way round is not cosmetic: the case file would say B paid C when C paid B, which
  is the failure that makes a timeline lie about who sent the money. The exact pairing is asserted
  per event, per side — not merely "two rows came out".
* **an account that is not there** — a null side is skipped rather than landed as account ``None``.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import polars as pl
import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

# The bootstrap `tests/unit/test_p7_audit_verifier_accounts_skips.py` uses: the pipeline package
# lives under `packages/pipeline`, and nothing here touches the artifacts that would pull in more.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from oxbow.adapters.warehouse.landing import (  # noqa: E402
    LandingError,
    evidence_event_rows,
    transaction_rows,
)

# --- the fixture's money, chosen so the arithmetic fits on paper ----------------

#: `CHAR(12)` account keys (03 §A: twelve hex characters, displayed ACC-XXXXXX).
A: Final = "a1b2c3d4e5f6"
B: Final = "b6f5e4d3c2b1"
C: Final = "c1d2e3f4a5b6"
assert len(A) == len(B) == len(C) == 12

#: Thirteen characters — one past the column, so it must never be cut to twelve.
TOO_LONG: Final = "b6f5e4d3c2b1x"

T1: Final = datetime(2024, 3, 1, 10, 30, tzinfo=UTC)
T2: Final = datetime(2024, 3, 2, 8, 5, tzinfo=UTC)
T3: Final = datetime(2024, 3, 3, 23, 59, tzinfo=UTC)
T4: Final = datetime(2024, 3, 4, 6, 0, tzinfo=UTC)
#: An instant with no transaction at it, so a rule hit cannot hide behind a tie.
T_MID: Final = datetime(2024, 3, 3, 12, 0, tzinfo=UTC)

D1: Final = date(2024, 3, 1)
D2: Final = date(2024, 3, 2)
D3: Final = date(2024, 3, 3)
D4: Final = date(2024, 3, 4)

#: `transaction`'s own columns, hand-written from `models.py:506-522` minus `run_id`, which the
#: loader stamps.
TRANSACTION_ROW_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "txn_id",
        "event_ts_utc",
        "event_date_local",
        "local_hour",
        "src_account_key",
        "dst_account_key",
        "amount_minor",
        "currency",
        "txn_type",
        "src_balance_before",
        "src_balance_after",
        "dst_balance_before",
        "dst_balance_after",
        "label_fraud",
        "label_typology",
        "source_dataset",
    }
)

#: `evidence_event`'s columns the builder can fill (`models.py:477-486`) minus `id`, `run_id` and
#: `object_key`, none of which an event frame can name.
EVIDENCE_ROW_COLUMNS: Final[frozenset[str]] = frozenset(
    {"account_key", "occurred_at", "kind", "label", "txn_id", "rule_id", "detail"}
)
EVIDENCE_DETAIL_COLUMNS: Final[frozenset[str]] = frozenset(
    {"direction", "counterparty", "amount_minor", "currency", "source_dataset"}
)

_TX0001: Final[dict[str, Any]] = {
    "txn_id": "paysim:tx0001",
    "event_ts_utc": T1,
    "event_date_local": D1,
    "local_hour": 10,
    "txn_type": "PAYMENT",
    "channel": "online",
    # A pays B 123_456 minor units = 1,234.56. A's ledger: 1,000,000 - 123,456 = 876,544.
    # B's ledger: 0 + 123,456 = 123,456.
    "amount_minor": 123_456,
    "currency": "USD",
    "account_from": A,
    "account_to": B,
    "src_balance_before_minor": 1_000_000,
    "src_balance_after_minor": 876_544,
    "dst_balance_before_minor": 0,
    "dst_balance_after_minor": 123_456,
    "label_is_fraud": 0,
    "label_typology": None,
    "source_dataset": "paysim",
    "batch_id": "batch-1",
}

# The other three events, one dict each, so the digits sit next to the subtraction they describe.
_TX0002: Final[dict[str, Any]] = {
    **_TX0001,
    "txn_id": "paysim:tx0002",
    "event_ts_utc": T2,
    "event_date_local": D2,
    "local_hour": 8,
    "txn_type": "CASH_OUT",
    "channel": "branch",
    # B pays C 45_000 = 450.00. B's ledger carried straight from tx0001: 123,456 - 45,000 = 78,456.
    "amount_minor": 45_000,
    "account_from": B,
    "account_to": C,
    "src_balance_before_minor": 123_456,
    "src_balance_after_minor": 78_456,
    "dst_balance_before_minor": 10_000_000,
    "dst_balance_after_minor": 10_045_000,
    "label_is_fraud": 1,
    "label_typology": "funnel",
    "batch_id": "batch-2",
}

_TX0003: Final[dict[str, Any]] = {
    **_TX0001,
    "txn_id": "paysim:tx0003",
    "event_ts_utc": T3,
    "event_date_local": D3,
    "local_hour": 23,
    "txn_type": "DEPOSIT",
    "channel": "app",
    # C pays A 999 = 9.99 — the other direction, so the pairing test cannot pass by symmetry.
    # C: 10,045,000 - 999 = 10,044,001. A: 876,544 + 999 = 877,543.
    "amount_minor": 999,
    "account_from": C,
    "account_to": A,
    "src_balance_before_minor": 10_045_000,
    "src_balance_after_minor": 10_044_001,
    "dst_balance_before_minor": 876_544,
    "dst_balance_after_minor": 877_543,
    "batch_id": "batch-3",
}

_TX0004: Final[dict[str, Any]] = {
    **_TX0001,
    "txn_id": "paysim:tx0004",
    "event_ts_utc": T4,
    "event_date_local": D4,
    "local_hour": 6,
    "txn_type": "TRANSFER",
    "channel": "pos",
    # A pays C 1 minor unit = 0.01. This row carries no balances at all: absent is not zero
    # (models.py:490-495), so its nulls must stay nulls.
    "amount_minor": 1,
    "account_from": A,
    "account_to": C,
    "src_balance_before_minor": None,
    "src_balance_after_minor": None,
    "dst_balance_before_minor": None,
    "dst_balance_after_minor": None,
    "batch_id": "batch-4",
}


def _events() -> pl.DataFrame:
    """Four canonical events over three accounts, written deliberately NOT in `txn_id` order.

    The frame order is tx0002, tx0001, tx0004, tx0003, while the table's stated order is `txn_id`,
    so the emitted sequence has to come out tx0001 … tx0004 with two pairs swapped relative to the
    input.

    Money, in minor units: 123_456 + 45_000 + 999 + 1 = 169_456, i.e. 1,694.56 of the major unit.
    Per account: A sends 123,456 + 1 = 123,457 and receives 999; B sends 45,000 and receives
    123,456; C sends 999 and receives 45,000 + 1 = 45,001. Every event names both sides, so the
    timeline this feeds is 4 x 2 = 8 transaction rows.
    """
    return pl.DataFrame([_TX0002, _TX0001, _TX0004, _TX0003])


def _event(**overrides: Any) -> pl.DataFrame:
    """One valid event (`paysim:tx0001`, A pays B 1,234.56) with the named columns replaced."""
    record: dict[str, Any] = {**_TX0001, **overrides}
    return pl.DataFrame([record])


def _no_events() -> pl.DataFrame:
    """The same schema and no rows, so a rule-firing test cannot be answered by the event half."""
    return _events().clear()


#: The scored-frame columns the rule-firing half reads. `fold` and `as_of_ts` are what
#: `_current_score_per_account` resolves "current" from; the severity columns are matched by
#: `^rule_(r\d+)_.*_severity$`, so the rule id is the leading `rNN` uppercased.
_SCORED_SCHEMA: Final[dict[str, pl.DataType]] = {
    "account_key": pl.String(),
    "role": pl.String(),
    "fold": pl.Int8(),
    "as_of_ts": pl.Datetime("us", "UTC"),
    "scoring_mode": pl.String(),
    "rule_r4_cycle_severity": pl.Float64(),
    "rule_r2_fan_in_severity": pl.Float64(),
    "rule_r9_regime_shift_severity": pl.Float64(),
}


def _scored_row(**overrides: Any) -> dict[str, Any]:
    """A quiet `test`-role row for account A at T1, with the named columns overridden."""
    row: dict[str, Any] = {
        "account_key": A,
        "role": "test",
        "fold": 1,
        "as_of_ts": T1,
        "scoring_mode": "walk_forward",
        "rule_r4_cycle_severity": 0.0,
        "rule_r2_fan_in_severity": 0.0,
        "rule_r9_regime_shift_severity": 0.0,
    }
    row.update(overrides)
    return row


def _scored_frame(*rows: dict[str, Any]) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=_SCORED_SCHEMA)


def _no_scores() -> pl.DataFrame:
    return _scored_frame(_scored_row()).clear()


def _timeline(rows: list[dict[str, Any]]) -> list[tuple[str, datetime, str, str, str | None]]:
    """The read order the table states: (account_key, occurred_at, kind, label, txn_id)."""
    return [
        (row["account_key"], row["occurred_at"], row["kind"], row["label"], row["txn_id"])
        for row in rows
    ]


# --- transaction_rows: the case rail's money table -----------------------------


def test_the_slice_lands_four_rows_in_txn_id_order_not_frame_order() -> None:
    events = _events()
    rows, refused = transaction_rows(events)

    assert refused == [], f"every figure in the fixture is present and an integer: {refused}"
    assert [row["txn_id"] for row in rows] == [
        "paysim:tx0001",
        "paysim:tx0002",
        "paysim:tx0003",
        "paysim:tx0004",
    ], "the stated order is the primary key, so a re-run lands the same sequence"
    assert [row["txn_id"] for row in rows] != list(events["txn_id"]), (
        "the fixture is written tx0002, tx0001, tx0004, tx0003; output that matches the input "
        "would be passing on frame order, not on the sort"
    )
    # 123,456 + 45,000 + 999 + 1, read off the fixture in the emitted order.
    assert [row["amount_minor"] for row in rows] == [123_456, 45_000, 999, 1]
    assert sum(row["amount_minor"] for row in rows) == 169_456, "1,694.56 in the major unit"
    assert all(
        isinstance(row["amount_minor"], int) for row in rows
    ), "DEV-005: a float here is not a rounding difference, it is a different packet total"


def test_a_landed_row_carries_the_tables_own_names_and_the_frames_own_values() -> None:
    rows, refused = transaction_rows(_events())
    assert refused == []

    for row in rows:
        assert (
            set(row) == TRANSACTION_ROW_COLUMNS
        ), f"a row that is not the table's column set cannot be written: {sorted(row)}"
        leaked = {"account_from", "account_to", "label_is_fraud", "channel", "batch_id"} & set(row)
        assert not leaked, f"the producer's vocabulary reached the handoff: {sorted(leaked)}"

    first = rows[0]  # tx0001: A pays B 1,234.56 at 2024-03-01T10:30Z
    assert first["src_account_key"] == A and first["dst_account_key"] == B
    assert first["event_ts_utc"] == T1 and first["event_date_local"] == D1
    assert first["local_hour"] == 10 and first["currency"] == "USD"
    assert first["txn_type"] == "PAYMENT" and first["source_dataset"] == "paysim"
    # 1,000,000 - 123,456 = 876,544, and 0 + 123,456 = 123,456.
    assert first["src_balance_before"] == 1_000_000 and first["src_balance_after"] == 876_544
    assert first["dst_balance_before"] == 0 and first["dst_balance_after"] == 123_456
    assert first["label_fraud"] is False, "label_is_fraud 0 lands as the column's own bool, not 0"
    assert first["label_typology"] is None

    assert rows[1]["label_fraud"] is True and rows[1]["label_typology"] == "funnel"

    assert (
        rows[3]["src_balance_before"] is None and rows[3]["dst_balance_after"] is None
    ), "this corpus carries no balances for the row; a zero would be a fabricated ledger"


@pytest.mark.parametrize(
    ("bad_amount", "expected_refusal"),
    [
        (
            123_456.75,
            "transaction paysim:tx0001: amount_minor (amount_minor=123456.75)"
            " is not integer minor units",
        ),
        (
            "123456",
            "transaction paysim:tx0001: amount_minor (amount_minor='123456')"
            " is not integer minor units",
        ),
        (
            True,
            "transaction paysim:tx0001: amount_minor (amount_minor=True)"
            " is not integer minor units",
        ),
    ],
)
def test_money_that_is_not_an_integer_refuses_the_row_and_is_not_rounded(
    bad_amount: Any, expected_refusal: str
) -> None:
    """123,456.75 is a quarter of a minor unit, and no rounding rule was ever measured (DEV-005).

    The refusal quotes the value itself, so a reviewer can see the precision the source carried
    rather than guessing whether the port rounded, truncated or dropped it. A numeric-looking
    string is refused too: `_integer` does not parse text into money.
    """
    rows, refused = transaction_rows(_event(amount_minor=bad_amount))

    assert rows == [], "a rounded row would be counted into the packet total as if it were measured"
    assert refused == [expected_refusal]


def test_a_float_money_column_refuses_the_whole_slice_and_lands_no_total() -> None:
    """The frame-level form of the same defect: Float64 money lands nothing, not a rounded sum.

    169,456.0 is the total the slice would have published, and four of its values are exactly
    integral floats. Being whole is not the same as being an integer: the column type is the claim
    that the money was measured in minor units, and here it is not.
    """
    float_slice = _events().with_columns(pl.col("amount_minor").cast(pl.Float64))
    rows, refused = transaction_rows(float_slice)

    assert rows == [], "0 rows and a float sum would be the silent version of DEV-005"
    assert len(refused) == 4, refused
    assert all("is not integer minor units" in line for line in refused), refused
    assert all(f"tx000{n}: " in line for n, line in enumerate(refused, start=1)), refused
    assert "amount_minor=45000.0" in refused[1], (
        "an exactly-integral float is refused too: " f"{refused[1]}"
    )


def test_a_repeated_txn_id_lands_once_and_refuses_the_repeat() -> None:
    """`txn_id` is the table's primary key, so a second row on it is a rejected write.

    The two copies differ in money and polars promises no order for equal keys, so the proof is the
    shape — one landed, one refused — not which amount survived.
    """
    twins = pl.concat(
        [
            _event(txn_id="paysim:tx0009", amount_minor=123_456),
            _event(txn_id="paysim:tx0009", amount_minor=45_000),
        ]
    )
    rows, refused = transaction_rows(twins)

    assert len(rows) == 1, f"one primary key landed {len(rows)} rows"
    assert rows[0]["txn_id"] == "paysim:tx0009"
    assert rows[0]["amount_minor"] in (123_456, 45_000)
    assert refused == [
        "transaction paysim:tx0009: the frame repeats it, and txn_id is the table's primary key"
    ]


def test_a_refused_row_does_not_reserve_the_primary_key_for_its_valid_twin() -> None:
    """`seen` is only charged for a row that actually landed.

    One copy has a 13-character account key and must refuse; the other is clean. Whichever sorts
    first, exactly one row survives and it is the valid one — a `seen` set charged by refusal order
    would throw away the measurable transaction.
    """
    frame = pl.concat(
        [
            _event(txn_id="paysim:tx0007", account_to=TOO_LONG),
            _event(txn_id="paysim:tx0007", amount_minor=999),
        ]
    )
    rows, refused = transaction_rows(frame)

    assert len(rows) == 1 and rows[0]["amount_minor"] == 999
    assert len(refused) == 1 and refused[0].startswith("transaction paysim:tx0007: ")


@pytest.mark.parametrize("column", ["amount_minor", "currency", "event_ts_utc", "txn_id"])
def test_a_frame_with_no_required_column_is_a_landing_error_not_an_empty_page(column: str) -> None:
    """§18: an absent column is a broken frame, and empty rows would render it as a quiet slice."""
    with pytest.raises(LandingError) as excinfo:
        transaction_rows(_events().drop(column))

    message = str(excinfo.value)
    assert column in message, f"the error has to name the column it is missing: {message}"
    assert "no transaction can be described" in message


def test_every_absent_required_column_is_named_in_the_one_error() -> None:
    """The missing list is sorted, so the message does not depend on the order columns were lost."""
    with pytest.raises(LandingError) as excinfo:
        transaction_rows(_events().drop("txn_id", "currency"))

    assert str(excinfo.value) == (
        "the event frame is missing ['currency', 'txn_id'], so no transaction can be described"
    )


@pytest.mark.parametrize(
    "column",
    [
        "event_ts_utc",
        "event_date_local",
        "local_hour",
        "amount_minor",
        "currency",
        "txn_type",
        "source_dataset",
    ],
)
def test_a_not_null_column_that_was_never_measured_refuses_naming_its_producer(
    column: str,
) -> None:
    """TRANSACTION_REQUIRED is the table's NOT NULL list; a null in it is an absence, not a zero."""
    rows, refused = transaction_rows(_event(**{column: None}))

    assert rows == [], f"{column} is NOT NULL; a null row would defer the lie to Postgres"
    assert refused == [f"transaction paysim:tx0001: {column} ({column}) was not measured"]


@pytest.mark.parametrize(
    ("column", "value", "expected_refusal"),
    [
        (
            "event_ts_utc",
            "2024-03-01T10:30:00+00:00",
            "transaction paysim:tx0001: event_ts_utc (event_ts_utc='2024-03-01T10:30:00+00:00')"
            " is unreadable",
        ),
        (
            "local_hour",
            10.0,
            "transaction paysim:tx0001: local_hour (local_hour='10.0') is unreadable",
        ),
        (
            "currency",
            "USDX",
            "transaction paysim:tx0001: currency (currency='USDX') is unreadable",
        ),
    ],
)
def test_a_value_the_column_cannot_hold_refuses_as_unreadable(
    column: str, value: Any, expected_refusal: str
) -> None:
    """A text instant is not a `datetime`, an hour must be an integer, and `_name` will not cut a
    four-character currency to three: each refusal quotes the value it rejected."""
    rows, refused = transaction_rows(_event(**{column: value}))

    assert rows == []
    assert refused == [expected_refusal]


def test_an_account_key_longer_than_char12_refuses_rather_than_being_cut() -> None:
    """`_account_key` returns None past twelve characters, and a cut key is a different account."""
    assert len(TOO_LONG) == 13
    rows, refused = transaction_rows(_event(account_from=TOO_LONG))

    assert rows == []
    assert refused == [
        "transaction paysim:tx0001: src_account_key (account_from='b6f5e4d3c2b1x')"
        " is not a 12-character key"
    ]


def test_a_transaction_with_no_account_on_either_side_is_refused() -> None:
    """Both sides nullable is not the same as no sides: nobody could ever read it as evidence."""
    rows, refused = transaction_rows(_event(account_from=None, account_to=None))

    assert rows == []
    assert refused == [
        "transaction paysim:tx0001: neither side names an account, so no case could ever read it "
        "as evidence"
    ]


def test_the_txn_id_column_boundary_is_exactly_its_64_characters() -> None:
    """`_name` refuses over-length instead of truncating, so the limit is a real edge."""
    fits = "paysim:" + "9" * 57  # 7 + 57 == 64
    over = "paysim:" + "9" * 58  # 65, one character past VARCHAR(64)

    rows, refused = transaction_rows(_event(txn_id=fits))
    assert refused == [] and [row["txn_id"] for row in rows] == [fits]

    rows, refused = transaction_rows(_event(txn_id=over))
    assert rows == [], "a cut txn_id is either a collision or an id that names nothing"
    assert refused == [f"event {over!r}: no txn_id that fits the column"]


# --- evidence_event_rows: one account's timeline --------------------------------


def test_a_two_account_transfer_lands_an_outbound_and_an_inbound_row() -> None:
    """The pairing, per event and per side: the sender's row names the receiver, and back.

    `account_from` is the side that paid out, so the outbound row is keyed on it and looks at
    `account_to`; the inbound row is the same money seen from the other end. Reversing one of the
    two is invisible to a count of rows, which is why each side is asserted by name.
    """
    rows, refused = evidence_event_rows(_events(), _no_scores())

    assert refused == [], f"every event names both sides: {refused}"
    assert len(rows) == 8, "four events x two sides"
    for row in rows:
        assert set(row) == EVIDENCE_ROW_COLUMNS
        assert set(row["detail"]) == EVIDENCE_DETAIL_COLUMNS
        assert row["kind"] == "transaction" and row["rule_id"] is None

    by_key = {(row["txn_id"], row["detail"]["direction"]): row for row in rows}
    assert len(by_key) == 8, "one (txn_id, direction) pair landed twice"

    # tx0001: A pays B 1,234.56.
    sent = by_key[("paysim:tx0001", "outbound")]
    assert sent["account_key"] == A
    assert sent["detail"]["counterparty"] == B
    assert sent["occurred_at"] == T1 and sent["label"] == "PAYMENT"
    received = by_key[("paysim:tx0001", "inbound")]
    assert received["account_key"] == B
    assert received["detail"]["counterparty"] == A
    assert received["occurred_at"] == T1 and received["label"] == "PAYMENT"
    assert {sent["detail"]["amount_minor"], received["detail"]["amount_minor"]} == {123_456}
    assert sent["detail"]["currency"] == "USD" and sent["detail"]["source_dataset"] == "paysim"

    # tx0002: B pays C 450.00 — a different pair of accounts, so a swapped mapping cannot pass by
    # being merely symmetric.
    assert by_key[("paysim:tx0002", "outbound")]["account_key"] == B
    assert by_key[("paysim:tx0002", "outbound")]["detail"]["counterparty"] == C
    assert by_key[("paysim:tx0002", "inbound")]["account_key"] == C
    assert by_key[("paysim:tx0002", "inbound")]["detail"]["counterparty"] == B
    assert by_key[("paysim:tx0002", "inbound")]["detail"]["amount_minor"] == 45_000

    # tx0003 turns around: C pays A 9.99.
    assert by_key[("paysim:tx0003", "outbound")]["account_key"] == C
    assert by_key[("paysim:tx0003", "outbound")]["detail"]["counterparty"] == A
    assert by_key[("paysim:tx0003", "inbound")]["account_key"] == A
    assert by_key[("paysim:tx0003", "inbound")]["detail"]["counterparty"] == C
    assert by_key[("paysim:tx0003", "inbound")]["detail"]["amount_minor"] == 999

    # The four outbound sides are the whole slice: 123,456 + 45,000 + 999 + 1 = 169,456.
    outbound = [row for row in rows if row["detail"]["direction"] == "outbound"]
    assert sum(row["detail"]["amount_minor"] for row in outbound) == 169_456


def test_the_timeline_is_ordered_by_account_then_instant_then_kind() -> None:
    """The table's read order is the timeline's, and the kind tie-break makes two runs agree.

    Hand-computed against `_events()`: A first (a1…, three events plus its rule hit), then B (b6…,
    two events), then C (c1…, three events plus its rule hit). A's hit shares tx0001's exact
    instant, and `rule_hit` sorts before `transaction`, so it leads that instant.
    """
    scored = _scored_frame(
        # A, fold 0: R2 fired 5.0. Superseded by fold 1, so nothing of it may land.
        _scored_row(fold=0, rule_r2_fan_in_severity=5.0),
        # A, fold 1, at the same instant as tx0001: R4 fired 3.0, R2 quiet.
        _scored_row(fold=1, rule_r4_cycle_severity=3.0),
        # A again in a later fold, but in-sample: the role filter drops it before any collapse.
        _scored_row(role="train", fold=3, as_of_ts=T4, rule_r9_regime_shift_severity=9.0),
        # C, fold 1 at noon on 3 March, an instant with no transaction at it.
        _scored_row(account_key=C, fold=1, as_of_ts=T_MID, rule_r9_regime_shift_severity=1.0),
    )

    rows, refused = evidence_event_rows(_events(), scored)
    assert refused == [], f"every row here has a key, an instant and a firing: {refused}"

    assert _timeline(rows) == [
        (A, T1, "rule_hit", "R4", None),
        (A, T1, "transaction", "PAYMENT", "paysim:tx0001"),
        (A, T3, "transaction", "DEPOSIT", "paysim:tx0003"),
        (A, T4, "transaction", "TRANSFER", "paysim:tx0004"),
        (B, T1, "transaction", "PAYMENT", "paysim:tx0001"),
        (B, T2, "transaction", "CASH_OUT", "paysim:tx0002"),
        (C, T2, "transaction", "CASH_OUT", "paysim:tx0002"),
        (C, T_MID, "rule_hit", "R9", None),
        (C, T3, "transaction", "DEPOSIT", "paysim:tx0003"),
        (C, T4, "transaction", "TRANSFER", "paysim:tx0004"),
    ]


def test_a_rule_hit_lands_for_the_current_fold_of_the_test_role_only() -> None:
    """`_current_score_per_account` keeps the latest fold and the role filter keeps `test`.

    A's fold-0 row fired R2 at 5.0 and its fold-1 row fired R4 at 3.0: fold 1 is the current score,
    so the timeline gets R4/3.0/fold 1 and no R2 at all. A's fold-3 row fired R9 at 9.0 but with
    role `train`, which is in-sample and never a case-rail fact.
    """
    scored = _scored_frame(
        _scored_row(fold=0, rule_r2_fan_in_severity=5.0),
        _scored_row(fold=1, rule_r4_cycle_severity=3.0),
        _scored_row(role="train", fold=3, as_of_ts=T4, rule_r9_regime_shift_severity=9.0),
        _scored_row(
            account_key=C,
            fold=1,
            as_of_ts=T_MID,
            rule_r9_regime_shift_severity=1.0,
            scoring_mode="fresh",
        ),
    )

    rows, refused = evidence_event_rows(_no_events(), scored)
    assert refused == []
    hits = [row for row in rows if row["kind"] == "rule_hit"]

    assert [(row["account_key"], row["label"], row["detail"]["severity"]) for row in hits] == [
        (A, "R4", 3.0),
        (C, "R9", 1.0),
    ], "the rule id is the leading rNN uppercased, and only the current fold's severity counts"
    assert all(row["txn_id"] is None for row in hits)
    assert [row["detail"]["fold"] for row in hits] == [1, 1]
    assert [row["detail"]["rule_column"] for row in hits] == [
        "rule_r4_cycle_severity",
        "rule_r9_regime_shift_severity",
    ]
    assert [row["detail"]["scoring_mode"] for row in hits] == ["walk_forward", "fresh"]
    for row in hits:
        assert set(row["detail"]) == {"severity", "rule_column", "fold", "scoring_mode"}
    assert "R2" not in [row["label"] for row in hits], (
        "fold 0 is not this account's current score; landing its firing would put a superseded "
        "reading on the case rail"
    )
    assert "R9" not in [
        row["label"] for row in hits if row["account_key"] == A
    ], "the train-role fold-3 firing is in-sample and must not appear as evidence"


def test_a_severity_that_is_zero_or_unmeasured_lands_no_rule_hit() -> None:
    """A non-firing rule belongs in `rule_hit` (where its margin is evidence), not on a timeline."""
    scored = _scored_frame(
        _scored_row(
            rule_r4_cycle_severity=0.0,
            rule_r2_fan_in_severity=None,
            rule_r9_regime_shift_severity=-1.0,
        )
    )

    rows, refused = evidence_event_rows(_no_events(), scored)

    assert rows == [], "0.0, a null and a negative margin are all 'did not fire'"
    assert refused == [], "the absence of a firing is not a refusal; the account has no event here"


@pytest.mark.parametrize(
    ("overrides", "expected_refusal"),
    [
        (
            {"txn_id": None},
            "event None: no txn_id, timestamp or type to put on a timeline "
            "(all three are NOT NULL on evidence_event)",
        ),
        (
            {"txn_id": ""},
            "event : no txn_id, timestamp or type to put on a timeline "
            "(all three are NOT NULL on evidence_event)",
        ),
        (
            {"txn_type": None},
            "event paysim:tx0001: no txn_id, timestamp or type to put on a timeline "
            "(all three are NOT NULL on evidence_event)",
        ),
        (
            {"event_ts_utc": D1},
            "event paysim:tx0001: no txn_id, timestamp or type to put on a timeline "
            "(all three are NOT NULL on evidence_event)",
        ),
        (
            {"event_ts_utc": "2024-03-01T10:30:00+00:00"},
            "event paysim:tx0001: no txn_id, timestamp or type to put on a timeline "
            "(all three are NOT NULL on evidence_event)",
        ),
    ],
)
def test_an_event_without_a_key_an_instant_or_a_type_refuses_on_the_not_nulls(
    overrides: dict[str, Any], expected_refusal: str
) -> None:
    """`occurred_at` must be a datetime: a bare calendar day and an ISO string are both not an
    instant a timeline can sort on, and `_name` rejects an empty label as readily as a null one."""
    rows, refused = evidence_event_rows(_event(**overrides), _no_scores())

    assert rows == [], "a timeline row with no key, no instant or no label is an invented event"
    assert refused == [expected_refusal]


@pytest.mark.parametrize(
    ("null_side", "surviving_side", "keyed_on"),
    [("account_to", "outbound", A), ("account_from", "inbound", B)],
)
def test_an_account_side_that_was_never_measured_is_skipped_not_invented(
    null_side: str, surviving_side: str, keyed_on: str
) -> None:
    """One null side means one timeline row, keyed on the account that does exist.

    A null account would hit a NOT NULL column, and dropping the event entirely would hide a
    payment the surviving account demonstrably made or received.
    """
    rows, refused = evidence_event_rows(_event(**{null_side: None}), _no_scores())

    assert refused == []
    assert len(rows) == 1, f"{null_side} is null, so only the {surviving_side} side exists"
    assert rows[0]["account_key"] == keyed_on
    assert rows[0]["detail"]["direction"] == surviving_side
    assert (
        rows[0]["detail"]["counterparty"] is None
    ), "the other side was not measured; a placeholder key there would create an account"


def test_an_account_key_too_long_for_char12_drops_that_side_and_says_so() -> None:
    """A side that is present but unkeyable is refused; a side that is null is a one-sided event.

    `transaction_rows` refuses the same event outright, and the timeline used to skip the side in
    silence — so B's case file omitted a payment it received while A's row made the pair look
    complete. The raw thirteen-character value still survives as the counterparty, because
    `detail` is JSONB with no column width to overflow and the reader needs to see what was there.
    """
    rows, refused = evidence_event_rows(_event(account_to=TOO_LONG), _no_scores())

    assert len(rows) == 1 and rows[0]["account_key"] == A
    assert rows[0]["detail"]["direction"] == "outbound"
    assert rows[0]["detail"]["counterparty"] == TOO_LONG
    assert len(refused) == 1, refused
    assert "inbound side" in refused[0] and TOO_LONG in refused[0], refused
    assert "12-character account key" in refused[0], refused


def test_a_null_side_lands_one_row_and_refuses_nothing() -> None:
    """The other half of the same branch: an absent counterparty is a fact, not a defect."""
    rows, refused = evidence_event_rows(_event(account_to=None), _no_scores())

    assert [row["detail"]["direction"] for row in rows] == ["outbound"], rows
    assert refused == [], refused


def test_timeline_money_that_is_not_an_integer_minor_unit_refuses_the_event() -> None:
    """The timeline and the money table now refuse the same event for the same reason.

    A float used to land as `amount_minor: null` on both sides with an empty refusal list, which
    reads as a payment of nothing; `transaction_rows` has always barred that event outright, and
    an exhibit and a money table disagreeing about whether a payment exists is the worse failure.
    """
    rows, refused = evidence_event_rows(_event(amount_minor=123_456.75), _no_scores())

    assert rows == [], "no side of an event whose amount could not be read is shown"
    assert len(refused) == 1, refused
    assert "123456.75" in refused[0] and "integer minor-unit amount" in refused[0], refused
    assert "DEV-005" in refused[0], refused


@pytest.mark.parametrize(
    "column", ["txn_id", "account_from", "account_to", "event_ts_utc", "txn_type"]
)
def test_an_event_frame_missing_a_timeline_column_is_a_landing_error(column: str) -> None:
    """Five columns are the least a timeline can be built from; the call fails instead of returning
    an empty list the read model would render as 'this account did nothing'."""
    with pytest.raises(LandingError) as excinfo:
        evidence_event_rows(_events().drop(column), _scored_frame(_scored_row()))

    assert str(excinfo.value) == f"the event frame has no {column!r}, so no timeline can be built"


def test_a_scored_frame_with_no_role_column_is_a_landing_error() -> None:
    """`role` is what keeps an in-sample firing off an out-of-sample case's timeline."""
    with pytest.raises(LandingError) as excinfo:
        evidence_event_rows(_events(), _scored_frame(_scored_row()).drop("role"))

    assert str(excinfo.value) == (
        "the scored frame carries no `role` column, so no out-of-sample rule firing can be "
        "placed on a timeline"
    )


@pytest.mark.parametrize(
    ("overrides", "expected_refusal"),
    [
        (
            {"account_key": None},
            "account 'None' (4 characters): no key or as-of timestamp, so its rule firings "
            "cannot be placed on a timeline",
        ),
        (
            {"as_of_ts": None},
            "account 'a1b2c3d4e5f6' (12 characters): no key or as-of timestamp, so its rule "
            "firings cannot be placed on a timeline",
        ),
        (
            {"account_key": TOO_LONG, "rule_r4_cycle_severity": 3.0},
            "account 'b6f5e4d3c2b1x' (13 characters): no key or as-of timestamp, so its rule "
            "firings cannot be placed on a timeline",
        ),
    ],
)
def test_a_scored_row_with_no_key_or_no_instant_is_refused_by_name(
    overrides: dict[str, Any], expected_refusal: str
) -> None:
    """A firing that cannot be placed is refused rather than dated to now or keyed to nothing.

    The third case also shows how the message identifies the account: the first twelve characters
    of its key, which for an over-long key is exactly the shape of a valid one.
    """
    rows, refused = evidence_event_rows(_no_events(), _scored_frame(_scored_row(**overrides)))

    assert rows == []
    assert refused == [expected_refusal]
