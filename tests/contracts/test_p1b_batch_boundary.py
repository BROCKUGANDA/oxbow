"""P1b batch-boundary and artifact tests: manifests, DEV-012 sidecars, DuckDB, one column list.

These are the claims the ingest stage makes about its *outputs* rather than its rows.
A canonical frame that passes every contract check but lands in a file nobody can name,
or a manifest that agrees with nothing, is a phase that cannot be audited -- so each
artifact is written for real here and then read back with a different reader.

The penultimate test pins the seam STATE.md's punch-list item 3 named: the graph used to
carry its own copy of the canonical column list, and that copy was missing ``batch_id``.
Two lists for one contract is how two modules start meaning different things by the same
name, so the graph imports the contract and this test fails if the identity is ever
broken again.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from pandera.errors import SchemaError

from oxbow.adapters.file.canonical_sink import (
    CANONICAL_VIEW,
    QUARANTINE_VIEW,
    CanonicalSink,
    SinkConfigError,
)
from oxbow.adapters.file.source import CsvSourceAdapter, manifest_for_file
from oxbow.contracts.canonical_v1 import (
    CANONICAL_COLUMNS,
    PERSISTED_CANONICAL_COLUMNS,
    SIDECAR_COLUMNS,
)
from oxbow.contracts.raw_paysim import PAYSIM_RAW_COLUMNS, raw_paysim_schema
from oxbow.ingest.canonical import RunIdentity
from oxbow.ingest.paysim import QuarantineRecord, canonicalize_batch
from oxbow.ports.source import BatchManifest
from tests.contracts.conftest import PAYSIM_NAMESPACE, TEST_BATCH_ID, TEST_SALT, base_row, raw_frame

REPO_ROOT = Path(__file__).resolve().parents[2]

DETERMINISM: dict[str, Any] = {
    "parquet_row_group_size": 100_000,
    "parquet_compression": "zstd",
    "parquet_compression_level": 3,
}

# The inbound drop the port describes: columns in the port's own short names, which are
# the documented wire shape and deliberately not the persisted Parquet names (DEV-012).
DROP_HEADER = ",".join(
    (
        "txn_id",
        "ts_utc",
        "src_account",
        "dst_account",
        "amount_minor",
        "currency",
        "channel",
        "txn_type",
        "src_balance_before",
        "src_balance_after",
        "dst_balance_before",
        "dst_balance_after",
        "label_fraud",
        "label_typology",
        "source_dataset",
        "ingested_at",
        "run_id",
    )
)


def drop_row(index: int, *, amount_minor: int, ts_utc: datetime) -> str:
    """One inbound row in the port's shape, with a distinct id and instant per index."""
    return ",".join(
        str(value)
        for value in (
            f"stub:{index}",
            ts_utc.isoformat(),
            f"A{index}",
            f"B{index}",
            amount_minor,
            "EUR",
            "app",
            "TRANSFER",
            100_00,
            0,
            0,
            amount_minor,
            0,
            "",
            "stub",
            "",
            "",
        )
    )


def _manifest(path: Path, *, row_count: int) -> BatchManifest:
    """A declared manifest for the drop: the count is what the test is disagreeing with."""
    start = datetime(2026, 3, 1, 9, 0, 0, tzinfo=UTC)
    return BatchManifest(
        batch_id="drop-0001",
        row_count=row_count,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        window_start=start,
        window_end=start + timedelta(hours=6),
        source_system="stub",
    )


