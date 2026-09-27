"""P7 outbox drain — two audit findings, one gate each, driven against the real code.

Finding 1 (dedupe and the leak). ``attempt_delivery`` used to resolve its sink through
``container.case_sink_factory()`` *per row*, and every production factory in
``api/deps.py`` builds a NEW adapter per call — so ``HttpSink._delivered`` was empty at
every ``has_delivered`` check (the within-process guard could never fire), and each
construction opened an ``httpx.Client`` that no caller ever closed. The notify row made
it worse: its stored key is ``notify_idempotency_key(...)`` while the sink recorded the
delivery under ``notification_id`` (the decision id) — two key spaces that never meet.

Finding 2 (the claim). ``in_flight`` is in ``CLAIMABLE_STATUSES`` and staleness logic
is built around it, but nothing ever wrote it: the ``FOR UPDATE`` lock was held until
the whole pass committed — across up to twenty HTTP posts — and a worker SIGKILLed
mid-POST left ``attempts`` untouched, so the five-attempt ladder was unreachable for a
crash-looping consumer. The fix claims each row (``in_flight``, ``attempts + 1``,
``next_attempt_at``) and commits the claim BEFORE the first packet leaves the process,
and buries an exhausted row at claim time instead of POSTing a sixth time.

Both gates drive the production sink classes with ``httpx.MockTransport`` substituted
at the far end of the wire (the sender, the signing and the status classification are
the ones production runs), or a recording fake — never a sleep, never a real network.
The scratch Postgres is compose's on host port 5433; 5432 is a different server and is
never a candidate here.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import api.deps as api_deps  # noqa: E402
from api import decisions as api_decisions  # noqa: E402
from api.deps import build_container  # noqa: E402
from api.outbox import (  # noqa: E402
    DRAIN_BATCH_DEFAULT,
    IN_FLIGHT_STALE_SECONDS,
    drain_once,
)
from api.settings import reset_settings_cache  # noqa: E402

from oxbow.adapters.retry import MAX_ATTEMPTS  # noqa: E402
from oxbow.adapters.signing import (  # noqa: E402
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    verify_signature,
)
from oxbow.adapters.warehouse.models import Case, OutboxMessage, Run  # noqa: E402
from oxbow.adapters.webhook.sinks import WebhookCaseSink, WebhookNotifySink  # noqa: E402
from oxbow.ports.case_sink import (  # noqa: E402
    CalibrationReading,
    CaseBundle,
    DecisionRecord,
    EconomicsBlock,
    ScoreBlock,
)

# Test-only secrets, explicit and synthetic; the repository's own RUN_SALT is never
# read and no secret value is ever printed.
TEST_DB_NAME = f"oxbow_p7drain_test_{os.getpid()}_{secrets.token_hex(3)}"
TEST_WEBHOOK_SECRET = "p7-outbox-drain-test-webhook-secret"
PASSED_AT = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _admin_url() -> str:
    """Compose Postgres on 5433 — never the native server on 5432."""
    user = os.environ.get("POSTGRES_USER", "")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    port = os.environ.get("POSTGRES_PORT", "5433")
    dbname = os.environ.get("POSTGRES_DB", "oxbow")
    if not user or not password:
        pytest.fail(
            "POSTGRES_USER/POSTGRES_PASSWORD are unset. Source the compose environment "
            f"(set -a; . ./.env; set +a) before running this module; Postgres is on port {port}."
        )
    return f"postgresql://{user}:{password}@127.0.0.1:{port}/{dbname}"


def _provision_database() -> str:
    import psycopg

    admin = _admin_url()
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    except Exception as exc:
        pytest.fail(
            f"no Postgres reachable at {_admin_url().split('@')[-1]!r} for a scratch "
            f"database: {type(exc).__name__}: {str(exc).splitlines()[0][:140]}"
        )
    parsed = urlparse(admin)
    return urlunparse(parsed._replace(path=f"/{TEST_DB_NAME}")).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )


def _migrate(database_url: str) -> None:
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


def _case_bundle(run_id: str, case_id: str, account_key: str, decision_seq: int) -> CaseBundle:
    """A real, self-describing bundle — the payload a decision transaction would store."""
    decided_at = PASSED_AT + timedelta(days=decision_seq)
    return CaseBundle(
        run_id=run_id,
        case_id=case_id,
        account_key=account_key,
        decided_at=decided_at,
        decision=DecisionRecord(
            decision_seq=decision_seq,
            action="escalate",
            reason="drain gate probe",
            actor_id="drain-test-operator",
            decided_at=decided_at,
        ),
        score=ScoreBlock(
            fused_score=0.82,
            band="high",
            scorecard_points=41,
            reason_codes=("fast_outflow",),
            calibration=CalibrationReading(band="high", observed_rate=0.31, n=120),
            model_version="drain-test-model-1",
        ),
        economics=EconomicsBlock(
            currency="USD",
            exposure_minor=12_345_67,
            expected_value_minor=1_000_00,
            recovery_rate=0.85,
            analyst_cost_minor=5_000,
            friction_cost_minor=2_50,
            assumptions={"recovery_rate": 0.85, "cost_basis": "config/economics.yaml"},
        ),
        evidence_refs=(),
        transaction_ids=(),
        provenance={"fixture": "p7_outbox_drain"},
    )


def _notification_payload(*, bundle: CaseBundle, decision_id: str) -> dict[str, Any]:
    """The notify row's stored payload, exactly as ``decisions.notification_payload`` writes
    it: its own ``notification_id`` (the decision id), which is what the sink guard used
    to key on — the key the row itself was promised under is something else entirely."""
    return {
        "schema_version": "1.0",
        "notification_id": decision_id,
        "run_id": bundle.run_id,
        "case_id": bundle.case_id,
        "account_key": bundle.account_key,
        "severity": "critical",
        "title": "OXBOW: escalate on drain-test account",
        "body": "drain gate notification",
        "requires_four_eyes": False,
        "expected_value_minor": 1_000_00,
        "currency": "USD",
        "assumptions": {"recovery_rate": 0.85, "cost_basis": "config/economics.yaml"},
        "model_version": "drain-test-model-1",
    }


def _insert_outbox(
    session: Session,
    *,
    run_id: str,
    case_id: str,
    decision_seq: int,
    case_seq: int,
    sink_id: str,
    payload: dict[str, Any],
    idempotency_key: str,
    status: str = "pending",
    sent_at: datetime | None = None,
) -> OutboxMessage:
    row = OutboxMessage(
        idempotency_key=idempotency_key,
        run_id=run_id,
        case_id=case_id,
        decision_seq=decision_seq,
        case_seq=case_seq,
        sink_id=sink_id,
        schema_version=str(payload["schema_version"]),
        payload=payload,
        status=status,
        attempts=1 if status == "sent" else 0,
        next_attempt_at=PASSED_AT,
        sent_at=sent_at,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture(scope="module")
def drain_env() -> Iterator[dict[str, Any]]:
    """A migrated scratch database plus a production-shaped postgres container."""
    mp = pytest.MonkeyPatch()
    url = _provision_database()
    for name, value in {
        "RUN_SALT": "p7-outbox-drain-test-salt-not-the-real-one",
        "OXBOW_SEED": "1337",
        "OXBOW_LOCAL_JWT_ENABLED": "true",
        "WEBHOOK_SIGNING_SECRET": TEST_WEBHOOK_SECRET,
        "WEBHOOK_ENDPOINT": "http://consumer.invalid/webhook",
        "OXBOW_OIDC_ISSUER": "",
        "OXBOW_OIDC_JWKS_URL": "",
        "OXBOW_S3_ENDPOINT_URL": "",
        "OXBOW_SLACK_WEBHOOK_URL": "",
        "OXBOW_REPO_ROOT": str(REPO_ROOT),
        "DATABASE_URL": url,
        "OXBOW_WAREHOUSE": "postgres",
    }.items():
        mp.setenv(name, value)
    _migrate(url)
    reset_settings_cache()

    engine = create_engine(url, future=True)
    from oxbow.adapters.warehouse.postgres import new_run_id

    run_id = new_run_id()
    with Session(engine) as session:
        # ``guard_run_mutable`` refuses child rows (cases, outbox) on a run whose state
        # is complete or superseded, so the seeded run stays running for this module.
        session.add(
            Run(
                run_id=run_id,
                state="running",
                seed=1337,
                timezone="UTC",
                provenance="fixture",
                config_hash="0" * 64,
                model_version="drain-test-model-1",
                created_at=PASSED_AT,
            )
        )
        session.flush()
        cases: dict[str, str] = {}
        for name in ("a", "b", "c", "d"):
            case_id = new_run_id()
            cases[name] = case_id
            session.add(
                Case(
                    case_id=case_id,
                    run_id=run_id,
                    account_key=f"ACCTDRAIN{sum(map(int, name.encode())):03d}"[:12],
                    status="open",
                    opened_at=PASSED_AT,
                    updated_at=PASSED_AT,
                )
            )
        session.commit()

    container = build_container()
    assert container.backend == "postgres", (
        f"the fixture asked for OXBOW_WAREHOUSE=postgres and got {container.backend}; the "
        "production sink factories this module gates only exist on the write path"
    )
    case_sink_probe = container.case_sink_factory()
    notify_sink_probe = container.notify_sink_factory()
    assert isinstance(case_sink_probe, WebhookCaseSink), (
        "the fixture must exercise the real deps.py wiring: a webhook endpoint plus a "
        "signing secret on the postgres backend selects WebhookCaseSink"
    )
    assert isinstance(notify_sink_probe, WebhookNotifySink), (
        "with no Slack URL configured the notify factory must be WebhookNotifySink — "
        "that is the sink whose delivered-key space finding 1 puts back in step"
    )
    case_sink_probe.close()
    notify_sink_probe.close()
    yield {"url": url, "engine": engine, "container": container, "run_id": run_id, **cases}
    container.close()
    engine.dispose()
    mp.undo()
    reset_settings_cache()
    import psycopg

    with psycopg.connect(_admin_url(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")


# --- finding 1: the within-process dedupe and the leaked clients ----------------


def test_one_drain_pass_builds_each_sink_once_closes_every_client_and_dedupes_by_row_key(
    drain_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opened clients == closed clients, and the guard holds the row's ACTUAL key.

    Four pending rows are due: two case rows (separate cases), one notify row whose
    case predecessor is already sent, and — as the leak's measuring rod — a third case
    row. One pass must therefore construct two sinks (one per ``sink_id``), close both,
    and record every accepted delivery under the row's own ``idempotency_key`` so the
    within-process guard can actually fire. Before the fix the factory ran per row:
    three clients opened, none closed, and the notify row's key never entered the map
    the check reads.
    """
    engine: Any = drain_env["engine"]
    container = drain_env["container"]
    run_id: str = drain_env["run_id"]

    built: list[Any] = []

    class CapturingCaseSink(WebhookCaseSink):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            built.append(self)

    class CapturingNotifySink(WebhookNotifySink):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            built.append(self)

    # Production wiring, observed rather than replaced: the factories in deps.py are
    # the per-row client openers the finding is about.
    monkeypatch.setattr(api_deps, "WebhookCaseSink", CapturingCaseSink)
    monkeypatch.setattr(api_deps, "WebhookNotifySink", CapturingNotifySink)

    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, json={"accepted": True})

    opened: list[httpx.Client] = []
    closed: list[httpx.Client] = []
    real_init = httpx.Client.__init__
    real_close = httpx.Client.close

    def spy_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        # The far end of the wire substituted INSIDE the client the sink itself
        # constructs: HttpSink owns a real client, and every open and close is counted.
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)
        opened.append(self)

    def spy_close(self: httpx.Client) -> None:
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(httpx.Client, "__init__", spy_init)
    monkeypatch.setattr(httpx.Client, "close", spy_close)

    account = "ACCTDRAIN001"
    bundle_a = _case_bundle(run_id, drain_env["b"], account, 1)
    bundle_c = _case_bundle(run_id, drain_env["c"], account, 1)
    sent_bundle = _case_bundle(run_id, drain_env["a"], account, 1)
    notify_key = api_decisions.notify_idempotency_key(sent_bundle.idempotency_key)
    decision_id = "01DECISIONIDNOTTHEKEY00001"

    with Session(engine) as session:
        _insert_outbox(
            session,
            run_id=run_id,
            case_id=drain_env["a"],
            decision_seq=1,
            case_seq=1,
            sink_id=api_decisions.OUTBOX_SINK_CASE,
            payload=sent_bundle.to_payload(),
            idempotency_key=sent_bundle.idempotency_key,
            status="sent",
            sent_at=PASSED_AT,
        )
        notify_row = _insert_outbox(
            session,
            run_id=run_id,
            case_id=drain_env["a"],
            decision_seq=1,
            case_seq=2,
            sink_id=api_decisions.OUTBOX_SINK_NOTIFY,
            payload=_notification_payload(bundle=sent_bundle, decision_id=decision_id),
            idempotency_key=notify_key,
        )
        case_row_b = _insert_outbox(
            session,
            run_id=run_id,
            case_id=drain_env["b"],
            decision_seq=1,
            case_seq=1,
            sink_id=api_decisions.OUTBOX_SINK_CASE,
            payload=bundle_a.to_payload(),
            idempotency_key=bundle_a.idempotency_key,
        )
        case_row_c = _insert_outbox(
            session,
            run_id=run_id,
            case_id=drain_env["c"],
            decision_seq=1,
            case_seq=1,
            sink_id=api_decisions.OUTBOX_SINK_CASE,
            payload=bundle_c.to_payload(),
            idempotency_key=bundle_c.idempotency_key,
        )
        session.commit()
        notify_id, case_b_id, case_c_id = (
            int(notify_row.outbox_id),
            int(case_row_b.outbox_id),
            int(case_row_c.outbox_id),
        )

    report = drain_once(container, now=datetime.now(UTC), batch=DRAIN_BATCH_DEFAULT)

    assert (report.claimed, report.sent, report.retrying, report.dead) == (
        3,
        3,
        0,
        0,
    ), f"the pass did not deliver exactly the three due rows: {report.as_dict()}"
    # --- the leak, asserted directly -------------------------------------------
    assert len(opened) == len(closed), (
        f"one drain pass opened {len(opened)} httpx clients and closed {len(closed)}; "
        "every unclosed one is a connection pool a dying worker never gave back"
    )
    assert {id(c) for c in closed} == {id(c) for c in opened}, "a different client closed"
    case_sinks = [sink for sink in built if isinstance(sink, WebhookCaseSink)]
    notify_sinks = [sink for sink in built if isinstance(sink, WebhookNotifySink)]
    assert (len(case_sinks), len(notify_sinks)) == (1, 1), (
        f"a pass that touched two sink ids built {len(case_sinks)}+{len(notify_sinks)}; "
        "one sink per row is where the clients leak and the dedupe dies"
    )
    # --- the dedupe guard now holds the row's actual key ------------------------
    case_sink, notify_sink = case_sinks[0], notify_sinks[0]
    assert case_sink.has_delivered(bundle_a.idempotency_key)
    assert case_sink.has_delivered(bundle_c.idempotency_key), (
        "the pass built a fresh sink per row, so two rows of the same sink id never "
        "shared a delivered-key map and the within-process guard could never fire"
    )
    with Session(engine) as session:
        row = session.get(OutboxMessage, notify_id)
        assert str(row.status) == "sent"
        assert notify_sink.has_delivered(str(row.idempotency_key)), (
            "the notify delivery is recorded under the notification id, not the row's "
            "idempotency key — two key spaces that never meet"
        )
    # --- what the consumer actually received ------------------------------------
    by_path = {str(request.url.path): request for request in posts}
    notify_post = by_path["/notify"]
    assert notify_post.headers[IDEMPOTENCY_HEADER] == notify_key, (
        "the consumer dedupes on the header; sending it the decision id while the "
        "database promised the notify key leaves the two halves of the promise apart"
    )
    assert json.loads(notify_post.content)["notification_id"] == notify_key
    assert by_path["/webhook"].headers[IDEMPOTENCY_HEADER] in {
        bundle_a.idempotency_key,
        bundle_c.idempotency_key,
    }
    assert {str(r.headers[IDEMPOTENCY_HEADER]) for r in posts} == {
        notify_key,
        bundle_a.idempotency_key,
        bundle_c.idempotency_key,
    }
    # --- signing and the replay window are untouched ----------------------------
    now_epoch = int(time.time())
    for request in posts:
        stamp = verify_signature(
            request.headers[SIGNATURE_HEADER],
            request.content,
            TEST_WEBHOOK_SECRET,
            now=now_epoch,
        )
        assert abs(now_epoch - stamp) <= 300, "the ±300 s window was not enforced"
    with Session(engine) as session:
        for outbox_id in (case_b_id, case_c_id):
            assert str(session.get(OutboxMessage, outbox_id).status) == "sent"


