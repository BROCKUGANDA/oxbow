"""Webhook echo receiver: verifies OXBOW's outbound signature, for real.

02 H: the point of this service is that signing, retries, idempotency and
dead-lettering are exercised end to end against a real HTTP endpoint rather than
a mock inside a test file. A mock proves your own arithmetic; a real receiver
proves the wire format -- that the header survives an actual HTTP round trip, that
the body it was computed over is byte-for-byte the body that arrives, and that a
retry with a fresh idempotency key is observable.

The scheme, stated once in ``oxbow.adapters.signing`` and not restated here:

    X-OXBOW-Signature: t=<unix>,v1=<hex>

over ``t + "." + raw_body`` with HMAC-SHA256, a 300-second replay window and a
constant-time compare.

**This endpoint does not implement verification of its own.** It used to, and the
duplicate drifted into a second definition of the header grammar, the replay window
and the compare. The single implementation is :func:`oxbow.adapters.signing.verify_signature`
and this fixture calls it, so the sender and the receiver cannot disagree by
construction. What is genuinely cross-checked here is the *transport*, not the
crypto, and that is the honest scope of the guarantee: a shared library cannot
catch a body that changed in flight, and that is exactly what this does.

It records deliveries; it never acts on them. OXBOW's payloads are advisory and
every one of them says so in the body.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request, Response

from oxbow.adapters.signing import (
    IDEMPOTENCY_HEADER,
    REPLAY_WINDOW_SECONDS,
    SIGNATURE_HEADER,
    MalformedSignatureError,
    SignatureExpiredError,
    SignatureMismatchError,
    parse_signature_header,
    verify_signature,
)

app = FastAPI(
    title="OXBOW webhook echo",
    description=(
        "Verifies the X-OXBOW-Signature header and records deliveries. Part of the "
        "integration test surface described in 02 H; not a product endpoint."
    ),
    version="0.1.0",
)

# No default. The sender in oxbow.adapters.webhook already refuses to dispatch when
# WEBHOOK_SIGNING_SECRET is unset, so a receiver that fell back to a published literal
# would be the weaker half of the same handshake: anyone reading this repository could
# forge a delivery that verifies. An unset secret is now a 503 that names itself.
SIGNING_SECRET: str = os.environ.get("SIGNING_SECRET", "")
RECORD_PATH: Path = Path(os.environ.get("RECORD_PATH", "/data/deliveries.jsonl"))


def _json_response(payload: dict[str, Any], status_code: int) -> Response:
    """One shape for every reply, so a rejection cannot differ by accident."""
    return Response(
        content=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        status_code=status_code,
        media_type="application/json",
    )


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
    Every one of those decisions is ``oxbow.adapters.signing``'s; this function only
    maps the three failure kinds onto the HTTP status each one deserves, and records
    what a verified delivery looked like.
    """
    raw_body = await request.body()

    if not SIGNING_SECRET:
        # Before the header check: a request that arrives here would otherwise be
        # refused for the wrong reason, and an operator would chase the client.
        return _json_response(
            {
                "detail": "SIGNING_SECRET is unset, so no signature can be verified. "
                "Set it to the same value the outbox signs with; deliveries are refused "
                "rather than accepted against a default."
            },
            503,
        )

    if x_oxbow_signature is None:
        return _json_response({"detail": f"missing {SIGNATURE_HEADER}"}, 401)

    # One clock for the whole request: the same `now` gates the replay window and
    # stamps the delivery record, so the two can never disagree about when it landed.
    now = int(time.time())
    try:
        verify_signature(x_oxbow_signature, raw_body, SIGNING_SECRET, now=now)
    except MalformedSignatureError as exc:
        # A client bug rather than an attack, and reported as one.
        return _json_response({"detail": str(exc)}, 400)
    except SignatureExpiredError as exc:
        # Distinct from a bad signature: this is clock skew or a replay, and the
        # operator needs to tell those two apart (03 J). The header parsed -- it was
        # the window that refused it -- so the skew is still reportable.
        try:
            stamp, _ = parse_signature_header(x_oxbow_signature)
            skew_seconds: int | None = now - int(stamp)
        except (MalformedSignatureError, ValueError):
            skew_seconds = None
        return _json_response(
            {
                "detail": str(exc),
                "error_type": "clock_skew_or_replay",
                "skew_seconds": skew_seconds,
                "window_seconds": REPLAY_WINDOW_SECONDS,
            },
            401,
        )
    except SignatureMismatchError:
        return _json_response(
            {
                "detail": "signature mismatch",
                "error_type": "bad_signature",
            },
            401,
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

    return _json_response(
        {
            "received": True,
            "signature_verified": True,
            "idempotency_key": idempotency_key,
            "advisory_only": True,
        },
        200,
    )


@app.get("/deliveries")
def deliveries() -> dict[str, Any]:
    """List recorded deliveries, so a test can assert a replay was deduplicated."""
    if not RECORD_PATH.exists():
        return {"count": 0, "deliveries": []}
    lines = RECORD_PATH.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines if line.strip()]
    return {"count": len(parsed), "deliveries": parsed}
