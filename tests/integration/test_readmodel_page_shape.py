"""The read side's work per request, measured in rows and statements — never in seconds.

Three findings from the 2026-09 audit are pinned here, all of them about *shape*:

* ``/api/alerts`` with no stored allocation read every joined row, sorted it in Python
  and took ``total`` from ``len(rows)``, so page 1 and page 40 cost the same as the whole
  run (:mod:`api.readmodel`, ``_alert_rows_from_allocations``).
* :meth:`PostgresSource.select` ran a ``count(*)`` on **every** call, including the reads
  that throw the total away, and :meth:`ReadModel.run_summary` fired four of them per run
  row, so ``GET /api/runs?limit=20`` was 100+ statements.
* A page boundary decided in SQL must not move the served order, so every sort is compared
  paged-against-unpaginated here rather than trusted.

A wall-time assertion on a shared machine is not a gate, so nothing here times anything.
The instrument is a recording wrapper around the session: every statement the read side
compiles is captured with its SQL text and the number of rows it actually pulled back.
The assertions are "no wide read fetched more than ``limit`` rows", "the total came from
a COUNT", and "a read that discards the total issues no COUNT at all".

Postgres is required and provisioned as a scratch database, so the SQL that is compiled
is the SQL that runs in the deployment. There is no live-clock assertion in this file, and
no mocked repository: the rows are real rows in the pipeline's own tables.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
# Served as `uvicorn main:app --app-dir apps/api`, so the modules are `api.*` with `apps`
# on the path — the same bootstrap tests/integration/test_p7_api.py uses.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api.readmodel import Base, PostgresSource, ReadModel  # noqa: E402
from api.settings import reset_settings_cache  # noqa: E402

from oxbow.adapters.warehouse.postgres import new_run_id  # noqa: E402

TEST_DB_NAME = f"oxbow_read_shape_{os.getpid()}_{secrets.token_hex(3)}"

CURRENCY = "UGX"
DAY = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
N_ACCOUNTS = 240
# Every priced account is in the live map; the first SELECTED_COUNT of them are inside
# the capacity budget, so `side_of_cutoff` has two non-empty sides to compare.
SELECTED_COUNT = 100
PAGE_SIZE = 5
BANDS = ("D", "E", "C", "B", "A")
# A column only the queue's wide projection selects, so the test can tell the page read
# apart from a key-only read that decides a rank window.
WIDE_MARKER = "score.reason_codes"


# --- the recording instrument --------------------------------------------------


@dataclass(frozen=True)
class _Statement:
    """One executed statement: its SQL, its flavour, and the rows it pulled."""

    sql: str
    kind: str
    rows: int


class _Ledger:
    """What the read side actually asked the database for."""

    def __init__(self) -> None:
        self.entries: list[_Statement] = []

    def add(self, sql: str, *, scalar: bool, rows: int) -> None:
        is_count = bool(re.search(r"count\s*\(", sql.lower()))
        kind = "count" if (scalar and is_count) else ("scalar" if scalar else "rows")
        self.entries.append(_Statement(sql=sql, kind=kind, rows=rows))

    @property
    def counts(self) -> list[_Statement]:
        return [entry for entry in self.entries if entry.kind == "count"]

    @property
    def row_reads(self) -> list[_Statement]:
        return [entry for entry in self.entries if entry.kind == "rows"]

    @property
    def wide_reads(self) -> list[_Statement]:
        """Reads that pulled the queue's wide projection — the one that costs real money."""
        return [entry for entry in self.row_reads if WIDE_MARKER in entry.sql]

    @property
    def narrow_reads(self) -> list[_Statement]:
        """Reads that pulled keys only: the window decision, not the page body."""
        return [entry for entry in self.row_reads if WIDE_MARKER not in entry.sql]

    def reset(self) -> None:
        self.entries.clear()

    def describe(self) -> str:
        return "; ".join(f"{entry.kind}[{entry.rows} rows]" for entry in self.entries) or "nothing"