# --- finding 2: the claim that survives the worker dying mid-POST ----------------


class _MidPostCrash(BaseException):
    """A process SIGKILLed while the POST was in flight: not caught, not rollable."""


class CrashingRecordingSink:
    """Records each attempt AND what the COMMITTED queue says about the row at that
    instant, via a fresh session — the question finding 2 asks is whether a claim is
    durable BEFORE the packet leaves, not afterwards."""

    def __init__(self, engine: Any, outbox_id: int) -> None:
        self._engine = engine
        self._outbox_id = outbox_id
        self.attempts_seen: list[dict[str, Any]] = []
        self.closed = 0

    @property
    def sink_id(self) -> str:
        return "recorded-crash"

    def has_delivered(self, idempotency_key: str) -> bool:
        return False

    def emit(self, bundle: CaseBundle) -> Any:
        with Session(self._engine) as probe:
            row = probe.get(OutboxMessage, self._outbox_id)
            self.attempts_seen.append(
                {
                    "key": bundle.idempotency_key,
                    "status": str(row.status),
                    "attempts": int(row.attempts),
                    "next_attempt_at": row.next_attempt_at,
                }
            )
        if len(self.attempts_seen) <= MAX_ATTEMPTS:
            raise _MidPostCrash(f"killed mid-POST of {bundle.idempotency_key}")
        raise AssertionError(
            "a sixth delivery was attempted after the five-attempt ladder was exhausted"
        )

    def close(self) -> None:
        self.closed += 1