def test_manifest_mismatch_rejects_batch(tmp_path: Path) -> None:
    """A batch is accepted whole or rejected whole: a short drop never enters partially.

    03 §B's argument is that half a day of transactions looks like a quiet day, and a
    quiet day looks like normal behaviour. The check is against the *declared* count, not
    a count taken from the same file, because the failure being prevented is a truncated
    drop and a self-consistent truncated file would otherwise pass.
    """
    path = tmp_path / "drop.csv"
    start = datetime(2026, 3, 1, 9, 0, 0, tzinfo=UTC)
    rows = [
        drop_row(index, amount_minor=10_000 + index, ts_utc=start + timedelta(minutes=index))
        for index in range(3)
    ]
    path.write_text("\n".join([DROP_HEADER, *rows]) + "\n", encoding="utf-8")

    agreed = CsvSourceAdapter(
        source_id="stub",
        path=path,
        run_id="01J6ZXN4T8V3WKQM5RPSGHYBED",
        manifest=_manifest(path, row_count=3),
        root=tmp_path,
    )
    delivered = list(agreed.iter_canonical())
    assert len(delivered) == 3
    assert agreed.quarantine_count() == 0
    assert agreed.read_manifest().row_count == 3
    assert delivered[0]["amount_minor"] == 10_000
    assert isinstance(delivered[0]["amount_minor"], int)

    # A manifest claiming one more row than arrived is a whole-batch rejection, raised
    # before any of it is trusted: the rows already yielded are not committed anywhere.
    short = CsvSourceAdapter(
        source_id="stub",
        path=path,
        run_id="01J6ZXN4T8V3WKQM5RPSGHYBED",
        manifest=_manifest(path, row_count=4),
        root=tmp_path,
    )
    with pytest.raises(ValueError, match="rejected whole"):
        list(short.iter_canonical())

    # The manifest's own internal consistency fails first, for each of the three ways a
    # hand-typed one goes wrong.
    with pytest.raises(ValueError, match="row_count"):
        BatchManifest(
            batch_id="b",
            row_count=-1,
            sha256="0" * 64,
            window_start=start,
            window_end=start,
            source_system="s",
        ).validate()
    with pytest.raises(ValueError, match="window_end precedes"):
        BatchManifest(
            batch_id="b",
            row_count=1,
            sha256="0" * 64,
            window_start=start,
            window_end=start - timedelta(seconds=1),
            source_system="s",
        ).validate()
    with pytest.raises(ValueError, match="sha256"):
        BatchManifest(
            batch_id="b",
            row_count=1,
            sha256="not-a-digest",
            window_start=start,
            window_end=start,
            source_system="s",
        ).validate()

    # A derived manifest measures the file instead of wishing about it, and the derived
    # count is the header-excluded data-row count.
    derived = manifest_for_file(path, source_id="stub", batch_id="drop-0001")
    assert derived.row_count == 3
    assert derived.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    derived.validate()


def test_raw_paysim_schema_is_strict_and_never_coerces() -> None:
    """The declared raw contract is the batch-level gate: strict, non-coercing, specific.

    This is the check the whole corpus passes on every batch before a row is processed.
    ``coerce=False`` is the point of it -- a schema that widened Int64 to Float64 to be
    helpful would turn a money defect into an invisible one.
    """
    frame = raw_frame([base_row(), base_row(step=2, nameOrig="C1001", nameDest="C2001")])
    assert raw_paysim_schema.validate(frame) is not None

    # `SchemaError`, not `Exception`: a refusal raised by anything else — a polars cast
    # error, a KeyError from the adapter — would satisfy a bare `pytest.raises(Exception)`
    # and the test would keep passing after the contract stopped enforcing anything.
    extra = frame.with_columns(pl.lit("x").alias("surprise"))
    with pytest.raises(SchemaError, match="surprise"):
        raw_paysim_schema.validate(extra)

    missing = frame.drop("isFraud")
    with pytest.raises(SchemaError, match="isFraud"):
        raw_paysim_schema.validate(missing)

    # A float-typed amount is refused rather than cast, and the message names the column:
    # coerce=False means a dtype disagreement is a failure, never a conversion.
    widened = frame.with_columns(pl.col("amount").cast(pl.Float64))
    with pytest.raises(SchemaError, match="amount"):
        raw_paysim_schema.validate(widened)

    # Three decimals is not a rounding decision this stage gets to make.
    ragged = frame.with_columns(pl.col("amount").str.replace("9839.64", "9839.641"))
    with pytest.raises(SchemaError):
        raw_paysim_schema.validate(ragged)

    assert list(PAYSIM_RAW_COLUMNS) == frame.columns


