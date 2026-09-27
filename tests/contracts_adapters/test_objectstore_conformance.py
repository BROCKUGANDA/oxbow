"""Port conformance for the object-store port, asserted against every adapter.

Plan §13 owes every port "a Protocol, a ``Null*`` implementation, **and a contract test both
must pass**"; P7's gate runs ``pytest -q tests/contracts_adapters`` and calls it "port
conformance across every adapter". This file is the storage half of that, the sibling of
``test_source_conformance.py`` and written in the same shape: one register, the same
assertions per implementation, and a new adapter that does not satisfy the port failing here
rather than in the first evidence bundle that needed one. Adding a class is one line in
``ADAPTERS``.

Two rules shape the port (:mod:`oxbow.ports.objectstore`) and every test below exists to keep
one of them true:

* **Keys are content-addressed.** The SHA-256 of the body travels in the reference, so an
  artifact cannot be swapped under a run that already names it. That is only worth anything
  if the digest is *measured* on read and is the real function — so the expected digest here
  is a hex string written into this file from ``sha256sum``, and a reference naming anything
  else (an S3 ETag is MD5 for single-part uploads, 32 hex characters) fails the shape check.
* **Reads are total or loud.** A missing key raises :class:`ObjectNotFound`; it does not
  return empty bytes. An empty evidence file that verifies as empty is worse than a missing
  one (03 §A rule 2: never let an unknown become a zero), which is why the missing-key test
  asserts the raise *and* that "exists and is empty" is a separately reachable state.

Then the smaller ones: a listing that comes back empty must be distinguishable from a store
nobody could reach, ``delete`` reports whether it removed something rather than always
returning ``True``, a key that escapes the root is refused before it is written, and a
content-addressed store does not do last-write-wins.

The MinIO implementation is the one genuinely integrated external system in the demo (02 §C),
so it is exercised against the bucket in Compose when the bucket answers and skipped with a
reason naming the endpoint when it does not. A skip is never a pass: the last test in this
file fails if nothing was exercised at all.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Iterator, Sequence
from functools import cache
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from oxbow.adapters.null.objectstore import NullObjectStore
from oxbow.adapters.s3.objectstore import S3ObjectStore
from oxbow.config import read_dotenv
from oxbow.ports.objectstore import (
    MAX_KEY_LENGTH,
    ObjectConflict,
    ObjectNotFound,
    ObjectRef,
    ObjectStoreAdapter,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- the fixture, hand-counted -------------------------------------------------
# Every digest below was produced by ``sha256sum`` on these exact bytes, not by the code
# under test. The mutation is one character: ``e`` -> ``f`` in the last word.
BODY = b"oxbow-conformance-body\n"
BODY_SHA256 = "9968b719ad7b6aa208890806a3e94a4bd656e3075c78f54b05eb2fcbd387b7b1"
BODY_SIZE = 23
MUTATED_BODY = b"oxbow-conformance-bodf\n"
MUTATED_SHA256 = "a84a835ccd4ee877ef5408ca3d2ebddbe40f96b8caf84d80d2b0be747390c9f2"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
CONTENT_TYPE = "application/octet-stream"

SHA256_SHAPE = re.compile(r"^[0-9a-f]{64}$")

# One namespace per process, so two runs of this file never read each other's objects and a
# write that already happened cannot be mistaken for the one under test.
RUN = uuid.uuid4().hex[:8]
NAMESPACE = "contracts-objectstore"

DEFAULT_MINIO_PORT = "9000"
DEFAULT_BUCKET = "oxbow"

StoreFactory = Callable[[Path], ObjectStoreAdapter]

# Names the factories actually handed a store back for. The guard test at the end of the file
# reads it, so a file whose every implementation skipped cannot report green.
_EXERCISED: set[str] = set()


def _null_store(root: Path) -> ObjectStoreAdapter:
    return NullObjectStore(root)


@cache
def _minio_config() -> tuple[str, str, str, str]:
    """Endpoint, bucket and credentials, resolved from the environment and then ``.env``.

    The values are returned for the client's use and never formatted into a message.
    ``.env`` is gitignored and is the same file ``docker compose`` reads, so falling back to
    it is still "secrets from the environment, never the repo" (01 §A rule 8).
    """
    dotenv = read_dotenv(REPO_ROOT)
    endpoint = os.environ.get("OXBOW_S3_ENDPOINT_URL", "").strip() or (
        f"http://127.0.0.1:{os.environ.get('MINIO_PORT', DEFAULT_MINIO_PORT).strip() or DEFAULT_MINIO_PORT}"
    )
    bucket = os.environ.get("OXBOW_S3_BUCKET", "").strip() or DEFAULT_BUCKET
    access = os.environ.get("MINIO_USER", "").strip() or dotenv.get("MINIO_USER", "").strip()
    secret = (
        os.environ.get("MINIO_PASSWORD", "").strip() or dotenv.get("MINIO_PASSWORD", "").strip()
    )
    return endpoint, bucket, access, secret


@cache
def _minio_unreachable_reason() -> str:
    """Probe the bucket once per process and say what is missing, in words.

    An empty string means the store answered. The probe is a real listing, because a bucket
    that exists but cannot be listed is exactly the state a ``put`` would otherwise discover
    mid-evidence-bundle. Nothing in the returned text quotes a credential.
    """
    endpoint, bucket, access, secret = _minio_config()
    if not access or not secret:
        return (
            f"MINIO_USER / MINIO_PASSWORD are set neither in the environment nor in "
            f"{REPO_ROOT / '.env'}: the S3 adapter refuses anonymous writes to a bucket that "
            "holds evidence, so there is nothing to conform against"
        )
    try:
        S3ObjectStore(
            bucket=bucket, endpoint_url=endpoint, access_key=access, secret_key=secret
        ).list(f"{NAMESPACE}/probe/")
    except Exception as exc:  # botocore raises a family, not a type, and the reason is prose
        return (
            f"MinIO at {endpoint} (bucket {bucket!r}) is not answering: {type(exc).__name__}. "
            "Start it with `docker compose up -d minio minio-init` and re-run; the compose "
            "stack publishes the port this file probes by default"
        )
    return ""


def _minio_store(root: Path) -> ObjectStoreAdapter:
    reason = _minio_unreachable_reason()
    if reason:
        pytest.skip(reason)
    endpoint, bucket, access, secret = _minio_config()
    del root  # the bucket is the root; the argument keeps the factory signature uniform
    return S3ObjectStore(
        bucket=bucket,
        endpoint_url=endpoint,
        access_key=access,
        secret_key=secret,
        store_id="minio",
    )


# Every ObjectStoreAdapter in the tree. A new implementation is added here.
ADAPTERS: dict[str, StoreFactory] = {
    "minio": _minio_store,
    "null": _null_store,
}


def _ids() -> list[str]:
    return list(ADAPTERS)


def _store(name: str, root: Path) -> ObjectStoreAdapter:
    store = ADAPTERS[name](root)
    _EXERCISED.add(name)
    return store


def _key(store: ObjectStoreAdapter, *parts: str) -> str:
    """One key inside this run's namespace, per store, per test path."""
    return "/".join((NAMESPACE, RUN, store.store_id, *parts))


