"""Canonical event v1: the one declaration of what a trusted transaction row is.

WHY THIS FILE EXISTS. Three modules each carried their own idea of the canonical
column list. ``ports/source.py`` declared ``CANONICAL_EVENT_FIELDS`` in the plan's
short names (``ts_utc``, ``src_account``, ``label_fraud``); ``ingest/canonical.py``
declared ``CANONICAL_COLUMNS`` in the implemented names (``event_ts_utc``,
``account_from``, ``label_is_fraud``); and the PaySim writer emitted a third,
smaller set. Two names for one contract is how a boundary stops being a boundary:
the port said one thing, the adapter did another, and nothing between them could
tell that they disagreed. Canonical v1 is declared **once, here**, and every other
module imports it.

WHY THE NAMES ARE THE IMPLEMENTED ONES. Plan §6 lists ``ts_utc``/``src_account``/
``label_fraud``, but no frame this repository has ever written contained a column
called ``ts_utc`` — and ``config/pipeline.yaml`` keyed its total order at that
nonexistent column, so the sort key was dead on arrival. Correcting the config to
name ``event_ts_utc`` is the smaller change, keeps every existing reader working,
and makes the declared order actually executable; a config that points at a column
nobody emits is a bug, not a spec. ``local_hour`` and ``event_date_local`` are the
pair 03 C and 01 P3 require: a UTC instant plus the deployment-timezone hour,
carried as two separate columns so no rule can quietly read the wrong one.

MONEY. ``amount_minor`` and the four balance columns are ``pl.Int64`` minor units
(DEV-005, 01 B). No float money appears in this schema, in the frames it accepts,
or in the writers that emit it: ``scripts/no_float_money.py`` audits the
annotations and the dtype checks below audit the data.

TIME. ``event_ts_utc`` and ``ingested_at`` are ``pl.Datetime('us', 'UTC')``.
Microseconds because PaySim's ``step`` is a day counter shared by ~8.5k rows, so
second resolution would leave them mutually indistinguishable and intra-step order
would be arbitrary; UTC because a naive timestamp is a claim about a timezone that
nobody agreed to.

DEV-012. ``ingested_at`` and ``run_id`` belong to the contract and are NOT written
into the Parquet bytes. ``make verify-determinism`` requires two runs to produce
byte-identical artifacts, and a wall-clock reading or a random ULID inside the data
makes that arithmetically impossible; both fields are sidecar-carried in the batch
manifest and the warehouse table instead. ``canonical_v1_schema`` (all 21 columns) and
``canonical_v1_persisted_schema`` (the remaining 19) are therefore both *derived*
from ``CANONICAL_COLUMNS``, never written out twice.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Final

import pandera.polars as pa
import polars as pl

# Reused rather than re-derived: both helpers encode pandera-0.20 polars backend
# behaviour discovered by failure in ``raw_paysim`` (a ``pl.DataFrame`` return raises
# "output type of check_fn not recognized" and a ``pl.Series`` raises earlier on a
# missing ``collect_schema``). ``raw_ibm_aml`` imports them the same way.
from oxbow.contracts.raw_paysim import _bool_col, _frame
from oxbow.dtypes import PolarsDtype

# --- the one column list --------------------------------------------------
# Exact and ordered. The Parquet writer selects in this order, the port declares
# it, and ``test_canonical_columns_match_contract`` fails if the three ever split.
# Adding a column is additive and legal; renaming, reordering or dropping one is a
# breaking change that belongs in canonical_v2 with both live during the migration
# (02 B seam 2).
CANONICAL_COLUMNS: Final[tuple[str, ...]] = (
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

# DEV-012: contract members, sidecar-carried, never in the persisted bytes.
SIDECAR_COLUMNS: Final[tuple[str, ...]] = ("ingested_at", "run_id")

PERSISTED_CANONICAL_COLUMNS: Final[tuple[str, ...]] = tuple(
    column for column in CANONICAL_COLUMNS if column not in SIDECAR_COLUMNS
)

UTC_MICROS: Final[pl.Datetime] = pl.Datetime("us", "UTC")

# Dtypes declared once so the writer, the assertions and the tests all check the
# same table instead of each re-guessing it.
CANONICAL_DTYPES: Final[dict[str, PolarsDtype]] = {
    "txn_id": pl.String(),
    "event_ts_utc": UTC_MICROS,
    "event_date_local": pl.Date(),
    "local_hour": pl.Int8(),
    "txn_type": pl.String(),
    "channel": pl.String(),
    "amount_minor": pl.Int64(),
    "currency": pl.String(),
    "account_from": pl.String(),
    "account_to": pl.String(),
    "src_balance_before_minor": pl.Int64(),
    "src_balance_after_minor": pl.Int64(),
    "dst_balance_before_minor": pl.Int64(),
    "dst_balance_after_minor": pl.Int64(),
    "label_is_fraud": pl.Int8(),
    "label_is_flagged": pl.Int8(),
    "label_typology": pl.String(),
    "source_dataset": pl.String(),
    "ingested_at": UTC_MICROS,
    "run_id": pl.String(),
    "batch_id": pl.String(),
}

# Every money column, named so the float-money audit, the writer and the tests
# check one set rather than three overlapping guesses.
MONEY_COLUMNS: Final[tuple[str, ...]] = (
    "amount_minor",
    "src_balance_before_minor",
    "src_balance_after_minor",
    "dst_balance_before_minor",
    "dst_balance_after_minor",
)

TIMESTAMP_COLUMNS: Final[tuple[str, ...]] = ("event_ts_utc", "ingested_at")

# The total order, declared once. ``_total_order_unique`` below is the check that
# makes it a *total* order, ``ingest/paysim.py`` sorts on it, and the Parquet writer
# re-sorts the concatenated multi-batch frame on it before writing — a per-batch sort
# is not a global one, and bytes that depend on batch order are bytes that differ
# between two runs of the same corpus at a different ``--limit``.
#
# ``config/pipeline.yaml`` names the first key by its logical name (``ts_utc``) and
# ``graph/settings.py`` owns the alias table that resolves it; this constant is the
# resolved form, so ingest and the graph sort on the same pair of columns without
# either importing the other.
CANONICAL_TOTAL_ORDER: Final[tuple[str, str]] = ("event_ts_utc", "txn_id")

# ``label_typology`` is nullable because PaySim has no typology taxonomy at all (its
# labels are isFraud/isFlaggedFraud), and the four balance columns are nullable
# because IBM-AML carries no balance ledger: DEV-013 read its header off real bytes
# and there is no balanceOrig/balanceDest anywhere in the 11 columns. Both nulls are
# "the source does not have this", never "the value is unknown", which is why the
# all-or-nothing rule in :func:`assert_balance_provenance` guards the second case --
# a half-populated balance column is a parse that lost rows, and no schema can tell
# that apart from an absent ledger without the per-source assertion.
BALANCE_COLUMNS: Final[tuple[str, ...]] = (
    "src_balance_before_minor",
    "src_balance_after_minor",
    "dst_balance_before_minor",
    "dst_balance_after_minor",
)

NULLABLE_COLUMNS: Final[tuple[str, ...]] = ("label_typology", *BALANCE_COLUMNS)

# 03 B: the account key is 12 hex characters, displayed uppercased as ACC-XXXXXX.
ACCOUNT_KEY_LENGTH: Final = 12

# Machine-readable codes. A quarantine record carries one of these so a triage run
# groups by cause instead of reading six thousand distinct sentences.
ERR_TXN_ID_NOT_NAMESPACED: Final = "txn_id_not_namespaced"
ERR_DUPLICATE_TOTAL_ORDER: Final = "duplicate_total_order"
ERR_NEGATIVE_MONEY: Final = "negative_money"
ERR_LABEL_NOT_BINARY: Final = "label_not_binary"
ERR_LOCAL_HOUR_RANGE: Final = "local_hour_out_of_range"
ERR_DATE_DRIFT: Final = "event_date_local_drift"
ERR_ACCOUNT_KEY_SHAPE: Final = "account_key_shape"
ERR_UNKNOWN_COLUMN: Final = "unknown_column"
ERR_MISSING_COLUMN: Final = "missing_required_column"
ERR_DTYPE_MISMATCH: Final = "dtype_mismatch"
ERR_NULL_IN_REQUIRED: Final = "null_in_required_column"
ERR_COERCION_REQUIRED: Final = "coercion_required"
ERR_LABEL_PROVENANCE: Final = "label_provenance_missing"
ERR_BALANCE_PROVENANCE: Final = "balance_provenance_ambiguous"


# --- frame-level checks ---------------------------------------------------


def _txn_id_namespaced(data: object) -> pl.LazyFrame:
    """``txn_id`` must begin with its own ``source_dataset`` plus a colon.

    DEV-004/C4 is what lets the two corpora share one table: PaySim row 41 and an
    IBM-AML row with the same ordinal are different transactions, and a bare
    integer id would merge them into one account's history. Checked against
    ``source_dataset`` rather than a literal so a new corpus cannot arrive with an
    id that impersonates an existing one.
    """
    frame = _frame(data)
    prefix = pl.concat_str([pl.col("source_dataset"), pl.lit(":")])
    return _bool_col(frame.select(pl.col("txn_id").str.starts_with(prefix)).to_series())


def _total_order_unique(data: object) -> pl.LazyFrame:
    """No two rows may share ``(event_ts_utc, txn_id)``.

    01 P3 fixes the total order at exactly that tuple and every sort in the
    codebase uses it, so a duplicate pair is not cosmetic: two rows that tie on the
    sort key land in whichever order the engine happened to produce, and a rule
    with a lookback window can then see a different history on the second run. The
    writer sorts on this tuple and this check is what proves the sort is total.
    """
    frame = _frame(data)
    return _bool_col(
        frame.select(~pl.struct(["event_ts_utc", "txn_id"]).is_duplicated()).to_series()
    )


def _money_nonnegative(data: object) -> pl.LazyFrame:
    """Amount and balance columns cannot be negative.

    A negative balance is not a smaller balance: in both corporates the columns are
    stated as unsigned, so a minus sign means the source changed shape. PaySim's
    balance columns are known to be *inconsistent with* ``amount`` and are
    deliberately not reconciled here (that inconsistency is a P2 feature, spec §7.2),
    but sign is not a reconciliation, it is a different encoding.
    """
    frame = _frame(data)
    mask = pl.lit(True)
    for column in MONEY_COLUMNS:
        mask = mask & (pl.col(column) >= 0)
    return _bool_col(frame.select(mask).to_series())


def _labels_are_binary(data: object) -> pl.LazyFrame:
    """Both binary labels must be 0 or 1.

    A label outside {0, 1} is not a weaker signal, it is a different encoding, and
    accepting it would put an unlearnable target into the training set.
    """
    frame = _frame(data)
    return _bool_col(
        frame.select(
            pl.col("label_is_fraud").is_in([0, 1]) & pl.col("label_is_flagged").is_in([0, 1])
        ).to_series()
    )


def _local_hour_in_range(data: object) -> pl.LazyFrame:
    """``local_hour`` is 0..23 in the deployment timezone, never anything else.

    03 C: rules that reason about human hours read this column and never
    ``event_ts_utc``. An hour of 24 or -1 means the derivation ran twice or ran
    against the wrong zone, which is exactly how an ODD_HOUR_SHIFT rule ends up
    flagging every East African morning as suspicious.
    """
    frame = _frame(data)
    return _bool_col(
        frame.select((pl.col("local_hour") >= 0) & (pl.col("local_hour") < 24)).to_series()
    )


def _event_date_local_bounded(data: object) -> pl.LazyFrame:
    """``event_date_local`` may differ from the UTC date by at most one day.

    Africa/Kampala is UTC+3 with no DST, so a UTC evening becomes a local date one
    day *ahead* and a UTC early morning is one day *behind* nothing: the offset can
    move the calendar date by at most 1. Bounding the drift rather than comparing
    against a computed local date keeps the schema timezone-agnostic — the contract
    must not hardcode the deployment zone, since a different deployment is a config
    change, not a schema change. A drift of 2 or more means the conversion was
    applied twice.
    """
    frame = _frame(data)
    utc_date = pl.col("event_ts_utc").dt.date()
    mask = (pl.col("event_date_local") >= (utc_date - pl.duration(days=1))) & (
        pl.col("event_date_local") <= (utc_date + pl.duration(days=1))
    )
    return _bool_col(frame.select(mask.fill_null(False)).to_series())


def _account_keys_wellformed(data: object) -> pl.LazyFrame:
    """Both endpoint keys are 12 lowercase hex characters.

    03 B fixes the width at 48 bits; ``config/sources.yaml`` fixes the stored form
    at the digest's own lowercase rendering and the *display* form at ``ACC-`` plus
    upper text. The distinction is not cosmetic: uppercasing before hashing or
    before storing would create a second identity for the same account, and every
    join between the Parquet and the warehouse would have to know which one it met.

    Shape is checked rather than trusted because a key that leaked out of the
    hasher — an unsalted digest, a truncated account name — still joins, still
    groups, and silently re-identifies the account it was meant to hide.
    """
    frame = _frame(data)
    pattern = f"^[0-9a-f]{{{ACCOUNT_KEY_LENGTH}}}$"
    return _bool_col(
        frame.select(
            pl.col("account_from").str.contains(pattern, literal=False)
            & pl.col("account_to").str.contains(pattern, literal=False)
        ).to_series()
    )


_CHECKS: Final[list[pa.Check]] = [
    # There is deliberately no check that ``account_from != account_to``. A
    # self-referential transfer is a real event, and 01 P3 asks for exactly the
    # treatment the money rules give reversals: kept in the record, excluded from
    # cycle and fan detection. Refusing it here was not conservative, it was lossy —
    # DEV-013 measured 591,212 self-transfers in IBM-AML's HI-Small bundle, 11.6% of
    # that corpus, which is the reinvestment traffic a portfolio's own turnover
    # profile is made of. The exclusion therefore lives in ``oxbow.graph``, where the
    # traversal decides what a counterparty is, and that layer counts what it drops.
    pa.Check(_txn_id_namespaced, error=ERR_TXN_ID_NOT_NAMESPACED),
    pa.Check(_total_order_unique, error=ERR_DUPLICATE_TOTAL_ORDER),
    pa.Check(_money_nonnegative, error=ERR_NEGATIVE_MONEY),
    pa.Check(_labels_are_binary, error=ERR_LABEL_NOT_BINARY),
    pa.Check(_local_hour_in_range, error=ERR_LOCAL_HOUR_RANGE),
    pa.Check(_event_date_local_bounded, error=ERR_DATE_DRIFT),
    pa.Check(_account_keys_wellformed, error=ERR_ACCOUNT_KEY_SHAPE),
]


def _columns(schema_columns: Sequence[str]) -> dict[str, pa.Column]:
    """Build the pandera column map for a subset of the canonical list.

    One builder for both schemas so the persisted variant cannot drift from the
    full one: they are the same declaration filtered, never a second hand-written
    list.
    """
    return {
        name: pa.Column(
            # pandera 0.20's ``Column`` stub is annotated ``str | type |
            # DataTypeClass`` and so rejects a ``DataType`` *instance*, which is what
            # ``backends/polars/components.py`` actually compares against the frame
            # schema at runtime. Instantiated dtypes are used throughout because a
            # naive ``pl.Datetime`` must not pass for ``Datetime(us, UTC)``; the
            # ignore is confined to this argument rather than loosening the
            # declaration of CANONICAL_DTYPES, which is read by assert_canonical_frame
            # and by the tests.
            CANONICAL_DTYPES[name],  # type: ignore[arg-type]
            nullable=name in NULLABLE_COLUMNS,
            unique=False,
        )
        for name in schema_columns
    }


canonical_v1_schema = pa.DataFrameSchema(
    _columns(CANONICAL_COLUMNS),
    checks=_CHECKS,
    # strict=True: an unrecognised column is schema drift, and ignoring it is how a
    # renamed upstream field becomes an all-null feature nobody notices until a demo.
    strict=True,
    # coerce=False is the whole point of the file. 01 B and 02 D agree from opposite
    # ends: an adapter yields canonical-valid rows or raises. A schema that quietly
    # widened Int64 to Float64 to "help" would turn a money defect into an invisible
    # one that only shows up when someone totals a column in a packet.
    coerce=False,
    name="canonical_v1",
)

canonical_v1_persisted_schema = pa.DataFrameSchema(
    _columns(PERSISTED_CANONICAL_COLUMNS),
    checks=_CHECKS,
    strict=True,
    coerce=False,
    name="canonical_v1_persisted",
)


class CanonicalContractError(RuntimeError):
    """Raised when a frame is not a valid canonical event table.

    Carries the offending column name in ``column`` so a caller can group failures
    by column instead of by sentence.
    """

    def __init__(self, message: str, *, column: str | None = None) -> None:
        super().__init__(message)
        self.column = column


def column_diff(frame_columns: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return ``(missing, unknown)`` against the full canonical list, order-free."""
    declared = set(CANONICAL_COLUMNS)
    present = set(frame_columns)
    return sorted(declared - present), sorted(present - declared)


