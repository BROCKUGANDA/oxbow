"""Run and batch identity: ULID plus content digests, stdlib only.

DEV-003 makes ``run_id`` a ULID rather than the spec's ``uuid``, because the
identity model in 02 B needs run ids that sort lexicographically by creation
time: 01 P7's SSE resume replays "everything after Last-Event-ID", and a random
UUID has no order to replay along. A ULID is 26 characters of Crockford base32
over 128 bits: 48 bits of millisecond timestamp followed by 80 bits of entropy,
so ``id_1 < id_2`` in string order means ``id_1`` was created first.

No dependency is added to get it (01 A rule 6). ``python-ulid`` is declared in
``pyproject.toml`` for downstream components, but the pipeline's identity layer
is the one place where a third-party encoder is pure overhead: the format is
128 bits and a lookup table, and a hand-rolled implementation is auditable line
by line against the spec it implements. It also keeps ``oxbow.identity``
importable from ``ports/``, which must not acquire dependencies (import-linter
contract 2).

Crockford base32 deliberately omits ``I``, ``L``, ``O`` and ``U`` so a
hand-transcribed run id cannot be misread. That also means an arbitrary 26
characters are *not* a valid ULID, so ``is_ulid`` is a real check and not a
length test.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, datetime
from typing import Final

# Crockford base32. Position N encodes value N. No I, L, O or U.
CROCKFORD_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_CROCKFORD_INDEX: Final[dict[str, int]] = {char: i for i, char in enumerate(CROCKFORD_ALPHABET)}

ULID_LENGTH: Final = 26
ULID_TOTAL_BITS: Final = 128
ULID_ENTROPY_BITS: Final = 80
ULID_ENTROPY_BYTES: Final = 10

# The largest value the leading character can hold: the top character encodes
# only bits 122..127 of a 128-bit id, so '7' is its ceiling. A ULID starting with
# '8' or above is more than 128 bits and therefore not well formed.
_ULID_LEADING_CHAR_MAX: Final = 7

SHA256_HEX_LENGTH: Final = 64
BATCH_ID_LENGTH: Final = 12


class IdentityError(ValueError):
    """Raised when an identifier is not well formed.

    Loud and at the boundary: a malformed ``run_id`` that survives into a
    manifest is a lineage claim that cannot be resolved, and it is discovered
    months later by whoever tries to join a scored row back to its run.
    """


def encode_crockford(value: int, *, width_bits: int) -> str:
    """Encode a non-negative integer that fits in ``width_bits`` as base32 text.

    A 128-bit field needs 26 characters, because 25 groups of five bits only
    cover 125: the leading character carries the remaining three bits and is
    therefore capped at ``7``. Round-tripping through :func:`decode_crockford`
    is exact only because that ceiling is enforced here rather than assumed.
    """
    if value < 0:
        raise IdentityError(f"cannot encode a negative value as a ULID: {value}")
    if value.bit_length() > width_bits:
        raise IdentityError(
            f"value needs {value.bit_length()} bits, which exceeds the {width_bits}-bit field"
        )
    length = -(-width_bits // 5)
    digits = [
        CROCKFORD_ALPHABET[(value >> (5 * (length - 1 - i))) & 0b11111] for i in range(length)
    ]
    return "".join(digits)


def decode_crockford(text: str) -> int:
    """Inverse of :func:`encode_crockford`, rejecting characters outside the alphabet."""
    value = 0
    for char in text:
        index = _CROCKFORD_INDEX.get(char.upper())
        if index is None:
            raise IdentityError(f"{char!r} is not in the Crockford base32 alphabet ({text!r})")
        value = (value << 5) | index
    return value


def new_ulid(timestamp_ms: int | None = None, entropy: bytes | None = None) -> str:
    """Build a 26-character ULID.

    ``timestamp_ms`` defaults to the current wall clock in UTC milliseconds and
    ``entropy`` to 10 fresh bytes. Both are overridable so a test can pin an id
    instead of asserting on randomness; production callers pass neither.

    The two arguments are *not* mixed: entropy is placed in the low 80 bits and
    the timestamp in the high 48, which is what makes string order equal time
    order for ids that share no entropy.
    """
    millis = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    if not 0 <= millis < (1 << 48):
        raise IdentityError(f"timestamp_ms {millis} is outside the 48-bit ULID clock range")
    raw = os.urandom(ULID_ENTROPY_BYTES) if entropy is None else entropy
    if len(raw) != ULID_ENTROPY_BYTES:
        raise IdentityError(
            f"ULID entropy must be exactly {ULID_ENTROPY_BYTES} bytes, got {len(raw)}"
        )
    value = (millis << (8 * ULID_ENTROPY_BYTES)) | int.from_bytes(raw, "big")
    return encode_crockford(value, width_bits=ULID_TOTAL_BITS)


def is_ulid(candidate: str) -> bool:
    """True when ``candidate`` is a well formed 26-character ULID.

    Checks length, the 3-bit ceiling on the leading character, and membership in
    the alphabet *in canonical upper case*. :func:`decode_crockford` is
    deliberately case-insensitive, because Crockford base32 decodes ``i``/``l``
    ambiguity by excluding those glyphs; a stored identifier has one spelling and
    anything else is a transcription error, not an equivalent form.
    """
    if len(candidate) != ULID_LENGTH:
        return False
    if candidate[0] not in CROCKFORD_ALPHABET[: _ULID_LEADING_CHAR_MAX + 1]:
        return False
    return all(char in _CROCKFORD_INDEX for char in candidate)


def require_ulid(value: str, *, field: str = "run_id") -> str:
    """Return ``value`` if it is a ULID, else fail naming the field."""
    if not is_ulid(value):
        raise IdentityError(
            f"{field} {value!r} is not a 26-character Crockford base32 ULID (DEV-003)"
        )
    return value


def ulid_to_datetime_utc(value: str) -> datetime:
    """Recover the creation instant encoded in a ULID's high 48 bits."""
    require_ulid(value)
    millis = decode_crockford(value) >> ULID_ENTROPY_BITS
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def sha256_hex(text: str) -> str:
    """Lowercase hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_of_bytes(payload: bytes) -> str:
    """Lowercase hex SHA-256 of a byte string, used for artifact digests."""
    return hashlib.sha256(payload).hexdigest()


def batch_id_from_digest(digest: str) -> str:
    """Reduce a full SHA-256 hex digest to the declared 12-character batch id.

    12 hex characters is 48 bits, which ``config/sources.yaml`` declares for
    ``account_key.prefix_length`` and 03 B declares for batch ids. Truncating a
    digest is safe for an *identity* (not a security claim) and it keeps batch
    paths short enough to read in a triage listing.
    """
    if len(digest) != SHA256_HEX_LENGTH:
        raise IdentityError(f"expected a {SHA256_HEX_LENGTH}-character digest, got {len(digest)}")
    return digest[:BATCH_ID_LENGTH]


def is_batch_id(candidate: str) -> bool:
    """True when ``candidate`` is 12 lowercase hex characters."""
    return len(candidate) == BATCH_ID_LENGTH and all(
        char in "0123456789abcdef" for char in candidate
    )


__all__ = [
    "BATCH_ID_LENGTH",
    "CROCKFORD_ALPHABET",
    "SHA256_HEX_LENGTH",
    "ULID_LENGTH",
    "IdentityError",
    "batch_id_from_digest",
    "decode_crockford",
    "encode_crockford",
    "is_batch_id",
    "is_ulid",
    "new_ulid",
    "require_ulid",
    "sha256_hex",
    "sha256_of_bytes",
    "ulid_to_datetime_utc",
]