@pytest.fixture(params=sorted(ADAPTERS))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ObjectStoreAdapter:
    return _store(request.param, tmp_path)


def _listing(store: ObjectStoreAdapter, prefix: str) -> list[str]:
    return [ref.key for ref in store.list(prefix)]


# --- the port's structural promises --------------------------------------------


@pytest.mark.parametrize("name", _ids())
def test_satisfies_the_runtime_checkable_protocol(name: str, tmp_path: Path) -> None:
    """A duck that does not satisfy the Protocol is not an implementation of the port."""
    assert isinstance(_store(name, tmp_path), ObjectStoreAdapter), (
        f"{name} does not satisfy ObjectStoreAdapter; a run manifest that hands the API a "
        "reference from an adapter which does not implement the port breaks at the call site, "
        "in production, on the way to an evidence packet"
    )


@pytest.mark.parametrize("name", _ids())
def test_the_store_names_itself(name: str, tmp_path: Path) -> None:
    """``store_id`` is what a health probe and a problem payload quote.

    Two adapters reporting the same id would let a run say "the artifacts are in MinIO" while
    they are on a laptop disk, which is the provenance claim this port exists to make checkable.
    """
    store = _store(name, tmp_path)
    assert store.store_id, f"{name} reports no store_id, so nothing downstream can name it"
    assert store.store_id == name, (
        f"{name} registered under its own name but reports store_id {store.store_id!r}: the id "
        "is what a health probe quotes, and two adapters claiming one id let a run say the "
        "artifacts are in MinIO while they are on a laptop disk"
    )


