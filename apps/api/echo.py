"""Webhook echo receiver: verifies OX BOW's outbound signature, for real.

02 H: the point of this service is that signing, retries, idempotency and
dead-lettering are exercised end to end against a real HTTP endpoint rather than
a mock inside a test file. A mock proves your own arithmetic; a real receiver
proves the wire format.

02 E, the signing scheme:

    X-OXBOW-Signature: t=<unix>,v1=<hex>

over ``t + "." + raw_body`` with HMAC-SHA256, a 300-second replay window and a
constant-time compare. The receiver recomputes the HMAC, compares in constant
time, rejects anything outside the replay window, and records the delivery so a
duplicate is detectable downstream.

It records deliveries; it never acts on them. OX BOW's payloads are advisory and
every one of them says so in the body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request, Response

app = FastAPI(
    title="OXBOW webhook echo",
    description=(
        "Verifies the X-OXBOW-Signature header and records deliveries. Part of the "
        "integration test surface described in 02 H; not a product endpoint."
    ),
    version="0.1.0",
)

SIGNING_SECRET: str = os.environ.get("SIGNING_SECRET", "oxbow_local_secret")
RECORD_PATH: Path = Path(os.environ.get("RECORD_PATH", "/data/deliveries.jsonl"))

# 02 E: 300-second replay window. A legitimate delivery rejected for clock skew,
# or a replay accepted because the window is too wide, are both real failures.
REPLAY_WINDOW_SECONDS: int = 300

SIGNATURE_HEADER = "X-OXBOW-Signature"
IDEMPOTENCY_HEADER = "Idempotency-Key"


def compute_signature(timestamp: str, raw_body: bytes, secret: str) -> str:
    """Recompute the expected v1 hex digest for a delivery."""
    signed_payload = timestamp.encode() + b"." + raw_body
    return hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()


def parse_signature_header(header_value: str) -> tuple[str, str]:
    """Split ``t=<unix>,v1=<hex>`` into its two parts.

    Raises ValueError on a malformed header rather than guessing which part is
    which: a signature check that parses ambiguously is not a signature check.
    """
    timestamp_part, _, digest_part = header_value.partition(",")
    if not _ or not digest_part:
        raise ValueError(f"malformed {SIGNATURE_HEADER}: expected 't=<unix>,v1=<hex>'")
    timestamp = timestamp_part.removeprefix("t=").strip()
    digest = digest_part.removeprefix("v1=").strip()
    if not timestamp or not digest:
        raise ValueError(f"malformed {SIGNATURE_HEADER}: empty timestamp or digest")
    return timestamp, digest


def record_delivery(entry: dict[str, Any]) -> None:
    """Append one verified delivery to the record file, line-delimited JSON."""
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RECORD_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
        handle.write("\n")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness. Compose depends on this, so it must answer even when empty."""
    return {"status": "ok", "role": "webhook-echo"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_oxbow_signature: str | None = Header(default=None, alias=SIGNATURE_HEADER),
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
) -> Response:
    """Verify the signature and record the delivery.

    Returns 401 on a bad or replayed signature, 400 on a malformed one, and 200
    only when the signature verified in constant time inside the replay window.
    """
    raw_body = await request.body()

    if x_oxbow_signature is None:
        return Response(
            content=json.dumps({"detail": f"missing {SIGNATURE_HEADER}"}),
            status_code=401,
            media_type="application/json",
        )

    try:
        timestamp, provided_digest = parse_signature_header(x_oxbow_signature)
    except ValueError as exc:
        return Response(
            content=json.dumps({"detail": str(exc)}),
            status_code=400,
            media_type="application/json",
        )

    try:
        sent_at = int(timestamp)
    except ValueError:
        return Response(
            content=json.dumps({"detail": f"non-integer timestamp: {timestamp!r}"}),
            status_code=400,
            media_type="application/json",
        )

    now = int(time.time())
    if abs(now - sent_at) > REPLAY_WINDOW_SECONDS:
        # Distinct from a bad signature: this is clock skew or a replay, and the
        # operator needs to tell those two apart (03 J).
        return Response(
            content=json.dumps(
                {
                    "detail": "signature outside the replay window",
                    "error_type": "clock_skew_or_replay",
                    "skew_seconds": now - sent_at,
                    "window_seconds": REPLAY_WINDOW_SECONDS,
                }
            ),
            status_code=401,
            media_type="application/json",
        )

    expected_digest = compute_signature(timestamp, raw_body, SIGNING_SECRET)
    if not hmac.compare_digest(expected_digest, provided_digest):
        return Response(
            content=json.dumps(
                {
                    "detail": "signature mismatch",
                    "error_type": "bad_signature",
                }
            ),
            status_code=401,
            media_type="application/json",
        )

    try:
        payload: Any = json.loads(raw_body) if raw_body else None
    except json.JSONDecodeError:
        payload = None

    record_delivery(
        {
            "received_at": now,
            "idempotency_key": idempotency_key,
            "signature_verified": True,
            "body_sha256": hashlib.sha256(raw_body).hexdigest(),
            "schema_version": payload.get("schema_version") if isinstance(payload, dict) else None,
            "case_id": payload.get("case_id") if isinstance(payload, dict) else None,
        }
    )

    return Response(
        content=json.dumps(
            {
                "received": True,
                "signature_verified": True,
                "idempotency_key": idempotency_key,
                "advisory_only": True,
            }
        ),
        status_code=200,
        media_type="application/json",
    )


@app.get("/deliveries")
def deliveries() -> dict[str, Any]:
    """List recorded deliveries, so a test can assert a replay was deduplicated."""
    if not RECORD_PATH.exists():
        return {"count": 0, "deliveries": []}
    lines = RECORD_PATH.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines if line.strip()]
    return {"count": len(parsed), "deliveries": parsed}