class _RecordingResult:
    def __init__(self, inner: Any, ledger: _Ledger, sql: str) -> None:
        self._inner = inner
        self._ledger = ledger
        self._sql = sql

    def mappings(self) -> Any:
        rows = list(self._inner.mappings())
        self._ledger.add(self._sql, scalar=False, rows=len(rows))
        return iter(rows)

    def scalar_one(self) -> Any:
        value = self._inner.scalar_one()
        self._ledger.add(self._sql, scalar=True, rows=0)
        return value

    def scalar(self) -> Any:
        value = self._inner.scalar()
        self._ledger.add(self._sql, scalar=True, rows=0)
        return value

    def all(self) -> Any:
        return self._inner.all()

    def first(self) -> Any:
        return self._inner.first()


class _RecordingSession:
    """Wraps a real session: records the statements, changes nothing about them."""

    def __init__(self, inner: Session, ledger: _Ledger) -> None:
        self._inner = inner
        self._ledger = ledger

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _RecordingResult:
        sql = str(statement)
        return _RecordingResult(self._inner.execute(statement, *args, **kwargs), self._ledger, sql)

    def close(self) -> None:
        self._inner.close()


# --- the scratch warehouse -----------------------------------------------------


def _candidate_admin_urls() -> list[str]:
    urls: list[str] = []
    override = os.environ.get("OXBOW_P7_PG_ADMIN_URL", "").strip()
    if override:
        urls.append(override)
    user = os.environ.get("POSTGRES_USER", "oxbow")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    port = os.environ.get("POSTGRES_PORT", "5433")
    dbname = os.environ.get("POSTGRES_DB", "oxbow")
    if password:
        urls.append(f"postgresql://{user}:{password}@127.0.0.1:{port}/{dbname}")
    urls.append(f"postgresql://{user}@127.0.0.1:{port}/{dbname}")
    urls.append("postgresql://postgres@127.0.0.1:5432/postgres")
    return urls


def _redact(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.password:
        return url
    host = parsed.netloc.split("@")[-1]
    return urlunparse(parsed._replace(netloc=f"{parsed.username}:***@{host}"))


@pytest.fixture(scope="module")
def warehouse() -> Any:
    """A scratch Postgres holding the pipeline's own metadata and a seeded queue.

    ``create_all`` rather than alembic: this file only reads. Migration 0002's triggers
    govern writes into a completed run, and no test here writes through that path.
    """
    import psycopg

    tried: list[str] = []
    last_error = "no candidate URL"
    admin_url = ""
    for candidate in _candidate_admin_urls():
        tried.append(_redact(candidate))
        try:
            with psycopg.connect(candidate, autocommit=True, connect_timeout=3) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
                conn.execute(f"CREATE DATABASE {TEST_DB_NAME}")
            admin_url = candidate
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:140]}"
    else:
        pytest.fail(
            "these read-shape tests compile real SQL against a real Postgres and none was "
            f"reachable; tried {tried}, last failure {last_error}. Start compose (`make up`) "
            "or export OXBOW_P7_PG_ADMIN_URL."
        )
    parsed = urlparse(admin_url)
    url = urlunparse(parsed._replace(path=f"/{TEST_DB_NAME}")).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    seeded = _seed(engine)
    yield {"url": url, "engine": engine, **seeded}
    engine.dispose()
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")


@pytest.fixture()
def ledger_and_source(warehouse: dict[str, Any]) -> Any:
    ledger = _Ledger()
    source = PostgresSource(lambda: _RecordingSession(Session(warehouse["engine"]), ledger))
    return ledger, source


def _keys(count: int) -> list[str]:
    """12-character pseudonymous-shaped keys, distinct and stable across runs."""
    return [
        hashlib.sha256(f"read-shape-{index}".encode()).hexdigest()[:12].upper()
        for index in range(count)
    ]


def _exposure(key: str) -> int:
    """Deterministic exposure with deliberate ties, so a sort has to have a tie-break."""
    return 1_000_000 * (1 + int(key[:3], 16) % 40)