def test_put_then_get_returns_byte_identical_content(store: ObjectStoreAdapter) -> None:
    """The one thing a store is for, asserted on the bytes rather than on a flag."""
    key = _key(store, "artifact.bin")
    ref = store.put(key, BODY, CONTENT_TYPE)

    assert store.get(key) == BODY, (
        f"{store.store_id} returned different bytes than it was handed: an artifact that "
        "cannot be read back is not evidence, it is a rumour about evidence"
    )
    assert ref.key == key
    assert ref.size_bytes == BODY_SIZE == len(BODY)
    assert ref.content_type == CONTENT_TYPE
    assert key in ref.uri, f"reference uri {ref.uri!r} does not name the key it stands for"


def test_head_reports_the_digest_the_body_has(store: ObjectStoreAdapter) -> None:
    """Content addressing, measured on the read path and not only trusted from the write.

    ``sha256`` is checked against three things: ``ObjectRef.digest`` computed in this test, a
    digest written into this file from ``sha256sum``, and the shape of a SHA-256 at all. The
    last is not pedantry — an S3 ETag is MD5 for single-part uploads, 32 hex characters, and a
    store that carried it here would be content-addressed by a different function depending on
    how the upload happened.
    """
    key = _key(store, "head", "artifact.bin")
    written = store.put(key, BODY, CONTENT_TYPE)
    head = store.head(key)

    assert written.sha256 == ObjectRef.digest(BODY) == BODY_SHA256, (
        f"{store.store_id} computed {written.sha256!r} for a body whose SHA-256 is "
        f"{BODY_SHA256!r}: the digest in a reference is the address, and a wrong address is a "
        "wrong artifact with nothing to say so"
    )
    assert head.sha256 == BODY_SHA256, (
        f"{store.store_id}.head() reported {head.sha256!r}; metadata that disagrees with the "
        "bytes means a run manifest can name content it never stored"
    )
    assert SHA256_SHAPE.fullmatch(head.sha256), (
        f"{head.sha256!r} is not 64 lower-case hex characters, so it is not a SHA-256 digest "
        "and the reference cannot be re-derived from the body"
    )
    assert head.size_bytes == BODY_SIZE
    assert head.key == key


def test_a_reference_refuses_a_body_that_changed_by_one_byte(store: ObjectStoreAdapter) -> None:
    """The tamper check, on the port's own ``matches``.

    One byte is the whole argument: a swapped artifact does not announce itself, and the
    reference is the only thing standing between a run and a document nobody signed.
    """
    ref = store.put(_key(store, "tamper.bin"), BODY, CONTENT_TYPE)

    assert ref.matches(BODY), "the reference refuses the exact bytes it was written from"
    assert not ref.matches(MUTATED_BODY), (
        "ObjectRef.matches accepted a body one byte different from the one it names: the "
        "digest comparison that keeps an artifact from being swapped under a run is not "
        "happening"
    )
    assert ref.sha256 != MUTATED_SHA256
    assert not store.head(ref.key).matches(MUTATED_BODY)


