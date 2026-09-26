"""Shared PaySim fixtures for the contract suite.

THE ORACLE IS NOT THE CODE UNDER TEST. ``account_key``, ``txn_id`` and the intra-step
offset are digests nobody can compute mentally, so this module re-implements each one
from the scheme its docstring states -- ``hmac.new(salt, f"{salt}|{name}", sha256)``
truncated to 48 bits, and so on -- using ``hashlib`` and ``hmac`` directly and never
``oxbow.ingest.canonical``. A test that compared ``account_key`` against ``account_key``
would pass if the salt were dropped, which is the one change that must never be silent
(00 B, and the same discipline ``tests/unit/test_p1b_ibm_aml.py`` applies to IBM).

Every other expected value in the suite is a literal written by hand: row counts, minor
units (``9839.64`` to ``983964``), and the hour a UTC instant becomes in Africa/Kampala.

The step-expansion parameters are read from ``config/pipeline.yaml`` because that file
*is* the rule under test; ``test_step_expansion_stable`` re-asserts the literals it was
given so the fixture cannot drift from the config it reflects.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_YAML = REPO_ROOT / "config" / "pipeline.yaml"

TEST_SALT = "oxbow-p1b-contract-salt-0001"
TEST_BATCH_ID = "0123456789ab"
RUN_ID = "01J6ZXN4T8V3WKQM5RPSGHYBED"
PAYSIM_NAMESPACE = "paysim"

# The five columns a PaySim transaction identity is made of, restated here from the
# adapter's own rule: the id is an HMAC over exactly these, which is what makes a
# conflicting duplicate expressible at all.
IDENTITY_COLUMNS: tuple[str, ...] = ("step", "type", "amount", "nameOrig", "nameDest")
PAYLOAD_COLUMNS: tuple[str, ...] = (
    "oldbalanceOrg",
    "newbalanceOrig",
    "nameDest",
    "oldbalanceDest",
    "newbalanceDest",
    "isFraud",
    "isFlaggedFraud",
)
RAW_HEADER: tuple[str, ...] = (
    "step",
    "type",
    "amount",
    "nameOrig",
    "oldbalanceOrg",
    "newbalanceOrig",
    "nameDest",
    "oldbalanceDest",
    "newbalanceDest",
    "isFraud",
    "isFlaggedFraud",
)


def _hmac_hex(message: str, key: str) -> str:
    """Independent SHA-256 HMAC, lowercase hex: the primitive the oracle is built from."""
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def oracle_account_key(name: str, salt: str = TEST_SALT) -> str:
    """HMAC-SHA256 of ``"<salt>|<name>"`` keyed by the salt, truncated to 12 hex chars."""
    return _hmac_hex(f"{salt}|{name}", salt)[:12]


def row_key(step: int, txn_type: str, amount: str, name_orig: str, name_dest: str) -> str:
    """The content key a PaySim row's identity is derived from, pipe-joined in file order."""
    return f"{step}|{txn_type}|{amount}|{name_orig}|{name_dest}"


def oracle_txn_id(key: str) -> str:
    """``paysim:`` plus the first 16 hex of an HMAC keyed by the corpus namespace.

    Keyed by the namespace rather than the run salt so a salt rotation re-keys accounts
    without re-labelling six million transactions.
    """
    return f"{PAYSIM_NAMESPACE}:{_hmac_hex(key, PAYSIM_NAMESPACE)[:16]}"


def oracle_offset_us(txn_id: str, offset_salt: str, modulus_us: int) -> int:
    """The deterministic intra-step microsecond offset for one transaction id."""
    return int(_hmac_hex(f"{offset_salt}|{txn_id}", offset_salt)[:16], 16) % modulus_us


def oracle_instant(
    step: int,
    key: str,
    *,
    epoch: datetime,
    step_hours: int,
    offset_salt: str,
    modulus_us: int,
) -> datetime:
    """The UTC instant a row expands to: epoch + step days + its own offset."""
    txn_id = oracle_txn_id(key)
    offset = oracle_offset_us(txn_id, offset_salt, modulus_us)
    return epoch + timedelta(hours=step_hours * step) + timedelta(microseconds=offset)


