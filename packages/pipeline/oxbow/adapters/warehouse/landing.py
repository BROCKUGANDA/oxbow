"""Shape a landed score run into the warehouse handoff rows the API reads.

This is the seam that was missing. ``ports/warehouse.py`` declares 22 handoff tables and
``apps/api/readmodel.py`` reads them — the queue is ``score`` joined to ``account`` joined to
``economics`` in one statement, and the case rail reads ``rule_hit`` and ``shap_contribution``.
Nothing in the repository had ever *written* ``score`` or ``account``, in either sink: the CLI's
score stage lands Parquet under ``out/score/<run>/`` and stops, and the fifth declared pipeline
stage, ``warehouse``, has no runner (``apps/api/worker.py:639`` says so in a docstring). Live
mode therefore had no rows to load, which is the root cause behind the P8 spec that reports
panes "never leave their loading state" — the route answered 200 because the read model
correctly said *there are none*.

Two rules shape every function here:

**A required column with no measurement refuses the row.** ``score.observed_rate`` and
``score.calibration_n`` are NOT NULL because a confidence figure without its ``n`` is an
adjective, and when a fold's calibration was refused there is no ``n`` — the row's own
``band_n`` is 0 and ``band_observed_rate`` is NaN. Writing ``0.0`` would be the fabrication the
plan's §18 calls a lie, and writing NaN into a NOT NULL double column would smuggle it into
arithmetic. So the row is refused, counted, and the reason is reported with the count. The fix
is named in the message: fit a fold that clears ``min_positives_for_calibration`` — which is
exactly what DEV-024's larger slice is for.

**Column names are the table's, not the frame's.** The scored frame carries feature columns and
model columns mixed; ``SCORE_FIELDS`` is the whitelist, so a renamed model column fails loudly
here instead of landing a row with a null in a column Postgres will accept as null.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final

import polars as pl

from oxbow.adapters.warehouse.models import ACCOUNT_KEY_LEN, RUN_ID_LEN
from oxbow.ports.warehouse import assert_run_id


class LandingError(ValueError):
    """The run cannot be landed as described, with the reason a reviewer can act on."""


#: ``account`` is a measurement over transactions: counts and money, no judgement. Everything
#: comes from the canonical event frame; nothing here reads a model output.
ACCOUNT_COLUMNS: Final = (
    "account_key",
    "source_dataset",
    "first_seen_at",
    "last_seen_at",
    "txn_count",
    "n_outbound",
    "n_inbound",
    "n_counterparties",
    "funding_minor",
    "currency",
    "age_days",
)

#: The ``score`` columns this stage can populate, and the scored-frame column each one is read
#: from. The two vocabularies differ on six of fifteen fields — the frame says `p_fused`,
#: `score_points`, `band_observed_rate`, `band_n`, `label_typology` — so the mapping is the
#: single source of truth and a rename on either side fails here with the column named, rather
#: than landing a null into a column Postgres would accept.
#: ``None`` means "derived from other columns", not "missing".
SCORE_SOURCES: Final[dict[str, str | None]] = {
    "account_key": "account_key",
    "fused_score": "p_fused",
    "band": "band",
    "scorecard_points": "score_points",
    "p_scorecard": "p_scorecard",
    "p_gbm": "p_gbm",
    "anomaly_norm": "anomaly_norm",
    # The fused probability IS the calibrated one whenever the fold calibrated, and the fold
    # has already been refused above if it did not (see `calibrated`/`band_n` handling).
    "calibrated_probability": "p_fused",
    # `band_observed_rate`/`band_n` are the rate measured *within the band*, so the band that
    # carries them is the band the column names. Two different names for one thing across the
    # seam; the alternative — inventing a tag the frame does not carry — would be worse.
    "calibration_band": "band",
    "observed_rate": "band_observed_rate",
    "calibration_n": "band_n",
    "predicted_typology": "label_typology",
    "reason_codes": "reason_codes",
    "rule_ids": None,  # derived from the rule_*_severity columns
    "model_version": "model_version",
}

SCORE_COLUMNS: Final = tuple(SCORE_SOURCES)

_BANDS: Final = frozenset("ABCDE")


def _pairs(events: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """One frame per direction, keyed on the side being described.

    ``account_from``/``account_to`` are the two ends of one event, so an account is both a
    sender and a receiver and the counts have to be taken from each side separately — a single
    group-by over a coalesced key is how ``n_outbound`` silently becomes "transactions this
    account appears in", which is neither side's count.
    """
    out = events.select(
        pl.col("account_from").alias("account_key"),
        pl.col("account_to").alias("counterparty"),
        pl.col("amount_minor"),
        pl.col("event_ts_utc"),
        pl.col("source_dataset"),
        pl.col("currency"),
        pl.lit(1).alias("_sent"),
        pl.lit(0).alias("_received"),
    )
    inc = events.select(
        pl.col("account_to").alias("account_key"),
        pl.col("account_from").alias("counterparty"),
        pl.col("amount_minor"),
        pl.col("event_ts_utc"),
        pl.col("source_dataset"),
        pl.col("currency"),
        pl.lit(0).alias("_sent"),
        pl.lit(1).alias("_received"),
    )
    return out, inc


def account_rows(events: pl.DataFrame) -> list[dict[str, Any]]:
    """One ``account`` row per account that appears on either side of an event.

    ``funding_minor`` is money *received*: the minor units that arrived from another account. It
    is not the balance, and it is not the sum of both directions — an account that pays out more
    than it takes in is not "funded" by the difference.
    """
    required = {"account_from", "account_to", "amount_minor", "event_ts_utc", "currency"}
    missing = required - set(events.columns)
    if missing:
        raise LandingError(
            f"the event frame is missing {sorted(missing)}; cannot describe accounts"
        )

    out, inc = _pairs(events)
    both = pl.concat([out, inc])
    grouped = both.group_by("account_key").agg(
        pl.len().alias("txn_count"),
        pl.col("_sent").sum().alias("n_outbound"),
        pl.col("_received").sum().alias("n_inbound"),
        pl.col("counterparty").n_unique().alias("n_counterparties"),
        pl.col("event_ts_utc").min().alias("first_seen_at"),
        pl.col("event_ts_utc").max().alias("last_seen_at"),
        # Received-side money only: the sent rows carry a zero here so the sum is inbound.
        (pl.col("amount_minor") * pl.col("_received")).sum().alias("funding_minor"),
        pl.col("source_dataset").first().alias("source_dataset"),
        pl.col("currency").mode().first().alias("currency"),
    )
    grouped = grouped.with_columns(
        ((pl.col("last_seen_at") - pl.col("first_seen_at")).dt.total_days().cast(pl.Int64)).alias(
            "age_days"
        )
    )

    rows: list[dict[str, Any]] = []
    for record in grouped.sort("account_key").to_dicts():
        row = {column: record.get(column) for column in ACCOUNT_COLUMNS}
        absent = [
            column for column in ACCOUNT_COLUMNS if column != "age_days" and row.get(column) is None
        ]
        if absent:
            raise LandingError(
                f"account {record.get('account_key')!r} has no measurement for {absent}; "
                "a NOT NULL column filled from a null group would be an invented fact"
            )
        rows.append(row)
    return rows


#: The frame's severity columns are `rule_r4_cycle_severity`, so the rule id is the leading
#: `rNN` and the rest of the name is the typology it describes. Slicing the affixes off would
#: yield `R4_CYCLE`, which is not a rule id anything else in the system recognises.
_RULE_COLUMN: Final = re.compile(r"^rule_(r\d+)_.*_severity$")


def _rule_id(column: str) -> str | None:
    """`rule_r4_cycle_severity` -> `R4`, or None when the column is not a rule's."""
    match = _RULE_COLUMN.match(column)
    return match.group(1).upper() if match else None


