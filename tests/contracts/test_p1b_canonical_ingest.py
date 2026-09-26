"""P1b contract tests: the fourteen behaviours plan §6 names, on hand-built PaySim batches.

WHY THESE TESTS EXIST AND WHAT THEY REFUSE TO DO. Every expected value below was
computed by hand from the fixture in ``conftest.py`` -- the row counts, the minor units
(``9839.64`` is ``983964``), the digest of one transaction's identity, the hour a UTC
instant becomes in Africa/Kampala. None of them is read back out of the code under test,
because a test whose expectation comes from the function it exercises proves only that
the function is itself (00 B).

The suite covers the boundary the whole pipeline rests on: money is integer minor units,
a row is canonicalised or quarantined and never dropped, a run is deterministic to the
byte, and identity is salted and namespaced so two corpora can never merge into one
account. A behaviour these tests require that the adapter did not implement (duplicate
handling, whole-batch rejection, the future-instant guard) is implemented in
``ingest/paysim.py`` rather than weakened here.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from oxbow.contracts.canonical_v1 import (
    ERR_DTYPE_MISMATCH,
    ERR_MISSING_COLUMN,
    ERR_NEGATIVE_MONEY,
    ERR_NULL_IN_REQUIRED,
    ERR_UNKNOWN_COLUMN,
    MONEY_COLUMNS,
    PERSISTED_CANONICAL_COLUMNS,
    CanonicalContractError,
    assert_canonical_frame,
)
from oxbow.ingest.canonical import (
    CanonicalizationError,
    RunIdentity,
    account_key,
    batch_id_for_rows,
    intra_step_offset_us,
    node_key,
    step_to_timestamp,
)
from oxbow.ingest.paysim import (
    DEFAULT_FUTURE_TOLERANCE_HOURS,
    PAYSIM_AGENT_RAIL_TYPES,
    PAYSIM_APP_RAIL_TYPES,
    PAYSIM_CHANNEL_AGENT,
    PAYSIM_CHANNEL_APP,
    canonicalize_batch,
    ingest_paysim,
    read_raw_batches,
)
from tests.contracts.conftest import (
    PAYSIM_NAMESPACE,
    RUN_ID,
    TEST_BATCH_ID,
    TEST_SALT,
    base_row,
    oracle_account_key,
    oracle_batch_id,
    oracle_instant,
    oracle_offset_us,
    oracle_txn_id,
    paysim_csv,
    raw_frame,
    row_key,
)

# The fixture: three distinct legitimate transactions plus the variants each test needs.
#
#   r0  step 1  TRANSFER   9839.64   C1000 -> C2000   unflagged, legitimate
#   r1  step 1  CASH_OUT  15750.50   C1001 -> C2001   isFraud = 1
#   r2  step 2  PAYMENT      12.00   C1002 -> C2002   isFraud = 0
#
# 9839.64 -> 9839 * 100 + 64 = 983964
# 15750.50 -> 15750 * 100 + 50 = 1575050
# 12.00    -> 12 * 100 + 0     = 1200
ROWS: tuple[dict[str, Any], ...] = (
    base_row(),
    base_row(
        type="CASH_OUT",
        amount="15750.50",
        nameOrig="C1001",
        oldbalanceOrg=15750.50,
        newbalanceOrig=0.00,
        nameDest="C2001",
        oldbalanceDest=0.00,
        newbalanceDest=15750.50,
        isFraud=1,
    ),
    base_row(
        step=2,
        type="PAYMENT",
        amount="12.00",
        nameOrig="C1002",
        oldbalanceOrg=12.00,
        newbalanceOrig=0.00,
        nameDest="C2002",
        oldbalanceDest=90.00,
        newbalanceDest=102.00,
    ),
)

EXPECTED_AMOUNT_MINOR: tuple[int, ...] = (983964, 1575050, 1200)

# Balances, same arithmetic, in canonical order: src before, src after, dst before,
# dst after. r0: 10000.00 / 160.36 / 0.00 / 0.00.
EXPECTED_R0_BALANCES: tuple[int, int, int, int] = (1000000, 16036, 0, 0)
EXPECTED_R1_BALANCES: tuple[int, int, int, int] = (1575050, 0, 0, 1575050)
EXPECTED_R2_BALANCES: tuple[int, int, int, int] = (1200, 0, 9000, 10200)

# Channel derivation: CASH_OUT touches an agent's float, TRANSFER and PAYMENT do not.
EXPECTED_CHANNELS: tuple[str, ...] = (
    PAYSIM_CHANNEL_APP,
    PAYSIM_CHANNEL_AGENT,
    PAYSIM_CHANNEL_APP,
)

# The three keys, digested independently in conftest and asserted against here.
KEYS: tuple[str, ...] = tuple(row_key(int(r["step"]), str(r["type"]), str(r["amount"]), str(r["nameOrig"]), str(r["nameDest"])) for r in ROWS)
TXN_IDS: tuple[str, ...] = tuple(oracle_txn_id(key) for key in KEYS)


def _batch(rows: list[dict[str, Any]], identity: RunIdentity, kwargs: dict[str, Any], **extra: Any) -> Any:
    return canonicalize_batch(raw_frame(rows), identity, **kwargs, **extra)


# --- duplicates ------------------------------------------------------------


def test_dup_txn_identical_dropped(identity: RunIdentity, canonical_kwargs: dict[str, Any]) -> None:
    """A re-delivered row is the same transaction: dropped, counted, never a second event.

    The corpus has zero exact re-deliveries (measured over all 6,362,620 rows), so this
    path exists for the source that changes, not the one we have. Two rows sharing all
    five identity columns AND every payload column cannot be two transactions; keeping
    both would double the amount in every aggregate and every exposure figure.
    """
    rows = [ROWS[0], dict(ROWS[0]), ROWS[1]]
    result = _batch(rows, identity, canonical_kwargs)

    assert result.rows_read == 3
    assert result.canonical_count == 2
    assert result.duplicates_dropped == 1
    assert result.quarantine_count == 0
    assert result.events.get_column("txn_id").to_list() == [TXN_IDS[0], TXN_IDS[1]]
    # The surviving copy is the first one in file order, so the result does not depend
    # on which row the hash distribution happened to visit first.
    assert result.events.get_column("amount_minor").to_list() == [
        EXPECTED_AMOUNT_MINOR[0],
        EXPECTED_AMOUNT_MINOR[1],
    ]


def test_dup_txn_conflicting_quarantined(
    identity: RunIdentity, canonical_kwargs: dict[str, Any]
) -> None:
    """Same identity, different payload: neither version is chosen silently.

    The five identity columns say "this is that transaction", so a disagreement in a
    balance column is a dispute *about* it. Picking one copy would invent a balance;
    keeping both would double-count the transfer. Both rows are quarantined with the
    reason, and the batch loses two of three rows with the loss recorded.
    """
    conflicting = dict(ROWS[0])
    conflicting["oldbalanceOrg"] = 9999.00  # same transaction, different balance
    result = _batch([ROWS[0], conflicting, ROWS[1]], identity, canonical_kwargs)

    assert result.canonical_count == 1
    assert result.duplicates_dropped == 0
    assert result.quarantine_count == 2
    reasons = {record.reason for record in result.quarantined}
    assert reasons == {"duplicate_conflict"}
    assert [int(record.row_index) for record in result.quarantined] == [0, 1]
    assert result.events.get_column("txn_id").to_list() == [TXN_IDS[1]]


# --- structural failures: fail closed, name the column ---------------------


def test_unknown_column_fails_closed(identity: RunIdentity, canonical_kwargs: dict[str, Any]) -> None:
    """An undeclared column is schema drift and rejects the batch whole.

    ``oldBalanceDest`` with a capital B is the misspelling this repo actually shipped in
    ``config/sources.yaml`` and in the first IBM contract. A reader that ignored the
    unknown name would produce a canonical table missing a balance column and still exit
    zero; strict mode is what turns that into one record that names the column.
    """
    frame = raw_frame([dict(r) for r in ROWS]).with_columns(pl.lit(0.0).alias("oldBalanceDest"))

    result = canonicalize_batch(frame, identity, **canonical_kwargs)

    assert result.quarantine_count == 1
    assert result.canonical_count == 0
    record = result.quarantined[0]
    assert record.reason == ERR_UNKNOWN_COLUMN
    assert "oldBalanceDest" in record.detail
    assert record.payload["columns"][-1] == "oldBalanceDest"
    # The batch is rejected whole: one undeclared name costs one record, not one record
    # per row, so a renamed column reads as a source problem rather than 100k row faults.
    assert record.row_index == 0


def test_missing_required_column(identity: RunIdentity, canonical_kwargs: dict[str, Any]) -> None:
    """A dropped column fails the same way, and says which one is gone."""
    columns = ("step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig",
               "nameDest", "oldbalanceDest", "newbalanceDest", "isFraud")
    frame = raw_frame([dict(r) for r in ROWS]).select(list(columns))

    result = canonicalize_batch(frame, identity, **canonical_kwargs)

    assert result.quarantine_count == 1
    assert result.canonical_count == 0
    record = result.quarantined[0]
    assert record.reason == ERR_MISSING_COLUMN
    assert "isFlaggedFraud" in record.detail


# --- money -----------------------------------------------------------------


def test_zero_silent_coercions(identity: RunIdentity, canonical_kwargs: dict[str, Any]) -> None:
    """Money is int64 minor units, exact, with no coercion and no silent rounding.

    Every assertion here is against an integer written out by hand from the fixture's
    decimal strings. ``float("9839.64") * 100`` is 983963.9999999999, which is precisely
    the defect 01 §B forbids: a cent error that appears on some rows only shows up as a
    reconciliation failure months later, in a packet someone quotes.
    """
    result = _batch([dict(r) for r in ROWS], identity, canonical_kwargs)
    events = result.events

    assert_canonical_frame(events, persisted=True)
    schema = events.collect_schema()
    for column in MONEY_COLUMNS:
        assert schema[column] == pl.Int64(), f"{column} is {schema[column]}, not Int64"

    assert events.get_column("amount_minor").to_list() == list(EXPECTED_AMOUNT_MINOR)
    for index, expected in enumerate((EXPECTED_R0_BALANCES, EXPECTED_R1_BALANCES, EXPECTED_R2_BALANCES)):
        row = events.slice(index, 1).to_dicts()[0]
        got = (
            row["src_balance_before_minor"],
            row["src_balance_after_minor"],
            row["dst_balance_before_minor"],
            row["dst_balance_after_minor"],
        )
        assert got == expected

    # A three-place decimal is not "close enough": the raw contract refuses the form, so
    # the batch is quarantined instead of rounded into silence.
    ragged = dict(ROWS[0])
    ragged["amount"] = "9839.641"
    ragged_result = _batch([ragged], identity, canonical_kwargs)
    assert ragged_result.canonical_count == 0
    assert ragged_result.quarantined[0].reason == "amount_format"

    # And a float dtype on a money column is a failure, never a cast: coerce=False.
    coerced = events.with_columns(pl.col("amount_minor").cast(pl.Float64))
    with pytest.raises(CanonicalContractError) as excinfo:
        assert_canonical_frame(coerced, persisted=True)
    assert ERR_DTYPE_MISMATCH in str(excinfo.value)

    # Nothing in the clean fixture was rounded, and nothing was expanded.
    assert result.rounding_applied == 0
    assert result.scientific_amounts_expanded == 0
    assert result.quarantine_count == 0


def test_scientific_amount_is_expanded_and_counted(
    identity: RunIdentity, canonical_kwargs: dict[str, Any]
) -> None:
    """``1.000191239E7`` is the same money written differently: parsed, counted, not hidden.

    5,650 rows of the real corpus arrive this way because PaySim's writer emitted a float
    repr for the largest TRANSFER amounts. The expansion is exact (10001912.39), so
    refusing the form would quarantine 5,650 genuine transactions -- but accepting it
    silently would be worse, which is why the count is printed and put in the manifest.
    """
    scientific = dict(ROWS[0])
    scientific["amount"] = "1.000191239E7"
    result = _batch([scientific], identity, canonical_kwargs)

    # 1.000191239 x 10^7 = 10001912.39 -> 10001912 * 100 + 39 = 1000191239
    assert result.events.get_column("amount_minor").to_list() == [1000191239]
    assert result.scientific_amounts_expanded == 1
    assert result.rounding_applied == 0
    assert result.quarantine_count == 0


# --- determinism -----------------------------------------------------------


def test_ingest_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: RunIdentity,
    canonical_kwargs: dict[str, Any],
) -> None:
    """Two runs of the same bytes produce the same events, ids and file.

    This is the property DEV-012 exists to make possible: ``run_id`` and ``ingested_at``
    are excluded from the persisted columns precisely so that ``make verify-determinism``
    can compare digests at all. The parallel identity path is checked against the serial
    one as well, because a hashing optimisation that changed a byte would be a
    correctness bug wearing a performance hat.

    Batch size is asserted separately and differently: ``batch_id`` is a digest of the
    batch's own rows, so repartitioning the same corpus *must* change that one column --
    and must change nothing else. Identity, instant and money are functions of row
    content, never of position, which is the claim the second half of this test makes.
    """
    path = paysim_csv(tmp_path / "ps.csv", [dict(r) for r in ROWS])

    def run(**overrides: Any) -> Any:
        return ingest_paysim(
            path,
            identity,
            ingested_at=datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC),
            use_process_pool=False,
            **canonical_kwargs,
            **overrides,
        )

    serial = run(batch_rows=3)
    again = run(batch_rows=3)
    assert serial.events.equals(again.events)
    assert [b.batch_id for b in serial.batches] == [b.batch_id for b in again.batches]

    one, two = tmp_path / "a.parquet", tmp_path / "b.parquet"
    serial.events.write_parquet(one)
    again.events.write_parquet(two)
    assert one.read_bytes() == two.read_bytes(), "two runs of one file produced different bytes"

    by_one = run(batch_rows=1)
    content_columns = [
        column for column in PERSISTED_CANONICAL_COLUMNS if column != "batch_id"
    ]
    assert serial.events.select(content_columns).equals(
        by_one.events.select(content_columns)
    ), "repartitioning changed a row's identity, instant or money"
    assert len({b.batch_id for b in by_one.batches}) == 3
    assert len({b.batch_id for b in serial.batches}) == 1

    # The batch id is a digest of the batch's content, recomputed here independently,
    # and the adapter's own helper agrees with the oracle rather than with itself.
    assert serial.batch_id == oracle_batch_id(0, list(KEYS))
    assert batch_id_for_rows(PAYSIM_NAMESPACE, 0, list(KEYS)) == oracle_batch_id(0, list(KEYS))
    assert by_one.batches[0].batch_id == oracle_batch_id(0, [KEYS[0]])
    assert by_one.batches[2].batch_id == oracle_batch_id(2, [KEYS[2]])
    assert by_one.batches[1].batch_id == oracle_batch_id(1, [KEYS[1]])
    assert serial.rows_read == 3
    assert serial.duplicates_dropped == 0

    # The hash pool is a performance knob, so it must not be able to move a byte:
    # ``hash_workers`` is what the ingest consults, and the bounded range is part of
    # the contract (a hint of 100 on a 4-core host is still capped, and still the
    # same output).
    import oxbow.ingest.canonical as canonical_module

    assert canonical_module.hash_workers(1) == 1
    assert canonical_module.hash_workers(100) == 16
    names = [f"C{3000 + index}" for index in range(12)]
    monkeypatch.setattr(canonical_module, "_HASH_CHUNK_MIN", 2)
    monkeypatch.setenv("OXBOW_HASH_WORKERS", "1")
    one_worker = canonical_module.account_keys_for_names(names, TEST_SALT)
    monkeypatch.setenv("OXBOW_HASH_WORKERS", "3")
    three_workers = canonical_module.account_keys_for_names(names, TEST_SALT)
    assert one_worker == three_workers, "worker count changed a digest"
    assert one_worker[0] == oracle_account_key(names[0])
    assert canonical_module.txn_ids_and_offsets(
        [row_key(1, "PAYMENT", "1.00", name, "M1") for name in names],
        namespace=PAYSIM_NAMESPACE,
        offset_salt="oxbow-paysim-step-v1",
        modulus_us=86_400_000_000,
    )[0][0] == oracle_txn_id(row_key(1, "PAYMENT", "1.00", names[0], "M1"))

    # Two runs of the whole stage leave the same file bytes, which is what
    # ``make verify-determinism`` compares: same path, same digest, same content.
    from oxbow.adapters.file.canonical_sink import CanonicalSink

    determinism = {
        "parquet_row_group_size": 100_000,
        "parquet_compression": "zstd",
        "parquet_compression_level": 3,
    }
    digests: list[str] = []
    for _ in range(2):
        sink = CanonicalSink(
            interim_root=tmp_path / "interim",
            duckdb_path=tmp_path / "out.duckdb",
            determinism=determinism,
        )
        artifact = sink.write_batch(PAYSIM_NAMESPACE, serial.batch_id, serial.events)
        landed = Path(str(artifact["path"]))
        digests.append(hashlib.sha256(landed.read_bytes()).hexdigest())
        assert artifact["sha256"] == digests[-1]
        assert artifact["rows"] == 3
    assert digests[0] == digests[1]


# --- labels ----------------------------------------------------------------


def test_label_provenance_required(
    tmp_path: Path, identity: RunIdentity, canonical_kwargs: dict[str, Any]
) -> None:
    """Every canonical row names its corpus, and PaySim never invents a typology.

    ``source_dataset`` is what makes "we report metrics per corpus and never average
    them" possible at all. ``label_typology`` is null on every PaySim row because the
    corpus has no typology taxonomy: a non-null there is not a missing value filled in,
    it is a fabricated label that reads like evidence (DEV-013, DEV-014).
    """
    result = _batch([dict(r) for r in ROWS], identity, canonical_kwargs)
    events = result.events

    assert events.get_column("source_dataset").unique().to_list() == ["paysim"]
    assert events.get_column("label_typology").null_count() == events.height
    assert events.get_column("label_is_fraud").to_list() == [0, 1, 0]
    assert events.get_column("label_is_flagged").to_list() == [0, 0, 0]

    from oxbow.contracts.canonical_v1 import assert_label_provenance

    assert_label_provenance(events, source_carries_typology=False)

    invented = events.with_columns(pl.lit("CYCLE").cast(pl.String).alias("label_typology"))
    with pytest.raises(CanonicalContractError, match="label_provenance_missing"):
        assert_label_provenance(invented, source_carries_typology=False)

    blanked = events.with_columns(pl.lit("").alias("source_dataset"))
    with pytest.raises(CanonicalContractError, match="label_provenance_missing"):
        assert_label_provenance(blanked, source_carries_typology=False)

    dropped = events.with_columns(
        pl.when(pl.col("txn_id") == events.get_column("txn_id")[0])
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.col("label_typology"))
        .alias("label_typology")
    )
    with pytest.raises(CanonicalContractError, match="label_provenance_missing"):
        assert_label_provenance(dropped, source_carries_typology=True)

    with pytest.raises(CanonicalContractError, match="label_provenance_missing"):
        assert_label_provenance(events.clear(), source_carries_typology=False)


def test_empty_batch_errors(
    tmp_path: Path, identity: RunIdentity, canonical_kwargs: dict[str, Any]
) -> None:
    """A source that reads zero rows is an error, not a clean run.

    03 §B: a run that scored nothing and a run that found nothing look identical
    downstream, and only one of them is true. Both entry points refuse it -- the batch
    function and the whole-file ingest -- because a config that allowed an empty batch
    would let a truncated file pass.
    """
    with pytest.raises(CanonicalizationError, match="empty batch"):
        canonicalize_batch(raw_frame([]), identity, **canonical_kwargs)

    header_only = paysim_csv(tmp_path / "header_only.csv", [])
    with pytest.raises(CanonicalizationError, match="no batches"):
        ingest_paysim(
            header_only,
            identity,
            ingested_at=datetime(2026, 9, 26, tzinfo=UTC),
            use_process_pool=False,
            **canonical_kwargs,
        )

    # A file that is empty apart from its header is the same failure as a file that is
    # empty entirely: `read_raw_batches` returns no batches, and the ingest refuses.
    assert read_raw_batches(header_only, batch_rows=100) == []
    empty_file = tmp_path / "empty.csv"
    empty_file.write_bytes(b"")
    with pytest.raises(CanonicalizationError, match="no batches"):
        ingest_paysim(
            empty_file,
            identity,
            ingested_at=datetime(2026, 9, 26, tzinfo=UTC),
            use_process_pool=False,
            **canonical_kwargs,
        )


# --- synthetic time --------------------------------------------------------


def test_step_expansion_stable(expansion: dict[str, Any]) -> None:
    """``step`` becomes a UTC instant deterministically, from the transaction's own id.

    The corpus shares one day counter across ~8.5k rows, so without an offset every
    transaction inside a step is mutually indistinguishable and intra-step order becomes
    arbitrary -- a lookback rule would then see a different history on the second run.
    The offset is an HMAC of the transaction id, so the same row lands at the same instant
    in every run at any batch size, and the scalar reference below agrees with the
    vectorised path the adapter actually takes.
    """
    assert expansion["step_hours"] == 24
    assert expansion["modulus_us"] == 86_400_000_000
    assert expansion["offset_salt"] == "oxbow-paysim-step-v1"
    assert expansion["epoch"] == datetime(2014, 1, 1, tzinfo=UTC)

    modulus = int(expansion["modulus_us"])
    salt = str(expansion["offset_salt"])
    epoch = expansion["epoch"]

    for index, key in enumerate(KEYS):
        txn_id = oracle_txn_id(key)
        step = int(ROWS[index]["step"])
        expected_offset = oracle_offset_us(txn_id, salt, modulus)
        assert intra_step_offset_us(txn_id, salt, modulus) == expected_offset
        assert step_to_timestamp(step, epoch, 24, expected_offset) == oracle_instant(
            step, key, epoch=epoch, step_hours=24, offset_salt=salt, modulus_us=modulus
        )
        # The offset never leaves its own step, which is what keeps a step's rows inside
        # one calendar day rather than leaking into the next.
        assert 0 <= expected_offset < modulus

    # Two independent computations of the same instant, from the id and from the salted
    # digest, must agree -- otherwise the adapter's vectorised path is a second source
    # of truth about time.
    first = oracle_instant(1, KEYS[0], epoch=epoch, step_hours=24, offset_salt=salt, modulus_us=modulus)
    second = oracle_instant(1, KEYS[0], epoch=epoch, step_hours=24, offset_salt=salt, modulus_us=modulus)
    assert first == second
    assert first.year == 2014


def test_future_timestamp_quarantined(
    tmp_path: Path, identity: RunIdentity, canonical_kwargs: dict[str, Any]
) -> None:
    """An instant beyond the run horizon is quarantined, never truncated into place.

    A step that lands past the batch window plus the tolerance means the synthetic epoch
    drifted or the file's counter changed meaning. Clamping it would fabricate a
    transaction at a moment nobody observed; the row is kept, with the reason and the
    instant it claimed.
    """
    tolerance = int(DEFAULT_FUTURE_TOLERANCE_HOURS)
    assert canonical_kwargs["future_tolerance_hours"] == tolerance

    # 4,383 days from a 2014-01-01 epoch is 2026-01-01, and 4,560 is 2026-06-27. The
    # modulus is a whole day in microseconds (86,400,000,000), so a step's rows span
    # [step 00:00, step+1 00:00) and the horizon has to be chosen against the end of
    # that range, not its start: against a reference of 2026-06-01T00:00Z plus 6h, the
    # step-4383 row can never be future and the step-4560 row can never be inside.
    future = dict(ROWS[2])
    future["step"] = 4560
    inside = dict(ROWS[1])
    inside["step"] = 4383

    result = canonicalize_batch(
        raw_frame([future, inside]),
        identity,
        ingested_at=datetime(2026, 6, 1, tzinfo=UTC),
        **canonical_kwargs,
    )

    assert result.canonical_count == 1
    assert result.quarantine_count == 1
    record = result.quarantined[0]
    assert record.reason == "future_timestamp"
    assert "beyond" in record.detail
    assert result.events.get_column("txn_id").to_list() == [
        oracle_txn_id(row_key(4383, "CASH_OUT", "15750.50", "C1001", "C2001"))
    ]

    # And the same two rows, unchanged, are both canonical against a later reference:
    # the guard is about the run horizon, not about the step value itself.
    later = canonicalize_batch(
        raw_frame([future, inside]),
        identity,
        ingested_at=datetime(2028, 1, 1, tzinfo=UTC),
        **canonical_kwargs,
    )
    assert later.quarantine_count == 0
    assert later.canonical_count == 2


# --- identity --------------------------------------------------------------


def test_account_key_unique_per_run(identity: RunIdentity, canonical_kwargs: dict[str, Any]) -> None:
    """48-bit salted keys carry no collisions over a 200-name run, and never leak the salt.

    Truncation is a judgement, not a guarantee: 2^48 sits far above the 9,073,900 distinct
    account names in PaySim, and this test is where that "far above" is checked rather
    than asserted in prose. The key shape is pinned to 12 lowercase hex, because a leaked
    unsalted digest still joins and still groups -- it just silently re-identifies the
    account it was meant to hide.
    """
    names = [f"C{1000 + index}" for index in range(200)] + [f"M{1000 + index}" for index in range(20)]
    keys = [account_key(name, identity) for name in names]

    assert len(set(keys)) == len(names), "a 48-bit key collided inside one run"
    for key in keys:
        assert len(key) == 12
        assert all(character in "0123456789abcdef" for character in key)
        assert TEST_SALT not in key
    assert keys[0] == oracle_account_key("C1000")
    assert account_key("C1000", identity) == keys[0]

    other = RunIdentity(run_salt="a-completely-different-salt", batch_id=TEST_BATCH_ID, run_id=RUN_ID)
    assert account_key("C1000", other) != keys[0], "the salt does not change the key"

    # The frame the adapter emits carries only keys, never the raw name, and the shape
    # check in the contract refuses anything that is not 12 lowercase hex.
    result = _batch([dict(r) for r in ROWS], identity, canonical_kwargs)
    events = result.events
    assert set(events.get_column("account_from").to_list()) == {
        oracle_account_key("C1000"),
        oracle_account_key("C1001"),
        oracle_account_key("C1002"),
    }
    assert "C1000" not in events.get_column("account_from").to_list()
    leaked = events.with_columns(pl.lit("C1000").cast(pl.String).alias("account_from"))
    with pytest.raises(CanonicalContractError, match="account_key_shape"):
        assert_canonical_frame(leaked, persisted=True)


def test_no_cross_source_node_merge(identity: RunIdentity, canonical_kwargs: dict[str, Any]) -> None:
    """Two corpora holding the same account name must never become one node.

    This is DEV-011's hazard stated at the only boundary that can prevent it. One shared
    deployment salt hashes ``C1000`` to one 12-hex key whichever corpus it arrives from,
    and a graph stage that built nodes from the key column alone would merge them and let
    each corpus inherit the other's counterparties, degree and community. That is not a
    subtle error: it fabricates network structure neither corpus has.

    The mitigation lives at the node, not in ``account_key``, whose scheme is externally
    pinned and whose column list is frozen at canonical v1 -- so changing either would
    break the contract before it fixed the hazard.
    """
    paysim_key = account_key("C1000", identity)
    ibm_identity = RunIdentity(run_salt=TEST_SALT, batch_id=TEST_BATCH_ID, run_id=RUN_ID)
    ibm_key = account_key("C1000", ibm_identity)

    # The hazard is real and stated: one salt, one name, one key.
    assert paysim_key == ibm_key

    # The node identity separates them, and a node id without a namespace is refused.
    assert node_key("paysim", paysim_key) != node_key("ibmaml", ibm_key)
    assert node_key("paysim", paysim_key) == f"paysim#{paysim_key}"
    with pytest.raises(CanonicalizationError, match="namespace"):
        node_key("", paysim_key)

    # ``txn_id`` is namespaced too, so one id can never impersonate the other corpus:
    # the contract's own check refuses a row whose id does not begin with its source.
    result = _batch([dict(r) for r in ROWS], identity, canonical_kwargs)
    events = result.events
    assert all(value.startswith("paysim:") for value in events.get_column("txn_id").to_list())
    impersonating = events.with_columns(
        pl.col("txn_id").str.replace("paysim:", "ibmaml:").alias("txn_id")
    )
    with pytest.raises(CanonicalContractError, match="txn_id_not_namespaced"):
        assert_canonical_frame(impersonating, persisted=True)

    # Concatenating two corpora's canonical rows therefore produces two distinct node
    # identities per shared name, and the persisted shape is unchanged by that.
    union = pl.concat([events, events.with_columns(pl.lit("paysim").alias("source_dataset"))])
    assert union.width == len(PERSISTED_CANONICAL_COLUMNS)
    assert len({node_key("paysim", paysim_key), node_key("ibmaml", paysim_key)}) == 2


# --- reported measurement --------------------------------------------------


def test_counterparty_reuse_reported() -> None:
    """The number that decided the architecture is reported, with its arithmetic checked.

    Plan §4 asks for the share of originators appearing more than once; DEV-011 records
    the answer, and the dataset card and the EDA notebook must both carry it. The ratio is
    recomputed here from the two counts in the measurement artifact and rounded by hand,
    so a card that quoted a different figure would fail rather than be believed.
    """
    import json

    root = Path(__file__).resolve().parents[2]
    measurement = json.loads((root / "data" / "graph_measurement.json").read_text(encoding="utf-8"))

    n_rows = 6_362_620
    n_distinct_orig = 6_353_307
    assert measurement["n_rows"] == n_rows
    assert measurement["n_distinct_nameOrig"] == n_distinct_orig

    # reuse = 1 - distinct/rows = (6362620 - 6353307) / 6362620 = 9313 / 6362620
    #       = 0.00146374... -> 0.001464 at six places
    expected = round(1 - n_distinct_orig / n_rows, 6)
    assert expected == 0.001464
    assert measurement["reuse_ratio"] == pytest.approx(expected, abs=1e-9)

    # The pass condition and the verdict that follows from failing it.
    assert measurement["thresholds"]["median_counterparty_degree_gt"] == 2.0
    assert measurement["counterparty_degree"]["median"] == 1.0
    assert measurement["verdict"] == "STAR_SHAPED_TRIGGER_DAY4_FALLBACK"

    card = (root / "data" / "DATASET_CARD.md").read_text(encoding="utf-8")
    decisions = (root / "DECISIONS.md").read_text(encoding="utf-8")
    notebook = (root / "notebooks" / "01_eda.ipynb").read_text(encoding="utf-8")
    for document, name in ((card, "data/DATASET_CARD.md"), (decisions, "DECISIONS.md"), (notebook, "notebooks/01_eda.ipynb")):
        assert "0.001464" in document, f"{name} does not state the reuse figure"
        assert "6362620" in document.replace(",", ""), f"{name} omits the row count"


def test_money_parsing_is_exact_across_both_forms(identity: RunIdentity, canonical_kwargs: dict[str, Any]) -> None:
    """Both spellings of the same money produce the same integer.

    A parse that agreed with itself on one form and drifted on the other is the cent-level
    defect that shows up as a reconciliation failure months later, so the scientific repr
    and its plain decimal must land on identical minor units.
    """
    plain = dict(ROWS[0])
    plain["amount"] = "10001912.39"
    scientific = dict(ROWS[0])
    scientific["amount"] = "1.000191239E7"
    # A distinct identity (different amount text) so both rows survive dedup.
    scientific["nameDest"] = "C2100"

    result = _batch([plain, scientific], identity, canonical_kwargs)
    assert result.events.get_column("amount_minor").to_list() == [1000191239, 1000191239]
    assert result.quarantine_count == 0
    assert result.scientific_amounts_expanded == 1


def test_negative_money_and_null_keys_fail_closed(
    identity: RunIdentity, canonical_kwargs: dict[str, Any]
) -> None:
    """A negative balance or a null required column is refused with its reason named.

    Both are "the source changed shape" rather than "a value we can round away": 03 §A
    rule 2, never let an unknown become a zero. A null in a required column would become
    an absent account or an unlabeled row downstream, and absence is the one claim a null
    must not be allowed to make silently.
    """
    negative = dict(ROWS[0])
    negative["newbalanceOrig"] = -1.00
    result = _batch([negative], identity, canonical_kwargs)
    assert result.canonical_count == 0
    assert result.quarantined[0].reason in {ERR_NEGATIVE_MONEY, "negative_balance"}

    nulled = raw_frame([dict(r) for r in ROWS]).with_columns(
        pl.when(pl.col("step") == 1)
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.col("nameOrig"))
        .alias("nameOrig")
    )
    quarantined = canonicalize_batch(nulled, identity, **canonical_kwargs)
    assert quarantined.canonical_count == 0
    assert quarantined.quarantine_count == 1
    assert quarantined.quarantined[0].reason == ERR_NULL_IN_REQUIRED
    assert "nameOrig" in quarantined.quarantined[0].detail

    # The same null, arriving inside an otherwise valid canonical frame, is refused by
    # the contract itself rather than only by the adapter.
    events = _batch([dict(r) for r in ROWS], identity, canonical_kwargs).events
    with_null = events.with_columns(
        pl.when(pl.col("txn_id") == events.get_column("txn_id")[0])
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.col("account_to"))
        .alias("account_to")
    )
    with pytest.raises(CanonicalContractError, match=ERR_NULL_IN_REQUIRED):
        assert_canonical_frame(with_null, persisted=True)


def test_reader_produces_deterministic_batches(tmp_path: Path) -> None:
    """Batch slicing is by exact row count, so a batch id is reproducible.

    ``pl.read_csv_batched`` hands out whatever its worker threads finished parsing --
    measured at 34,545 / 34,044 / 31,451 rows for a request of three equal batches --
    and a boundary that moves between runs moves every batch id derived from it.
    """
    rows = [dict(base_row(step=1 + index // 2, nameOrig=f"C{2000 + index}", nameDest=f"M{2000 + index}",
                         amount=f"{index + 1}.00", oldbalanceOrg=float(index), newbalanceOrig=0.0,
                         oldbalanceDest=0.0, newbalanceDest=float(index))) for index in range(7)]
    path = paysim_csv(tmp_path / "ps.csv", rows)

    batches = read_raw_batches(path, batch_rows=3)
    assert [frame.height for frame in batches] == [3, 3, 1]
    # A limit truncates before the slicing, so the fourth row lands in the second batch.
    assert [frame.height for frame in read_raw_batches(path, batch_rows=3, limit=4)] == [3, 1]
    assert read_raw_batches(path, batch_rows=3, limit=0) == []
    assert read_raw_batches(path, batch_rows=7)[0].height == 7
    # Amount stays a String through the reader, which is what lets the contract inspect
    # the digits before anything multiplies them.
    assert batches[0].collect_schema()["amount"] == pl.String
    assert batches[0].collect_schema()["oldbalanceOrg"] == pl.Float64


def test_channel_and_type_are_derived_by_rule(
    identity: RunIdentity, canonical_kwargs: dict[str, Any]
) -> None:
    """``channel`` comes from a stated rule, because R10 FAST_CASH_OUT is about the rail.

    PaySim has no channel column. CASH_IN and CASH_OUT are the legs that touch an agent's
    physical float; TRANSFER, PAYMENT and DEBIT never leave the app. Deriving it per run
    instead of per rule would let the mapping drift under the rule that reads it.
    """
    assert set(PAYSIM_AGENT_RAIL_TYPES) == {"CASH_IN", "CASH_OUT"}
    assert set(PAYSIM_APP_RAIL_TYPES) == {"TRANSFER", "PAYMENT", "DEBIT"}

    result = _batch([dict(r) for r in ROWS], identity, canonical_kwargs)
    assert result.events.get_column("channel").to_list() == list(EXPECTED_CHANNELS)
    assert result.events.get_column("txn_type").to_list() == ["TRANSFER", "CASH_OUT", "PAYMENT"]
    assert result.events.get_column("currency").unique().to_list() == ["EUR"]


def test_total_order_is_the_sort_key(identity: RunIdentity, canonical_kwargs: dict[str, Any]) -> None:
    """Rows land in ``(event_ts_utc, txn_id)`` order, which is what makes the sort total.

    Every lookback rule and every edge id downstream inherits this order; two rows that tie
    on it would land in whichever order the engine produced, and the same history would
    read differently on the second run.
    """
    result = _batch([dict(r) for r in reversed(ROWS)], identity, canonical_kwargs)
    events = result.events
    keys = list(zip(events.get_column("event_ts_utc").to_list(), events.get_column("txn_id").to_list(), strict=True))
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)

    duplicated = pl.concat([events, events.head(1)])
    with pytest.raises(CanonicalContractError, match="duplicate_total_order"):
        assert_canonical_frame(duplicated, persisted=True)
