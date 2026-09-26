"""P1b IBM-AML tests: a hand-built fixture over the REAL header, with hand-computed answers.

Every expected value below was computed by hand from the corpus's actual bytes and
checked in, per 00 B: a fixture whose expectation came from the code it tests proves
nothing. Where an expectation is a digest a person cannot compute mentally
(``account_key`` is HMAC-SHA256 truncated to 48 bits; the ``txn_id`` digest is the same
digest truncated to 32) the test carries an INDEPENDENT implementation of the documented
scheme and asserts agreement, so changing or dropping the salt or the id layout in
``ingest/`` breaks this file rather than being followed by it.

THE SCHEMA HERE IS THE ONE ON DISK, NOT THE ONE IN THE PUBLISHED DESCRIPTION. The
previous revision of this file pinned fourteen columns — ``stepFrom``, ``stepTo``,
``Type``, ``Category``, ``Amount``, ``nameOrig``, ``balanceOrig``, ``nameDest``,
``balanceDest``, ``isLaundering``, ``isFlood``, ``isDateSpam``, ``isForcedCashout``,
``unlabeled`` — that belong to a different IBM artefact, and all 90 of its tests passed
against that invention. ``data/raw/ibmaml/HI-Small_Trans.csv`` is 475,664,283 bytes with
5,078,345 data rows and this header, read off the bytes with ``od -c``:

    Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,
    Amount Paid,Payment Currency,Payment Format,Is Laundering

Eleven columns, two of them named ``Account``. There is no step counter, no balance
column and no four-way typology set, so the tests that pinned those are gone: a
tie-break priority over columns that do not exist is decoration, not behaviour. What
replaced them is asserted against measurements over the whole file, named where used.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
import pytest
import yaml
from pandera.errors import SchemaError

from oxbow.contracts.raw_ibm_aml import (
    CURRENCY_ISO,
    CURRENCY_NAMES_MEASURED_HI_SMALL,
    IBMAML_LABEL_COLUMN,
    IBMAML_ORDINAL_FILTER_PATTERN,
    IBMAML_POSITIONAL_COLUMNS,
    IBMAML_RAW_HEADER,
    IBMAML_TIMESTAMP_FORMAT,
    IBMAML_TYPOLOGY_KEY_COLUMN,
    IBMAML_TYPOLOGY_VALUES,
    MINOR_EXPONENT_BY_NAME,
    TXN_TYPE_BY_PAYMENT_FORMAT,
    raw_ibm_aml_schema,
)
from oxbow.ingest.canonical import (
    CANONICAL_COLUMNS,
    CanonicalizationError,
    RunIdentity,
)
from oxbow.ingest.ibm_aml import (
    BALANCE_COLUMNS,
    CANONICAL_P1B_COLUMNS,
    IBMAML_SOURCE_NAME,
    ORDINAL_COLUMN,
    TXN_ID_DIGEST_HEX,
    TXN_ID_NAMESPACE,
    TXN_ID_ORDINAL_WIDTH,
    BatchIntegrityError,
    IBMAMLAdapter,
    IBMAMLIngestPolicy,
    TypologyAlignmentError,
    assert_row_satisfies_boundary_guards,
    canonicalize_batch,
    count_in_scan_rows,
    ingest_ibm_aml,
    policy_from_config,
    read_header,
    read_raw_batches,
    read_typology_artifact,
    schema_error_codes,
    validate_run_id,
)
from oxbow.ingest.paysim import IngestResult
from oxbow.ports.source import QuarantineRecord as PortQuarantineRecord
from oxbow.ports.source import SourceAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_YAML = REPO_ROOT / "config" / "pipeline.yaml"
SOURCES_YAML = REPO_ROOT / "config" / "sources.yaml"
IBMAML_RAW_DIR = REPO_ROOT / "data" / "raw" / "ibmaml"
TRANSACTION_CSV = IBMAML_RAW_DIR / "HI-Small_Trans.csv"
TYPOLOGY_ARTIFACT = REPO_ROOT / "data" / "processed" / "ibm_typologies.parquet"
MEASURE_CURRENCY_SCRIPT = REPO_ROOT / "scripts" / "measure_ibm_cycles.py"
BUILD_TYPOLOGIES_SCRIPT = REPO_ROOT / "scripts" / "build_ibm_typologies.py"
PLACEHOLDER = "RECORDED_AT_DOWNLOAD"

TEST_SALT = "oxbow-p1b-test-salt-0001"
TEST_BATCH_ID = "0123456789ab"
RUN_ID = "01J6ZXN4T8V3WKQM5RPSGHYBED"  # 26 chars of Crockford base32, no I/L/O/U
INGESTED_AT = dt.datetime(2026, 9, 26, 12, 0, 0, tzinfo=dt.UTC)
KAMPALA = ZoneInfo("Africa/Kampala")

# The real ``ibmaml:`` block. ``test_ibmaml_config_declares_the_assumption_and_no_epoch``
# re-reads config/pipeline.yaml and asserts this dict still equals it, so the fixture
# cannot drift from the values production reads.
CONFIG_BLOCK: dict[str, Any] = {
    "source_timezone_assumption": "Africa/Kampala",
    "timestamp_format": IBMAML_TIMESTAMP_FORMAT,
    "typology_join_artifact": "data/processed/ibm_typologies.parquet",
}

HEADER = ",".join(IBMAML_RAW_HEADER)

# Six hand-built rows over the real header:
#   Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,
#   Amount Paid,Payment Currency,Payment Format,Is Laundering
#
#   r0  00:20  010   8000EBD30 -> 010  8000EBD30   self, 3697.34 USD, Reinvestment
#       (the first data row of HI-Small_Trans.csv, copied from the bytes)
#   r1  00:20  03208 8000F4580 -> 001  8000F5340   0.01 USD, Cheque (the file's row 2)
#   r2  16:18  021   81AAA0001 -> 045  82BBB0002   0.281983 BTC, Bitcoin, Is Laundering 1
#   r3  09:05  010   8000EBD31 -> 011  8000EBD32   received 100.00 EUR, PAID 97.50 EUR
#   r4  23:40  007   8000CC0001 -> 002 8000DD0002  received 1000.00 USD, PAID 920.00 EUR
#   r5  12:00  010   8000EE0001 -> 010  8000EE0001 14918.11 JPY, Cash, self
#
# Timestamps use the slash separator the bytes carry (0x2f at offsets 4 and 7 of the
# first data row, confirmed from a hex dump rather than from how a terminal renders it).
# r0/r5 are the self-transfers that must survive; r2 the six-decimal Bitcoin amount; r3
# the spread that has no canonical column; r4 the case where the receiving and payment
# currencies differ; r5 the two-decimal Yen amount ISO-4217 would have rounded away.
FIXTURE_ROWS: tuple[tuple[str, ...], ...] = (
    (
        "2022/09/01 00:20",
        "010",
        "8000EBD30",
        "010",
        "8000EBD30",
        "3697.34",
        "US Dollar",
        "3697.34",
        "US Dollar",
        "Reinvestment",
        "0",
    ),
    (
        "2022/09/01 00:20",
        "03208",
        "8000F4580",
        "001",
        "8000F5340",
        "0.01",
        "US Dollar",
        "0.01",
        "US Dollar",
        "Cheque",
        "0",
    ),
    (
        "2022/09/18 16:18",
        "021",
        "81AAA0001",
        "045",
        "82BBB0002",
        "0.281983",
        "Bitcoin",
        "0.281983",
        "Bitcoin",
        "Bitcoin",
        "1",
    ),
    (
        "2022/09/02 09:05",
        "010",
        "8000EBD31",
        "011",
        "8000EBD32",
        "100.00",
        "Euro",
        "97.50",
        "Euro",
        "Wire",
        "0",
    ),
    (
        "2022/09/05 23:40",
        "007",
        "8000CC0001",
        "002",
        "8000DD0002",
        "1000.00",
        "US Dollar",
        "920.00",
        "Euro",
        "ACH",
        "0",
    ),
    (
        "2022/09/06 12:00",
        "010",
        "8000EE0001",
        "010",
        "8000EE0001",
        "14918.11",
        "Yen",
        "14918.11",
        "Yen",
        "Cash",
        "0",
    ),
)

ROW_COUNT = len(FIXTURE_ROWS)

# Column position by name. Every mutation below goes through this map rather than
# through a literal index: the header has two columns named ``Account`` and eight other
# slots that differ only by position, and a test that edits the wrong one silently tests
# nothing.
IDX = {name: index for index, name in enumerate(IBMAML_POSITIONAL_COLUMNS)}

# Amount Paid at each currency's own minor exponent, computed by hand:
#   3697.34 USD  -> 3697*100 + 34   = 369734
#      0.01 USD  -> 1
#   0.281983 BTC -> 0*1e8 + 28198300 = 28198300
#     97.50 EUR  -> 9750            (NOT 10000: received and paid differ, r3)
#    920.00 EUR  -> 92000           (NOT 100000: the payment currency is EUR, r4)
#  14918.11 JPY  -> 1491811         (two places, although ISO-4217 gives JPY zero)
EXPECTED_AMOUNT_MINOR: tuple[int, ...] = (369734, 1, 28198300, 9750, 92000, 1491811)
EXPECTED_CURRENCY: tuple[str, ...] = ("USD", "USD", "BTC", "EUR", "EUR", "JPY")

# ``channel`` carries the corpus's own Payment Format spelling; ``txn_type`` carries the
# stable upper-case token. One mapping, stated in the contract, asserted here.
EXPECTED_CHANNEL: tuple[str, ...] = (
    "Reinvestment",
    "Cheque",
    "Bitcoin",
    "Wire",
    "ACH",
    "Cash",
)
EXPECTED_TXN_TYPE: tuple[str, ...] = (
    "REINVESTMENT",
    "CHEQUE",
    "BITCOIN",
    "WIRE",
    "ACH",
    "CASH",
)

# The file's wall clock is read as Africa/Kampala (UTC+3, no DST) under the assumption the
# config declares, so event_ts_utc is three hours EARLIER than the printed minute.
# Hand-computed, including the two rows that fall on the previous UTC day:
#   2022/09/01 00:20 local -> 2022/08/31 21:20 UTC
#   2022/09/18 16:18 local -> 2022/09/18 13:18 UTC
#   2022/09/02 09:05 local -> 2022/09/02 06:05 UTC
#   2022/09/05 23:40 local -> 2022/09/05 20:40 UTC
#   2022/09/06 12:00 local -> 2022/09/06 09:00 UTC
EXPECTED_EVENT_TS_UTC: tuple[dt.datetime, ...] = (
    dt.datetime(2022, 8, 31, 21, 20, tzinfo=dt.UTC),
    dt.datetime(2022, 8, 31, 21, 20, tzinfo=dt.UTC),
    dt.datetime(2022, 9, 18, 13, 18, tzinfo=dt.UTC),
    dt.datetime(2022, 9, 2, 6, 5, tzinfo=dt.UTC),
    dt.datetime(2022, 9, 5, 20, 40, tzinfo=dt.UTC),
    dt.datetime(2022, 9, 6, 9, 0, tzinfo=dt.UTC),
)

# The local hour and date are the file's own wall clock, which is the point of the column
# (03 C): an ODD_HOUR_SHIFT rule must read Kampala hours, not UTC hours.
EXPECTED_LOCAL_HOUR: tuple[int, ...] = (0, 0, 16, 9, 23, 12)
EXPECTED_LOCAL_DATE: tuple[dt.date, ...] = (
    dt.date(2022, 9, 1),
    dt.date(2022, 9, 1),
    dt.date(2022, 9, 18),
    dt.date(2022, 9, 2),
    dt.date(2022, 9, 5),
    dt.date(2022, 9, 6),
)

# ``Is Laundering`` is the corpus's only label column.
EXPECTED_LABEL_IS_FRAUD: tuple[int, ...] = (0, 0, 1, 0, 0, 0)

# Which fixture rows are self-transfers, by (bank, account) pair: r0 and r5.
SELF_ROWS: tuple[int, ...] = (0, 5)

# The reason codes the row-level mask can emit. ``amount_overflow`` and
# ``null_in_required_column`` are the two the frame schema expresses through dtype and
# nullability rather than through a named check.
MASK_CODES: tuple[str, ...] = (
    "null_in_required_column",
    "timestamp_format",
    "timestamp_out_of_range",
    "amount_format",
    "amount_scale",
    "amount_overflow",
    "label_not_binary",
    "currency_shape",
    "unknown_currency",
    "unknown_payment_format",
    "account_identity_missing",
)

EMPTY_JOIN = pl.DataFrame(
    {ORDINAL_COLUMN: [], "label_typology": []},
    schema={ORDINAL_COLUMN: pl.Int64, "label_typology": pl.String},
)


def make_identity(batch_id: str = TEST_BATCH_ID) -> RunIdentity:
    return RunIdentity(run_salt=TEST_SALT, batch_id=batch_id)


def make_policy(artifact: Path | None = None) -> IBMAMLIngestPolicy:
    return IBMAMLIngestPolicy(
        source_timezone=KAMPALA,
        timestamp_format=IBMAML_TIMESTAMP_FORMAT,
        typology_artifact=artifact or REPO_ROOT / CONFIG_BLOCK["typology_join_artifact"],
        deployment_timezone="Africa/Kampala",
    )


def raw_frame(rows: list[list[str]] | tuple[tuple[str, ...], ...] | None = None) -> pl.DataFrame:
    """A frame in the shape ``read_raw_batches`` produces: positional, all String."""
    data = [list(row) for row in (FIXTURE_ROWS if rows is None else rows)]
    return pl.DataFrame(
        {name: [row[i] for row in data] for i, name in enumerate(IBMAML_POSITIONAL_COLUMNS)}
    )


def mutated(column: str, value: str, *, rows: list[list[str]] | None = None) -> list[list[str]]:
    """The fixture with one column replaced in every row."""
    body = [list(row) for row in (FIXTURE_ROWS if rows is None else rows)]
    index = list(IBMAML_POSITIONAL_COLUMNS).index(column)
    for row in body:
        row[index] = value
    return body


def artifact_frame(tmp_path: Path, rows: list[tuple[int, str, str]]) -> pl.DataFrame:
    """A join artifact in the builder's own shape, read back through the real loader.

    ``rows`` are (ordinal, attempt_id, typology). It is written as Parquet with the column
    names ``scripts/build_ibm_typologies.py`` writes, so the tests exercise
    :func:`read_typology_artifact` rather than bypassing it.
    """
    table = pl.DataFrame(
        {
            IBMAML_TYPOLOGY_KEY_COLUMN: [ordinal for ordinal, _attempt, _typology in rows],
            "attempt_id": [attempt for _ordinal, attempt, _typology in rows],
            "typology": [typology for _ordinal, _attempt, typology in rows],
            "attempt_description": ["fixture block"] * len(rows),
        },
        schema={
            IBMAML_TYPOLOGY_KEY_COLUMN: pl.Int64,
            "attempt_id": pl.String,
            "typology": pl.String,
            "attempt_description": pl.String,
        },
    )
    path = tmp_path / "ibm_typologies.parquet"
    table.write_parquet(path)
    return read_typology_artifact(path)


def fixture_csv(
    tmp_path: Path,
    rows: list[list[str]] | tuple[tuple[str, ...], ...] | None = None,
    *,
    header: str = HEADER,
    trailing_newline: bool = True,
    extra_blank_line_at_eof: bool = False,
) -> Path:
    body = rows if rows is not None else list(FIXTURE_ROWS)
    lines = [header, *(",".join(str(value) for value in row) for row in body)]
    text = "\n".join(lines) + ("\n" if trailing_newline else "")
    if extra_blank_line_at_eof:
        text += "\n"
    path = tmp_path / "HI-Small_Trans.csv"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def ingest_file(
    path: Path,
    *,
    identity: RunIdentity | None = None,
    policy: IBMAMLIngestPolicy | None = None,
    typology: pl.DataFrame | None = None,
    batch_rows: int = 100_000,
    limit: int | None = None,
    run_id: str = RUN_ID,
    ingested_at: dt.datetime | None = INGESTED_AT,
) -> IngestResult:
    return ingest_ibm_aml(
        path,
        identity or make_identity(),
        run_id=run_id,
        deployment_tz=KAMPALA,
        policy=policy or make_policy(),
        typology=typology if typology is not None else EMPTY_JOIN,
        batch_rows=batch_rows,
        limit=limit,
        ingested_at=ingested_at,
        use_process_pool=False,
    )


def canonicalize(
    frame: pl.DataFrame,
    *,
    identity: RunIdentity | None = None,
    typology: pl.DataFrame | None = None,
    ingested_at: dt.datetime | None = INGESTED_AT,
) -> IngestResult:
    return canonicalize_batch(
        frame,
        identity or make_identity(),
        run_id=RUN_ID,
        policy=make_policy(),
        deployment_tz=KAMPALA,
        typology=typology if typology is not None else EMPTY_JOIN,
        ingested_at=ingested_at,
    )


def clean_events(tmp_path: Path) -> pl.DataFrame:
    result = ingest_file(fixture_csv(tmp_path))
    result.assert_no_quarantine()
    return result.events


# --- the contract is strict, against the real columns ----------------------


def test_contract_accepts_the_shape_the_bytes_are_in() -> None:
    """The fixture is the contract's happy path; asserted first so every failure below is
    about the mutation, not about a broken fixture."""
    raw_ibm_aml_schema.validate(raw_frame())


def test_the_declared_header_has_eleven_columns_and_two_are_named_account() -> None:
    """The fact that broke the previous adapter, pinned where nobody can forget it.

    ``Account`` appears twice, so the header is not a set: deduplicating it — or reading
    the file by name — loses one side of the pair and turns every transaction into a
    transfer to itself, which is precisely the kind of defect a green test suite cannot
    see.
    """
    assert len(IBMAML_RAW_HEADER) == 11
    assert len(set(IBMAML_RAW_HEADER)) == 10
    assert IBMAML_RAW_HEADER.count("Account") == 2
    assert IBMAML_RAW_HEADER[2] == "Account"
    assert IBMAML_RAW_HEADER[4] == "Account"
    assert list(IBMAML_RAW_HEADER) == HEADER.split(",")
    assert len(IBMAML_POSITIONAL_COLUMNS) == 11
    assert len(set(IBMAML_POSITIONAL_COLUMNS)) == 11
    assert IBMAML_POSITIONAL_COLUMNS[2] == "from_account"
    assert IBMAML_POSITIONAL_COLUMNS[4] == "to_account"


@pytest.mark.skipif(not TRANSACTION_CSV.is_file(), reason="corpus not on this host")
def test_the_declared_header_is_the_header_on_disk() -> None:
    """The contract is checked against the bytes, not against a description of them.

    This is the test the previous revision could not have: it asserts the first line of
    the real ``HI-Small_Trans.csv`` equals the declared header verbatim, duplicate
    included.
    """
    assert read_header(TRANSACTION_CSV) == list(IBMAML_RAW_HEADER)


@pytest.mark.parametrize(
    "invented",
    [
        "stepFrom",
        "stepTo",
        "Type",
        "Category",
        "Amount",
        "nameOrig",
        "balanceOrig",
        "nameDest",
        "balanceDest",
        "isLaundering",
        "isFlood",
        "isDateSpam",
        "isForcedCashout",
        "unlabeled",
    ],
)
def test_no_column_of_the_fabricated_schema_survives_anywhere(invented: str) -> None:
    """Every name the old contract invented is refused here and absent from config.

    Not a style rule: ``isFlood``/``isDateSpam``/``isForcedCashout``/``unlabeled`` are what
    the deleted typology tie-break ordered, ``balanceOrig``/``balanceDest`` what the
    deleted balance mapping read, and ``stepFrom``/``stepTo`` what ``event_ts_utc`` used to
    be expanded from. Letting any of them back in re-imports the fiction.
    """
    assert invented not in IBMAML_RAW_HEADER
    assert invented not in IBMAML_POSITIONAL_COLUMNS
    assert invented not in CANONICAL_P1B_COLUMNS
    frame = raw_frame().with_columns(pl.Series(invented, ["0"] * ROW_COUNT, dtype=pl.String))
    with pytest.raises(SchemaError) as excinfo:
        raw_ibm_aml_schema.validate(frame)
    assert invented in str(excinfo.value)
    block = yaml.safe_load(PIPELINE_YAML.read_text(encoding="utf-8"))["ibmaml"]
    assert invented not in block


def test_contract_rejects_unknown_column_and_names_it() -> None:
    """01 P1b / ``test_unknown_column_fails_closed``: an undeclared column fails the batch
    and says which one, instead of becoming an all-null feature nobody notices until a
    demo — and on this corpus that feature sits inside the Module B thesis (DEV-011)."""
    frame = raw_frame().with_columns(pl.Series("isSmurfing", ["0"] * ROW_COUNT, dtype=pl.String))
    with pytest.raises(SchemaError) as excinfo:
        raw_ibm_aml_schema.validate(frame)
    assert "isSmurfing" in str(excinfo.value)
    assert "not in DataFrameSchema" in str(excinfo.value)


def test_contract_rejects_a_missing_required_column() -> None:
    """A column the corpus stopped shipping is a source change, not a null."""
    with pytest.raises(SchemaError) as excinfo:
        raw_ibm_aml_schema.validate(raw_frame().drop("payment_format"))
    assert "payment_format" in str(excinfo.value)


def test_contract_rejects_a_money_column_read_as_float() -> None:
    """Amount must arrive as text. Read as Float64, the string-level check that refuses
    ``1e9`` and ``nan`` could never run, because the damage happened in the reader."""
    frame = raw_frame().with_columns(pl.col("amount_paid").cast(pl.Float64))
    with pytest.raises(SchemaError) as excinfo:
        raw_ibm_aml_schema.validate(frame)
    assert "amount_paid" in str(excinfo.value)


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("amount_paid", "4647.641", "amount_scale"),
        ("amount_paid", "-5.00", "amount_format"),
        ("amount_paid", "1e9", "amount_format"),
        ("amount_paid", "nan", "amount_format"),
        ("amount_paid", "", "amount_format"),
        ("amount_received", "12.", "amount_format"),
        ("payment_ccy", "Dinar", "unknown_currency"),
        ("receiving_ccy", "Peso", "unknown_currency"),
        ("payment_ccy", "US Dollar, off", "currency_shape"),
        ("payment_format", "Zelle", "unknown_payment_format"),
        ("is_laundering", "2", "label_not_binary"),
        ("is_laundering", "", "label_not_binary"),
        ("ts", "2022/9/1 00:20", "timestamp_format"),
        ("ts", "2022/09/01T00:20", "timestamp_format"),
        ("ts", "2022/09/01 00:60", "timestamp_out_of_range"),
        ("from_bank", "10x", "account_identity_missing"),
        ("from_account", "8000ebd30", "account_identity_missing"),
        ("to_bank", "", "account_identity_missing"),
    ],
    ids=[
        "paid_three_decimals_on_a_two_digit_currency",
        "paid_negative",
        "paid_exponent",
        "paid_nan",
        "paid_empty",
        "received_trailing_point",
        "unknown_payment_currency",
        "unknown_receiving_currency",
        "currency_with_a_comma",
        "unknown_payment_format",
        "label_two",
        "label_empty",
        "timestamp_single_digit_fields",
        "timestamp_iso_T_separator",
        "timestamp_minute_60_never_happened",
        "bank_with_a_letter",
        "lowercase_account",
        "bank_missing",
    ],
)
def test_contract_fails_closed_naming_its_check_code(column: str, value: str, code: str) -> None:
    """Each check carries its own machine-readable code, so a quarantine reads
    "unknown_currency, 412 rows" instead of quoting a stack trace.

    Note the first case: a three-place amount is refused as ``amount_scale`` — the currency
    has no third minor unit — rather than as a malformed number, and it is refused at all
    because rounding it would be a silent repair.
    """
    with pytest.raises(SchemaError) as excinfo:
        raw_ibm_aml_schema.validate(raw_frame(mutated(column, value)))
    assert code in str(excinfo.value), str(excinfo.value)[:300]


def test_bitcoin_is_allowed_six_places_because_the_currency_has_eight() -> None:
    """The scale check is about the currency, not about the file.

    146,066 rows of this corpus carry six fractional digits and all of them are Bitcoin.
    A flat two-decimal rule would have quarantined every one of them; a flat
    "multiply by one hundred" rule would have rounded them instead. Both refuse real money,
    and the one that rounds is worse because it stays green.
    """
    assert MINOR_EXPONENT_BY_NAME["Bitcoin"] == 8
    assert MINOR_EXPONENT_BY_NAME["Yen"] == 2
    rows = [list(row) for row in FIXTURE_ROWS]
    rows[0][IDX["amount_paid"]] = "0.281983"
    rows[0][IDX["payment_ccy"]] = "Bitcoin"
    raw_ibm_aml_schema.validate(raw_frame(rows))


def test_the_dashed_spelling_is_accepted_because_the_bundle_uses_both() -> None:
    """``HI-Small_Trans.csv`` writes slashes and ``HI-Small_Patterns.txt`` writes dashes
    for the same instants, so the shape both files accept is the shape that defines the
    row ordinal."""
    rows = [list(row) for row in FIXTURE_ROWS]
    for row in rows:
        row[IDX["ts"]] = row[IDX["ts"]].replace("/", "-")
    raw_ibm_aml_schema.validate(raw_frame(rows))


# --- money ----------------------------------------------------------------


def test_amount_minor_is_amount_paid_at_the_currencys_own_scale(tmp_path: Path) -> None:
    events = clean_events(tmp_path)
    assert events.sort("txn_id")["amount_minor"].to_list() == list(EXPECTED_AMOUNT_MINOR)
    assert events.schema["amount_minor"] == pl.Int64
    assert events.sort("txn_id")["currency"].to_list() == list(EXPECTED_CURRENCY)


def test_amount_received_is_not_what_becomes_amount_minor(tmp_path: Path) -> None:
    """r3 receives 100.00 EUR and pays 97.50 EUR: the canonical amount is 9750 minor units
    — what left the sender — and 10000 appears nowhere in the frame.

    The 2.50 EUR spread has no slot in canonical v1 and is therefore dropped. Measured over
    the whole file, 72,158 rows (1.4209%) have ``amount_paid`` different from
    ``amount_received``, so the dropped figure is a real loss and belongs in LIMITATIONS,
    not in an invented column.
    """
    events = clean_events(tmp_path)
    row = events.sort("txn_id").with_row_index("i").filter(pl.col("i") == 3)
    assert row["amount_minor"].to_list() == [9750]
    assert 10000 not in events["amount_minor"].to_list()
    assert "amount_received_minor" not in CANONICAL_P1B_COLUMNS


def test_a_cross_currency_row_is_labelled_with_the_currency_it_paid_in(tmp_path: Path) -> None:
    """r4 receives US Dollars and pays Euros, so every total that crosses it is in EUR.
    Reading the receiving currency instead would mislabel the 72,170 rows (1.4211%) where
    the two names differ."""
    events = clean_events(tmp_path)
    row = events.sort("txn_id").with_row_index("i").filter(pl.col("i") == 4)
    assert row["currency"].to_list() == ["EUR"]
    assert row["amount_minor"].to_list() == [92000]


def test_bitcoin_money_is_exact_at_eight_decimals(tmp_path: Path) -> None:
    """0.281983 BTC is 28,198,300 in the corpus's own minor units, not 28 cents."""
    events = clean_events(tmp_path)
    row = events.sort("txn_id").with_row_index("i").filter(pl.col("i") == 2)
    assert row["amount_minor"].to_list() == [28198300]


