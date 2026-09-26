"""IBM-AML adapter: HI-Small_Trans.csv rows to validated canonical events, or a loud failure.

WHY THIS ADAPTER CARRIES THE BUILD. DEV-011 measured PaySim's median account degree
at 1.0 with zero surviving time-respecting cycles, which moved IBM-AML from secondary
to PRIMARY for Module B: the 12 typology rules, the graph features and the
network-detection thesis all read the canonical events this file produces. It is not a
nice-to-have alongside PaySim.

IT RUNS ON REAL BYTES, AND THAT IS NEW. The previous revision was written against the
corpus's published description and was unrunnable: it declared fourteen columns
(``stepFrom``/``stepTo``/``Type``/``Category``/``Amount``/``nameOrig``/``balanceOrig``/
``nameDest``/``balanceDest``/``isLaundering``/``isFlood``/``isDateSpam``/
``isForcedCashout``/``unlabeled``) belonging to a different IBM artefact.
``data/raw/ibmaml/HI-Small_Trans.csv`` on this host is 475,664,283 bytes, 5,078,345 data
rows, SHA-256 pinned in ``config/sources.yaml``, and carries eleven columns of which two
are both named ``Account``. Nothing in the old mapping applies to it. Every number in the
comments below was measured over those bytes on 2026-09-26, not recalled; the contract in
``contracts/raw_ibm_aml.py`` carries the same header and the same value domains.

MAPPING DECISIONS, each forced by a measurement or a governing rule:

* **Positional read.** :func:`read_raw_batches` reads with ``has_header=False`` into the
  contract's positional names, and the verbatim header is asserted separately by
  :func:`read_header`. Polars renames the duplicate ``Account`` to
  ``Account_duplicated_0``, so a name-keyed reader either reads one column twice or loses
  the receiver, and every transaction silently becomes a transfer to itself.
* ``txn_id`` = ``ibmaml:<zero-padded ordinal>:<8-hex content digest>``. Content-derived
  alone is not enough here: timestamps are minute-precise, (sender, receiver, amount)
  repeats inside a minute, and nine rows in this file are identical across all eleven
  columns (measured). The graph layer's ``require_events`` rightly refuses a duplicate
  ``txn_id``, because the total order is ``(event_ts_utc, txn_id)`` and a tie there is not
  one the engine may resolve arbitrarily. The ordinal is file-relative, so an id does not
  move when the batch size or the ``limit`` changes, and the digest keeps each id
  traceable to the row that produced it.
* ``account_from`` / ``account_to`` = ``account_key()`` over the (bank, account) PAIR,
  not over the account string. ``HI-Small_accounts.csv`` keys an entity on Bank ID plus
  Account Number, and four account numbers recur under two different banks in this file,
  so the account alone would merge distinct accounts into one node and hand each other
  their counterparties, degree and community. IBM's ids are already pseudonymous hashes
  and are still re-salted (03 D): one salting scheme covers both corpora, and an unsalted
  hash is reversible against a list of candidate ids.
* ``amount_minor`` = ``Amount Paid` — what leaves the sender — at the currency's own
  minor exponent, from the decimal string, through integer arithmetic only.
  ``float("9839.64") * 100`` is 983963.9999999999, and 146,066 Bitcoin rows carry six
  fractional digits that a fixed x100 rule would silently round away. ``Amount Received``
  has no slot in canonical v1: where the two differ (72,158 rows) the difference is a
  spread or fee, and it is currently dropped. That is a LIMITATIONS item, not a column to
  invent here.
* ``currency`` = the 3-letter ISO-4217 code from the explicit name table in the contract,
  taken from ``Payment Currency`` so it describes the amount that was actually paid.
* ``event_ts_utc`` is parsed from the file's own ``YYYY/MM/DD HH:MM`` wall clock. There is
  no ``step`` column, so there is no epoch to expand and no synthetic intra-step offset to
  hash in. The bytes carry no timezone, so the instants are treated as being in
  ``ibmaml.source_timezone_assumption`` from ``config/pipeline.yaml`` — a stated
  ASSUMPTION, not a fact. ``event_date_local`` and ``local_hour`` are then derived from
  ``deployment_timezone`` exactly once, here (03 C).
* ``channel`` carries ``Payment Format`` verbatim and ``txn_type`` carries its stable
  upper-case token. This corpus does have a payment-channel dimension; the fabricated
  mapping recorded it as null.
* The four ``*_balance_minor`` columns are NULL, not zero (03 A rule 2). The corpus has no
  balance columns at all, so the exposure proxy differs per corpus and exposure is
  reported per corpus, never averaged into one headline number.
* A self-transfer (sender == receiver) is KEPT. 591,212 rows — 11.6% of the file — are
  self-transfers, and every Reinvestment row is one. PaySim's contract rejects
  ``self_transfer`` at the boundary; doing that here would quarantine an eighth of the
  corpus, delete real annotated typology rows and fail the run on valid data. Plan §7
  excludes src == dst from cycle and fan detection at the GRAPH layer while keeping the
  self-transfer count as a feature, and that is where the exclusion belongs.
* ``label_is_fraud`` is ``Is Laundering``, the corpus's only label (5,177 positives,
  0.1019%). ``label_is_flagged`` is 0 on every row: no threshold-flag concept exists here,
  and canonical v1 requires the column non-null, so the value records "no equivalent"
  rather than "checked and cleared".
* ``label_typology`` comes from the join artifact ``data/processed/ibm_typologies.parquet``
  (built by ``scripts/build_ibm_typologies.py``) keyed on the row ordinal, never from a
  label column. Every annotated row is checked to carry ``Is Laundering == 1``; if that
  invariant breaks the run fails with the mismatch count rather than shipping positions
  that have shifted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import polars as pl

from oxbow.contracts.raw_ibm_aml import (
    CURRENCY_ISO,
    IBMAML_ACCOUNT_CODE_PATTERN,
    IBMAML_AMOUNT_PAID_COLUMN,
    IBMAML_BANK_CODE_PATTERN,
    IBMAML_CURRENCY_PATTERN,
    IBMAML_DASHED_TIMESTAMP_FORMAT,
    IBMAML_LABEL_COLUMN,
    IBMAML_MONEY_TEXT_PATTERN,
    IBMAML_ORDINAL_FILTER_PATTERN,
    IBMAML_PAYMENT_FORMATS,
    IBMAML_POSITIONAL_COLUMNS,
    IBMAML_RAW_HEADER,
    IBMAML_TIMESTAMP_FORMAT,
    IBMAML_TIMESTAMP_PATTERN,
    IBMAML_TYPOLOGY_KEY_COLUMN,
    IBMAML_TYPOLOGY_VALUES,
    MINOR_EXPONENT_BY_NAME,
    TXN_TYPE_BY_PAYMENT_FORMAT,
    raw_ibm_aml_schema,
)
from oxbow.identity import is_ulid
from oxbow.ingest.canonical import (
    CANONICAL_COLUMNS,
    CANONICAL_DTYPES,
    CanonicalizationError,
    RunIdentity,
    account_keys_for_names,
    batch_id_for_rows,
    hash_workers,
    hmac_sha256_hex,
    local_date_expr,
    local_hour_expr,
    map_in_chunks,
    resolve_local_timezone,
)
from oxbow.ingest.paysim import (
    DEFAULT_FUTURE_TOLERANCE_HOURS,
    IngestResult,
    QuarantineRecord,
)
from oxbow.ports.source import (
    BatchManifest,
    assert_no_float_money,
)
from oxbow.ports.source import (
    QuarantineRecord as PortQuarantineRecord,
)

IBMAML_SOURCE_NAME: Final = "ibmaml"

# DEV-004: the namespace prefix sits outside the id so one transaction table can hold
# both corpora without a collision, and canonical v1's ``_txn_id_namespaced`` check
# asserts the prefix against ``source_dataset`` rather than against a literal.
TXN_ID_NAMESPACE: Final = "ibmaml"

# 5,078,345 rows in this bundle; nine digits cover the largest sibling bundle without an
# id whose width — and therefore whose lexicographic order — changes between corpora.
TXN_ID_ORDINAL_WIDTH: Final = 9

# Eight hex characters. The ordinal already guarantees uniqueness; the digest is here so
# an id quoted in a case packet can be traced back to the row that produced it.
TXN_ID_DIGEST_HEX: Final = 8

# DEV-003: run_id is a ULID, stored as char(26); the check lives in ``oxbow.identity``.
STREAM_CHUNK: Final = 1 << 20

# The sentinel ``scripts/download_data.py`` leaves until a hash is measured. All three
# HI-Small members are pinned for real now, so this is the "nothing to compare against"
# state rather than the normal one — and it must still not read as a mismatch.
RECORDED_AT_DOWNLOAD: Final = "RECORDED_AT_DOWNLOAD"

BALANCE_COLUMNS: Final[tuple[str, ...]] = (
    "src_balance_before_minor",
    "src_balance_after_minor",
    "dst_balance_before_minor",
    "dst_balance_after_minor",
)

# Internal columns that never reach the canonical frame. The ordinal is the row's
# position in the timestamp-filtered scan, which is also the key the typology join uses;
# the line is the physical file position, which is what a quarantine record must quote.
ORDINAL_COLUMN: Final = "_ordinal"
LINE_COLUMN: Final = "_line"
ROW_KEY_COLUMN: Final = "_row_key"

# The separator inside a row-content key. ``\\x1f`` cannot appear in a CSV field, so a key
# is never ambiguous about where one column ended.
KEY_SEPARATOR: Final = "\x1f"

# Fixed width of the micro-unit scale used for exact decimal shifting. 10**8 is the
# largest exponent any currency in the contract's table needs (Bitcoin's satoshi), so
# every amount is first expressed in 1e-8 units and then divided by an exact power of ten.
# Integer arithmetic only: no amount is ever multiplied by a float.
MICRO_EXPONENT: Final = 8

# The two per-currency integer factors, derived from the contract's exponent table so a
# new currency cannot arrive with a scale nobody chose.
_POW10_BY_NAME: Final[dict[str, int]] = {
    name: 10**exponent for name, exponent in MINOR_EXPONENT_BY_NAME.items()
}
_MICRO_DIVISOR_BY_NAME: Final[dict[str, int]] = {
    name: 10 ** (MICRO_EXPONENT - exponent) for name, exponent in MINOR_EXPONENT_BY_NAME.items()
}

# An Int64 minor value must not wrap: polars does not check overflow, so a row whose
# integer part would exceed the room the exponent leaves is refused rather than silently
# negated. Measured maximum integer-part length in this file is 13 digits.
_MAX_WHOLE_DIGITS: Final[dict[int, int]] = {2: 16, 8: 10}
_MAX_WHOLE_DIGITS_BY_NAME: Final[dict[str, int]] = {
    name: _MAX_WHOLE_DIGITS[exponent] for name, exponent in MINOR_EXPONENT_BY_NAME.items()
}


# --- failures ------------------------------------------------------------


class BatchIntegrityError(RuntimeError):
    """Raised when a file's bytes disagree with what the batch promised.

    03 B: a batch is accepted whole or rejected whole. Half a file looks like a quiet
    period, and a quiet period looks like normal behaviour, which is the specific failure
    this exception exists to prevent.
    """


class TypologyAlignmentError(RuntimeError):
    """Raised when the typology join lands on rows the corpus does not label.

    The join artifact is keyed by row ordinal in the timestamp-filtered scan, so a drift
    between the scan this adapter makes and the scan ``scripts/build_ibm_typologies.py``
    made would still produce a plausible-looking ``label_typology`` column: 3,209 rows
    would carry a pattern, just the wrong one, and the per-typology recall that is the
    whole point of DEV-011 would be measuring noise. The corpus's own assertion is the
    guard — every annotated row carries ``Is Laundering == 1`` — so a disagreement is
    fatal and reports the count rather than being averaged over.
    """


# --- canonical projection -------------------------------------------------

# The canonical contract is declared ONCE, in ``oxbow.contracts.canonical_v1``, and
# re-exported by ``ingest/canonical.py``. This adapter deliberately carries no column
# list and no dtype table of its own: the two names below are bindings to the central
# declaration, so a canonical v2 lands in one file and every adapter follows. What this
# module does assert is the subset of the contract it depends on structurally — the
# columns this corpus must write have to exist, or its missing data silently becomes a
# missing column instead.

CANONICAL_P1B_COLUMNS: Final[tuple[str, ...]] = CANONICAL_COLUMNS
CANONICAL_EVENT_DTYPES: Final[dict[str, pl.DataType]] = CANONICAL_DTYPES


def _assert_projection_supports_this_corpus() -> None:
    """Every column this corpus must write has to exist in the shared contract."""
    required = (
        *BALANCE_COLUMNS,
        "channel",
        "txn_type",
        "label_is_fraud",
        "label_is_flagged",
        "label_typology",
        "currency",
        "ingested_at",
        "run_id",
        "batch_id",
    )
    absent = [name for name in required if name not in CANONICAL_P1B_COLUMNS]
    if absent:
        raise CanonicalizationError(
            f"canonical v1 as centralised lacks {absent}; {IBMAML_SOURCE_NAME} cannot emit "
            "a contract it has nowhere to put these values."
        )
    if tuple(CANONICAL_EVENT_DTYPES) != CANONICAL_P1B_COLUMNS:
        raise CanonicalizationError(
            "CANONICAL_DTYPES and CANONICAL_COLUMNS disagree on order or membership; the "
            "canonical contract must be one declaration, not two"
        )


_assert_projection_supports_this_corpus()


# --- config ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IBMAMLIngestPolicy:
    """The ``ibmaml:`` block of ``config/pipeline.yaml``, typed at the boundary.

    The config file owns these values (00 G); this is the read path, and a missing or
    malformed key is a startup failure rather than a silent default. Note what is NOT
    here: there is no epoch and no step size, because the corpus carries real minutes
    rather than day counters, and there is no intra-step offset salt, because a
    minute-precision timestamp needs no synthetic jitter — rows sharing a minute are
    ordered by ``txn_id``, which is the total order the contract already fixes.
    """

    source_timezone: ZoneInfo
    timestamp_format: str
    typology_artifact: Path
    deployment_timezone: str

    @property
    def source_timezone_name(self) -> str:
        return self.source_timezone.key

    @property
    def parse_format(self) -> str:
        """The strptime pattern for the separator-normalised text."""
        return IBMAML_DASHED_TIMESTAMP_FORMAT


def policy_from_config(
    block: Mapping[str, Any], *, repo_root: Path, deployment_timezone: str
) -> IBMAMLIngestPolicy:
    """Build the ingest policy from the raw ``ibmaml:`` mapping, validating each key.

    ``source_timezone_assumption`` is resolved here rather than in the middle of a
    five-million-row ``collect``, because Polars raises an unlocatable ComputeError from
    inside a query. It is deliberately not cross-checked against
    ``deployment_timezone``: the two are separate decisions — the zone the source's naive
    clocks are read in, and the zone the operator's ``local_hour`` is computed in — and
    they only happen to agree for this deployment.
    """
    for key in ("source_timezone_assumption", "timestamp_format", "typology_join_artifact"):
        if key not in block:
            raise CanonicalizationError(f"config/pipeline.yaml ibmaml block lacks '{key}'")
    zone_name = str(block["source_timezone_assumption"])
    try:
        source_timezone = ZoneInfo(zone_name)
    except Exception as exc:
        raise CanonicalizationError(
            f"ibmaml.source_timezone_assumption {zone_name!r} is not resolvable by the host "
            f"tz database: {exc}. It is an ASSUMPTION about undeclared wall clocks, so it "
            "has to name a real zone rather than guess."
        ) from exc
    timestamp_format = str(block["timestamp_format"])
    if timestamp_format not in (IBMAML_TIMESTAMP_FORMAT, IBMAML_DASHED_TIMESTAMP_FORMAT):
        raise CanonicalizationError(
            f"ibmaml.timestamp_format is {timestamp_format!r}; the corpus is read as either "
            f"{IBMAML_TIMESTAMP_FORMAT!r} or {IBMAML_DASHED_TIMESTAMP_FORMAT!r} and "
            "nothing else, "
            "because the row ordinal that keys the typology join is defined by which lines "
            "parse as a minute."
        )
    artifact = repo_root / str(block["typology_join_artifact"])
    return IBMAMLIngestPolicy(
        source_timezone=source_timezone,
        timestamp_format=timestamp_format,
        typology_artifact=artifact,
        deployment_timezone=deployment_timezone,
    )


def read_typology_artifact(path: Path) -> pl.DataFrame:
    """Load the typology join artifact as a join-ready (ordinal, typology) frame.

    Checked rather than trusted: the file is produced by a separate script, and a silent
    change to its key column, its vocabulary or its uniqueness would show up as a
    plausible ``label_typology`` column rather than as an error — the worst outcome for a
    corpus whose whole purpose is per-typology ground truth.
    """
    if not path.is_file():
        raise CanonicalizationError(
            f"typology join artifact {path} is missing. Run "
            "`uv run python scripts/build_ibm_typologies.py` first: without it every "
            "label_typology would be null and DEV-011's per-typology premise would quietly "
            "become one unlabeled class."
        )
    artifact = pl.read_parquet(path)
    required = (IBMAML_TYPOLOGY_KEY_COLUMN, "typology")
    missing = [name for name in required if name not in artifact.columns]
    if missing:
        raise CanonicalizationError(
            f"{path.name} lacks {missing}; it carries {list(artifact.columns)} but "
            "build_ibm_typologies.py is expected to key rows by "
            f"{IBMAML_TYPOLOGY_KEY_COLUMN} with a `typology` column."
        )
    if artifact.schema[IBMAML_TYPOLOGY_KEY_COLUMN] != pl.Int64:
        raise CanonicalizationError(
            f"{path.name}: {IBMAML_TYPOLOGY_KEY_COLUMN} must be Int64 row ordinals, got "
            f"{artifact.schema[IBMAML_TYPOLOGY_KEY_COLUMN]}"
        )
    unknown = sorted(
        set(artifact.get_column("typology").cast(pl.String).unique().to_list())
        - set(IBMAML_TYPOLOGY_VALUES)
    )
    if unknown:
        raise CanonicalizationError(
            f"{path.name} carries typologies outside the measured vocabulary {unknown}; "
            "the rule set and the scorecard bins reason about the eight named categories "
            "and nothing else, so a ninth is a decision, not a value."
        )
    if bool(artifact.get_column(IBMAML_TYPOLOGY_KEY_COLUMN).is_duplicated().any()):
        raise CanonicalizationError(
            f"{path.name}: a row ordinal appears in two attempt blocks. The join would fan "
            "one transaction out into several canonical rows."
        )
    return artifact.rename(
        {IBMAML_TYPOLOGY_KEY_COLUMN: ORDINAL_COLUMN, "typology": "label_typology"}
    ).select(pl.col(ORDINAL_COLUMN), pl.col("label_typology"))


# --- reading --------------------------------------------------------------


def schema_error_codes() -> tuple[str, ...]:
    """The check codes the contract declares, read off the schema itself.

    Derived rather than retyped, so adding a check to the contract cannot leave the
    adapter's reason mapping silently behind.
    """
    codes: list[str] = []
    for check in raw_ibm_aml_schema.checks:
        code = getattr(check, "error", None)
        if isinstance(code, str):
            codes.append(code)
    return tuple(codes)


def read_header(path: Path) -> list[str]:
    """The file's own header line, split on commas.

    Read as ``utf-8-sig`` because a Kaggle CSV can open with a BOM, and a BOM left
    attached would make the first column name disagree with the contract for a reason that
    has nothing to do with the corpus's shape. The result keeps the duplicate ``Account``:
    this function's whole job is to compare against the bytes.
    """
    with path.open(encoding="utf-8-sig", newline="\n") as handle:
        first_line = handle.readline().rstrip("\n").rstrip("\r")
    return first_line.split(",")


def _open_positional(path: Path, *, limit: int | None) -> pl.DataFrame:
    """One positional, all-String read of the data rows. The single reader in this file.

    ``has_header=True`` with an explicit ``schema`` is what makes this a positional read:
    the reader consumes the header line itself and takes the column NAMES from the schema
    in file order, so the duplicated ``Account`` never has to be resolved by name at all.
    ``has_header=False`` with ``skip_rows=1`` was the obvious spelling and is wrong twice
    over — it is 4.5x slower on this file because it defeats parallel parsing, and on a
    header-only file ``skip_rows`` walks past the end and hands back one phantom all-null
    row, which a zero-row source is supposed to refuse rather than mis-count.

    Every column is forced to String. Left to inference the money columns would become
    Float64 and the bank codes would lose their leading zeros — both before the contract
    that exists to notice could run (01 B).

    A line with MORE fields than the schema is raised by the reader itself, which is the
    right direction of failure: eleven columns is the contract, and a twelfth field means
    the file is not this corpus. A line with FEWER arrives as nulls, which the row-level
    ``null_in_required_column`` reason catches per row.
    """
    return pl.read_csv(
        path,
        n_rows=limit,
        has_header=True,
        schema=dict.fromkeys(IBMAML_POSITIONAL_COLUMNS, pl.String()),
        infer_schema_length=0,
    )


def read_raw_batches(
    path: Path,
    *,
    batch_rows: int = 100_000,
    limit: int | None = None,
) -> list[pl.DataFrame]:
    """Read the raw CSV positionally and slice it into deterministic batches.

    ``has_header=False`` with ``skip_rows=1`` is not a quirk: the header repeats the name
    ``Account``, so letting the reader name the columns produces ``Account_duplicated_0``
    and a mapping step that can silently pick the wrong side of the pair. Naming by
    position and asserting the header separately means the pair is unambiguous and a
    reordered file is caught before any row is parsed.

    Batching is a memory and reporting concern only: ``txn_id`` is built from the
    file-relative ordinal, so a row carries the same id at any ``batch_rows`` or ``limit``.
    ``pl.read_csv_batched`` was not used because it hands out whatever its worker threads
    finish first, and boundaries that move between runs move every derived batch id.
    """
    frame = _open_positional(path, limit=limit)
    if frame.height == 0:
        return []
    return [frame.slice(start, batch_rows) for start in range(0, frame.height, batch_rows)]


def count_in_scan_rows(frame: pl.DataFrame) -> int:
    """How many rows of a raw batch fall inside the timestamp-filtered scan."""
    return int(
        frame.get_column(IBMAML_POSITIONAL_COLUMNS[0])
        .str.contains(IBMAML_ORDINAL_FILTER_PATTERN, literal=False)
        .sum()
    )


# --- quarantine reasons ---------------------------------------------------


def _reason_from_schema_error(exc: Exception) -> str:
    """Map a pandera failure onto the failing check's own error code.

    Codes appear verbatim in pandera's message, so the quarantine reason is the rule that
    failed rather than a stringified traceback. A triage run groups on this string;
    ``schema_violation`` is the fallback for a failure the contract does not name, and it
    is deliberately visible rather than guessed at.
    """
    text = str(exc)
    for code in schema_error_codes():
        if code in text:
            return code
    if "not in DataFrameSchema" in text:
        return "unknown_column"
    if "not in dataframe" in text or "is missing" in text:
        return "missing_column"
    if "non-nullable column" in text or "null count" in text:
        return "null_in_required_column"
    if "to have type" in text or "dtype" in text:
        return "column_dtype"
    return "schema_violation"


def _structural_failure(frame: pl.DataFrame, *, line_offset: int) -> QuarantineRecord | None:
    """Whole-batch structural gate, recorded once rather than per row.

    A file with the wrong columns is a source problem, not 100,000 row problems: the
    caller gets one record naming the offending column(s) instead of a quarantine table
    repeating one sentence a hundred thousand times and burying the cause.
    """
    present = list(frame.columns)
    if present != list(IBMAML_POSITIONAL_COLUMNS):
        missing = [name for name in IBMAML_POSITIONAL_COLUMNS if name not in present]
        extra = [name for name in present if name not in IBMAML_POSITIONAL_COLUMNS]
        return QuarantineRecord(
            reason="schema_mismatch",
            source_dataset=IBMAML_SOURCE_NAME,
            row_index=line_offset,
            payload={"columns": present, "missing": missing, "unexpected": extra},
            detail=(
                "columns do not match the declared positional IBM-AML contract; expected "
                f"{list(IBMAML_POSITIONAL_COLUMNS)} (from the verbatim header "
                f"{list(IBMAML_RAW_HEADER)}), got {present}. The batch is rejected whole."
            ),
        )
    actual = frame.collect_schema()
    mismatched = {
        name: f"declared String, got {actual[name]}"
        for name in IBMAML_POSITIONAL_COLUMNS
        if actual[name] != pl.String()
    }
    if mismatched:
        return QuarantineRecord(
            reason="column_dtype",
            source_dataset=IBMAML_SOURCE_NAME,
            row_index=line_offset,
            payload={"columns": present},
            detail=(
                f"column dtype(s) disagree with the declared contract: {mismatched}. "
                "coerce=False, so this rejects the batch instead of casting the data."
            ),
        )
    return None


def _real_minute_expr() -> pl.Expr:
    """The timestamp parsed strictly-but-silently: null where the minute is not real.

    Mirrors the contract's ``timestamp_out_of_range`` check. It is a separate reason from
    ``timestamp_format`` because the two failures mean different things to whoever reads
    the ledger: one says the column is not a timestamp at all, the other says it is a
    timestamp that never happened.
    """
    return (
        pl.col("ts")
        .str.strip_chars()
        .str.replace_all("/", "-")
        .str.strptime(pl.Datetime("us"), IBMAML_DASHED_TIMESTAMP_FORMAT, strict=False)
    )


def _money_shape_expr(column: str) -> pl.Expr:
    """A non-negative plain decimal, tested on the text before any conversion."""
    return pl.col(column).str.strip_chars().str.contains(IBMAML_MONEY_TEXT_PATTERN, literal=False)


def _fraction_digits_expr(column: str) -> pl.Expr:
    """How many digits sit after the decimal point, zero when there is no point."""
    return pl.col(column).str.split(".").list.get(1, null_on_oob=True).str.len_chars().fill_null(0)


def _whole_digits_expr(column: str) -> pl.Expr:
    """How many digits sit before the decimal point, which bounds the Int64 result."""
    return pl.col(column).str.split(".").list.get(0, null_on_oob=True).str.len_chars()


def _row_reason(frame: pl.DataFrame) -> pl.Series:
    """The first failing value-level contract check per row, else null.

    Mirrors the frame-level checks declared in ``contracts/raw_ibm_aml.py`` as
    expressions with the same reason codes, in a fixed priority order so counts by cause
    sum to the quarantine count instead of exceeding it when a row fails two checks. The
    per-row form has to exist because a quarantine record needs the position of the row
    that failed, and a frame-level check can only say that *something* in the batch did.

    Null is first and separate on purpose: a blank line at EOF arrives as an all-null row,
    and "this corpus has a hole in a required column" is the true, actionable answer,
    where every pattern check would also (uselessly) fire. Each condition carries its own
    ``fill_null``, because "a null should count as a failure" is true of a shape check and
    false of a comparison against an exponent that an unmapped currency does not have.
    """
    any_null = pl.any_horizontal(*(pl.col(name).is_null() for name in IBMAML_POSITIONAL_COLUMNS))
    conditions: tuple[tuple[str, pl.Expr], ...] = (
        ("null_in_required_column", any_null.fill_null(False)),
        (
            "timestamp_format",
            (~pl.col("ts").str.contains(IBMAML_TIMESTAMP_PATTERN, literal=False)).fill_null(True),
        ),
        (
            "timestamp_out_of_range",
            (~_real_minute_expr().is_not_null()).fill_null(False),
        ),
        (
            "amount_format",
            (~_money_shape_expr("amount_received") | ~_money_shape_expr("amount_paid")).fill_null(
                True
            ),
        ),
        (
            "amount_scale",
            (
                _fraction_digits_expr(IBMAML_AMOUNT_PAID_COLUMN)
                > pl.col("payment_ccy").replace_strict(
                    MINOR_EXPONENT_BY_NAME, default=None, return_dtype=pl.Int64
                )
            ).fill_null(False),
        ),
        (
            "amount_overflow",
            (
                _whole_digits_expr(IBMAML_AMOUNT_PAID_COLUMN)
                > pl.col("payment_ccy").replace_strict(
                    _MAX_WHOLE_DIGITS_BY_NAME, default=None, return_dtype=pl.Int64
                )
            ).fill_null(False),
        ),
        ("label_not_binary", (~pl.col(IBMAML_LABEL_COLUMN).is_in(["0", "1"])).fill_null(True)),
        (
            "currency_shape",
            (
                ~pl.col("receiving_ccy").str.contains(IBMAML_CURRENCY_PATTERN, literal=False)
                | ~pl.col("payment_ccy").str.contains(IBMAML_CURRENCY_PATTERN, literal=False)
            ).fill_null(True),
        ),
        (
            "unknown_currency",
            (
                ~pl.col("receiving_ccy").is_in(list(CURRENCY_ISO))
                | ~pl.col("payment_ccy").is_in(list(CURRENCY_ISO))
            ).fill_null(True),
        ),
        (
            "unknown_payment_format",
            (~pl.col("payment_format").is_in(list(IBMAML_PAYMENT_FORMATS))).fill_null(True),
        ),
        (
            "account_identity_missing",
            (
                ~pl.col("from_bank").str.contains(IBMAML_BANK_CODE_PATTERN, literal=False)
                | ~pl.col("to_bank").str.contains(IBMAML_BANK_CODE_PATTERN, literal=False)
                | ~pl.col(IBMAML_POSITIONAL_COLUMNS[2]).str.contains(
                    IBMAML_ACCOUNT_CODE_PATTERN, literal=False
                )
                | ~pl.col(IBMAML_POSITIONAL_COLUMNS[4]).str.contains(
                    IBMAML_ACCOUNT_CODE_PATTERN, literal=False
                )
            ).fill_null(True),
        ),
    )
    reason: pl.Expr = pl.lit(None, dtype=pl.String)
    for code, condition in reversed(conditions):
        reason = pl.when(condition).then(pl.lit(code)).otherwise(reason)
    return frame.select(reason.alias("_reason")).to_series()


# --- money, time, identity ------------------------------------------------


def amount_minor_frame(frame: pl.DataFrame) -> pl.DataFrame:
    """Attach ``amount_minor`` and ``currency`` to a batch that cleared the contract.

    The value is first expressed in 1e-8 units by right-padding the fractional text to
    eight digits, then divided by the currency's exact power of ten, then added to the
    integer part scaled the same way. Nothing here touches a float: ``0.281983`` Bitcoin
    becomes 28,198,300 satoshi-units and ``14918.11`` Yen becomes 1,491,811, both exactly.

    ``parse_amount_minor`` from ``ingest/canonical.py`` is deliberately not used on this
    path: it is the shared scalar primitive and it is fixed at one hundred minor units per
    major unit, quantising anything finer with ROUND_HALF_EVEN. That is right for PaySim's
    two-decimal corpus and wrong here, where rounding a Bitcoin amount would be a silent
    repair in a money column. The scale question is answered upstream by the contract's
    ``amount_scale`` check, which refuses instead of rounding.
    """
    whole = pl.col(IBMAML_AMOUNT_PAID_COLUMN).str.split(".").list.get(0, null_on_oob=True)
    fraction = pl.col(IBMAML_AMOUNT_PAID_COLUMN).str.split(".").list.get(1, null_on_oob=True)
    micro_digits = fraction.str.slice(0, MICRO_EXPONENT).str.pad_end(MICRO_EXPONENT, "0")
    return frame.with_columns(
        whole.cast(pl.Int64).alias("_whole_int"),
        pl.when(fraction.is_null())
        .then(pl.lit("0" * MICRO_EXPONENT, dtype=pl.String))
        .otherwise(micro_digits)
        .cast(pl.Int64)
        .alias("_micro_units"),
        pl.col("payment_ccy").replace_strict(_POW10_BY_NAME, return_dtype=pl.Int64).alias("_pow10"),
        pl.col("payment_ccy")
        .replace_strict(_MICRO_DIVISOR_BY_NAME, return_dtype=pl.Int64)
        .alias("_divisor"),
        pl.col("payment_ccy")
        .replace_strict(CURRENCY_ISO, return_dtype=pl.String)
        .alias("currency"),
    ).with_columns(
        (pl.col("_whole_int") * pl.col("_pow10") + pl.col("_micro_units") // pl.col("_divisor"))
        .cast(pl.Int64)
        .alias("amount_minor")
    )


def event_timestamp_expr(policy: IBMAMLIngestPolicy) -> pl.Expr:
    """The corpus's naive minute, localised once, then expressed in UTC.

    ``replace_time_zone`` is the whole assumption: it attaches
    ``source_timezone_assumption`` to a naive clock reading, i.e. it claims the file's wall
    clock is that zone's local time. ``convert_time_zone`` then changes the label and not
    the instant. If the corpus was in fact recorded in UTC, every derived ``local_hour``
    and ``event_date_local`` is three hours off — which is why the assumption is declared
    in ``config/pipeline.yaml`` rather than embedded here.
    """
    normalised = pl.col("ts").str.strip_chars().str.replace_all("/", "-")
    naive = normalised.str.strptime(pl.Datetime("us"), policy.parse_format)
    return (
        naive.dt.replace_time_zone(policy.source_timezone_name)
        .dt.convert_time_zone("UTC")
        .alias("event_ts_utc")
    )


def row_keys(frame: pl.DataFrame) -> tuple[list[str], list[str]]:
    """``(id_keys, content_keys)`` for a batch, in file order.

    Built in one vectorised ``concat_str`` and then handed to Python, because the digest
    is a per-row hash. ``id_keys`` carry the zero-padded ordinal and the separator, so the
    id builder can split them without re-deriving anything; ``content_keys`` are the
    batch's content, which is what a content-derived batch id must be derived from.
    """
    content = pl.concat_str(
        [pl.col(name).cast(pl.String) for name in IBMAML_POSITIONAL_COLUMNS],
        separator=KEY_SEPARATOR,
    ).alias(ROW_KEY_COLUMN)
    keyed = frame.with_columns(content).select(
        ROW_KEY_COLUMN,
        pl.concat_str(
            [
                pl.col(ORDINAL_COLUMN).cast(pl.String).str.pad_start(TXN_ID_ORDINAL_WIDTH, "0"),
                pl.lit(KEY_SEPARATOR),
                pl.col(ROW_KEY_COLUMN),
            ],
            separator="",
        ).alias("_id_key"),
    )
    return (
        [str(value) for value in keyed.get_column("_id_key").to_list()],
        [str(value) for value in keyed.get_column(ROW_KEY_COLUMN).to_list()],
    )


def _txn_id_chunk(keys: list[str]) -> list[object]:
    """Worker body: the namespaced id for a contiguous slice of row keys.

    Module-level and taking one plain list argument because it is handed to a
    ``ProcessPoolExecutor`` — a lambda is not picklable, and a ``partial`` of a
    two-argument function is what mypy's ``Callable`` check rejects, so the namespace is
    read from the module constant instead. That is the same value either way: it is a
    corpus identity, not a parameter. The ``list[object]`` return is the callback shape
    ``canonical.map_in_chunks`` declares; the caller narrows every element back to ``str``.

    The id is keyed by the namespace rather than by the run salt because ``txn_id`` is not
    personal data: it must survive a salt rotation, so that re-running an ingest with a
    fresh salt changes every account key (03 L's erasure property) without re-labelling
    five million transactions.
    """
    out: list[object] = []
    for ordinal, _separator, content in (key.partition(KEY_SEPARATOR) for key in keys):
        digest = hmac_sha256_hex(content, TXN_ID_NAMESPACE)[:TXN_ID_DIGEST_HEX]
        out.append(f"{TXN_ID_NAMESPACE}:{ordinal}:{digest}")
    return out


def txn_ids_for_keys(keys: Sequence[str], *, pool: ProcessPoolExecutor | None = None) -> list[str]:
    """Namespaced ordinal-plus-digest ids for a sequence of row keys, order preserved."""
    results = map_in_chunks(_txn_id_chunk, list(keys), pool=pool)
    return [str(item) for item in results]


def window_bounds(events: pl.DataFrame) -> tuple[datetime | None, datetime | None]:
    """The first and last instant of a canonical frame, typed as the contract declares it.

    ``Series.min()`` is annotated as a union over every column type polars can hold, which
    is true of the library and false of this frame: canonical v1 declares
    ``event_ts_utc`` as ``Datetime('us', UTC)``. The dtype is checked rather than assumed,
    so the narrowing is enforced at runtime instead of being a cast over a hope. Reading
    the bounds this way also avoids materialising five million Python datetimes, which the
    ``list(column)`` form this replaces did per batch.
    """
    if events.height == 0:
        return None, None
    column = events.get_column("event_ts_utc")
    if column.dtype != CANONICAL_EVENT_DTYPES["event_ts_utc"]:
        raise CanonicalizationError(
            f"event_ts_utc is {column.dtype}, not the declared "
            f"{CANONICAL_EVENT_DTYPES['event_ts_utc']}; a window cannot be read off it"
        )
    minimum = column.min()
    maximum = column.max()
    if not isinstance(minimum, datetime) or not isinstance(maximum, datetime):
        raise CanonicalizationError(
            f"event_ts_utc bounds came back as {type(minimum).__name__}/"
            f"{type(maximum).__name__}, expected datetime"
        )
    return minimum, maximum


def account_pair_columns(
    frame: pl.DataFrame,
    identity: RunIdentity,
    *,
    pool: ProcessPoolExecutor | None = None,
) -> pl.DataFrame:
    """Salted endpoint keys for a batch, hashing each distinct (bank, account) pair once.

    Same distinct-key-then-join shape PaySim's adapter uses. The message is
    ``"<bank>|<account>"`` rather than the bare account, because the account number alone
    is not an identity in this corpus: four numbers recur under two banks in this file and
    ``HI-Small_accounts.csv`` keys an entity on the pair.
    """
    with_pairs = frame.with_columns(
        pl.concat_str(
            [pl.col("from_bank"), pl.lit("|"), pl.col("from_account")], separator=""
        ).alias("_source_pair"),
        pl.concat_str([pl.col("to_bank"), pl.lit("|"), pl.col("to_account")], separator="").alias(
            "_dest_pair"
        ),
    )
    distinct = (
        pl.concat([with_pairs.get_column("_source_pair"), with_pairs.get_column("_dest_pair")])
        .unique(maintain_order=True)
        .to_list()
    )
    pairs = [str(pair) for pair in distinct]
    keys = account_keys_for_names(pairs, identity.run_salt, pool=pool)
    lookup = pl.DataFrame({"pair": pairs, "key": keys})
    joined = with_pairs.join(lookup, left_on="_source_pair", right_on="pair", how="left").rename(
        {"key": "account_from"}
    )
    joined = joined.join(lookup, left_on="_dest_pair", right_on="pair", how="left").rename(
        {"key": "account_to"}
    )
    return joined.drop("_source_pair", "_dest_pair")


# --- canonicalisation -----------------------------------------------------


def canonicalize_batch(
    frame: pl.DataFrame,
    identity: RunIdentity,
    *,
    run_id: str,
    policy: IBMAMLIngestPolicy,
    deployment_tz: ZoneInfo,
    typology: pl.DataFrame,
    batch_ordinal: int = 0,
    ingested_at: datetime | None = None,
    future_tolerance_hours: int = DEFAULT_FUTURE_TOLERANCE_HOURS,
    line_offset: int = 0,
    ordinal_base: int = 0,
    pool: ProcessPoolExecutor | None = None,
) -> IngestResult:
    """Turn one raw batch into canonical events, vectorised.

    The order of operations is not arbitrary:

    1. structural gate, so a wrong-shaped file costs one record and not 100k;
    2. the file-relative ordinal, before any row is dropped — the ordinal is defined by
       the timestamp-filtered scan, so a row that fails some *other* check but carries a
       valid minute must still consume its position, or every typology join after it
       shifts by one;
    3. the row-scoped contract mask, before any identity is computed, so a quarantined row
       never consumes a ``txn_id`` that would then have to be un-issued;
    4. the strict contract re-run as the authority over the survivors, so a mask that
       disagrees with the schema fails the run instead of quietly shipping rows;
    5. money, identities, timestamps and the typology join over what survived.

    ``batch_id`` is derived from the batch's own content (``batch_id_for_rows``) rather
    than from ``uuid4()``, because a random id inside the canonical columns makes two runs
    of one corpus produce different bytes and ``make verify-determinism`` becomes
    unachievable by construction (DEV-012).
    """
    if frame.height == 0:
        raise CanonicalizationError(
            "canonicalize_batch received an empty batch; config sets "
            "ingest.allow_empty_batch: false, so a batch that reads zero rows is an error "
            "and not a clean run"
        )
    rows_read = frame.height
    struct = _structural_failure(frame, line_offset=line_offset)
    if struct is not None:
        return IngestResult(
            events=pl.DataFrame(schema=CANONICAL_EVENT_DTYPES),
            quarantined=[struct],
            rows_read=rows_read,
            batch_id=identity.batch_id,
        )

    # --- 2: the ordinal, over the whole batch, before anything is dropped.
    in_scan = frame.get_column("ts").str.contains(IBMAML_ORDINAL_FILTER_PATTERN, literal=False)
    ordinals = in_scan.cast(pl.Int64).cum_sum() - 1 + ordinal_base
    working = frame.with_row_index(LINE_COLUMN, offset=line_offset).with_columns(
        pl.when(in_scan)
        .then(ordinals)
        .otherwise(pl.lit(None, dtype=pl.Int64))
        .cast(pl.Int64)
        .alias(ORDINAL_COLUMN)
    )

    # --- 3: row-scoped contract failures.
    working = working.with_columns(_reason=_row_reason(working))
    rejected = working.filter(pl.col("_reason").is_not_null())
    quarantined: list[QuarantineRecord] = [
        QuarantineRecord(
            reason=str(row["_reason"]),
            source_dataset=IBMAML_SOURCE_NAME,
            row_index=int(row[LINE_COLUMN]),
            payload={name: row[name] for name in IBMAML_POSITIONAL_COLUMNS},
            detail=(
                f"row violates the declared raw_ibm_aml contract check {row['_reason']!r}; "
                "quarantined with the original row, not repaired and not dropped"
            ),
        )
        for row in rejected.to_dicts()
    ]
    survivors = working.filter(pl.col("_reason").is_null()).drop("_reason")

    if survivors.height == 0:
        return IngestResult(
            events=pl.DataFrame(schema=CANONICAL_EVENT_DTYPES),
            quarantined=quarantined,
            rows_read=rows_read,
            batch_id=identity.batch_id,
        )

    # --- 4: the contract is the authority over what the mask let through.
    try:
        raw_ibm_aml_schema.validate(survivors.select(list(IBMAML_POSITIONAL_COLUMNS)))
    except Exception as exc:  # pandera raises several SchemaError shapes
        raise CanonicalizationError(
            "the row-level mask and the declared contract disagree: the mask kept "
            f"{survivors.height} row(s) of this batch and raw_ibm_aml_schema refused them "
            f"({_reason_from_schema_error(exc)}). {str(exc).splitlines()[0][:300]}"
        ) from exc

    # --- 5: identity, money, time, typology.
    id_keys, content_keys = row_keys(survivors)
    batch_id = batch_id_for_rows(IBMAML_SOURCE_NAME, batch_ordinal, content_keys)
    txn_ids = txn_ids_for_keys(id_keys, pool=pool)

    events = account_pair_columns(survivors, identity, pool=pool).with_columns(
        pl.Series("txn_id", txn_ids, dtype=pl.String),
        pl.lit(batch_id).cast(pl.String).alias("batch_id"),
        pl.lit(run_id).cast(pl.String).alias("run_id"),
        pl.lit(IBMAML_SOURCE_NAME).cast(pl.String).alias("source_dataset"),
        # No per-account before/after balance pair exists in this corpus, so all four
        # columns stay null instead of being filled with one-sided values or zeros. The
        # exposure proxy therefore differs per corpus: PaySim can read balances and this
        # one cannot, which is why exposure is reported per corpus (DEV-014).
        pl.lit(None, dtype=pl.Int64).alias("src_balance_before_minor"),
        pl.lit(None, dtype=pl.Int64).alias("src_balance_after_minor"),
        pl.lit(None, dtype=pl.Int64).alias("dst_balance_before_minor"),
        pl.lit(None, dtype=pl.Int64).alias("dst_balance_after_minor"),
        # No threshold-based flag exists here. Canonical v1 requires the column non-null,
        # so 0 records "no equivalent in this source", NOT "checked and cleared".
        pl.lit(0, dtype=pl.Int8).alias("label_is_flagged"),
        pl.col(IBMAML_LABEL_COLUMN).cast(pl.Int8).alias("label_is_fraud"),
        # `channel` carries the corpus's own spelling verbatim; `txn_type` carries the
        # stable upper-case token the rules and bins match on. One mapping, stated here and
        # in the contract, and no invented rail taxonomy.
        pl.col("payment_format").cast(pl.String).alias("channel"),
        pl.col("payment_format")
        .replace_strict(TXN_TYPE_BY_PAYMENT_FORMAT, return_dtype=pl.String)
        .alias("txn_type"),
        event_timestamp_expr(policy),
    )
    events = amount_minor_frame(events)

    events = events.join(typology, on=ORDINAL_COLUMN, how="left")
    mislabelled = events.filter(
        pl.col("label_typology").is_not_null() & (pl.col(IBMAML_LABEL_COLUMN) != "1")
    )
    if mislabelled.height:
        examples = [
            (row[ORDINAL_COLUMN], row["label_typology"], row[IBMAML_LABEL_COLUMN])
            for row in mislabelled.head(5).to_dicts()
        ]
        raise TypologyAlignmentError(
            f"{mislabelled.height} annotated row(s) in batch {batch_ordinal} do not carry "
            "Is Laundering == 1, so the row ordinals this adapter computes no longer line "
            "up with data/processed/ibm_typologies.parquet. The join is keyed on the "
            "timestamp-filtered scan order scripts/build_ibm_typologies.py used; example "
            f"(ordinal, typology, is_laundering): {examples}. Failing rather than shipping "
            "a typology column that is confidently wrong."
        )

    zone = resolve_local_timezone(deployment_tz)
    events = events.with_columns(
        local_hour_expr("event_ts_utc", zone).alias("local_hour"),
        local_date_expr("event_ts_utc", zone).alias("event_date_local"),
    )

    reference = ingested_at if ingested_at is not None else datetime.now(UTC)
    events = events.with_columns(
        pl.lit(reference).cast(CANONICAL_EVENT_DTYPES["ingested_at"]).alias("ingested_at")
    )
    horizon = reference + timedelta(hours=future_tolerance_hours)
    beyond = events.get_column("event_ts_utc") > horizon
    if bool(beyond.any()):
        for row in events.filter(beyond).to_dicts():
            quarantined.append(
                QuarantineRecord(
                    reason="future_timestamp",
                    source_dataset=IBMAML_SOURCE_NAME,
                    row_index=int(row[LINE_COLUMN]),
                    payload={
                        "txn_id": row["txn_id"],
                        "ts": row["ts"],
                        "event_ts_utc": str(row["event_ts_utc"]),
                    },
                    detail=(
                        f"event_ts_utc {row['event_ts_utc']} is beyond the "
                        f"{future_tolerance_hours}h tolerance from the run reference "
                        f"{horizon.isoformat()}; with the source timezone assumption in "
                        "config/pipeline.yaml this row is dated in the future, which is a "
                        "clock or assumption problem rather than a transaction"
                    ),
                )
            )
        events = events.filter(~beyond)

    events = events.select(CANONICAL_P1B_COLUMNS).sort(["event_ts_utc", "txn_id"])
    return IngestResult(
        events=events,
        quarantined=quarantined,
        rows_read=rows_read,
        batch_id=batch_id,
        window_start=window_bounds(events)[0],
        window_end=window_bounds(events)[1],
    )


def next_ordinal_base(frame: pl.DataFrame, ordinal_base: int) -> int:
    """Ordinal base for the batch after ``frame``; exported so the rule is testable."""
    return ordinal_base + count_in_scan_rows(frame)


# --- ingest ---------------------------------------------------------------


def validate_run_id(run_id: str) -> str:
    """Fail loud unless ``run_id`` is a ULID (DEV-003).

    The alphabet check itself lives in ``oxbow.identity`` and is called rather than
    reimplemented, because a second definition of "what a run id is" is how the ingest and
    the API start disagreeing about the same value. Re-raised as ``CanonicalizationError``
    so this boundary has one error type.
    """
    if not is_ulid(run_id):
        raise CanonicalizationError(
            f"run_id {run_id!r} is not a 26-character Crockford base32 ULID (DEV-003). Run "
            "ids are ordered lexicographically so a stream can resume where it stopped; a "
            "non-ULID breaks that."
        )
    return run_id


def ingest_ibm_aml(
    path: Path,
    identity: RunIdentity,
    *,
    run_id: str,
    deployment_tz: ZoneInfo,
    policy: IBMAMLIngestPolicy,
    typology: pl.DataFrame | None = None,
    batch_rows: int = 100_000,
    limit: int | None = None,
    future_tolerance_hours: int = DEFAULT_FUTURE_TOLERANCE_HOURS,
    ingested_at: datetime | None = None,
    use_process_pool: bool = True,
) -> IngestResult:
    """Ingest an IBM-AML CSV, concatenating canonical events across batches.

    ``ingest_paysim``'s shape on purpose, so the CLI treats both corpora identically.
    Three differences are substantive rather than cosmetic: there is no step-expansion
    modulus, because the corpus carries its own minutes; ``typology`` may be supplied by a
    caller that already loaded the join artifact; and rows the corpus repeats are kept
    rather than deduplicated, because an identical row here is a second real transaction
    (measured: nine such rows file-wide) rather than a re-delivery.

    A fixed ``ingested_at`` is what makes byte-identical artifacts achievable, which is
    what ``make verify-determinism`` is: it is stamped once per batch, not per row.
    """
    validate_run_id(run_id)

    # The header is checked before a single row is parsed, because a reordered or renamed
    # file otherwise fails as a dtype error ("could not parse 'Cheque' as Int64") and the
    # operator spends the afternoon on the wrong problem instead of reading that the
    # declared header does not match the file. It also catches the specific hazard of this
    # corpus: a file that swapped the two ``Account`` columns would parse perfectly.
    header = read_header(path)
    if header != list(IBMAML_RAW_HEADER):
        return IngestResult(
            events=pl.DataFrame(schema=CANONICAL_EVENT_DTYPES),
            quarantined=[
                QuarantineRecord(
                    reason="schema_mismatch",
                    source_dataset=IBMAML_SOURCE_NAME,
                    row_index=0,
                    payload={"columns": header},
                    detail=(
                        "header does not match the declared IBM-AML contract; expected "
                        f"{list(IBMAML_RAW_HEADER)}, got {header}. Note that this corpus "
                        "names column 3 and column 5 both `Account` (sender, then "
                        "receiver), which is why the header is asserted before the "
                        "positional read rather than resolved by name."
                    ),
                )
            ],
            batch_id=identity.batch_id,
        )

    join = read_typology_artifact(policy.typology_artifact) if typology is None else typology
    batches = read_raw_batches(path, batch_rows=batch_rows, limit=limit)
    if not batches:
        return IngestResult(
            events=pl.DataFrame(schema=CANONICAL_EVENT_DTYPES),
            quarantined=[
                QuarantineRecord(
                    reason="empty_batch",
                    source_dataset=IBMAML_SOURCE_NAME,
                    row_index=0,
                    payload={"path": str(path)},
                    detail=(
                        "no data rows in the file; a zero-row source is an error, not a "
                        "success (config ingest.allow_empty_batch)"
                    ),
                )
            ],
            batch_id=identity.batch_id,
        )

    combined: list[pl.DataFrame] = []
    quarantined: list[QuarantineRecord] = []
    rows_read = 0
    ordinal_base = 0
    pool: ProcessPoolExecutor | None = None
    if use_process_pool:
        workers = hash_workers()
        if workers > 1:
            try:
                pool = ProcessPoolExecutor(max_workers=workers)
            except Exception:
                pool = None
    try:
        for ordinal, frame in enumerate(batches):
            result = canonicalize_batch(
                frame,
                identity,
                run_id=run_id,
                policy=policy,
                deployment_tz=deployment_tz,
                typology=join,
                batch_ordinal=ordinal,
                ingested_at=ingested_at,
                future_tolerance_hours=future_tolerance_hours,
                line_offset=rows_read,
                ordinal_base=ordinal_base,
                pool=pool,
            )
            if result.events.height:
                combined.append(result.events)
            quarantined.extend(result.quarantined)
            rows_read += result.rows_read
            ordinal_base = next_ordinal_base(frame, ordinal_base)
    finally:
        if pool is not None:
            pool.shutdown()

    events = (
        pl.concat(combined, how="vertical").sort(["event_ts_utc", "txn_id"])
        if combined
        else pl.DataFrame(schema=CANONICAL_EVENT_DTYPES)
    )
    # A per-batch sort is not a global one. Each batch arrives ordered, but the batches
    # concatenate in file order over a corpus whose timestamps are not monotonic in the
    # file, so the aggregate is re-sorted here: the total order is what every downstream
    # sort claims to be, and re-sorting at the boundary is what makes the emitted frame
    # independent of ``batch_rows`` rather than only of the seed.
    return IngestResult(
        events=events,
        quarantined=quarantined,
        rows_read=rows_read,
        batch_id=identity.batch_id,
        window_start=window_bounds(events)[0],
        window_end=window_bounds(events)[1],
    )


# --- adapter --------------------------------------------------------------


def _stream_file(path: Path) -> tuple[str, int]:
    """SHA-256 of the bytes as they stream, plus the number of data rows.

    Hashing what is on disk rather than trusting a recorded number is the whole pin: wrong
    bytes entering the pipeline would make every downstream figure a lie that still looks
    plausible. Read in 1 MiB blocks, because a corpus of this size is never held in memory.

    The row count is byte-level, and the final byte is what makes it exact: a file whose
    last line has no terminator has one fewer newline than it has lines, and reading that
    as a truncated file would reject a perfectly complete corpus over a formatting
    accident.
    """
    digest = hashlib.sha256()
    newlines = 0
    last_byte = b""
    with path.open("rb") as handle:
        while block := handle.read(STREAM_CHUNK):
            digest.update(block)
            newlines += block.count(b"\n")
            last_byte = block[-1:]
    data_rows = newlines - 1 if last_byte == b"\n" else newlines
    return digest.hexdigest(), max(data_rows, 0)


def assert_row_satisfies_boundary_guards(row: Mapping[str, Any]) -> None:
    """Apply the port's runtime money guard to one canonical row.

    ``assert_no_float_money`` from ``ports/source.py`` is the port's own check and is
    called rather than reimplemented. ``assert_canonical_row`` is NOT called here: that
    function still validates the pre-P1b field names (``ts_utc``, ``src_account``,
    ``label_fraud``) while the canonical contract this adapter writes uses
    ``event_ts_utc``, ``account_from`` and ``label_is_fraud``. The port module is not owned
    by this phase, so the disagreement is reported rather than papered over; the projection
    check below enforces the P1b names in the meantime.
    """
    missing = [column for column in CANONICAL_P1B_COLUMNS if column not in row]
    if missing:
        raise CanonicalizationError(f"canonical row is missing required fields: {missing}")
    assert_no_float_money(dict(row))


class IBMAMLAdapter:
    """The ``SourceAdapter`` implementation for IBM-AML ``HI-Small_Trans.csv``.

    Behaviourally interchangeable with the PaySim adapter and the null adapters: same
    port, same manifest discipline, same quarantine contract, and the same rule that a
    batch is accepted whole or rejected whole (02 D, 03 B).
    """

    def __init__(
        self,
        path: Path,
        identity: RunIdentity,
        *,
        run_id: str,
        deployment_tz: ZoneInfo,
        policy: IBMAMLIngestPolicy,
        typology: pl.DataFrame | None = None,
        expected_sha256: str | None = None,
        batch_rows: int = 100_000,
        limit: int | None = None,
        future_tolerance_hours: int = DEFAULT_FUTURE_TOLERANCE_HOURS,
        ingested_at: datetime | None = None,
    ) -> None:
        self._path = path
        self._identity = identity
        self._run_id = validate_run_id(run_id)
        self._deployment_tz = deployment_tz
        self._policy = policy
        self._typology = typology
        self._expected_sha256 = expected_sha256
        self._batch_rows = batch_rows
        self._limit = limit
        self._future_tolerance_hours = future_tolerance_hours
        self._ingested_at = ingested_at
        self._result: IngestResult | None = None
        self._scan: tuple[str, int] | None = None
        self._external_quarantine: list[PortQuarantineRecord] = []

    @property
    def source_id(self) -> str:
        """The declared source id from ``config/sources.yaml``."""
        return IBMAML_SOURCE_NAME

    def ingest(self) -> IngestResult:
        """Run the ingest once and cache it, so the port may be queried repeatedly."""
        if self._result is None:
            self._result = ingest_ibm_aml(
                self._path,
                self._identity,
                run_id=self._run_id,
                deployment_tz=self._deployment_tz,
                policy=self._policy,
                typology=self._typology,
                batch_rows=self._batch_rows,
                limit=self._limit,
                future_tolerance_hours=self._future_tolerance_hours,
                ingested_at=self._ingested_at,
            )
        return self._result

    def file_digest(self) -> tuple[str, int]:
        """The computed SHA-256 and data-row count of the file as it sits on disk."""
        if self._scan is None:
            self._scan = _stream_file(self._path)
        return self._scan

    def read_manifest(self) -> BatchManifest:
        """The batch's proof of completeness, or a rejection of the whole batch.

        ``sha256`` is computed from the bytes on disk, never copied from a record. When
        ``config/sources.yaml`` carries a real pin it must agree, and a mismatch rejects
        the batch; while a pin is still ``RECORDED_AT_DOWNLOAD`` there is nothing to
        compare against and the computed digest is what the operator records.
        ``row_count`` is cross-checked against the file's own newline count whenever no
        ``limit`` was set, so a truncated file cannot be mistaken for a quiet period.

        Over the real corpus this is a one-minute pass over 475 MB, which is the price of
        a manifest that means something; a smoke run with ``limit`` set skips the
        row-count cross-check but still pays the hash.
        """
        result = self.ingest()
        sha256, declared_rows = self.file_digest()

        pin = self._expected_sha256
        if isinstance(pin, str) and pin != RECORDED_AT_DOWNLOAD and pin.lower() != sha256:
            raise BatchIntegrityError(
                f"{self._path.name}: SHA-256 disagrees with the recorded pin.\n"
                f"  recorded {pin}\n  actual   {sha256}\n"
                "The batch is rejected whole. Either upstream changed or the transfer was "
                "corrupted; either way the recorded row counts and base rates are "
                "untrustworthy."
            )

        if self._limit is None and result.rows_read and result.rows_read != declared_rows:
            raise BatchIntegrityError(
                f"{self._path.name}: read {result.rows_read} data rows but the file holds "
                f"{declared_rows} by byte-level line count. Either the file is truncated or "
                "a field carries an embedded newline; this corpus has no free-text field, "
                "so the batch never enters as a partial one."
            )

        if result.events.height == 0:
            reasons = sorted({record.reason for record in result.quarantined})
            raise BatchIntegrityError(
                f"{self._path.name}: no canonical events were produced, so the batch has no "
                f"window to declare. {result.quarantine_count} record(s) quarantined: {reasons}."
            )

        ordered = result.events.sort(["event_ts_utc", "txn_id"])
        # Read as a list of the column's declared dtype rather than through polars'
        # ``min()``/``max()``, whose return is annotated as a union over every possible
        # column type and would hand a ``list[Any]`` to a datetime field.
        instants: list[datetime] = list(ordered["event_ts_utc"])
        if not instants:
            raise BatchIntegrityError(f"{self._path.name}: window came back empty")
        manifest = BatchManifest(
            batch_id=self._identity.batch_id,
            row_count=result.rows_read,
            sha256=sha256,
            window_start=min(instants),
            window_end=max(instants),
            source_system=IBMAML_SOURCE_NAME,
        )
        manifest.validate()
        return manifest

    def iter_canonical(self) -> Iterator[dict[str, Any]]:
        """Canonical rows in the total order ``(event_ts_utc, txn_id)``, or raise.

        No coercion, no repair, no silent drop: whatever this yields already cleared the
        strict contract, and everything it could not map is counted by
        :meth:`quarantine_count`. The total order is the one plan §7 fixes for every sort
        in the codebase, applied at the boundary so downstream stages cannot disagree.
        """
        for row in self.ingest().events.sort(["event_ts_utc", "txn_id"]).iter_rows(named=True):
            assert_row_satisfies_boundary_guards(row)
            yield dict(row)

    def quarantine(self, record: PortQuarantineRecord) -> None:
        """Record an unmappable row raised outside the ingest loop."""
        self._external_quarantine.append(record)

    def quarantine_count(self) -> int:
        """Rows this adapter could not map. Surfaced in the UI, with reasons."""
        ingested = self._result.quarantine_count if self._result is not None else 0
        return ingested + len(self._external_quarantine)

    @property
    def quarantine_records(self) -> tuple[QuarantineRecord, ...]:
        """The ingest-loop records and their reason codes, for the stage ledger."""
        if self._result is None:
            return ()
        return tuple(self._result.quarantined)

    def canonical_columns(self) -> tuple[str, ...]:
        """Fail loud unless the written frame carries the canonical contract exactly."""
        columns = tuple(self.ingest().events.columns)
        if columns != CANONICAL_P1B_COLUMNS:
            raise CanonicalizationError(
                f"canonical projection disagrees with the contract: wrote {list(columns)}"
            )
        return columns


__all__ = [
    "BALANCE_COLUMNS",
    "CANONICAL_EVENT_DTYPES",
    "CANONICAL_P1B_COLUMNS",
    "IBMAML_SOURCE_NAME",
    "KEY_SEPARATOR",
    "LINE_COLUMN",
    "MICRO_EXPONENT",
    "ORDINAL_COLUMN",
    "RECORDED_AT_DOWNLOAD",
    "ROW_KEY_COLUMN",
    "TXN_ID_DIGEST_HEX",
    "TXN_ID_NAMESPACE",
    "TXN_ID_ORDINAL_WIDTH",
    "BatchIntegrityError",
    "IBMAMLAdapter",
    "IBMAMLIngestPolicy",
    "TypologyAlignmentError",
    "account_pair_columns",
    "amount_minor_frame",
    "assert_row_satisfies_boundary_guards",
    "canonicalize_batch",
    "count_in_scan_rows",
    "event_timestamp_expr",
    "ingest_ibm_aml",
    "next_ordinal_base",
    "policy_from_config",
    "read_header",
    "read_raw_batches",
    "read_typology_artifact",
    "row_keys",
    "schema_error_codes",
    "txn_ids_for_keys",
    "validate_run_id",
]
