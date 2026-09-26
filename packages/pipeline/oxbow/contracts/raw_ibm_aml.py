"""Pandera schema for raw IBM-AML rows, as the bytes actually are.

WHY THIS FILE IS LOAD-BEARING. DEV-011 promoted IBM-AML to *primary* for Module B:
PaySim measured a median account degree of 1.0 and zero surviving time-respecting
cycles, so the 12 typology rules, every graph feature and the whole
network-detection thesis run on this corpus. A contract that quietly accepted a
shifted IBM file would not corrupt one module, it would corrupt the thesis.

MEASURED AGAINST REAL BYTES, 2026-09-26. ``data/raw/ibmaml/HI-Small_Trans.csv``:
475,664,283 bytes, 5,078,345 data rows, SHA-256
``b19d39f515523373f991b689c07e11e7b0b95c17a2c27a87d91584ae16c5b040``, header read
off the bytes with ``od -c`` and reproduced verbatim in
:data:`IBMAML_RAW_HEADER`. Everything this module declares is either in that
header or counted over those rows; nothing here is recalled from the corpus's
published description.

THE PREVIOUS REVISION OF THIS FILE WAS FABRICATED, AND WHAT IT INVENTED IS WORTH
NAMING, because the invented columns are the reason the adapter could not run. It
declared ``stepFrom, stepTo, Type, Category, Amount, nameOrig, balanceOrig,
nameDest, balanceDest, isLaundering, isFlood, isDateSpam, isForcedCashout,
unlabeled`` — fourteen columns belonging to a *different* IBM artefact (the DTL-era
``transactions.csv``). In THIS corpus none of them exist:

* no ``step``/``stepFrom``/``stepTo``. Timestamps are real wall-clock minutes
  (``YYYY/MM/DD HH:MM``), so there is no day counter to expand and no epoch to
  choose. See ``ibmaml.source_timezone_assumption`` in ``config/pipeline.yaml``.
* no ``balanceOrig``/``balanceDest``. The four canonical balance columns are null
  for this corpus, so the exposure proxy differs per corpus (DEV-014).
* no ``isFlood``/``isDateSpam``/``isForcedCashout``/``unlabeled``, and therefore no
  typology tie-break to resolve: a priority order over columns that do not exist is
  decoration. Typology membership comes from ``HI-Small_Patterns.txt`` through the
  join artifact described in :data:`IBMAML_TYPOLOGY_VALUES`.
* no integer ``Type`` code, so there is no code-to-name table. The corpus's own
  channel column (``Payment Format``) is text and feeds ``txn_type``/``channel``.

ELEVEN COLUMNS, TWO OF THEM NAMED ``Account`` — sender first, receiver second. That
is the file, not a transcription error, and it is why this module reads
POSITIONALLY: :func:`polars.read_csv` renames the duplicate to
``Account_duplicated_0``, so a reader that addressed columns by their published
names would either take the same column twice or lose one, and every transaction
would silently become a transfer to itself. The adapter therefore reads the file by
position into :data:`IBMAML_POSITIONAL_COLUMNS` and asserts the verbatim header
separately, so a reordered file is reported as a header mismatch and not as a swap
of sender and receiver.

STRICT ON PURPOSE, mirroring ``raw_paysim``: unknown columns fail the batch and name
themselves, ``coerce=False``, and every check carries a short machine-readable
``error`` code so a quarantine record says *which* rule failed rather than quoting a
stack trace. The pandera-0.20 polars backend workarounds (``_frame``, ``_bool_col``)
are imported from ``raw_paysim`` rather than re-derived, because they were discovered
the hard way there.
"""

from __future__ import annotations

from typing import Final

import pandera.polars as pa
import polars as pl

from oxbow.contracts.raw_paysim import _bool_col, _frame

# --- the header, verbatim -------------------------------------------------