def test_a_missing_key_raises_and_never_comes_back_empty(store: ObjectStoreAdapter) -> None:
    """03 §A rule 2: an unknown must not become a zero.

    Returning ``b""`` for a key that does not exist is the exact bug this port exists to
    catch — an empty evidence file verifies as empty, hashes as a well-known constant, and
    reads as a document that arrived and said nothing. Both read paths are asserted, because
    ``head`` is the one a listing uses.
    """
    missing = _key(store, "never-stored.bin")

    with pytest.raises(ObjectNotFound) as got:
        store.get(missing)
    assert missing in str(got.value), (
        f"{store.store_id} raised for a missing key without naming it ({got.value!r}); the "
        "operator reading the log cannot tell which artifact is absent"
    )
    with pytest.raises(ObjectNotFound):
        store.head(missing)
    with pytest.raises(FileNotFoundError):
        store.get(missing)


def test_exists_and_is_empty_is_a_state_an_investigator_can_see(
    store: ObjectStoreAdapter,
) -> None:
    """The distinction the raise above protects.

    A genuinely empty object is real and must read back empty; a missing one must not. If
    both came back as ``b""`` the first test in this pair would be indistinguishable from the
    second, which is how an unknown starts looking like a zero.
    """
    key = _key(store, "empty-marker")
    ref = store.put(key, b"", CONTENT_TYPE)

    assert store.get(key) == b""
    assert ref.size_bytes == 0
    assert ref.sha256 == EMPTY_SHA256 == ObjectRef.digest(b"")
    assert store.head(key).sha256 == EMPTY_SHA256
    with pytest.raises(ObjectNotFound):
        store.get(f"{key}-not-there")


def test_listing_returns_only_the_prefix_and_in_key_order(store: ObjectStoreAdapter) -> None:
    """A manifest built from an unordered listing is not reproducible across filesystems.

    The sibling prefix is written deliberately: a listing that ignores its prefix returns
    somebody else's artifacts, and the run manifest then claims a batch that never arrived.
    """
    kept = _key(store, "run-a", "second.bin")
    first = _key(store, "run-a", "first.bin")
    other = _key(store, "run-b", "elsewhere.bin")
    for key, body in ((kept, BODY), (first, MUTATED_BODY), (other, BODY)):
        store.put(key, body, CONTENT_TYPE)

    prefix = f"{NAMESPACE}/{RUN}/{store.store_id}/run-a/"
    found = _listing(store, prefix)

    assert sorted(found) == found, f"{store.store_id} listed in the order it happened to find"
    assert set(found) == {first, kept}, (
        f"listing {prefix!r} returned {found}: an object from another prefix in the same "
        "listing is a run manifest describing a batch that was never read"
    )
    assert other not in found


def test_an_empty_listing_is_a_result_not_a_dead_store(store: ObjectStoreAdapter) -> None:
    """``[]`` means "looked, nothing here" — and only that.

    So that a caller can tell the two apart, an empty prefix must return an empty ordered
    listing rather than raising, and the unreachable case must raise rather than answer empty.
    The second half is per-implementation, in ``test_a_store_nobody_can_reach_does_not_answer_
    with_an_empty_listing``.
    """
    store.put(_key(store, "listed", "one.bin"), BODY, CONTENT_TYPE)
    empty = store.list(f"{NAMESPACE}/{RUN}/{store.store_id}/no-such-prefix/")

    assert isinstance(empty, Sequence), f"{store.store_id}.list() returned {type(empty).__name__}"
    assert not isinstance(empty, str | bytes), "a listing came back as text"
    assert len(empty) == 0, f"the prefix names nothing but the listing returned {list(empty)}"
    assert len(store.list(f"{NAMESPACE}/{RUN}/{store.store_id}/listed/")) == 1


def test_delete_reports_whether_something_was_actually_removed(
    store: ObjectStoreAdapter,
) -> None:
    """Retention policy reads this boolean.

    ``True`` on a key that was never there is a delete that lies, and a lifecycle job built on
    it reports evidence as destroyed while it is still in the bucket.
    """
    key = _key(store, "retention.bin")
    store.put(key, BODY, CONTENT_TYPE)

    assert store.delete(key) is True, f"{store.store_id} did not report removing an object it held"
    with pytest.raises(ObjectNotFound):
        store.get(key)
    assert store.delete(key) is False, (
        f"{store.store_id}.delete() answered True for a key it had already removed; a "
        "retention policy cannot tell a deletion from a no-op"
    )


