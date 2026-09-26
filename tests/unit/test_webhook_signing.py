"""Webhook signing: one implementation, and the rejections it is trusted to make.

`adapters/signing.py` and `apps/api/echo.py` each carried a full copy of signature
verification -- the header grammar, the 300-second replay window, the HMAC recompute and
the compare. Two copies of a security check is how one of them ends up wrong, so echo now
calls `verify_signature` instead.

The properties this pins are the ones that were load-bearing in both copies, so they are
what a merge could silently lose:

* a **tampered** delivery is rejected (the digest covers the exact bytes sent);
* an **expired** delivery is rejected, and stays distinguishable from a bad signature;
* the compare is **constant-time** (`hmac.compare_digest`), not `==`;
* the window is **300 seconds**, inclusive at the boundary;
* echo has no private re-implementation left behind, and answers 200/400/401 exactly as
  it did before, so the Compose integration surface keeps the contract it advertised.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from oxbow.adapters import signing
from oxbow.adapters.signing import (
    REPLAY_WINDOW_SECONDS,
    SIGNATURE_HEADER,
    MalformedSignatureError,
    SignatureExpiredError,
    SignatureMismatchError,
    sign_body,
    verify_signature,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# echo is served as `uvicorn echo:app` from apps/api, and its siblings import each other
# as `api.<thing>` with apps/ on the path. Import it the same way here. Imported at
# module level rather than skipped: an echo that will not import is a failure to read,
# not a reason to report the guard as passing (STATE.md §11).
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api import echo  # noqa: E402  (the path bootstrap above must run first)

SECRET: Final = "test_signing_secret"
BODY: Final = json.dumps(
    {"case_id": "CASE-4411", "schema_version": "1", "advisory_only": True}
).encode("utf-8")


def _sign(body: bytes = BODY, secret: str = SECRET, *, now: int | None = None) -> str:
    header, _ts = sign_body(body, secret, now=now)
    return header


# --------------------------------------------------------------- the unified module


def test_a_honestly_signed_body_verifies_and_reports_its_signing_time() -> None:
    header, signed_at = sign_body(BODY, SECRET)
    assert verify_signature(header, BODY, SECRET) == signed_at


def test_a_tampered_body_is_rejected_even_though_the_header_is_authentic() -> None:
    """The signature was made over different bytes than the ones being checked."""
    header = _sign()
    tampered = BODY.replace(b"CASE-4411", b"CASE-9999")
    with pytest.raises(SignatureMismatchError):
        verify_signature(header, tampered, SECRET)


def test_a_tampered_digest_is_rejected() -> None:
    header = _sign()
    stamp, digest = header.split(",")
    flipped = digest[:-1] + ("0" if digest[-1] != "0" else "1")
    with pytest.raises(SignatureMismatchError):
        verify_signature(f"{stamp},{flipped}", BODY, SECRET)


def test_a_signature_made_under_another_secret_is_rejected() -> None:
    header = _sign(secret="not_the_shared_secret")
    with pytest.raises(SignatureMismatchError):
        verify_signature(header, BODY, SECRET)


def test_the_digest_comparison_is_constant_time_not_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`==` on a MAC leaks how many leading hex characters were right."""
    calls: list[int] = []
    real = hmac.compare_digest

    def spy(left: Any, right: Any) -> bool:
        calls.append(1)
        return real(left, right)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    verify_signature(_sign(), BODY, SECRET)
    assert calls, "verify_signature compared the digest without hmac.compare_digest"


def test_a_delivery_outside_the_replay_window_is_rejected_as_expired() -> None:
    """A captured request must stop being usable, so its age is checked at all."""
    stale = _sign(now=int(time.time()) - (REPLAY_WINDOW_SECONDS + 1))
    with pytest.raises(SignatureExpiredError, match="replay window"):
        verify_signature(stale, BODY, SECRET)


def test_a_future_delivery_beyond_the_window_is_rejected_too() -> None:
    """The window is two-sided; a clock ahead is as unusable as a clock behind."""
    ahead = _sign(now=int(time.time()) + (REPLAY_WINDOW_SECONDS + 1))
    with pytest.raises(SignatureExpiredError):
        verify_signature(ahead, BODY, SECRET)


def test_the_replay_window_is_300_seconds_and_inclusive_at_the_edge() -> None:
    assert REPLAY_WINDOW_SECONDS == 300
    edge = _sign(now=int(time.time()) - REPLAY_WINDOW_SECONDS)
    assert verify_signature(edge, BODY, SECRET)  # exactly 300 is still inside
    just_out = _sign(now=int(time.time()) - (REPLAY_WINDOW_SECONDS + 1))
    with pytest.raises(SignatureExpiredError):
        verify_signature(just_out, BODY, SECRET)