def _rule_ids(row: Mapping[str, Any]) -> list[str]:
    """The rules that fired, from the severity columns the frame carries."""
    fired: list[str] = []
    for column, value in row.items():
        rule_id = _rule_id(str(column))
        if rule_id is None:
            continue
        if value is not None and float(value) > 0.0:
            fired.append(rule_id)
    return sorted(fired)


def _load_json(value: Any, *, column: str, account_key: str) -> Any:
    if value is None:
        return None
    if isinstance(value, list | dict):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise LandingError(
            f"{column} for account {account_key!r} is not parseable JSON: {str(exc)[:80]}"
        ) from exc


def _current_score_per_account(part: pl.DataFrame) -> pl.DataFrame:
    """One row per account: the latest fold's latest as-of, which is the run's current score.

    ``score`` is ``UNIQUE (run_id, account_key)`` and carries no fold or as-of column, so the
    table's own declaration says an account has one score per run. A walk-forward run scores an
    account again in every fold it is still active in — measured on the landed 40k run: 43,720
    test rows over 43,046 accounts, with 359 accounts scored in two folds, so 674 rows would
    collide. Same grain finding as DEV-026, at the warehouse boundary instead of the ledger.

    The later fold wins because it is the later information. An account whose current row is
    refused is NOT replaced by an older fold's calibrated number: that would put a stale score
    in the queue and let the reader believe it is the live one. The refusal is reported by
    account instead, in the caller.
    """
    missing = [name for name in ("fold", "as_of_ts") if name not in part.columns]
    if missing:
        raise LandingError(
            f"the scored frame has no {missing} column(s), so 'the latest scoring pass for each "
            "account' cannot be resolved and one account would land as several current scores"
        )
    ordered = part.with_row_index("_landing_row").sort(
        ["account_key", "fold", "as_of_ts", "_landing_row"]
    )
    kept = ordered.unique(subset=["account_key"], keep="last")
    return kept.sort("_landing_row").drop("_landing_row")