def _run_row(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "state": "complete",
        "seed": 1337,
        "timezone": "UTC",
        "provenance": "pipeline",
        "config_hash": "0" * 64,
        "model_version": "read-shape-1",
        "dataset_ref": "paysim-fake",
        "created_at": DAY,
        "finished_at": DAY + timedelta(minutes=5),
    }


def _seed(engine: Any) -> dict[str, Any]:
    """Two complete runs of ``N_ACCOUNTS`` accounts.

    Run ``full`` has an ``economics`` row for every scored account, so the live rank map
    covers the whole join. Run ``gapped`` withholds economics for three of them: those
    accounts have no rank, and they are what stops a "slice the rank map and call it a
    page" implementation from losing rows off the end of the queue and under-reporting
    ``total``.
    """
    tables = Base.metadata.tables
    keys = _keys(N_ACCOUNTS)
    # The map's rank order is exposure-descending, which deliberately disagrees with
    # account_key order so an ordering bug cannot hide as a coincidence.
    ordered = sorted(keys, key=lambda key: (-_exposure(key), key))
    unpriced = set(keys[-3:])
    runs = {"full": new_run_id(), "gapped": new_run_id()}
    with Session(engine) as session:
        session.execute(insert(tables["run"]), [_run_row(rid) for rid in runs.values()])
        for name, run_id in runs.items():
            priced = ordered if name == "full" else [key for key in ordered if key not in unpriced]
            session.execute(
                insert(tables["account"]),
                [
                    {
                        "run_id": run_id,
                        "account_key": key,
                        "source_dataset": "paysim-fake",
                        "first_seen_at": DAY + timedelta(days=index % 7),
                        "last_seen_at": DAY + timedelta(days=30),
                        "txn_count": 10 + index % 5,
                        "n_outbound": 4,
                        "n_inbound": 6,
                        "n_counterparties": 3,
                        "funding_minor": 500_000_00,
                        "currency": CURRENCY,
                        "age_days": 90,
                    }
                    for index, key in enumerate(keys)
                ],
            )
            session.execute(
                insert(tables["score"]),
                [
                    {
                        "run_id": run_id,
                        "account_key": key,
                        "fused_score": 0.5 + (index % 40) / 100,
                        "band": BANDS[index % 5],
                        "scorecard_points": 40 + index % 11,
                        "calibrated_probability": 0.61,
                        "calibration_band": "0.6-0.7",
                        "observed_rate": 0.63,
                        "calibration_n": 400 + index,
                        "predicted_typology": "crowdfunding" if index % 2 else "rapid",
                        "reason_codes": [{"code": "RC-RAPID-ACCUM", "label": "rapid"}],
                        "rule_ids": ["R4"] if index % 3 == 0 else ["R7"],
                        "model_version": "read-shape-1",
                    }
                    for index, key in enumerate(keys)
                ],
            )
            session.execute(
                insert(tables["economics"]),
                [
                    {
                        "run_id": run_id,
                        "account_key": key,
                        "currency": CURRENCY,
                        "exposure_minor": _exposure(key),
                        "expected_value_minor": 10_000_00 + index * 1000,
                        "loss_avoided_minor": 3_000_00,
                        "analyst_cost_minor": 150_00,
                        "friction_cost_minor": 250_00,
                        "analyst_minutes": 30.0,
                        "recovery_rate": 0.35,
                        "ev_density": 0.11,
                        "mc_runs": 1000,
                        "mc_seed": 1337,
                        "mc_p05_minor": 9_000_00,
                        "mc_p50_minor": 12_000_00,
                        "mc_p95_minor": 18_000_00,
                        "mc_interval": [9_000_00, 18_000_00],
                        "assumptions": {"recovery_rate": 0.35},
                    }
                    for index, key in enumerate(priced)
                ],
            )
        session.commit()
    live_map = {
        key: {
            "rank": index + 1,
            "selected": index < SELECTED_COUNT,
            "beyond_capacity": index >= SELECTED_COUNT,
        }
        for index, key in enumerate(ordered)
    }
    return {
        "run_full": runs["full"],
        "run_gapped": runs["gapped"],
        "keys": keys,
        "band_by_key": {key: BANDS[index % 5] for index, key in enumerate(keys)},
        "ordered_keys": ordered,
        "unpriced": sorted(unpriced),
        "live_map": live_map,
    }


