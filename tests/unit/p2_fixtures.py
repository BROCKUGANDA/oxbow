"""Shared fixtures for the P2 feature-layer tests.

EVERY EXPECTED NUMBER IN THE P2 TESTS IS COMPUTED BY HAND from one of the frames built
here, and the hand computation is written next to the assertion that uses it. 00 §B: a
fixture whose expected value came from the code it tests proves nothing — which is the
exact failure mode plan §8 spends its whole gate on. Nothing in this module calls the
feature layer.

THE FRAMES ARE SMALL AND SHAPED ON PURPOSE. A leakage defect, a money defect and a
boundary-straddle defect are all visible in eight rows if the rows are placed at the
distances that trigger them; a hundred thousand rows hide all three.

TIME. ``Africa/Kampala`` is UTC+3 year-round (``config/pipeline.yaml``
``deployment_timezone``), so ``local = utc + 3h`` here is the same arithmetic the ingest
performs, written out rather than imported so a drift between the two shows up as a wrong
hand-computed value rather than as a self-consistent pair of bugs.

CANONICAL SHAPE. Frames carry all twenty-one canonical-v1 columns with the contract's own
dtype map, because ``features.build.assert_input_contract`` refuses anything else — and
refusing a malformed fixture would be the good outcome. ``run_id`` and ``ingested_at`` are
present because they are members of the in-memory contract (DEV-012 keeps them out of the
persisted bytes, not out of the frame).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from oxbow.contracts.canonical_v1 import CANONICAL_COLUMNS, CANONICAL_DTYPES

BASE: datetime = datetime(2024, 1, 1, tzinfo=UTC)
LOCAL_OFFSET: timedelta = timedelta(hours=3)

# Account keys are 12 lowercase hex characters (canonical_v1._account_keys_wellformed).
ALICE: str = "a11ce0000000"
BOB: str = "b0b000000000"
CAROL: str = "c0c500000000"
DAVE: str = "d0de00000000"
MALL: str = "ma1100000000"
CASH: str = "c05400000000"
SOLO: str = "501000000000"

UGX: str = "UGX"
KES: str = "KES"

#: Default balances for a hand-built row: sender holds 1000.00, receiver starts empty.
DEFAULT_SENDER_BALANCE: int = 100_000
DEFAULT_RECEIVER_BALANCE: int = 0


def event(
    index: int,
    src: str,
    dst: str,
    amount_minor: int,
    offset: timedelta,
    *,
    txn_type: str = "TRANSFER",
    channel: str = "APP",
    currency: str = UGX,
    sender_balance: int = DEFAULT_SENDER_BALANCE,
    receiver_balance: int = DEFAULT_RECEIVER_BALANCE,
    fraud: int = 0,
    flagged: int = 0,
    typology: str | None = None,
    ingested_at: datetime | None = None,
    batch_id: str = "0123456789ab",
    source_dataset: str = "paysim",
) -> dict[str, Any]:
    """One canonical event as a mutable mapping, with the money identity kept true.

    ``sender_balance``/``receiver_balance`` are the balances *before* the event and the
    after-values are derived from them plus the amount, so a hand-built row satisfies
    ``balance_after - balance_before == sign * amount`` unless a test deliberately breaks
    it through :func:`with_balances`.
    """
    moment = BASE + offset
    local = moment + LOCAL_OFFSET
    row: dict[str, Any] = {
        "txn_id": f"{source_dataset}:{index}",
        "event_ts_utc": moment,
        "event_date_local": local.date(),
        "local_hour": local.hour,
        "txn_type": txn_type,
        "channel": channel,
        "amount_minor": amount_minor,
        "currency": currency,
        "account_from": src,
        "account_to": dst,
        "src_balance_before_minor": sender_balance,
        "src_balance_after_minor": sender_balance - amount_minor,
        "dst_balance_before_minor": receiver_balance,
        "dst_balance_after_minor": receiver_balance + amount_minor,
        "label_is_fraud": fraud,
        "label_is_flagged": flagged,
        "label_typology": typology,
        "source_dataset": source_dataset,
        "ingested_at": ingested_at if ingested_at is not None else moment,
        "run_id": "01HQX2Y3Z4A5B6C7D8E9F0G1H2",
        "batch_id": batch_id,
    }
    return row


def with_balances(
    row: dict[str, Any],
    *,
    src_before: int,
    src_after: int,
    dst_before: int,
    dst_after: int,
) -> dict[str, Any]:
    """Overwrite a row's four balances, for the deliberately-inconsistent PaySim case."""
    row = dict(row)
    row["src_balance_before_minor"] = src_before
    row["src_balance_after_minor"] = src_after
    row["dst_balance_before_minor"] = dst_before
    row["dst_balance_after_minor"] = dst_after
    return row