def test_yen_keeps_two_places_although_iso_4217_gives_it_zero(tmp_path: Path) -> None:
    """The exponent follows the bytes.

    ISO-4217 says JPY has no minor unit, but every Yen amount in this corpus is written to
    two places (``14918.11``) and so is every other non-Bitcoin value: measured, all
    4,932,279 non-Bitcoin rows carry exactly two fractional digits. Following the standard
    instead of the data would quarantine them all, which is how a theoretically correct
    rule becomes an outage.
    """
    events = clean_events(tmp_path)
    row = events.sort("txn_id").with_row_index("i").filter(pl.col("i") == 5)
    assert row["currency"].to_list() == ["JPY"]
    assert row["amount_minor"].to_list() == [1491811]


def test_zero_amount_survives_ingest(tmp_path: Path) -> None:
    """Plan §8 keeps a zero amount as a row the feature layer is specified to see, so it is
    not refused at the boundary. The real file's minimum is 0.01; a zero is legal text
    either way."""
    rows = [list(row) for row in FIXTURE_ROWS]
    rows[1][IDX["amount_paid"]] = "0.00"
    result = canonicalize(raw_frame(rows))
    result.assert_no_quarantine()
    assert result.events["amount_minor"].to_list()[1] == 0


def test_an_amount_that_would_wrap_int64_is_quarantined(tmp_path: Path) -> None:
    """Polars does not check integer overflow, so a scale guard is the only thing between a
    huge amount and a negative one. Measured maximum in this file is 13 integer digits
    (1,046,302,363,293.48); eighteen is the fixture, and it is refused rather than
    wrapped."""
    rows = [list(row) for row in FIXTURE_ROWS]
    rows[1][IDX["amount_paid"]] = "123456789012345678.90"
    result = ingest_file(fixture_csv(tmp_path, rows))
    assert [record.reason for record in result.quarantined] == ["amount_overflow"]
    assert result.events.height == ROW_COUNT - 1


