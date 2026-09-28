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

**A required column with no measurement refuses the row.** ``score.band`` and
``score.fused_score`` are NOT NULL because a queue cannot position an account without them,
and a row missing one is refused, counted, and the reason reported with the count.

**A refused calibration is a labelled state, not a dropped row.** ``score.observed_rate`` and
``score.calibration_n`` used to be NOT NULL, on the right reasoning — a confidence figure
without its ``n`` is an adjective — applied the wrong way round. When a fold's calibration was
refused there is no ``n``, so the loader refused, and a run whose every fold sat below
``min_positives_for_calibration`` landed *nothing*: the queue an analyst opens came up empty,
which is indistinguishable on screen from an account nobody scored. Plan 03 §H specifies the
other behaviour ("calibration is refused and the UI says probabilities are uncalibrated") and
plan §12.8 requires degraded rather than broken, so the row now lands with
``calibration_kind='uncalibrated'``, the four measurement columns NULL, and the fold's own
refusal text in ``calibration_note``. ``ck_score_calibration_pairing`` in migration 0003 makes
a half-populated row impossible at the database, which is where the adjective ban belongs;
the calibration floor itself is untouched, and an uncalibrated probability is still never
written into a column that calls itself calibrated.

**Column names are the table's, not the frame's.** The scored frame carries feature columns and
model columns mixed; ``SCORE_FIELDS`` is the whitelist, so a renamed model column fails loudly
here instead of landing a row with a null in a column Postgres will accept as null.

The same two rules govern the analytical tables added below, with one extension the grain forces.
``account``/``score``/``rule_hit`` are shaped from one landed frame; ``ablation_row``,
``validation_metric``, ``fairness_row``, ``perturbation_row`` and ``backtest_fold`` are shaped from
the decoded backtest document, and several of those figures are recorded **once per ablation arm**
while the tables declare them once per run: ``uq_validation_metric`` is ``(run_id, name, corpus)``,
``uq_fairness_row`` is ``(run_id, axis, bucket)``, ``uq_backtest_fold`` is ``(run_id, fold_index)``.
``uq_ablation_row`` is the one key that already names the arm. So a figure that recurs across arms
lands only when the arms agree on it, and the agreement is stated; a figure the arms disagree on
refuses. ``band_definition``, ``scorecard_bin`` and ``scorecard_point`` are aggregates over the
run's own landed scored rows at the grain ``score`` uses — one current row per account — because a
band table counted per account-instant would report a population the queue cannot have.
``economics`` is priced at that same grain by :func:`economics_rows`, and it is the table the two
rules bite hardest on: every money column is read from a scored-frame column or an
``config/economics.yaml`` declaration, ``EV_i`` is computed by ``quant/ev.py`` rather than restated,
and the three Monte Carlo quantile columns — NOT NULL, and measured by nothing in this repository —
refuse the row by name instead of taking a zero or an invented draw count.

Nothing here computes a statistic the producer did not publish. A rate or a share taken over the
run's own rows (a band's observed rate, a bin's population share) is the same class of aggregation
as ``account.txn_count``: it counts landed rows, and the columns it reads are named. AUROC for a
fold, a per-attribute PSI, a bin's fit-time ordering — those are not counts of anything in the
artifact, and a mapper that produced them would be the pipeline computing an answer it never
measured, which is what §18 refuses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, Final

import polars as pl

from oxbow.adapters.warehouse.models import (
    ACCOUNT_KEY_LEN,
    CURRENCY_LEN,
    REASON_CODE_LEN,
    RUN_ID_LEN,
)
from oxbow.ports.case_sink import MonteCarloInterval
from oxbow.ports.warehouse import assert_run_id
from oxbow.quant.economics import Economics
from oxbow.quant.ev import CalibratedScore, price_account
from oxbow.quant.money import Money


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
    # The fused probability IS the calibrated one whenever the fold calibrated. When it did
    # not, `calibrated_probability` is stored NULL and the fused score still lands — see the
    # `calibrated`/`band_n` handling in `score_rows`.
    "calibrated_probability": "p_fused",
    # `band_observed_rate`/`band_n` are the rate measured *within the band*, so the band that
    # carries them is the band the column names. Two different names for one thing across the
    # seam; the alternative — inventing a tag the frame does not carry — would be worse.
    "calibration_band": "band",
    "observed_rate": "band_observed_rate",
    "calibration_n": "band_n",
    "calibration_kind": None,  # derived from `calibrated`, the frame's own boolean
    "calibration_note": "uncalibrated_reason",
    "predicted_typology": "label_typology",
    "reason_codes": "reason_codes",
    "rule_ids": None,  # derived from the rule_*_severity columns
    "model_version": "model_version",
}

SCORE_COLUMNS: Final = tuple(SCORE_SOURCES)

_BANDS: Final = frozenset("ABCDE")

# The refusal reason is the fold's sentence, and the fold's sentence is long: it names the
# positive count it measured, the configured floor, and why the floor exists. Truncating it
# mid-clause would leave a quoted number with no referent, so the ceiling is generous and
# stated, and the value it bounds is the storage column's practical limit rather than a
# number the reader has to guess at.
_CALIBRATION_NOTE_MAX: Final = 512


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


def _require_out_of_sample(part: pl.DataFrame, *, table: str, role: str) -> pl.DataFrame:
    """Refuse an empty slice rather than land an empty table.

    ``score_rows`` has always raised on this input, and these tables are read by the same studio
    panes beside it: the run with no out-of-sample rows would otherwise post a green warehouse
    stage with a blank band table, a blank scorecard and a blank case rail, and nothing on disk
    would say which of "no accounts", "wrong role" and "the mapper refused" happened.
    """
    if part.height == 0:
        raise LandingError(
            f"{table}: no scored rows with role={role!r}, so there is nothing out-of-sample to "
            "describe; the frame that would carry them is "
            "out/score/<run>/scored_rows.parquet"
        )
    return part


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
        fused = record.get("p_fused")

        # Two different questions get two different answers here. "Is there a score?" is
        # about the run's output: no fused probability or a band outside A-E means there is
        # nothing to land, and the row is refused. "Was that score calibrated?" is about the
        # *confidence* attached to it, and plan 03 §H answers it by labelling, not by
        # discarding: below the positive-count floor calibration is refused and the UI says
        # the probabilities are uncalibrated. Refusing the row for the second question is
        # what emptied the queue when every fold of a run sat below the floor.
        problems: list[str] = []
        if band not in _BANDS:
            problems.append(f"band={band!r} is not one of A-E")
        if fused is None:
            problems.append("p_fused is absent, so the run recorded no score for this account")
        if problems:
            refused.append(f"account {account_key[:ACCOUNT_KEY_LEN]}: " + "; ".join(problems))
            continue

        calibrated_claim = record.get("calibrated")
        # The measurement and the flag have to agree, and neither one is trusted alone.
        # `band_n > 0` with a finite rate is what "there is a measured population" means;
        # `calibrated` is what the fold decided. A frame that carries no `calibrated` column
        # at all predates the flag, so the measurement answers alone — and a frame that
        # carries a flag contradicting its own measurement is refused, because either the
        # flag or the rate is a lie and this loader cannot tell which.
        has_measurement = (
            calibration_n is not None
            and int(calibration_n) > 0
            and observed_rate is not None
            and observed_rate == observed_rate  # NaN is not a measurement
        )
        if calibrated_claim is None:
            calibrated = has_measurement
            if not calibrated and calibration_n is not None and int(calibration_n) > 0:
                problems.append(
                    f"band_n={calibration_n!r} but band_observed_rate={observed_rate!r} is not a "
                    "measurement, and the frame carries no `calibrated` flag to arbitrate"
                )
        else:
            calibrated = bool(calibrated_claim)
            if calibrated != has_measurement:
                problems.append(
                    f"calibrated={calibrated_claim!r} contradicts the population it reports "
                    f"(band_n={calibration_n!r}, band_observed_rate={observed_rate!r}); the row "
                    "cannot be labelled either way without discarding one of the two"
                )
        if problems:
            refused.append(f"account {account_key[:ACCOUNT_KEY_LEN]}: " + "; ".join(problems))
            continue

        reason_codes = record.get("reason_codes") or []
        row: dict[str, Any] = {
            "account_key": account_key,
            "fused_score": float(fused),
            "band": str(band),
            "scorecard_points": int(record["score_points"]),
            "p_scorecard": record.get("p_scorecard"),
            "p_gbm": record.get("p_gbm"),
            "anomaly_norm": record.get("anomaly_norm"),
            "calibration_kind": "calibrated_band" if calibrated else "uncalibrated",
            "predicted_typology": record.get("label_typology"),
            "reason_codes": (
                reason_codes
                if isinstance(reason_codes, list)
                else _load_json(reason_codes, column="reason_codes", account_key=account_key) or []
            ),
            "rule_ids": _rule_ids(record),
            "model_version": str(
                record.get("model_version") or record.get("model_fingerprint") or ""
            )[:128],
        }
        if calibrated:
            row.update(
                {
                    "calibrated_probability": float(fused),
                    # The band the rate was measured *within* is the score band, which is
                    # why both travel together; `band_observed_rate` is computed per band by
                    # the scorer, so this is the same string, not a substitute for one.
                    "calibration_band": str(band),
                    "observed_rate": float(observed_rate),
                    "calibration_n": int(calibration_n),
                    "calibration_note": None,
                }
            )
        else:
            # The whole pairing goes empty together, and the reason the fold recorded comes
            # with it, so the queue can print "uncalibrated: <why>" instead of a blank. The
            # reason is the fold's own text (models/run.py writes it), never a summary here.
            why = str(record.get("uncalibrated_reason") or "").strip()
            row.update(
                {
                    "calibrated_probability": None,
                    "calibration_band": None,
                    "observed_rate": None,
                    "calibration_n": None,
                    "calibration_note": (
                        why or "the fold recorded calibrated=False with no reason"
                    )[:_CALIBRATION_NOTE_MAX],
                }
            )
        rows.append(row)

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


# --- shaping helpers for the analytical tables ------------------------------


def _dig(payload: Any, path: str) -> Any:
    """A dotted path out of a decoded JSON document; None when any step is absent.

    The backtest artifact nests its figures three levels deep (``policies.score_threshold.pr_auc_ci
    .low``). Declaring the path as text keeps the source map readable; returning None on a missing
    step is what turns a renamed producer key into a refusal naming that key.
    """
    current = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _integer(value: Any) -> int | None:
    """An exact integer, or None for absent, boolean, string or fractional input.

    ``True`` is Python's ``int``, and a boolean in a money column is the defect DEV-005 names, so
    the bool is refused before the int check. A float is refused rather than rounded: no rounding
    rule was measured, and `assert_money_is_integer_minor` would only repeat the refusal later,
    after the row had already been built.
    """
    if value is None or isinstance(value, bool | float | str):
        return None
    index = getattr(value, "__index__", None)
    return int(index()) if callable(index) else None


def _ratio(value: Any) -> float | None:
    """A finite real number, or None. NaN and infinity are not measurements."""
    if value is None or isinstance(value, bool | str):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _flag(value: Any) -> float | None:
    """A recorded boolean as the scalar the table can hold, with the text kept in ``note``."""
    return None if not isinstance(value, bool) else (1.0 if value else 0.0)


def _name(value: Any, *, limit: int) -> str | None:
    """A non-empty string that fits its column, or None.

    Over-length refuses rather than truncates. ``score.model_version`` is cut at 128 because that
    column was sized for a version string; a rule id, a corpus name or a bin label cut mid-word is
    a *different* name, and the reader would have no way to tell it from the real one.
    """
    if value is None:
        return None
    text = str(value)
    if not text.strip() or len(text) > limit:
        return None
    return text


def _text(value: Any) -> str | None:
    """A non-empty string for a ``Text`` column, which has no length to overflow.

    Split from :func:`_name` because the two failures are different: a name that does not fit its
    VARCHAR is a schema question, and an empty sentence in a free-text column is a missing
    measurement.
    """
    if value is None:
        return None
    written = str(value)
    return written if written.strip() else None