def _validate_columns(frame_columns: Sequence[str], expected: Sequence[str], label: str) -> None:
    missing = [name for name in expected if name not in frame_columns]
    unknown = [name for name in frame_columns if name not in expected]
    if missing:
        raise CanonicalContractError(
            f"{ERR_MISSING_COLUMN}: {label} is missing required column(s) {missing}",
            column=missing[0],
        )
    if unknown:
        raise CanonicalContractError(
            f"{ERR_UNKNOWN_COLUMN}: {label} carries undeclared column(s) {unknown}; "
            "canonical v1 is strict, so an extra column is schema drift and not a "
            "detail to ignore",
            column=unknown[0],
        )


def assert_canonical_frame(frame: pl.DataFrame, *, persisted: bool | None = None) -> pl.DataFrame:
    """Fail loud, naming the offending column, unless ``frame`` is canonical v1.

    Checks in the order that produces the most specific message: column set first
    (so a rename says which name it missed), then dtype (so a float says which
    money column it is), then nulls, then the pandera schema's row-level checks.
    ``coerce=False`` throughout means a dtype mismatch is a failure, never a cast,
    which is what ``test_zero_silent_coercions`` asserts.

    ``persisted=None`` infers the shape from the frame: the on-disk Parquet omits
    the DEV-012 sidecar columns, so the same function validates both the in-memory
    contract frame and the bytes that get written.
    """
    if persisted is None:
        persisted = not all(name in frame.columns for name in SIDECAR_COLUMNS)
    expected = PERSISTED_CANONICAL_COLUMNS if persisted else CANONICAL_COLUMNS
    label = "canonical_v1_persisted" if persisted else "canonical_v1"

    _validate_columns(frame.columns, expected, label)

    actual = frame.collect_schema()
    mismatches = [
        (name, f"{name}: declared {CANONICAL_DTYPES[name]}, got {actual[name]}")
        for name in expected
        if actual[name] != CANONICAL_DTYPES[name]
    ]
    if mismatches:
        first_column, first_message = mismatches[0]
        raise CanonicalContractError(
            f"{ERR_DTYPE_MISMATCH}/{ERR_COERCION_REQUIRED}: "
            + "; ".join(message for _name, message in mismatches)
            + ". coerce=False, so this is a failure and not a cast.",
            column=first_column,
        )

    required_columns = [name for name in expected if name not in NULLABLE_COLUMNS]
    nulls = frame.select([pl.col(name).null_count().alias(name) for name in required_columns])
    offenders = [
        (name, int(nulls.get_column(name)[0]))
        for name in nulls.columns
        if int(nulls.get_column(name)[0])
    ]
    if offenders:
        name, count = offenders[0]
        raise CanonicalContractError(
            f"{ERR_NULL_IN_REQUIRED}: column {name} has {count} null(s) and is not "
            f"nullable; only {list(NULLABLE_COLUMNS)} may be null in {label}",
            column=name,
        )

    target = canonical_v1_persisted_schema if persisted else canonical_v1_schema
    try:
        target.validate(frame)
    except Exception as exc:  # pandera raises several SchemaError shapes
        # Re-raised as the one exception type callers can catch, with the check's own
        # machine-readable code preserved so a quarantine record says which rule
        # failed rather than quoting a stack trace.
        code, columns = _code_from_schema_error(str(exc))
        named = f" (columns: {list(columns)})" if columns else ""
        raise CanonicalContractError(
            f"{code}: {str(exc).splitlines()[0]}{named}",
            column=columns[0] if columns else None,
        ) from exc
    return frame


