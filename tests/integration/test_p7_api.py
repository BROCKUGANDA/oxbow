"""P7 — the API/service layer, executed rather than described.

Before this file, nothing in the repository had ever imported `apps/api` through a
request. This boots the real app with `fastapi.testclient.TestClient`, sends real requests,
and pins the six P7 contracts. Several assertions here failed when the file was first run
against the code; the fixes are in `apps/api/**` and each is guarded by the test that found
it, so a regression points at the reason.

Two environments, because the layer has two honest halves:

* ``null_client`` — the app on the ``null-file`` warehouse with no database, no Redis, no
  Keycloak and no MinIO running. That is plan §14's "degraded, not broken" path, and it is
  enough to prove boot, the route table, the OpenAPI document, the error contract and the
  authentication gates.
* ``wh_client`` — the same app against a real Postgres created by this module, migrated by
  `apps/api/alembic` (not `create_all`, so migration 0002's integrity triggers exist) and
  seeded through `oxbow.adapters.warehouse.postgres.PostgresWarehouseSink`, the pipeline's
  own handoff. The outbox transaction, the audit chain, per-case ordering and the 409 race
  are properties of a database with real snapshots and real unique constraints; they are
  not provable against a substitute, so they are not attempted against one.

If no reachable Postgres is found the write-path tests fail naming the URL they tried,
rather than skipping: a silently-skipped suite is how an unverified layer reads as a
verified one. Secrets are explicit test values injected through the environment — the
repository's own ``RUN_SALT`` is never read, and no secret value is printed.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from urllib.parse import urlparse, urlunparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, text
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]

# Served as `uvicorn main:app --app-dir apps/api`, so the packages are `api.*` with `apps`
# on the path. Same bootstrap `tests/integration/test_envelope_doctrine.py` uses.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api import decisions as api_decisions  # noqa: E402
from api import echo as echo_module  # noqa: E402
from api.deps import PROBED_COMPONENTS, build_container  # noqa: E402
from api.main import create_app  # noqa: E402
from api.outbox import claim_due  # noqa: E402
from api.problems import PROBLEM_MEDIA_TYPE, PROBLEM_TYPE_BASE  # noqa: E402
from api.readmodel import FileWarehouseSource, ReadModel  # noqa: E402
from api.schemas.catalog import RunDetail, RunSummary  # noqa: E402
from api.schemas.common import ENVELOPE_KEYS  # noqa: E402
from api.schemas.health import ComponentName  # noqa: E402
from api.security import Principal, b64url_encode, decode_token, mint_local_token  # noqa: E402
from api.settings import reset_settings_cache  # noqa: E402

from oxbow.adapters.signing import (  # noqa: E402
    REPLAY_WINDOW_SECONDS,
    SIGNATURE_HEADER,
    compute_signature,
    sign_body,
    verify_signature,
)
from oxbow.adapters.warehouse.models import (  # noqa: E402
    AuditEvent,
    Case,
    Decision,
    OutboxMessage,
    Policy,
    PolicyAllocation,
    PolicySummary,
)
from oxbow.adapters.warehouse.postgres import PostgresWarehouseSink, new_run_id  # noqa: E402
from oxbow.audit.chain import PendingChainRow, verify_chain  # noqa: E402
from oxbow.ingest.canonical import RunIdentity, account_key, display_account_key  # noqa: E402
from oxbow.ports.case_sink import build_idempotency_key  # noqa: E402
from oxbow.ports.warehouse import RunState  # noqa: E402

# Reuse the doctrine predicates rather than inventing a second, looser one.
from tests.integration.test_envelope_doctrine import ENVELOPE_LEVEL_FIELDS, _offenders  # noqa: E402

# --- test-only secrets ---------------------------------------------------------
# Explicit and synthetic, injected through the environment so `api.settings` reads them the
# way a deployment reads real ones. These setenv calls override anything exported by the
# shell, so the repository's own RUN_SALT cannot be picked up even accidentally.
TEST_RUN_SALT = "p7-integration-test-salt-not-the-real-one"
TEST_JWT_SECRET = "p7-integration-test-local-jwt-secret"
TEST_WEBHOOK_SECRET = "p7-integration-test-webhook-secret"

TEST_DB_NAME_PREFIX = "oxbow_p7_test"
# One scratch database per process. The name used to be a constant, which made two
# concurrent runs of this module destroy each other: `_provision_database` opens with
# `DROP DATABASE ... WITH (FORCE)`, so run B's setup terminated run A's live backends and
# A's remaining tests failed with `psycopg.OperationalError: server closed the connection
# unexpectedly` — the same wording the dead-VM proxy produced, and far harder to spot
# because the engine was healthy the whole time. A per-process name cannot collide; the
# reaper below keeps abandoned ones from accumulating.
TEST_DB_NAME = f"{TEST_DB_NAME_PREFIX}_{os.getpid()}_{secrets.token_hex(3)}"
RFC9457_REQUIRED = ("type", "title", "status", "detail", "instance")

ACCOUNT_KEY_SHAPE = re.compile(r"^(?:ACC-)?[0-9A-Fa-f]{12}$")
RAW_ACCOUNT_ID_SHAPE = re.compile(r"^C\d{8,}$")  # PaySim `oldbalanceOrg`-style identifiers

ANALYST = "p7-operator-analyst"
REVIEWER = "p7-operator-reviewer"
ISSUER = "https://identity.example/realms/oxbow"
POLICY_ID = "p7-capacity-3000"

CURRENCY = "UGX"
DAY = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
# config/economics.yaml fixes four_eyes.threshold_exposure_minor at 50,000,000 minor units,
# so "low" is genuinely under that gate and "high" genuinely over it. Get either one on the
# wrong side and every four-eyes assertion below silently tests the other branch.
LOW_EXPOSURE_MINOR = 10_000_000  # under the threshold: the outbox row is written at once
HIGH_EXPOSURE_MINOR = 90_000_000  # over it: nothing is promised until a second reviewer signs


def _configure_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    base = {
        "RUN_SALT": TEST_RUN_SALT,
        "OXBOW_SEED": "1337",
        "OXBOW_LOCAL_JWT_SECRET": TEST_JWT_SECRET,
        "OXBOW_LOCAL_JWT_ENABLED": "true",
        "WEBHOOK_SIGNING_SECRET": TEST_WEBHOOK_SECRET,
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


# --- the Postgres the write path needs -----------------------------------------


def _candidate_admin_urls() -> list[str]:
    """Where to look for a scratch Postgres, most-intended first.

    `docker-compose.yml` puts Postgres on host port 5433 with the compose credentials.
    An explicit `OXBOW_P7_PG_ADMIN_URL` wins so an operator can point the suite at a server
    they control without editing this file. Passwords are only ever taken from the caller's
    own environment, never from the repository, and never echoed back.
    """
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
    netloc = f"{parsed.username}:***@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _provision_database() -> str:
    """Create the scratch database and return its URL.

    A dedicated database, not a schema inside the deployment's own: this module TRUNCATEs
    and DROPs, and it must never be able to do that to a warehouse someone is investigating.

    There is deliberately no reaper for databases a crashed run left behind. One was tried
    here — dropping any `oxbow_p7_test_*` with no active backend — and it was worse than the
    leak: between this function's `CREATE DATABASE` and `_migrate`'s first connection the new
    database has *no* backends at all, so a second run in flight judged it abandoned and
    dropped it, and the victim failed with `FATAL: database ... does not exist`. A scratch
    database nobody deletes is a few kilobytes in a container volume that `make destroy`
    clears; a scratch database deleted under somebody is a false red.
    """
    import psycopg

    tried: list[str] = []
    last_error = "no candidate URL"
    for admin_url in _candidate_admin_urls():
        tried.append(_redact(admin_url))
        try:
            with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
                conn.execute(f"CREATE DATABASE {TEST_DB_NAME}")
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:140]}"
            continue
        parsed = urlparse(admin_url)
        # `postgresql+psycopg`, not the bare `postgresql://`: SQLAlchemy maps the bare
        # scheme to the psycopg2 dialect, which is not installed (02 F pins psycopg3), and
        # this URL is used by `create_engine` directly as well as through
        # `Settings.sqlalchemy_url`. Same normalisation the app applies.
        return (
            urlunparse(parsed._replace(path=f"/{TEST_DB_NAME}"))
            .replace("postgresql://", "postgresql+psycopg://", 1)
            .replace("postgres://", "postgresql+psycopg://", 1)
        )
    pytest.fail(
        "The P7 write-path tests need a real Postgres and none was reachable. Tried "
        f"{tried}; last failure {last_error}. Start it with `make up` (compose Postgres on "
        "POSTGRES_PORT, default 5433), or export OXBOW_P7_PG_ADMIN_URL at a scratch server. "
        "The outbox transaction, the audit-chain race and per-case ordering are properties "
        "of a database with real snapshots and real unique constraints; they are not tested "
        "against a fake one."
    )
    raise AssertionError("unreachable")


def _migrate(database_url: str) -> None:
    """Apply `apps/api/alembic` to head so the integrity triggers exist.

    The Config is built in memory rather than read from `apps/api/alembic.ini`, because
    `alembic/env.py` calls `fileConfig()` whenever a config file is named, which disables
    every pre-existing logger for the rest of the pytest session. The migrations that run
    are the same ones `make db-migrate` runs; only the logging side effect is skipped.
    """
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        config = Config()
        config.set_main_option("script_location", str(REPO_ROOT / "apps" / "api" / "alembic"))
        config.set_main_option("version_path_separator", "os")
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _drop_database(database_url: str) -> None:
    import psycopg

    parsed = urlparse(database_url)
    for url in (urlunparse(parsed._replace(path="/postgres")), database_url):
        try:
            with psycopg.connect(url, autocommit=True, connect_timeout=3) as conn:
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
                conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
            return
        except Exception:
            continue


# --- the seeded warehouse ------------------------------------------------------


def _pseudonymous_keys(count: int) -> list[str]:
    """Real keys from the real ingest function, under the test salt.

    Going through `oxbow.ingest.canonical.account_key` rather than a literal is the point:
    the PII boundary is at ingest, so the fixture crosses it exactly where the pipeline does.
    """
    identity = RunIdentity(run_salt=TEST_RUN_SALT, batch_id="a1b2c3d4e5f6", run_id=new_run_id())
    return [account_key(f"p7-subject-{index}", identity) for index in range(count)]


def _seed_warehouse(engine: Any, run_id: str, keys: list[str]) -> dict[str, Any]:
    """Write one complete run the way the pipeline writes it, then close the run.

    Rows go through `PostgresWarehouseSink` where the pipeline uses it and through plain
    INSERTs for the tables the sink does not own. The run is completed last because
    migration 0002's `trg_*_run_mutable` refuses rows into a completed run — a refusal this
    fixture would rather hit than paper over.
    """
    from oxbow.adapters.warehouse.models import Base

    tables = Base.metadata.tables
    bands = ("D", "E", "C", "B", "A", "D", "E", "C")
    seeded: dict[str, Any] = {"run_id": run_id, "keys": keys, "low": keys[:4], "high": keys[4:]}

    with Session(engine) as session:
        sink = PostgresWarehouseSink(session)
        sink.open_run(
            run_id,
            seed=1337,
            timezone="UTC",
            provenance="pipeline",
            config_hash="0" * 64,
            model_version="p7-fixture-1",
            dataset_ref="paysim-fake",
        )
        # The active policy is the API's own write-path table, but `policy_allocation`
        # FKs to it, so it has to exist before the pipeline's handoff rows are written.
        session.add(
            Policy(
                policy_id=POLICY_ID,
                name="P7 test capacity policy",
                active=True,
                capacity_minutes=3000,
                recovery_rate=0.35,
                recovery_sensitivity_band=[0.20, 0.35, 0.50],
                analyst_cost_per_hour_minor=9_000_00,
                min_review_minutes=10.0,
                friction_cost_minor=25_000_00,
                four_eyes_threshold_minor=50_000_000,
                review_minutes_by_band={"D": 30.0, "E": 45.0},
                currency=CURRENCY,
                solver="cp_sat",
                degraded=False,
                solve_ms=42,
            )
        )
        session.flush()

        def write(table: str, rows: list[dict[str, Any]]) -> None:
            if rows:
                sink.write(table, run_id, rows)

        def raw(table: str, rows: list[dict[str, Any]]) -> None:
            if not rows:
                return
            target = tables[table]
            payload = rows
            if "run_id" in target.columns and not any("run_id" in row for row in rows):
                payload = [{**row, "run_id": run_id} for row in rows]
            session.execute(insert(target), payload)

        write(
            "account",
            [
                {
                    "account_key": key,
                    "source_dataset": "paysim-fake",
                    "first_seen_at": DAY,
                    "last_seen_at": DAY + timedelta(days=3),
                    "txn_count": 10 + index,
                    "n_outbound": 4 + index,
                    "n_inbound": 6,
                    "n_counterparties": 3,
                    "funding_minor": 50_000_000_00 + index * 100,
                    "currency": CURRENCY,
                    "age_days": 90,
                }
                for index, key in enumerate(keys)
            ],
        )
        write(
            "score",
            [
                {
                    "account_key": key,
                    "fused_score": 0.55 + 0.05 * index,
                    "band": bands[index],
                    "scorecard_points": 40 + index,
                    "p_scorecard": 0.5,
                    "p_gbm": 0.52,
                    "anomaly_norm": 0.3,
                    "calibrated_probability": 0.61,
                    "calibration_band": "0.6-0.7",
                    "observed_rate": 0.63,
                    "calibration_n": 400 + index,
                    "predicted_typology": "crowdfunding",
                    "reason_codes": [{"code": "RC-RAPID-ACCUM", "label": "rapid accumulation"}],
                    "rule_ids": ["R4", "R7"],
                    "model_version": "p7-fixture-1",
                }
                for index, key in enumerate(keys)
            ],
        )
        write(
            "economics",
            [
                {
                    "account_key": key,
                    "currency": CURRENCY,
                    "exposure_minor": (
                        LOW_EXPOSURE_MINOR if key in seeded["low"] else HIGH_EXPOSURE_MINOR
                    ),
                    "expected_value_minor": 350_000_00,
                    "loss_avoided_minor": 120_000_00,
                    "analyst_cost_minor": 15_000_00,
                    "friction_cost_minor": 25_000_00,
                    "analyst_minutes": 30.0,
                    "recovery_rate": 0.35,
                    "ev_density": 0.11,
                    "mc_runs": 1000,
                    "mc_seed": 1337,
                    "mc_p05_minor": 90_000_00,
                    "mc_p50_minor": 120_000_00,
                    "mc_p95_minor": 160_000_00,
                    "mc_interval": [0.9, 1.2, 1.6],
                    "assumptions": {"recovery.rate": 0.35, "currency": CURRENCY},
                }
                for key in keys
            ],
        )
        write(
            "rule_hit",
            [
                {
                    "account_key": key,
                    "rule_id": "R4",
                    "rule_name": "rapid accumulation then dispersal",
                    "typology": "crowdfunding",
                    "fired": True,
                    "detail": {"hits": 3},
                }
                for key in keys
            ],
        )
        write(
            "shap_contribution",
            [
                {
                    "account_key": key,
                    "feature": "out_degree",
                    "shap": 0.21,
                    "rank": 1,
                    "evidence_txn_ids": ["paysim:0", "paysim:1"],
                }
                for key in keys
            ],
        )
        write(
            "scorecard_point",
            [
                {
                    "account_key": key,
                    "attribute": "out_degree",
                    "bin_label": "[4,8)",
                    "points": 12,
                    "woe": 0.4,
                    "reason_code": "RC-OUT-DEGREE",
                    "population_share": 0.08,
                    "bad_rate": 0.31,
                }
                for key in keys
            ],
        )
        write(
            "evidence_event",
            [
                {
                    "account_key": key,
                    "occurred_at": DAY + timedelta(hours=index),
                    "kind": "transaction",
                    "label": "inbound transfer",
                    "detail": {"amount_minor": 90_000_00, "currency": CURRENCY},
                }
                for index, key in enumerate(keys)
            ],
        )
        write(
            "transaction",
            [
                {
                    "txn_id": f"paysim:{index}",
                    "event_ts_utc": DAY + timedelta(minutes=index),
                    "event_date_local": DAY.date(),
                    "local_hour": 12,
                    "src_account_key": keys[index],
                    "dst_account_key": keys[index + 1],
                    "amount_minor": 100_000_00 + index,
                    "currency": CURRENCY,
                    "txn_type": "CASH_IN",
                    "source_dataset": "paysim-fake",
                    "src_balance_before": 10_000_000_00,
                    "src_balance_after": 10_100_000_00,
                    "dst_balance_before": 5_000_000_00,
                    "dst_balance_after": 5_100_000_00,
                    "label_fraud": 1,
                    "label_typology": "crowdfunding",
                }
                for index in range(len(seeded["low"]))
            ],
        )
        write(
            "graph_edge",
            [
                {
                    "src_account_key": keys[0],
                    "dst_account_key": keys[1],
                    "txn_count": 5,
                    "total_minor": 50_000_000_00,
                    "currency": CURRENCY,
                    "first_ts": DAY,
                    "last_ts": DAY + timedelta(hours=2),
                    "flags": ["cycle"],
                },
                {
                    "src_account_key": keys[1],
                    "dst_account_key": keys[2],
                    "txn_count": 3,
                    "total_minor": 30_000_000_00,
                    "currency": CURRENCY,
                    "first_ts": DAY,
                    "last_ts": DAY + timedelta(hours=2),
                    "flags": ["fan_out"],
                },
            ],
        )
        write(
            "community",
            [
                {
                    "canonical_index": 0,
                    "raw_label": "0",
                    "size": 3,
                    "algorithm": "leiden",
                    "seed": 1337,
                }
            ],
        )
        write(
            "account_membership",
            [{"account_key": key, "community_id": 0, "degree": 2} for key in keys[:3]],
        )
        write(
            "band_definition",
            [
                {
                    "band": band,
                    "lower_points": points,
                    "observed_rate": 0.4,
                    "n": 500,
                    "action": "review",
                    "review_minutes": 30.0,
                }
                for band, points in (("A", 0), ("B", 30), ("C", 45), ("D", 60), ("E", 80))
            ],
        )
        write(
            "backtest_fold",
            [
                {
                    "fold_index": 0,
                    "corpus": "paysim-fake",
                    "train_start": datetime(2025, 1, 1).date(),
                    "train_end": datetime(2025, 6, 1).date(),
                    "embargo_days": 14,
                    "embargo_end": datetime(2025, 6, 15).date(),
                    "test_start": datetime(2025, 6, 15).date(),
                    "test_end": datetime(2025, 9, 15).date(),
                    "n_train": 4000,
                    "n_test": 1000,
                    "pr_auc": 0.84,
                    "auroc": 0.91,
                    "brier": 0.12,
                    "precision_undefined": False,
                    "alerts": 120,
                    "captured_value_minor": 800_000_00,
                    "cost_minor": 9_000_00,
                    "net_benefit_minor": 710_000_00,
                    "max_drawdown_minor": 0,
                    "var95_minor": 1_000_00,
                    "es975_minor": 1_200_00,
                    "mc_runs": 1000,
                    "mc_seed": 1337,
                    "currency": CURRENCY,
                    "entity_disjoint": True,
                }
            ],
        )
        write(
            "validation_metric",
            [
                {"name": name, "value": value, "corpus": "paysim-fake"}
                for name, value in (("pr_auc", 0.847), ("auroc", 0.921), ("brier", 0.118))
            ],
        )
        write(
            "ablation_row",
            [
                {
                    "variant": "full",
                    "question": "does the rule layer add anything?",
                    "corpus": "paysim-fake",
                    "pr_auc": 0.847,
                    "net_benefit_minor": 710_000_00,
                    "currency": CURRENCY,
                    "ci_low": 0.8,
                    "ci_high": 0.89,
                    "ci_method": "bootstrap",
                    "n_resamples": 200,
                    "seed": 1337,
                }
            ],
        )
        write(
            "fairness_row",
            [
                {
                    "axis": "corridor",
                    "axis_rationale": "proxy for region",
                    "bucket": "north",
                    "fp_rate": 0.04,
                    "n": 400,
                }
            ],
        )
        write(
            "drift_period",
            [
                {
                    "attribute": "out_degree",
                    "period": "2025-Q2",
                    "psi": 0.08,
                    "n": 900,
                    "bad_rate": 0.31,
                }
            ],
        )
        write(
            "model_disagreement",
            [
                {
                    "account_key": key,
                    "band_scorecard": "D",
                    "band_gbm": "C",
                    "p_scorecard": 0.5,
                    "p_gbm": 0.62,
                    "delta": 0.12,
                }
                for key in keys[:3]
            ],
        )
        write(
            "policy_allocation",
            [
                {
                    "policy_id": POLICY_ID,
                    "account_key": key,
                    "rank": index + 1,
                    "selected": True,
                    "beyond_capacity": False,
                    "expected_value_minor": 350_000_00,
                    "exposure_minor": LOW_EXPOSURE_MINOR,
                    "analyst_minutes": 30.0,
                    "ev_density": 0.11,
                }
                for index, key in enumerate(keys)
            ],
        )

        # Tables outside the sink's handoff vocabulary, inserted against the real schema so
        # the real constraints and triggers still apply.
        raw(
            "confusion_cell",
            [{"label": "1", "prediction": "D", "n": 40}, {"label": "0", "prediction": "E", "n": 7}],
        )
        raw(
            "curve_point",
            [
                {
                    "family": "pr_curve",
                    "point_index": 0,
                    "x": 0.1,
                    "y": 0.9,
                    "n": 100,
                    "label": None,
                },
                {
                    "family": "pr_curve",
                    "point_index": 1,
                    "x": 0.2,
                    "y": 0.8,
                    "n": 200,
                    "label": None,
                },
                {
                    "family": "reliability",
                    "point_index": 0,
                    "x": 0.6,
                    "y": 0.63,
                    "n": 400,
                    "label": None,
                },
                {
                    "family": "shap_global",
                    "point_index": 0,
                    "x": 0.21,
                    "y": 0.0,
                    "n": None,
                    "label": "out_degree",
                },
                {
                    "family": "typology_recall",
                    "point_index": 0,
                    "x": 0.8,
                    "y": 0.7,
                    "n": 50,
                    "label": "crowdfunding",
                },
            ],
        )
        raw(
            "scorecard_spec",
            [
                {
                    "model_version": "p7-fixture-1",
                    "pdo": 20,
                    "base_score": 600,
                    "base_odds": 0.5,
                    "points_to_double": 40.0,
                    "offset": 600.0,
                    "scale_factor": 28.85,
                    "points_formula": "offset - factor * ln(odds)",
                    "n_total": 5000,
                    "n_bad": 900,
                }
            ],
        )
        raw(
            "scorecard_attribute",
            [
                {
                    "attribute": "out_degree",
                    "iv": 0.42,
                    "n_bins": 1,
                    "monotone": True,
                    "family": "network",
                }
            ],
        )
        raw(
            "scorecard_bin",
            [
                {
                    "attribute": "out_degree",
                    "bin_index": 0,
                    "label": "[4,8)",
                    "woe": 0.4,
                    "points": 12,
                    "population_share": 0.08,
                    "bad_rate": 0.31,
                    "n": 400,
                }
            ],
        )
        raw(
            "dataset_source",
            [
                {
                    "source_id": "paysim-fake",
                    "name": "PaySim (fixture slice)",
                    "role": "transactions",
                    "module": "oxbow.ingest",
                    "source_url": "https://example.invalid/paysim",
                    "retrieval": "fixture",
                    "license": "CC BY-SA 4.0",
                    "license_obligation": "attribution",
                    "citation": "Cruces 2018",
                    "description": "synthetic mobile-money transactions",
                    "label_caveat": "the label is a proxy, not a conviction",
                    "known_biases": [],
                    "synthetic_fields": ["amount"],
                    "ingest_allowed": True,
                    "retrieved_at": datetime(2026, 1, 1).date(),
                }
            ],
        )
        raw(
            "dataset_file",
            [
                {
                    "source_id": "paysim-fake",
                    "file_name": "paysim-fake.csv",
                    "sha256": "a" * 64,
                    "size_bytes": 1024,
                    "row_count": 6_362_620,
                    "verified_at": DAY,
                }
            ],
        )
        raw(
            "measurement",
            [
                {
                    "scope": "corpus",
                    "name": "n_rows",
                    "value": 6_362_620.0,
                    "unit": "rows",
                    "command": "uv run oxbow measure",
                    "measured_at": DAY,
                }
            ],
        )
        raw(
            "quarantine_row",
            [
                {
                    "source_dataset": "paysim-fake",
                    "failing_constraint": "amount >= 0",
                    "detail": "negative amount",
                    "original_row": {"amount": -1},
                }
            ],
        )

        for index in range(4):
            sink.record_stage_event(
                run_id,
                stage=("ingest", "graph", "score", "backtest")[index],
                status="complete",
                rows=100 + index,
                elapsed_ms=250 + index,
            )

        # The API's own write-path tables are not the pipeline's handoff, so these go in
        # through the ORM rather than pretending the sink owns them. Before the run is
        # completed, because `trg_policy_summary_run_mutable` refuses rows into a completed
        # run — the same refusal `make db-migrate` + `oxbow score` are subject to.
        session.add(
            PolicySummary(
                run_id=run_id,
                policy_id=POLICY_ID,
                currency=CURRENCY,
                capacity_minutes=3000,
                selected_count=4,
                candidate_count=8,
                loss_avoided_minor=480_000_00,
                analyst_cost_minor=6_000_00,
                friction_cost_minor=1_000_000_00,
                net_benefit_minor=420_000_00,
                benefit_per_analyst_hour_minor=84_000_00,
                max_drawdown_minor=0,
                zero_drawdown=True,
                var_alpha=0.95,
                var95_minor=1_000_00,
                es_alpha=0.975,
                es975_minor=1_200_00,
                mc_runs=1000,
                mc_seed=1337,
                alerts_per_10k_accounts=12.0,
                risk_adjusted_benefit=390_000_00,
                risk_adjusted_benefit_note="net benefit at the 95% VaR draw",
                cumulative_curve=[{"rank": 1, "cum_minor": 100}],
                frontier=[{"minutes": 60, "net_benefit_minor": 100}],
                baselines=[{"name": "threshold", "net_benefit_minor": 80}],
                assumptions={"recovery.rate": 0.35},
            )
        )
        session.flush()
        sink.complete_run(run_id, RunState.COMPLETE)
        session.commit()
    return seeded


# --- fixtures ------------------------------------------------------------------


@pytest.fixture(scope="module")
def null_client():
    """The app with nothing running but itself, with an explicitly null container."""
    mp = pytest.MonkeyPatch()
    _configure_env(mp)
    container = build_container()
    assert (
        container.backend == "null-file"
    ), f"the null fixture asked for OXBOW_WAREHOUSE=null and got {container.backend}"
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        # The lifespan builds its own container from the ambient environment; this fixture
        # owns the one the tests must see, so it is installed after startup, not before.
        app.state.container = container
        yield client
    container.close()
    mp.undo()
    reset_settings_cache()


@pytest.fixture(scope="module")
def warehouse():
    """A migrated, seeded Postgres. Provisioning is expensive, so it is module-scoped;
    per-test isolation is the write-path TRUNCATE in ``wh_client``."""
    mp = pytest.MonkeyPatch()
    url = _provision_database()
    _configure_env(mp, DATABASE_URL=url, OXBOW_WAREHOUSE="postgres")
    _migrate(url)

    from sqlalchemy import create_engine

    engine = create_engine(url, future=True)
    keys = _pseudonymous_keys(8)
    run_id = new_run_id()
    seeded = _seed_warehouse(engine, run_id, keys)

    container = build_container()
    assert container.backend == "postgres", (
        f"the fixture asked for OXBOW_WAREHOUSE=postgres and got {container.backend}; every "
        "write-path assertion below would be testing the null refusal instead"
    )
    yield {
        "url": url,
        "engine": engine,
        "container": container,
        "run_id": run_id,
        "keys": keys,
        **seeded,
    }
    container.close()
    engine.dispose()
    mp.undo()
    reset_settings_cache()
    _drop_database(url)


@pytest.fixture()
def clean_write_path(warehouse: dict[str, Any]) -> None:
    """Empty the API's own write tables, so a test sees the chain tip the seed left.

    This is the module's per-test isolation, factored out of ``wh_client`` because one
    write-path test deliberately does not take an HTTP client: the concurrent-append race
    needs two open transactions, and no request can hold one for it. Sharing the fixture is
    the only way that test gets the same empty chain every other write test assumes —
    without it it inherited the previous test's decision row and the first link of the
    chain was number two.
    """
    with Session(warehouse["engine"]) as session:
        session.execute(
            text(
                "TRUNCATE decision, audit_event, outbox, review_case, job_run, "
                "policy_simulation RESTART IDENTITY CASCADE"
            )
        )
        session.commit()


@pytest.fixture()
def wh_client(warehouse: dict[str, Any], clean_write_path: None):
    """A fresh app over the shared seeded warehouse, write-path tables cleared."""
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.container = warehouse["container"]
        assert app.state.container.backend == "postgres"
        yield client


def token_for(client: TestClient, subject: str, roles: list[str]) -> str:
    """Mint through the real demo-token route, not by calling `mint_local_token` directly.

    The route is part of the contract under test; if it refuses, these tests must fail
    rather than quietly work around it.
    """
    response = client.post(
        "/api/auth/demo-token",
        json={"subject": subject, "roles": roles, "display_name": subject},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["access_token"])


def analyst_headers(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(client, ANALYST, ['analyst'])}"}


def reviewer_headers(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(client, REVIEWER, ['analyst', 'reviewer'])}"}


# --- envelope / problem predicates (shared with the doctrine test) -------------


def assert_envelope(body: Any, *, where: str) -> dict[str, Any]:
    """The doctrine on a *served* body: exactly {data, meta}, and nothing else.

    Same allowed-key set and same forbidden-name set that
    `tests/integration/test_envelope_doctrine.py` uses, so the schema walk and this body
    walk cannot disagree about what the rule is.
    """
    assert isinstance(body, dict), f"{where}: body is {type(body).__name__}, not an object"
    keys = set(body)
    assert keys == set(ENVELOPE_KEYS), (
        f"{where}: operational body carries {sorted(keys)}; the doctrine allows exactly "
        f"{sorted(ENVELOPE_KEYS)}"
    )
    grown = sorted(keys & set(ENVELOPE_LEVEL_FIELDS))
    assert not grown, f"{where}: the envelope itself restates request status via {grown}"
    meta = body["meta"]
    assert isinstance(meta, dict), f"{where}: meta is not an object"
    assert meta.get("disclaimer"), f"{where}: meta carries no disclaimer"
    return body


def assert_problem(response: Any, *, status: int, where: str) -> dict[str, Any]:
    """RFC 9457 on the wire: media type, required members, and a status that agrees."""
    assert (
        response.status_code == status
    ), f"{where}: expected {status}, got {response.status_code} {response.text[:240]}"
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith(
        PROBLEM_MEDIA_TYPE
    ), f"{where}: error body is {content_type!r}, expected {PROBLEM_MEDIA_TYPE!r}"
    body = response.json()
    missing = [name for name in RFC9457_REQUIRED if name not in body]
    assert not missing, f"{where}: problem document lacks {missing}: {body}"
    assert body["status"] == status, f"{where}: body status {body['status']} != HTTP {status}"
    assert isinstance(body["title"], str) and body["title"], f"{where}: empty title"
    assert isinstance(body["detail"], str) and body["detail"], f"{where}: empty detail"
    assert body["type"].startswith(
        (PROBLEM_TYPE_BASE, "about:blank")
    ), f"{where}: type {body['type']!r} is neither an OXBOW problem URI nor about:blank"
    assert isinstance(body["instance"], str) and body["instance"].startswith(
        "/"
    ), f"{where}: instance {body['instance']!r} is not the request URI"
    return body


def walk(value: Any, path: str = "$") -> list[tuple[str, str, Any]]:
    """Every (path, key, scalar) in a served body, for the shape-independent checks."""
    found: list[tuple[str, str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict | list):
                found.extend(walk(item, f"{path}.{key}"))
            else:
                found.append((f"{path}.{key}", str(key), item))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict | list):
                found.extend(walk(item, f"{path}[{index}]"))
    return found


def api_routes(app: Any) -> list[Any]:
    from fastapi.routing import APIRoute

    return [route for route in app.routes if isinstance(route, APIRoute)]


def sse_event_ids(text_body: str) -> list[int]:
    return [int(line.split(":", 1)[1]) for line in text_body.splitlines() if line.startswith("id:")]


# --- 1. boot, route table, OpenAPI --------------------------------------------


def test_app_boots_in_process_and_registers_a_route_table(null_client: TestClient) -> None:
    """The layer is executable. Before this file, nothing had ever sent it a request."""
    app = null_client.app
    routes = api_routes(app)
    assert routes, "the app registered no API routes"
    paths = {route.path for route in routes}
    # Asserted by name, not by count: a route that silently vanishes is a failure, and a
    # route added later is not.
    for expected in (
        "/healthz",
        "/api/health",
        "/api/meta/dataset",
        "/api/meta/economics",
        "/api/meta/disclaimer",
        "/api/meta/licence",
        "/api/runs",
        "/api/runs/{run_id}",
        "/api/runs/{run_id}/progress",
        "/api/runs/{run_id}/rescore",
        "/api/runs/{run_id}/events",
        "/api/runs/{run_id}/events/stream",
        "/api/alerts",
        "/api/alerts/facets",
        "/api/cases/{case_id}",
        "/api/cases/{case_id}/transactions",
        "/api/cases/{case_id}/decisions",
        "/api/decisions/{decision_id}/confirm",
        "/api/outbox",
        "/api/outbox/pending",
        "/api/graph/subgraph",
        "/api/policy",
        "/api/policy/simulate",
        "/api/validation",
        "/api/scorecard",
        "/api/dashboard",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/auth/oidc",
        "/api/auth/demo-token",
        "/api/me",
    ):
        assert expected in paths, f"{expected} is not registered"
    for route in routes:
        assert route.methods, f"{route.path} registered with no HTTP method"


def test_openapi_schema_builds_and_declares_the_problem_union(
    null_client: TestClient,
) -> None:
    app = null_client.app
    document = app.openapi()
    assert document["openapi"].startswith("3."), document["openapi"]
    declared = set(document["paths"])
    registered = {route.path for route in api_routes(app) if route.include_in_schema}
    assert (
        registered <= declared
    ), f"routes missing from the document: {sorted(registered - declared)}"

    schemas = document["components"]["schemas"]
    assert "ProblemDetail" in schemas, sorted(schemas)
    for member in RFC9457_REQUIRED:
        assert member in schemas["ProblemDetail"]["properties"], (
            f"ProblemDetail schema lacks {member} — the generated client's error union "
            "would be built from a partial type"
        )
    sampled = document["paths"]["/api/runs"]["get"]["responses"]
    for code in ("400", "401", "403", "404", "409", "422", "500", "503"):
        content = sampled[code]["content"]
        assert PROBLEM_MEDIA_TYPE in content, f"{code} declares no {PROBLEM_MEDIA_TYPE}: {content}"
        ref = content[PROBLEM_MEDIA_TYPE]["schema"]["$ref"]
        assert ref.endswith("/ProblemDetail"), f"{code} declares {ref}"


def test_healthz_answers_unauthenticated_and_labels_every_degraded_component(
    null_client: TestClient,
) -> None:
    """/healthz is unauthenticated on purpose, and names what it is standing in for.

    This is the route that answered 500 when `api.deps.PROBED_COMPONENTS` grew
    ``watchlist`` and `schemas.health.ComponentName` did not: the Pydantic Literal refused
    the name, so the one endpoint a degraded deployment relies on was the one that broke.
    """
    response = null_client.get("/healthz")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = assert_envelope(response.json(), where="GET /healthz")
    data = body["data"]
    assert data["service"] == "oxbow-api"
    assert data["warehouse_backend"] == "null-file"
    assert data["write_path_enabled"] is False
    assert data["status"] in {"ok", "degraded"}
    assert data["auth_mode"] == "local-jwt-fallback", data["auth_mode"]
    names = {component["name"] for component in data["components"]}
    assert names == set(PROBED_COMPONENTS), names ^ set(PROBED_COMPONENTS)
    for component in data["components"]:
        assert component["state"] in {"available", "degraded", "unavailable"}, component
        assert component["detail"], f"{component['name']} reports no probe detail"
        if component["state"] != "available":
            assert component["fallback"], (
                f"{component['name']} is {component['state']} with no labelled fallback; "
                "plan §14 forbids the unlabelled substitution"
            )
    assert set(data["degraded_components"]) == {
        item["name"] for item in data["components"] if item["state"] != "available"
    }
    # The degraded state is a fact about the deployment, not a request status: a 200 whose
    # body claims failure is the second status channel the doctrine exists to prevent.
    assert data["status"] == "degraded", "nothing was down, which this host cannot claim"


def test_health_schema_literal_covers_every_probed_component() -> None:
    """The two halves of the health contract, pinned against each other."""
    declared = set(get_args(ComponentName))
    probed = set(PROBED_COMPONENTS)
    assert declared == probed, (
        f"api.deps probes {sorted(probed - declared)} that schemas.health.ComponentName "
        f"cannot represent, and the schema declares {sorted(declared - probed)} that nothing "
        "probes; /healthz 500s on the difference"
    )
    from api.schemas.health import FALLBACKS

    assert probed <= set(FALLBACKS) | {
        "warehouse"
    }, f"components with no fallback copy: {sorted(probed - set(FALLBACKS))}"


def test_run_schema_covers_every_field_the_read_model_builds() -> None:
    """`ReadModel.run_summary()` vs `RunSummary`/`RunDetail` — the second 500 found here.

    The accessor emitted ``notes`` and ``artifact_hashes``; with ``extra="forbid"`` an
    undeclared key is not dropped, it is a ValidationError inside FastAPI's response
    serialisation, so `GET /api/runs` failed for every run in the warehouse.
    """
    sample = ReadModel(FileWarehouseSource(REPO_ROOT / "out")).run_summary(
        {
            "run_id": "0" * 26,
            "created_at": datetime.now(UTC),
            "finished_at": None,
            "state": "complete",
            "seed": 1337,
            "timezone": "UTC",
            "provenance": "pipeline",
            "model_version": "m",
            "config_hash": "c",
        }
    )
    assert sample, "run_summary() produced nothing — the accessor moved, update this test"
    for model in (RunSummary, RunDetail):
        unmodeled = sorted(set(sample) - set(model.model_fields))
        assert not unmodeled, f"{model.__name__} cannot serialise {unmodeled}"
        assert _offenders(model) == [], f"{model.__name__} declares a second status channel"


# --- 2. the error contract, four tiers ----------------------------------------

# DESIGN.md §5 names four error tiers — inline field, pane-level, route-level, global.
# Mapped onto HTTP they are: the field tier (422 with per-field detail), the client-refusal
# tier (400/401/403/404), the conflict tier (409), and the global tier (the labelled 503
# degraded refusal and the catch-all 500). Each is produced below by a real request.


def test_field_tier_422_is_a_problem_document_with_field_detail(null_client: TestClient) -> None:
    headers = analyst_headers(null_client)
    response = null_client.get("/api/runs/not-a-ulid", headers=headers)
    body = assert_problem(response, status=422, where="GET /api/runs/{not-a-ulid}")
    assert body["errors"], f"the field tier arrived with no field detail: {body}"
    entry = body["errors"][0]
    assert {"location", "message"} <= set(entry), entry
    assert body.get("retryable") in (None, False), "a 422 must never be marked retryable"

    blank = null_client.post(
        f"/api/cases/{'0' * 26}/decisions",
        json={"action": "dismiss", "reason": "   ", "expected_version": 1},
        headers=headers,
    )
    body = assert_problem(blank, status=422, where="a decision with a blank reason")
    assert any("reason" in item["location"] for item in body["errors"]), body["errors"]


def test_client_refusal_tier_400_401_403_404_are_problems(null_client: TestClient) -> None:
    anonymous = null_client.get("/api/runs")
    assert_problem(anonymous, status=401, where="GET /api/runs with no Authorization")
    assert "Authorization" in anonymous.json()["detail"], "a 401 that names no cause"

    garbage = null_client.get("/api/runs", headers={"Authorization": "Bearer nonsense"})
    assert_problem(garbage, status=401, where="GET /api/runs with an unverifiable token")

    headers = analyst_headers(null_client)
    forbidden = null_client.post(
        "/api/policy/simulate", json={"capacity_minutes": 10}, headers=headers
    )
    assert_problem(forbidden, status=403, where="an analyst on a reviewer route")
    assert "reviewer" in forbidden.json()["detail"], forbidden.json()["detail"]

    bad_sort = null_client.get("/api/runs", params={"sort": "not_a_column"}, headers=headers)
    assert_problem(bad_sort, status=400, where="GET /api/runs with an unsortable column")

    missing = null_client.get(f"/api/runs/{'0' * 26}", headers=headers)
    assert_problem(missing, status=404, where="unknown run id")
    assert missing.json()["type"].endswith("run-not-found"), missing.json()["type"]


def test_conflict_tier_409_is_a_problem_document(null_client: TestClient) -> None:
    """The tier that has to exist for the audit chain's concurrent-append refusal."""
    headers = reviewer_headers(null_client)
    # `rescore` on an unknown-but-ULID run 404s; the reachable 409 here is rescore of a run
    # the file warehouse holds in a non-terminal state, which the queue must refuse.
    listing = null_client.get("/api/runs", headers=analyst_headers(null_client))
    assert listing.status_code == 200, listing.text
    running = next(
        (row["run_id"] for row in listing.json()["data"] if row["state"] == "running"), None
    )
    assert running, "the file warehouse holds no running run, so this tier is unexercised"
    response = null_client.post(f"/api/runs/{running}/rescore", headers=headers)
    body = assert_problem(response, status=409, where=f"rescore of running run {running}")
    assert "running" in body["detail"], body["detail"]