# The eleven names as they appear in the bytes, in file order, INCLUDING the repeated
# ``Account``. This tuple is compared against the file's first line before a single
# row is parsed. It is deliberately not a set and deliberately not unique: deduplicating
# it here is exactly the mistake the positional reading exists to avoid.
IBMAML_RAW_HEADER: Final[tuple[str, ...]] = (
    "Timestamp",
    "From Bank",
    "Account",
    "To Bank",
    "Account",
    "Amount Received",
    "Receiving Currency",
    "Amount Paid",
    "Payment Currency",
    "Payment Format",
    "Is Laundering",
)

# The same eleven columns as a reader must name them. Two accounts, one name, so the
# names come from position. These spellings are the ones
# ``scripts/build_ibm_typologies.py`` already uses for this file, so the scan and the
# typology artifact share one vocabulary instead of two that have to be mapped by hand.
IBMAML_POSITIONAL_COLUMNS: Final[tuple[str, ...]] = (
    "ts",
    "from_bank",
    "from_account",
    "to_bank",
    "to_account",
    "amount_received",
    "receiving_ccy",
    "amount_paid",
    "payment_ccy",
    "payment_format",
    "is_laundering",
)

# The account pair, by position in the header. Named here because "which Account is
# which" is the single question a reader of this corpus can get wrong silently.
IBMAML_SOURCE_ACCOUNT: Final = "from_account"
IBMAML_DEST_ACCOUNT: Final = "to_account"
IBMAML_ACCOUNT_BANK_COLUMNS: Final[tuple[str, ...]] = (
    "from_bank",
    "from_account",
    "to_bank",
    "to_account",
)

# Money columns, both of them. They are read as String, never inferred: some rows
# carry six fractional digits (every Bitcoin row measured), and an inferred Float64
# would put the damage before any check could see it (01 B, DEV-005).
IBMAML_MONEY_COLUMNS: Final[tuple[str, ...]] = ("amount_received", "amount_paid")

# The one money column that becomes ``amount_minor``: what leaves the sender.
# ``amount_received`` has no slot in canonical v1, and the difference between the two
# is a spread or fee — counted and reported, not invented into a column (LIMITATIONS).
IBMAML_AMOUNT_PAID_COLUMN: Final = "amount_paid"

# The only label column the corpus carries.
IBMAML_LABEL_COLUMN: Final = "is_laundering"

# --- currencies ----------------------------------------------------------

# English currency NAMES, as this corpus writes them, to ISO-4217 codes. Reused
# verbatim from ``scripts/measure_ibm_cycles.py::CURRENCY_ISO``, which was built by
# counting the real file: it had to tolerate two spellings for the same currency
# (``Brazilian Real``/``Brazil Real`` and ``British Pound``/``UK Pound``), which is
# why the table is keyed by name rather than normalised first.
# ``tests/unit/test_p1b_ibm_aml.py`` asserts this table and that script's table stay
# identical, so the adapter and the measurement cannot disagree about a currency.
CURRENCY_ISO: Final[dict[str, str]] = {
    "US Dollar": "USD",
    "Euro": "EUR",
    "Swiss Franc": "CHF",
    "Yuan": "CNY",
    "Shekel": "ILS",
    "Mexican Peso": "MXN",
    "Indian Rupee": "INR",
    "Canadian Dollar": "CAD",
    "Congo Franc": "CDF",
    "Malaysian": "MYR",
    "Brazilian Real": "BRL",
    "British Pound": "GBP",
    "Romanian Leu": "RON",
    "Turkish Lira": "TRY",
    "Vietnamese Dong": "VND",
    "Australian Dollar": "AUD",
    "Brazil Real": "BRL",
    "Ruble": "RUB",
    "Rupee": "INR",
    "Saudi Riyal": "SAR",
    "UK Pound": "GBP",
    "Yen": "JPY",
    "Bitcoin": "BTC",
}