def _map_for(run_id: str, warehouse: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The live allocation for one run: the gapped run has no entry for unpriced keys."""
    if run_id == warehouse["run_full"]:
        return warehouse["live_map"]
    return {
        key: value
        for key, value in warehouse["live_map"].items()
        if key not in set(warehouse["unpriced"])
    }


def _live(source: PostgresSource, warehouse: dict[str, Any], run_id: str, **kwargs: Any):
    """``alert_rows`` on the live-rank branch: a mapping, so no stored allocation join."""
    allocations = kwargs.pop("allocations", _map_for(run_id, warehouse))
    kwargs.setdefault("limit", PAGE_SIZE)
    kwargs.setdefault("offset", 0)
    return source.alert_rows(run_id=run_id, allocations=allocations, **kwargs)


# --- finding 1: the live-rank queue must page in the statement -----------------


def test_live_rank_page_reads_no_more_rows_than_it_serves(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """The finding: page 1 and page 40 each read the whole run and sliced in Python."""
    ledger, source = ledger_and_source
    for offset in (0, PAGE_SIZE, 120):
        ledger.reset()
        rows, total = _live(
            source, warehouse, warehouse["run_full"], sort="rank", order="asc", offset=offset
        )
        assert len(rows) == PAGE_SIZE, f"offset {offset} served {len(rows)} rows"
        assert total == N_ACCOUNTS, f"total was {total}, the join holds {N_ACCOUNTS} rows"
        oversized = [entry for entry in ledger.wide_reads if entry.rows > PAGE_SIZE]
        assert not oversized, (
            f"offset {offset}: the queue fetched {oversized[0].rows} wide rows to serve "
            f"{PAGE_SIZE}, so pagination is not in the statement "
            f"({ledger.describe()})"
        )
        assert ledger.counts, (
            f"offset {offset}: no COUNT ran, so `total` is the length of whatever this call "
            f"fetched, not the size of the queue ({ledger.describe()})"
        )


def test_live_rank_last_page_reads_one_page(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """A deep page still reads one page of wide rows, and the ranks land on the right keys."""
    ledger, source = ledger_and_source
    ledger.reset()
    rows, total = _live(
        source,
        warehouse,
        warehouse["run_full"],
        sort="rank",
        order="asc",
        offset=N_ACCOUNTS - PAGE_SIZE,
    )
    assert [row["account_key"] for row in rows] == warehouse["ordered_keys"][-PAGE_SIZE:]
    assert [int(row["rank"]) for row in rows] == list(
        range(N_ACCOUNTS - PAGE_SIZE + 1, N_ACCOUNTS + 1)
    )
    assert total == N_ACCOUNTS
    assert (
        max((entry.rows for entry in ledger.wide_reads), default=0) <= PAGE_SIZE
    ), f"the last page read {ledger.describe()}"


@pytest.mark.parametrize(
    ("sort", "order"),
    [
        ("rank", "asc"),
        ("rank", "desc"),
        ("exposure_minor", "asc"),
        ("exposure_minor", "desc"),
        ("band", "desc"),
        ("fused_score", "desc"),
    ],
)
def test_paged_assembly_equals_one_unpaginated_read(
    warehouse: dict[str, Any], ledger_and_source: Any, sort: str, order: str
) -> None:
    """Order must not move when the page boundary moves into SQL.

    ``exposure_minor`` and ``band`` carry deliberate ties, and the gapped run has NULL sort
    values, so this compares a total order rather than a lucky one.
    """
    _, source = ledger_and_source
    for run_id in (warehouse["run_full"], warehouse["run_gapped"]):
        whole, whole_total = _live(
            source, warehouse, run_id, sort=sort, order=order, limit=N_ACCOUNTS * 10
        )
        assert whole_total == len(whole), "the single big read disagrees with itself"
        assembled: list[str] = []
        step, offset = 7, 0
        while offset < whole_total:
            page, total = _live(
                source, warehouse, run_id, sort=sort, order=order, limit=step, offset=offset
            )
            assert (
                total == whole_total
            ), f"total moved between pages: {total} at offset {offset} vs {whole_total}"
            assembled.extend(str(row["account_key"]) for row in page)
            offset += step
        assert assembled == [
            str(row["account_key"]) for row in whole
        ], f"{sort}/{order} paged in {step}s does not assemble to the whole read for {run_id}"
        assert len(set(assembled)) == len(assembled), f"{sort}/{order} served a row twice"


def test_rank_order_keeps_unranked_accounts_last_either_way(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """Three scored accounts have no economics row, so no rank. They must not vanish.

    They are also why the page boundary cannot come from the rank map alone: the map covers
    237 of the 240 joined rows, and a window taken straight out of it would drop the tail
    and under-report ``total``.
    """
    ledger, source = ledger_and_source
    for order in ("asc", "desc"):
        ledger.reset()
        rows, total = _live(
            source,
            warehouse,
            warehouse["run_gapped"],
            sort="rank",
            order=order,
            limit=N_ACCOUNTS * 10,
        )
        priced = _map_for(warehouse["run_gapped"], warehouse)
        assert total == N_ACCOUNTS, f"{order}: total {total}, the join holds {N_ACCOUNTS}"
        assert len(rows) == N_ACCOUNTS
        tail = [str(row["account_key"]) for row in rows][-3:]
        assert (
            sorted(tail) == warehouse["unpriced"]
        ), f"{order}: unpriced accounts landed at {tail}, not last"
        assert all(row["rank"] is None for row in rows[-3:])
        present = sorted(int(value["rank"]) for value in priced.values())
        assert [int(row["rank"]) for row in rows[:-3]] == (
            present if order == "asc" else list(reversed(present))
        ), f"{order}: the served ranks are not monotone in the requested direction"


def test_unpriced_accounts_are_paged_like_any_other_row(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """The run whose map does not cover the whole join still reads one page of wide rows.

    This is the case a "slice the rank map for the page keys" shortcut gets wrong: the map
    has 237 entries, the join has 240 rows, and the three extra rows are part of the queue.
    """
    ledger, source = ledger_and_source
    for offset in (0, 15):
        ledger.reset()
        rows, total = _live(
            source, warehouse, warehouse["run_gapped"], sort="rank", order="asc", offset=offset
        )
        assert len(rows) == PAGE_SIZE
        assert total == N_ACCOUNTS
        assert (
            max((entry.rows for entry in ledger.wide_reads), default=0) <= PAGE_SIZE
        ), f"offset {offset} on the gapped run read {ledger.describe()}"
        assert ledger.narrow_reads, (
            f"offset {offset}: nothing in the database decided this window, so total cannot "
            f"be the queue size ({ledger.describe()})"
        )


def test_side_of_cutoff_page_reads_one_page_of_wide_rows(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """The cutoff filter must not cost a full read either, on either sort."""
    ledger, source = ledger_and_source
    for sort in ("rank", "exposure_minor"):
        ledger.reset()
        rows, total = _live(
            source,
            warehouse,
            warehouse["run_full"],
            side_of_cutoff="above",
            sort=sort,
            limit=PAGE_SIZE,
        )
        assert len(rows) == PAGE_SIZE and total == SELECTED_COUNT
        assert (
            max((entry.rows for entry in ledger.wide_reads), default=0) <= PAGE_SIZE
        ), f"sort={sort} above the cutoff read {ledger.describe()}"


def test_side_of_cutoff_pages_without_fetching_the_other_side(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """``side_of_cutoff`` used to be a Python filter after the fetch, then ``len(rows)``."""
    _, source = ledger_and_source
    for sort in ("rank", "exposure_minor"):
        above, above_total = _live(
            source,
            warehouse,
            warehouse["run_full"],
            side_of_cutoff="above",
            sort=sort,
            limit=N_ACCOUNTS * 10,
        )
        assert above_total == SELECTED_COUNT, f"{sort}/above total {above_total}"
        assert len(above) == SELECTED_COUNT
        assert all(row["selected"] for row in above), f"{sort}/above served an unselected row"
        below, below_total = _live(
            source,
            warehouse,
            warehouse["run_full"],
            side_of_cutoff="below",
            sort=sort,
            limit=N_ACCOUNTS * 10,
        )
        assert below_total == N_ACCOUNTS - SELECTED_COUNT, f"{sort}/below total {below_total}"
        assert all(not row["selected"] for row in below), f"{sort}/below served a selected row"
        page, page_total = _live(
            source,
            warehouse,
            warehouse["run_full"],
            side_of_cutoff="below",
            sort=sort,
            limit=PAGE_SIZE,
            offset=PAGE_SIZE,
        )
        assert page_total == below_total
        assert len(page) == PAGE_SIZE
        assert [row["account_key"] for row in page] == [
            row["account_key"] for row in below[PAGE_SIZE : 2 * PAGE_SIZE]
        ]


def test_filters_narrow_the_page_and_the_total_together(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """A filter must move the page boundary and the total as one, on both sorts."""
    _, source = ledger_and_source
    band_rows, band_total = _live(
        source, warehouse, warehouse["run_full"], bands=["D"], sort="rank", limit=PAGE_SIZE
    )
    assert {row["band"] for row in band_rows} == {"D"}
    expected_band = sum(
        1 for key in warehouse["ordered_keys"] if warehouse["band_by_key"][key] == "D"
    )
    assert band_total == expected_band, f"band total {band_total}, expected {expected_band}"
    assert len(band_rows) == PAGE_SIZE
    exposure_rows, exposure_total = _live(
        source,
        warehouse,
        warehouse["run_full"],
        min_exposure_minor=1_020_000,
        sort="exposure_minor",
        order="desc",
        limit=PAGE_SIZE,
        offset=PAGE_SIZE,
    )
    assert exposure_total < N_ACCOUNTS
    assert len(exposure_rows) == PAGE_SIZE
    assert all(int(row["exposure_minor"]) >= 1_020_000 for row in exposure_rows)


def test_deep_page_past_the_end_is_empty_and_still_reports_the_total(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """``rows[offset:offset+limit]`` on an over-run offset is []; SQL must agree."""
    ledger, source = ledger_and_source
    ledger.reset()
    rows, total = _live(
        source, warehouse, warehouse["run_full"], sort="rank", limit=PAGE_SIZE, offset=N_ACCOUNTS
    )
    assert rows == []
    assert total == N_ACCOUNTS, f"past-the-end total was {total}"
    assert not ledger.wide_reads, f"a past-the-end page still read rows: {ledger.describe()}"


# --- finding 4: the count is a choice the caller makes -------------------------


def test_a_read_that_discards_the_total_issues_no_count(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """``select`` ran a COUNT on every call, including the ones that throw the total away."""
    ledger, source = ledger_and_source
    read_model = ReadModel(source)
    ledger.reset()
    row = read_model.run_row(warehouse["run_full"])
    assert row["run_id"] == warehouse["run_full"]
    assert not ledger.counts, f"run_row paid for a COUNT it threw away ({ledger.describe()})"

    ledger.reset()
    events = read_model.stage_events(warehouse["run_full"])
    assert isinstance(events, list)
    assert not ledger.counts, f"the stage-ledger read paid for a COUNT ({ledger.describe()})"

    ledger.reset()
    scored = read_model.rows("score", {"run_id": warehouse["run_full"]})
    assert len(scored) == N_ACCOUNTS
    assert not ledger.counts, f"rows() paid for a COUNT ({ledger.describe()})"


def test_count_needs_one_statement_not_two(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """``count()`` fetched zero rows *and* counted them: two round trips for one number."""
    ledger, source = ledger_and_source
    read_model = ReadModel(source)
    ledger.reset()
    total = read_model.count("score", {"run_id": warehouse["run_full"]})
    assert total == N_ACCOUNTS
    assert len(ledger.entries) == 1, f"a count took {ledger.describe()}"
    assert ledger.counts, f"the single statement was not a COUNT: {ledger.describe()}"


def test_run_summary_costs_four_counts_and_no_empty_row_reads(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """Four aggregates per run row, one statement each. Not eight, and not twelve."""
    ledger, source = ledger_and_source
    read_model = ReadModel(source)
    row = read_model.run_row(warehouse["run_full"])
    ledger.reset()
    summary = read_model.run_summary(row)
    assert summary["account_count"] == N_ACCOUNTS
    assert summary["scored_count"] == N_ACCOUNTS
    assert summary["quarantine_count"] == 0
    assert len(ledger.counts) == 4, f"expected four aggregates, saw {ledger.describe()}"
    assert (
        not ledger.row_reads
    ), f"run_summary fetched rows in order to count them ({ledger.describe()})"


def test_list_runs_statement_budget_is_the_page_plus_four_per_row(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """``GET /api/runs?limit=2`` costs one page read plus four aggregates per served row."""
    ledger, source = ledger_and_source
    read_model = ReadModel(source)
    ledger.reset()
    rows, total = read_model.list_runs(
        state="complete", limit=2, offset=0, sort="run_id", order="desc"
    )
    assert len(rows) == 2
    assert total == 2, f"the runs page total came back {total} ({ledger.describe()})"
    assert len(ledger.counts) == 9, f"statement mix was {ledger.describe()}"
    assert len(ledger.row_reads) == 1, (
        f"list_runs issued {len(ledger.row_reads)} row reads for a 2-row page: "
        f"{ledger.describe()}"
    )


def test_paging_a_table_still_reports_the_real_total(
    warehouse: dict[str, Any], ledger_and_source: Any
) -> None:
    """The opt-out must not become an opt-out from correctness.

    ``paged`` is what every list endpoint uses for ``meta.total``; a None there is a 500 in
    the router, so the counted path is pinned as well as the uncounted one.
    """
    ledger, source = ledger_and_source
    read_model = ReadModel(source)
    ledger.reset()
    rows, total = read_model.paged(
        "score", {"run_id": warehouse["run_full"]}, order="account_key", limit=5, offset=0
    )
    assert len(rows) == 5
    assert total == N_ACCOUNTS
    assert ledger.counts, f"no COUNT ran, so total is not the queue size: {ledger.describe()}"

    ledger.reset()
    uncounted, absent = source.select(
        "score", where={"run_id": warehouse["run_full"]}, limit=5, with_count=False
    )
    assert len(uncounted) == 5
    assert (
        absent is None
    ), "a read that asked not to count still reported a total; the opt-out is not real"
    assert not ledger.counts, f"the opt-out still issued a COUNT: {ledger.describe()}"


# --- the same queue, served -----------------------------------------------------


def _configure_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    """Test-local secrets injected through the environment.

    The repository's own ``RUN_SALT`` is never read and no value here is echoed back.
    """
    base = {
        "RUN_SALT": "read-shape-test-salt-not-the-real-one",
        "OXBOW_SEED": "1337",
        "OXBOW_LOCAL_JWT_SECRET": "read-shape-test-local-jwt-secret",
        "OXBOW_LOCAL_JWT_ENABLED": "true",
        "WEBHOOK_SIGNING_SECRET": "read-shape-test-webhook-secret",
        "WEBHOOK_ENDPOINT": "http://127.0.0.1:1/webhook",
        "OXBOW_OIDC_ISSUER": "",
        "OXBOW_OIDC_JWKS_URL": "",
        "OXBOW_S3_ENDPOINT_URL": "",
        "OXBOW_SLACK_WEBHOOK_URL": "",
        "OXBOW_REPO_ROOT": str(REPO_ROOT),
        "OXBOW_LOG_FORMAT": "console",
        "DATABASE_URL": "",
        "OXBOW_WAREHOUSE": "null",
    }
    base.update(overrides)
    for name, value in base.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    reset_settings_cache()


@pytest.fixture(scope="module")
def queue_client(warehouse: dict[str, Any]) -> Any:
    """The real app over the scratch warehouse, so the served body is the artifact."""
    from api.deps import build_container
    from api.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch = pytest.MonkeyPatch()
    _configure_env(monkeypatch, DATABASE_URL=warehouse["url"], OXBOW_WAREHOUSE="postgres")
    container = build_container()
    assert (
        container.backend == "postgres"
    ), f"the fixture asked for OXBOW_WAREHOUSE=postgres and got {container.backend}"
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.container = container
        response = client.post(
            "/api/auth/demo-token",
            json={"subject": "read-shape-analyst", "roles": ["analyst"]},
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = f"Bearer {response.json()['data']['access_token']}"
        yield client
    container.close()
    monkeypatch.undo()
    reset_settings_cache()


def _walk_money(body: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    """Every `{minor, currency, decimals}` object in a served body, with where it lives."""
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(body, dict):
        if {"minor", "currency", "decimals"} <= set(body) and isinstance(body["minor"], int):
            found.append((path, body))
        for key, value in body.items():
            found.extend(_walk_money(value, f"{path}.{key}"))
    elif isinstance(body, list):
        for index, value in enumerate(body):
            found.extend(_walk_money(value, f"{path}[{index}]"))
    return found


def test_the_served_queue_pages_with_a_total_and_no_recomputed_money(
    queue_client: Any, warehouse: dict[str, Any]
) -> None:
    """`GET /api/alerts` is the finding's route: it must still answer, and still say how
    many rows the queue holds, with the ranks labelled as a live re-allocation."""
    response = queue_client.get("/api/alerts", params={"run_id": warehouse["run_full"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"data", "meta"}, sorted(body)
    meta = body["meta"]
    assert meta["total"] == N_ACCOUNTS, f"meta.total was {meta['total']}"
    assert meta["limit"] == 50 and meta["offset"] == 0, meta
    data = body["data"]
    assert len(data["rows"]) == 50
    assert data["allocation_source"] == "reallocated", data["allocation_source"]
    first_page = [row["account_key"] for row in data["rows"]]
    second = queue_client.get(
        "/api/alerts", params={"run_id": warehouse["run_full"], "offset": 50}
    ).json()
    assert second["meta"]["total"] == N_ACCOUNTS
    assert set(first_page).isdisjoint(
        {row["account_key"] for row in second["data"]["rows"]}
    ), "two pages of the same queue share a row: the page boundary moved"
    ranks = [int(row["rank"]) for row in data["rows"]]
    assert ranks == sorted(ranks), f"sort=rank served {ranks}"


def test_served_money_carries_the_decimal_exponent_not_the_config_base(
    queue_client: Any, warehouse: dict[str, Any]
) -> None:
    """`Money.decimals` is an EXPONENT, and this route was putting the BASE in it.

    `config/economics.yaml` declares `minor_units_per_major: 100`. Both renderers —
    `schemas/common.py` (`minor / 10**decimals`) and `apps/web/src/lib/format/money.ts`
    (`10 ** decimals`) — raise ten to it, so a served `decimals: 100` divides every figure
    by 10^100 and the queue renders as zero. Asserted on the composed response, not on the
    helper that converts, because a test that called the helper stayed green while the
    container's own field was being ignored.
    """
    import yaml

    response = queue_client.get("/api/alerts", params={"run_id": warehouse["run_full"]})
    assert response.status_code == 200, response.text
    declared = yaml.safe_load(
        (REPO_ROOT / "config" / "economics.yaml").read_text(encoding="utf-8")
    )["minor_units_per_major"]
    assert declared == 100, f"the card declares {declared}; this test's premise is stale"
    money_objects = _walk_money(response.json()["data"])
    assert money_objects, "the queue served no money object at all"
    for path, figure in money_objects:
        decimals = figure["decimals"]
        assert 10**decimals == declared, (
            f"{path} serves decimals={decimals} for a base of {declared}: a client computing "
            "10**decimals divides the amount by the wrong factor"
        )
        assert figure["currency"] == CURRENCY, figure