def test_global_tier_503_is_labelled_retryable_and_500_never_leaks(
    null_client: TestClient,
) -> None:
    """The write path is Postgres-only and says so instead of faking an accept."""
    headers = analyst_headers(null_client)
    refused = null_client.post(
        f"/api/cases/{'0' * 26}/decisions",
        json={"action": "dismiss", "reason": "no database, no decision", "expected_version": 1},
        headers=headers,
    )
    body = assert_problem(refused, status=503, where="decision write with no warehouse")
    assert body["retryable"] is True, "a dependency refusal is retryable; a 4xx is not"
    assert "Postgres" in body["detail"], body["detail"]

    # The catch-all: an undesigned raise must still be a problem document, must still be
    # correlatable, and must not repeat the traceback.
    app = create_app()

    @app.get("/api/_probe/boom", include_in_schema=False)
    def _boom() -> None:
        raise RuntimeError("deliberate unhandled failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.container = null_client.app.state.container
        crashed = client.get("/api/_probe/boom")
    body = assert_problem(crashed, status=500, where="unhandled RuntimeError")
    assert "deliberate unhandled failure" not in body["detail"], "traceback text leaked"
    assert body["trace_id"], "a 500 with no trace id cannot be matched to a log line"
    assert body["retryable"] is True


def test_no_reachable_route_answers_with_an_undesigned_internal_error(
    null_client: TestClient,
) -> None:
    """Every registered GET, called with well-formed input and a valid token, with nothing
    running.

    A 404/409/503 here is a *designed* refusal — `DependencyUnavailable` is a declared
    response of every route, and plan §14 requires the degraded answer to name itself. A 500
    whose type is ``internal-error`` is the thing this sweep exists to catch: a handler that
    raised something nobody designed. Two of those were found here (`/healthz`,
    `/api/graph/subgraph`), and the graph one would otherwise have looked exactly like a
    missing dependency.
    """
    headers = analyst_headers(null_client)
    listing = null_client.get("/api/runs", headers=headers)
    assert listing.status_code == 200, listing.text
    rows = listing.json()["data"]
    run_id = str(rows[0]["run_id"]) if rows else None

    sweeps: list[tuple[str, dict[str, Any]]] = [
        ("/healthz", {}),
        ("/api/health", {}),
        ("/api/meta/dataset", {}),
        ("/api/meta/economics", {}),
        ("/api/meta/disclaimer", {}),
        ("/api/meta/licence", {}),
        ("/api/meta/roles", {}),
        ("/api/runs", {}),
        ("/api/alerts", {}),
        ("/api/alerts/facets", {}),
        ("/api/policy", {}),
        ("/api/validation", {}),
        ("/api/scorecard", {}),
        ("/api/dashboard", {}),
        ("/api/jobs", {}),
        ("/api/auth/oidc", {}),
        ("/api/me", {}),
        ("/api/roles", {}),
        ("/api/outbox", {}),
        ("/api/outbox/pending", {}),
    ]
    if run_id is not None:
        sweeps += [
            (f"/api/runs/{run_id}", {}),
            (f"/api/runs/{run_id}/progress", {}),
            (f"/api/runs/{run_id}/events", {}),
            (f"/api/cases/{run_id}", {}),
            (f"/api/cases/{run_id}/transactions", {}),
            (f"/api/cases/{run_id}/decisions", {}),
            ("/api/graph/subgraph", {"params": {"account_key": run_id[:12]}}),
        ]
    offenders: list[str] = []
    undesigned = f"{PROBLEM_TYPE_BASE}internal-error"
    for path, kwargs in sweeps:
        response = null_client.get(path, headers=headers, **kwargs)
        if response.status_code < 500:
            continue
        try:
            problem = response.json()
        except Exception:
            offenders.append(f"GET {path} -> {response.status_code} with a non-JSON body")
            continue
        if problem.get("type") == undesigned:
            offenders.append(
                f"GET {path} -> {response.status_code} {problem.get('type')}: "
                f"{str(problem.get('detail'))[:200]}"
            )
        else:
            # A labelled refusal must still be a problem document and must still be honest.
            assert_problem(
                response, status=response.status_code, where=f"GET {path} (labelled refusal)"
            )
    assert not offenders, "reachable handlers raised an undesigned 500:\n" + "\n".join(offenders)


# --- authentication gates ------------------------------------------------------


def test_alg_none_and_the_algorithm_confusion_attack_are_refused() -> None:
    header = b64url_encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64url_encode(json.dumps({"iss": "oxbow-local", "sub": "x"}).encode())
    with pytest.raises(Exception, match="refused"):
        decode_token(
            f"{header}.{payload}.",
            oidc_issuer="",
            audience="oxbow-web",
            jwks_cache=None,
            local_secret=TEST_JWT_SECRET,
        )

    # HS256 signed with the local secret but claiming the OIDC issuer: the algorithm is
    # chosen by the issuer, never by the token header. This is the classic confusion attack
    # that turns a public JWKS into an HMAC secret.
    foreign = b64url_encode(json.dumps({"iss": ISSUER, "sub": "x", "aud": "oxbow-web"}).encode())
    hs_header = b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    signature = b64url_encode(
        hmac.new(
            TEST_JWT_SECRET.encode("utf-8"),
            f"{hs_header}.{foreign}".encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    with pytest.raises(Exception, match="RS256"):
        decode_token(
            f"{hs_header}.{foreign}.{signature}",
            oidc_issuer=ISSUER,
            audience="oxbow-web",
            jwks_cache=None,
            local_secret=TEST_JWT_SECRET,
        )


def test_a_token_with_no_oxbow_role_is_refused_not_defaulted(null_client: TestClient) -> None:
    bare = mint_local_token(subject="p7-operator-no-role", roles=[], secret=TEST_JWT_SECRET)
    response = null_client.get("/api/me", headers={"Authorization": f"Bearer {bare}"})
    assert_problem(response, status=403, where="a token carrying no OXBOW role")
    assert "analyst" in response.json()["detail"], "the refusal must name the role set"

    stale = mint_local_token(
        subject=ANALYST,
        roles=["analyst"],
        secret=TEST_JWT_SECRET,
        now=time.time() - 7200,
        ttl_seconds=60,
    )
    expired = null_client.get("/api/me", headers={"Authorization": f"Bearer {stale}"})
    assert_problem(expired, status=401, where="an expired demo token")


def test_demo_token_route_is_the_only_identity_here_and_labels_itself(
    null_client: TestClient,
) -> None:
    minted = null_client.post(
        "/api/auth/demo-token", json={"subject": ANALYST, "roles": ["analyst"]}
    )
    assert minted.status_code == 200, minted.text
    body = assert_envelope(minted.json(), where="POST /api/auth/demo-token")
    data = body["data"]
    assert data["issuer"] == "oxbow-local"
    assert data["principal"]["source"] == "local-jwt"
    assert data["warning"], "a demo identity that does not label itself is a backdoor"

    unknown_role = null_client.post(
        "/api/auth/demo-token", json={"subject": ANALYST, "roles": ["superuser"]}
    )
    assert_problem(unknown_role, status=422, where="a demo token for an unknown role")


# --- 3. the envelope doctrine on served bodies ---------------------------------


OPERATIONAL_PATHS: tuple[str, ...] = (
    "/healthz",
    "/api/health",
    "/api/meta/dataset",
    "/api/meta/economics",
    "/api/meta/disclaimer",
    "/api/meta/licence",
    "/api/meta/roles",
    "/api/runs",
    "/api/alerts",
    "/api/alerts/facets",
    "/api/policy",
    "/api/validation",
    "/api/scorecard",
    "/api/dashboard",
    "/api/jobs",
    "/api/outbox",
    "/api/outbox/pending",
    "/api/auth/oidc",
    "/api/me",
    "/api/roles",
)


def test_every_operational_endpoint_answers_200_with_the_documented_shape(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    """With a real warehouse behind it, none of these may refuse."""
    headers = analyst_headers(wh_client)
    run_id = warehouse["run_id"]
    paths = [
        *OPERATIONAL_PATHS,
        f"/api/runs/{run_id}",
        f"/api/runs/{run_id}/progress",
        f"/api/runs/{run_id}/events",
        f"/api/graph/subgraph?account_key={warehouse['low'][0]}",
    ]
    failures: list[str] = []
    for path in paths:
        url, _, query = path.partition("?")
        response = wh_client.get(
            url,
            params=dict(pair.split("=") for pair in query.split("&")) if query else None,
            headers=headers,
        )
        if response.status_code != 200:
            failures.append(f"GET {path} -> {response.status_code} {response.text[:200]}")
            continue
        assert response.headers["content-type"].startswith("application/json"), path
        assert_envelope(response.json(), where=f"GET {path}")
    assert not failures, "operational endpoints that did not answer:\n" + "\n".join(failures)


def test_paged_endpoints_keep_paging_inside_meta(wh_client: TestClient) -> None:
    headers = analyst_headers(wh_client)
    response = wh_client.get("/api/runs", params={"limit": 3, "offset": 0}, headers=headers)
    body = assert_envelope(response.json(), where="GET /api/runs paged")
    meta = body["meta"]
    assert meta["limit"] == 3 and meta["offset"] == 0
    assert meta["total"] >= 1
    assert {"limit", "offset", "total", "sort", "order"} <= set(meta), sorted(meta)
    assert isinstance(body["data"], list) and len(body["data"]) <= 3


def test_money_never_leaves_the_api_as_a_float(wh_client: TestClient) -> None:
    """`*_minor` is int64 everywhere in a served body — never a float, never a bool."""
    headers = analyst_headers(wh_client)
    bodies: list[tuple[str, Any]] = []
    for path in (
        "/api/alerts",
        "/api/dashboard",
        "/api/policy",
        "/api/validation",
        f"/api/runs/{wh_client.get('/api/runs', headers=headers).json()['data'][0]['run_id']}",
    ):
        response = wh_client.get(path, headers=headers)
        assert response.status_code == 200, f"{path} -> {response.text[:200]}"
        bodies.append((path, response.json()))
    offenders: list[str] = []
    total_minor_fields = 0
    for path, body in bodies:
        for location, key, value in walk(body):
            if not (key.endswith("_minor") or key == "minor"):
                continue
            total_minor_fields += 1
            if isinstance(value, bool) or not isinstance(value, int):
                offenders.append(f"{path}{location} ({key}) = {value!r} {type(value).__name__}")
    assert total_minor_fields >= 10, f"only {total_minor_fields} money fields were exercised"
    assert not offenders, "money outside integer minor units:\n" + "\n".join(offenders)

    alerts = wh_client.get("/api/alerts", headers=headers).json()["data"]
    row = alerts["rows"][0] if isinstance(alerts, dict) and alerts.get("rows") else alerts[0]
    exposure = row["exposure"]
    assert set(exposure) == {"minor", "currency", "decimals"}, exposure
    assert isinstance(exposure["minor"], int) and exposure["currency"] == CURRENCY


def test_only_pseudonymous_account_keys_reach_a_response(wh_client: TestClient) -> None:
    """The PII boundary is at ingest, so nothing downstream can name a real subject."""
    headers = analyst_headers(wh_client)
    alerts = wh_client.get("/api/alerts", headers=headers).json()["data"]
    rows = alerts["rows"] if isinstance(alerts, dict) else alerts
    assert rows, "the fixture produced no queue rows, so this check would be vacuous"

    key_names = ("account_key", "src_account_key", "dst_account_key")
    seen: list[str] = []
    for location, key, value in walk(alerts):
        if key in key_names:
            assert isinstance(value, str) and ACCOUNT_KEY_SHAPE.match(
                value
            ), f"{location} = {value!r} is not a pseudonymous account key"
            seen.append(value)
    assert seen, "no account key was exercised"

    run_id = wh_client.get("/api/runs", headers=headers).json()["data"][0]["run_id"]
    for path in ("/api/alerts", "/api/dashboard", "/api/validation", f"/api/runs/{run_id}"):
        for location, key, value in walk(wh_client.get(path, headers=headers).json()):
            if not isinstance(value, str):
                continue
            assert not RAW_ACCOUNT_ID_SHAPE.match(
                value
            ), f"{path}{location} carries what looks like a raw account identifier: {value!r}"
            if key in key_names:
                assert ACCOUNT_KEY_SHAPE.match(value), f"{path}{location} = {value!r}"

    assert display_account_key(seen[0]).startswith("ACC-"), "the operator spelling diverged"


def test_sse_frames_are_exempt_from_the_envelope_and_resume_without_duplicates(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    """`text/event-stream` is exempt from the envelope rule — but the exemption is bought
    with the cursor rule, which is what this asserts."""
    headers = analyst_headers(wh_client)
    run_id = warehouse["run_id"]
    path = f"/api/runs/{run_id}/events/stream"
    first = wh_client.get(path, headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers["content-type"].startswith("text/event-stream")
    assert "retry:" in first.text, "the stream must set the reconnect schedule"

    ids = sse_event_ids(first.text)
    assert ids, f"no event frames in {first.text[:200]}"
    assert len(ids) == len(set(ids)), f"duplicate ids delivered: {ids}"
    assert ids == sorted(ids)
    assert "event: end" in first.text, "a terminal run must close the stream, not hang"
    assert '"exposure_minor"' not in first.text  # frames are the stage ledger, not money

    cursor = ids[1]
    resumed = wh_client.get(path, headers={**headers, "Last-Event-ID": str(cursor)})
    assert resumed.status_code == 200, resumed.text
    assert sse_event_ids(resumed.text) == [item for item in ids if item > cursor], (
        f"resume from {cursor} delivered {sse_event_ids(resumed.text)}, "
        f"expected {[item for item in ids if item > cursor]}"
    )
    malformed = wh_client.get(path, headers={**headers, "Last-Event-ID": "not-a-number"})
    assert_problem(malformed, status=400, where="a Last-Event-ID that is not an integer")


def test_the_envelope_guard_bites_on_a_known_bad_body(wh_client: TestClient) -> None:
    """A guard that has never rejected anything is decoration. Hand it the violation."""
    with pytest.raises(AssertionError, match="doctrine"):
        assert_envelope({"data": [], "meta": {}, "success": True}, where="negative control")
    with pytest.raises(AssertionError, match="doctrine"):
        assert_envelope({"data": [], "meta": {}, "error": None}, where="negative control")
    with pytest.raises(AssertionError, match="doctrine"):
        assert_envelope({"rows": [], "total": 0}, where="negative control")
    # And a domain object inside data may still carry its own `error` — RunDetail.error and
    # JobStatus.error are facts about a run, not a second request status.
    assert_envelope(
        {"data": {"error": "stage failed"}, "meta": {"disclaimer": "d"}},
        where="nested domain error",
    )


# --- 4. the write path: decision, audit chain, outbox --------------------------


def _open_case(client: TestClient, headers: dict[str, str], run_id: str, key: str) -> str:
    response = client.post(
        "/api/cases", params={"run_id": run_id, "account_key": key}, headers=headers
    )
    assert response.status_code == 200, response.text
    return str(assert_envelope(response.json(), where="POST /api/cases")["data"]["case_id"])


def _counts_for_trace(warehouse: dict[str, Any], trace: str) -> dict[str, int]:
    """Rows belonging to one writer, counted without trusting test ordering.

    ``_counts`` reads whole tables, and this database is shared by every test in the
    file, so an absolute count is a statement about which tests ran first. This scopes
    the count to one writer's ``trace_id``.

    The outbox cannot be filtered by trace: ``outbox_message`` has no ``trace_id``
    column and its payload is the case bundle, which does not carry one. It is joined
    the way the schema relates it — the decision's own ``case_id`` + ``decision_seq`` —
    so the two delivery rows an escalation owes are counted against the winner and can
    never pick up the loser's.
    """
    with Session(warehouse["engine"]) as session:
        decisions = int(
            session.execute(
                select(func.count()).select_from(Decision).where(Decision.trace_id == trace)
            ).scalar_one()
        )
        audits = int(
            session.execute(
                select(func.count()).select_from(AuditEvent).where(AuditEvent.trace_id == trace)
            ).scalar_one()
        )
        anchors = session.execute(
            select(Decision.case_id, Decision.decision_seq).where(Decision.trace_id == trace)
        ).all()
        outbox = 0
        for case_id, decision_seq in anchors:
            outbox += int(
                session.execute(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(
                        OutboxMessage.case_id == case_id,
                        OutboxMessage.decision_seq == decision_seq,
                    )
                ).scalar_one()
            )
    return {"decision": decisions, "audit_event": audits, "outbox": outbox}


def _rows_for_trace(warehouse: dict[str, Any], trace: str) -> list[Any]:
    """Every row a refused writer left behind, by trace. Should always be empty."""
    with Session(warehouse["engine"]) as session:
        found = session.execute(select(Decision).where(Decision.trace_id == trace)).scalars().all()
    return list(found)


def _counts(warehouse: dict[str, Any]) -> dict[str, int]:
    with Session(warehouse["engine"]) as session:
        return {
            "decision": int(
                session.execute(select(func.count()).select_from(Decision)).scalar_one()
            ),
            "audit_event": int(
                session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
            ),
            "outbox": int(
                session.execute(select(func.count()).select_from(OutboxMessage)).scalar_one()
            ),
        }


def test_a_decision_commit_writes_the_three_rows_together(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    """One POST, one transaction: decision row + audit-chain row + outbox row."""
    headers = analyst_headers(wh_client)
    run_id, low = warehouse["run_id"], warehouse["low"]
    case_id = _open_case(wh_client, headers, run_id, low[0])
    before = _counts(warehouse)

    response = wh_client.post(
        f"/api/cases/{case_id}/decisions",
        json={
            "action": "dismiss",
            "reason": "counterparty verified as a registered market stall",
            "expected_version": 1,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = assert_envelope(response.json(), where="POST /api/cases/{id}/decisions")["data"]
    assert data["outbox_queued"] is True
    assert data["four_eyes_state"] == "not_required"
    assert data["chain_seq"] == before["decision"] + 1

    after = _counts(warehouse)
    assert after["decision"] == before["decision"] + 1
    assert after["audit_event"] == before["audit_event"] + 1
    assert after["outbox"] == before["outbox"] + 1

    with Session(warehouse["engine"]) as session:
        row = session.get(Decision, data["decision_id"])
        assert row is not None
        assert row.row_hash == data["row_hash"]
        assert (
            row.reason == "counterparty verified as a registered market stall"
        ), "the reason is the sentence that survives; it must not be reworded"
        assert isinstance(row.exposure_minor, int) and not isinstance(row.exposure_minor, bool)
        assert row.exposure_minor == LOW_EXPOSURE_MINOR, "exposure came from the code, not the row"
        assert row.account_key == low[0]
        outbox_row = (
            session.execute(select(OutboxMessage).where(OutboxMessage.case_id == case_id))
            .scalars()
            .one()
        )
        assert outbox_row.status == "pending"
        assert outbox_row.case_seq == 1
        assert outbox_row.idempotency_key == build_idempotency_key(run_id, case_id, 1)
        assert outbox_row.payload["economics"]["currency"] == CURRENCY
        assert isinstance(outbox_row.payload["economics"]["exposure_minor"], int)
        audit = session.execute(select(AuditEvent).order_by(AuditEvent.chain_seq)).scalars().all()
        assert len(audit) == 1
        assert (
            audit[0].payload["decision_row_hash"] == row.row_hash
        ), "the audit row must pin the digest the decision row carries"


def test_outbox_write_failure_rolls_back_the_audit_and_decision_rows(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    """The outbox invariant, demonstrated the way it must be: make the second write fail
    and show the first is absent.

    A committed `outbox` row already holds the idempotency key this decision would use, so
    `uq_outbox_idempotency` refuses the delivery promise. The decision row and its
    audit-chain row were already flushed inside the same transaction, so they have to
    vanish with it. Anything else is the failure the pattern exists to prevent: an audit
    trail that records a delivery nobody promised.
    """
    headers = analyst_headers(wh_client)
    run_id, low = warehouse["run_id"], warehouse["low"]
    case_id = _open_case(wh_client, headers, run_id, low[0])
    decoy_case = _open_case(wh_client, headers, run_id, low[1])
    decoy_key = build_idempotency_key(run_id, case_id, 1)

    with Session(warehouse["engine"]) as session:
        # A different case_id, so the constraint that fires is the idempotency one — the
        # one that means "this delivery promise already exists".
        session.execute(
            insert(OutboxMessage.__table__),
            {
                "idempotency_key": decoy_key,
                "run_id": run_id,
                "case_id": decoy_case,
                "decision_seq": 99,
                "case_seq": 99,
                "sink_id": api_decisions.OUTBOX_SINK_CASE,
                "schema_version": api_decisions.OUTBOX_SCHEMA_VERSION,
                "payload": {"decoy": True},
                "status": "pending",
                "attempts": 0,
                "max_attempts": 5,
                "next_attempt_at": datetime.now(UTC),
                "attempt_log": [],
            },
        )
        session.commit()

    before = _counts(warehouse)
    response = wh_client.post(
        f"/api/cases/{case_id}/decisions",
        json={
            "action": "escalate",
            "reason": "funds dispersed to six counterparties met this week",
            "expected_version": 1,
        },
        headers=headers,
    )
    body = assert_problem(response, status=409, where="a decision whose delivery promise exists")
    assert decoy_key in body["detail"], body["detail"]

    after = _counts(warehouse)
    assert after["decision"] == before["decision"], "the decision row survived the rollback"
    assert after["audit_event"] == before["audit_event"], (
        "the audit-chain row survived the outbox failure — the two are one transaction, and "
        "this is the assertion that makes that claim real rather than documented"
    )
    assert after["outbox"] == before["outbox"]

    with Session(warehouse["engine"]) as session:
        case = session.get(Case, case_id)
        assert case is not None and case.version == 1, "a rolled back write bumped the version"
        assert (
            session.execute(select(Decision).where(Decision.case_id == case_id)).scalars().all()
            == []
        )


def test_confirmation_outbox_failure_rolls_back_the_confirmation_audit_row(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    """The same invariant from the other side: confirmation releases the outbox row in one
    commit, so a refused delivery leaves the decision still awaiting second review."""
    headers = analyst_headers(wh_client)
    reviewer = reviewer_headers(wh_client)
    run_id, high = warehouse["run_id"], warehouse["high"]
    case_id = _open_case(wh_client, headers, run_id, high[0])
    decoy_case = _open_case(wh_client, headers, run_id, high[1])

    created = wh_client.post(
        f"/api/cases/{case_id}/decisions",
        json={
            "action": "escalate",
            "reason": "exposure above the four-eyes threshold",
            "expected_version": 1,
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    decision = assert_envelope(created.json(), where="four-eyes decision")["data"]
    assert decision["four_eyes_required"] is True
    assert decision["four_eyes_state"] == "pending"
    assert (
        decision["outbox_queued"] is False
    ), "a decision awaiting second review must not already have a delivery promise"
    assert _counts(warehouse)["outbox"] == 0

    with Session(warehouse["engine"]) as session:
        session.execute(
            insert(OutboxMessage.__table__),
            {
                "idempotency_key": build_idempotency_key(run_id, case_id, 1),
                "run_id": run_id,
                "case_id": decoy_case,
                "decision_seq": 99,
                "case_seq": 99,
                "sink_id": api_decisions.OUTBOX_SINK_CASE,
                "schema_version": api_decisions.OUTBOX_SCHEMA_VERSION,
                "payload": {"decoy": True},
                "status": "pending",
                "attempts": 0,
                "max_attempts": 5,
                "next_attempt_at": datetime.now(UTC),
                "attempt_log": [],
            },
        )
        session.commit()

    audit_before = _counts(warehouse)["audit_event"]
    confirm = wh_client.post(
        f"/api/decisions/{decision['decision_id']}/confirm",
        json={"expected_version": 2, "confirmation_note": "reviewed the dispersal pattern"},
        headers=reviewer,
    )
    assert_problem(confirm, status=409, where="a confirm whose delivery promise exists")

    with Session(warehouse["engine"]) as session:
        row = session.get(Decision, decision["decision_id"])
        assert row is not None
        assert row.four_eyes_state == "pending", "the confirmation half-applied"
        assert row.confirmed_by is None
        case = session.get(Case, case_id)
        assert case is not None and case.version == 2, "a rolled back confirm bumped the version"
    assert (
        _counts(warehouse)["audit_event"] == audit_before
    ), "the confirmation's audit row was written while its outbox row was refused"


def test_four_eyes_needs_a_different_subject_not_a_different_role(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    headers = analyst_headers(wh_client)
    run_id, high = warehouse["run_id"], warehouse["high"]
    case_id = _open_case(wh_client, headers, run_id, high[0])
    created = wh_client.post(
        f"/api/cases/{case_id}/decisions",
        json={
            "action": "escalate",
            "reason": "structured deposits then an immediate payout",
            "expected_version": 1,
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    decision = created.json()["data"]
    assert decision["four_eyes_state"] == "pending"

    both_roles = {
        "Authorization": f"Bearer {token_for(wh_client, ANALYST, ['analyst', 'reviewer'])}"
    }
    self_confirm = wh_client.post(
        f"/api/decisions/{decision['decision_id']}/confirm",
        json={"expected_version": 2, "confirmation_note": "same person, second role"},
        headers=both_roles,
    )
    body = assert_problem(self_confirm, status=403, where="a self-confirmation")
    assert body["type"].endswith("four-eyes-not-satisfied"), body["type"]

    other = reviewer_headers(wh_client)
    confirm = wh_client.post(
        f"/api/decisions/{decision['decision_id']}/confirm",
        json={"expected_version": 2, "confirmation_note": "independently reviewed"},
        headers=other,
    )
    assert confirm.status_code == 200, confirm.text
    data = assert_envelope(confirm.json(), where="confirm")["data"]
    assert data["four_eyes_state"] == "confirmed"
    assert data["outbox_queued"] is True

    with Session(warehouse["engine"]) as session:
        rows = (
            session.execute(
                select(OutboxMessage)
                .where(OutboxMessage.case_id == case_id)
                .order_by(OutboxMessage.case_seq)
            )
            .scalars()
            .all()
        )
        assert [row.sink_id for row in rows] == [
            api_decisions.OUTBOX_SINK_CASE,
            api_decisions.OUTBOX_SINK_NOTIFY,
        ], "an escalation owes the case sink and a notification, in that order"
        assert rows[0].decision_seq == 1
        depth = session.execute(select(func.count()).select_from(OutboxMessage)).scalar_one()
        assert depth == 2

    again = wh_client.post(
        f"/api/decisions/{decision['decision_id']}/confirm",
        json={"expected_version": 3, "confirmation_note": "a second confirmation"},
        headers=other,
    )
    assert_problem(again, status=409, where="an already-confirmed decision")


def test_stale_expected_version_returns_409_carrying_the_merge_view(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    """Optimistic concurrency: the loser is shown the current decision, not told it lost."""
    headers = analyst_headers(wh_client)
    run_id, low = warehouse["run_id"], warehouse["low"]
    case_id = _open_case(wh_client, headers, run_id, low[0])
    first = wh_client.post(
        f"/api/cases/{case_id}/decisions",
        json={"action": "review", "reason": "needs a call to the agent", "expected_version": 1},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["case_version"] == 2

    stale = wh_client.post(
        f"/api/cases/{case_id}/decisions",
        json={"action": "dismiss", "reason": "a stale write attempt", "expected_version": 1},
        headers=headers,
    )
    body = assert_problem(stale, status=409, where="a write against a stale version")
    assert body["type"].endswith("version-conflict"), body["type"]
    assert body["expected_version"] == 1 and body["current_version"] == 2, body
    current = body["current"]
    assert current["action"] == "review"
    assert current["reason"] == "needs a call to the agent"
    assert len(current["row_hash"]) == 64, "the merge view must show the hash the winner signed"
    assert _counts(warehouse)["decision"] == 1, "the loser wrote a row anyway"


def test_concurrent_append_gives_one_success_and_one_409(warehouse: dict[str, Any]) -> None:
    """Two writers that read the same chain tip: one link lands, the other is refused.

    The interleaving is produced by snapshot isolation rather than by sleeping. Both
    transactions are open at the same instant; ``loser`` takes its snapshot first, ``winner``
    appends and commits, and the loser's INSERT then collides with
    ``uq_decision_chain_seq`` — which is exactly the race plan §13 describes. The answer has
    to be a conflict. It was a 500 before this file existed, because the raw `IntegrityError`
    from the decision INSERT was never translated.
    """
    container = warehouse["container"]
    read_model = container.read_model
    run_id, low = warehouse["run_id"], warehouse["low"]

    loader = container.new_session()
    case_ids = [
        str(api_decisions.open_case(loader, read_model, run_id=run_id, account_key=key).case_id)
        for key in (low[0], low[1])
    ]
    loader.commit()
    loader.close()

    def principal(subject: str) -> Principal:
        return Principal(
            subject=subject, roles=("analyst",), display_name=subject, source="local-jwt"
        )

    def submit(session: Session, case_id: str, subject: str, trace: str) -> dict[str, Any]:
        case = session.get(Case, case_id)
        assert case is not None
        return api_decisions.record_decision(
            session,
            read_model,
            container.audit_sink(session),
            container.economics,
            case=case,
            write=api_decisions.DecisionWrite(
                action="escalate",
                reason="concurrent append candidate",
                expected_version=int(case.version),
                reversal_of_decision_id=None,
                principal=principal(subject),
                # Each writer is given its own trace_id at the call site so the
                # assertions below can ask "did *this* writer persist?" rather than
                # "how many rows are in the table?", which is a question about test
                # ordering, not about the race.
                trace_id=trace,
            ),
        )

    winner = container.new_session()
    loser = container.new_session()
    try:
        loser.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        loser.execute(text("SELECT 1"))  # fixes the loser's snapshot, as a real race does
        result = submit(winner, case_ids[0], "p7-operator-winner", "p7-concurrency-probe")
        winner.commit()

        with pytest.raises(api_decisions.Conflict) as refused:
            submit(loser, case_ids[1], "p7-operator-loser", "p7-concurrency-loser-probe")
        assert refused.value.status == 409
        loser.rollback()

        # Not `== 1`. `chain_seq` is the position in the *global* decision chain, so this
        # test only saw 1 when it happened to run before any other test committed a
        # decision into the same database. That is an ordering accident, not a property:
        # the winner is the one and only append here, so what it must own is a sequence
        # number no other decision in this test produced, and the loser's 409 must carry
        # that same number. Run in isolation, or after any other decision test, the old
        # `== 1` failed on a correct implementation.
        assert result["chain_seq"] >= 1
        # An escalation owes two delivery promises — the case sink and the notification —
        # which is what `test_four_eyes_needs_a_different_subject_not_a_different_role`
        # pins. Both belong to the winner. This count was 1 when the file was written, but
        # the notify INSERT could not then be reached at all: its idempotency key was 71
        # characters into a VARCHAR(64) column, so the whole test died on `DataError`
        # before any count ran. The corrected expectation is still exact — one decision,
        # one audit row, two outbox rows — and a loser leaving anything behind still breaks
        # it, which is the property the assertion exists for.
        # Counted by trace_id, not globally. `_counts` reads the whole decision table, and
        # this database is shared by every test in the file, so asserting absolute totals
        # here only passed when this test happened to run first — the moment another
        # decision test committed, the loser-free counts came back 2/2/3 and the
        # assertion fired against a correct implementation. The property under test is
        # scoped to this race: exactly one of *these two* writers persisted, and the
        # winner's trace_id is the only one present.
        assert _counts_for_trace(warehouse, "p7-concurrency-probe") == {
            "decision": 1,
            "audit_event": 1,
            "outbox": 2,
        }, "the loser left rows behind"
        assert not _rows_for_trace(
            warehouse, "p7-concurrency-loser-probe"
        ), "the losing writer persisted something; a refused append must leave no trace"
        with Session(warehouse["engine"]) as session:
            assert (
                session.execute(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(OutboxMessage.case_id == case_ids[1])
                ).scalar_one()
                == 0
            ), "the refused writer queued a delivery for its own case"

        # The next writer after the race is not poisoned by it — and the refused
        # writer did not burn a chain position on the way out. `chain_seq` is global,
        # so the absolute value (this test used to demand `== 2`) depends on which
        # tests ran first; the *relationship* does not. The writer that follows the
        # winner must land on the very next link, which is only true if the 409
        # rolled its sequence back with everything else.
        third = container.new_session()
        again = submit(third, case_ids[1], "p7-operator-third", "p7-concurrency-third-probe")
        third.commit()
        third.close()
        assert again["chain_seq"] == result["chain_seq"] + 1
        assert _counts_for_trace(warehouse, "p7-concurrency-third-probe") == {
            "decision": 1,
            "audit_event": 1,
            "outbox": 2,
        }, "the writer after the race did not land a complete append"
    finally:
        for session in (winner, loser):
            session.close()


def test_audit_chain_verifies_after_the_write_path(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    """The chain the decisions built walks clean, and a tampered row is named by sequence."""
    headers = analyst_headers(wh_client)
    run_id, low = warehouse["run_id"], warehouse["low"]
    for index, key in enumerate(low[:3]):
        case_id = _open_case(wh_client, headers, run_id, key)
        response = wh_client.post(
            f"/api/cases/{case_id}/decisions",
            json={
                "action": "dismiss",
                "reason": f"cleared on review {index}",
                "expected_version": 1,
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text

    with Session(warehouse["engine"]) as session:
        rows = api_decisions.decision_chain_rows(session)
    assert len(rows) == 3, [row.seq for row in rows]
    verification = verify_chain(rows)
    assert verification.ok, verification.first_broken
    assert verification.rows_checked == 3
    assert [row.seq for row in rows] == [1, 2, 3]
    assert rows[1].prev_hash == rows[0].row_hash, "a chain link that does not link"

    edited = rows[1]
    tampered_row = type(rows[1])(
        seq=edited.seq,
        occurred_at=edited.occurred_at,
        actor_id=edited.actor_id,
        subject=edited.subject,
        action=edited.action,
        payload={**edited.payload, "reason": "rewritten after the fact"},
        prev_hash=edited.prev_hash,
        row_hash=edited.row_hash,
    )
    tampered = verify_chain([rows[0], tampered_row, rows[2]])
    assert not tampered.ok and tampered.first_broken is not None
    assert tampered.first_broken.seq == 2, "the break must be named by sequence number"
    assert "digest mismatch" in tampered.first_broken.reason, tampered.first_broken.reason


def test_outbox_ordering_is_per_case_and_never_global(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    """`case_seq` restarts per case, and the drain refuses to send a later row while an
    earlier one for the same case is still unsent."""
    headers = analyst_headers(wh_client)
    run_id, low = warehouse["run_id"], warehouse["low"]
    case_a = _open_case(wh_client, headers, run_id, low[0])
    case_b = _open_case(wh_client, headers, run_id, low[1])

    for pass_number in range(1, 4):
        for case_id in (case_a, case_b):
            response = wh_client.post(
                f"/api/cases/{case_id}/decisions",
                json={
                    "action": "review",
                    "reason": f"review pass {pass_number}",
                    "expected_version": pass_number,
                },
                headers=headers,
            )
            assert response.status_code == 200, response.text

    with Session(warehouse["engine"]) as session:
        rows_a = api_decisions.outbox_rows_for(session, case_a)
        rows_b = api_decisions.outbox_rows_for(session, case_b)
        assert [row.case_seq for row in rows_a] == [1, 2, 3]
        assert [row.case_seq for row in rows_b] == [
            1,
            2,
            3,
        ], "case_seq continued from the other case, i.e. global ordering"
        assert [row.decision_seq for row in rows_a] == [1, 2, 3]
        assert [row.idempotency_key for row in rows_a] == [
            build_idempotency_key(run_id, case_a, seq) for seq in (1, 2, 3)
        ]

        due = claim_due(session, now=datetime.now(UTC) + timedelta(hours=1), batch=50)
        claimed: dict[str, list[int]] = {}
        for row in due:
            claimed.setdefault(str(row.case_id), []).append(int(row.case_seq))
        assert claimed == {case_a: [1], case_b: [1]}, (
            f"the drain claimed {claimed}; only the earliest unsent row of each case is "
            "claimable, which is what lets several workers drain in parallel without "
            "delivering a dismissal after its escalation"
        )
        session.rollback()

    ledger_body = assert_envelope(
        wh_client.get("/api/outbox", params={"case_id": case_a}, headers=headers).json(),
        where="GET /api/outbox",
    )["data"]
    assert ledger_body["depth"]["pending"] == 6, ledger_body["depth"]
    assert len(ledger_body["rows"]) == 3
    assert {row["case_seq"] for row in ledger_body["rows"]} == {1, 2, 3}
    assert all(row["status"] == "pending" for row in ledger_body["rows"])


def test_the_null_write_path_refuses_loudly_instead_of_answering_empty(
    null_client: TestClient,
) -> None:
    """Not a stub and not an empty list: a read that needs the decision store names the
    missing dependency (plan §14 forbids the unlabelled fallback)."""
    headers = analyst_headers(null_client)
    for path in ("/api/outbox", "/api/outbox/pending", "/api/jobs"):
        response = null_client.get(path, headers=headers)
        body = assert_problem(response, status=503, where=f"GET {path} with no warehouse")
        assert "Postgres" in body["detail"], body["detail"]
        assert body["retryable"] is True

    queue = null_client.get("/api/alerts", headers=headers)
    assert queue.status_code in (200, 503), queue.text
    if queue.status_code == 503:
        assert "no priced accounts" in queue.json()["detail"], queue.json()


def test_decision_and_outbox_money_columns_are_bigint_minor_units(
    warehouse: dict[str, Any],
) -> None:
    """`amount_minor` is int64 in storage as well as on the wire (DEV-005)."""
    from sqlalchemy import BigInteger

    for model, column in (
        (Decision, "exposure_minor"),
        (PolicyAllocation, "expected_value_minor"),
        (PolicySummary, "net_benefit_minor"),
    ):
        column_type = model.__table__.columns[column].type
        assert isinstance(
            column_type, BigInteger
        ), f"{model.__tablename__}.{column} is {column_type}; money is integer minor units"
    with Session(warehouse["engine"]) as session:
        row = session.execute(
            select(PolicyAllocation.expected_value_minor)
            .where(PolicyAllocation.run_id == warehouse["run_id"])
            .limit(1)
        ).scalar_one()
        assert isinstance(row, int) and not isinstance(row, bool)


# --- 5. webhook signature ------------------------------------------------------


@pytest.fixture()
def echo_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The compose echo receiver, in-process, with a test secret and a temp record file.

    `apps/api/echo.py` holds no signature logic of its own — it calls
    `oxbow.adapters.signing` — so driving it over HTTP proves the transport, which is the
    one thing a shared library cannot prove: that the header survives a real round trip and
    that the signed bytes are the bytes that arrive.
    """
    monkeypatch.setattr(echo_module, "SIGNING_SECRET", TEST_WEBHOOK_SECRET)
    monkeypatch.setattr(echo_module, "RECORD_PATH", tmp_path / "deliveries.jsonl")
    with TestClient(echo_module.app) as client:
        yield client


def _signed(body: bytes, *, secret: str = TEST_WEBHOOK_SECRET, at: int | None = None) -> str:
    header, _ = sign_body(body, secret, now=at)
    return header


def test_a_correctly_signed_delivery_is_accepted_and_recorded(echo_client: TestClient) -> None:
    body = json.dumps({"case_id": "0" * 26, "amount_minor": 123_400}).encode()
    response = echo_client.post(
        "/webhook",
        content=body,
        headers={
            SIGNATURE_HEADER: _signed(body),
            "Idempotency-Key": "idem-1",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["signature_verified"] is True
    assert response.json()["advisory_only"] is True
    recorded = echo_client.get("/deliveries").json()
    assert recorded["count"] == 1
    assert recorded["deliveries"][0]["body_sha256"] == hashlib.sha256(body).hexdigest()


def test_a_stale_timestamp_is_refused(echo_client: TestClient) -> None:
    body = json.dumps({"case_id": "0" * 26}).encode()
    stale_at = int(time.time()) - (REPLAY_WINDOW_SECONDS + 60)
    response = echo_client.post(
        "/webhook",
        content=body,
        headers={SIGNATURE_HEADER: _signed(body, at=stale_at), "Idempotency-Key": "idem-2"},
    )
    assert response.status_code == 401, response.text
    payload = response.json()
    assert payload["error_type"] == "clock_skew_or_replay", payload
    assert payload["skew_seconds"] >= REPLAY_WINDOW_SECONDS + 60
    assert payload["window_seconds"] == 300
    assert echo_client.get("/deliveries").json()["count"] == 0, "a refused delivery was recorded"


def test_a_wrong_signature_is_refused(echo_client: TestClient) -> None:
    body = json.dumps({"case_id": "0" * 26}).encode()
    wrong_secret = echo_client.post(
        "/webhook",
        content=body,
        headers={
            SIGNATURE_HEADER: _signed(body, secret="some-other-tenant-secret"),
            "Idempotency-Key": "idem-3",
        },
    )
    assert wrong_secret.status_code == 401, wrong_secret.text
    assert wrong_secret.json()["error_type"] == "bad_signature"

    zeroed = echo_client.post(
        "/webhook", content=body, headers={SIGNATURE_HEADER: f"t={int(time.time())},v1={'0' * 64}"}
    )
    assert zeroed.status_code == 401, zeroed.text
    assert echo_client.get("/deliveries").json()["count"] == 0


def test_a_signature_valid_for_a_different_body_is_refused(echo_client: TestClient) -> None:
    """The body is signed as raw bytes, so one changed byte invalidates the digest."""
    signed_body = json.dumps({"expected_value_minor": 10_000, "case_id": "0" * 26}).encode()
    forged_body = json.dumps({"expected_value_minor": 990_000, "case_id": "0" * 26}).encode()
    assert signed_body != forged_body
    response = echo_client.post(
        "/webhook",
        content=forged_body,
        headers={SIGNATURE_HEADER: _signed(signed_body), "Idempotency-Key": "idem-4"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["error_type"] == "bad_signature"
    assert echo_client.get("/deliveries").json()["count"] == 0


def test_a_malformed_header_is_400_and_a_missing_one_is_401(echo_client: TestClient) -> None:
    body = b"{}"
    for value in ("v1=" + "a" * 64, "t=abc,v1=deadbeef", "t=,v1=deadbeef", "garbage", ""):
        response = echo_client.post("/webhook", content=body, headers={SIGNATURE_HEADER: value})
        assert response.status_code == 400, f"{value!r} -> {response.status_code} {response.text}"
    absent = echo_client.post("/webhook", content=body)
    assert absent.status_code == 401, absent.text


def test_the_scheme_is_hmac_sha256_over_the_timestamp_and_the_raw_body() -> None:
    """Recomputed independently, not compared against the library's own output."""
    body = b'{"case_id":"00000000000000000000000000","exposure_minor":4200}'
    at = 1_800_000_000
    expected = hmac.new(
        TEST_WEBHOOK_SECRET.encode("utf-8"),
        f"{at}".encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    header, stamp = sign_body(body, TEST_WEBHOOK_SECRET, now=at)
    assert stamp == at
    assert header == f"t={at},v1={expected}", header
    assert len(expected) == 64, "a v1 digest is SHA-256, so 64 hex characters"
    assert compute_signature(str(at), body, TEST_WEBHOOK_SECRET) == expected
    assert verify_signature(header, body, TEST_WEBHOOK_SECRET, now=at) == at


def test_the_replay_window_is_300_seconds_and_the_compare_is_constant_time() -> None:
    body = b'{"case_id":"00000000000000000000000000"}'
    at = 1_800_000_100
    header = _signed(body, at=at)
    assert REPLAY_WINDOW_SECONDS == 300
    # Exactly on the boundary in both directions is inside; one second past is not.
    assert verify_signature(header, body, TEST_WEBHOOK_SECRET, now=at + 300) == at
    assert verify_signature(header, body, TEST_WEBHOOK_SECRET, now=at - 300) == at
    with pytest.raises(Exception, match="replay window"):
        verify_signature(header, body, TEST_WEBHOOK_SECRET, now=at + 301)
    with pytest.raises(Exception, match="replay window"):
        verify_signature(header, body, TEST_WEBHOOK_SECRET, now=at - 301)

    source = inspect.getsource(verify_signature)
    assert "compare_digest" in source, (
        "the digest comparison is no longer constant-time; a timing oracle in a webhook "
        "receiver is a forgery search, not a theoretical concern"
    )
    assert (
        "==" not in source[source.index("expected =") :]
    ), "the expected digest is being compared with `==` before the constant-time compare"


def test_sender_and_receiver_agree_over_a_retried_delivery(echo_client: TestClient) -> None:
    """Two attempts of the same bytes are two records of one fact, not two facts."""
    payload = {"case_id": "0" * 26, "schema_version": "1", "amount_minor": 500_00}
    raw = json.dumps(payload, sort_keys=True).encode()
    for attempt in range(2):
        response = echo_client.post(
            "/webhook",
            content=raw,
            headers={
                SIGNATURE_HEADER: _signed(raw),
                "Idempotency-Key": f"retry-{attempt}",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["idempotency_key"] == f"retry-{attempt}"
    recorded = echo_client.get("/deliveries").json()["deliveries"]
    assert len(recorded) == 2
    assert (
        len({item["body_sha256"] for item in recorded}) == 1
    ), "the same bytes produced two digests, so the signed body is not the body that arrived"
    assert all(item["signature_verified"] for item in recorded)


def test_the_outbox_uses_the_same_implementation_the_receiver_verifies_against() -> None:
    """One scheme, both sides of the wire.

    The outbound sinks must sign through `oxbow.adapters.signing`, and the echo receiver
    must not have grown a second HMAC of its own — that duplicate is what drifted once
    (`apps/api/echo.py`'s own docstring records it), and a drifted signature check is a
    check that passes for the wrong reason.
    """
    from oxbow.adapters.webhook import sinks as webhook_sinks

    sender = inspect.getsource(webhook_sinks)
    assert "sign_body(" in sender, (
        "the outbound sinks no longer sign through oxbow.adapters.signing, so sender and "
        "receiver can disagree by construction"
    )
    assert "hmac.new" not in sender, "the sender has grown its own HMAC implementation"
    assert "hmac.new" not in inspect.getsource(
        echo_module.webhook
    ), "the echo receiver has grown its own HMAC, which is the duplicate that drifted once"
    assert SIGNATURE_HEADER in sender or "SIGNATURE_HEADER" in sender, (
        "the outbound sinks must send the signature under the one agreed header name. The "
        "constant is the better spelling and is what the sender now uses, so accept either "
        "the name or a reference to the constant that defines it - asserting the literal "
        "string pushed the sender toward hardcoding a header name that has to stay in "
        "step with oxbow.adapters.signing, which is the drift this test exists to prevent."
    )
    # Accepting either spelling above is not yet a check: a module that imported the
    # constant and posted under some other name would satisfy it. So pin the binding
    # itself — the sender's header must *be* the receiver's header — and keep the literal
    # out, which is the duplicate that drifted once.
    assert webhook_sinks.SIGNATURE_HEADER == SIGNATURE_HEADER, (
        "the sender's signature header no longer resolves to "
        "oxbow.adapters.signing.SIGNATURE_HEADER, so sender and receiver can name different "
        "headers and each verify its own"
    )
    assert f'"{SIGNATURE_HEADER}"' not in sender, (
        "the sender hard-codes the header name instead of importing the shared constant; "
        "a duplicated literal is exactly what drifts"
    )


# --- 6. the contract areas sections 1-5 do not reach ----------------------------
#
# Plan §13/§14 name behaviours the tests above leave unasserted: what happens to a
# delivery the consumer *permanently* refuses and whether the per-case queue is released
# behind one, whether an OIDC token is verified against the issuer's own key rather than
# merely parsed, and whether the audit digest can be walked across a field boundary by
# putting a "|" in a value. Each is driven through the real code below.


def test_a_refused_delivery_is_dead_lettered_and_releases_the_cases_next_row(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    """Dead-letter, the retry ladder, and the ordering rule's own release condition.

    Three properties in one pass, because they only mean anything together: a 4xx
    dead-letters on the *first* attempt (putting a schema refusal on a five-attempt ladder
    is a hammer pointed at someone's endpoint, plan §18); a transient failure walks the
    jittered ladder and is buried at the fifth; and a dead predecessor stops blocking its
    case, because a row that will never be sent must not wedge everything behind it for
    ever. The ledger has to show all of it — a delivery that never happened is a fact the
    product carries, not a line in a log nobody reads.
    """
    import httpx
    from api.outbox import drain_once

    from oxbow.adapters.retry import MAX_ATTEMPTS
    from oxbow.adapters.webhook.sinks import WebhookCaseSink

    headers = analyst_headers(wh_client)
    run_id, low = warehouse["run_id"], warehouse["low"]
    case_a = _open_case(wh_client, headers, run_id, low[0])
    case_b = _open_case(wh_client, headers, run_id, low[1])
    for case_id, version in ((case_a, 1), (case_a, 2), (case_b, 1)):
        posted = wh_client.post(
            f"/api/cases/{case_id}/decisions",
            json={
                "action": "review",
                "reason": f"queue probe {version}",
                "expected_version": version,
            },
            headers=headers,
        )
        assert posted.status_code == 200, posted.text

    container = warehouse["container"]
    original_factory = container.case_sink_factory
    sink_url = "http://consumer.invalid/webhook"

    def refused(request: httpx.Request) -> httpx.Response:
        # A 422 is the consumer saying the payload is wrong, not that it is busy.
        return httpx.Response(422, json={"detail": "schema violation: unknown field"})

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("consumer is down", request=request)

    def use(handler: Any) -> None:
        # The real sender, with only the far end of the wire substituted: `HttpSink`
        # exposes `client` precisely so the request path, the signing and the status
        # classification below are the ones production runs.
        client = httpx.Client(transport=httpx.MockTransport(handler))
        container.case_sink_factory = lambda: WebhookCaseSink(
            url=sink_url, secret=TEST_WEBHOOK_SECRET, client=client
        )

    try:
        use(refused)
        started = datetime.now(UTC)
        with Session(warehouse["engine"]) as session:
            report = drain_once(container, now=started, batch=50, session=session)
            # `rejected_permanently`, not `dead`: the consumer's own refusal is a
            # different fact from running out of attempts, and the report keeps them
            # apart so an operator can tell "our payload is wrong" from "they are down".
            assert report.claimed == 2, report.as_dict()
            assert (report.rejected_permanently, report.sent, report.dead) == (2, 0, 0)
            assert report.retrying == 0, "a 4xx must not be put back on the retry ladder"
            rows = {
                int(row.case_seq): row for row in api_decisions.outbox_rows_for(session, case_a)
            }
            assert rows[1].status == "dead"
            assert int(rows[1].attempts) == 1, "a permanent refusal was retried before buried"
            assert rows[1].dead_at == started
            assert rows[1].sent_at is None
            assert "422" in str(rows[1].last_error), rows[1].last_error
            assert len(rows[1].attempt_log) == 1
            # case_a's second row was never even claimed: its predecessor was unsent.
            assert rows[2].status == "pending"
            assert int(rows[2].attempts) == 0
            session.commit()

        with Session(warehouse["engine"]) as session:
            due = claim_due(session, now=datetime.now(UTC), batch=50)
            assert {(str(row.case_id), int(row.case_seq)) for row in due} == {(case_a, 2)}, (
                "a dead row must not block its case for ever, and a case whose head of "
                "line is still pending must not overtake it"
            )
            session.rollback()

        use(unavailable)
        started = datetime.now(UTC)
        with Session(warehouse["engine"]) as session:
            report = drain_once(container, now=started, batch=50, session=session)
            assert (report.claimed, report.retrying) == (1, 1), report.as_dict()
            row = api_decisions.outbox_rows_for(session, case_a)[1]
            assert row.status == "pending" and int(row.attempts) == 1
            outcome = report.outcomes[0]
            assert outcome.jitter_delay_seconds is not None
            assert 0.0 <= outcome.jitter_delay_seconds <= 1.0, (
                f"attempt one of the ladder must land inside the documented 1s rung, got "
                f"{outcome.jitter_delay_seconds}"
            )
            assert row.next_attempt_at == started + timedelta(
                seconds=outcome.jitter_delay_seconds
            ), "the worker must store the next attempt time, not sleep it"
            assert "consumer is down" in str(row.last_error)
            session.commit()

        with Session(warehouse["engine"]) as session:
            row = api_decisions.outbox_rows_for(session, case_a)[1]
            row.attempts = MAX_ATTEMPTS - 1
            row.next_attempt_at = datetime.now(UTC) - timedelta(minutes=5)
            session.commit()

        report = drain_once(container, batch=50)
        assert report.dead == 1, report.as_dict()
        with Session(warehouse["engine"]) as session:
            row = api_decisions.outbox_rows_for(session, case_a)[1]
            assert row.status == "dead"
            assert int(row.attempts) == MAX_ATTEMPTS, row.attempts
            assert row.dead_at is not None and row.sent_at is None
            assert len(row.attempt_log) == 2, row.attempt_log

        dead = assert_envelope(
            wh_client.get("/api/outbox", params={"status": "dead"}, headers=headers).json(),
            where="GET /api/outbox?status=dead",
        )["data"]
        assert {str(row["status"]) for row in dead["rows"]} == {"dead"}
        assert all(row["sent_at"] is None for row in dead["rows"]), "a dead row is not a sent one"
        # Three buried promises, not two: case_b's single row was refused in the same first
        # pass as case_a's head of line, and case_a's second row only died later, on the
        # ladder. The ledger is the only place that story survives, so it has to be exact.
        assert {(str(row["case_id"]), int(row["case_seq"])) for row in dead["rows"]} == {
            (case_a, 1),
            (case_a, 2),
            (case_b, 1),
        }, dead["rows"]
        depth = assert_envelope(
            wh_client.get("/api/outbox", headers=headers).json(), where="GET /api/outbox"
        )["data"]["depth"]
        assert depth["dead"] == 3 and depth["pending"] == 0 and depth["sent"] == 0, depth
    finally:
        container.case_sink_factory = original_factory


@contextlib.contextmanager
def _oidc_client(
    jwks_url: str, document: dict[str, Any]
) -> Iterator[tuple[TestClient, Any, list[str]]]:
    """The app over a JWKS the test itself signs, with the fetch counted."""
    import httpx
    from api.security import JwksCache

    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, json=document)

    container = build_container()
    container.jwks_cache = JwksCache(
        jwks_url, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    app = create_app()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # The lifespan builds its own container from the ambient environment; this is
            # the one holding the key set under test, so it goes in after startup.
            app.state.container = container
            yield client, container, fetched
    finally:
        container.close()


def test_an_oidc_token_is_verified_against_the_issuers_own_key_and_nothing_else() -> None:
    """OIDC verification end to end, against a key set this test generated.

    `test_alg_none_and_the_algorithm_confusion_attack_are_refused` calls `decode_token`
    directly, which leaves the deployment's half untested: the container's key cache, the
    configured issuer and audience, Keycloak's own `realm_access.roles` claim, and the
    refusal of the local fallback the moment an issuer is authoritative. The RSA pair is
    generated rather than read from a fixture so that "verifies" cannot quietly mean
    "matches a captured byte string".
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    jwks_url = f"{ISSUER}/protocol/openid-connect/certs"
    mp = pytest.MonkeyPatch()
    _configure_env(
        mp,
        OXBOW_OIDC_ISSUER=ISSUER,
        OXBOW_OIDC_JWKS_URL=jwks_url,
        OXBOW_LOCAL_JWT_ENABLED="false",
        OXBOW_LOCAL_JWT_SECRET="",
    )

    def new_key() -> Any:
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    signer, stranger, other = new_key(), new_key(), new_key()

    def jwk(kid: str, private: Any) -> dict[str, str]:
        numbers = private.public_key().public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": kid,
            "n": b64url_encode(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": b64url_encode(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }

    served = "p7-serving-key"
    document = {"keys": [jwk(served, signer), jwk("p7-other-key", other)]}

    def mint(
        *,
        private: Any = signer,
        kid: str = served,
        issuer: str = ISSUER,
        audience: str = "oxbow-web",
        roles: list[str] | None = None,
        realm_roles: list[str] | None = None,
    ) -> str:
        header = b64url_encode(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}).encode())
        claims: dict[str, Any] = {
            "iss": issuer,
            "sub": "p7-oidc-analyst",
            "aud": audience,
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
            "name": "P7 OIDC Analyst",
        }
        if roles is None and realm_roles is None:
            # A token with neither claim is the `no_role` case, not the accepted one, so
            # the default carries this project's own role claim.
            roles = ["analyst"]
        if roles is not None:
            claims["oxbow_roles"] = roles
        if realm_roles is not None:
            claims["realm_access"] = {"roles": realm_roles}
        payload = b64url_encode(json.dumps(claims).encode())
        signature = b64url_encode(
            private.sign(f"{header}.{payload}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        )
        return f"{header}.{payload}.{signature}"

    stack = contextlib.ExitStack()
    try:
        client, container, fetched = stack.enter_context(_oidc_client(jwks_url, document))
        minted = client.post(
            "/api/auth/demo-token", json={"subject": ANALYST, "roles": ["analyst"]}
        )
        body = assert_problem(
            minted, status=403, where="a demo token while an issuer is authoritative"
        )
        assert "issuer" in body["detail"] or "OIDC" in body["detail"], body["detail"]

        fallback = assert_problem(
            client.get(
                "/api/me",
                headers={
                    "Authorization": f"Bearer {mint_local_token(subject=ANALYST, roles=['analyst'], secret=TEST_JWT_SECRET)}"
                },
            ),
            status=401,
            where="a local HS256 token while the fallback is disabled",
        )
        assert "refused" in fallback["detail"] or "verify" in fallback["detail"], fallback

        accepted = client.get("/api/me", headers={"Authorization": f"Bearer {mint()}"})
        assert accepted.status_code == 200, accepted.text
        data = assert_envelope(accepted.json(), where="GET /api/me with an RS256 token")["data"]
        assert data["source"] == "oidc", data
        assert data["issuer"] == ISSUER, data
        assert data["roles"] == ["analyst"], data
        assert data["subject"] == "p7-oidc-analyst", data
        assert fetched == [jwks_url], fetched

        realm = client.get(
            "/api/me", headers={"Authorization": f"Bearer {mint(realm_roles=['reviewer'])}"}
        )
        assert realm.status_code == 200, realm.text
        assert realm.json()["data"]["roles"] == ["reviewer"], (
            "an API that read only its own `oxbow_roles` claim would reject a correctly "
            "configured Keycloak user"
        )

        wrong_key = client.get(
            "/api/me",
            headers={"Authorization": f"Bearer {mint(private=stranger, kid=served)}"},
        )
        assert_problem(wrong_key, status=401, where="a token signed by a key not in the set")
        assert "verif" in wrong_key.json()["detail"], wrong_key.json()["detail"]

        unknown_kid = client.get("/api/me", headers={"Authorization": f"Bearer {mint(kid='nope')}"})
        assert_problem(unknown_kid, status=401, where="a kid absent from a two-key set")
        assert "kid" in unknown_kid.json()["detail"], unknown_kid.json()["detail"]

        other_audience = client.get(
            "/api/me", headers={"Authorization": f"Bearer {mint(audience='some-other-api')}"}
        )
        assert_problem(other_audience, status=401, where="a token minted for another audience")
        assert "audience" in other_audience.json()["detail"], other_audience.json()["detail"]

        foreign = client.get(
            "/api/me",
            headers={"Authorization": f"Bearer {mint(issuer='https://evil.example')}"},
        )
        assert_problem(foreign, status=401, where="a token claiming a different issuer")

        hs_on_rs256 = client.get(
            "/api/me",
            headers={
                "Authorization": f"Bearer {mint_local_token(subject=ANALYST, roles=['analyst'], secret='not-the-real-one')}"
            },
        )
        assert_problem(hs_on_rs256, status=401, where="an HMAC token against an RS256 issuer")

        no_role = client.get(
            "/api/me", headers={"Authorization": f"Bearer {mint(roles=['observer'])}"}
        )
        assert_problem(no_role, status=403, where="a verified token with no OXBOW role")
    finally:
        stack.close()
        mp.undo()
        reset_settings_cache()


def test_a_pipe_in_a_signed_field_cannot_move_the_digest_boundary(
    wh_client: TestClient, warehouse: dict[str, Any]
) -> None:
    """STATE.md item 14, checked where the chain is actually written.

    The retired scheme joined
    ``HASH_VERSION|seq|occurred_at|actor_id|subject|action|canonical_json(payload)|prev_hash``
    with ``"|"``. ``actor_id`` arrives from an identity provider and is free text, so
    ``("p7|operator", "case:X")`` and ``("p7", "operator|case:X")`` hash identically: a
    decision could be re-attributed across subjects without breaking its own digest.
    `tests/unit/test_audit_chain.py` proves the array scheme closes that hole; what this
    proves is that the API's write path goes through it — that a real decision whose
    reason and actor carry pipes is digested, chained, verified, and detectably altered
    by a rewrite that only moves a pipe.
    """
    from oxbow.audit.chain import GENESIS_HASH, canonical_json, compute_row_hash

    headers = analyst_headers(wh_client)
    run_id, low = warehouse["run_id"], warehouse["low"]
    hostile = 'dismissed|reason moved|{"actor_id":"p7-operator-reviewer"}'
    case_id = _open_case(wh_client, headers, run_id, low[0])
    posted = wh_client.post(
        f"/api/cases/{case_id}/decisions",
        json={"action": "dismiss", "reason": hostile, "expected_version": 1},
        headers=headers,
    )
    assert posted.status_code == 200, posted.text
    assert_envelope(posted.json(), where="a decision whose reason carries pipes")

    with Session(warehouse["engine"]) as session:
        rows = api_decisions.decision_chain_rows(session)
    assert len(rows) == 1, [row.seq for row in rows]
    row = rows[0]
    assert row.payload["reason"] == hostile, "the stored reason was reworded in transit"
    assert verify_chain(rows).ok
    assert row.row_hash == compute_row_hash(row)
    assert row.prev_hash == GENESIS_HASH

    retired = "|".join(
        [
            "oxbow-audit-v1",
            str(row.seq),
            row.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            row.actor_id,
            row.subject,
            row.action,
            canonical_json(row.payload),
            row.prev_hash,
        ]
    )
    assert hashlib.sha256(retired.encode()).hexdigest() != row.row_hash, (
        "the stored digest is the retired pipe-join again: the delimiter weakness "
        "STATE.md item 14 records has come back"
    )

    def split(actor_id: str, subject: str) -> PendingChainRow:
        return PendingChainRow(
            seq=row.seq,
            occurred_at=row.occurred_at,
            actor_id=actor_id,
            subject=subject,
            action=row.action,
            payload=row.payload,
            prev_hash=row.prev_hash,
            row_hash=row.row_hash,
        )

    # The two splits that used to be indistinguishable: the same joined bytes, reached
    # from an actor carrying a trailing field and a subject carrying the remainder.
    left = split("p7|operator", f"case:{'0' * 26}")
    right = split("p7", f"operator|case:{'0' * 26}")
    assert left.actor_id + "|" + left.subject == right.actor_id + "|" + right.subject, (
        "this fixture stopped straddling the boundary, which would make the comparison "
        "below vacuous"
    )
    assert compute_row_hash(left) != compute_row_hash(right), (
        "two different field splits hash identically, so a re-attribution of a signed "
        "decision would verify"
    )

    # And the free text really does reach the digest through the API: `actor_id` is the
    # identity provider's `sub`, so a pipe in an operator's subject is the ordinary case,
    # not a synthetic one. The chain must carry it and still verify.
    pipey = "p7-operator|rotated"
    headers_pipey = {
        "Authorization": f"Bearer {token_for(wh_client, pipey, ['analyst'])}",
    }
    case_pipey = _open_case(wh_client, headers_pipey, run_id, low[1])
    second = wh_client.post(
        f"/api/cases/{case_pipey}/decisions",
        json={"action": "dismiss", "reason": "cleared|second probe", "expected_version": 1},
        headers=headers_pipey,
    )
    assert second.status_code == 200, second.text
    with Session(warehouse["engine"]) as session:
        chained = api_decisions.decision_chain_rows(session)
    assert [row.actor_id for row in chained[1:]] == [pipey], chained
    assert chained[1].payload["reason"] == "cleared|second probe"
    assert chained[0].row_hash == row.row_hash
    assert chained[1].prev_hash == chained[0].row_hash, "the second link does not join the first"
    verification = verify_chain(chained)
    assert verification.ok and verification.rows_checked == 2, verification.first_broken


def test_two_unrelated_cases_can_be_decided_at_the_same_instant(
    warehouse: dict[str, Any],
) -> None:
    """The other half of the chain race: two writers on DIFFERENT cases must both land.

    `test_concurrent_append_gives_one_success_and_one_409` forces REPEATABLE READ to
    exercise the unique-constraint backstop, which is a real property but not the one plan
    §13 is about. §13's "one success and one 409" is the four-eyes rule for two analysts on
    the SAME case; it is enforced by the case row lock and `expected_version`, not by the
    chain. Before the advisory lock, the chain was global and its tip was read unlocked, so
    two decisions on unrelated cases both computed the same link and one was refused with a
    409 blaming a collision the server had caused — a per-case integrity rule paid for as a
    per-database throughput limit.

    What is asserted is the consequence of the lock rather than its existence: the second
    writer's `chain_seq` is exactly one above the first, which is only possible if it read
    the tip AFTER the first committed — i.e. it waited, instead of racing and losing.

    The overlap here is a real thread, so the window is narrow; the mutation is the proof.
    Removing `chain_append_lock` from `decisions.py` turns this red with a Conflict, and
    that is the assertion doing its job, not the timing.
    """
    container = warehouse["container"]
    read_model = container.read_model
    run_id, low = warehouse["run_id"], warehouse["low"]

    loader = container.new_session()
    case_ids = [
        str(api_decisions.open_case(loader, read_model, run_id=run_id, account_key=key).case_id)
        for key in (low[0], low[1])
    ]
    loader.commit()
    loader.close()

    def principal(subject: str) -> Principal:
        return Principal(
            subject=subject, roles=("analyst",), display_name=subject, source="local-jwt"
        )

    def submit(session: Session, case_id: str, subject: str, trace: str) -> dict[str, Any]:
        case = session.get(api_decisions.Case, case_id)
        assert case is not None
        return api_decisions.record_decision(
            session,
            read_model,
            container.audit_sink(session),
            container.economics,
            case=case,
            write=api_decisions.DecisionWrite(
                action="escalate",
                reason="cross-case concurrency candidate",
                expected_version=int(case.version),
                reversal_of_decision_id=None,
                principal=principal(subject),
                trace_id=trace,
            ),
        )

    holder = container.new_session()
    waiter = container.new_session()
    outcome: dict[str, Any] = {}

    def wait_and_write() -> None:
        # The thread must not let an exception escape: a refused writer has to be reported
        # through the assertion below, not through a lost thread that makes the test pass by
        # never observing the failure.
        try:
            outcome["receipt"] = submit(
                waiter, case_ids[1], "p7-operator-waiter", "p7-crosscase-waiter"
            )
            waiter.commit()
            outcome["status"] = "ok"
        except BaseException as exc:
            waiter.rollback()
            outcome["status"] = type(exc).__name__
            outcome["error"] = exc

    try:
        first = submit(holder, case_ids[0], "p7-operator-holder", "p7-crosscase-holder")
        # The holder's transaction is still open, so it still owns the chain lock. Starting
        # the writer now is what asks the question: does it wait, or does it collide?
        thread = threading.Thread(target=wait_and_write, name="p7-crosscase-waiter")
        thread.start()
        # Wait until the second writer is demonstrably parked on the advisory lock.
        # `pg_locks` is the observable; a sleep would only prove the machine was slow,
        # and a test that passes because nothing happened yet is worse than no test.
        parked = False
        probe = container.new_session()
        try:
            for _ in range(200):
                waiting = probe.execute(
                    text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND NOT granted"
                    )
                ).scalar_one()
                if int(waiting) > 0:
                    parked = True
                    break
                time.sleep(0.05)
        finally:
            probe.close()
        assert parked, (
            "the second writer never blocked on the chain lock, so this test did not "
            "create the overlap it claims to test — it proved nothing about ordering"
        )
        holder.commit()
        thread.join(timeout=30)
        assert not thread.is_alive(), (
            "the second writer is still parked after the holder committed: the lock is "
            "not being released with the transaction"
        )

        assert outcome["status"] == "ok", (
            "the second writer on an UNRELATED case was refused as "
            + str(outcome.get("status"))
            + ": "
            + repr(outcome.get("error"))
        )
        seq_a = int(first["chain_seq"])
        seq_b = int(outcome["receipt"]["chain_seq"])
        assert seq_b == seq_a + 1 or seq_a == seq_b + 1, (
            f"the two links are {seq_a} and {seq_b}, which is not a contiguous pair — the "
            "second writer read a stale tip instead of waiting for the first to commit"
        )

        # Both links must exist in the table, not just in the receipts the callers
        # were handed: a refused writer that was quietly swallowed would still have
        # returned a receipt if the exception had been caught in the wrong place.
        both = container.new_session()
        try:
            landed = both.execute(
                text("SELECT count(*) FROM decision WHERE trace_id IN (:first, :second)"),
                {"first": "p7-crosscase-holder", "second": "p7-crosscase-waiter"},
            ).scalar_one()
            assert (
                int(landed) == 2
            ), f"the chain receipts said two decisions, the table holds {landed}"
        finally:
            both.close()
    finally:
        holder.close()
        waiter.close()