def test_a_bad_value_quarantines_its_row_and_not_the_batch(tmp_path: Path) -> None:
    """Per-row triage, with the original row attached.

    One unmapped currency name in a batch of 100,000 must cost one row, not the batch: a
    12% quarantine rate is what would fail this run on real data, and a run that fails on
    valid data is the defect this phase exists to remove.
    """
    rows = [list(row) for row in FIXTURE_ROWS]
    rows[3][IDX["payment_ccy"]] = "Dinar"
    result = ingest_file(fixture_csv(tmp_path, rows))
    assert result.rows_read == ROW_COUNT
    assert result.events.height == ROW_COUNT - 1
    assert [record.reason for record in result.quarantined] == ["unknown_currency"]
    record = result.quarantined[0]
    assert record.source_dataset == IBMAML_SOURCE_NAME
    assert record.payload["payment_ccy"] == "Dinar"
    assert record.payload["amount_paid"] == "97.50"
    assert "unknown_currency" in record.detail
    with pytest.raises(CanonicalizationError):
        result.assert_no_quarantine()


def test_the_row_reason_priority_reports_one_cause_per_row(tmp_path: Path) -> None:
    """A row that fails three checks is reported once, by the highest-priority cause, so
    counts by reason sum to the quarantine count instead of exceeding it."""
    rows = [list(row) for row in FIXTURE_ROWS]
    rows[1][IDX["amount_paid"]] = "-1.00"  # amount_format
    rows[1][IDX["payment_ccy"]] = "Dinar"  # unknown_currency
    rows[1][IDX["payment_format"]] = "Zelle"  # unknown_payment_format
    result = ingest_file(fixture_csv(tmp_path, rows))
    assert [record.reason for record in result.quarantined] == ["amount_format"]


def test_the_shared_scalar_parser_would_have_rounded_so_this_adapter_does_not_use_it() -> None:
    """Why the money path is written out rather than delegated.

    ``ingest/canonical.py``'s ``parse_amount_minor`` is the shared scalar primitive, fixed
    at one hundred minor units per major unit and quantising anything finer with
    ROUND_HALF_EVEN — hand-computed: ``12.345`` -> 1234.5 -> ties-to-even -> 1234, and
    ``0.281983`` -> 28.1983 -> 28. On PaySim that path is unreachable because every row has
    two places; on this corpus it would have flattened all 146,066 Bitcoin amounts to
    cent scale. The contract's ``amount_scale`` check is what refuses instead.
    """
    from oxbow.ingest.canonical import parse_amount_minor

    assert parse_amount_minor("12.345") == 1234
    assert parse_amount_minor("0.281983") == 28

    rows = [list(row) for row in FIXTURE_ROWS]
    rows[0][IDX["amount_received"]] = "0.281983"  # the column the contract does not gate
    result = canonicalize(raw_frame(rows))
    result.assert_no_quarantine()
    assert result.events["amount_minor"].to_list()[0] == 369734  # still amount_paid


# --- identity -------------------------------------------------------------