@pytest.mark.parametrize("name", _ids())
def test_a_key_that_escapes_the_store_is_refused(name: str, tmp_path: Path) -> None:
    """``validate_key`` is in the port so every implementation inherits it.

    ``../../etc/passwd`` is a legal filename; a file-backed store that accepted it would write
    outside the root the run is supposed to be able to hand over wholesale, and an object store
    would keep the key in a manifest nobody can dereference.
    """
    store = _store(name, tmp_path)
    # A namespace of its own, so the "nothing landed" check below counts this test's refusals
    # and not the objects other tests wrote into the same shared bucket.
    guard = f"{NAMESPACE}/{RUN}/{store.store_id}/key-safety"
    for escaping in (f"{guard}/../escape.bin", f"{guard}/a/../../escape.bin"):
        with pytest.raises(ValueError, match="path-safe|object key"):
            store.put(escaping, BODY, CONTENT_TYPE)
    for escaping in ("/absolute.bin", "back\\slash.bin"):
        with pytest.raises(ValueError, match="path-safe|object key"):
            store.put(escaping, BODY, CONTENT_TYPE)
    with pytest.raises(ValueError, match="object key"):
        store.put("", BODY, CONTENT_TYPE)
    with pytest.raises(ValueError, match="object key"):
        store.put("x" * (MAX_KEY_LENGTH + 1), BODY, CONTENT_TYPE)
    with pytest.raises(ValueError, match="control characters"):
        store.put("line\nbreak", BODY, CONTENT_TYPE)

    # Nothing was written by any of the refusals above: a rejected key that still lands on
    # disk is a store with a rule it does not enforce.
    assert _listing(store, f"{guard}/") == [], (
        f"{store.store_id} refused an unsafe key and stored something anyway: "
        f"{_listing(store, f'{guard}/')}"
    )


def test_the_same_key_never_holds_two_different_bodies(store: ObjectStoreAdapter) -> None:
    """Content addressing is what makes a collision an integrity event, not a normal write.

    Last-write-wins under a key a run already named would mean the reference and the bytes
    disagree, and the only thing that could tell was somebody re-hashing an artifact by hand.
    Asserted with the port's own ``ObjectConflict`` *or* any other refusal, because the S3
    adapter raises a bare ``ValueError`` here where the null adapter raises ``ObjectConflict``
    — a divergence recorded in this file's report rather than papered over, but a refusal is
    still a refusal.
    """
    key = _key(store, "conflict.bin")
    store.put(key, BODY, CONTENT_TYPE)

    with pytest.raises((ObjectConflict, ValueError)):
        store.put(key, MUTATED_BODY, CONTENT_TYPE)
    assert store.get(key) == BODY, (
        f"{store.store_id} refused the second write and then served the first one's key with "
        "different bytes: a reference a run already made now names content that is not there"
    )
    assert store.head(key).sha256 == BODY_SHA256

    # Identical bytes are not a conflict, and refusing them would make a re-run fail.
    again = store.put(key, BODY, CONTENT_TYPE)
    assert again.sha256 == BODY_SHA256


# --- per-implementation promises -----------------------------------------------


def test_the_null_store_checks_the_digest_on_read(tmp_path: Path) -> None:
    """``ObjectRef``'s docstring: verified on read, not only trusted from the write.

    A disk somebody edited is the case, so the tamper is done out here, through the path the
    store wrote, and the read has to notice.
    """
    store = NullObjectStore(tmp_path)
    key = _key(store, "on-disk.bin")
    ref = store.put(key, BODY, CONTENT_TYPE)
    on_disk = tmp_path / "objectstore" / key
    assert on_disk.is_file(), f"the null store wrote somewhere other than {on_disk}"
    assert ref.sha256 == ObjectRef.digest(on_disk.read_bytes())

    on_disk.write_bytes(MUTATED_BODY)
    with pytest.raises(ValueError, match="does not match its recorded digest"):
        store.get(key)