# The fifteen of those names that actually occur in HI-Small_Trans.csv, counted over
# all 5,078,345 rows (both currency columns carry the same fifteen). Listed because the
# table above covers the other six names for the sibling bundles, and a reader needs to
# know which half has been measured. Note the spellings this bundle uses: ``Brazil
# Real`` and ``UK Pound`` rather than ``Brazilian Real``/``British Pound``, and
# ``Rupee`` rather than ``Indian Rupee``.
CURRENCY_NAMES_MEASURED_HI_SMALL: Final[tuple[str, ...]] = (
    "US Dollar",
    "Euro",
    "Swiss Franc",
    "Yuan",
    "Shekel",
    "Rupee",
    "UK Pound",
    "Ruble",
    "Yen",
    "Bitcoin",
    "Canadian Dollar",
    "Australian Dollar",
    "Mexican Peso",
    "Saudi Riyal",
    "Brazil Real",
)

# Minor units per currency. This is a property of the BYTES, not of ISO-4217, and the
# difference is not academic: ISO-4217 gives JPY a zero exponent, while every Yen
# amount in this corpus is written to two places (``14918.11``). Following the standard
# here would quarantine every Yen row, so the exponent follows the corpus.
# Measured over all rows: every non-Bitcoin ``amount_paid`` carries exactly two
# fractional digits; every Bitcoin one carries six (max observed), and the accepted
# sub-unit is the satoshi at 1e-8, so eight is declared and anything finer fails.
DEFAULT_MINOR_EXPONENT: Final = 2
MINOR_EXPONENT_BY_ISO: Final[dict[str, int]] = {"BTC": 8}

# Names are mapped through the ISO code rather than restated, so the two tables cannot
# disagree about a currency without this dict being rebuilt.
MINOR_EXPONENT_BY_NAME: Final[dict[str, int]] = {
    name: MINOR_EXPONENT_BY_ISO.get(iso, DEFAULT_MINOR_EXPONENT)
    for name, iso in CURRENCY_ISO.items()
}

# --- channel / type ------------------------------------------------------

# ``Payment Format``, counted over all 5,078,345 rows: Cheque 1,864,331,
# Credit Card 1,323,324, ACH 600,797, Cash 490,891, Reinvestment 481,056,
# Wire 171,855, Bitcoin 146,091. This corpus therefore DOES carry a payment-channel
# dimension, which the fabricated fourteen-column mapping recorded as null.
IBMAML_PAYMENT_FORMATS: Final[tuple[str, ...]] = (
    "ACH",
    "Bitcoin",
    "Cash",
    "Cheque",
    "Credit Card",
    "Reinvestment",
    "Wire",
)

# ONE mapping, stated once. ``channel`` carries the corpus's own ``Payment Format``
# string verbatim — no invented rail taxonomy, so the raw value survives to the
# canonical frame and an operator can read the source spelling off a case packet.
# ``txn_type`` carries the stable upper-case token, which is what rules and feature
# bins match on, because PaySim's ``type`` arrives already upper-cased and single-spaced
# and one rule set has to match both corpora. ``Bitcoin`` is legitimately both a
# currency name and a payment format in this corpus; those are two different columns
# describing two different facts, and neither implies the other.
TXN_TYPE_BY_PAYMENT_FORMAT: Final[dict[str, str]] = {
    "ACH": "ACH",
    "Bitcoin": "BITCOIN",
    "Cash": "CASH",
    "Cheque": "CHEQUE",
    "Credit Card": "CREDIT_CARD",
    "Reinvestment": "REINVESTMENT",
    "Wire": "WIRE",
}

# --- labels and typologies -----------------------------------------------