def score_rows(
    scored: pl.DataFrame, *, role: str = "test"
) -> tuple[list[dict[str, Any]], list[str]]:
    """Landable ``score`` rows, and the refusals that explain the rest.

    Only one role per run may be scored against: ``test`` rows are the ones the model did not
    see. Landing ``train`` rows too would put a fitted-on number in the table the API reads as a
    current score, and the queue would rank accounts by how well the model memorised them.
    """
    if "role" not in scored.columns:
        raise LandingError(
            "the scored frame carries no `role` column, so no row can be trusted "
            "to be out-of-sample"
        )
    part = scored.filter(pl.col("role") == role)
    if part.height == 0:
        raise LandingError(f"no scored rows with role={role!r}; nothing out-of-sample to land")

    unmapped = sorted(
        {
            source
            for source in SCORE_SOURCES.values()
            if source is not None and source not in part.columns
        }
    )
    if unmapped:
        raise LandingError(
            f"the scored frame cannot populate {unmapped}; the run was produced by a model "
            "stack whose output columns this loader does not recognise"
        )

    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    for record in _current_score_per_account(part).to_dicts():
        account_key = str(record.get("account_key", ""))
        observed_rate = record.get("band_observed_rate")
        calibration_n = record.get("band_n")
        band = record.get("band")
        calibrated_probability = record.get("p_fused")

        problems: list[str] = []
        if band not in _BANDS:
            problems.append(f"band={band!r} is not one of A-E")
        if calibrated_probability is None:
            problems.append("p_fused is absent")
        if calibration_n is None or int(calibration_n) <= 0:
            problems.append(
                f"calibration_n={calibration_n!r}: the fold's calibration was refused "
                f"({str(record.get('uncalibrated_reason') or 'no reason recorded')[:72]}), so "
                "there is no population behind the rate and none can be stored"
            )
        elif observed_rate is None or observed_rate != observed_rate:  # NaN is not a measurement
            problems.append("band_observed_rate is NaN while band_n is non-zero")
        if problems:
            refused.append(f"account {account_key[:ACCOUNT_KEY_LEN]}: " + "; ".join(problems))
            continue

        reason_codes = record.get("reason_codes") or []
        rows.append(
            {
                "account_key": account_key,
                "fused_score": float(record["p_fused"]),
                "band": str(band),
                "scorecard_points": int(record["score_points"]),
                "p_scorecard": record.get("p_scorecard"),
                "p_gbm": record.get("p_gbm"),
                "anomaly_norm": record.get("anomaly_norm"),
                "calibrated_probability": float(record["p_fused"]),
                "calibration_band": str(band),
                "observed_rate": float(observed_rate),
                "calibration_n": int(calibration_n),
                "predicted_typology": record.get("label_typology"),
                "reason_codes": (
                    reason_codes
                    if isinstance(reason_codes, list)
                    else _load_json(reason_codes, column="reason_codes", account_key=account_key)
                    or []
                ),
                "rule_ids": _rule_ids(record),
                "model_version": str(
                    record.get("model_version") or record.get("model_fingerprint") or ""
                )[:128],
            }
        )

    if rows and len(refused) == len(part):  # pragma: no cover - defensive, rows implies passes
        raise LandingError("score_rows returned nothing while refusing every row")
    return rows, refused