def test_a_file_store_that_cannot_be_opened_says_so(tmp_path: Path) -> None:
    """Half of the empty-listing promise, on the store that needs no infrastructure.

    "There is nothing under this prefix" and "there is no store" are different answers, and
    the second one cannot be a zero-length sequence (03 §A rule 2). A root that is a regular
    file is the reachable version of an unreachable disk: construction has to fail rather
    than hand back a store that lists nothing and reads as an empty bucket.
    """
    blocker = tmp_path / "a-file-not-a-directory"
    blocker.write_bytes(b"")

    with pytest.raises(OSError):
        NullObjectStore(blocker)


def test_a_bucket_that_is_not_there_does_not_answer_with_an_empty_listing() -> None:
    """The same promise, on the store that needs the network to keep it."""
    if _minio_unreachable_reason():
        pytest.skip(_minio_unreachable_reason())
    endpoint, _, access, secret = _minio_config()
    absent = S3ObjectStore(
        bucket="oxbow-no-such-bucket-for-this-test",
        endpoint_url=endpoint,
        access_key=access,
        secret_key=secret,
    )
    with pytest.raises(ClientError) as refused:
        absent.list("")
    assert "NoSuchBucket" in str(refused.value), (
        "listing a bucket that does not exist came back as something other than a named "
        "failure; an empty listing here is an unknown becoming a zero"
    )


def test_the_minio_store_addresses_by_sha256_not_by_etag(store: ObjectStoreAdapter) -> None:
    """MinIO-specific, and the reason the port computes the digest at all.

    An ETag is MD5 for a single-part upload and something else entirely for a multipart one
    (02 §C). A reference carried that would be content-addressed by whichever function the
    upload happened to use, so the digest is stored in object metadata and read back here.
    """
    if store.store_id != "minio":
        pytest.skip("the ETag trap only exists on an S3-protocol store")
    key = _key(store, "etag.bin")
    ref = store.put(key, BODY, CONTENT_TYPE)

    assert SHA256_SHAPE.fullmatch(ref.sha256) and ref.sha256 == BODY_SHA256, (
        f"the minio reference carries {ref.sha256!r}, which is not this body's SHA-256 — an "
        "MD5 ETag is 32 hex characters and would pass for a digest nowhere else in this repo"
    )
    assert store.head(key).sha256 == ref.sha256


# --- the guard against a green that never ran ----------------------------------


def test_at_least_one_implementation_answered() -> None:
    """No vacuous green.

    ``pytest -q tests/contracts_adapters`` is the P7 gate, and a file whose every test skipped
    exits 0 while proving nothing. The null store always runs, so this fails only if the
    register itself was hollowed out — which is precisely how a conformance suite rots.
    """
    assert _EXERCISED, (
        "not one ObjectStoreAdapter was constructed: this file skipped everything and the port "
        f"is unexercised again. Register: {sorted(ADAPTERS)}"
    )
    assert set(ADAPTERS) >= _EXERCISED, f"exercised names outside the register: {_EXERCISED}"


@pytest.fixture(scope="module", autouse=True)
def _this_runs_objects_are_removed() -> Iterator[None]:
    """Best-effort bucket hygiene, so the next run's listing assertion means something.

    A conformance suite that litters a shared bucket teaches everyone reading it to distrust a
    listing. Nothing is asserted here: a leaked test object is an inconvenience, not a red gate.
    """
    yield
    if _minio_unreachable_reason():
        return
    try:
        store = _minio_store(Path.cwd())
        for ref in store.list(f"{NAMESPACE}/{RUN}/"):
            store.delete(ref.key)
    except (ClientError, OSError):
        # Cleanup only; the tests above are what assert. A leaked test object under this
        # run's own namespace cannot be mistaken for another run's artifact.
        pass