def test_a_row_whose_delivery_dies_mid_post_counts_every_attempt_and_reaches_dead_letter(
    drain_env: dict[str, Any],
) -> None:
    """Five crashed passes must exhaust the ladder, not restart it forever.

    Each pass kills the worker mid-POST (``BaseException`` — no handler runs, and with
    it any state the pass had not yet committed). The claim fix persists, per crashed
    pass: ``in_flight``, ``attempts`` incremented, and a ``next_attempt_at`` at least
    the staleness horizon old before the row is claimable again. After the fifth
    crashed attempt the ladder is spent, so the next pass buries the row AT CLAIM TIME
    — five wire touches, never a sixth. Before the fix none of that held: the pass
    committed nothing until its end, so each crash rolled the attempt count back to
    zero and the row re-posted forever.
    """
    engine: Any = drain_env["engine"]
    container = drain_env["container"]
    run_id: str = drain_env["run_id"]
    account = "ACCTDRAIN009"

    bundle = _case_bundle(run_id, drain_env["d"], account, 1)
    with Session(engine) as session:
        row = _insert_outbox(
            session,
            run_id=run_id,
            case_id=drain_env["d"],
            decision_seq=1,
            case_seq=1,
            sink_id=api_decisions.OUTBOX_SINK_CASE,
            payload=bundle.to_payload(),
            idempotency_key=bundle.idempotency_key,
        )
        session.commit()
        outbox_id = int(row.outbox_id)

    fake = CrashingRecordingSink(engine, outbox_id)
    original_factory = container.case_sink_factory
    container.case_sink_factory = lambda: fake
    started = datetime.now(UTC)
    step = IN_FLIGHT_STALE_SECONDS + 60
    try:
        for pass_number in range(1, MAX_ATTEMPTS + 1):
            now = started + timedelta(seconds=pass_number * step)
            with pytest.raises(_MidPostCrash):
                drain_once(container, now=now, batch=DRAIN_BATCH_DEFAULT)
            with Session(engine) as session:
                dead_row = session.get(OutboxMessage, outbox_id)
                assert int(dead_row.attempts) == pass_number, (
                    f"after crashed pass {pass_number} the row reports "
                    f"{dead_row.attempts} attempts — the claim died with the worker, "
                    "so the ladder can never advance"
                )
                assert str(dead_row.status) == "in_flight", dead_row.status
                assert dead_row.next_attempt_at is not None
                assert (
                    dead_row.next_attempt_at >= now
                ), "next_attempt_at must carry the claim instant, not the insert time"
            observed = fake.attempts_seen[-1]
            assert observed["status"] == "in_flight", (
                "at the instant the POST was in flight the committed row still said "
                f"{observed['status']!r}: the FOR UPDATE lock was held across the whole "
                "pass and nothing durable separated a claim from a delivery"
            )
            assert observed["attempts"] == pass_number, observed
            assert observed["next_attempt_at"] >= now, observed

        # Five attempts have crashed mid-POST. The ladder is spent: the next pass must
        # bury the row at claim time without touching the wire a sixth time.
        report = drain_once(
            container,
            now=started + timedelta(seconds=(MAX_ATTEMPTS + 1) * step),
            batch=DRAIN_BATCH_DEFAULT,
        )
        assert report.dead == 1 and report.claimed == 1, report.as_dict()
        assert (
            len(fake.attempts_seen) == MAX_ATTEMPTS
        ), f"{len(fake.attempts_seen)} deliveries attempted for a five-attempt ladder"
        with Session(engine) as session:
            buried = session.get(OutboxMessage, outbox_id)
            assert str(buried.status) == "dead", buried.status
            assert int(buried.attempts) == MAX_ATTEMPTS, buried.attempts
            assert buried.dead_at is not None and buried.sent_at is None
            assert buried.next_attempt_at is not None
        assert fake.closed >= 1, "the pass never closed the sink it built"
    finally:
        container.case_sink_factory = original_factory