_CHECK_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    ERR_TXN_ID_NOT_NAMESPACED: ("txn_id", "source_dataset"),
    ERR_DUPLICATE_TOTAL_ORDER: ("event_ts_utc", "txn_id"),
    ERR_NEGATIVE_MONEY: MONEY_COLUMNS,
    ERR_LABEL_NOT_BINARY: ("label_is_fraud", "label_is_flagged"),
    ERR_LOCAL_HOUR_RANGE: ("local_hour",),
    ERR_DATE_DRIFT: ("event_date_local", "event_ts_utc"),
    ERR_ACCOUNT_KEY_SHAPE: ("account_from", "account_to"),
}


def _code_from_schema_error(text: str) -> tuple[str, tuple[str, ...]]:
    """Pair a pandera failure with the check's own code and the columns it reads.

    The code is what a quarantine record stores; the columns are what makes the
    message actionable, since pandera reports a failed frame-level check by the
    function's name and never says which column drove it.
    """
    for code, columns in _CHECK_COLUMNS.items():
        if code in text:
            return code, columns
    return "canonical_check_failed", ()


def assert_label_provenance(
    frame: pl.DataFrame, *, source_carries_typology: bool, slice_requested: bool = False
) -> None:
    """Assert every canonical row declares where its labels came from.

    Three separate failures are being prevented, which is why the message names
    whichever one happened:

    * ``source_dataset`` null or blank means the row does not say which corpus it
      came from, so the per-corpus reporting the gate requires (01 P1: "we report
      metrics per corpus and never average them") is impossible.
    * ``source_carries_typology=True`` with a null ``label_typology`` is normal and
      means the row sits outside any annotated block. What is not normal is *every*
      row being null: DEV-014 measured 3,209 annotated transactions inside a
      5,078,345-row stream, so annotations cover six hundredths of one percent and
      the rest is the graph's background topology. A whole frame of nulls means the
      annotation join stopped working, which is how the network pillar loses its own
      ground truth without any error being raised.
    * ``source_carries_typology=True`` with a *blank* (empty or whitespace) value
      means an annotation lost its name — a typology that is not a named typology is
      not evidence of anything.
    * ``source_carries_typology=False`` with a *non-null* ``label_typology`` means
      somebody invented a typology for PaySim, which has none. That is worse than a
      null, because it reads like evidence.
    """
    if frame.height == 0:
        raise CanonicalContractError(
            f"{ERR_LABEL_PROVENANCE}: cannot assert label provenance on an empty frame"
        )
    source = frame.get_column("source_dataset")
    blank = (source.str.strip_chars() == "").any()
    if bool(source.is_null().any()) or bool(blank):
        raise CanonicalContractError(
            f"{ERR_LABEL_PROVENANCE}: source_dataset is null or blank on some rows; "
            "every canonical row must name the corpus its labels came from",
            column="source_dataset",
        )

    typology = frame.get_column("label_typology")
    null_count = int(typology.null_count())
    if source_carries_typology:
        named = typology.drop_nulls()
        blank_named = int((named.str.strip_chars() == "").sum())
        if blank_named:
            raise CanonicalContractError(
                f"{ERR_LABEL_PROVENANCE}: {blank_named} row(s) carry a blank "
                "label_typology; an annotation that lost its name is not a typology",
                column="label_typology",
            )
        if null_count == frame.height and not slice_requested:
            raise CanonicalContractError(
                f"{ERR_LABEL_PROVENANCE}: this source ships a typology taxonomy and none "
                f"of the {frame.height} canonical row(s) joined to it. One null per row "
                "is expected — DEV-014 measured 3,209 annotated rows out of 5,078,345 — "
                "but a frame with zero annotations is a broken join, not an unlabelled "
                "corpus. If this run read a `--limit` slice, pass slice_requested=True: a "
                "slice from the head of the file can legitimately contain no "
                "annotations, and that is reported rather than refused.",
                column="label_typology",
            )
    elif null_count != frame.height:
        raise CanonicalContractError(
            f"{ERR_LABEL_PROVENANCE}: this source has no typology taxonomy, so "
            "label_typology must be null on every row; a value there is an invented "
            "label and reads like evidence",
            column="label_typology",
        )


