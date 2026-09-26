"""Loaders for the golden fixture - the ONLY sanctioned way to read it.

CSV plus strict-schema loader rather than a parquet blob because a
hand-verified ground truth must be diffable in review: a reviewer checking
"does golden:000057 send 460000 within 40 minutes" should open the file, not
decode a columnar binary. The loader pins every money column to Int64, so a
float ever reaching a money cell is a hard parse error, not a silent
coercion (01 SB money rule, DECISIONS DEV-005).

Public surface for the rules engine (P3b):

    load_golden_transactions() -> polars.DataFrame   # 534 rows, canonical v1
    load_golden_expectations() -> dict                # expected.yaml as data

The frame arrives in the pipeline's total order (event_ts_utc, txn_id) with
event_ts_utc materialised as a tz-aware UTC datetime and ingested_at as a
string (parsing it into the frame would make two columns of the same instant
and invite a comparison that is never the point).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
import yaml

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "transactions.csv"
EXPECTED_PATH = HERE / "expected.yaml"
MANIFEST_PATH = HERE / "build_manifest.json"

GOLDEN_COLUMNS: tuple[str, ...] = (
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

MONEY_COLUMNS: tuple[str, ...] = (
    "amount_minor",
    "src_balance_before_minor",
    "src_balance_after_minor",
    "dst_balance_before_minor",
    "dst_balance_after_minor",
)

INT_COLUMNS: tuple[str, ...] = (*MONEY_COLUMNS, "local_hour", "label_is_fraud", "label_is_flagged")


def load_golden_transactions(path: Path | None = None) -> pl.DataFrame:
    """Read transactions.csv with the pinned strict schema.

    No inference, no coercion: every non-money column is read as Utf8 and the
    five money columns plus the two small integers as Int64. A float or empty
    in any money cell raises, which is the point.
    """
    csv_path = path or CSV_PATH
    schema = {c: pl.Utf8 for c in GOLDEN_COLUMNS}
    for c in INT_COLUMNS:
        schema[c] = pl.Int64
    frame = pl.read_csv(csv_path, schema=schema, infer_schema_length=0)
    if tuple(frame.columns) != GOLDEN_COLUMNS:
        raise ValueError(f"golden columns drifted: {frame.columns}")
    return frame.with_columns(
        pl.col("event_ts_utc").str.to_datetime(format="%Y-%m-%dT%H:%M:%S%.f%:z")
    ).sort(["event_ts_utc", "txn_id"])


def load_golden_expectations(path: Path | None = None) -> dict[str, Any]:
    """Read expected.yaml as a plain dict (schema_version golden-v1.0)."""
    return yaml.safe_load((path or EXPECTED_PATH).read_text(encoding="utf-8"))


def load_build_manifest(path: Path | None = None) -> dict[str, Any]:
    """Read build_manifest.json: seed, row count, sha256, scenario -> rows."""
    return json.loads((path or MANIFEST_PATH).read_text(encoding="utf-8"))


def txn_ids(ordinals: list[int]) -> list[str]:
    """Map the integer row ordinals used in expected.yaml to txn_id strings."""
    return [f"golden:{o:06d}" for o in ordinals]