# The published label caveat, restated for what these bytes actually carry.
IBMAML_LABEL_CAVEAT: Final = (
    "`Is Laundering` is a curated research annotation, not a prosecuted case: 5,177 of "
    "5,078,345 rows (0.1019%) carry it. Typology membership is NOT a label column — it "
    "comes from the annotated attempt blocks in HI-Small_Patterns.txt, which cover 3,209 "
    "of those 5,177 positives, so 1,968 laundering rows carry no typology and are not "
    "evidence of any particular pattern. A row with no typology is unlabeled, not clean."
)

# The eight typologies the join artifact carries, counted over its 3,209 rows in 370
# attempt blocks: GATHER-SCATTER 716, SCATTER-GATHER 626, STACK 466, FAN-OUT 342,
# FAN-IN 318, CYCLE 287, BIPARTITE 263, RANDOM 191. They map onto the rule set directly
# (CYCLE to R4, FAN-IN/FAN-OUT to R2/R3, GATHER-SCATTER/SCATTER-GATHER to R1/R10, STACK
# to R5), and `RANDOM` rows keep their own string because downstream uses them as the
# negative control: a block the corpus generated without a pattern is the honest test of
# whether a rule detects a pattern or merely detects activity.
IBMAML_TYPOLOGY_VALUES: Final[tuple[str, ...]] = (
    "BIPARTITE",
    "CYCLE",
    "FAN-IN",
    "FAN-OUT",
    "GATHER-SCATTER",
    "RANDOM",
    "SCATTER-GATHER",
    "STACK",
)

# What the join artifact must look like. The artifact is built by
# ``scripts/build_ibm_typologies.py`` from HI-Small_Patterns.txt; the adapter joins it
# on the row ordinal, so a shape change here means that script changed underneath.
IBMAML_TYPOLOGY_KEY_COLUMN: Final = "txn_ordinal"
IBMAML_TYPOLOGY_COLUMNS: Final[tuple[str, ...]] = (
    "txn_ordinal",
    "attempt_id",
    "typology",
    "attempt_description",
)

# --- timestamps ----------------------------------------------------------

# The separator the bytes use. Confirmed from the raw hex, not from how a terminal
# renders it: the first data row's characters 4 and 7 are 0x2f, i.e. ``2022/09/01``.
IBMAML_TIMESTAMP_FORMAT: Final = "%Y/%m/%d %H:%M"

# The same shape with the other separator. Both files of one bundle are read: the
# transaction file writes slashes, ``HI-Small_Patterns.txt`` writes dashes, and the row
# ordinal that keys the typology join is defined by which lines parse, so accepting only
# one of the two spellings would silently renumber every row after the first disagreement.
IBMAML_DASHED_TIMESTAMP_FORMAT: Final = "%Y-%m-%d %H:%M"

# Minutes, no seconds, no offset. The offset is absent, which is the whole reason
# ``config/pipeline.yaml`` declares ``source_timezone_assumption`` instead of quietly
# assuming UTC: an unlabelled wall clock converted to UTC is a claim nobody made.
IBMAML_TIMESTAMP_PATTERN: Final = r"^\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}$"

# Both separators are accepted, exactly as ``scripts/build_ibm_typologies.py`` accepts
# them. It has to: the transaction file writes slashes and the pattern file writes
# dashes for the same instants, and the row ordinal this contract defines is taken from
# that script's timestamp-filtered scan. Diverging from its pattern would shift every
# ordinal and silently mis-join every typology.
IBMAML_ORDINAL_FILTER_PATTERN: Final = IBMAML_TIMESTAMP_PATTERN

# --- text shapes ---------------------------------------------------------

# Non-negative plain decimal, any number of places. The place count is a separate
# check (:func:`_amount_within_currency_scale`), because "how many decimals are money"
# is a question about the currency, not about the file.
IBMAML_MONEY_TEXT_PATTERN: Final = r"^\d+(\.\d+)?$"

# Bank codes are numeric WITH leading zeros significant (``010``, ``03208``, lengths
# 3..7 measured over all rows). Read as integer, ``010`` becomes ``10`` and collides
# with a different bank, so the reader keeps them as text and this check keeps them so.
IBMAML_BANK_CODE_PATTERN: Final = r"^[0-9]{1,8}$"