def _day(value: Any) -> date | None:
    """A calendar day from the artifact's ISO text, or None when it is not a date at all."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _collapse(
    pairs: Sequence[tuple[str, Any]], *, what: str, exclude_controlled: bool = False
) -> tuple[Any, str | None]:
    """One value shared by several records, or the reason it cannot be collapsed.

    ``pairs`` is (source_label, value) over the records that all want to write the same row — the
    ablation arms behind one corpus-level metric, the arms and policy ladders behind one fold. The
    rule, stated: the value lands only when every source that reports it reports the identical one,
    and the refusal names the disagreeing sources. Silent first-wins is the failure DEV-026 found
    at the ledger, and averaging two published figures would be inventing a third. ``(None, None)``
    means no source recorded the figure at all, which refuses a required column and leaves a
    nullable one honestly unset.

    Arms the artifact itself labels as a control are excluded when asked, because a control
    measures the harness and not the configuration.
    """
    considered: list[tuple[str, Any]] = []
    for label, value in pairs:
        if exclude_controlled and label.startswith("CONTROL"):
            continue
        considered.append((label, value))
    present = [(label, value) for label, value in considered if value is not None]
    if not present:
        # Nothing recorded it. That is an absence, not a disagreement: whether it refuses the row is
        # the caller's decision, because a nullable column may honestly hold nothing.
        return None, None
    first = present[0][1]
    divergent = sorted(label for label, value in present if value != first)
    if divergent:
        return None, (
            f"{what}: {len(set(map(str, (value for _, value in present))))} different values "
            f"across {len(present)} source record(s) (diverging: {', '.join(divergent[:4])}); "
            f"{what} is keyed once per run, so landing one would silently discard the others"
        )
    return first, None


# --- the backtest document's analytical tables ------------------------------

#: ``ablation_row`` is the ablation table itself — "the rigor score" (spec §7.6). Its key already
#: names the arm (``uq_ablation_row`` is ``(run_id, variant, corpus)``), so this is the one
#: analytical table with no cross-arm collapse: every arm is its own row.
#: The rows come from the model card's published ``ablation_table`` because that is the table the
#: card was built to publish: flat, one currency per row, and its own CI pair. The three
#: document-level figures the column set needs but the row does not carry (the CI method, its
#: resample count, the seed) are resolved from the ablation document beside it — see
#: :func:`ablation_rows`.
ABLATION_SOURCES: Final[dict[str, str]] = {
    "variant": "row_id",
    "question": "question",
    "corpus": "corpus",
    "pr_auc": "pr_auc",
    "net_benefit_minor": "net_benefit_total_minor",
    "currency": "currency",
    "ci_low": "pr_auc_ci_low",
    "ci_high": "pr_auc_ci_high",
    "ci_method": "ci_method",
    "n_resamples": "n_resamples",
    "seed": "seed",
}

#: Which of the mapped columns are real numbers, which are integers, and the length each name has
#: to fit. Anything not listed here is a text column with no declared limit (``question`` is Text).
_ABLATION_RATIOS: Final = frozenset({"pr_auc", "ci_low", "ci_high"})
_ABLATION_INTEGERS: Final = frozenset({"net_benefit_minor", "n_resamples", "seed"})
_ABLATION_NAME_LIMITS: Final[dict[str, int]] = {
    "variant": 64,
    "corpus": 64,
    "currency": CURRENCY_LEN,
    "ci_method": 64,
}
#: Why a missing figure has no source, in the message. ``n_resamples`` is the one the artifact
#: claims to carry and does not: its own note reads "resamples and seed stored".
_ABLATION_HINTS: Final[dict[str, str]] = {
    "n_resamples": (
        " — the artifact's CI note says the resample count is stored and it is not serialised; "
        "the count is taken from config/splits.yaml report.bootstrap.resamples, and neither "
        "reached this row"
    ),
    "ci_method": " — pr_auc_ci.confidence_note is absent from every arm's policy record",
}


def _document_ci(ablation: Mapping[str, Any]) -> tuple[str | None, int | None, str | None]:
    """The CI method and resample count the arms agree on, plus any refusal worth reporting.

    An arm runs two or three policy ladders here, and PR-AUC is pooled over the scored accounts, so
    the interval is the same figure whatever the allocator. That is checked rather than assumed: a
    document whose arms report two different methods cannot fill one ``ci_method`` column, and a
    document with two different resample counts cannot fill one ``n_resamples`` column either.
    """
    methods: list[tuple[str, Any]] = []
    counts: list[tuple[str, Any]] = []
    for variant in ablation.get("variants") or []:
        if not isinstance(variant, Mapping):
            continue
        label = str(variant.get("label") or variant.get("row_id") or "?")
        policies = variant.get("policies")
        if not isinstance(policies, Mapping):
            continue
        for policy_name, policy in sorted(policies.items()):
            if not isinstance(policy, Mapping):
                continue
            ci = policy.get("pr_auc_ci")
            if not isinstance(ci, Mapping):
                continue
            source = f"{label}/{policy_name}"
            methods.append((source, ci.get("confidence_note")))
            counts.append((source, ci.get("resamples", ci.get("draws"))))

    if not methods:
        return None, None, "ci_method: no arm's policy record carries a pr_auc_ci block"
    method, method_problem = _collapse(methods, what="ci_method")
    count, count_problem = _collapse(counts, what="n_resamples")
    # A count no arm recorded is not a refusal: the caller falls back to the count config declares,
    # and the row that gets neither refuses with both paths named.
    return (
        _name(method, limit=64),
        _integer(count),
        method_problem
        or (None if method is not None else "ci_method: no arm recorded pr_auc_ci.confidence_note")
        or count_problem,
    )


def ablation_rows(
    card: Mapping[str, Any],
    ablation: Mapping[str, Any],
    *,
    declared_resamples: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One ``ablation_row`` per published arm, and the arms that could not be landed.

    Determinism: rows are emitted sorted by ``(variant, corpus)`` — the artifact's arm order is a
    build-order artefact and the table's key is the arm, so the names decide the order.

    ``declared_resamples`` is the count ``config/splits.yaml`` declares at
    ``report.bootstrap.resamples``, read by the caller rather than restated here. It is the fallback
    because the artifact's own CI note claims "resamples and seed stored" while ``pr_auc_ci``
    serialises only the bounds: the seed *is* recorded (once, on the document), the count is not.
    A reviewer who wants the run's own count in the row should have the harness serialise
    ``BootstrapCI.resamples``, which it computes and then drops.
    """
    table = card.get("ablation_table")
    if not isinstance(table, list) or not table:
        raise LandingError(
            "the model card carries no `ablation_table`, so `ablation_row` has no source rows; "
            "the artifact that would be needed is out/backtest/<run>/model_card.json"
        )
    ci_method, resamples, ci_problem = _document_ci(ablation)
    if resamples is None:
        resamples = _integer(declared_resamples)
    seed = _integer(card.get("seed"))
    if seed is None:
        seed = _integer(ablation.get("seed"))

    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    if ci_problem:
        refused.append(ci_problem)
    for entry in table:
        if not isinstance(entry, Mapping):
            refused.append(f"ablation row {entry!r} is not a mapping")
            continue
        source = dict(entry)
        source["ci_method"] = ci_method
        source["n_resamples"] = resamples
        source["seed"] = seed
        arm = str(entry.get("row_id") or "?")
        problems: list[str] = []
        row: dict[str, Any] = {}
        for column, producer in ABLATION_SOURCES.items():
            value = source.get(producer)
            if column in _ABLATION_RATIOS:
                parsed: Any = _ratio(value)
            elif column in _ABLATION_INTEGERS:
                parsed = _integer(value)
            elif column == "question":
                parsed = _text(value)
            else:
                parsed = _name(value, limit=_ABLATION_NAME_LIMITS.get(column, 128))
            if parsed is None:
                problems.append(f"{column} ← {producer}{_ABLATION_HINTS.get(column, '')}")
            else:
                row[column] = parsed
        if problems:
            refused.append(f"ablation {arm}: no measurement for {', '.join(problems)}")
            continue
        rows.append(row)

    return sorted(rows, key=lambda row: (row["variant"], row["corpus"])), refused


#: ``validation_metric`` is ``(run_id, name, corpus)`` UNIQUE while the backtest document records
#: its scalars once per arm (and once per policy ladder inside an arm). The collapse rule is the
#: one in :func:`_collapse`: an arm-level figure lands when the honest arms report the identical
#: value, with the arms the card labels a control excluded because a control measures the harness.
#: Paths are dotted and relative to the record the scope names.
VALIDATION_VARIANT_METRICS: Final[tuple[tuple[str, str, str, str | None], ...]] = (
    # (metric name, value path on a variant, unit, n path)
    ("label_prevalence", "base_rate", "share", "n_total_rows"),
    ("labelled_positive_rows", "n_total_positive", "rows", "n_total_rows"),
    ("corpus_rows", "n_total_rows", "rows", None),
    ("fold_count", "fold_count", "folds", None),
    ("embargo_days", "embargo_days", "days", None),
)
VALIDATION_DOCUMENT_METRICS: Final[tuple[tuple[str, str, str, str | None], ...]] = (
    ("configurations_evaluated", "overfitting_controls.configs_evaluated", "configurations", None),
    ("corpus_span_days", "corpus_feasibility.corpus_span_days", "days", None),
    (
        "max_full_embargo_gaps_in_span",
        "corpus_feasibility.max_full_embargo_gaps_in_span",
        "gaps",
        None,
    ),
    ("folds_supplied", "corpus_feasibility.folds_supplied", "folds", None),
    ("honest_ablation_rows", "honest_ablation_rows", "arms", None),
    ("honest_model_fits", "honest_model_fits", "fits", None),
    (
        "corpus_fold_column_agreement",
        "corpus_fold_column_check.agreement_share",
        "share",
        "corpus_fold_column_check.rows_compared",
    ),
)
#: Recorded booleans, landed as 1.0/0.0 with the recorded text beside them, because the table's
#: value column is a double and the producer's own field is a flag.
VALIDATION_FLAG_METRICS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "test_fold_touched_once",
        "overfitting_controls.test_fold_touched_once",
        "the test fold was touched exactly once",
    ),
    (
        "supports_literal_walk_forward",
        "corpus_feasibility.supports_literal_walk_forward",
        "walk-forward as literal walk-forward",
    ),
    (
        "leakage_control_detected",
        "leakage_control.detected",
        "the lookahead control beat every honest arm",
    ),
)
#: The model card's headline: the figures the card publishes for the corpus as a whole.
VALIDATION_HEADLINE_METRICS: Final[tuple[tuple[str, str, str, str | None], ...]] = (
    ("headline_pr_auc", "headline.pr_auc", "share", None),
    ("headline_pr_auc_ci_low", "headline.pr_auc_ci_low", "share", None),
    ("headline_pr_auc_ci_high", "headline.pr_auc_ci_high", "share", None),
    ("headline_net_benefit_minor", "headline.net_benefit_total_minor", "minor units", None),
    ("benefit_per_analyst_hour_minor", "benefit_per_analyst_hour_minor", "minor units", None),
    ("risk_adjusted_benefit_ratio", "benefit_ratio.value", "ratio", None),
)
#: ``limitation:`` names are the contract :meth:`api.routers.validation._limitations` reads, and
#: the note is the artifact's own sentence — echoed, never re-authored here (plan §15).
VALIDATION_LIMITATIONS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "limitation:multiple_testing",
        "overfitting_controls.configs_evaluated",
        "overfitting_controls.multiple_testing_caveat",
    ),
    ("limitation:ablation_arms", "honest_ablation_rows", "ablation_caveat"),
    (
        "limitation:walk_forward_feasibility",
        "corpus_feasibility.folds_supplied",
        "corpus_feasibility.note",
    ),
    ("limitation:leakage_control", "leakage_control.control_pr_auc", "leakage_control.message"),
)


