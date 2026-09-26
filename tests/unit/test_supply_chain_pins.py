"""02 §F / plan T4: container images are pinned by digest, and `make lint` says so.

The rule is `image: repo:tag@sha256:<64 hex>` -- tag *and* digest, because a bare
digest is unreadable in review and a bare tag is mutable: anyone with write access to
the upstream repository can move `:26.0` to different bytes, and the supply-chain claim
the plan makes is then false.

Two MinIO images are listed in `KNOWN_UNRESOLVED` rather than deleted, and the reason is
recorded in `docker-compose.yml`: quay.io grants an anonymous pull token whose
`actions` list is empty, so their manifest digest cannot be fetched without a
credential, and guessing one would be worse than admitting the gap. The allowlist is
checked against the file, so removing the exception without removing the entry -- or
pinning it and leaving the entry -- both fail.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"

IMAGE_LINE: re.Pattern[str] = re.compile(r"^\s*image:\s*(?P<ref>\S+)\s*$", re.MULTILINE)
# `repo[:tag]@sha256:<64hex>`, with or without a registry host. The first draft of
# this pattern required an algorithm-prefixed form (`@sha256-sha256:`), so it rejected
# every correctly pinned image in the file and would have "passed" only by failing to
# recognise anything -- which is why the pattern has its own positive case below.
PINNED: re.Pattern[str] = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")

# Images whose digest could not be resolved anonymously, with the reason the exception
# is legitimate. Kept as data so the test can demand a documented reason per entry.
KNOWN_UNRESOLVED: dict[str, str] = {
    "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z": (
        "quay.io grants an anonymous token whose claims list actions: [] for this "
        "repository, so the manifest digest needs a credential to resolve"
    ),
    "minio/mc:RELEASE.2024-08-13T05-33-17Z": (
        "unresolvable on both registries tried: docker.io answers insufficient_scope / "
        "does-not-exist for this tag and quay.io blocks anonymous manifest reads, so the "
        "reference itself needs correcting as well as pinning -- recorded as a finding, "
        "not silently dropped, and unverifiable further while the engine is down"
    ),
}


def _refs(text: str) -> list[str]:
    return [m.group("ref") for m in IMAGE_LINE.finditer(text)]


def test_every_compose_image_is_digest_pinned_or_explicitly_excused() -> None:
    refs = _refs(COMPOSE.read_text(encoding="utf-8"))
    assert refs, "no `image:` lines found; this gate would pass by reading nothing"
    unpinned = [ref for ref in refs if not PINNED.match(ref)]
    unknown = [ref for ref in unpinned if ref not in KNOWN_UNRESOLVED]
    assert not unknown, (
        "these images are mutable-by-upstream and not digest-pinned, and carry no "
        f"documented reason: {unknown}. Resolve with "
        "`docker buildx imagetools inspect <ref> --format '{{json .Digest}}'` and pin as "
        "`repo:tag@sha256:...`"
    )


def test_the_exception_list_matches_the_file() -> None:
    """An excuse no longer in use must be deleted, not left to absorb the next slip."""
    refs = set(_refs(COMPOSE.read_text(encoding="utf-8")))
    stale = sorted(ref for ref in KNOWN_UNRESOLVED if ref not in refs)
    assert not stale, f"KNOWN_UNRESOLVED lists images that are no longer used: {stale}"
    for ref, reason in KNOWN_UNRESOLVED.items():
        assert len(reason) > 40, f"{ref}: the exception needs a real reason, not a stub"
        assert ref in COMPOSE.read_text(encoding="utf-8")


def test_a_bare_tag_is_rejected_by_the_pattern() -> None:
    """Prove the check can fail: it must reject exactly the shape it exists to catch."""
    for bad in (
        "postgres:16.4-alpine",
        "quay.io/keycloak/keycloak:26.0",
        "redis@sha256:short",
        "img@sha512:" + "a" * 128,
    ):
        assert not PINNED.match(bad), f"{bad} must not satisfy the pinning rule"
    good = "postgres:16.4-alpine@sha256:" + "5" * 64
    assert PINNED.match(good), good
    dual = "registry.example.com:5000/team/app:1.2.3@sha256:" + "a" * 64
    assert PINNED.match(dual), "a private registry with a port must still be pinnable"