def oracle_txn_id(ordinal: int, fields: tuple[str, ...]) -> str:
    """Independent implementation of the documented id scheme.

    ``ibmaml:<zero-padded ordinal>:<first 8 hex of HMAC-SHA256 keyed by the namespace over
    the row's eleven fields joined with \\x1f>``. Written from the layout rather than from
    the adapter, so reordering the fields or changing the key fails this file instead of
    being followed by it.
    """
    content = "\x1f".join(fields)
    digest = hmac.new(b"ibmaml", content.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"ibmaml:{ordinal:09d}:{digest[:8]}"


def test_txn_id_is_namespaced_ordinal_padded_and_digest_traced(tmp_path: Path) -> None:
    events = clean_events(tmp_path)
    expected = [oracle_txn_id(index, row) for index, row in enumerate(FIXTURE_ROWS)]
    assert sorted(events["txn_id"].to_list()) == sorted(expected)
    for txn_id in events["txn_id"].to_list():
        namespace, ordinal, digest = txn_id.split(":")
        assert namespace == TXN_ID_NAMESPACE == IBMAML_SOURCE_NAME
        assert len(ordinal) == TXN_ID_ORDINAL_WIDTH
        assert ordinal.isdigit()
        assert len(digest) == TXN_ID_DIGEST_HEX
        assert all(char in "0123456789abcdef" for char in digest)


def test_two_identical_rows_survive_with_distinct_ids(tmp_path: Path) -> None:
    """The uniqueness proof this corpus actually needs.

    Nine rows of ``HI-Small_Trans.csv`` are identical across all eleven columns (measured:
    5,078,345 rows, 5,078,336 distinct), and the corpus repeats (sender, receiver, amount)
    inside one minute-precision timestamp freely. A content-derived id therefore collides
    on rows that are genuinely two transactions, and the graph layer's ``require_events``
    rightly refuses a duplicate ``txn_id`` because the total order is
    ``(event_ts_utc, txn_id)``. The ordinal is what lets both rows survive; dropping one
    would leave the run nine rows short of the truth in silence.
    """
    rows = [list(row) for row in FIXTURE_ROWS] + [list(FIXTURE_ROWS[0]), list(FIXTURE_ROWS[0])]
    result = ingest_file(fixture_csv(tmp_path, rows))
    result.assert_no_quarantine()
    assert result.rows_read == ROW_COUNT + 2
    assert result.events.height == ROW_COUNT + 2
    ids = result.events["txn_id"].to_list()
    assert len(ids) == len(set(ids)), "a duplicate txn_id would be rejected by the graph layer"
    assert oracle_txn_id(6, FIXTURE_ROWS[0]) in ids
    assert oracle_txn_id(7, FIXTURE_ROWS[0]) in ids
    assert oracle_txn_id(6, FIXTURE_ROWS[0]) != oracle_txn_id(7, FIXTURE_ROWS[0])


def test_txn_id_is_stable_across_batch_sizes_limits_and_batch_ids(tmp_path: Path) -> None:
    """A position-derived id would move when the chunk size moved and every join downstream
    would silently miss; a content-only id collides. This pins both halves."""
    path = fixture_csv(tmp_path)
    by_one = ingest_file(path, batch_rows=1)
    by_six = ingest_file(path, batch_rows=6)
    limited = ingest_file(path, batch_rows=2, limit=4)
    other_batch = ingest_file(path, identity=make_identity("aaaaaaaaaaaa"))
    assert sorted(by_one.events["txn_id"].to_list()) == sorted(by_six.events["txn_id"].to_list())
    assert sorted(by_one.events["txn_id"].to_list()) == sorted(
        other_batch.events["txn_id"].to_list()
    )
    # A limit truncates the id set; it does not renumber the rows that remain.
    assert set(limited.events["txn_id"].to_list()) <= set(by_one.events["txn_id"].to_list())


def test_the_digest_half_of_the_id_tracks_the_row_content(tmp_path: Path) -> None:
    """Same ordinal, different amount -> different digest. The digest is what lets an
    operator trace an id quoted in a packet back to the row that produced it."""
    changed = [list(row) for row in FIXTURE_ROWS]
    changed[0][7] = "3697.35"
    first = ingest_file(fixture_csv(tmp_path, list(FIXTURE_ROWS)))
    second = ingest_file(fixture_csv(tmp_path, changed))
    left = first.events["txn_id"].to_list()[0].split(":")
    right = second.events["txn_id"].to_list()[0].split(":")
    assert left[1] == right[1] == "000000000"
    assert left[2] != right[2]


def test_two_corpora_cannot_collide_on_txn_id(tmp_path: Path) -> None:
    """PaySim row 41 and IBM row 41 are different transactions; the namespace prefix is
    what keeps them two rows in one table (DEV-004)."""
    ids = clean_events(tmp_path)["txn_id"].to_list()
    assert all(txn.startswith("ibmaml:") for txn in ids)
    assert not any(txn.startswith("paysim:") for txn in ids)


def oracle_account_key(salt: str, bank: str, account: str) -> str:
    """Independent implementation of the documented scheme: HMAC-SHA256 keyed by the salt
    over ``"<salt>|<bank>|<account>"``, truncated to 12 hex characters (48 bits)."""
    message = f"{salt}|{bank}|{account}"
    return hmac.new(salt.encode(), message.encode(), hashlib.sha256).hexdigest()[:12]


def test_account_key_matches_an_independent_implementation_and_hashes_the_pair(
    tmp_path: Path,
) -> None:
    """03 D and sources.yaml, over the pair rather than the account.

    ``HI-Small_accounts.csv`` keys an entity on Bank ID plus Account Number, and four
    account numbers recur under two different banks in this file (measured), so hashing the
    account string alone would merge distinct accounts into one node and hand each other
    their counterparties, degree and community.
    """
    events = clean_events(tmp_path).sort("txn_id")
    assert events["account_from"].to_list() == [
        oracle_account_key(TEST_SALT, row[1], row[2]) for row in FIXTURE_ROWS
    ]
    assert events["account_to"].to_list() == [
        oracle_account_key(TEST_SALT, row[3], row[4]) for row in FIXTURE_ROWS
    ]
    for value in events["account_from"].to_list() + events["account_to"].to_list():
        assert len(value) == 12
        assert all(char in "0123456789abcdef" for char in value)


def test_the_same_account_number_under_two_banks_is_two_nodes() -> None:
    """The collision the pair-keying exists to prevent, made concrete."""
    rows = [list(row) for row in FIXTURE_ROWS]
    rows[1][IDX["from_bank"]] = "010"
    rows[1][IDX["from_account"]] = "8000EBD30"  # r0's account number, and r0's bank too
    events = canonicalize(raw_frame(rows)).events.sort("txn_id")
    assert events["account_from"].to_list()[0] == events["account_from"].to_list()[1]
    apart = oracle_account_key(TEST_SALT, "011", "8000EBD30")
    assert apart != events["account_from"].to_list()[0], "the bank must be part of the identity"


def test_account_key_is_stable_across_runs_with_a_fixed_salt_and_changes_without_it(
    tmp_path: Path,
) -> None:
    """Same salt -> the same account in a different batch of a different run; a different
    salt -> a different key, which is what makes erasure possible (destroy the salt, the
    chain survives and the subject is unrecoverable)."""
    path = fixture_csv(tmp_path)
    run_a = clean_events(tmp_path)
    run_b = ingest_file(path, identity=make_identity("bbbbbbbbbbbb"))
    assert run_b.quarantine_count == 0
    assert (
        run_a.sort("txn_id")["account_from"].to_list()
        == run_b.events.sort("txn_id")["account_from"].to_list()
    )

    resalted = canonicalize(
        raw_frame(), identity=RunIdentity("a-different-salt-entirely", TEST_BATCH_ID)
    )
    assert sorted(resalted.events["account_from"].to_list()) != sorted(
        run_a["account_from"].to_list()
    )
    # Ids survive the rotation, because a txn_id is not personal data.
    assert sorted(resalted.events["txn_id"].to_list()) == sorted(run_a["txn_id"].to_list())


def test_the_raw_bank_and_account_ids_never_leave_the_adapter(tmp_path: Path) -> None:
    """The PII boundary: no canonical column carries an upstream identifier. IBM's ids are
    already pseudonymous hashes and are still re-salted, so this asserts the stronger claim
    too — nothing in the output even resembles an input."""
    events = clean_events(tmp_path)
    raw_ids = {value for row in FIXTURE_ROWS for value in (row[1], row[2], row[3], row[4])}
    for column in events.columns:
        values = {value for value in events[column].to_list() if isinstance(value, str)}
        assert not raw_ids & values, f"{column} leaked a raw bank or account id"


def test_endpoints_are_not_transposed(tmp_path: Path) -> None:
    """The specific hazard of two columns named ``Account``: a reader that resolved them by
    name would send every transaction to itself, and a suite that never checked would look
    healthy while it did. r1 has a different bank and account on each side, so a swap is
    visible as a mismatch."""
    events = clean_events(tmp_path).sort("txn_id")
    row = events.with_row_index("i").filter(pl.col("i") == 1)
    assert row["account_from"].to_list() == [oracle_account_key(TEST_SALT, "03208", "8000F4580")]
    assert row["account_to"].to_list() == [oracle_account_key(TEST_SALT, "001", "8000F5340")]


# --- labels and the typology join -----------------------------------------


def test_label_is_fraud_is_the_only_label_the_corpus_carries(tmp_path: Path) -> None:
    events = clean_events(tmp_path).sort("txn_id")
    assert events["label_is_fraud"].to_list() == list(EXPECTED_LABEL_IS_FRAUD)
    assert events.schema["label_is_fraud"] == pl.Int8
    assert IBMAML_LABEL_COLUMN == "is_laundering"


def test_label_is_flagged_is_zero_on_every_row_because_no_such_concept_exists() -> None:
    """No threshold-flag concept exists in this corpus. Canonical v1 requires the column
    non-null, so the value is 0 and the meaning is "no equivalent", NOT "checked and
    cleared" — which is why the balance columns stay null while this one cannot."""
    events = canonicalize(raw_frame()).events.sort("txn_id")
    assert events["label_is_flagged"].to_list() == [0] * ROW_COUNT
    assert events.schema["label_is_flagged"] == pl.Int8


def test_typology_comes_from_the_join_artifact_not_a_label_column(tmp_path: Path) -> None:
    """The row keyed by ordinal gets its block's typology; nobody resolves a tie between
    columns that do not exist."""
    join = artifact_frame(tmp_path, [(2, "ibmaml:HI-Small:0001", "CYCLE")])
    events = canonicalize(raw_frame(), typology=join).events.sort("txn_id")
    assert events["label_typology"].to_list() == [None, None, "CYCLE", None, None, None]
    assert events["label_is_fraud"].to_list()[2] == 1


def test_unannotated_rows_have_no_typology_and_none_is_invented(tmp_path: Path) -> None:
    """1,968 of the corpus's 5,177 laundering positives (38.01% of them) are not inside an
    annotated block, and the other 5,073,168 rows are not launderers. Both are null: the
    corpus asserts nothing about them, and a label that reads like evidence is worse than
    an empty one."""
    join = artifact_frame(tmp_path, [(2, "ibmaml:HI-Small:0001", "CYCLE")])
    events = canonicalize(raw_frame(), typology=join).events
    unannotated = events.filter(pl.col("label_typology").is_null())
    assert unannotated.height == ROW_COUNT - 1
    assert set(unannotated["label_typology"].to_list()) == {None}
    assert "unlabeled" not in events["label_typology"].to_list()


def test_random_typology_rows_keep_their_own_string(tmp_path: Path) -> None:
    """RANDOM is a block the corpus generated without a pattern, and downstream uses it as
    the negative control. Collapsing it into "some other typology" or into null would
    destroy the only honest test of whether a rule detects a network or merely detects
    activity."""
    assert "RANDOM" in IBMAML_TYPOLOGY_VALUES
    join = artifact_frame(tmp_path, [(2, "ibmaml:HI-Small:0002", "RANDOM")])
    events = canonicalize(raw_frame(), typology=join).events.sort("txn_id")
    assert events["label_typology"].to_list()[2] == "RANDOM"


def test_an_annotated_row_without_the_laundering_flag_fails_the_run(tmp_path: Path) -> None:
    """The alignment invariant, with teeth.

    The join is keyed on the row ordinal in the timestamp-filtered scan, so a drift would
    still produce a plausible ``label_typology`` — just on the wrong rows. The corpus's own
    assertion (every annotated row carries ``Is Laundering == 1``) is what catches it, and
    the failure names the count.
    """
    join = artifact_frame(tmp_path, [(0, "ibmaml:HI-Small:0001", "CYCLE")])
    with pytest.raises(TypologyAlignmentError) as excinfo:
        canonicalize(raw_frame(), typology=join)
    message = str(excinfo.value)
    assert "1 annotated row" in message
    assert "is_laundering" in message


def test_two_misaligned_rows_report_a_count_of_two(tmp_path: Path) -> None:
    join = artifact_frame(
        tmp_path,
        [(0, "ibmaml:HI-Small:0001", "CYCLE"), (1, "ibmaml:HI-Small:0001", "CYCLE")],
    )
    with pytest.raises(TypologyAlignmentError) as excinfo:
        canonicalize(raw_frame(), typology=join)
    assert "2 annotated row" in str(excinfo.value)


def test_the_join_artifact_is_shape_checked_before_it_is_used(tmp_path: Path) -> None:
    """A silent change to the builder's output would otherwise arrive as data."""
    wrong_columns = tmp_path / "wrong_columns.parquet"
    pl.DataFrame({"ordinal": [1], "pattern": ["CYCLE"]}).write_parquet(wrong_columns)
    with pytest.raises(CanonicalizationError) as excinfo:
        read_typology_artifact(wrong_columns)
    assert "txn_ordinal" in str(excinfo.value)

    ninth = tmp_path / "ninth.parquet"
    pl.DataFrame(
        {IBMAML_TYPOLOGY_KEY_COLUMN: [1], "typology": ["FUNNEL"]},
        schema={IBMAML_TYPOLOGY_KEY_COLUMN: pl.Int64, "typology": pl.String},
    ).write_parquet(ninth)
    with pytest.raises(CanonicalizationError) as excinfo:
        read_typology_artifact(ninth)
    assert "FUNNEL" in str(excinfo.value)

    twice = tmp_path / "twice.parquet"
    pl.DataFrame(
        {
            IBMAML_TYPOLOGY_KEY_COLUMN: [1, 1],
            "typology": ["CYCLE", "STACK"],
            "attempt_id": ["a", "b"],
        },
        schema={
            IBMAML_TYPOLOGY_KEY_COLUMN: pl.Int64,
            "typology": pl.String,
            "attempt_id": pl.String,
        },
    ).write_parquet(twice)
    with pytest.raises(CanonicalizationError) as excinfo:
        read_typology_artifact(twice)
    assert "two attempt blocks" in str(excinfo.value)


def test_a_missing_join_artifact_is_a_refusal_not_an_all_null_column(tmp_path: Path) -> None:
    with pytest.raises(CanonicalizationError) as excinfo:
        read_typology_artifact(tmp_path / "absent.parquet")
    assert "build_ibm_typologies.py" in str(excinfo.value)


# --- time ------------------------------------------------------------------


def test_the_timestamp_is_parsed_and_not_expanded(tmp_path: Path) -> None:
    """There is no step column, so there is no epoch and no synthetic intra-step jitter."""
    events = clean_events(tmp_path).sort("txn_id")
    assert events["event_ts_utc"].to_list() == list(EXPECTED_EVENT_TS_UTC)
    assert events["event_ts_utc"].dtype == pl.Datetime("us", "UTC")
    assert all(instant.tzinfo is not None for instant in events["event_ts_utc"].to_list())


def test_local_hour_is_the_files_own_wall_clock_and_is_derived_once(tmp_path: Path) -> None:
    """03 C: a rule about human hours reads ``local_hour``. Under the declared assumption
    the local hour is exactly the hour printed in the file, and it can never equal the UTC
    hour, because the offset is three hours."""
    events = clean_events(tmp_path).sort("txn_id")
    assert events["local_hour"].to_list() == list(EXPECTED_LOCAL_HOUR)
    assert events.schema["local_hour"] == pl.Int8
    assert events.schema["event_date_local"] == pl.Date
    assert events["event_date_local"].to_list() == list(EXPECTED_LOCAL_DATE)
    for instant, hour in zip(
        events["event_ts_utc"].to_list(), events["local_hour"].to_list(), strict=True
    ):
        assert hour != instant.hour, "local_hour is the UTC hour under another name"


def test_the_local_and_utc_dates_differ_by_one_day_on_the_early_morning_rows(
    tmp_path: Path,
) -> None:
    """``2022/09/01 00:20`` local is ``2022/08/31 21:20`` UTC: the drift is exactly the
    offset, and a conversion applied twice would show up here as two days."""
    events = clean_events(tmp_path).sort("txn_id")
    first = events.with_row_index("i").filter(pl.col("i") == 0)
    assert first["event_date_local"].to_list()[0] == dt.date(2022, 9, 1)
    assert first["event_ts_utc"].to_list()[0].date() == dt.date(2022, 8, 31)


def test_a_third_timestamp_shape_is_quarantined_and_loses_the_batch(tmp_path: Path) -> None:
    """Both separators are accepted because the two files of one bundle disagree about
    which one to use; a third spelling fails, because silently accepting a format nobody
    looked at is how a join quietly returns zero rows and a run still goes green."""
    dotted = [list(row) for row in FIXTURE_ROWS]
    for row in dotted:
        row[IDX["ts"]] = row[IDX["ts"]].replace("/", ".")
    refused = ingest_file(fixture_csv(tmp_path, dotted))
    assert {record.reason for record in refused.quarantined} == {"timestamp_format"}
    assert refused.events.height == 0


def test_rows_sharing_a_minute_are_ordered_by_txn_id(tmp_path: Path) -> None:
    """Minute precision means many rows share one instant — the whole 5,078,345-row corpus
    spans 17 days — so the total order is decided by the second key, deterministically."""
    events = clean_events(tmp_path)
    keys = list(zip(events["event_ts_utc"].to_list(), events["txn_id"].to_list(), strict=True))
    assert keys == sorted(keys)
    tied = events.filter(pl.col("event_ts_utc") == EXPECTED_EVENT_TS_UTC[0])
    assert tied["txn_id"].to_list() == sorted(tied["txn_id"].to_list())


def test_a_future_instant_is_quarantined_not_shifted(tmp_path: Path) -> None:
    """The guard bites: with the run reference set before the corpus every row is in the
    future, and none of them is silently moved into the past."""
    result = ingest_file(fixture_csv(tmp_path), ingested_at=dt.datetime(2021, 1, 1, tzinfo=dt.UTC))
    assert result.events.height == 0
    assert {record.reason for record in result.quarantined} == {"future_timestamp"}
    assert len(result.quarantined) == ROW_COUNT


# --- structural / boundary -------------------------------------------------


def test_self_transfers_are_kept_and_visible(tmp_path: Path) -> None:
    """The decision that keeps this corpus ingestable.

    591,212 of 5,078,345 rows — 11.642% — are self-transfers, and every Reinvestment row is
    one (481,056 of them). PaySim's contract rejects ``self_transfer`` at the boundary;
    doing that here would quarantine an eighth of the corpus, delete annotated typology rows
    with it, and fail the run on valid data. Plan §7 excludes src == dst at the GRAPH layer,
    where the exclusion belongs, and keeps the count as a feature.
    """
    events = clean_events(tmp_path)
    self_rows = events.filter(pl.col("account_from") == pl.col("account_to"))
    assert self_rows.height == len(SELF_ROWS)
    assert sorted(self_rows["channel"].to_list()) == ["Cash", "Reinvestment"]


def test_balances_are_null_because_the_corpus_has_no_balance_columns(tmp_path: Path) -> None:
    """03 A rule 2: an unknown stays null, never a zero. There is no before/after balance
    pair anywhere in this file, so the exposure proxy differs per corpus and exposure is
    reported per corpus rather than averaged into one headline number (DEV-014)."""
    events = clean_events(tmp_path)
    for column in BALANCE_COLUMNS:
        assert events[column].null_count() == events.height, f"{column} must be null, not zero"
        assert events.schema[column] == pl.Int64


def test_channel_and_txn_type_carry_the_payment_format_mapping(tmp_path: Path) -> None:
    events = clean_events(tmp_path).sort("txn_id")
    assert events["channel"].to_list() == list(EXPECTED_CHANNEL)
    assert events["txn_type"].to_list() == list(EXPECTED_TXN_TYPE)
    assert events["source_dataset"].to_list() == [IBMAML_SOURCE_NAME] * ROW_COUNT
    assert TXN_TYPE_BY_PAYMENT_FORMAT["Credit Card"] == "CREDIT_CARD"


def test_bitcoin_is_legitimately_both_a_currency_and_a_payment_format(tmp_path: Path) -> None:
    """Two columns describing two facts; neither implies the other. A reader that assumed
    the currency column explained the rail would mis-file the 146,091 rows whose format is
    Bitcoin, and ignore the ones that merely happen to share a name."""
    events = clean_events(tmp_path).sort("txn_id")
    row = events.with_row_index("i").filter(pl.col("i") == 2)
    assert row["currency"].to_list() == ["BTC"]
    assert row["channel"].to_list() == ["Bitcoin"]
    assert row["txn_type"].to_list() == ["BITCOIN"]


def test_a_rejected_row_does_not_shift_the_ordinal_of_the_rows_after_it(tmp_path: Path) -> None:
    """The ordinal is the position in the TIMESTAMP-FILTERED scan, which is what the typology
    join is keyed on. A row rejected for a currency reason still consumed its place in that
    scan, so the annotated row after it must still land."""
    rows = [list(row) for row in FIXTURE_ROWS]
    rows[1][IDX["payment_ccy"]] = "Dinar"  # rejected, but its minute parsed
    join = artifact_frame(tmp_path, [(2, "ibmaml:HI-Small:0001", "CYCLE")])
    result = ingest_file(fixture_csv(tmp_path, rows), typology=join)
    assert [record.reason for record in result.quarantined] == ["unknown_currency"]
    annotated = result.events.filter(pl.col("label_typology") == "CYCLE")
    assert annotated.height == 1
    assert annotated["amount_minor"].to_list() == [28198300]


def test_a_row_outside_the_timestamp_scan_does_not_consume_an_ordinal(tmp_path: Path) -> None:
    """The other half of the same rule: a line whose minute does not parse was not in the
    builder's scan either, so numbering continues past it rather than leaving a hole."""
    rows = [list(row) for row in FIXTURE_ROWS]
    rows.insert(
        0,
        [
            "not-a-timestamp",
            "010",
            "8000FF0001",
            "010",
            "8000FF0001",
            "1.00",
            "US Dollar",
            "1.00",
            "US Dollar",
            "Cash",
            "0",
        ],
    )
    join = artifact_frame(tmp_path, [(2, "ibmaml:HI-Small:0001", "STACK")])
    result = ingest_file(fixture_csv(tmp_path, rows), typology=join)
    assert [record.reason for record in result.quarantined] == ["timestamp_format"]
    annotated = result.events.filter(pl.col("label_typology") == "STACK")
    assert annotated["amount_minor"].to_list() == [28198300]
    assert annotated["txn_id"].to_list() == [oracle_txn_id(2, FIXTURE_ROWS[2])]


def test_count_in_scan_rows_separates_rows_read_from_rows_numbered() -> None:
    """Rows read, rows in the scan, and rows canonicalised are three numbers, and the run
    report must not conflate them."""
    assert count_in_scan_rows(raw_frame()) == ROW_COUNT
    assert count_in_scan_rows(raw_frame(mutated("ts", "nope"))) == 0
    assert count_in_scan_rows(raw_frame(mutated("ts", "2022/09/01 00:20"))) == ROW_COUNT


def test_a_reordered_header_fails_the_batch_whole(tmp_path: Path) -> None:
    """Column order is part of the contract. Two of the eleven columns are named
    ``Account``, so a file that swapped positions 3 and 5 would parse perfectly and make
    every transaction a transfer to itself — which is why the header line is asserted before
    any row is read rather than resolved by name afterwards."""
    reordered = ",".join(
        ("Timestamp", "From Bank", "Account", "Account", "To Bank") + IBMAML_RAW_HEADER[5:]
    )
    result = ingest_file(fixture_csv(tmp_path, header=reordered))
    assert [record.reason for record in result.quarantined] == ["schema_mismatch"]
    assert "Account" in result.quarantined[0].detail
    assert result.events.height == 0


def test_an_extra_column_in_the_file_is_refused_by_name(tmp_path: Path) -> None:
    result = ingest_file(
        fixture_csv(
            tmp_path,
            header=HEADER + ",SourceNote",
            rows=[[*row, "note"] for row in FIXTURE_ROWS],
        )
    )
    assert [record.reason for record in result.quarantined] == ["schema_mismatch"]
    assert "SourceNote" in str(result.quarantined[0].payload)
    assert result.events.height == 0


def test_a_header_only_source_is_an_error(tmp_path: Path) -> None:
    """config ``allow_empty_batch: false``: a zero-row source shows red rather than looking
    like a clean run that found nothing."""
    result = ingest_file(fixture_csv(tmp_path, []))
    assert result.rows_read == 0
    assert [record.reason for record in result.quarantined] == ["empty_batch"]
    with pytest.raises(CanonicalizationError):
        result.assert_no_quarantine()


def test_an_empty_batch_handed_to_the_batch_function_raises(tmp_path: Path) -> None:
    assert read_raw_batches(fixture_csv(tmp_path, [])) == []
    with pytest.raises(CanonicalizationError):
        canonicalize_batch(
            raw_frame([]),
            make_identity(),
            run_id=RUN_ID,
            policy=make_policy(),
            deployment_tz=KAMPALA,
            typology=artifact_frame(tmp_path, []),
        )


def test_run_id_must_be_a_ulid(tmp_path: Path) -> None:
    """DEV-003: run ids are ordered lexicographically so a stream can resume."""
    assert len(RUN_ID) == 26
    assert validate_run_id(RUN_ID) == RUN_ID
    with pytest.raises(CanonicalizationError):
        validate_run_id("run-42")
    with pytest.raises(CanonicalizationError):
        ingest_file(fixture_csv(tmp_path), run_id="run-42")


def test_read_raw_batches_forces_every_column_to_text(tmp_path: Path) -> None:
    """Inference would make the money columns Float64 and strip the leading zeros from the
    bank codes before the checks that exist to notice could run."""
    frames = read_raw_batches(fixture_csv(tmp_path), batch_rows=2)
    assert sum(frame.height for frame in frames) == ROW_COUNT
    for frame in frames:
        assert frame.columns == list(IBMAML_POSITIONAL_COLUMNS)
        for name, dtype in frame.schema.items():
            assert dtype == pl.String, f"{name} arrived as {dtype}"


def test_batch_slicing_is_deterministic(tmp_path: Path) -> None:
    path = fixture_csv(tmp_path)
    by_two = [frame.get_column("ts").to_list() for frame in read_raw_batches(path, batch_rows=2)]
    by_five = [frame.get_column("ts").to_list() for frame in read_raw_batches(path, batch_rows=5)]
    assert [ts for chunk in by_two for ts in chunk] == [ts for chunk in by_five for ts in chunk]
    assert [len(chunk) for chunk in by_two] == [2, 2, 2]


def test_the_emitted_frame_does_not_depend_on_the_batch_size(tmp_path: Path) -> None:
    """Re-slicing the same bytes at another ``batch_rows`` must not move a row.

    ``txn_id`` is built from the file-relative ordinal, and the concatenated batches are
    re-sorted on the total order before the frame is returned, so the aggregate is the same
    frame at two, three or a hundred batches. ``batch_id`` is the one column allowed to
    differ, because it is derived from a batch's own content (DEV-012) — and a batch id that
    did not change with the batch would be the actual bug.
    """
    path = fixture_csv(tmp_path)
    order = ["event_ts_utc", "txn_id"]
    columns = [name for name in CANONICAL_P1B_COLUMNS if name != "batch_id"]
    one_batch = ingest_file(path, batch_rows=100)
    three_batches = ingest_file(path, batch_rows=2)
    assert one_batch.events.height == three_batches.events.height == ROW_COUNT
    assert (
        one_batch.events.sort(order)
        .select(columns)
        .equals(three_batches.events.sort(order).select(columns))
    )
    assert one_batch.events["batch_id"].n_unique() == 1
    assert three_batches.events["batch_id"].n_unique() == 3
    # The aggregate is globally ordered, not merely batch-by-batch ordered: a per-batch
    # sort concatenated in file order is the shape that lets two stages disagree about the
    # same corpus, which is what the total order exists to prevent.
    for events in (one_batch.events, three_batches.events):
        assert events.select(order).equals(events.select(order).sort(order))
    # The returned frame is globally ordered, not merely batch-by-batch ordered.
    for events in (one_batch.events, three_batches.events):
        assert events.select(order).equals(events.select(order).sort(order))


def test_limit_bounds_the_rows_read(tmp_path: Path) -> None:
    result = ingest_file(fixture_csv(tmp_path), limit=2)
    assert result.rows_read == 2
    assert result.events.height == 2


def test_the_mask_and_the_contract_declare_the_same_rules() -> None:
    """Every frame-level check has a row-level mirror, and the mask adds only the two
    structural reasons it must own alone. A check added to the contract without a mirror
    would quarantine whole batches instead of rows, and this is what notices."""
    from oxbow.ingest.ibm_aml import _row_reason

    assert _row_reason(raw_frame()).to_list() == [None] * ROW_COUNT
    codes = set(schema_error_codes())
    assert codes <= set(MASK_CODES), codes - set(MASK_CODES)
    assert set(MASK_CODES) - codes == {"null_in_required_column", "amount_overflow"}


def test_the_contract_is_the_authority_over_the_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the two ever disagree, the batch is refused loudly rather than shipped.

    The mask is an optimisation of the contract, not a replacement for it: patching the mask
    to pass everything through proves the schema still fires.
    """
    rows = [list(row) for row in FIXTURE_ROWS]
    rows[0][IDX["payment_ccy"]] = "Dinar"
    monkeypatch.setattr(
        "oxbow.ingest.ibm_aml._row_reason",
        lambda frame: pl.Series("_reason", [None] * frame.height, dtype=pl.String),
    )
    with pytest.raises(CanonicalizationError) as excinfo:
        canonicalize(raw_frame(rows))
    assert "disagree" in str(excinfo.value)
    assert "unknown_currency" in str(excinfo.value)


# --- the port --------------------------------------------------------------


def make_adapter(
    tmp_path: Path,
    *,
    rows: list[list[str]] | tuple[tuple[str, ...], ...] | None = None,
    header: str = HEADER,
    trailing_newline: bool = True,
    extra_blank_line_at_eof: bool = False,
    **adapter_kwargs: Any,
) -> IBMAMLAdapter:
    path = fixture_csv(
        tmp_path,
        rows,
        header=header,
        trailing_newline=trailing_newline,
        extra_blank_line_at_eof=extra_blank_line_at_eof,
    )
    kwargs: dict[str, Any] = {
        "run_id": RUN_ID,
        "deployment_tz": KAMPALA,
        "policy": make_policy(),
        "typology": EMPTY_JOIN,
        "ingested_at": INGESTED_AT,
    }
    kwargs.update(adapter_kwargs)
    return IBMAMLAdapter(path, make_identity(), **kwargs)


def test_adapter_satisfies_the_source_adapter_protocol(tmp_path: Path) -> None:
    """02 H: every implementation of a port is behaviourally interchangeable, so conformance
    is asserted against the protocol that actually ships in ports/source.py."""
    adapter = make_adapter(tmp_path)
    assert isinstance(adapter, SourceAdapter)
    assert adapter.source_id == "ibmaml"
    for member in ("read_manifest", "iter_canonical", "quarantine", "quarantine_count"):
        assert callable(getattr(adapter, member))
    assert adapter.quarantine_count() == 0
    assert adapter.canonical_columns() == CANONICAL_P1B_COLUMNS


def test_iter_canonical_yields_full_contract_rows_in_the_total_order(tmp_path: Path) -> None:
    """Every row carries all 21 canonical columns in the declared order, sorted by
    (event_ts_utc, txn_id) at the boundary so no downstream stage can disagree about it."""
    rows = list(make_adapter(tmp_path).iter_canonical())
    assert len(rows) == ROW_COUNT
    assert tuple(rows[0]) == CANONICAL_P1B_COLUMNS
    keys = [(row["event_ts_utc"], row["txn_id"]) for row in rows]
    assert keys == sorted(keys)


def test_boundary_money_guard_bites_on_an_injected_float(tmp_path: Path) -> None:
    """A guard that never fails is decoration.

    ``assert_canonical_row`` from the port is deliberately NOT wired in here: it still
    validates the pre-P1b field names (``ts_utc``, ``src_account``, ``label_fraud``) while
    the contract this adapter writes uses ``event_ts_utc``, ``account_from`` and
    ``label_is_fraud``. ports/source.py is not owned by this phase, so the disagreement is
    reported rather than papered over.
    """
    row = dict(
        zip(
            CANONICAL_P1B_COLUMNS,
            [f"v{index}" for index in range(len(CANONICAL_P1B_COLUMNS))],
            strict=True,
        )
    )
    row["amount_minor"] = 12.34
    with pytest.raises(TypeError):
        assert_row_satisfies_boundary_guards(row)

    clean = dict(next(iter(make_adapter(tmp_path).iter_canonical())))
    assert_row_satisfies_boundary_guards(clean)

    del clean["label_typology"]
    with pytest.raises(CanonicalizationError):
        assert_row_satisfies_boundary_guards(clean)


def test_read_manifest_hashes_the_bytes_and_reports_a_real_window(tmp_path: Path) -> None:
    path = fixture_csv(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = make_adapter(tmp_path).read_manifest()
    assert manifest.sha256 == digest
    assert manifest.row_count == ROW_COUNT
    assert manifest.source_system == "ibmaml"
    assert manifest.batch_id == TEST_BATCH_ID
    assert manifest.window_start == EXPECTED_EVENT_TS_UTC[0]
    assert manifest.window_end == EXPECTED_EVENT_TS_UTC[2]
    manifest.validate()


def test_read_manifest_rejects_bytes_that_disagree_with_the_pin(tmp_path: Path) -> None:
    """Fail closed on a wrong hash: wrong bytes make every downstream number a lie that
    still looks plausible (01 B, 03 B)."""
    with pytest.raises(BatchIntegrityError) as excinfo:
        make_adapter(tmp_path, expected_sha256="0" * 64).read_manifest()
    assert "disagrees" in str(excinfo.value)


def test_read_manifest_accepts_a_file_with_no_trailing_newline(tmp_path: Path) -> None:
    """A complete file without a final newline is not a truncated file, and a guard that
    cries wolf is how the real failures get ignored."""
    assert make_adapter(tmp_path, trailing_newline=False).read_manifest().row_count == ROW_COUNT


def test_read_manifest_refuses_a_row_count_that_disagrees_with_the_bytes(tmp_path: Path) -> None:
    """A parsed row count below the file's own line count means a field carried an embedded
    newline, and this corpus has no free-text field that legitimately could."""
    rows = [list(row) for row in FIXTURE_ROWS]
    rows[1][IDX["from_account"]] = '"8000\nF4580"'
    text = "\n".join([HEADER, *[",".join(row) for row in rows]]) + "\n"
    path = tmp_path / "HI-Small_Trans.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    adapter = IBMAMLAdapter(
        path,
        make_identity(),
        run_id=RUN_ID,
        deployment_tz=KAMPALA,
        policy=make_policy(),
        typology=EMPTY_JOIN,
        ingested_at=INGESTED_AT,
    )
    assert adapter.ingest().rows_read > 0, "the reader must have taken the row in"
    with pytest.raises(BatchIntegrityError) as excinfo:
        adapter.read_manifest()
    assert "byte-level line count" in str(excinfo.value)


def test_a_blank_line_at_eof_is_quarantined_as_a_hole_in_a_required_column(tmp_path: Path) -> None:
    """The junk row is named, not silently dropped, and the count stays honest.

    Polars reads a blank line as an all-null row. The mask reports it as
    ``null_in_required_column`` — pandera's own nullability failure carries no check code —
    and the run is still red because a quarantine exists.
    """
    adapter = make_adapter(tmp_path, extra_blank_line_at_eof=True)
    manifest = adapter.read_manifest()
    assert manifest.row_count == ROW_COUNT + 1
    assert adapter.quarantine_count() == 1
    record = adapter.quarantine_records[0]
    assert record.reason == "null_in_required_column"
    assert record.row_index == ROW_COUNT, "the physical line, not the filtered ordinal"
    with pytest.raises(CanonicalizationError):
        adapter.ingest().assert_no_quarantine()


def test_unpinned_sentinel_is_not_reported_as_a_mismatch(tmp_path: Path) -> None:
    """``RECORDED_AT_DOWNLOAD`` means "not pinned yet", not "wrong bytes". Reporting it as
    MISMATCH would train everyone to ignore the one error that matters."""
    manifest = make_adapter(tmp_path, expected_sha256=PLACEHOLDER).read_manifest()
    assert len(manifest.sha256) == 64


def test_external_quarantine_is_counted_with_the_rest(tmp_path: Path) -> None:
    """quarantine_count is what the UI shows; records raised outside the ingest loop have to
    appear in it or the count lies."""
    adapter = make_adapter(tmp_path)
    assert list(adapter.iter_canonical())
    assert adapter.quarantine_count() == 0
    adapter.quarantine(
        PortQuarantineRecord(
            batch_id=TEST_BATCH_ID,
            source_dataset=IBMAML_SOURCE_NAME,
            original_row={"from_bank": "010"},
            failing_constraint="amount_format",
            detail="raised outside the loop",
        )
    )
    assert adapter.quarantine_count() == 1


def test_the_ingest_is_deterministic_across_two_runs(tmp_path: Path) -> None:
    """Seed-independent and wall-clock-free: same bytes, same identity, same output frame.
    This is what ``make verify-determinism`` compares, so any per-run random value inside
    the pipeline would show up here."""
    path = fixture_csv(tmp_path)
    first = ingest_file(path)
    second = ingest_file(path)
    assert first.events.equals(second.events)
    pooled = ingest_ibm_aml(
        path,
        make_identity(),
        run_id=RUN_ID,
        deployment_tz=KAMPALA,
        policy=make_policy(),
        typology=EMPTY_JOIN,
        ingested_at=INGESTED_AT,
        use_process_pool=True,
    )
    assert pooled.events.equals(first.events)


# --- canonical projection --------------------------------------------------


def test_canonical_projection_is_the_centralised_list_and_not_a_fork() -> None:
    """The adapter binds to canonical v1's declaration instead of carrying its own."""
    assert CANONICAL_P1B_COLUMNS is CANONICAL_COLUMNS
    assert CANONICAL_COLUMNS == (
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


def test_canonical_v1_keeps_the_self_transfers_this_corpus_is_full_of(
    tmp_path: Path,
) -> None:
    """The whole frame validates, self-edges included, and the sink is what proves it.

    Both cross-phase conflicts this test originally pinned are now closed. The four
    balance columns are nullable because this corpus has no ledger at all (DEV-017),
    and ``_no_self_edge`` is gone: DEV-013 measured 591,212 self-transfers here,
    11.6% of the corpus, and plan 01 P3 asks for them to be kept and excluded from
    cycle and fan detection rather than refused at the boundary (DEV-018). Refusing
    them was not a conservative choice, it was a lossy one.

    The load-bearing half is that this is checked by the write path, not by this
    test's optimism: ``adapters/file/canonical_sink.py`` calls
    ``assert_canonical_frame(persisted=True)`` on every batch, so an artifact on disk
    cannot exist unless it validates. What is asserted here is the shape the graph
    layer will receive -- self-edges present, and countable so it can report how many
    it set aside.
    """
    from oxbow.contracts.canonical_v1 import assert_canonical_frame

    events = clean_events(tmp_path)
    self_edges = events.filter(pl.col("account_from") == pl.col("account_to"))
    assert self_edges.height == len(SELF_ROWS), (
        "the fixture no longer carries the self-transfers this test is about, so it "
        "would pass by proving nothing"
    )
    validated = assert_canonical_frame(events, persisted=False)
    assert validated.height == ROW_COUNT
    assert validated.filter(pl.col("account_from") == pl.col("account_to")).height == len(SELF_ROWS)


def test_every_currency_name_the_contract_maps_is_unique_and_three_letter() -> None:
    """The name table is shared with the measurement script; its output must be a valid ISO
    code, and the two spellings of one currency must land on one code."""
    assert len(CURRENCY_ISO) == 23
    assert all(len(code) == 3 for code in CURRENCY_ISO.values())
    assert CURRENCY_ISO["Brazil Real"] == CURRENCY_ISO["Brazilian Real"] == "BRL"
    assert CURRENCY_ISO["UK Pound"] == CURRENCY_ISO["British Pound"] == "GBP"
    assert CURRENCY_ISO["Rupee"] == CURRENCY_ISO["Indian Rupee"] == "INR"
    assert CURRENCY_ISO["Bitcoin"] == "BTC"
    assert set(CURRENCY_NAMES_MEASURED_HI_SMALL) <= set(CURRENCY_ISO)
    assert len(CURRENCY_NAMES_MEASURED_HI_SMALL) == 15


def test_the_currency_table_still_matches_the_measurement_script() -> None:
    """The adapter and ``scripts/measure_ibm_cycles.py`` must not disagree about a currency,
    because they are meant to be counting the same rows."""
    script = _load_script("ibm_measure_fixture", MEASURE_CURRENCY_SCRIPT)
    assert script.CURRENCY_ISO == CURRENCY_ISO


def test_the_ordinal_definition_still_matches_the_typology_builder() -> None:
    """The row ordinal is defined by the builder's timestamp-filtered scan. If its pattern
    changes and this one does not, every typology lands on the wrong row, quietly, and every
    test above still passes."""
    script = _load_script("ibm_build_fixture", BUILD_TYPOLOGIES_SCRIPT)
    assert script.TRANS_CSV.name == "HI-Small_Trans.csv"
    assert script.TS_PATTERN == IBMAML_ORDINAL_FILTER_PATTERN
    assert tuple(script.COLUMN_NAMES) == tuple(IBMAML_POSITIONAL_COLUMNS)


def _load_script(module_name: str, path: Path) -> ModuleType:
    """Import one of the repository's measurement scripts by path.

    Both are guarded by ``if __name__ == "__main__"``, so importing them reads their
    constants without running a corpus scan.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_the_fabricated_declarations_are_gone_not_commented_out() -> None:
    """Deleting a fiction should be verifiable. These names are what the fourteen-column
    contract and its typology tie-break hung on; if any returns, the schema it described
    never existed in this corpus."""
    import oxbow.contracts.raw_ibm_aml as contract
    import oxbow.ingest.ibm_aml as adapter

    for name in (
        "IBMAML_RAW_COLUMNS",
        "IBMAML_LABEL_COLUMNS",
        "IBMAML_TYPE_LABEL_COLUMNS",
        "IBMAML_TYPOLOGY_PRIORITY",
        "IBMAML_TYPE_CODE_NAMES",
        "IBMAML_UNLABELED",
        "IBMAML_PATTERNS_COLUMNS",
        "raw_ibm_aml_patterns_schema",
        "IBMAML_2023_TRANSACTIONS_COLUMNS",
    ):
        assert not hasattr(contract, name), name
    for name in (
        "resolve_primary_typology",
        "step_expansion_from_config",
        "IBMAMLStepExpansion",
        "step_to_instant",
        "TYPOLOGY_LABEL_TO_SOURCE_COLUMN",
        "_UNLABELED_SOURCE_COLUMN",
        "IBMAML_CURRENCY",
        "MICROSECONDS_PER_HOUR",
        "DEFAULT_MAX_TXN_SPAN_DAYS",
    ):
        assert not hasattr(adapter, name), name


# --- config and the source declaration ------------------------------------


def test_ibmaml_config_declares_the_assumption_and_no_epoch() -> None:
    """The block the adapter reads, checked rather than assumed (00 G).

    ``epoch_utc``, ``step_hours``, ``intra_step_offset`` and ``max_txn_span_days`` were the
    previous block's keys and belong to a corpus with day counters. This one has minutes, so
    what has to be declared instead is the timezone assumption the minutes are read in, the
    format they arrive in, and the artifact the typologies join from.
    """
    loaded = yaml.safe_load(PIPELINE_YAML.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    block = loaded["ibmaml"]
    assert block == CONFIG_BLOCK, "the fixture's ibmaml block drifted from config/pipeline.yaml"
    for stale in ("epoch_utc", "step_hours", "intra_step_offset", "max_txn_span_days"):
        assert stale not in block
    assert block["source_timezone_assumption"] == loaded["deployment_timezone"]
    assert loaded["seed"] == 1337
    policy = policy_from_config(block, repo_root=REPO_ROOT, deployment_timezone="Africa/Kampala")
    assert policy.source_timezone_name == "Africa/Kampala"
    assert policy.typology_artifact == TYPOLOGY_ARTIFACT


def test_the_source_timezone_is_declared_as_an_assumption_not_a_fact() -> None:
    """The key's own name is the documentation.

    The corpus ships no timezone, so reading its clocks as Kampala local is a decision the
    config states and the report names. A deployment in another zone can keep the source
    reading and change only ``deployment_timezone``, because the two are separate keys and
    the policy dataclass keeps them separate.
    """
    assert "source_timezone_assumption" in CONFIG_BLOCK
    policy = make_policy()
    assert policy.source_timezone_name == "Africa/Kampala"
    assert policy.deployment_timezone == "Africa/Kampala"
    other = policy_from_config(
        {**CONFIG_BLOCK, "source_timezone_assumption": "UTC"},
        repo_root=REPO_ROOT,
        deployment_timezone="Africa/Kampala",
    )
    assert other.source_timezone_name == "UTC"
    assert other.deployment_timezone == "Africa/Kampala"


def test_config_rejects_a_missing_or_unparseable_value() -> None:
    """A missing key is a startup failure, not a default (00 G)."""
    for key in CONFIG_BLOCK:
        missing = {name: value for name, value in CONFIG_BLOCK.items() if name != key}
        with pytest.raises(CanonicalizationError) as excinfo:
            policy_from_config(missing, repo_root=REPO_ROOT, deployment_timezone="Africa/Kampala")
        assert key in str(excinfo.value)
    with pytest.raises(CanonicalizationError):
        policy_from_config(
            {**CONFIG_BLOCK, "source_timezone_assumption": "Mars/Olympus_Mons"},
            repo_root=REPO_ROOT,
            deployment_timezone="Africa/Kampala",
        )
    with pytest.raises(CanonicalizationError):
        policy_from_config(
            {**CONFIG_BLOCK, "timestamp_format": "%Y/%m/%d"},
            repo_root=REPO_ROOT,
            deployment_timezone="Africa/Kampala",
        )


def test_sources_yaml_declares_the_real_files_with_measured_hashes() -> None:
    """The three HI-Small members, with the fabricated ``transactions.csv``/``patterns.csv``
    entries and the ``RECORDED_AT_DOWNLOAD`` sentinels gone."""
    sources = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    ibm = next(item for item in sources["sources"] if item["id"] == IBMAML_SOURCE_NAME)
    declared = {entry["name"]: entry.get("sha256", "") for entry in ibm["files"]}
    assert set(declared) == {
        "HI-Small_Trans.csv",
        "HI-Small_accounts.csv",
        "HI-Small_Patterns.txt",
    }
    assert PLACEHOLDER not in declared.values()
    assert declared["HI-Small_Trans.csv"] == (
        "b19d39f515523373f991b689c07e11e7b0b95c17a2c27a87d91584ae16c5b040"
    )
    assert declared["HI-Small_accounts.csv"] == (
        "786808526e33cfc441212dd6fccda7edfc24172149bed59c6ef59b186836b014"
    )
    assert declared["HI-Small_Patterns.txt"] == (
        "2c546b5ce6009e73851f0139af053cf845f08bf92f3bc82fe1eb937dec2ef39b"
    )
    for pin in declared.values():
        assert len(pin) == 64
        assert all(char in "0123456789abcdef" for char in pin)


def test_ibmaml_is_declared_primary_for_module_b_and_names_its_scope() -> None:
    """DEV-011/DEV-013: this corpus is what the network thesis runs on, and only one of the
    six bundles it ships was acquired. The scope fact lives in the source declaration so an
    operator reading config knows exactly what the numbers cover."""
    sources = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    ibm = next(item for item in sources["sources"] if item["id"] == IBMAML_SOURCE_NAME)
    assert ibm["role"] == "primary"
    assert ibm["module"] == "network typology detection (Module B)"
    text = SOURCES_YAML.read_text(encoding="utf-8")
    scope = text[text.index("id: ibmaml") : text.index("- id: elliptic")]
    for word in ("HI", "LI", "Small", "Medium", "Large"):
        assert word in scope, f"the acquisition scope is missing {word!r}"
    assert (
        "only hi-small is acquired" in scope.lower()
    ), "the declaration must say that five of the six bundles were never fetched"


@pytest.mark.skipif(not TRANSACTION_CSV.is_file(), reason="corpus not on this host")
def test_the_pinned_hash_still_matches_the_bytes_on_disk() -> None:
    """The strongest check in the file: 475 MB digested rather than trusted. Wrong bytes
    would make every count and base rate quoted anywhere a lie that still looked
    plausible."""
    declared = {
        entry["name"]: entry["sha256"]
        for entry in next(
            item
            for item in yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))["sources"]
            if item["id"] == IBMAML_SOURCE_NAME
        )["files"]
    }
    digest = hashlib.sha256()
    with TRANSACTION_CSV.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    assert digest.hexdigest() == declared["HI-Small_Trans.csv"]


# --- the real corpus --------------------------------------------------------


@pytest.mark.skipif(not TRANSACTION_CSV.is_file(), reason="corpus not on this host")
def test_a_real_slice_ingests_with_the_measured_counts(tmp_path: Path) -> None:
    """200,000 rows of the real file through the real adapter, against numbers counted over
    those bytes rather than produced by this code.

    The head of ``HI-Small_Trans.csv`` is dominated by a Reinvestment block, so 147,257 of
    the first 200,000 rows are self-transfers while the file as a whole is 591,212 of
    5,078,345; the slice's laundering count is 25 against 5,177 file-wide. Asserting the
    slice's own numbers is the point — a fixture that agreed with the file-wide rate would
    have been written from the card, not from the bytes.
    """
    join = read_typology_artifact(TYPOLOGY_ARTIFACT)
    result = ingest_file(TRANSACTION_CSV, limit=200_000, typology=join, batch_rows=50_000)
    events = result.events
    assert result.rows_read == 200_000
    assert result.quarantine_count == 0, [record.reason for record in result.quarantined][:5]
    assert events.height == 200_000
    assert events["txn_id"].n_unique() == events.height
    assert int((events["account_from"] == events["account_to"]).sum()) == 147_257
    assert int(events["label_is_fraud"].sum()) == 25
    assert events["currency"].n_unique() == 15
    assert sorted(events["currency"].unique().to_list()) == [
        "AUD",
        "BRL",
        "BTC",
        "CAD",
        "CHF",
        "CNY",
        "EUR",
        "GBP",
        "ILS",
        "INR",
        "JPY",
        "MXN",
        "RUB",
        "SAR",
        "USD",
    ]
    assert sorted(events["channel"].unique().to_list()) == [
        "ACH",
        "Bitcoin",
        "Cash",
        "Cheque",
        "Credit Card",
        "Reinvestment",
        "Wire",
    ]
    # 16 of the artifact's 3,209 annotated rows have ordinals inside the first 200,000, all
    # of them FAN-OUT rows of the first attempt block, and every one carries the laundering
    # flag the invariant demands. The other 9 positives in the slice are real laundering
    # rows outside any annotated block, and they stay null rather than acquiring a type.
    annotated = events.filter(events["label_typology"].is_not_null())
    assert annotated.height == 16
    assert set(annotated["label_typology"].to_list()) == {"FAN-OUT"}
    assert annotated["label_is_fraud"].to_list() == [1] * 16
    assert int((events["label_is_fraud"] == 1).sum()) - annotated.height == 9


@pytest.mark.slow
@pytest.mark.skipif(not TRANSACTION_CSV.is_file(), reason="corpus not on this host")
def test_the_full_corpus_card_and_the_alignment_invariant() -> None:
    """The counts this phase quotes, recomputed from the bytes whenever the slow tier runs.

    5,078,345 rows, 5,177 positives (0.1019%), 591,212 self-transfers (11.642%), and all
    3,209 of the join artifact's annotated ordinals carrying ``Is Laundering == 1``. That
    last clause is what the typology column rests on: if the scan order drifted from the
    builder's, the typologies would still populate and still look right.
    """
    frame = pl.read_csv(
        TRANSACTION_CSV,
        has_header=False,
        skip_rows=1,
        schema=dict.fromkeys(IBMAML_POSITIONAL_COLUMNS, pl.String()),
        infer_schema_length=0,
        columns=[0, 2, 4, 10],
    )
    assert frame.height == 5_078_345
    positives = int((frame.get_column("is_laundering") == "1").sum())
    assert positives == 5_177
    assert round(100 * positives / frame.height, 4) == 0.1019
    self_loops = int((frame.get_column("from_account") == frame.get_column("to_account")).sum())
    assert self_loops == 591_212
    assert round(100 * self_loops / frame.height, 3) == 11.642

    in_scan = frame.get_column("ts").str.contains(IBMAML_ORDINAL_FILTER_PATTERN, literal=False)
    assert int(in_scan.sum()) == frame.height, "every row must fall inside the scan"
    keyed = frame.with_columns(
        pl.when(in_scan)
        .then(in_scan.cast(pl.Int64).cum_sum() - 1)
        .otherwise(None)
        .cast(pl.Int64)
        .alias(ORDINAL_COLUMN)
    )
    join = read_typology_artifact(TYPOLOGY_ARTIFACT)
    assert join.height == 3_209
    joined = keyed.join(join, on=ORDINAL_COLUMN, how="inner")
    assert joined.height == 3_209
    assert joined.filter(joined.get_column("is_laundering") != "1").height == 0
    assert set(joined.get_column("label_typology").unique().to_list()) == set(
        IBMAML_TYPOLOGY_VALUES
    )
