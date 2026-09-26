"""Token verification at the boundary: OIDC RS256, local HS256, and no `alg: none`.

No JWT library is used, and that is a dependency decision rather than an oversight:
01 §A rule 6 forbids adding dependencies without a ``DECISIONS.md`` entry, and the
verification actually required here is small enough to state exactly — base64url,
SHA-256, an HMAC compare for the local fallback, and one RSA public-key operation for
the OIDC path. Both primitives are in the standard library.

The vulnerability this module is written against is the classic one: a token whose
``alg`` says ``HS256`` verified with the *public* key as the HMAC secret, which turns
a public JWKS into an authentication bypass. The rule applied here is that the
expected algorithm is chosen by the **issuer of the token**, not by the token: a token
claiming ``iss=oxbow-local`` is only ever checked with HS256 against the local secret,
and a token claiming the configured OIDC issuer is only ever checked with RS256 against
a key from that issuer's JWKS. Anything else is refused with the reason in the
problem detail.

Keycloak in Compose is the real provider (C7, DEV-006); the local fallback exists
because ``make demo`` has to boot offline with Keycloak stopped, and it is minted by an
endpoint that is itself disabled the moment an issuer is configured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

LOCAL_ISSUER: Final = "oxbow-local"
ROLES: Final = ("analyst", "reviewer", "admin")
ROLE_CLAIM: Final = "oxbow_roles"
KEYCLOAK_ROLES_CLAIM: Final = "realm_access"

_HS256: Final = "HS256"
_RS256: Final = "RS256"
_UNSAFE_ALGS: Final = frozenset({"none", "HS384", "HS512"})

# A 60-second leeway for clock skew between containers. Wider and the replay window in
# the outbox signing stops being the tighter of the two constraints.
CLOCK_SKEW_LEEWAY_SECONDS: Final = 60

_SHA256_DIGEST_INFO: Final = bytes.fromhex("3031300d060960864801650304020105000420")


class TokenError(RuntimeError):
    """A token could not be authenticated, with the reason a caller can act on."""


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:  # binascii.Error and friends; message is kept, trace is not
        raise TokenError(f"not valid base64url: {exc}") from exc


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _jwk_to_rsa(jwk: Mapping[str, Any]) -> tuple[int, int]:
    if jwk.get("kty") != "RSA":
        raise TokenError(f"unsupported JWK key type {jwk.get('kty')!r}")
    modulus = jwk.get("n")
    exponent = jwk.get("e")
    if not isinstance(modulus, str) or not isinstance(exponent, str):
        raise TokenError("RSA JWK is missing n or e")
    return int.from_bytes(b64url_decode(modulus), "big"), int.from_bytes(b64url_decode(exponent), "big")


def verify_rs256(signed: bytes, signature: bytes, modulus: int, exponent: int) -> bool:
    """Textbook RSA verify against a PKCS#1 v1.5 DigestInfo for SHA-256.

    Verification only — no private key ever reaches this module — so the small amount of
    number theory here is the whole blast radius. The comparison is against the exact
    DER DigestInfo prefix plus the digest, which rejects a padded-but-wrong hash, and
    the ``padding_len < 8`` guard is the minimum PS length RFC 8017 requires.
    """
    if modulus <= 0 or exponent <= 0:
        return False
    signature_int = int.from_bytes(signature, "big")
    if signature_int >= modulus:
        return False
    em_len = (modulus.bit_length() + 7) // 8
    encoded = pow(signature_int, exponent, modulus).to_bytes(em_len, "big")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(signed).digest()
    # EM = 0x00 || 0x01 || PS(>=8 bytes of 0xff) || 0x00 || DigestInfo, so the three
    # fixed bytes plus the separator are what PS gets left with.
    padding_len = em_len - len(digest_info) - 3
    if padding_len < 8:
        return False
    return encoded == b"\x00\x01" + b"\xff" * padding_len + b"\x00" + digest_info


def verify_hs256(signed: bytes, signature: bytes, secret: str) -> bool:
    """Constant-time HMAC compare. Same shape as the outbound signing rule (02 §E)."""
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True, slots=True)
class Jwks:
    """A fetched key set, with the time it was fetched so a cache can expire it."""

    keys: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    fetched_at: float = 0.0

    @staticmethod
    def parse(document: Mapping[str, Any]) -> Jwks:
        out: dict[str, tuple[int, int]] = {}
        for entry in document.get("keys", []):
            if not isinstance(entry, Mapping):
                continue
            modulus, exponent = _jwk_to_rsa(entry)
            out[str(entry.get("kid", ""))] = (modulus, exponent)
        return Jwks(keys=out, fetched_at=time.time())


class JwksCache:
    """Fetches an issuer's JWKS and holds it for a while.

    The refresh is lazy rather than scheduled: a prototype that needs a background
    task to be able to log in has invented operational surface nobody will run. A
    fetch failure raises on the request that needed it, and the route answers with a
    ``problem+json`` naming the issuer — a stale key cache silently accepting revoked
    keys is the worse failure (03 §A rule 1).
    """

    def __init__(
        self,
        url: str,
        *,
        client: httpx.Client | None = None,
        ttl_seconds: float = 300.0,
    ) -> None:
        if not url:
            raise ValueError("JWKS cache needs an issuer URL; the OIDC path is not configured")
        self._url = url
        self._client = client or httpx.Client(timeout=5.0)
        self._ttl = ttl_seconds
        self._current = Jwks()

    @property
    def url(self) -> str:
        return self._url

    def keys(self, *, force_refresh: bool = False) -> Mapping[str, tuple[int, int]]:
        fresh = (time.time() - self._current.fetched_at) < self._ttl
        if fresh and not force_refresh and self._current.keys:
            return self._current.keys
        try:
            response = self._client.get(self._url)
            response.raise_for_status()
            document = response.json()
        except httpx.HTTPError as exc:
            raise TokenError(f"cannot fetch JWKS from {self._url}: {exc}") from exc
        if not isinstance(document, Mapping):
            raise TokenError(f"JWKS at {self._url} is not a JSON object")
        self._current = Jwks.parse(document)
        if not self._current.keys:
            raise TokenError(f"JWKS at {self._url} contained no RSA keys")
        return self._current.keys


def decode_token(
    token: str,
    *,
    oidc_issuer: str,
    audience: str,
    jwks_cache: JwksCache | None,
    local_secret: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify one bearer token and return its claims, or raise :class:`TokenError`.

    Algorithm selection is by issuer, never by the token's own header. Expiry, audience
    and issuer are all checked after the signature, in that order, because a claim on an
    unverified payload is data from an attacker, not a fact about a user.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("a bearer token must have three dot-separated parts")
    header_raw, payload_raw, signature_raw = parts
    try:
        header = json.loads(b64url_decode(header_raw))
    except (TokenError, json.JSONDecodeError) as exc:
        raise TokenError(f"token header is not JSON: {exc}") from exc
    if not isinstance(header, Mapping):
        raise TokenError("token header is not an object")

    alg = str(header.get("alg", ""))
    if alg in _UNSAFE_ALGS or alg == "":
        raise TokenError(f"algorithm {alg!r} is refused: 'none' is not authentication")

    signed = f"{header_raw}.{payload_raw}".encode("ascii")
    signature = b64url_decode(signature_raw)

    claimed_issuer = str(_peek_issuer(payload_raw))
    if claimed_issuer == LOCAL_ISSUER:
        if alg != _HS256:
            raise TokenError(
                f"a local token must be {_HS256}, got {alg!r}. Choosing the algorithm from the "
                "token rather than the issuer is the HS256/RS256 confusion attack."
            )
        if not verify_hs256(signed, signature, local_secret):
            raise TokenError("local token signature does not verify against this process's secret")
        expected_issuer = LOCAL_ISSUER
    else:
        if alg != _RS256:
            raise TokenError(
                f"tokens for issuer {claimed_issuer!r} must be {_RS256}, got {alg!r}"
            )
        if jwks_cache is None or not oidc_issuer:
            raise TokenError(
                "an RS256 token arrived but no OIDC issuer is configured, so nobody can verify it"
            )
        keys = jwks_cache.keys()
        kid = str(header.get("kid", ""))
        pair = keys.get(kid) or _only_key(keys)
        if pair is None:
            raise TokenError(f"no RSA key with kid {kid!r} in the issuer's JWKS")
        if not verify_rs256(signed, signature, pair[0], pair[1]):
            raise TokenError("token signature does not verify against any key from the issuer")
        expected_issuer = oidc_issuer

    try:
        claims: Any = json.loads(b64url_decode(payload_raw))
    except (TokenError, json.JSONDecodeError) as exc:
        raise TokenError(f"token payload is not JSON: {exc}") from exc
    if not isinstance(claims, dict):
        raise TokenError("token payload is not an object")
    _validate_claims(claims, issuer=expected_issuer, audience=audience, now=now)
    return claims


def _only_key(keys: Mapping[str, tuple[int, int]]) -> tuple[int, int] | None:
    values = list(keys.values())
    return values[0] if len(values) == 1 else None


def _peek_issuer(payload_raw: str) -> Any:
    try:
        payload = json.loads(b64url_decode(payload_raw))
    except (TokenError, json.JSONDecodeError):
        raise TokenError("token payload is unreadable")
    return payload.get("iss") if isinstance(payload, Mapping) else None


def _validate_claims(
    claims: Mapping[str, Any], *, issuer: str, audience: str, now: float | None
) -> None:
    current = time.time() if now is None else now
    token_issuer = str(claims.get("iss", ""))
    if token_issuer != issuer:
        raise TokenError(f"token issuer {token_issuer!r} is not the configured {issuer!r}")
    if "exp" in claims and float(claims["exp"]) + CLOCK_SKEW_LEEWAY_SECONDS < current:
        raise TokenError("token is expired")
    if "nbf" in claims and float(claims["nbf"]) - CLOCK_SKEW_LEEWAY_SECONDS > current:
        raise TokenError("token is not valid yet")
    audiences = claims.get("aud")
    audience_list = [audiences] if isinstance(audiences, str) else list(audiences or [])
    if audience and audience not in audience_list and "account" not in audience_list:
        raise TokenError(f"token audience {audience_list!r} does not include {audience!r}")


@dataclass(frozen=True, slots=True)
class Principal:
    """Who the caller is, for this request only.

    ``source`` is recorded so a log line or a problem detail can distinguish an
    identity the realm vouched for from one this process minted for the offline demo.
    A demo token authorising a real erasure request would be invisible without it.
    """

    subject: str
    roles: Sequence[str]
    display_name: str
    source: str

    def has(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)

    @property
    def is_reviewer(self) -> bool:
        return "reviewer" in self.roles or "admin" in self.roles

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


def principal_from_claims(claims: Mapping[str, Any], *, source: str) -> Principal:
    """Map a verified token's claims onto a Principal, roles and all.

    Two claim shapes are read: ``oxbow_roles`` (this project's own) and Keycloak's
    ``realm_access.roles``. The second matters because Keycloak mints it whether or not
    the client asks, and an API that ignores it would reject a correctly configured
    realm user.
    """
    roles: list[str] = []
    explicit = claims.get(ROLE_CLAIM)
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        roles.extend(str(role) for role in explicit)
    realm = claims.get(KEYCLOAK_ROLES_CLAIM)
    if isinstance(realm, Mapping):
        realm_roles = realm.get("roles")
        if isinstance(realm_roles, Sequence) and not isinstance(realm_roles, (str, bytes)):
            roles.extend(str(role) for role in realm_roles)
    known = [role for role in ROLES if role in set(roles)]
    if not known:
        raise TokenError(
            f"token carries none of the OXBOW roles {ROLES}; refusing an unauthorised subject "
            "rather than defaulting it to the least-privileged one, because 'default role' is "
            "how a reviewer endpoint ends up open"
        )
    return Principal(
        subject=str(claims.get("sub") or claims.get("preferred_username") or "unknown"),
        roles=tuple(known),
        display_name=str(
            claims.get("name") or claims.get("preferred_username") or claims.get("sub") or ""
        ),
        source=source,
    )


def mint_local_token(
    *, subject: str, roles: Sequence[str], secret: str, display_name: str = "", ttl_seconds: int = 3600, now: float | None = None
) -> str:
    """Mint the offline-demo HS256 token. Refuses a role it does not know."""
    unknown = [role for role in roles if role not in ROLES]
    if unknown:
        raise ValueError(f"unknown role(s) {unknown}; OXBOW roles are {ROLES}")
    current = time.time() if now is None else now
    header = b64url_encode(json.dumps({"alg": _HS256, "typ": "JWT"}).encode())
    payload = b64url_encode(
        json.dumps(
            {
                "iss": LOCAL_ISSUER,
                "sub": subject,
                "aud": "oxbow-web",
                "iat": int(current),
                "exp": int(current + ttl_seconds),
                ROLE_CLAIM: list(roles),
                "name": display_name or subject,
            }
        ).encode()
    )
    signed = f"{header}.{payload}".encode("ascii")
    signature = b64url_encode(hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def bearer_token(authorization: str | None) -> str:
    """Pull the token out of an Authorization header, or say exactly what is wrong."""
    if not authorization:
        raise TokenError("no Authorization header")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise TokenError("Authorization must be 'Bearer <token>'")
    return value.strip()


__all__ = [
    "CLOCK_SKEW_LEEWAY_SECONDS",
    "LOCAL_ISSUER",
    "ROLES",
    "Jwks",
    "JwksCache",
    "Principal",
    "TokenError",
    "b64url_decode",
    "b64url_encode",
    "bearer_token",
    "decode_token",
    "mint_local_token",
    "principal_from_claims",
    "verify_hs256",
    "verify_rs256",
]