def test_persisted_parquet_carries_no_run_scoped_columns(tmp_path: Path) -> None:
    """DEV-012: the Parquet holds deterministic columns; the manifest holds the rest.

    ``run_id`` is a random ULID and ``ingested_at`` is a wall-clock reading. Either one
    inside the data bytes makes two runs of one corpus differ arithmetically, which would
    make ``make verify-determinism`` a request rather than a check. The columns are
    therefore sidecar-carried, and this test reads the landed file back with a different
    reader to prove they are genuinely absent rather than merely unnamed.
    """
    identity = RunIdentity(run_salt=TEST_SALT, batch_id=TEST_BATCH_ID)
    result = canonicalize_batch(
        raw_frame([dict(base_row())]),
        identity,
        deployment_tz=ZoneInfo("Africa/Kampala"),
        epoch_utc=datetime(2014, 1, 1, tzinfo=UTC),
        step_hours=24,
        offset_modulus_us=86_400_000_000,
        offset_salt="oxbow-paysim-step-v1",
        ingested_at=datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC),
    )
    assert result.canonical_count == 1

    sink = CanonicalSink(
        interim_root=tmp_path / "interim",
        duckdb_path=tmp_path / "warehouse.duckdb",
        determinism=DETERMINISM,
    )
    artifact = sink.write_batch(PAYSIM_NAMESPACE, result.batch_id, result.events)
    # `path` is recorded repo-relative so the manifest survives the container/host
    # handoff; the sink resolves it back to something openable on this machine.
    landed_path = sink.resolve(str(artifact["path"]))

    landed = pl.read_parquet(landed_path)
    assert landed.columns == list(PERSISTED_CANONICAL_COLUMNS)
    for column in SIDECAR_COLUMNS:
        assert column not in landed.columns
    assert set(PERSISTED_CANONICAL_COLUMNS) | set(SIDECAR_COLUMNS) == set(CANONICAL_COLUMNS)
    assert artifact["rows"] == 1
    assert artifact["sha256"] == hashlib.sha256(landed_path.read_bytes()).hexdigest()

    manifest_path = Path(
        sink.write_run_manifest(PAYSIM_NAMESPACE, {"run_id": "X", "ingested_at": "Y"})
    )
    assert manifest_path.name == "run_manifest.json"
    # The manifest's name is the marker verify_determinism.py excludes by substring, and
    # that exclusion is the reason the sidecar can live there at all.
    assert "run_manifest" in manifest_path.name


def test_duckdb_views_read_back_the_landed_parquet(tmp_path: Path) -> None:
    """The registered views resolve against the files, and the row count is queried.

    A view that exists in the catalog but cannot be read is the failure this stage would
    otherwise discover in the graph layer three stages later, so the count comes out of a
    ``SELECT`` here rather than being carried in from the ingest counters.
    """
    import duckdb

    identity = RunIdentity(run_salt=TEST_SALT, batch_id=TEST_BATCH_ID)
    rows = [
        dict(base_row()),
        dict(base_row(step=2, nameOrig="C1001", nameDest="C2001", amount="12.00")),
        dict(base_row(step=3, type="CASH_OUT", nameOrig="C1002", nameDest="C2002", amount="7.50")),
    ]
    result = canonicalize_batch(
        raw_frame(rows),
        identity,
        deployment_tz=ZoneInfo("Africa/Kampala"),
        epoch_utc=datetime(2014, 1, 1, tzinfo=UTC),
        step_hours=24,
        offset_modulus_us=86_400_000_000,
        offset_salt="oxbow-paysim-step-v1",
        ingested_at=datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC),
    )
    assert result.canonical_count == 3

    sink = CanonicalSink(
        interim_root=tmp_path / "interim",
        duckdb_path=tmp_path / "warehouse.duckdb",
        determinism=DETERMINISM,
    )
    artifact = sink.write_batch(PAYSIM_NAMESPACE, result.batch_id, result.events)
    quarantine = sink.write_quarantine(
        PAYSIM_NAMESPACE,
        result.batch_id,
        [
            QuarantineRecord(
                reason="test_only",
                source_dataset=PAYSIM_NAMESPACE,
                row_index=7,
                payload={"a": 1},
                detail="d",
            )
        ],
    )
    views = sink.register_views(
        canonical_files={PAYSIM_NAMESPACE: [str(sink.resolve(str(artifact["path"])))]},
        quarantine_files=[quarantine],
    )
    names = {str(view["view"]) for view in views}
    assert {f"{CANONICAL_VIEW}_{PAYSIM_NAMESPACE}", CANONICAL_VIEW, QUARANTINE_VIEW} <= names
    assert all(int(view["rows"]) > 0 for view in views)

    with duckdb.connect(str(sink.duckdb_path)) as connection:
        assert connection.execute(f"SELECT count(*) FROM {CANONICAL_VIEW}").fetchone()[0] == 3
        money = connection.execute(
            f"SELECT typeof(amount_minor) FROM {CANONICAL_VIEW} LIMIT 1"
        ).fetchone()[0]
        assert money.upper() == "BIGINT", f"money reached the query layer as {money}"
        landed = connection.execute(
            f"SELECT count(*) FROM {QUARANTINE_VIEW} WHERE reason = 'test_only'"
        ).fetchone()[0]
        assert landed == 1


