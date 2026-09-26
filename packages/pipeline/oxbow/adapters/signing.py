"""Outbound payload signing: ``X-OXBOW-Signature: t=<unix>,v1=<hex>`` (02 §E).

The scheme, stated once so sender and receiver cannot drift:

    v1 = HMAC-SHA256(secret, str(t) + "." + raw_body)

* the timestamp is inside the signed material, so a captured request cannot be
  replayed later with a fresh ``t`` without the secret;
* the body is signed as **raw bytes**, never as reparsed JSON: re-serialising would
  change key order or number formatting and invalidate a correct signature, which
  is the kind of bug that only appears in production under a different Python;
* verification is constant-time (``hmac.compare_digest``), and the replay window is
  300 seconds on both sides.

This module is the **only** implementation of the scheme, on both sides of the wire.
:func:`sign_body` is the sender's half and :func:`verify_signature` is the receiver's
half, which the Compose echo fixture (``apps/api/echo.py``) calls directly rather than
re-deriving the HMAC a second time. Echo used to carry its own copy of the parse, the
window and the compare; the copies drifted, and a drifted signature check is a check that
passes for the wrong reason. What echo still proves that this module cannot is the
transport -- that the header survives a real HTTP round trip and that the signed bytes
are the bytes that arrive.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Final

SIGNATURE_HEADER: Final = "X-OXBOW-Signature"
IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
SCHEMA_VERSION_HEADER: Final = "X-OXBOW-Schema-Version"

# 02 §E: 300 seconds. Wide enough for clock drift between containers, narrow enough
# that a captured request stops being usable before anyone can act on it.
REPLAY_WINDOW_SECONDS: Final = 300

_PREFIX_TIME: Final = "t="
_PREFIX_DIGEST: Final = "v1="


class SignatureError(RuntimeError):
    """Base for every signature failure, so a caller can catch one thing."""


class MalformedSignatureError(SignatureError):
    """The header is not ``t=<unix>,v1=<hex>`` — a client bug, not an attack."""


class SignatureExpiredError(SignatureError):
    """Outside the replay window. Distinct from a mismatch: this is clock skew."""


class SignatureMismatchError(SignatureError):
    """The digest does not correspond to the body under this secret."""


def compute_signature(timestamp: str, raw_body: bytes, secret: str) -> str:
    """The v1 hex digest over ``t + "." + raw_body``."""
    signed = timestamp.encode("utf-8") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def sign_body(raw_body: bytes, secret: str, *, now: int | None = None) -> tuple[str, int]:
    """Build the header value and the timestamp used to build it.

    Returns both: a caller that needs to log or store the signing time cannot
    recover it from the header without re-parsing, and re-parsing is where a subtle
    bug would live.
    """
    timestamp = int(time.time()) if now is None else int(now)
    stamp = str(timestamp)
    digest = compute_signature(stamp, raw_body, secret)
    return f"{_PREFIX_TIME}{stamp},{_PREFIX_DIGEST}{digest}", timestamp


def parse_signature_header(header_value: str) -> tuple[str, str]:
    """Split the header into ``(timestamp, digest)``, refusing ambiguity."""
    time_part, sep, digest_part = header_value.partition(",")
    if not sep or not digest_part:
        raise MalformedSignatureError(
            f"expected '{_PREFIX_TIME}<unix>,{_PREFIX_DIGEST}<hex>', got {header_value!r}"
        )
    if not time_part.startswith(_PREFIX_TIME) or not digest_part.startswith(_PREFIX_DIGEST):
        raise MalformedSignatureError(f"unrecognised signature parts in {header_value!r}")
    timestamp = time_part[len(_PREFIX_TIME) :].strip()
    digest = digest_part[len(_PREFIX_DIGEST) :].strip()
    if not timestamp or not digest:
        raise MalformedSignatureError("signature header has an empty timestamp or digest")
    return timestamp, digest


def verify_signature(
    header_value: str,
    raw_body: bytes,
    secret: str,
    *,
    now: int | None = None,
    window_seconds: int = REPLAY_WINDOW_SECONDS,
) -> int:
    """Verify a delivery signature, raising the specific failure it is.

    Order matters: a malformed header is a 400-shaped problem and a stale timestamp
    is a clock-skew problem, and an operator reading one log line has to be able to
    tell them apart (03 §J). The digest comparison is the last step and is
    constant-time.
    """
    timestamp, provided = parse_signature_header(header_value)
    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise MalformedSignatureError(f"non-integer timestamp {timestamp!r}") from exc

    current = int(time.time()) if now is None else int(now)
    if abs(current - sent_at) > window_seconds:
        raise SignatureExpiredError(
            f"signature is {current - sent_at}s old, outside the {window_seconds}s replay window"
        )

    expected = compute_signature(timestamp, raw_body, secret)
    if not hmac.compare_digest(expected, provided):
        raise SignatureMismatchError("digest does not match the body under this secret")
    return sent_at


__all__ = [
    "IDEMPOTENCY_HEADER",
    "REPLAY_WINDOW_SECONDS",
    "SCHEMA_VERSION_HEADER",
    "SIGNATURE_HEADER",
    "MalformedSignatureError",
    "SignatureError",
    "SignatureExpiredError",
    "SignatureMismatchError",
    "compute_signature",
    "parse_signature_header",
    "sign_body",
    "verify_signature",
]