def assert_balance_provenance(frame: pl.DataFrame) -> None:
    """Assert a frame's balance columns are either fully present or wholly absent.

    Both are legitimate. PaySim ships a ledger, so every row carries four balances
    (and the plan treats their inconsistency as a feature signal, not an error).
    IBM-AML ships no ledger at all, so every balance is null and no balance-derived
    feature may be claimed for it. What is not legitimate is a mixture: that is a
    parse which lost a column on some rows, and because the columns are nullable in
    the schema, nothing else in the pipeline would notice.

    ``source_dataset`` is checked first per row, so a frame mixing corpora is refused
    rather than passing on the strength of one corpus's complete ledger.
    """
    if frame.height == 0:
        return
    corpora = frame.get_column("source_dataset").unique().sort().to_list()
    if len(corpora) != 1:
        raise CanonicalContractError(
            f"{ERR_BALANCE_PROVENANCE}: a batch spans {len(corpora)} corpora "
            f"({corpora}); balance presence is asserted per source, so batches must be "
            "kept apart",
        )
    for name in BALANCE_COLUMNS:
        column = frame.get_column(name)
        nulls = int(column.null_count())
        if nulls and nulls != frame.height:
            raise CanonicalContractError(
                f"{ERR_BALANCE_PROVENANCE}: {name} is null on {nulls} of {frame.height} "
                f"{corpora[0]} row(s). Either the source has a ledger and every row "
                "carries it, or it has none and no row does -- a partial ledger is a "
                "column that failed to parse, and a null here would then be read "
                "downstream as 'this account had no balance' rather than 'unknown'",
                column=name,
            )