def test_sink_honours_the_configured_codec_and_nothing_else(tmp_path: Path) -> None:
    """An unknown compression name fails rather than being quietly substituted.

    The codec, the compression level and the row-group size are byte-level decisions, so
    a writer that picked a sensible default behind the operator's back would change every
    checksum while changing no number -- and ``verify-determinism`` would report a
    determinism problem when the problem was a config typo.
    """
    with pytest.raises(SinkConfigError, match="brotli"):
        CanonicalSink(
            interim_root=tmp_path,
            duckdb_path=tmp_path / "w.duckdb",
            determinism={**DETERMINISM, "parquet_compression": "brotli"},
        )
    with pytest.raises(SinkConfigError, match="missing"):
        CanonicalSink(
            interim_root=tmp_path,
            duckdb_path=tmp_path / "w.duckdb",
            determinism={"parquet_compression": "zstd"},
        )


def test_graph_imports_the_canonical_column_list() -> None:
    """The graph has no copy of the column list, and therefore cannot be missing a column.

    Punch-list item 3: ``graph/events.py`` declared twenty column names of its own while
    the contract declared twenty-one, and the one it dropped was ``batch_id`` -- which is
    the lineage key every artifact is traced by. ``graph`` importing ``contracts`` is
    legal under ``.importlinter`` (contract 1 forbids adapters, not contracts), and
    ``uv run lint-imports`` keeps it that way.
    """
    from oxbow.graph.events import CANONICAL_EVENT_V1_COLUMNS

    assert CANONICAL_EVENT_V1_COLUMNS is CANONICAL_COLUMNS
    assert "batch_id" in CANONICAL_EVENT_V1_COLUMNS
    assert len(CANONICAL_EVENT_V1_COLUMNS) == 21
    assert len(PERSISTED_CANONICAL_COLUMNS) == 19

    # The graph reads a declared subset of the contract, and every name it reads exists.
    from oxbow.graph.events import OPTIONAL_EVENT_COLUMNS, REQUIRED_EVENT_COLUMNS

    for column in (*REQUIRED_EVENT_COLUMNS, *OPTIONAL_EVENT_COLUMNS):
        assert (
            column in CANONICAL_COLUMNS
        ), f"graph reads {column}, which the contract does not declare"

    # The real artifact the graph will be handed round-trips through the persisted shape
    # with the lineage columns intact.
    assert "source_dataset" in PERSISTED_CANONICAL_COLUMNS
    assert "batch_id" in PERSISTED_CANONICAL_COLUMNS
    assert (REPO_ROOT / ".importlinter").is_file()


def test_row_identity_and_payload_split_is_stated_and_used() -> None:
    """Which columns identify a transaction, and why balances are not among them.

    The five identity columns are what makes a conflicting duplicate expressible at all:
    two rows agreeing on them are the same transaction, so any other disagreement is a
    dispute about it rather than a second transaction. Balances and labels are payload
    precisely because they are the columns a duplicate would disagree about.
    """
    from oxbow.ingest.paysim import ROW_IDENTITY_COLUMNS, ROW_PAYLOAD_COLUMNS

    assert ROW_IDENTITY_COLUMNS == ("step", "type", "amount", "nameOrig", "nameDest")
    assert set(ROW_PAYLOAD_COLUMNS) == {
        "oldbalanceOrg",
        "newbalanceOrig",
        "oldbalanceDest",
        "newbalanceDest",
        "isFraud",
        "isFlaggedFraud",
    }
    assert set(ROW_IDENTITY_COLUMNS) | set(ROW_PAYLOAD_COLUMNS) == set(PAYSIM_RAW_COLUMNS)
