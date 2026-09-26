"""PaySim adapter: 6.36M raw CSV rows to validated canonical events, vectorised.

This is the ``SourceAdapter`` implementation for the primary Module-A corpus. Its
contract, in one sentence: it yields canonical-valid events or it raises. It does not
repair malformed rows, does not coerce them, and does not drop them quietly.

WHY THE SHAPE OF THIS FILE CHANGED. The previous revision walked every row with
``iter_rows(named=True)`` and built a Python dict per row, which put the full corpus
at roughly twenty minutes and made ``make ingest`` unusable as a gate command — a gate
nobody runs cannot protect anything. Everything expensive is now either a Polars
expression evaluated over a whole batch, or one Python pass over a batch's *distinct*
key set with the result joined back:

  * account keys: the 200k name slots in a 100k-row batch collapse to roughly 130k
    distinct values, hashed once instead of 12.7M times per slot.
  * transaction ids and intra-step offsets: one pass over the batch's distinct row
    keys. Both are functions of row content only, never of row position, so a row gets
    the same ``txn_id`` in batch 1 and in batch 40 and the output does not depend on
    where the chunk boundaries fall.
  * money and balances: string expressions. No float is ever multiplied by 100.

Deduplication was not implemented at all before. An identical re-delivery is now
dropped and counted, and a row that arrives twice with the same identity but a
different payload is quarantined rather than guessed between, because two conflicting
versions of one transaction is a source problem a human has to see.

The split in ``ingest`` is not negotiable — ``allow_silent_coercion: false`` in config
means any coercion quarantines the row, and a green run requires the quarantine count
to be zero. A batch that lost rows without recording why would be indistinguishable,
downstream, from a batch that legitimately had none.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import polars as pl

from oxbow.contracts.canonical_v1 import (
    ERR_DTYPE_MISMATCH,
    ERR_MISSING_COLUMN,
    ERR_NEGATIVE_MONEY,
    ERR_NULL_IN_REQUIRED,
    ERR_UNKNOWN_COLUMN,
    PERSISTED_CANONICAL_COLUMNS,
    empty_canonical_frame,
)
from oxbow.contracts.raw_paysim import (
    PAYSIM_RAW_COLUMNS,
    PAYSIM_TXN_TYPES,
    raw_paysim_schema,
)
from oxbow.ingest.canonical import (
    DECIMAL_2DP_PATTERN,
    PAYSIM_CURRENCY,
    SCIENTIFIC_DECIMAL_PATTERN,
    UTC_MICROS,
    CanonicalizationError,
    RunIdentity,
    account_keys_for_names,
    amount_minor_to_text,
    batch_id_for_rows,
    hash_workers,
    local_date_expr,
    local_hour_expr,
    minor_units_expr,
    minor_units_from_decimal_text,
    resolve_local_timezone,
    txn_ids_and_offsets,
)

PAYSIM_SOURCE_NAME: Final = "paysim"

# 03 B: reject anything past the batch window end plus this tolerance.
DEFAULT_FUTURE_TOLERANCE_HOURS: Final = 6

# The columns that *identify* a PaySim transaction. ``txn_id`` is an HMAC over these
# and nothing else, which is what makes a conflicting duplicate expressible at all:
# two rows sharing these five values are the same transaction, so a disagreement in any
# other column is a dispute about that transaction rather than a second transaction.
# Balances and labels are excluded on purpose — they are the payload a duplicate would
# disagree about.
ROW_IDENTITY_COLUMNS: Final[tuple[str, ...]] = (
    "step",
    "type",
    "amount",
    "nameOrig",
    "nameDest",
)

ROW_PAYLOAD_COLUMNS: Final[tuple[str, ...]] = tuple(
    column for column in PAYSIM_RAW_COLUMNS if column not in ROW_IDENTITY_COLUMNS
)

# PaySim has no channel column, but ``channel`` is canonical and downstream features
# and rules need the rail distinction (R10 FAST_CASH_OUT is about money leaving the app
# for physical cash). It is therefore derived from ``type``, and derived by a stated
# rule rather than tuned per run: CASH_IN and CASH_OUT are the legs that touch an
# agent's physical float, and TRANSFER, PAYMENT and DEBIT never leave the app. A type
# outside the five is already failed by the contract before this mapping is reached, so
# the mapping cannot silently invent a rail for an unknown product.
PAYSIM_AGENT_RAIL_TYPES: Final[tuple[str, ...]] = ("CASH_IN", "CASH_OUT")
PAYSIM_APP_RAIL_TYPES: Final[tuple[str, ...]] = ("TRANSFER", "PAYMENT", "DEBIT")
PAYSIM_CHANNEL_AGENT: Final = "agent"
PAYSIM_CHANNEL_APP: Final = "app"

# Raw balance columns in canonical order: source before, source after, destination
# before, destination after. The pairing is positional and is asserted by
# ``test_zero_silent_coercions`` against a hand-built row, because a file whose balance
# columns are in a different order than the contract claims would otherwise silently
# swap in and out.
PAYSIM_BALANCE_COLUMNS: Final[tuple[str, ...]] = (
    "oldbalanceOrg",
    "newbalanceOrig",
    "oldbalanceDest",
    "newbalanceDest",
)

CANONICAL_BALANCE_TARGETS: Final[tuple[str, ...]] = (
    "src_balance_before_minor",
    "src_balance_after_minor",
    "dst_balance_before_minor",
    "dst_balance_after_minor",
)

# The declared raw dtypes, read off the contract object rather than restated here, so
# the structural gate and the schema cannot drift apart. pandera wraps a polars dtype
# in its own ``polars_engine.DataType`` and that wrapper never compares equal to the
# polars dtype a frame actually carries, so ``.type`` unwraps it before it is used as a
# gate; without the unwrap every batch fails closed with "declared Int64, got Int64".
RAW_DTYPES: Final[dict[str, pl.DataType]] = {
    name: raw_paysim_schema.columns[name].dtype.type for name in PAYSIM_RAW_COLUMNS
}

# Both amount encodings the corpus actually uses, as one pattern for the mask.
_AMOUNT_PATTERN: Final = f"^({DECIMAL_2DP_PATTERN[1:-1]}|(\\d+(\\.\\d+)?[Ee]\\+?\\d+))$"


@dataclass(slots=True)
class QuarantineRecord:
    """One rejected row, with the reason, kept rather than discarded.

    The reason code comes from the failing check, not from a stack trace, so a triage
    run can group by cause. A quarantine that only says "invalid" is an outage; one that
    says "amount_format, 412 rows" is a work item. ``row_index`` is the absolute offset
    in the source file, not the offset inside whichever chunk happened to carry it.
    """

    reason: str
    source_dataset: str
    row_index: int
    payload: dict[str, Any]
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "source_dataset": self.source_dataset,
            "row_index": self.row_index,
            "payload": self.payload,
            "detail": self.detail,
        }


@dataclass(slots=True)
class BatchOutcome:
    """What happened to one batch, as the manifest needs to know it.

    Carried separately from the concatenated events because the manifest is per batch:
    a single aggregate row count would hide the fact that batch 40 was empty while the
    run as a whole looked healthy, which is the specific failure 03 B says a manifest
    exists to prevent.
    """

    batch_id: str
    row_count: int
    canonical_count: int
    quarantine_count: int
    window_start: dt.datetime | None
    window_end: dt.datetime | None
    parquet_path: str = ""
    sha256: str = ""


@dataclass(slots=True)
class IngestResult:
    """Canonical events plus everything that failed to become one.

    ``events`` is the persisted 19-column canonical shape; DEV-012 keeps
    ``ingested_at`` and ``run_id`` in the manifest rather than in the bytes. The
    counters are separate from the quarantine list because a dropped identical
    duplicate is not a defect — it is the same transaction arriving twice — and
    conflating it with a rejection would make the rejection count a number nobody can
    act on.
    """

    events: pl.DataFrame
    quarantined: list[QuarantineRecord] = field(default_factory=list)
    rows_read: int = 0
    batch_id: str = ""
    duplicates_dropped: int = 0
    scientific_amounts_expanded: int = 0
    rounding_applied: int = 0
    window_start: dt.datetime | None = None
    window_end: dt.datetime | None = None
    batches: list[BatchOutcome] = field(default_factory=list)

    @property
    def quarantine_count(self) -> int:
        return len(self.quarantined)

    @property
    def canonical_count(self) -> int:
        return self.events.height

    def assert_no_quarantine(self) -> IngestResult:
        """Fail the run if anything was quarantined.

        The green path for a real corpus. Called by the CLI after ingest so a non-zero
        quarantine count stops the run rather than producing an artifact that looks
        complete.
        """
        if self.quarantined:
            reasons: dict[str, int] = {}
            for record in self.quarantined:
                reasons[record.reason] = reasons.get(record.reason, 0) + 1
            raise CanonicalizationError(
                f"{len(self.quarantined)} of {self.rows_read} rows were quarantined: "
                f"{reasons}. Fix the source or widen the contract deliberately; do "
                "not silence this."
            )
        return self


def read_raw_batches(
    path: Path,
    *,
    batch_rows: int = 100_000,
    limit: int | None = None,
) -> list[pl.DataFrame]:
    """Read the raw CSV into deterministic batches of exactly ``batch_rows``.

    The earlier revision used ``pl.read_csv_batched`` and asked it for chunks, which
    looked reasonable and was not reproducible: measured on this file, a request for
    one 100,000-row batch came back as three frames of 34,545 / 34,004 / 31,451 rows,
    because the reader hands out whatever its worker threads have finished parsing.
    Batch boundaries that move between runs move every batch id derived from them, so
    ``make verify-determinism`` could never pass. The file is therefore parsed once in
    full (measured 0.99 s for 6,362,620 rows, which is cheaper than the batching ever
    was) and sliced on exact row counts.

    ``amount`` is read as String deliberately. Left to inference it becomes Float64,
    and then the string-level check that rejects ``nan`` and ``inf`` can never run —
    the damage would already be done by the reader, before any contract existed to
    catch it. The balance columns are Float64 because that is what the file holds; they
    are converted through their decimal-string form, never by arithmetic.

    A file with no bytes returns no batches rather than leaking a polars ``NoDataError``:
    a truncated drop is exactly the case ``ingest.allow_empty_batch: false`` exists to
    turn into a named failure at the boundary, and the caller's message should say which
    source delivered nothing.
    """
    if not path.is_file():
        raise CanonicalizationError(f"{path} is not a file; there is no batch to read")
    if path.stat().st_size == 0:
        return []
    overrides = {col: pl.Float64 for col in PAYSIM_RAW_COLUMNS if "alance" in col} | {
        "step": pl.Int64,
        "isFraud": pl.Int64,
        "isFlaggedFraud": pl.Int64,
    }
    frame = pl.read_csv(
        path,
        n_rows=limit,
        infer_schema_length=0,
        schema_overrides=overrides,
    )
    if frame.height == 0:
        return []
    return [frame.slice(start, batch_rows) for start in range(0, frame.height, batch_rows)]


def _reason_from_schema_error(exc: Exception) -> str:
    """Map a pandera failure onto the failing check's own ``error=`` code.

    The codes are declared in ``contracts/raw_paysim.py`` and appear verbatim in the
    message, so a quarantine reason is the rule that failed rather than a stringified
    traceback. A triage run groups on this string; "schema_violation" is the fallback
    for a failure the contract does not name, and it is deliberately visible rather
    than guessed at.
    """
    text = str(exc)
    for code in (
        "amount_format",
        "self_transfer",
        "negative_balance",
        "label_not_binary",
        "unknown_txn_type",
    ):
        if code in text:
            return code
    if "not in DataFrameSchema" in text:
        return ERR_UNKNOWN_COLUMN
    if "is missing" in text or "column 'null" in text:
        return ERR_MISSING_COLUMN
    return "schema_violation"


def _structural_failure(frame: pl.DataFrame, *, row_offset: int) -> QuarantineRecord | None:
    """Whole-batch structural gate, recorded once rather than per row.

    A file with the wrong header is a source problem, not 100,000 row problems. The
    caller gets one record naming the offending column(s) instead of a quarantine table
    repeating the same sentence a hundred thousand times and burying the cause.
    """
    present = set(frame.columns)
    declared = set(PAYSIM_RAW_COLUMNS)
    if declared - present:
        return QuarantineRecord(
            reason=ERR_MISSING_COLUMN,
            source_dataset=PAYSIM_SOURCE_NAME,
            row_index=row_offset,
            payload={"columns": frame.columns},
            detail=(
                f"batch is missing required column(s) {sorted(declared - present)}; the "
                "declared PaySim contract cannot be evaluated against this file"
            ),
        )
    if present - declared:
        return QuarantineRecord(
            reason=ERR_UNKNOWN_COLUMN,
            source_dataset=PAYSIM_SOURCE_NAME,
            row_index=row_offset,
            payload={"columns": frame.columns},
            detail=(
                f"batch carries undeclared column(s) {sorted(present - declared)}; "
                "canonical v1 is strict, so an extra column fails closed and names itself"
            ),
        )
    actual = frame.collect_schema()
    mismatched = {
        name: f"declared {RAW_DTYPES[name]}, got {actual[name]}"
        for name in PAYSIM_RAW_COLUMNS
        if actual[name] != RAW_DTYPES[name]
    }
    if mismatched:
        return QuarantineRecord(
            reason=ERR_DTYPE_MISMATCH,
            source_dataset=PAYSIM_SOURCE_NAME,
            row_index=row_offset,
            payload={"columns": frame.columns},
            detail=(
                f"column dtype(s) disagree with the declared contract: {mismatched}. "
                "coerce=False, so this rejects the batch instead of casting the data"
            ),
        )
    nulls = frame.select([pl.col(name).null_count().alias(name) for name in PAYSIM_RAW_COLUMNS])
    offending = {
        name: int(nulls.get_column(name)[0]) for name in nulls.columns if int(nulls.get_column(name)[0])
    }
    if offending:
        return QuarantineRecord(
            reason=ERR_NULL_IN_REQUIRED,
            source_dataset=PAYSIM_SOURCE_NAME,
            row_index=row_offset,
            payload={"columns": frame.columns},
            detail=f"required column(s) carry nulls: {offending}",
        )
    return None


def _row_reason(frame: pl.DataFrame) -> pl.Series:
    """The first failing row-level contract check per row, else null.

    Mirrors the frame-level checks declared in ``contracts/raw_paysim.py`` as
    expressions, with the same reason codes. The per-row form has to exist because a
    quarantine record needs the absolute offset of the row that failed, and a
    frame-level check can only say that *something* in the batch did.

    One reason per row, in a fixed priority order, so counts by cause sum to the
    quarantine count instead of exceeding it when a row fails two checks.
    """
    conditions: tuple[tuple[str, pl.Expr], ...] = (
        ("amount_format", ~pl.col("amount").str.strip_chars().str.contains(_AMOUNT_PATTERN)),
        ("unknown_txn_type", ~pl.col("type").is_in(list(PAYSIM_TXN_TYPES))),
        (
            "label_not_binary",
            ~pl.col("isFraud").is_in([0, 1]) | ~pl.col("isFlaggedFraud").is_in([0, 1]),
        ),
        (
            ERR_NEGATIVE_MONEY,
            (pl.col("oldbalanceOrg") < 0)
            | (pl.col("newbalanceOrig") < 0)
            | (pl.col("oldbalanceDest") < 0)
            | (pl.col("newbalanceDest") < 0),
        ),
        ("self_transfer", pl.col("nameOrig") == pl.col("nameDest")),
    )
    reason: Any = pl.lit(None, dtype=pl.String)
    for code, condition in reversed(conditions):
        reason = pl.when(condition).then(pl.lit(code)).otherwise(reason)
    return frame.select(reason).to_series()


def normalise_money_texts(values: pl.Series) -> tuple[pl.Series, int, int]:
    """Expand non-plain decimal money text to its exact two-place decimal form.

    PaySim's CSV writer emitted float reprs for the largest TRANSFER amounts, so 5,650
    rows of 6,362,620 arrive as ``1.000191239E7`` (measured this session and recorded in
    the dataset card). ``Decimal`` expands that to ``10001912.39`` with no loss: it is
    the same money written in a different form, so this is a parse rather than a repair
    — and it is counted, printed and put in the manifest, which is what separates the
    two in practice.

    The two counters are deliberately distinct. A scientific repr is a *form* change:
    exact, zero information lost, ``expanded``. A value carrying three or more fractional
    places is a *rounding decision*, and that one is a fact the operator is owed, because
    it is the only case where the integer written down is not the integer that arrived.
    Counting them together would report six thousand rounding decisions where the corpus
    contains none, and a card figure that overstates a defect is as misleading as one
    that hides it.

    Returns the normalised series and the row counts for the expanded and rounded cases.
    """
    plain = values.str.contains(DECIMAL_2DP_PATTERN, literal=False)
    if bool(plain.all()):
        return values, 0, 0
    odd = values.filter(~plain)
    substitutions: dict[str, str] = {}
    # A value needing a rounding decision is one whose own exponent reaches past
    # hundredths; ``Decimal`` already knows, so this is read rather than inferred.
    rounding_needed: set[str] = set()
    for text in odd.unique(maintain_order=True).to_list():
        cleaned = str(text).strip()
        substitutions[cleaned] = amount_minor_to_text(minor_units_from_decimal_text(cleaned))
        if -Decimal(cleaned).as_tuple().exponent > 2:
            rounding_needed.add(cleaned)
    normalised = (
        values.to_frame("_raw")
        .with_columns(
            # ``default`` keeps every plain value untouched: ``replace_strict`` refuses
            # an incomplete mapping rather than producing a null, which is the right
            # failure mode for money but wrong here, where the mapping is only meant to
            # cover the non-plain minority.
            pl.col("_raw")
            .replace_strict(substitutions, default=pl.col("_raw"), return_dtype=pl.String)
            .alias("_norm")
        )
        .get_column("_norm")
    )
    expanded = int(odd.str.contains(SCIENTIFIC_DECIMAL_PATTERN, literal=False).sum())
    rounded = int(odd.is_in(sorted(rounding_needed)).sum()) if rounding_needed else 0
    return normalised, expanded, rounded


def canonicalize_batch(
    frame: pl.DataFrame,
    identity: RunIdentity,
    *,
    deployment_tz: ZoneInfo,
    epoch_utc: dt.datetime,
    step_hours: int,
    offset_modulus_us: int,
    offset_salt: str,
    future_tolerance_hours: int = DEFAULT_FUTURE_TOLERANCE_HOURS,
    row_offset: int = 0,
    batch_ordinal: int = 0,
    ingested_at: dt.datetime | None = None,
    pool: ProcessPoolExecutor | None = None,
) -> IngestResult:
    """Turn one validated raw batch into canonical events, vectorised.

    The order of operations is not arbitrary:

    1. structural gate, so a wrong-shaped file costs one record and not 100k;
    2. row-scoped contract mask, before any identity is computed, so a quarantined row
       never consumes a ``txn_id`` that would then have to be un-issued;
    3. deduplication, before the money parse, so a duplicate cannot double-count;
    4. money, identity and timestamp as expressions over whatever survived.

    ``batch_id`` is derived from the batch's own content (``batch_id_for_rows``) rather
    than from ``uuid4()``, because a random id inside the canonical columns makes two
    runs of the same corpus produce different bytes and ``make verify-determinism``
    becomes unachievable by construction.
    """
    if frame.height == 0:
        raise CanonicalizationError(
            "canonicalize_batch received an empty batch; config sets "
            "ingest.allow_empty_batch: false, so a batch that reads zero rows is an "
            "error and not a clean run"
        )

    rows_read = frame.height
    struct = _structural_failure(frame, row_offset=row_offset)
    if struct is not None:
        return IngestResult(
            events=empty_canonical_frame(persisted=True),
            quarantined=[struct],
            rows_read=rows_read,
            batch_id=identity.batch_id,
            batches=[
                BatchOutcome(
                    batch_id=identity.batch_id,
                    row_count=rows_read,
                    canonical_count=0,
                    quarantine_count=1,
                    window_start=None,
                    window_end=None,
                )
            ],
        )

    frame = frame.select(PAYSIM_RAW_COLUMNS)

    # The declared schema is the authority, so it runs as a whole-batch gate before any
    # row is processed. It costs 0.06 s per 200k rows measured on this corpus, which is
    # cheap enough that the adapter's own vectorised mask below is an *addition* to it
    # rather than a substitute: the mask exists because a frame-level check can only
    # say that something in the batch failed, and a quarantine record has to say which
    # row. If the two ever disagree, this gate fires first and the batch is rejected
    # whole, which is the direction that cannot lose rows silently.
    try:
        raw_paysim_schema.validate(frame)
    except Exception as exc:
        code = _reason_from_schema_error(exc)
        record = QuarantineRecord(
            reason=code,
            source_dataset=PAYSIM_SOURCE_NAME,
            row_index=row_offset,
            payload={"rows": frame.height},
            detail=str(exc).splitlines()[0][:500],
        )
        return IngestResult(
            events=empty_canonical_frame(persisted=True),
            quarantined=[record],
            rows_read=rows_read,
            batch_id=identity.batch_id,
            batches=[
                BatchOutcome(
                    batch_id=identity.batch_id,
                    row_count=rows_read,
                    canonical_count=0,
                    quarantine_count=1,
                    window_start=None,
                    window_end=None,
                )
            ],
        )

    frame = frame.with_row_index("_position")
    # Absolute offset in the source file, fixed once here so every later filter, join
    # and dedup carries it: a quarantine record that names a position inside whichever
    # chunk happened to contain the row is useless for triage against a 493 MB file.
    frame = frame.with_columns(pl.col("_position") + pl.lit(row_offset, dtype=pl.UInt32))
    quarantined: list[QuarantineRecord] = []

    # --- 2: row-scoped contract failures ------------------------------------
    frame = frame.with_columns(_reason=_row_reason(frame))
    rejected = frame.filter(pl.col("_reason").is_not_null())
    if rejected.height:
        quarantined.extend(
            QuarantineRecord(
                reason=str(row["_reason"]),
                source_dataset=PAYSIM_SOURCE_NAME,
                row_index=int(row["_position"]),
                payload={name: row[name] for name in PAYSIM_RAW_COLUMNS},
                detail=(
                    f"row violates the declared raw_paysim contract check {row['_reason']!r}; "
                    "quarantined with the original row, not repaired and not dropped"
                ),
            )
            for row in rejected.to_dicts()
        )
    frame = frame.filter(pl.col("_reason").is_null()).drop("_reason")

    # --- 3: duplicates ------------------------------------------------------
    frame = frame.with_columns(
        pl.concat_str([pl.col(name).cast(pl.String) for name in ROW_IDENTITY_COLUMNS], separator="|").alias(
            "_row_key"
        ),
        pl.concat_str([pl.col(name).cast(pl.String) for name in ROW_PAYLOAD_COLUMNS], separator="|").alias(
            "_payload_key"
        ),
    )
    grouped = frame.group_by("_row_key", maintain_order=True).agg(
        pl.len().alias("_occurrences"),
        pl.col("_payload_key").n_unique().alias("_variants"),
    )
    conflicting = grouped.filter(pl.col("_variants") > 1).get_column("_row_key").to_list()
    identical = grouped.filter((pl.col("_occurrences") > 1) & (pl.col("_variants") == 1))
    duplicates_dropped = int(identical["_occurrences"].sum() - identical.height) if identical.height else 0

    if conflicting:
        conflict_rows = frame.filter(pl.col("_row_key").is_in(conflicting))
        quarantined.extend(
            QuarantineRecord(
                reason="duplicate_conflict",
                source_dataset=PAYSIM_SOURCE_NAME,
                row_index=int(row["_position"]),
                payload={name: row[name] for name in PAYSIM_RAW_COLUMNS},
                detail=(
                    f"rows disagree on {ROW_PAYLOAD_COLUMNS} while sharing the transaction "
                    f"identity {ROW_IDENTITY_COLUMNS}; neither version is chosen silently"
                ),
            )
            for row in conflict_rows.to_dicts()
        )
        frame = frame.filter(~pl.col("_row_key").is_in(conflicting))

    # ``unique(keep='first')`` is order-stable because the frame keeps the file's row
    # order until the final sort, so which copy of an identical duplicate survives does
    # not depend on the hash distribution or the chunk boundaries.
    frame = frame.unique(subset=["_row_key"], keep="first", maintain_order=True)

    if frame.height == 0:
        return IngestResult(
            events=empty_canonical_frame(persisted=True),
            quarantined=quarantined,
            rows_read=rows_read,
            batch_id=identity.batch_id,
            duplicates_dropped=duplicates_dropped,
        )

    # --- 4: money, identities, timestamps -----------------------------------
    amount_text, amount_expanded, amount_rounded = normalise_money_texts(
        frame.get_column("amount")
    )
    balance_plain: dict[str, pl.Series] = {}
    balance_expanded = 0
    balance_rounded = 0
    for column in PAYSIM_BALANCE_COLUMNS:
        text, expanded, rounded = normalise_money_texts(frame.get_column(column).cast(pl.String))
        balance_plain[column] = text
        balance_expanded += expanded
        balance_rounded += rounded

    names = pl.concat([frame.get_column("nameOrig"), frame.get_column("nameDest")])
    distinct_names = names.unique(maintain_order=True).to_list()
    distinct_keys = frame.get_column("_row_key").unique(maintain_order=True).to_list()

    name_keys = account_keys_for_names(distinct_names, identity.run_salt, pool=pool)
    txn_ids, offsets_us = txn_ids_and_offsets(
        distinct_keys,
        namespace=PAYSIM_SOURCE_NAME,
        offset_salt=offset_salt,
        modulus_us=offset_modulus_us,
        pool=pool,
    )
    key_lookup = pl.DataFrame({"_row_key": distinct_keys, "txn_id": txn_ids, "_offset_us": offsets_us})
    name_lookup = pl.DataFrame({"name": distinct_names, "key": name_keys})

    batch_id = batch_id_for_rows(PAYSIM_SOURCE_NAME, batch_ordinal, frame.get_column("_row_key"))
    zone = resolve_local_timezone(deployment_tz)
    epoch_us = int(epoch_utc.replace(tzinfo=dt.UTC).timestamp() * 1_000_000)

    events = (
        frame.with_columns(
            pl.lit(PAYSIM_CURRENCY).cast(pl.String).alias("currency"),
            pl.lit(None, dtype=pl.String).alias("label_typology"),
            pl.lit(PAYSIM_SOURCE_NAME).cast(pl.String).alias("source_dataset"),
            pl.lit(batch_id).cast(pl.String).alias("batch_id"),
            pl.col("isFraud").cast(pl.Int8).alias("label_is_fraud"),
            pl.col("isFlaggedFraud").cast(pl.Int8).alias("label_is_flagged"),
            # The raw header keeps PaySim's own spelling all the way to this line; the
            # canonical name is assigned here and nowhere else, so the rename happens at
            # the boundary and a downstream reader never has to know what `type` was.
            pl.col("type").cast(pl.String).alias("txn_type"),
            pl.when(pl.col("type").is_in(list(PAYSIM_AGENT_RAIL_TYPES)))
            .then(pl.lit(PAYSIM_CHANNEL_AGENT))
            .otherwise(pl.lit(PAYSIM_CHANNEL_APP))
            .alias("channel"),
            pl.Series("_amount_plain", amount_text),
        )
        .join(key_lookup, on="_row_key", how="left")
        .join(name_lookup, left_on=pl.col("nameOrig"), right_on="name", how="left")
        .rename({"key": "account_from"})
        .join(name_lookup, left_on=pl.col("nameDest"), right_on="name", how="left")
        .rename({"key": "account_to"})
        .with_columns(
            (
                pl.lit(epoch_us, dtype=pl.Int64)
                + pl.col("step").cast(pl.Int64) * pl.lit(step_hours * 3_600_000_000, dtype=pl.Int64)
                + pl.col("_offset_us").cast(pl.Int64)
            )
            .cast(UTC_MICROS)
            .alias("event_ts_utc"),
            minor_units_expr("_amount_plain", alias="amount_minor"),
        )
        .with_columns(
            local_hour_expr("event_ts_utc", zone).alias("local_hour"),
            local_date_expr("event_ts_utc", zone).alias("event_date_local"),
        )
    )

    for column, target in zip(PAYSIM_BALANCE_COLUMNS, CANONICAL_BALANCE_TARGETS, strict=True):
        events = events.with_columns(
            pl.Series(f"_plain_{column}", balance_plain[column]),
        ).with_columns(minor_units_expr(f"_plain_{column}", alias=target))

    # --- future timestamps, checked after the expansion because the check is
    # --- about the instant the expansion produced, not about the raw step.
    reference = ingested_at if ingested_at is not None else dt.datetime.now(dt.UTC)
    horizon = reference + dt.timedelta(hours=future_tolerance_hours)
    beyond = events.get_column("event_ts_utc") > horizon
    if bool(beyond.any()):
        offenders = events.filter(beyond).to_dicts()
        quarantined.extend(
            QuarantineRecord(
                reason="future_timestamp",
                source_dataset=PAYSIM_SOURCE_NAME,
                row_index=int(row["_position"]),
                payload={
                    "txn_id": row["txn_id"],
                    "step": row["step"],
                    "event_ts_utc": str(row["event_ts_utc"]),
                },
                detail=(
                    f"event_ts_utc {row['event_ts_utc']} is beyond the "
                    f"{future_tolerance_hours}h tolerance from the run reference "
                    f"{horizon.isoformat()}; a synthetic epoch drift or a clock problem, "
                    "not a transaction"
                ),
            )
            for row in offenders
        )
        events = events.filter(~beyond)

    events = events.select(PERSISTED_CANONICAL_COLUMNS).sort(["event_ts_utc", "txn_id"])

    window_start = events.get_column("event_ts_utc").min() if events.height else None
    window_end = events.get_column("event_ts_utc").max() if events.height else None

    return IngestResult(
        events=events,
        quarantined=quarantined,
        rows_read=rows_read,
        batch_id=batch_id,
        duplicates_dropped=duplicates_dropped,
        scientific_amounts_expanded=amount_expanded + balance_expanded,
        rounding_applied=amount_rounded + balance_rounded,
        window_start=window_start,
        window_end=window_end,
        batches=[
            BatchOutcome(
                batch_id=batch_id,
                row_count=rows_read,
                canonical_count=events.height,
                quarantine_count=sum(1 for record in quarantined if record.reason != "schema_mismatch"),
                window_start=window_start,
                window_end=window_end,
            )
        ],
    )


def ingest_paysim(
    path: Path,
    identity: RunIdentity,
    *,
    deployment_tz: ZoneInfo,
    epoch_utc: dt.datetime,
    step_hours: int,
    offset_modulus_us: int,
    offset_salt: str,
    batch_rows: int = 100_000,
    limit: int | None = None,
    future_tolerance_hours: int = DEFAULT_FUTURE_TOLERANCE_HOURS,
    ingested_at: dt.datetime | None = None,
    use_process_pool: bool = True,
    on_batch: Callable[[IngestResult], None] | None = None,
    keep_events: bool = True,
) -> IngestResult:
    """Ingest a whole PaySim CSV, concatenating canonical events across batches.

    Row offsets are threaded through so a quarantine record points at the row's
    position in the original file, not its position inside whichever chunk happened to
    contain it. Without that, triage against chunk 40 is a guessing game.

    One warm ``ProcessPoolExecutor`` is created for the run and reused by every batch,
    because pool start-up on Windows costs about a second and sixty-four cold pools
    would spend more spawning than the hashing saves. ``use_process_pool=False`` runs
    the same work serially and produces byte-identical output, which is what makes the
    worker count a performance knob rather than a semantic one.

    ``on_batch`` is the memory valve. This host had 4.6 GiB free with both corpora on
    disk, and holding six million canonical rows in one frame beside the six-million
    row raw frame is the difference between a run and a paging stall. When a sink is
    supplied the batch's events are handed to it, dropped, and the aggregate returns an
    empty ``events`` frame with the counters intact; ``keep_events`` is the default for
    tests, where the frame IS the assertion.
    """
    batches = read_raw_batches(path, batch_rows=batch_rows, limit=limit)
    if not batches:
        raise CanonicalizationError(
            f"{path} yielded no batches; config sets ingest.allow_empty_batch: false, "
            "so a source that reads zero rows is an error rather than a clean run"
        )

    combined_events: list[pl.DataFrame] = []
    quarantined: list[QuarantineRecord] = []
    outcomes: list[BatchOutcome] = []
    rows_read = 0
    duplicates_dropped = 0
    scientific_expanded = 0
    rounding_applied = 0
    row_offset = 0

    pool: ProcessPoolExecutor | None = None
    workers = hash_workers()
    if use_process_pool and workers > 1:
        try:
            pool = ProcessPoolExecutor(max_workers=workers)
        except Exception:
            pool = None
    try:
        for ordinal, frame in enumerate(batches):
            result = canonicalize_batch(
                frame,
                identity,
                deployment_tz=deployment_tz,
                epoch_utc=epoch_utc,
                step_hours=step_hours,
                offset_modulus_us=offset_modulus_us,
                offset_salt=offset_salt,
                future_tolerance_hours=future_tolerance_hours,
                row_offset=row_offset,
                batch_ordinal=ordinal,
                ingested_at=ingested_at,
                pool=pool,
            )
            if result.events.height and (on_batch is None or keep_events):
                combined_events.append(result.events)
            if on_batch is not None:
                on_batch(result)
                if not keep_events:
                    result = replace(result, events=empty_canonical_frame(persisted=True))
            quarantined.extend(result.quarantined)
            outcomes.extend(result.batches)
            rows_read += result.rows_read
            duplicates_dropped += result.duplicates_dropped
            scientific_expanded += result.scientific_amounts_expanded
            rounding_applied += result.rounding_applied
            row_offset += result.rows_read
    finally:
        if pool is not None:
            pool.shutdown()

    events = (
        pl.concat(combined_events, how="vertical")
        if combined_events
        else empty_canonical_frame(persisted=True)
    )
    return IngestResult(
        events=events,
        quarantined=quarantined,
        rows_read=rows_read,
        batch_id=outcomes[0].batch_id if outcomes else identity.batch_id,
        duplicates_dropped=duplicates_dropped,
        scientific_amounts_expanded=scientific_expanded,
        rounding_applied=rounding_applied,
        window_start=events.get_column("event_ts_utc").min() if events.height else None,
        window_end=events.get_column("event_ts_utc").max() if events.height else None,
        batches=outcomes,
    )


__all__ = [
    "CANONICAL_BALANCE_TARGETS",
    "DEFAULT_FUTURE_TOLERANCE_HOURS",
    "PAYSIM_AGENT_RAIL_TYPES",
    "PAYSIM_APP_RAIL_TYPES",
    "PAYSIM_BALANCE_COLUMNS",
    "PAYSIM_CHANNEL_AGENT",
    "PAYSIM_CHANNEL_APP",
    "PAYSIM_SOURCE_NAME",
    "ROW_IDENTITY_COLUMNS",
    "ROW_PAYLOAD_COLUMNS",
    "BatchOutcome",
    "IngestResult",
    "QuarantineRecord",
    "canonicalize_batch",
    "ingest_paysim",
    "normalise_money_texts",
    "read_raw_batches",
]