# Account numbers are nine upper-case alphanumeric characters in this bundle (length
# 9 measured over all 5,078,345 rows). Only the character class is asserted: the width
# is a property of one bundle out of six the dataset ships, and pinning it here would
# refuse a valid HI-Medium row for a reason this host has not measured.
IBMAML_ACCOUNT_CODE_PATTERN: Final = r"^[0-9A-Z]+$"

IBMAML_CURRENCY_PATTERN: Final = r"^[^,]+$"


# --- check functions -----------------------------------------------------


def _money_format(data: object) -> pl.LazyFrame:
    """Both amount columns are non-negative plain decimals, checked as text.

    Parsing first would accept ``1e9`` and ``nan``, both of which parse happily as
    floats and neither of which is money. The pattern also rejects a leading minus, so
    a negative amount cannot reach the minor-unit conversion.
    """
    f = _frame(data)
    mask = pl.Series("ok", [True] * f.height, dtype=pl.Boolean)
    for column in IBMAML_MONEY_COLUMNS:
        text = f[column].str.strip_chars()
        mask = mask & text.str.contains(IBMAML_MONEY_TEXT_PATTERN, literal=False)
    return _bool_col(mask.fill_null(False))


def _amount_within_currency_scale(data: object) -> pl.LazyFrame:
    """No more fractional digits than the currency's minor exponent allows.

    This is the check that keeps money exact without rounding it. The corpus writes
    Bitcoin to six places and everything else to two; a two-decimal rule would
    quarantine all 146,066 Bitcoin rows and a "just multiply by 100" rule would silently
    round 0.281983 down to 0.28. Anything finer than the declared exponent is refused
    here rather than quantised, because a rounding decision in a money column is a fact
    the operator is owed, not a detail the adapter gets to bury.

    An unmapped currency name is NOT failed here: :func:`_currency_is_known` owns that
    one, and a row would otherwise be reported twice for one defect.

    Every check in this module builds its mask from ``Series`` operations rather than
    ``Expr``, because ``_bool_col`` wraps a materialised mask and an ``Expr`` reaching
    it raises "passing Expr objects to the DataFrame constructor is not supported" from
    the middle of pandera — the failure the earlier revision of this file had.
    """
    f = _frame(data)
    exponent = f["payment_ccy"].replace_strict(
        MINOR_EXPONENT_BY_NAME, default=None, return_dtype=pl.Int64
    )
    fraction = f[IBMAML_AMOUNT_PAID_COLUMN].str.split(".").list.get(1, null_on_oob=True)
    digits = fraction.str.len_chars().fill_null(0)
    return _bool_col((digits <= exponent).fill_null(True))


def _timestamp_shape(data: object) -> pl.LazyFrame:
    """``YYYY/MM/DD HH:MM`` at the minute, and nothing else.

    The same pattern defines the row ordinal the typology join is keyed on, so a row
    outside it is both a parse failure and an ordinal hazard; the adapter quarantines it
    and does not let it advance the counter.
    """
    f = _frame(data)
    mask = f["ts"].str.strip_chars().str.contains(IBMAML_TIMESTAMP_PATTERN, literal=False)
    return _bool_col(mask.fill_null(False))


def _timestamp_is_a_real_minute(data: object) -> pl.LazyFrame:
    """The shape is not enough: ``00:60`` has the right number of digits and is not a time.

    This check exists because the parse downstream is strict, and a strict ``strptime``
    inside a five-million-row query raises a ComputeError that names no row — which would
    take the batch down as a source failure rather than quarantining the one line. Checked
    on the separator-normalised text with ``strict=False`` so a null here means "a real
    calendar minute did not come out".

    Deliberately separated from :func:`_timestamp_shape` even though both are about the
    same column: the shape is what defines the scan order, and the value is what defines
    the instant. A row can be in the scan and still be unparseable, and the quarantine has
    to say which of the two happened.
    """
    f = _frame(data)
    parsed = (
        f["ts"]
        .str.strip_chars()
        .str.replace_all("/", "-")
        .str.strptime(pl.Datetime("us"), IBMAML_DASHED_TIMESTAMP_FORMAT, strict=False)
    )
    return _bool_col(parsed.is_not_null().fill_null(False))