def canonical_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Assemble canonical-v1 columns and dtypes from :func:`event` mappings."""
    columns: dict[str, pl.Series] = {}
    for name in CANONICAL_COLUMNS:
        values = [row.get(name) for row in rows]
        columns[name] = pl.Series(name, values, dtype=CANONICAL_DTYPES[name])
    frame = pl.DataFrame(columns)
    return frame.sort(["event_ts_utc", "txn_id"])


def timeline(days: int = 1, hours: int = 0) -> timedelta:
    """``days``/``hours`` after :data:`BASE`, spelled out at each call site."""
    return timedelta(days=days, hours=hours)


def as_local_date(moment: datetime) -> date:
    """The deployment-timezone calendar date of a UTC instant (UTC+3, no DST)."""
    return (moment.astimezone(UTC) + LOCAL_OFFSET).date()


_REAL_SLICE_CACHE: dict[int, pl.DataFrame] = {}


def with_sidecar_columns(
    events: pl.DataFrame, *, ingested_at: datetime, run_id: str
) -> pl.DataFrame:
    """Re-attach the two DEV-012 sidecar columns to a persisted canonical frame.

    ``ingest_paysim`` returns the *persisted* nineteen-column shape on purpose — run id and
    ingest instant live in the batch manifest so that two runs of the same bytes can be
    compared at all (DEV-012). ``features.build.assert_input_contract`` demands the
    twenty-one-column in-memory contract, because the sealed-window rule reads
    ``ingested_at``. Nothing in the ingest layer bridges the two, so this test helper does
    the bridging from values the caller already passed in — the same shape
    ``_load_canonical_events(..., with_sidecar=True)`` hands the builder at runtime.
    """
    return events.with_columns(
        pl.lit(ingested_at, dtype=pl.Datetime("us", UTC)).alias("ingested_at"),
        pl.lit(run_id, dtype=pl.String).alias("run_id"),
    )


def paysim_slice(limit: int) -> pl.DataFrame:
    """The first ``limit`` PaySim rows, canonicalised and contract-shaped.

    The head of the file is PaySim's own ``step`` order, so a slice by row count is a
    slice by time and is reproducible without a seed. Cached per size because several
    tests want the same slice and the ingest is the slow part of the gate measurement.
    """
    cached = _REAL_SLICE_CACHE.get(limit)
    if cached is not None:
        return cached
    from oxbow.config import find_repo_root, load_pipeline_config
    from oxbow.ingest.canonical import RunIdentity
    from oxbow.ingest.paysim import ingest_paysim

    root = find_repo_root()
    path = root / "data" / "raw" / "paysim" / "PS_20174392719_1491204439457_log.csv"
    if not path.exists():  # pragma: no cover - the gate is measured, not assumed
        raise AssertionError(
            f"the PaySim corpus is absent at {path}; plan §8's gate is unmeasurable"
        )
    config = load_pipeline_config(root)
    paysim = config.raw["paysim"]
    assert isinstance(paysim, dict)
    offset = paysim["intra_step_offset"]
    assert isinstance(offset, dict)
    epoch = datetime.fromisoformat(str(paysim["epoch_utc"]).replace("Z", "+00:00")).astimezone(UTC)
    result = ingest_paysim(
        path,
        RunIdentity(
            run_salt="oxbow-p2-test-slice-salt",
            batch_id="0123456789ab",
            run_id="01HQX2Y3Z4A5B6C7D8E9F0G1H2",
        ),
        deployment_tz=_deployment_tz(config),
        epoch_utc=epoch,
        step_hours=int(paysim["step_hours"]),
        offset_modulus_us=int(offset["modulus_us"]),
        offset_salt=str(offset["salt"]),
        limit=limit,
        ingested_at=datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC),
        use_process_pool=False,
    )
    events = result.events
    assert events.height == limit, f"the slice returned {events.height} rows, not {limit}"
    events = with_sidecar_columns(
        events,
        ingested_at=datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC),
        run_id="01HQX2Y3Z4A5B6C7D8E9F0G1H2",
    )
    _REAL_SLICE_CACHE[limit] = events
    return events


def _deployment_tz(config: Any) -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo(str(config.raw["deployment_timezone"]))