def empty_canonical_frame(*, persisted: bool = True) -> pl.DataFrame:
    """An empty canonical table with the full declared dtype map.

    Concatenation over zero batches and the "nothing arrived" branch both need a
    frame whose schema is the real one. Hand-writing ``pl.DataFrame()`` there would
    produce a frame with no columns at all, which fails the contract in a way that
    looks like a source problem rather than like the empty case it is.
    """
    columns = PERSISTED_CANONICAL_COLUMNS if persisted else CANONICAL_COLUMNS
    return pl.DataFrame(
        {name: pl.Series(name, [], dtype=CANONICAL_DTYPES[name]) for name in columns}
    )


def utc_now_us() -> dt.datetime:
    """The wall clock in UTC, at the resolution ``ingested_at`` is stored at.

    ``datetime.now`` is microsecond-precise in CPython, so no narrowing happens on
    insert and polars never raises on an implicit cast. The single reason this is a
    function rather than a call site: ``ingested_at`` is stamped in exactly one
    place per batch (DEV-012), and a per-row ``now()`` would put a different
    microsecond into every row of six million and make the column meaningless as
    the moment the batch crossed the boundary.
    """
    return dt.datetime.now(dt.UTC)


__all__ = [
    "ACCOUNT_KEY_LENGTH",
    "BALANCE_COLUMNS",
    "CANONICAL_COLUMNS",
    "CANONICAL_DTYPES",
    "CANONICAL_TOTAL_ORDER",
    "ERR_ACCOUNT_KEY_SHAPE",
    "ERR_BALANCE_PROVENANCE",
    "ERR_COERCION_REQUIRED",
    "ERR_DATE_DRIFT",
    "ERR_DTYPE_MISMATCH",
    "ERR_DUPLICATE_TOTAL_ORDER",
    "ERR_LABEL_NOT_BINARY",
    "ERR_LABEL_PROVENANCE",
    "ERR_LOCAL_HOUR_RANGE",
    "ERR_MISSING_COLUMN",
    "ERR_NEGATIVE_MONEY",
    "ERR_NULL_IN_REQUIRED",
    "ERR_TXN_ID_NOT_NAMESPACED",
    "ERR_UNKNOWN_COLUMN",
    "MONEY_COLUMNS",
    "NULLABLE_COLUMNS",
    "PERSISTED_CANONICAL_COLUMNS",
    "SIDECAR_COLUMNS",
    "TIMESTAMP_COLUMNS",
    "UTC_MICROS",
    "CanonicalContractError",
    "assert_balance_provenance",
    "assert_canonical_frame",
    "assert_label_provenance",
    "canonical_v1_persisted_schema",
    "canonical_v1_schema",
    "column_diff",
    "empty_canonical_frame",
]