def validation_metric_rows(
    ablation: Mapping[str, Any], card: Mapping[str, Any] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Named scalars for the run's corpus, collapsed across the arms that repeat them.

    Only figures the artifacts record are emitted. The router also reads ``review_budget`` and
    ``flagged_fraud_rows``: the first is a capacity the run consumed from config and never
    published, the second is a PaySim column this corpus run never measured, so neither name is
    landed — an absent metric is a gap the page can show, a zero-filled one is a claim.
    """
    variants = [item for item in (ablation.get("variants") or []) if isinstance(item, Mapping)]
    if not variants:
        raise LandingError(
            "the ablation document carries no `variants`, so no corpus-level figure can be "
            "collapsed; the artifact that would be needed is out/backtest/<run>/ablation_results.json"
        )
    corpus = _name(ablation.get("corpus"), limit=64)
    if corpus is None:
        raise LandingError(
            "the ablation document records no `corpus`, and validation_metric.corpus is NOT NULL"
        )

    rows: list[dict[str, Any]] = []
    refused: list[str] = []

    def land(name: str, value: Any, unit: str | None, n: Any, note: str | None) -> None:
        number = _ratio(value)
        if number is None:
            refused.append(f"validation_metric {name}: no finite measurement recorded")
            return
        rows.append(
            {
                "name": name,
                "value": number,
                "unit": _name(unit, limit=32),
                "corpus": corpus,
                "note": note,
                "n": _integer(n),
            }
        )

    for name, path, unit, n_path in VALIDATION_VARIANT_METRICS:
        pairs = [
            (str(_dig(item, "label") or _dig(item, "row_id") or "?"), _dig(item, path))
            for item in variants
        ]
        value, problem = _collapse(pairs, what=name, exclude_controlled=True)
        if problem:
            refused.append(problem)
            continue
        n = None if n_path is None else _dig(variants[0], n_path)
        land(name, value, unit, n, None)

    for name, path, unit, n_path in VALIDATION_DOCUMENT_METRICS:
        value = _dig(ablation, path)
        if value is None:
            refused.append(f"validation_metric {name}: {path} is absent from the document")
            continue
        n = None if n_path is None else _dig(ablation, n_path)
        land(name, value, unit, n, None)

    for name, path, note in VALIDATION_FLAG_METRICS:
        value = _flag(_dig(ablation, path))
        if value is None:
            refused.append(f"validation_metric {name}: {path} is not a recorded boolean")
            continue
        rows.append(
            {
                "name": name,
                "value": value,
                "unit": "flag",
                "corpus": corpus,
                "note": note,
                "n": None,
            }
        )

    if isinstance(card, Mapping):
        for name, path, unit, n_path in VALIDATION_HEADLINE_METRICS:
            value = _dig(card, path)
            if value is None:
                refused.append(f"validation_metric {name}: {path} is absent from the model card")
                continue
            note = None
            if name == "risk_adjusted_benefit_ratio":
                ratio = card.get("benefit_ratio")
                note = (
                    f"{ratio.get('label')} = {ratio.get('formula')}; "
                    f"is_sharpe_ratio={ratio.get('is_sharpe_ratio')}"
                    if isinstance(ratio, Mapping)
                    else None
                )
            n = None if n_path is None else _dig(card, n_path)
            land(name, value, unit, n, note)
        fairness = card.get("fairness")
        note = (
            _dig(fairness, "protected_attributes_note") if isinstance(fairness, Mapping) else None
        )
        axes = fairness.get("axes") if isinstance(fairness, Mapping) else None
        if isinstance(axes, list) and note:
            rows.append(
                {
                    "name": "limitation:protected_attributes",
                    "value": float(len(axes)),
                    "unit": "proxy axes",
                    "corpus": corpus,
                    "note": str(note),
                    "n": None,
                }
            )
        for name, path, note_path in VALIDATION_LIMITATIONS:
            value = _dig(ablation, path)
            note = _dig(ablation, note_path)
            if value is None or note is None:
                refused.append(f"validation_metric {name}: value or note absent from the document")
                continue
            rows.append(
                {
                    "name": name,
                    "value": _ratio(value) if not isinstance(value, bool) else _flag(value),
                    "unit": "see note",
                    "corpus": corpus,
                    "note": str(note),
                    "n": None,
                }
            )
        typologies = _dig(card, "per_typology_recall.recall_by_typology_mean_over_folds")
        if isinstance(typologies, Mapping) and not typologies:
            refused.append(
                "typology_recall: the artifact records 0 typologies with a recall figure "
                f"({_dig(card, 'per_typology_recall.source')}), so there is no typology_recall "
                "metric to land and none is written as 0"
            )

    # A name cannot appear twice for one corpus: the table's key says so, and a duplicate here is
    # the write failing on its own constraint. Refusing beats letting Postgres decide the order.
    seen: dict[tuple[str, str], str] = {}
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row["name"]), str(row["corpus"]))
        if key in seen:
            refused.append(
                f"validation_metric {key[0]!r} for corpus {key[1]!r} was reported twice "
                f"(second reading: {str(row['note'])[:60] or str(row['value'])}); the table is "
                "unique on (run_id, name, corpus)"
            )
            continue
        seen[key] = str(row["value"])
        unique.append(row)

    return sorted(unique, key=lambda row: (row["name"], row["corpus"])), refused


#: ``fn_rate`` is the column the producer never emits: the artifact's bucket records n_accounts,
#: n_clean and n_false_positives, so the miss rate is not measured and the nullable column stays
#: unset rather than being derived from a denominator the table never declares.
FAIRNESS_SOURCES: Final[dict[str, str]] = {
    "axis": "axis",
    "bucket": "bucket",
    "fp_rate": "false_positive_rate",
    "n": "n_accounts",
}


def fairness_rows(card: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """One ``fairness_row`` per bucket of each usable proxy axis the model card publishes.

    The card's table is the published one. The ablation document carries a fairness block per arm
    as well, and those blocks disagree with the card and with each other, which ``uq_fairness_row``
    on ``(run_id, axis, bucket)`` cannot hold — so the arms are not read here, and the disagreement
    is a finding about the artifact rather than something this mapper arbitrates by picking a winner.
    """
    fairness = card.get("fairness")
    if not isinstance(fairness, Mapping):
        raise LandingError(
            "the model card carries no `fairness` block; the artifact that would be needed is "
            "out/backtest/<run>/model_card.json"
        )
    rationale = _text(fairness.get("protected_attributes_note"))
    axes = fairness.get("axes")
    if not isinstance(axes, list) or not axes:
        raise LandingError("the fairness block records no `axes`, so there is nothing to land")

    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    if rationale is None:
        refused.append(
            "fairness: protected_attributes_note is absent, and axis_rationale is NOT NULL"
        )
    for axis in axes:
        if not isinstance(axis, Mapping):
            continue
        name = _name(axis.get("axis"), limit=64)
        if not axis.get("available"):
            refused.append(
                f"fairness axis {name!r}: the artifact marks it unavailable — "
                f"{str(axis.get('reason') or 'no reason recorded')[:120]}"
            )
            continue
        buckets = axis.get("buckets")
        if not isinstance(buckets, list) or not buckets:
            refused.append(f"fairness axis {name!r}: available but with no buckets recorded")
            continue
        for bucket in buckets:
            if not isinstance(bucket, Mapping) or rationale is None:
                continue
            row = {
                "axis": name,
                "axis_rationale": rationale,
                "bucket": _name(bucket.get(FAIRNESS_SOURCES["bucket"]), limit=64),
                "fp_rate": _ratio(bucket.get(FAIRNESS_SOURCES["fp_rate"])),
                "n": _integer(bucket.get(FAIRNESS_SOURCES["n"])),
            }
            absent = [column for column, value in row.items() if value is None]
            if absent:
                refused.append(
                    f"fairness {name}/{str(bucket.get('bucket'))[:24]}: no measurement for "
                    f"{absent} (the artifact's own keys are {sorted(FAIRNESS_SOURCES.values())})"
                )
                continue
            rows.append(row)

    ordered = sorted(rows, key=lambda row: (row["axis"], row["bucket"]))
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for row in ordered:
        key = (str(row["axis"]), str(row["bucket"]))
        if key in seen:
            refused.append(
                f"fairness {key}: the artifact repeats one bucket; (run_id, axis, bucket) is unique"
            )
            continue
        seen.add(key)
        unique.append(row)
    return unique, refused


#: ``perturbation_row`` holds one magnitude and one result per check, while the artifact records a
#: different pair of figures per family. The declaration names which recorded figure is which, so a
#: producer that renames `shift_ratio` makes the row refuse rather than land an unrelated number.
PERTURBATION_SOURCES: Final[dict[str, dict[str, str | None]]] = {
    "amount_shift": {
        "magnitude": "shift_ratio",
        "result": "spearman",
        "unit": "spearman rho",
        "note": "reordered_at_cutoff",
        "seed": None,
    },
    "edge_drop": {
        "magnitude": "drop_ratio",
        "result": "max_abs_shift",
        "unit": "recall share",
        "note": "caveat",
        "seed": "seed",
    },
}


def perturbation_rows(
    card: Mapping[str, Any], ablation: Mapping[str, Any] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """One robustness row per perturbation the run actually ran, with its magnitude and result.

    ``seed`` falls back to the document's own recorded seed when the check does not carry one: the
    artifact states the run's seed once, and every check in the document was run under it.
    """
    perturbations = card.get("perturbations")
    if not isinstance(perturbations, Mapping) or not perturbations:
        raise LandingError(
            "the model card records no `perturbations`, so `perturbation_row` has no source; the "
            "artifact that would be needed is out/backtest/<run>/model_card.json"
        )
    document_seed = _integer(card.get("seed"))
    if document_seed is None and isinstance(ablation, Mapping):
        document_seed = _integer(ablation.get("seed"))

    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    for kind, record in sorted(perturbations.items()):
        if not isinstance(record, Mapping):
            refused.append(f"perturbation {kind}: the artifact's entry is not a mapping")
            continue
        source = next(
            (value for prefix, value in PERTURBATION_SOURCES.items() if kind.startswith(prefix)),
            None,
        )
        if source is None:
            refused.append(
                f"perturbation {kind}: no declared source map for this family, so its magnitude "
                "and result would be a guess about which recorded figure is which"
            )
            continue
        note_key = source["note"]
        note = record.get(note_key) if note_key else None
        row = {
            "kind": _name(kind, limit=64),
            "magnitude": _ratio(record.get(str(source["magnitude"]))),
            "result": _ratio(record.get(str(source["result"]))),
            "unit": _name(source["unit"], limit=32),
            "note": (f"{note_key}={note}" if isinstance(note, bool) else _text(note)),
            "seed": _integer(record.get(str(source["seed"]))) if source["seed"] else document_seed,
        }
        absent = [column for column, value in row.items() if value is None]
        if absent:
            refused.append(
                f"perturbation {kind}: no measurement for {absent}; the recorded keys are "
                f"{sorted(record)}"
            )
            continue
        rows.append(row)
    return rows, refused


#: ``backtest_fold`` is one row per fold: ``(run_id, fold_index)`` unique, and the window bounds and
#: the per-fold classifier figures are NOT NULL. The artifact records a fold once per arm per policy
#: ladder, and it records none of the five window dates, the fold's AUROC, Brier, drawdown or its
#: entity-disjointness flag — see :func:`backtest_fold_rows` for the refusal that produces.
BACKTEST_FOLD_SOURCES: Final[dict[str, str]] = {
    "fold_index": "fold_index",
    "corpus": "corpus",
    "embargo_days": "embargo_days",
    "n_train": "n_fit_rows",
    "n_test": "n_scored_rows",
    "pr_auc": "pr_auc",
    "alerts": "accounts_reviewed",
    "precision_at_budget": "precision",
    "recall_at_budget": "recall",
    "captured_value_minor": "captured_value_minor",
    "cost_minor": "cost_minor",
    "net_benefit_minor": "net_benefit_minor",
    "var95_minor": "var95_minor",
    "es975_minor": "es975_minor",
    "mc_runs": "mc_draws",
    "mc_seed": "mc_seed",
    "currency": "currency",
}
#: The two columns a fold may honestly leave unset: a fold whose alerts never crossed the cutoff
#: has no precision and no recall at all, and `precision_undefined` is how the table says so.
BACKTEST_FOLD_NULLABLE: Final = frozenset({"precision_at_budget", "recall_at_budget"})
#: Figures the table declares NOT NULL and the fold record does not carry, with what would carry it.
#: One ``backtest_fold`` row per (run, fold) means one owner per fold. The plan publishes the final
#: statistical configuration under the constrained-optimal queue as the headline, so that arm's fold
#: record owns the row: the other arms differ by MODEL (and their discrimination belongs in
#: ``ablation_row``, which is keyed by variant), while the policy ladders within one arm book
#: different money for the same fold and cannot be averaged into a single figure. A fold with no
#: record from this owner is refused by name rather than silently reattributed.
BACKTEST_FOLD_OWNER: Final = ("full_calibrated", "ev_cpsat")
BACKTEST_FOLD_INTEGERS: Final = frozenset(
    {"fold_index", "embargo_days", "n_train", "n_test", "alerts", "mc_runs", "mc_seed"}
)
BACKTEST_FOLD_MONEY: Final = frozenset(
    {"captured_value_minor", "cost_minor", "net_benefit_minor", "var95_minor", "es975_minor"}
)


#: The five window bounds `backtest_fold` declares NOT NULL, and the keys the run's own fold-plan
#: section uses for them. They come from ``oxbow.backtest.splits.SplitPlan`` via the artifact's
#: ``fold_windows``, because the splits module owns fold arithmetic and nothing downstream may
#: recompute a boundary it was not given (DEV-013).
BACKTEST_FOLD_WINDOW_KEYS: Final = (
    "train_start",
    "train_end",
    "embargo_end",
    "test_start",
    "test_end",
)


def _fold_window_lookup(ablation: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """The run's per-fold date windows, keyed by the fold index the harness reports.

    A document with no ``fold_windows`` section is an artifact from before the boundaries were
    serialised; that is reported per fold by the mapper rather than guessed from the corpus span,
    which would be a window the run never used.
    """
    sections = ablation.get("fold_windows")
    if not isinstance(sections, list):
        return {}
    found: dict[int, dict[str, Any]] = {}
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        index = _integer(section.get("fold_index"))
        if index is None or index in found:
            continue
        found[index] = {key: section.get(key) for key in BACKTEST_FOLD_WINDOW_KEYS}
    return found


def _fold_records(ablation: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every fold record the document holds, tagged with the arm and ladder that reported it.

    The grain of the artifact is (arm, policy ladder, fold); the grain of the table is (run, fold).
    The tag is what makes the collapse report name the sources that disagree.
    """
    tagged: list[tuple[str, dict[str, Any]]] = []
    windows = _fold_window_lookup(ablation)
    for variant in ablation.get("variants") or []:
        if not isinstance(variant, Mapping):
            continue
        arm = str(variant.get("row_id") or "?")
        label = str(variant.get("label") or arm)
        policies = variant.get("policies")
        if not isinstance(policies, Mapping):
            continue
        for policy_name, policy in sorted(policies.items()):
            if not isinstance(policy, Mapping):
                continue
            corpus = _name(variant.get("corpus"), limit=64)
            for fold in policy.get("folds") or []:
                if not isinstance(fold, Mapping):
                    continue
                record = {
                    "fold_index": fold.get("fold_index"),
                    # The stable row id, not the display label: the fold row's owner is named by id
                    # so a reworded label cannot silently move the ownership to another arm.
                    "row_id": arm,
                    "embargo_days": fold.get("embargo_days"),
                    "n_fit_rows": fold.get("n_fit_rows"),
                    "n_scored_rows": fold.get("n_scored_rows"),
                    "pr_auc": fold.get("pr_auc"),
                    "precision": fold.get("precision"),
                    "recall": fold.get("recall"),
                    "corpus": corpus,
                }
                economics = fold.get("economics")
                if isinstance(economics, Mapping):
                    record.update(
                        {
                            "accounts_reviewed": economics.get("accounts_reviewed"),
                            "captured_value_minor": economics.get("captured_value_minor"),
                            "cost_minor": economics.get("cost_minor"),
                            "net_benefit_minor": economics.get("net_benefit_minor"),
                            "var95_minor": economics.get("var95_minor"),
                            "es975_minor": economics.get("es975_minor"),
                            "mc_draws": economics.get("mc_draws"),
                            "mc_seed": economics.get("mc_seed"),
                            "currency": economics.get("currency"),
                        }
                    )
                for column in ("auroc", "brier", "max_drawdown_minor", "entity_disjoint"):
                    if column in fold:
                        record[column] = fold[column]
                # The window bounds are not per-arm figures: every arm of a fold shares its dates,
                # so the run states them once at the document level and they join in here by fold
                # index. A fold with no section keeps its columns absent and is refused by name.
                window = windows.get(_integer(fold.get("fold_index")))
                if window is not None:
                    record.update(window)
                tagged.append((f"{label}/{policy_name}", record))
    return tagged


def backtest_fold_rows(
    ablation: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """One ``backtest_fold`` row per fold index, owned by the run's headline configuration.

    The artifact reports a fold once per arm per policy ladder, and those records do not agree:
    the EV ladders book different money for the same fold, and — since DEV-027's fix — the arms
    differ by MODEL, so their discrimination columns measure different things. ``(run_id,
    fold_index)`` therefore needs a declared owner rather than a collapse across records that were
    never the same measurement. :data:`BACKTEST_FOLD_OWNER` names one: the final calibrated system
    under the constrained-optimal queue. Every other arm belongs to ``ablation_row``, which is
    keyed by variant and can hold all of them.

    The window bounds join in from the document's own ``fold_windows``, written by ``oxbow
    backtest`` from the ``SplitPlan`` the folds were cut with; the per-fold AUROC, Brier, drawdown
    and entity-disjointness come from the fold record itself. An artifact from before any of that
    has the columns absent and refuses by name — a fold row with an invented boundary or an
    assumed ``entity_disjoint`` is exactly the second source of fold arithmetic DEV-013 forbids.
    """
    records = _fold_records(ablation)
    if not records:
        raise LandingError(
            "the ablation document holds no fold records at all, so backtest_fold cannot be "
            "described; the artifact that would be needed is out/backtest/<run>/ablation_results.json"
        )

    owner_arm, owner_policy = BACKTEST_FOLD_OWNER
    by_index: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for source, record in records:
        index = _integer(record.get("fold_index"))
        if index is None:
            continue
        by_index.setdefault(index, []).append((source, record))

    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    for index in sorted(by_index):
        all_for_index = by_index[index]
        group = [
            (source, record)
            for source, record in all_for_index
            if record.get("row_id") == owner_arm and source.endswith(f"/{owner_policy}")
        ]
        if not group:
            seen = ", ".join(sorted(source for source, _ in all_for_index))[:400]
            refused.append(
                f"fold {index}: {len(all_for_index)} fold record(s) and none of them is the "
                f"declared owner {owner_arm!r} under the {owner_policy!r} ladder "
                f"(seen: {seen}); one row per (run, fold) needs one owner, and averaging ladders "
                "that book different money would state a figure no arm produced"
            )
            continue
        # Every required figure is present in the sources; now they have to agree, fold by fold.
        row: dict[str, Any] = {"fold_index": index}
        problems: list[str] = []
        for column, producer in BACKTEST_FOLD_SOURCES.items():
            pairs = [(source, record.get(producer)) for source, record in group]
            value, problem = _collapse(
                pairs, what=f"fold {index} {column}", exclude_controlled=True
            )
            if problem:
                problems.append(problem)
                continue
            if value is None:
                if column in BACKTEST_FOLD_NULLABLE:
                    row[column] = None
                    continue
                problems.append(f"fold {index} {column}: no source recorded it")
                continue
            if column in BACKTEST_FOLD_INTEGERS or column in BACKTEST_FOLD_MONEY:
                parsed = _integer(value)
            elif column == "currency":
                parsed = _name(value, limit=CURRENCY_LEN)
            elif column == "corpus":
                parsed = _name(value, limit=64)
            else:
                parsed = _ratio(value)
            if parsed is None:
                problems.append(f"fold {index} {column}: {value!r} is not a {column}")
            else:
                row[column] = parsed
        for column, producer in (
            ("train_start", "train_start"),
            ("train_end", "train_end"),
            ("embargo_end", "embargo_end"),
            ("test_start", "test_start"),
            ("test_end", "test_end"),
        ):
            pairs = [(source, record.get(producer)) for source, record in group]
            value, problem = _collapse(
                pairs, what=f"fold {index} {column}", exclude_controlled=True
            )
            day = None if problem else _day(value)
            if value is None and not problem:
                problems.append(f"fold {index} {column}: no source recorded the window bound")
                continue
            if problem or day is None:
                problems.append(
                    problem or f"fold {index} {column}: {value!r} is not a calendar day"
                )
            else:
                row[column] = day
        for column in ("auroc", "brier", "max_drawdown_minor"):
            pairs = [(source, record.get(column)) for source, record in group]
            value, problem = _collapse(
                pairs, what=f"fold {index} {column}", exclude_controlled=True
            )
            if problem:
                problems.append(problem)
                continue
            if value is None:
                problems.append(f"fold {index} {column}: no source recorded it")
                continue
            parsed = _integer(value) if column.endswith("_minor") else _ratio(value)
            if parsed is None:
                problems.append(f"fold {index} {column}: {value!r} is not a measurement")
            else:
                row[column] = parsed
        disjoint = [(source, record.get("entity_disjoint")) for source, record in group]
        value, problem = _collapse(
            disjoint, what=f"fold {index} entity_disjoint", exclude_controlled=True
        )
        if problem or value is None or not isinstance(value, bool):
            problems.append(
                problem or f"fold {index} entity_disjoint: {value!r} is not a recorded flag"
            )
        else:
            row["entity_disjoint"] = value

        precision = row.get("precision_at_budget")
        alerts = row.get("alerts")
        if not problems:
            # `precision_undefined` is the honest half of the pair the table splits: a fold with no
            # alerts has no denominator, and the router prints its alert count instead (03 §I).
            if not isinstance(alerts, int):
                problems.append(f"fold {index} alerts: {alerts!r} is not a count")
            else:
                row["precision_undefined"] = precision is None or alerts == 0
                if alerts == 0:
                    row["precision_at_budget"] = None
                    row["recall_at_budget"] = None
        if problems:
            refused.extend(dict.fromkeys(problems))
            continue
        if row.get("corpus") is None:
            refused.append(f"fold {index}: no corpus recorded by any source")
            continue
        rows.append(row)
    return rows, refused


# --- the scored frame's studio tables ---------------------------------------

#: ``band_definition`` is the studio's band table: letter, point range, observed rate, population,
#: action and the review effort the alert class costs. The point range, the population and the rate
#: are counted over the run's own landed rows — the same class of aggregation as
#: ``account.txn_count`` — because the fit-time ``BandTable`` the score stage builds in
#: :mod:`oxbow.scoring.bands` is never persisted: ``band_cut_points`` in ``config/scorecard.yaml``
#: is still ``null`` and no artifact carries the fitted band payload.
#: The action and the minutes are read from the config the run consumed and passed in by the caller
#: (the precedent is ``_attach_corpus_economics``, which reads the price and the floor from
#: ``config/economics.yaml`` rather than restating them), and a band with no declared action refuses.
BAND_SOURCES: Final[dict[str, str]] = {
    "band": "band",
    "lower_points": "score_points",
    "upper_points": "score_points",
    "observed_rate": "label_is_fraud",
    "n": "account_key",
}
#: THE GRAIN, STATED: one current row per account, exactly as ``score`` takes it, so the band
#: population is the population the queue can actually page through — not the account-instances the
#: walk-forward scored, which would count a recurring account twice.
#: THE ORDER, STATED: bands ascending by letter, because the table is keyed on the letter.
BAND_COLUMNS: Final = ("band", "lower_points", "upper_points", "observed_rate", "n", "action")


def band_definition_rows(
    scored: pl.DataFrame,
    *,
    role: str = "test",
    actions: Mapping[str, str] | None = None,
    review_minutes: Mapping[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One ``band_definition`` row per band the run scored, with its measured rate and population.

    A band whose rows are not all labelled refuses: ``observed_rate`` would then be a rate over the
    labelled part presented as a rate over the band, which is the substitution plan §18 calls a lie.
    """
    for column in BAND_SOURCES.values():
        if column not in scored.columns:
            raise LandingError(
                f"the scored frame has no {column!r} column, so the band table cannot be counted"
            )
    part = _require_out_of_sample(
        _current_score_per_account(scored.filter(pl.col("role") == role)),
        table="band_definition",
        role=role,
    )
    grouped = part.group_by("band").agg(
        pl.col("score_points").min().alias("lower_points"),
        pl.col("score_points").max().alias("upper_points"),
        pl.len().alias("n"),
        pl.col("label_is_fraud").sum().alias("positives"),
        pl.col("label_is_fraud").null_count().alias("unlabelled"),
    )
    declared_actions = actions or {}
    declared_minutes = review_minutes or {}

    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    for record in grouped.sort("band").to_dicts():
        band = _name(record.get("band"), limit=1)
        problems: list[str] = []
        if band not in _BANDS:
            refused.append(
                f"band {record.get('band')!r}: not one of A-E, so `ck_band_definition_band` would "
                "reject it and no action or review effort is declared for it either"
            )
            continue
        if _integer(record.get("unlabelled")):
            problems.append(
                f"{record['unlabelled']} of {record['n']} row(s) carry no label, so the band's "
                "rate is not a rate over its population"
            )
        n = _integer(record.get("n"))
        positives = _integer(record.get("positives"))
        lower = _integer(record.get("lower_points"))
        upper = _integer(record.get("upper_points"))
        action = _text(declared_actions.get(band or ""))
        minutes = _ratio(declared_minutes.get(band or ""))
        if action is None:
            problems.append(f"action: no action declared for band {band} in config/scorecard.yaml")
        if minutes is None:
            problems.append(
                f"review_minutes: no review effort declared for band {band} in "
                "config/economics.yaml review_minutes_by_alert_class"
            )
        if None in (n, lower, upper, positives) or n == 0:
            problems.append(f"population or point range missing from {record}")
        if problems:
            refused.append(f"band {band}: " + "; ".join(problems))
            continue
        rows.append(
            {
                "band": band,
                "lower_points": lower,
                "upper_points": upper,
                "observed_rate": float(positives) / float(n),
                "n": n,
                "action": action,
                "review_minutes": minutes,
            }
        )
    return rows, refused


# --- the queue's money: ``economics`` ---------------------------------------

#: ``economics`` is the table ``/api/alerts`` joins to ``score`` to put a number on a card, and
#: ``apps/api/schemas/catalog.py``'s ``AlertRow`` declares ``exposure`` and ``expected_value`` as
#: required, so an account with no economics row is an account the queue cannot price. Every value
#: below comes from one of exactly two places: a column the run wrote into its own scored frame, or
#: a key ``config/economics.yaml`` declares. There is no third.
#:
#: ``E_i`` — plan §3.2's "value still interceptable" — is read from **`downstream_outflow_24h_minor`**
#: and capped at **`amount_in_24h_minor`**. The first is the graph layer's fold-scoped measure of "the
#: money that left the node *and the nodes it pays directly*" inside the 24 hours ending at the fold's
#: cutoff (:class:`oxbow.features.fold_providers.GraphFeatureProvider`, ``config/features.yaml`` group
#: ``exposure``), which is the stored column whose definition matches the spec's — account plus
#: 1-hop downstream, same window. The cap is read from the stored inflow of the same window. Two
#: differences from §3.2 are stated rather than smoothed over, because a reader who does not know
#: them will over-read the number: the frame records inflow to the **subject** while §3.2 caps against
#: inflow to the **cluster**, so the cap applied here is never looser than the spec's; and the two
#: windows have different anchors (fold cutoff vs the scored row's event timestamp), which the frame
#: cannot reconcile because it stores no per-cluster inflow at all.
#: **`amount_out_24h_minor`** measures the subject's own outflow only — no downstream legs — and is
#: deliberately NOT used as a fallback when ``downstream_outflow_24h_minor`` is absent. Swapping in a
#: narrower definition to fill a null is the substitution this module's first rule exists to stop, and
#: the refusal names both columns so the choice is visible in the output rather than only here.
ECONOMICS_EXPOSURE_SOURCE: Final = "downstream_outflow_24h_minor"
ECONOMICS_INFLOW_CAP_SOURCE: Final = "amount_in_24h_minor"
#: The frame's own currency column, when the producer kept the corpus's currency dimension.
#: ``config/features.yaml`` groups the money features by ``[entity, currency]`` and the score stage
#: collapses that dimension before writing ``scored_rows.parquet``, so on the landed 40k run the
#: column is absent and the currency is ``config/economics.yaml``'s declaration — which is what the
#: costs are denominated in, so it is the only basis there is to price against. A frame that DOES
#: carry one is cross-checked against the configuration rather than trusted, and a disagreement
#: raises: the exposure would otherwise be multiplied by a per-minute price stated in another money,
#: which is a difference of two unrelated amounts and not an expected value.
ECONOMICS_CURRENCY_SOURCE: Final = "currency"
#: The scored frame's fused probability. ``SCORE_SOURCES`` already maps it into
#: ``score.calibrated_probability`` for a fold that calibrated and ``score_rows`` refuses a row
#: without it, so this layer reads the exact number the queue ranks on and no other.
ECONOMICS_PROBABILITY_SOURCE: Final = "p_fused"
#: The three ``economics`` columns that ``models.py`` declares NOT NULL and that no artifact this
#: repository writes measures per account. ``mc_runs``, ``mc_seed`` and ``mc_interval`` ARE declared
#: by ``config/economics.yaml`` (``monte_carlo.*``) — they describe the intended experiment. The
#: quantiles are results of an experiment that has to be run per account against the component's
#: edge list (:func:`oxbow.quant.monte_carlo.simulate_exposure_interval`), and the score stage never
#: runs it: it builds its graph in memory for the rules layer and does not land it, so the artifact
#: the propagator would read (`out/graph/<run>/pairs.parquet`) is the one :func:`_graph_tables`
#: already reports as missing. Landing them is therefore refused by name, not filled.
ECONOMICS_UNMEASURED_COLUMNS: Final = ("mc_p05_minor", "mc_p50_minor", "mc_p95_minor")
#: How many accounts a gap line names before switching to a count. A 43k-entry refusal list would
#: bury the other reasons, and the count is the finding.
_MC_GAP_NAMED: Final = 6


def _mc_gap_line(accounts: Sequence[str], *, supplied: bool, config: Economics) -> str:
    """The refusal every account without a measured exposure interval gets, phrased once."""
    named = ", ".join(sorted(accounts)[:_MC_GAP_NAMED])
    more = f" and {len(accounts) - _MC_GAP_NAMED} more" if len(accounts) > _MC_GAP_NAMED else ""
    header = (
        f"economics: {len(accounts):,} account(s) have no simulated exposure interval"
        f" ({named}{more})"
    )
    return (
        f"{header}. `{config.source_path.name}` declares monte_carlo.runs/seed/interval — those "
        "describe the experiment — while "
        f"{', '.join(f'`economics.{column}`' for column in ECONOMICS_UNMEASURED_COLUMNS)} are its "
        "results, and the run recorded none: `oxbow score` builds its graph in memory for the rules "
        "layer and never lands the edge list "
        "(`oxbow.quant.monte_carlo.simulate_exposure_interval`) would propagate. A zero would claim "
        "a distribution concentrated at nothing and a config `runs` would claim "
        f"{config.monte_carlo.runs:,} draws that were never taken, so the rows are refused and "
        "counted instead. Supply measured intervals through `intervals=` and the same accounts "
        "land; note that `oxbow.ports.case_sink.EconomicsBlock` already makes this block optional "
        "for the packet, so it is the warehouse table's NOT NULL set that is out of step with the "
        "money boundary the rest of the product uses."
        if not supplied
        else (
            f"{header}. `economics.{', '.join(ECONOMICS_UNMEASURED_COLUMNS)}` are NOT NULL and this "
            "run's caller supplied intervals for the rest, so these accounts stay unpriced rather "
            "than borrowing another account's distribution."
        )
    )


def _assumptions_record(
    config: Economics,
    *,
    exposure_column: str,
    cap_column: str,
    probability_column: str,
    score: Mapping[str, Any],
    capped: bool,
    outflow_minor: int,
    currency: str,
    currency_from_frame: bool,
) -> dict[str, Any]:
    """The assumption line that travels with every money figure on this row (plan §13).

    Stored on the row rather than joined, so a later edit of ``config/economics.yaml`` cannot
    reinterpret an older run's money: the numbers below are the ones this row's arithmetic used.
    """
    return {
        "source": f"config/{config.source_path.name}",
        "currency": currency,
        "currency_is_frame_measurement": currency_from_frame,
        "minor_units_per_major": config.minor_units_per_major,
        "recovery.rate": config.recovery.rate,
        "recovery.sensitivity_band": list(config.recovery.band),
        "analyst.cost_per_minute_minor": config.analyst.cost_per_minute_minor,
        "analyst.min_review_minutes": config.analyst.min_review_minutes,
        "friction_cost_minor": config.friction_cost.minor,
        "review_minutes_by_alert_class": dict(config.review_minutes_by_alert_class),
        "exposure.window_hours": config.exposure.window_hours,
        "exposure.downstream_hops": config.exposure.downstream_hops,
        "capacity.review_minutes_per_period": config.capacity.review_minutes_per_period,
        "ev_formula": "EV_i = p_i * E_i * r - c_i - (1 - p_i) * f, ranked by EV_i / m_i",
        # Where each term was read from, so a reviewer can go to the column rather than the code.
        "exposure_source_column": exposure_column,
        "exposure_cap_column": cap_column,
        "exposure_capped_by_inflow": capped,
        "exposure_before_cap_minor": outflow_minor,
        "probability_source_column": probability_column,
        # The confidence label is part of the money row, not a tooltip: plan §12.8 and this file's
        # own rules both say an uncalibrated figure may not reach a reader unlabelled, and
        # `apps/api/routers/cases.py` renders every key of this column as an assumption line beside
        # the figures it qualifies. `CalibratedScore.confidence_label` is the pipeline's own wording
        # for both states, so the queue and the case page cannot drift into two different sentences.
        "calibration_kind": score["calibration_kind"],
        "probability_is_uncalibrated": score["calibration_kind"] != "calibrated_band",
        "confidence_label": score["confidence_label"],
        "monte_carlo_propagated": True,
        "disclaimer": (
            "Monetary figures are model estimates derived from the stated assumptions, not measured "
            "outcomes, and are not validated for operational use by any financial institution."
        ),
    }


def economics_rows(
    scored: pl.DataFrame,
    *,
    config: Economics,
    role: str = "test",
    intervals: Mapping[str, MonteCarloInterval] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One priced ``economics`` row per landed ``score`` row's account, plus the refusals.

    THE GRAIN is the queue's, not the frame's: :func:`_current_score_per_account`, the same rule
    ``score_rows`` uses, so an account scored in two folds is priced once and on the same row the
    score table shows. Pricing a stale fold while the queue shows the current one is DEV-026's
    double-charge wearing a money column.

    THE ARITHMETIC is not implemented here. :func:`oxbow.quant.ev.price_account` is called once per
    account, because it already does the §11 formula in integer minor units over probabilities
    carried as micro-ratios, already raises on a currency disagreement, already refuses to divide
    ``m_i`` below ``analyst.min_review_minutes``, and already defines the ``(-density, account_key)``
    order the allocator ranks on. A second implementation of ``EV_i`` is how the queue and the case
    page start disagreeing about the same money, which is the failure this whole seam exists to
    prevent.

    THE PROBABILITY is ``p_fused``, calibrated or not, and an uncalibrated one still prices. That is
    a decision, and it is not a new one: ``apps/api/policy_engine.stored_priced_rows`` already reads
    the stored score the same way on the read side and its own comment cites DEV-024 — "pricing an
    uncalibrated alert is the documented position; calling it calibrated is not". Refusing to price
    an uncalibrated account would leave this run's 43,046 accounts unpriced and the queue
    unrankable, i.e. the empty screen again from the other side. So the row prices, and it says so
    on its face: ``CalibratedScore`` is built with the band's rate and population exactly as
    ``score_rows`` landed them — both ``None`` for an uncalibrated fold — so
    ``confidence_label`` comes out in the pipeline's own words ("probabilities are uncalibrated: no
    observed rate was measured for the fold that scored this account"), and it is stored in this
    row's ``assumptions`` column along with ``probability_is_uncalibrated`` and the source frame's
    ``calibration_kind``. ``calibrated_probability``, ``observed_rate`` and ``calibration_n`` are
    never written into anything that calls itself calibrated. What the label does NOT do is make the
    figure trustworthy: an uncalibrated EV is a ranking device under stated assumptions, and the
    row hands the API the vocabulary to say exactly that.

    THE INTERVAL is the other gate, and it is not a labelling question. ``models.py`` declares
    ``mc_p05_minor``, ``mc_p50_minor`` and ``mc_p95_minor`` NOT NULL, and they are the results of a
    per-account propagation run (:func:`oxbow.quant.monte_carlo.simulate_exposure_interval` over the
    fold's landed edge list) that ``oxbow score`` never performs and never can retroactively: the
    graph it scores against is built in memory and not written. So ``intervals`` is the measured
    answer, supplied by a caller who ran it, and with nothing supplied every account is refused by
    name — because a zero would claim a distribution concentrated at nothing, and the config's
    ``monte_carlo.runs`` would claim 10,000 draws that were never taken. Either is a fabrication with
    a convincing face, and a row that does not exist is a smaller lie than a row that invents one of
    its own columns. See :func:`drift_period_rows`, which refuses ``bad_rate`` on exactly this
    reasoning, and note that ``ports/case_sink.EconomicsBlock`` already treats this block as optional
    at the money boundary the packet crosses: it is the warehouse table that is out of step.

    A ``*_minor`` column is an integer or the row does not exist. ``assert_money_is_integer_minor``
    runs again in both sinks, but it runs after the row was built: a float that got this far has
    already been through a rounding rule nobody measured, so :func:`_integer` bars it here.
    """
    required = {
        "account_key",
        "band",
        ECONOMICS_PROBABILITY_SOURCE,
        ECONOMICS_EXPOSURE_SOURCE,
        ECONOMICS_INFLOW_CAP_SOURCE,
    }
    missing = sorted(required - set(scored.columns))
    if missing:
        raise LandingError(
            f"the scored frame cannot populate {missing} for `economics`, so no account can be "
            f"priced: E_i is read from {ECONOMICS_EXPOSURE_SOURCE} capped at "
            f"{ECONOMICS_INFLOW_CAP_SOURCE} and p_i from {ECONOMICS_PROBABILITY_SOURCE}"
        )

    # Asked first, and its answer is the only account set this table may price: the calibration
    # state, the band, the rate and the population all come off the row the queue will show, so the
    # money and the score cannot be two different readings of the same account.
    scores, score_refusals = score_rows(scored, role=role)
    landed = {str(row["account_key"]): row for row in scores}
    part = _require_out_of_sample(
        _current_score_per_account(scored.filter(pl.col("role") == role)),
        table="economics",
        role=role,
    )

    supplied = dict(intervals or {})
    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    unmeasured: list[str] = []

    for record in part.to_dicts():
        account_key = _account_key(record.get("account_key"))
        if account_key is None:
            raw = str(record.get("account_key"))
            refused.append(
                f"account {raw[:32]!r} ({len(raw)} characters): not a {ACCOUNT_KEY_LEN}-character "
                "key, so it cannot be priced into `economics.account_key` (CHAR"
                f"({ACCOUNT_KEY_LEN})) — a truncated key would price a different account"
            )
            continue
        score = landed.get(account_key)
        if score is None:
            # Refused by `score_rows` for a named reason, which that function already reported.
            # Re-refusing it here would report one gap twice under two wordings.
            continue

        outflow = _integer(record.get(ECONOMICS_EXPOSURE_SOURCE))
        inflow = _integer(record.get(ECONOMICS_INFLOW_CAP_SOURCE))
        if outflow is None:
            refused.append(
                f"account {account_key}: `{ECONOMICS_EXPOSURE_SOURCE}` is "
                f"{record.get(ECONOMICS_EXPOSURE_SOURCE)!r}, not a measured amount, so E_i was never "
                "observed for this fold (the feature's own null policy is null_when_unobserved: the "
                "fold's graph carried no downstream edge, DEV-011). The account is left unpriced "
                "rather than priced at E_i = 0, and `amount_out_24h_minor` is not substituted for "
                "it: that column measures the subject's outflow only, with no downstream legs, so "
                "using it would answer a different question and let the reader believe otherwise"
            )
            continue
        if inflow is None:
            refused.append(
                f"account {account_key}: `{ECONOMICS_INFLOW_CAP_SOURCE}` is "
                f"{record.get(ECONOMICS_INFLOW_CAP_SOURCE)!r}. The inflow cap is part of E_i's "
                "definition, not a refinement of it, so an uncapped exposure would be a larger "
                "number than the plan licenses and no other stored column carries the window's "
                "inflow"
            )
            continue
        if outflow < 0:
            refused.append(
                f"account {account_key}: {ECONOMICS_EXPOSURE_SOURCE}={outflow} is negative, and a "
                "cluster cannot have removed less than nothing; an absolute value here would be a "
                "sign correction nobody measured"
            )
            continue

        interval = supplied.get(account_key) or None
        if interval is None:
            unmeasured.append(account_key)
            continue

        capped = inflow < outflow
        exposure_minor = inflow if capped else outflow
        currency_from_frame = ECONOMICS_CURRENCY_SOURCE in scored.columns
        currency = (
            str(record.get(ECONOMICS_CURRENCY_SOURCE)) if currency_from_frame else config.currency
        )
        exposure = Money(exposure_minor, currency)

        # `calibrated_probability` when the fold measured one, `fused_score` when it did not — which
        # is the same branch, in the same order, the read side takes, so a row priced here and a row
        # re-priced by `/api/alerts` start from one number.
        probability = score["calibrated_probability"]
        if probability is None:
            probability = score["fused_score"]
        score_row = CalibratedScore(
            account_key=account_key,
            p_calibrated=float(probability),
            alert_class=str(score["band"]),
            band_observed_rate=score["observed_rate"],
            band_n=score["calibration_n"],
        )
        # Raises `CurrencyMismatchError` on a foreign exposure and `PricingError` on a review time
        # below the configured floor. Both propagate: the first is a build that committed to one
        # money meeting a row that contradicts it, the second is a registry/config contradiction,
        # and defaulting either would price an alert on a term the configuration rejects.
        priced = price_account(score_row, exposure, config)

        rows.append(
            {
                "account_key": account_key,
                "currency": priced.exposure.currency,
                "exposure_minor": priced.exposure.minor,
                "expected_value_minor": priced.ev.minor,
                # Gross p*E*r: `expected_loss_avoided` is defined as the gross figure, and netting
                # the two cost terms off it here would double-count them against EV.
                "loss_avoided_minor": priced.expected_intercept.minor,
                "analyst_cost_minor": priced.review_cost.minor,
                "friction_cost_minor": priced.expected_friction_cost.minor,
                "analyst_minutes": priced.review_minutes,
                "recovery_rate": config.recovery.rate,
                "ev_density": priced.density_ratio,
                "mc_runs": interval.runs,
                "mc_seed": interval.seed,
                "mc_p05_minor": interval.p05_minor,
                "mc_p50_minor": interval.p50_minor,
                "mc_p95_minor": interval.p95_minor,
                "mc_interval": list(interval.interval),
                "assumptions": _assumptions_record(
                    config,
                    exposure_column=ECONOMICS_EXPOSURE_SOURCE,
                    cap_column=ECONOMICS_INFLOW_CAP_SOURCE,
                    probability_column=ECONOMICS_PROBABILITY_SOURCE,
                    score={**score, "confidence_label": score_row.confidence_label},
                    capped=capped,
                    outflow_minor=outflow,
                    currency=currency,
                    currency_from_frame=currency_from_frame,
                ),
            }
        )

    if unmeasured:
        refused.append(_mc_gap_line(unmeasured, supplied=bool(supplied), config=config))
    # THE ORDER, STATED: EV density descending, account key ascending — `positive_ev_rows`' and
    # `price_exposures`' own key, so the landed order is the order the allocator would have produced
    # and the capacity cutoff line falls between the same two rows whichever side draws it.
    rows.sort(key=lambda row: (-row["ev_density"], row["account_key"]))
    return rows, score_refusals + refused


#: ``scorecard_point`` and ``scorecard_bin`` both come out of the frame's ``points_json`` payload:
#: one entry per admitted attribute with the bin the account fell in, the frozen WOE and the integer
#: points. The bin table's population columns (``population_share``, ``bad_rate``, ``n``) are counted
#: over the same landed rows, because the fitted ``FeatureBinning`` — which does carry them at
#: fit time — is not persisted by any path the CLI runs.
#: ``attribute`` is the feature key in BOTH tables so the studio's join and the case rail's
#: "scorecard_point vs scorecard_bin vs band_definition" comparison line up; the registry's English
#: sentence stays in the artifact and in ``score.reason_codes`` rather than being copied into a data
#: row, which is the same rule ``rule_hit.rule_name`` follows by storing the id twice.
SCORECARD_ENTRY_FIELDS: Final = ("feature", "bin_label", "bin_kind", "points", "woe")
#: ``reason_code`` is a code, and the artifact renders one only for the three largest negative
#: contributions (``oxbow.scoring.reasons``), as a sentence too long for the column. The code stored
#: is therefore the attribute's own key — the id, kept twice, exactly as ``rule_hit.rule_name`` does
#: — because inventing a code the producer never issued would be a name nobody else recognises.
SCORECARD_POINT_SOURCES: Final[dict[str, str | None]] = {
    "account_key": "account_key",
    "attribute": "feature",
    "bin_label": "bin_label",
    "points": "points",
    "woe": "woe",
    "reason_code": None,  # the attribute key, stored as the id
    "population_share": None,  # counted over the run's landed rows, per attribute
    "bad_rate": None,  # counted over the run's landed rows, per bin
}
#: THE ORDER, STATED: a bin's index is its position among its attribute's bins sorted by WOE
#: descending, ties broken by the bin label ascending. The fit-time bin order is not in the
#: artifact, so the order is declared here; the label carries the edges a reader wants anyway.
_BIN_INDEX_KEY: Final = ("woe_descending", "bin_label_ascending")


def _scorecard_entries(
    scored: pl.DataFrame, *, role: str
) -> tuple[list[tuple[str, int, dict[str, Any]]], list[str], dict[str, set[str]]]:
    """(account_key, position, entry) over the run's current out-of-sample rows, plus refusals.

    A row whose payload repeats an attribute refuses that whole row: ``uq_scorecard_point`` is
    ``(run_id, account_key, attribute)``, and two entries for one attribute is the duplicate-key
    crash DEV-026 found at the ledger, arriving here instead.

    An entry whose own figures are unreadable refuses *that attribute on that account*, counted per
    attribute rather than printed per account: one categorical bin label longer than the
    128-character column would otherwise emit a refusal line for every account the scorecard
    touched, and a finding printed 43,046 times is noise, not a finding.

    The third return value maps an account to the attributes dropped from ITS OWN row. It exists
    because ``scorecard_point_rows`` publishes a per-account list the case page sums against
    ``band_definition``'s point ranges, and an account missing one attribute would read as a SAFER
    account — so an account with a dropped entry lands nothing. It stays per-account because
    making it run-level would let one corrupt row empty the table of 43,000 accounts' rows.
    """
    if "points_json" not in scored.columns:
        raise LandingError(
            "the scored frame carries no `points_json`, so no attribute contribution exists to "
            "land; the artifact that would be needed is out/score/<run>/scored_rows.parquet"
        )
    part = _require_out_of_sample(
        _current_score_per_account(scored.filter(pl.col("role") == role)),
        table="scorecard_bin / scorecard_point",
        role=role,
    )
    missing = [name for name in ("label_is_fraud",) if name not in part.columns]
    if missing:
        raise LandingError(
            f"the scored frame has no {missing} column, so a bin's bad rate cannot be counted"
        )

    entries: list[tuple[str, int, dict[str, Any]]] = []
    refused: list[str] = []
    dropped: dict[tuple[str, str], int] = {}
    dropped_by_account: dict[str, set[str]] = {}
    for position, record in enumerate(part.to_dicts()):
        pending: list[tuple[str, int, dict[str, Any]]] = []
        account_key = str(record.get("account_key", ""))
        payload = _load_json(
            record.get("points_json"), column="points_json", account_key=account_key
        )
        if not isinstance(payload, list) or not payload:
            refused.append(
                f"account {account_key[:ACCOUNT_KEY_LEN]}: points_json is not a list of entries"
            )
            continue
        label = record.get("label_is_fraud")
        if not isinstance(label, int) or isinstance(label, bool) or label not in (0, 1):
            refused.append(
                f"account {account_key[:ACCOUNT_KEY_LEN]}: label_is_fraud={label!r} is not a 0/1 "
                "label, so the bins it sits in would count an unlabelled row in a bad rate"
            )
            continue
        seen: set[str] = set()
        fatal: list[str] = []
        for entry in payload:
            if not isinstance(entry, Mapping):
                fatal.append("an entry is not a mapping")
                continue
            absent = [field for field in SCORECARD_ENTRY_FIELDS if entry.get(field) is None]
            if absent:
                fatal.append(f"an entry has no value for {absent}")
                continue
            feature = _name(entry.get("feature"), limit=128)
            if feature is None:
                fatal.append(f"feature {entry.get('feature')!r} does not fit 128 characters")
                continue
            if feature in seen:
                fatal.append(f"attribute {feature} appears twice in one row")
                continue
            seen.add(feature)
            bin_label = _name(entry.get("bin_label"), limit=128)
            if bin_label is None:
                reason = "its bin label is empty or longer than the 128-character column"
                dropped[(feature, reason)] = dropped.get((feature, reason), 0) + 1
                dropped_by_account.setdefault(account_key, set()).add(feature)
                continue
            points = _integer(entry.get("points"))
            woe = _ratio(entry.get("woe"))
            if points is None or woe is None:
                reason = f"points={entry.get('points')!r}/woe={entry.get('woe')!r} is not a number"
                dropped[(feature, reason)] = dropped.get((feature, reason), 0) + 1
                dropped_by_account.setdefault(account_key, set()).add(feature)
                continue
            pending.append(
                (
                    account_key,
                    position,
                    {
                        "attribute": feature,
                        "bin_label": bin_label,
                        "bin_kind": str(entry.get("bin_kind")),
                        "points": points,
                        "woe": woe,
                        "is_bad": int(label),
                        "fold": _integer(record.get("fold")),
                    },
                )
            )
        if fatal:
            # The row is refused as a unit: an entry landed beside a refused duplicate would put a
            # second weight on the attribute the table can only hold once.
            refused.append(
                f"account {account_key[:ACCOUNT_KEY_LEN]}: " + "; ".join(sorted(set(fatal)))
            )
            continue
        entries.extend(pending)

    for (feature, reason), count in sorted(dropped.items()):
        refused.append(
            f"attribute {feature}: {count:,} contribution(s) refused because {reason}; the points "
            "still count in score.scorecard_points, but the row has no bin to land against, so "
            "no account lands a scorecard_point list without it"
        )
    return entries, refused, dropped_by_account


def _bin_table(
    entries: Sequence[tuple[str, int, dict[str, Any]]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], set[str], list[str]]:
    """Aggregate the landed contributions into one row per (attribute, bin).

    WOE and points are frozen at fit time, so every row that falls in a bin must report the identical
    pair. It usually does not: each fold of a walk-forward run fits its own scorecard, so the same
    label carries a different weight in fold 0 and fold 3, and ``scorecard_bin`` — keyed
    ``(run_id, attribute, bin_index)``, with no fold in the key — has no row that could say which
    fold's weight it is holding.

    The whole attribute therefore refuses, not just the offending bin. A bin table that kept the
    agreeable bins and dropped the rest would show ``population_share`` summing to less than one with
    nothing on the page saying why, and ``api.routers.cases.counterfactual`` sums a stored point row
    against these ranges to decide which band an account could have landed in — a partial point table
    reads as a smaller score, and a smaller score reads as a safer account.

    Returns the bins, the refused attribute names, and the refusal lines that name them.
    """
    bins: dict[tuple[str, str], dict[str, Any]] = {}
    conflicted: dict[tuple[str, str], tuple[Any, Any]] = {}
    carriers: dict[str, int] = {}
    folds: dict[tuple[str, str], set[int]] = {}
    for _account_key, _position, entry in entries:
        attribute = str(entry["attribute"])
        label = entry["bin_label"]
        if label is None:
            continue
        key = (attribute, str(label))
        carriers[attribute] = carriers.get(attribute, 0) + 1
        fold = entry.get("fold")
        if isinstance(fold, int):
            folds.setdefault(key, set()).add(fold)
        bucket = bins.get(key)
        if bucket is None:
            bins[key] = {
                "attribute": attribute,
                "label": str(label),
                "kind": entry["bin_kind"],
                "woe": entry["woe"],
                "points": entry["points"],
                "n": 1,
                "positives": entry["is_bad"],
            }
            continue
        if bucket["woe"] != entry["woe"] or bucket["points"] != entry["points"]:
            conflicted.setdefault(key, (bucket["woe"], bucket["points"]))
            continue
        bucket["n"] += 1
        bucket["positives"] += entry["is_bad"]

    refused_attributes = sorted({key[0] for key in conflicted})
    refusals: list[str] = []
    for attribute in refused_attributes:
        offenders = sorted(key for key in conflicted if key[0] == attribute)
        detail = "; ".join(
            f"bin {key[1]!r} reports more than one woe/points pair (first seen "
            f"{conflicted[key][0]}/{conflicted[key][1]}) across fold(s) "
            f"[{', '.join(str(fold) for fold in sorted(folds.get(key, set())))}]"
            for key in offenders[:3]
        )
        for key in offenders:
            bins.pop(key, None)
        for key in [item for item in bins if item[0] == attribute]:
            bins.pop(key, None)
        refusals.append(
            f"attribute {attribute}: {len(offenders)} bin(s) disagree across folds — {detail}. "
            "The table is keyed (run_id, attribute, bin_index) with no fold column and each fold "
            "fits its own scorecard, so the attribute has no single weight to land; what would fix "
            "it is the fitted FeatureBinning payload persisted per fold "
            "(binning.to_dict(), which no path writes to out/score/<run>/)."
        )
    for key, bucket in bins.items():
        total = carriers.get(key[0], 0)
        bucket["population_share"] = float(bucket["n"]) / float(total) if total else None
        bucket["bad_rate"] = float(bucket["positives"]) / float(bucket["n"])
    return bins, set(refused_attributes), refusals


def _scorecard_population(
    scored: pl.DataFrame, *, role: str
) -> tuple[
    list[tuple[str, int, dict[str, Any]]],
    dict[tuple[str, str], dict[str, Any]],
    set[str],
    list[str],
]:
    """The landed contributions, the bin table counted from them, and the refusals between them.

    The fifth value is the per-account record of entries dropped as unreadable. It stays
    per-account on purpose: promoting it to a run-level refusal, as the bin table's own conflicts
    are, would remove one account's corrupt attribute from EVERY account's list — and because an
    account lands whole or not at all, one bad row in a 43k-account run would empty the table.
    """
    entries, refused, dropped_by_account = _scorecard_entries(scored, role=role)
    bins, refused_attributes, bin_refusals = _bin_table(entries)
    return entries, bins, refused_attributes, refused + bin_refusals, dropped_by_account


def scorecard_bin_rows(
    scored: pl.DataFrame, *, role: str = "test"
) -> tuple[list[dict[str, Any]], list[str]]:
    """One ``scorecard_bin`` row per (attribute, bin) of every attribute the folds agreed on.

    THE ORDER, STATED: rows come out sorted by ``(attribute, bin_index)`` and ``bin_index`` follows
    :data:`_BIN_INDEX_KEY`. ``uq_scorecard_bin`` is ``(run_id, attribute, bin_index)``.
    """
    _entries, bins, _refused, refused, _dropped = _scorecard_population(scored, role=role)
    by_attribute: dict[str, list[dict[str, Any]]] = {}
    for bucket in bins.values():
        if bucket["woe"] is None or bucket["points"] is None or bucket["population_share"] is None:
            refused.append(
                f"attribute {bucket['attribute']} bin {bucket['label']!r}: no woe/points measured"
            )
            continue
        by_attribute.setdefault(str(bucket["attribute"]), []).append(bucket)

    rows: list[dict[str, Any]] = []
    for attribute in sorted(by_attribute):
        ordered = sorted(
            by_attribute[attribute], key=lambda item: (-float(item["woe"]), str(item["label"]))
        )
        for index, bucket in enumerate(ordered):
            rows.append(
                {
                    "attribute": attribute,
                    "bin_index": index,
                    "label": bucket["label"],
                    "woe": float(bucket["woe"]),
                    "points": int(bucket["points"]),
                    "population_share": float(bucket["population_share"]),
                    "bad_rate": float(bucket["bad_rate"]),
                    "n": int(bucket["n"]),
                }
            )
    return rows, refused


def scorecard_point_rows(
    scored: pl.DataFrame, *, role: str = "test"
) -> tuple[list[dict[str, Any]], list[str]]:
    """One ``scorecard_point`` row per account per attribute — for the accounts it can land whole.

    ``(run_id, account_key, attribute)`` is the key, and an account is scored again in a later fold,
    so the rows come from the one current scored row per account: the same collapse ``score_rows``
    makes and for the same reason — emitting per fold would die on the table's own duplicate key.

    An account lands all of its contributions or none. ``api.routers.cases.counterfactual`` sums the
    stored rows and matches that sum to ``band_definition``'s point ranges, so dropping seven
    attributes off a twenty-attribute scorecard would report the account as being in a safer band
    than it is. The refusal says which attributes the fold-scoped scorecards denied it.
    """
    entries, bins, refused_attributes, refused, dropped_by_account = _scorecard_population(
        scored, role=role
    )
    by_account: dict[str, list[dict[str, Any]]] = {}
    incomplete: dict[str, int] = {
        account_key: len(attributes) for account_key, attributes in dropped_by_account.items()
    }
    unreportable: set[str] = set()
    for account_key, _position, entry in entries:
        attribute = str(entry["attribute"])
        bucket = bins.get((attribute, str(entry["bin_label"])))
        row = {
            "account_key": account_key,
            "attribute": attribute,
            "bin_label": entry["bin_label"],
            "points": int(entry["points"]),
            "woe": float(entry["woe"]),
            "reason_code": attribute,
            "population_share": None if bucket is None else float(bucket["population_share"]),
            "bad_rate": None if bucket is None else float(bucket["bad_rate"]),
        }
        if len(attribute) > REASON_CODE_LEN:
            # `reason_code` copies the attribute and is the narrower column, so a feature key can
            # fit `attribute` and still be unlandable here. Truncating it would mint a code nobody
            # issued; refusing keeps the account whole-or-nothing and says which key it was.
            unreportable.add(attribute)
            incomplete[account_key] = incomplete.get(account_key, 0) + 1
            continue
        if bucket is None or attribute in refused_attributes:
            incomplete[account_key] = incomplete.get(account_key, 0) + 1
            continue
        by_account.setdefault(account_key, []).append(row)
    for attribute in sorted(unreportable):
        refused.append(
            f"scorecard_point: attribute {attribute!r} is {len(attribute)} characters and "
            f"reason_code is {REASON_CODE_LEN}; the code a case page shows IS the attribute key, "
            "so every account carrying it is refused rather than the code cut mid-word"
        )

    rows: list[dict[str, Any]] = []
    for account_key in sorted(by_account):
        if account_key in incomplete:
            continue
        rows.extend(sorted(by_account[account_key], key=lambda row: row["attribute"]))
    if incomplete:
        refused.append(
            f"scorecard_point: {len(incomplete):,} account(s) refused because at least one of "
            "their attributes either has no run-level bin table "
            f"({len(refused_attributes)} attribute(s) refused above) or arrived unreadable on "
            f"that account's own row ({len(dropped_by_account):,} account(s) affected); an "
            "account lands whole or not at all, because the case page sums these rows against "
            "band_definition's point ranges and a short list reads as a safer account"
        )
    return rows, refused


# --- drift -------------------------------------------------------------------

#: ``drift_period`` is keyed ``(run_id, attribute, period)``: PSI for one attribute over one period,
#: with the population and the bad rate of the population it describes.
DRIFT_REPORT_SOURCES: Final[dict[str, str]] = {
    "attribute": "feature",
    "period": "period",
    "psi": "psi",
    "csi": "csi_total",
    "n": "population",
}


def drift_period_rows(
    scored: pl.DataFrame, *, report: Mapping[str, Any] | None = None, role: str = "test"
) -> tuple[list[dict[str, Any]], list[str]]:
    """Per-attribute drift rows, or the reason there are none to write.

    Two refusals are structural, and both are reported rather than filled in:

    * the run's own scored frame publishes ``drift_score_psi`` — one number for the fused score —
      and on the landed 40k run it is null on every one of 43,720 rows, because
      ``oxbow.models.run.drift_gate`` only reaches the scored frame when a fold degraded, and none
      of this run's folds did;
    * ``drift_period.bad_rate`` is NOT NULL while :class:`oxbow.scoring.drift.FeatureDrift` records
      feature, period, psi, the CSI decomposition and a population — never a bad rate. So even a
      landed ``DriftReport`` would refuse every row until the drift layer carries the rate of the
      period it is describing.

    What would land it is ``DriftReport.to_dict()`` written beside the scored rows
    (``out/score/<run>/drift_report.json``) with the period's label rate on each feature entry.
    """
    if report is not None:
        features = report.get("features")
        if not isinstance(features, list):
            raise LandingError(
                "the drift report carries no `features` list, so drift_period has no rows to shape"
            )
        rows: list[dict[str, Any]] = []
        refused: list[str] = []
        for entry in features:
            if not isinstance(entry, Mapping):
                refused.append(f"drift entry {entry!r} is not a mapping")
                continue
            row = {
                "attribute": _name(_dig(entry, DRIFT_REPORT_SOURCES["attribute"]), limit=128),
                "period": _name(_dig(entry, DRIFT_REPORT_SOURCES["period"]), limit=32),
                "psi": _ratio(_dig(entry, DRIFT_REPORT_SOURCES["psi"])),
                "csi": _ratio(_dig(entry, DRIFT_REPORT_SOURCES["csi"])),
                "n": _integer(_dig(entry, DRIFT_REPORT_SOURCES["n"])),
                "bad_rate": _ratio(entry.get("bad_rate", entry.get("observed_bad_rate"))),
            }
            absent = [column for column, value in row.items() if value is None and column != "csi"]
            if absent:
                refused.append(
                    f"drift {_dig(entry, 'feature')}/{_dig(entry, 'period')}: no measurement for "
                    f"{absent}; FeatureDrift records {sorted(DRIFT_REPORT_SOURCES.values())} and "
                    "never a period bad rate"
                )
                continue
            rows.append(row)
        return sorted(rows, key=lambda item: (item["attribute"], item["period"])), refused

    if "drift_score_psi" not in scored.columns:
        raise LandingError(
            "the scored frame carries no drift column at all, so no drift figure was measured for "
            "this run; the artifact that would be needed is out/score/<run>/drift_report.json"
        )
    part = scored.filter(pl.col("role") == role) if "role" in scored.columns else scored
    measured = part.get_column("drift_score_psi").drop_nulls().len() if part.height else 0
    return (
        [],
        [
            f"drift_period: `drift_score_psi` is null on all {part.height:,} out-of-sample row(s) "
            f"({measured} measured), and it is one score-level PSI whatever its value while "
            "drift_period is keyed per (attribute, period). The run lands no drift rows, so the "
            "studio's drift pane reports the absence rather than a zero."
        ],
    )


# --- the case workspace's evidence ------------------------------------------

#: ``transaction`` is the case rail's evidence table, and the canonical event frame is its one
#: source: every column is carried, not derived. ``txn_id`` arrives already corpus-namespaced
#: (``paysim:da6ad804…``, DEV-004) and the port re-checks the namespace by name.
TRANSACTION_SOURCES: Final[dict[str, str]] = {
    "txn_id": "txn_id",
    "event_ts_utc": "event_ts_utc",
    "event_date_local": "event_date_local",
    "local_hour": "local_hour",
    "src_account_key": "account_from",
    "dst_account_key": "account_to",
    "amount_minor": "amount_minor",
    "currency": "currency",
    "txn_type": "txn_type",
    "src_balance_before": "src_balance_before_minor",
    "src_balance_after": "src_balance_after_minor",
    "dst_balance_before": "dst_balance_before_minor",
    "dst_balance_after": "dst_balance_after_minor",
    "label_fraud": "label_is_fraud",
    "label_typology": "label_typology",
    "source_dataset": "source_dataset",
}
#: The columns the table declares NOT NULL. The rest are nullable because a corpus need not carry
#: them — IBM-AML has no balances — and ``models.py`` says a zero-filled absent column would be a
#: fabricated value.
TRANSACTION_REQUIRED: Final = frozenset(
    {
        "txn_id",
        "event_ts_utc",
        "event_date_local",
        "local_hour",
        "amount_minor",
        "currency",
        "txn_type",
        "source_dataset",
    }
)
TRANSACTION_MONEY: Final = frozenset(
    {
        "amount_minor",
        "src_balance_before",
        "src_balance_after",
        "dst_balance_before",
        "dst_balance_after",
    }
)
#: THE ORDER, STATED: rows come out sorted by ``txn_id``, the table's own primary key, so a re-run
#: of the same slice lands the same sequence whatever order the batches were read in.


def _account_key(value: Any) -> str | None:
    """An account key that fits ``CHAR(12)``, or None — a longer key would be silently cut."""
    if value is None:
        return None
    text = str(value)
    return text if 0 < len(text) <= ACCOUNT_KEY_LEN else None


def transaction_rows(events: pl.DataFrame) -> tuple[list[dict[str, Any]], list[str]]:
    """The slice's canonical events as ``transaction`` rows, with the refusals beside them.

    An event whose money is not an integer refuses the row. ``amount_minor`` is the column a packet
    totals, and a float there does not crash — it drifts (DEV-005), and the row that carried it
    would carry it into every sum downstream.
    """
    required = {"txn_id", "amount_minor", "currency", "event_ts_utc"}
    missing = sorted(required - set(events.columns))
    if missing:
        raise LandingError(
            f"the event frame is missing {missing}, so no transaction can be described"
        )

    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    seen: set[str] = set()
    for record in events.sort("txn_id").to_dicts():
        txn_id = _name(record.get("txn_id"), limit=64)
        if txn_id is None:
            refused.append(f"event {record.get('txn_id')!r}: no txn_id that fits the column")
            continue
        if txn_id in seen:
            refused.append(
                f"transaction {txn_id}: the frame repeats it, and txn_id is the table's primary key"
            )
            continue
        problems: list[str] = []
        row: dict[str, Any] = {}
        for column, producer in TRANSACTION_SOURCES.items():
            value = record.get(producer)
            if column == "txn_id":
                row[column] = txn_id
                continue
            if column in TRANSACTION_MONEY:
                parsed: Any = _integer(value)
                if parsed is None and value is not None:
                    problems.append(f"{column} ({producer}={value!r}) is not integer minor units")
                    continue
            elif column in {"src_account_key", "dst_account_key"}:
                parsed = _account_key(value)
                if parsed is None and value is not None:
                    problems.append(f"{column} ({producer}={value!r}) is not a 12-character key")
                    continue
            elif column == "local_hour":
                parsed = _integer(value)
            elif column == "label_fraud":
                number = _integer(value)
                parsed = None if number is None or number not in (0, 1) else bool(number)
            elif column in {"currency", "txn_type", "source_dataset"}:
                limit = {
                    "currency": CURRENCY_LEN,
                    "txn_type": 32,
                    "source_dataset": 64,
                }[column]
                parsed = _name(value, limit=limit)
            elif column in {"event_ts_utc", "event_date_local"}:
                parsed = value if isinstance(value, datetime | date) else None
            else:
                parsed = _name(value, limit=64)
            if parsed is None and value is not None:
                problems.append(f"{column} ({producer}={str(value)[:40]!r}) is unreadable")
                continue
            if value is None and column in TRANSACTION_REQUIRED:
                problems.append(f"{column} ({producer}) was not measured")
                continue
            row[column] = parsed
        if problems:
            refused.append(f"transaction {txn_id}: " + "; ".join(problems))
            continue
        if row.get("src_account_key") is None and row.get("dst_account_key") is None:
            refused.append(
                f"transaction {txn_id}: neither side names an account, so no case could ever "
                "read it as evidence"
            )
            continue
        seen.add(txn_id)
        rows.append(row)
    return rows, refused


#: ``evidence_event`` is one row of a case's timeline. Two kinds are measured by the artifacts this
#: stage already reads: the transactions the account appears in, and the rules that fired on it.
#: A non-firing rule stays in ``rule_hit`` (where its margin is evidence); a timeline of things that
#: did not happen is not. ``screening`` and ``watchlist`` events are the API's own write path, not
#: the pipeline's, so they are absent here rather than empty.
EVIDENCE_KIND_TRANSACTION: Final = "transaction"
EVIDENCE_KIND_RULE_HIT: Final = "rule_hit"
#: THE ORDER, STATED: (account_key, occurred_at, kind, label, txn_id) — the table's read order is
#: the timeline's, and the tie-break on txn_id is what makes two runs of the same slice agree.


def evidence_event_rows(
    events: pl.DataFrame, scored: pl.DataFrame, *, role: str = "test"
) -> tuple[list[dict[str, Any]], list[str]]:
    """The run's landed events and fired rules as per-account timeline rows."""
    for column in ("txn_id", "account_from", "account_to", "event_ts_utc", "txn_type"):
        if column not in events.columns:
            raise LandingError(f"the event frame has no {column!r}, so no timeline can be built")

    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    for record in events.to_dicts():
        txn_id = _name(record.get("txn_id"), limit=64)
        occurred = record.get("event_ts_utc")
        label = _name(record.get("txn_type"), limit=128)
        if txn_id is None or not isinstance(occurred, datetime) or label is None:
            refused.append(
                f"event {str(record.get('txn_id'))[:40]}: no txn_id, timestamp or type to put on a "
                "timeline (all three are NOT NULL on evidence_event)"
            )
            continue
        amount = _integer(record.get("amount_minor"))
        if amount is None:
            # Refused once, for the event: `transaction_rows` bars the same event from the money
            # table, and a timeline row whose amount is null reads as a payment of nothing rather
            # than as an amount that could not be read.
            refused.append(
                f"event {txn_id}: amount_minor={record.get('amount_minor')!r} is not an integer "
                "minor-unit amount (DEV-005), so it is left off the timeline rather than shown "
                "as an amount-less payment"
            )
            continue
        for side, key_column, other in (
            ("outbound", "account_from", "account_to"),
            ("inbound", "account_to", "account_from"),
        ):
            raw_side = record.get(key_column)
            account_key = _account_key(raw_side)
            if account_key is None:
                if raw_side is not None:
                    # A null side is a one-sided event, which is a fact. A side that is present
                    # but not a 12-character key would silently delete that account's view of the
                    # payment while the other side still landed.
                    refused.append(
                        f"event {txn_id} {side} side: {raw_side!r} is not a "
                        f"{ACCOUNT_KEY_LEN}-character account key, so the holder of that side "
                        "gets no timeline row"
                    )
                continue
            rows.append(
                {
                    "account_key": account_key,
                    "occurred_at": occurred,
                    "kind": EVIDENCE_KIND_TRANSACTION,
                    "label": label,
                    "txn_id": txn_id,
                    "rule_id": None,
                    "detail": {
                        "direction": side,
                        "counterparty": record.get(other),
                        "amount_minor": amount,
                        "currency": record.get("currency"),
                        "source_dataset": record.get("source_dataset"),
                    },
                }
            )

    if "role" in scored.columns:
        part = _current_score_per_account(scored.filter(pl.col("role") == role))
    else:
        raise LandingError(
            "the scored frame carries no `role` column, so no out-of-sample rule firing can be "
            "placed on a timeline"
        )
    severity_columns = [column for column in part.columns if _rule_id(str(column)) is not None]
    for record in part.to_dicts():
        account_key = _account_key(record.get("account_key"))
        occurred = record.get("as_of_ts")
        if account_key is None or not isinstance(occurred, datetime):
            raw_key = str(record.get("account_key"))
            refused.append(
                f"account {raw_key!r} ({len(raw_key)} characters): no key or as-of timestamp, so "
                f"its rule firings cannot be placed on a timeline"
            )
            continue
        for column in severity_columns:
            rule_id = _rule_id(str(column))
            severity = _ratio(record.get(column))
            if rule_id is None or severity is None or severity <= 0.0:
                continue
            rows.append(
                {
                    "account_key": account_key,
                    "occurred_at": occurred,
                    "kind": EVIDENCE_KIND_RULE_HIT,
                    "label": rule_id,
                    "txn_id": None,
                    "rule_id": rule_id,
                    "detail": {
                        "severity": severity,
                        "rule_column": column,
                        "fold": _integer(record.get("fold")),
                        "scoring_mode": record.get("scoring_mode"),
                    },
                }
            )

    rows.sort(
        key=lambda row: (
            row["account_key"],
            row["occurred_at"],
            row["kind"],
            row["label"],
            str(row["txn_id"]),
        )
    )
    return rows, refused


# --- the graph the `graph` stage lands ----------------------------------------

#: ``community`` carries the deterministic remap that makes ids stable across runs
#: (``config/pipeline.yaml``: size-then-min-key). ``density`` and ``total_minor`` are nullable: the
#: graph artifact measures a per-node local density and never a per-community one, and a community's
#: internal money total is meaningless across two currencies, so a mixed community keeps both unset.
GRAPH_EDGE_SOURCES: Final[dict[str, str]] = {
    "src_account_key": "account_from",
    "dst_account_key": "account_to",
    "txn_count": "edge_count",
    "total_minor": "total_value_minor",
    "currency": "currency",
    "first_ts": "first_ts_us",
    "last_ts": "last_ts_us",
}
NODE_COMMUNITY_COLUMN: Final = "community_id"
#: THE ORDER, STATED for ``community``: by the canonical rule itself — size descending, then the
#: smallest account key in the community ascending — so ``canonical_index`` 0 is the biggest
#: community and the numbering is a function of the artifact, not of dict order.
CANONICAL_COMMUNITY_ORDER: Final = "size_then_min_node_key"


def _moment_from_micros(value: Any) -> datetime | None:
    """A UTC instant from the graph artifact's epoch-microseconds column."""
    micros = _integer(value)
    if micros is None:
        return None
    return datetime.fromtimestamp(micros / 1_000_000, tz=UTC)


def community_index_by_raw_label(
    nodes: pl.DataFrame,
) -> tuple[dict[int, int], list[dict[str, Any]], list[str]]:
    """The canonical remap, the community rows it describes, and the refusals beside them.

    Returned as one function because ``community`` and ``account_membership`` must agree on the
    numbering: a membership row pointing at a different index than the community table is a border
    colour on the wrong node, and nothing in the schema would catch it.
    """
    for column in ("account", NODE_COMMUNITY_COLUMN):
        if column not in nodes.columns:
            raise LandingError(
                f"the graph artifact's nodes frame has no {column!r}, so communities cannot be "
                "described; the file that would carry it is out/graph/<run>/nodes.parquet"
            )
    members = nodes.filter(pl.col(NODE_COMMUNITY_COLUMN).is_not_null())
    if members.height == 0:
        return (
            {},
            [],
            [
                "community: every node's community_id is null, because the graph stage recorded a "
                "community_skipped_reason instead of a partition — the absence is the measurement "
                "(DEV-011, a star-shaped corpus)"
            ],
        )
    grouped = members.group_by(NODE_COMMUNITY_COLUMN).agg(
        pl.len().alias("size"),
        pl.col("account").min().alias("min_account"),
    )
    ordered = grouped.sort(["size", "min_account"], descending=[True, False])

    rows: list[dict[str, Any]] = []
    index_by_raw: dict[int, int] = {}
    refused: list[str] = []
    for record in ordered.to_dicts():
        raw = _integer(record.get(NODE_COMMUNITY_COLUMN))
        if raw is None:
            # Named, never skipped: a community the remap cannot read is a group of nodes the
            # studio will not colour, and an empty community table with no refusal beside it is
            # indistinguishable from a graph that found no communities.
            refused.append(
                f"community group {record.get(NODE_COMMUNITY_COLUMN)!r} "
                f"(a {type(record.get(NODE_COMMUNITY_COLUMN)).__name__}, "
                f"{record.get('size')} node(s)): `{NODE_COMMUNITY_COLUMN}` has to be an integer "
                "label to be remapped to a canonical index; the frame that would carry it is "
                "out/graph/<run>/nodes.parquet"
            )
            continue
        index_by_raw[raw] = len(rows)
        rows.append(
            {
                "canonical_index": len(rows),
                "raw_label": str(raw),
                "size": _integer(record.get("size")),
            }
        )
    return index_by_raw, rows, refused


def community_rows(
    nodes: pl.DataFrame,
    pairs: pl.DataFrame,
    *,
    stats: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One ``community`` row per detected community, with the canonical index and its algorithm.

    ``algorithm`` and ``seed`` are NOT NULL and the graph artifact records them in
    ``manifest.json``'s ``settings_fingerprint.community``. A run that never wrote that manifest
    cannot describe a community, because a community without the algorithm and seed that produced it
    is not reproducible and the table exists to be reproducible.
    """
    index_by_raw, rows, refused = community_index_by_raw_label(nodes)
    if not rows:
        return rows, refused
    fingerprint = (stats or {}).get("settings_fingerprint")
    community_cfg = fingerprint.get("community") if isinstance(fingerprint, Mapping) else None
    algorithm = _name(
        community_cfg.get("algorithm") if isinstance(community_cfg, Mapping) else None, limit=32
    )
    seed = _integer(community_cfg.get("seed") if isinstance(community_cfg, Mapping) else None)
    order = community_cfg.get("canonical_order") if isinstance(community_cfg, Mapping) else None
    if algorithm is None or seed is None:
        raise LandingError(
            "the graph manifest records no community algorithm or seed; the file that would carry "
            "it is out/graph/<run>/manifest.json, and without them a community cannot be re-run"
        )
    if order is not None and str(order) != CANONICAL_COMMUNITY_ORDER:
        raise LandingError(
            f"the manifest declares canonical_order={order!r} but this mapper orders communities "
            f"by {CANONICAL_COMMUNITY_ORDER!r}; the two numberings would disagree with each other"
        )

    src = nodes.select(
        pl.col("account").alias("account_from"),
        pl.col(NODE_COMMUNITY_COLUMN).alias("src_community"),
    )
    dst = nodes.select(
        pl.col("account").alias("account_to"),
        pl.col(NODE_COMMUNITY_COLUMN).alias("dst_community"),
    )
    wanted = {"account_from", "account_to", "currency", "total_value_minor"}
    internal: dict[int, dict[str, int]] = {}
    #: Communities with at least one internal-money row that could not be stated. They get no
    #: total at all: `total_minor` of 0 would claim the community circulates nothing, and a
    #: partial sum would be a smaller wrong number rather than a true one. Both columns are
    #: nullable precisely so the absence can be recorded instead of invented.
    unstated: set[int] = set()
    if wanted <= set(pairs.columns):
        joined = (
            pairs.join(src, on="account_from", how="inner")
            .join(dst, on="account_to", how="inner")
            .filter(
                pl.col("src_community").is_not_null()
                & (pl.col("src_community") == pl.col("dst_community"))
            )
            .group_by(["src_community", "currency"])
            .agg(pl.col("total_value_minor").sum().alias("total_minor"))
        )
        for record in joined.to_dicts():
            raw = _integer(record.get("src_community"))
            total = _integer(record.get("total_minor"))
            currency = _name(record.get("currency"), limit=CURRENCY_LEN)
            where = (
                f"community {record.get('src_community')!r} " f"currency {record.get('currency')!r}"
            )
            if raw is None:
                refused.append(
                    f"{where}: its community label is not an integer, so its internal money "
                    "cannot be attributed to any community row"
                )
                continue
            if total is None:
                refused.append(
                    f"{where}: its internal `total_value_minor` sums to "
                    f"{record.get('total_minor')!r}, which is not an integer minor-unit amount "
                    "(DEV-005), so the community books no internal money"
                )
                unstated.add(raw)
                continue
            if currency is None:
                refused.append(
                    f"{where}: an amount has to name the {CURRENCY_LEN}-character currency it is "
                    f"in, so the community books no internal money rather than a sum in no "
                    "currency"
                )
                unstated.add(raw)
                continue
            internal.setdefault(raw, {})[currency] = total

    for row in rows:
        raw = int(row["raw_label"])
        currencies = internal.get(raw, {})
        if raw in unstated:
            row["total_minor"] = None
            row["currency"] = None
        elif len(currencies) == 1:
            currency, total = next(iter(currencies.items()))
            row["total_minor"] = total
            row["currency"] = currency
        elif not currencies:
            row["total_minor"] = 0
            row["currency"] = None
        else:
            row["total_minor"] = None
            row["currency"] = None
            refused.append(
                f"community {raw}: its internal edges carry {len(currencies)} currencies "
                f"({', '.join(sorted(currencies))}); one money column holds one currency, so the "
                "total is left unset rather than summed across an exchange rate nobody declared"
            )
        row["algorithm"] = algorithm
        row["seed"] = seed
        if row["size"] is None:
            refused.append(f"community {raw}: no size counted")
    return (
        sorted(rows, key=lambda row: row["canonical_index"]),
        refused,
    )


def account_membership_rows(
    nodes: pl.DataFrame, index_by_raw: Mapping[int, int]
) -> tuple[list[dict[str, Any]], list[str]]:
    """One ``account_membership`` row per node the graph placed in a community.

    ``(run_id, account_key)`` is the key and a node appears once in ``nodes``, so there is no
    collapse to do — but a node whose community id the remap does not know refuses, because a
    membership pointing at a community the run never wrote is a dangling colour, not a fact.
    """
    if not index_by_raw:
        return [], ["account_membership: the run detected no communities, so no node is a member"]
    for column in ("account", NODE_COMMUNITY_COLUMN, "total_degree"):
        if column not in nodes.columns:
            raise LandingError(
                f"the nodes frame has no {column!r}, so membership cannot be described"
            )
    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    for record in nodes.sort("account").to_dicts():
        raw = _integer(record.get(NODE_COMMUNITY_COLUMN))
        if raw is None:
            continue
        account_key = _account_key(record.get("account"))
        degree = _integer(record.get("total_degree"))
        community = index_by_raw.get(raw)
        if account_key is None or degree is None or community is None:
            refused.append(
                f"membership {str(record.get('account'))[:ACCOUNT_KEY_LEN]}: "
                + (
                    f"community {raw} is not in the run's community table"
                    if community is None
                    else "no 12-character key or no total degree measured"
                )
            )
            continue
        row: dict[str, Any] = {
            "account_key": account_key,
            "community_id": community,
            "degree": degree,
        }
        pagerank = _ratio(record.get("pagerank"))
        if pagerank is not None:
            row["pagerank"] = pagerank
        rows.append(row)
    return sorted(rows, key=lambda row: row["account_key"]), refused


def graph_edge_rows(
    pairs: pl.DataFrame, nodes: pl.DataFrame
) -> tuple[list[dict[str, Any]], list[str]]:
    """One ``graph_edge`` row per aggregated pair the graph stage published.

    ``is_rail`` is the table's per-edge reading of a per-node measurement: the artifact flags rails
    by account (``nodes.is_rail``, the degree percentile in ``manifest.json``), so an edge is a rail
    edge when either endpoint is. An endpoint the nodes frame does not know refuses the edge rather
    than defaulting it, because "not a rail" would be a claim about a node nobody measured.
    """
    missing = sorted(set(GRAPH_EDGE_SOURCES.values()) - set(pairs.columns))
    if missing:
        raise LandingError(
            f"the pairs frame has no {missing} column(s), so no edge can be described; the file "
            "that would carry them is out/graph/<run>/pairs.parquet"
        )
    if "account" not in nodes.columns or "is_rail" not in nodes.columns:
        raise LandingError(
            "the nodes frame has no account/is_rail columns, so an edge cannot be labelled a rail "
            "edge and every row would be a guess"
        )
    rails = {
        str(row["account"]): bool(row["is_rail"])
        for row in nodes.select(["account", "is_rail"]).iter_rows(named=True)
    }
    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    seen: set[tuple[str, str]] = set()
    for record in pairs.sort(["account_from", "account_to"]).to_dicts():
        src = _account_key(record.get("account_from"))
        dst = _account_key(record.get("account_to"))
        if src is None or dst is None:
            refused.append(
                f"edge {record.get('account_from')}->{record.get('account_to')}: an endpoint is not "
                "a 12-character key"
            )
            continue
        if (src, dst) in seen:
            refused.append(
                f"edge {src}->{dst}: the pairs frame repeats it, and (run_id, src, dst) is the key"
            )
            continue
        if src not in rails or dst not in rails:
            refused.append(
                f"edge {src}->{dst}: an endpoint is absent from the nodes frame, so its rail flag "
                "was never measured"
            )
            continue
        row = {
            "src_account_key": src,
            "dst_account_key": dst,
            "txn_count": _integer(record.get("edge_count")),
            "total_minor": _integer(record.get("total_value_minor")),
            "currency": _name(record.get("currency"), limit=CURRENCY_LEN),
            "first_ts": _moment_from_micros(record.get("first_ts_us")),
            "last_ts": _moment_from_micros(record.get("last_ts_us")),
            "is_rail": bool(rails[src] or rails[dst]),
            # `flags` is the artifact's own structural marks on this pair; an empty list is the
            # measured absence of one, not an unmeasured field.
            "flags": (["self_pair"] if record.get("is_self_pair") else []),
        }
        absent = [column for column, value in row.items() if value is None]
        if absent:
            refused.append(
                f"edge {src}->{dst}: no measurement for {absent} "
                f"(the pairs frame's keys are {sorted(pairs.columns)})"
            )
            continue
        seen.add((src, dst))
        rows.append(row)
    return rows, refused


__all__ = [
    "ABLATION_SOURCES",
    "ACCOUNT_COLUMNS",
    "BACKTEST_FOLD_OWNER",
    "BACKTEST_FOLD_SOURCES",
    "BAND_COLUMNS",
    "BAND_SOURCES",
    "CANONICAL_COMMUNITY_ORDER",
    "DRIFT_REPORT_SOURCES",
    "ECONOMICS_CURRENCY_SOURCE",
    "ECONOMICS_EXPOSURE_SOURCE",
    "ECONOMICS_INFLOW_CAP_SOURCE",
    "ECONOMICS_PROBABILITY_SOURCE",
    "ECONOMICS_UNMEASURED_COLUMNS",
    "EVIDENCE_KIND_RULE_HIT",
    "EVIDENCE_KIND_TRANSACTION",
    "FAIRNESS_SOURCES",
    "GRAPH_EDGE_SOURCES",
    "NODE_COMMUNITY_COLUMN",
    "PERTURBATION_SOURCES",
    "SCORECARD_ENTRY_FIELDS",
    "SCORECARD_POINT_SOURCES",
    "SCORE_COLUMNS",
    "SCORE_SOURCES",
    "TRANSACTION_MONEY",
    "TRANSACTION_REQUIRED",
    "TRANSACTION_SOURCES",
    "VALIDATION_VARIANT_METRICS",
    "LandingError",
    "ablation_rows",
    "account_membership_rows",
    "account_rows",
    "assert_run_identifiable",
    "backtest_fold_rows",
    "band_definition_rows",
    "community_index_by_raw_label",
    "community_rows",
    "drift_period_rows",
    "economics_rows",
    "evidence_event_rows",
    "fairness_rows",
    "graph_edge_rows",
    "perturbation_rows",
    "rule_hit_rows",
    "score_rows",
    "scorecard_bin_rows",
    "scorecard_point_rows",
    "transaction_rows",
    "validation_metric_rows",
]