def oracle_batch_id(ordinal: int, keys: list[str]) -> str:
    """The content-derived 12-hex batch id for one batch's surviving row keys, in order."""
    digest = hashlib.sha256()
    digest.update(f"{PAYSIM_NAMESPACE}|{ordinal}".encode())
    for key in keys:
        digest.update(key.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:12]


@pytest.fixture(scope="session")
def expansion() -> dict[str, Any]:
    """The step-expansion rule exactly as the deployment config states it."""
    raw = yaml.safe_load(PIPELINE_YAML.read_text(encoding="utf-8"))
    paysim = raw["paysim"]
    return {
        "epoch": datetime.fromisoformat(str(paysim["epoch_utc"]).replace("Z", "+00:00")).astimezone(
            UTC
        ),
        "step_hours": int(paysim["step_hours"]),
        "modulus_us": int(paysim["intra_step_offset"]["modulus_us"]),
        "offset_salt": str(paysim["intra_step_offset"]["salt"]),
        "timezone": str(raw["deployment_timezone"]),
        "future_tolerance_hours": int(raw["ingest"]["future_timestamp_tolerance_hours"]),
    }


@pytest.fixture(scope="session")
def deployment_tz(expansion: dict[str, Any]) -> ZoneInfo:
    return ZoneInfo(str(expansion["timezone"]))


def base_row(**overrides: Any) -> dict[str, Any]:
    """One legitimate PaySim row, with the columns in the file's own order.

    The defaults are a TRANSFER of 9839.64 (983964 minor units) from C1000 to C2000 on
    step 1, legitimate and unflagged. Every test overrides the one field it is about,
    so a failure says which field broke rather than which fixture was rewritten.
    """
    row: dict[str, Any] = {
        "step": 1,
        "type": "TRANSFER",
        "amount": "9839.64",
        "nameOrig": "C1000",
        "oldbalanceOrg": 10000.00,
        "newbalanceOrig": 160.36,
        "nameDest": "C2000",
        "oldbalanceDest": 0.00,
        "newbalanceDest": 0.00,
        "isFraud": 0,
        "isFlaggedFraud": 0,
    }
    row.update(overrides)
    return row


def raw_frame(
    rows: list[dict[str, Any]], *, columns: tuple[str, ...] | None = None
) -> pl.DataFrame:
    """A raw batch frame with the reader's dtypes: amount String, balances Float64."""
    header = list(columns or RAW_HEADER)
    frame = pl.DataFrame({name: [row.get(name) for row in rows] for name in header})
    return frame.with_columns(
        pl.col("step").cast(pl.Int64),
        pl.col("isFraud").cast(pl.Int64),
        pl.col("isFlaggedFraud").cast(pl.Int64),
        pl.col("amount").cast(pl.String),
        *[
            pl.col(name).cast(pl.Float64)
            for name in ("oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest")
            if name in header
        ],
    )


def paysim_csv(
    path: Path, rows: list[dict[str, Any]], *, header: tuple[str, ...] | None = None
) -> Path:
    """Write rows as a raw PaySim CSV, header spelled exactly as the real file spells it."""
    columns = list(header or RAW_HEADER)
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(_cell(row[name]) for name in columns))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _cell(value: Any) -> str:
    """Render one cell the way the corpus does: floats keep their two decimals."""
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


@pytest.fixture
def identity() -> Any:
    """A ``RunIdentity`` with a fixed salt, so keys are comparable across runs."""
    from oxbow.ingest.canonical import RunIdentity

    return RunIdentity(run_salt=TEST_SALT, batch_id=TEST_BATCH_ID, run_id=RUN_ID)


@pytest.fixture
def canonical_kwargs(expansion: dict[str, Any], deployment_tz: ZoneInfo) -> dict[str, Any]:
    """The keyword arguments every ``canonicalize_batch`` call in the suite shares."""
    return {
        "deployment_tz": deployment_tz,
        "epoch_utc": expansion["epoch"],
        "step_hours": expansion["step_hours"],
        "offset_modulus_us": expansion["modulus_us"],
        "offset_salt": expansion["offset_salt"],
        "future_tolerance_hours": expansion["future_tolerance_hours"],
    }