def test_expired_is_distinguishable_from_bad_and_malformed() -> None:
    """Three operator meanings, three exception types: skew, attack, client bug."""
    assert issubclass(SignatureExpiredError, signing.SignatureError)
    assert issubclass(SignatureMismatchError, signing.SignatureError)
    assert issubclass(MalformedSignatureError, signing.SignatureError)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "t=123",
        "t=123,",
        ",v1=abc",
        "123,v1=abc",  # missing the t= prefix: no guessing which part is which
        "t=,v1=abc",
        "t=abc,v1=def",  # non-integer timestamp
        "t=123,v1=",
        "x=1,y=2",
    ],
)
def test_a_malformed_header_is_refused_rather_than_parsed_ambiguously(header: str) -> None:
    with pytest.raises(MalformedSignatureError):
        verify_signature(header, BODY, SECRET)


# ------------------------------------------------------- echo, which now delegates


def test_echo_keeps_no_private_copy_of_the_signature_scheme() -> None:
    """The regression guard for the duplication this change removed.

    echo may *name* the shared functions -- it has to, to verify a delivery -- but it
    must not own an implementation of any of them. If someone re-adds a local HMAC or
    window check, this fails on the module that defines it rather than on a diff.
    """
    for name in ("verify_signature", "parse_signature_header"):
        assert name in vars(echo), f"echo no longer imports {name} from the shared module"
        owning = getattr(echo, name).__module__
        assert owning == "oxbow.adapters.signing", (
            f"echo.{name} resolves to {owning!r}: the signature scheme has grown a "
            "second copy again"
        )

    # The pieces echo used to implement itself are gone, not shadowed.
    assert not hasattr(echo, "compute_signature")
    assert not hasattr(echo, "hmac")
    # ...and the constants it shares are the shared ones, not restatements.
    assert echo.REPLAY_WINDOW_SECONDS == signing.REPLAY_WINDOW_SECONDS
    assert echo.SIGNATURE_HEADER == signing.SIGNATURE_HEADER
    assert echo.IDEMPOTENCY_HEADER == signing.IDEMPOTENCY_HEADER


@pytest.fixture()
def echo_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real echo app, recording into tmp_path rather than /data."""
    monkeypatch.setattr(echo, "RECORD_PATH", tmp_path / "deliveries.jsonl")
    monkeypatch.setattr(echo, "SIGNING_SECRET", SECRET)
    return TestClient(echo.app)


def _recorded(client: TestClient) -> list[dict[str, Any]]:
    body = client.get("/deliveries").json()
    return body["deliveries"]


def test_echo_accepts_an_honest_delivery_and_records_it(echo_client: TestClient) -> None:
    response = echo_client.post(
        "/webhook",
        content=BODY,
        headers={SIGNATURE_HEADER: _sign(), "Idempotency-Key": "k-1"},
    )
    assert response.status_code == 200
    assert response.json()["signature_verified"] is True
    recorded = _recorded(echo_client)
    assert len(recorded) == 1
    assert recorded[0]["body_sha256"] == hashlib.sha256(BODY).hexdigest()
    assert recorded[0]["idempotency_key"] == "k-1"


def test_echo_rejects_a_tampered_signature_with_401(echo_client: TestClient) -> None:
    """Signed bytes are not the bytes that arrived: nothing is recorded."""
    response = echo_client.post(
        "/webhook",
        content=BODY.replace(b"CASE-4411", b"CASE-9999"),
        headers={SIGNATURE_HEADER: _sign()},
    )
    assert response.status_code == 401
    assert response.json()["error_type"] == "bad_signature"
    assert _recorded(echo_client) == []


def test_echo_rejects_an_expired_signature_and_calls_it_clock_skew(
    echo_client: TestClient,
) -> None:
    """401, but a different reason than a forgery -- skew is an ops problem (03 J)."""
    stale = _sign(now=int(time.time()) - (REPLAY_WINDOW_SECONDS + 60))
    response = echo_client.post("/webhook", content=BODY, headers={SIGNATURE_HEADER: stale})
    assert response.status_code == 401
    payload = response.json()
    assert payload["error_type"] == "clock_skew_or_replay"
    assert payload["window_seconds"] == REPLAY_WINDOW_SECONDS
    assert payload["skew_seconds"] is not None and abs(payload["skew_seconds"]) > 300
    assert _recorded(echo_client) == []


def test_echo_rejects_a_malformed_header_with_400_not_401(echo_client: TestClient) -> None:
    response = echo_client.post(
        "/webhook", content=BODY, headers={SIGNATURE_HEADER: "t=abc,v1=def"}
    )
    assert response.status_code == 400
    assert _recorded(echo_client) == []


def test_echo_rejects_a_missing_signature_header_with_401(echo_client: TestClient) -> None:
    response = echo_client.post("/webhook", content=BODY)
    assert response.status_code == 401
    assert _recorded(echo_client) == []