def _label_is_binary(data: object) -> pl.LazyFrame:
    """``Is Laundering`` is 0 or 1, the corpus's only label.

    A label outside {0, 1} is not a weaker signal, it is a different encoding, and
    accepting it would put an unlearnable target into the training set.
    """
    f = _frame(data)
    return _bool_col(f[IBMAML_LABEL_COLUMN].is_in(["0", "1"]).fill_null(False))


def _currency_is_known(data: object) -> pl.LazyFrame:
    """Both currency columns hold a name this table maps to an ISO code.

    Asserted as membership rather than as "non-empty", unlike the earlier revision of
    this file, because canonical ``currency`` is now an ISO-4217 code and an unmapped
    name has no code to become. Refusing the row is the honest failure: guessing a code
    would put a wrong currency into every amount downstream, and a wrong currency is a
    wrong sum.
    """
    f = _frame(data)
    known = list(CURRENCY_ISO)
    mask = f["receiving_ccy"].is_in(known) & f["payment_ccy"].is_in(known)
    return _bool_col(mask.fill_null(False))


def _currency_is_a_name(data: object) -> pl.LazyFrame:
    """A currency cell is a short name, not a comma-laden fragment.

    Weaker than membership and separated from it on purpose: this one catches a
    mis-parsed row (a field count that shifted the columns by one) before the mapping
    check reports it as an unknown currency, so the reason code says "shape" when the
    problem is shape.
    """
    f = _frame(data)
    mask = pl.Series("ok", [True] * f.height, dtype=pl.Boolean)
    for column in ("receiving_ccy", "payment_ccy"):
        text = f[column].str.strip_chars()
        mask = mask & text.str.contains(IBMAML_CURRENCY_PATTERN, literal=False)
    return _bool_col(mask.fill_null(False))


def _payment_format_is_known(data: object) -> pl.LazyFrame:
    """``Payment Format`` stays inside the seven measured values.

    The column feeds the canonical ``channel`` and ``txn_type``, both of which rules and
    scorecard bins match on by name, so an unseen format would become a category nobody
    has reasoned about. Failing here means the corpus gained a rail and a human decides
    what it means.
    """
    f = _frame(data)
    mask = f["payment_format"].is_in(list(IBMAML_PAYMENT_FORMATS))
    return _bool_col(mask.fill_null(False))


def _account_identity_present(data: object) -> pl.LazyFrame:
    """Bank and account codes are present and in their documented shape.

    An account identity in this corpus is the (bank, account) PAIR: ``HI-Small_accounts
    .csv`` maps Bank ID plus Account Number onto an Entity ID, and four account numbers
    recur under two different banks in this file (measured), so hashing the account
    string alone would merge distinct accounts into one graph node and invent shared
    counterparties, degree and community out of nothing.
    """
    f = _frame(data)
    mask = pl.Series("ok", [True] * f.height, dtype=pl.Boolean)
    for column in ("from_bank", "to_bank"):
        mask = mask & f[column].str.contains(IBMAML_BANK_CODE_PATTERN, literal=False)
    for column in (IBMAML_SOURCE_ACCOUNT, IBMAML_DEST_ACCOUNT):
        mask = mask & f[column].str.contains(IBMAML_ACCOUNT_CODE_PATTERN, literal=False)
    return _bool_col(mask.fill_null(False))