def rule_hit_rows(scored: pl.DataFrame, *, role: str = "test") -> list[dict[str, Any]]:
    """One ``rule_hit`` row per (account, rule) the run evaluated, fired or not.

    A row is kept when the rule did not fire as well as when it did: the case rail's
    "R4 did not fire, here is the margin" is evidence, and dropping non-firing hits would
    leave the absence unrecorded rather than measured.

    Collapsed across folds, because the table's unique key is (run_id, account_key, rule_id)
    and an account is scored in more than one fold's test window. The stored severity is the
    maximum over the folds — the strongest evidence about the account, which is what the rail
    shows — with the fold count alongside so "one fold said 3" and "four folds said 3" are not
    the same claim. Emitting one row per fold instead makes the landing fail on its own
    duplicate key, which is how this was found.
    """
    part = scored.filter(pl.col("role") == role)
    severity_columns = [column for column in part.columns if _rule_id(str(column)) is not None]
    if not severity_columns:
        return []

    collapsed: dict[tuple[str, str], dict[str, Any]] = {}
    for record in part.sort(["account_key", "fold"]).to_dicts():
        account_key = str(record.get("account_key"))
        for column in severity_columns:
            rule_id = str(_rule_id(str(column)))
            value = record.get(column)
            severity = None if value is None else float(value)
            fired = severity is not None and severity > 0.0
            key = (account_key, rule_id)
            existing = collapsed.get(key)
            if existing is None:
                collapsed[key] = {
                    "account_key": account_key,
                    "rule_id": rule_id,
                    # `rule_name` is NOT NULL and the scored frame carries only the id, so the
                    # id is stored twice rather than inventing a display name here. The name a
                    # human reads comes from `config/rules.yaml`, the single place it exists;
                    # copying it into a data row is how the two drift.
                    "rule_name": rule_id,
                    "typology": str(record.get("label_typology") or "unlabelled"),
                    "fired": fired,
                    "detail": {
                        "severity": severity,
                        "folds": 1,
                        "folds_fired": 1 if fired else 0,
                        "as_of_ts": str(record.get("as_of_ts")),
                    },
                }
                continue
            detail = existing["detail"]
            detail["folds"] = int(detail["folds"]) + 1
            detail["folds_fired"] = int(detail["folds_fired"]) + (1 if fired else 0)
            if severity is not None and (
                detail["severity"] is None or severity > detail["severity"]
            ):
                detail["severity"] = severity
                existing["typology"] = str(record.get("label_typology") or "unlabelled")
            existing["fired"] = bool(existing["fired"] or fired)

    return sorted(collapsed.values(), key=lambda row: (row["account_key"], row["rule_id"]))


def assert_run_identifiable(run_id: str) -> str:
    """The run id is the partition key of every table here, so it is checked once, up front."""
    if len(run_id) != RUN_ID_LEN:
        raise LandingError(f"run id {run_id!r} is not {RUN_ID_LEN} characters")
    return assert_run_id(run_id.strip().upper())


__all__ = [
    "ACCOUNT_COLUMNS",
    "SCORE_COLUMNS",
    "SCORE_SOURCES",
    "LandingError",
    "account_rows",
    "assert_run_identifiable",
    "rule_hit_rows",
    "score_rows",
]