raw_ibm_aml_schema = pa.DataFrameSchema(
    {
        # Everything is String, and that is a measurement rather than a preference.
        # The bank columns need their leading zeros, both money columns exceed two
        # decimals on some rows, and ``Is Laundering`` is compared as text before it is
        # cast, so an inferred Float64 anywhere would damage the row before a check
        # could see it (01 B, DEV-005).
        "ts": pa.Column(pl.String, nullable=False),
        "from_bank": pa.Column(pl.String, nullable=False),
        "from_account": pa.Column(pl.String, nullable=False),
        "to_bank": pa.Column(pl.String, nullable=False),
        "to_account": pa.Column(pl.String, nullable=False),
        "amount_received": pa.Column(pl.String, nullable=False),
        "receiving_ccy": pa.Column(pl.String, nullable=False),
        "amount_paid": pa.Column(pl.String, nullable=False),
        "payment_ccy": pa.Column(pl.String, nullable=False),
        "payment_format": pa.Column(pl.String, nullable=False),
        "is_laundering": pa.Column(pl.String, nullable=False),
    },
    # Frame-level checks: the only level where ``error=`` is accepted in pandera 0.20,
    # matching raw_paysim. Codes are quoted verbatim in the failure message, which is
    # how the adapter's reason mapper recovers the rule name for the quarantine ledger.
    checks=[
        pa.Check(_money_format, error="amount_format"),
        pa.Check(_amount_within_currency_scale, error="amount_scale"),
        pa.Check(_timestamp_shape, error="timestamp_format"),
        pa.Check(_timestamp_is_a_real_minute, error="timestamp_out_of_range"),
        pa.Check(_label_is_binary, error="label_not_binary"),
        pa.Check(_currency_is_a_name, error="currency_shape"),
        pa.Check(_currency_is_known, error="unknown_currency"),
        pa.Check(_payment_format_is_known, error="unknown_payment_format"),
        pa.Check(_account_identity_present, error="account_identity_missing"),
    ],
    # strict=True: an unknown column is schema drift. Silently ignoring it is how a
    # renamed upstream field becomes an all-null feature nobody notices until a demo, and
    # on this corpus that feature would sit inside the Module B thesis (DEV-011).
    strict=True,
    coerce=False,
    name="raw_ibm_aml",
)

__all__ = [
    "CURRENCY_ISO",
    "CURRENCY_NAMES_MEASURED_HI_SMALL",
    "DEFAULT_MINOR_EXPONENT",
    "IBMAML_ACCOUNT_BANK_COLUMNS",
    "IBMAML_ACCOUNT_CODE_PATTERN",
    "IBMAML_AMOUNT_PAID_COLUMN",
    "IBMAML_BANK_CODE_PATTERN",
    "IBMAML_CURRENCY_PATTERN",
    "IBMAML_DASHED_TIMESTAMP_FORMAT",
    "IBMAML_DEST_ACCOUNT",
    "IBMAML_LABEL_CAVEAT",
    "IBMAML_LABEL_COLUMN",
    "IBMAML_MONEY_COLUMNS",
    "IBMAML_MONEY_TEXT_PATTERN",
    "IBMAML_ORDINAL_FILTER_PATTERN",
    "IBMAML_PAYMENT_FORMATS",
    "IBMAML_POSITIONAL_COLUMNS",
    "IBMAML_RAW_HEADER",
    "IBMAML_SOURCE_ACCOUNT",
    "IBMAML_TIMESTAMP_FORMAT",
    "IBMAML_TIMESTAMP_PATTERN",
    "IBMAML_TYPOLOGY_COLUMNS",
    "IBMAML_TYPOLOGY_KEY_COLUMN",
    "IBMAML_TYPOLOGY_VALUES",
    "MINOR_EXPONENT_BY_ISO",
    "MINOR_EXPONENT_BY_NAME",
    "TXN_TYPE_BY_PAYMENT_FORMAT",
    "raw_ibm_aml_schema",
]
