"""Recorded artifact paths must survive being written by one checkout and read by another.

The container image runs the pipeline with its repo at ``/srv`` and the host runs the same
commands at ``C:\\Users\\HP\\Desktop\\OXBOW``. Both read and write the same
``data/interim/<source>/run_manifest.json`` through a bind mount, so a manifest written
inside the container is read by the host and vice versa. That only works if the path
recorded per batch is relative to the repo root.

Recording the absolute path looked correct and was not: the container wrote
``/srv/data/interim/paysim/<batch>.parquet``, the host read that manifest, found no
literal ``/srv`` directory, and refused the run with "is declared by paysim's run
manifest but is absent on disk" -- a 6.4M-event corpus that had already been ingested
successfully, discarded by a path string. These tests pin the relative form on both the
write and the read side.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from oxbow.adapters.file.canonical_sink import (
    RUN_MANIFEST_FILENAME,
    CanonicalSink,
    _relative_to_root,
    _repo_root_for,
)
from oxbow.cli import _read_lineage
from oxbow.ingest.canonical import RunIdentity
from oxbow.ingest.paysim import canonicalize_batch
from tests.contracts.conftest import (
    TEST_BATCH_ID,
    TEST_SALT,
    base_row,
    raw_frame,
)

SOURCE = "paysim"
BATCH_ID = TEST_BATCH_ID
TEST_RUN_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
TEST_INGESTED_AT = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC).isoformat()


def _determinism() -> dict[str, Any]:
    """The determinism block the sink requires before it will write anything.

    The sink raises SinkConfigError rather than writing with unpinned Parquet knobs, so a
    test cannot leave this out and still get a file on disk to assert against. It is read
    from the shipped config rather than hand-written so this test cannot drift from the
    knobs the pipeline actually pins.
    """
    from oxbow.config import load_pipeline_config

    return load_pipeline_config().determinism


def _write_one_batch(root: Path) -> dict[str, Any]:
    """Write one batch through the real sink and return the record it produces.

    The frame is built by `canonicalize_batch` -- the same call the real ingest makes --
    because the sink enforces persisted canonical v1 strictly: a hand-built frame is
    refused for carrying undeclared columns, which is the contract working, not an
    obstacle to route around.
    """
    sink = CanonicalSink.for_repo(root, out_override=None, determinism=_determinism())
    identity = RunIdentity(run_salt=TEST_SALT, batch_id=BATCH_ID)
    result = canonicalize_batch(
        raw_frame([dict(base_row())]),
        identity,
        deployment_tz=ZoneInfo("Africa/Kampala"),
        epoch_utc=datetime(2014, 1, 1, tzinfo=UTC),
        step_hours=24,
        offset_modulus_us=86_400_000_000,
        offset_salt="oxbow-paysim-step-v1",
        ingested_at=datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC),
    )
    return dict(sink.write_batch(SOURCE, BATCH_ID, result.events))


def _manifest_payload(record: dict[str, Any]) -> dict[str, Any]:
    """A run manifest in exactly the shape cli.py:_read_lineage reads.

    Built by hand because these tests are *posing* manifests -- an absolute path from an
    older build, a path pointing at a file that is not there -- shapes the real writer no
    longer produces. Every key the reader requires is present, because it reads them
    unconditionally (cli.py:526-535): `source.source_id`, `run_id`, `canonical_count`,
    `quarantine_count` and `ingested_at`. The last is a real ISO-8601 instant, not a
    string that merely looks like one -- `_parse_instant` refuses anything else.
    """
    return {
        "source": {"source_id": SOURCE},
        "run_id": TEST_RUN_ID,
        "canonical_count": record["rows"],
        "quarantine_count": 0,
        "ingested_at": TEST_INGESTED_AT,
        "batches": [record],
    }


def _write_manifest(root: Path, payload: dict[str, Any]) -> Path:
    """Write a run manifest by hand, so a test can pose a path shape no writer produces now."""
    directory = root / "data" / "interim" / SOURCE
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / RUN_MANIFEST_FILENAME
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def test_a_written_batch_path_is_relative_to_the_repo_root(tmp_path: Path) -> None:
    """The recorded path starts with `data/interim`, not the absolute checkout path.

    This is the assertion that would have caught the /srv bug at the moment it was
    introduced, rather than 20 minutes later when a host-side graph stage could not find
    a corpus it had itself produced.
    """
    record = _write_one_batch(tmp_path)
    recorded = str(record["path"])
    assert not Path(recorded).is_absolute(), (
        f"batch path {recorded!r} is absolute, so the manifest only resolves on the machine "
        "that wrote it. Record it relative to the repo root."
    )
    assert recorded == f"data/interim/{SOURCE}/{BATCH_ID}.parquet"
    assert (tmp_path / recorded).is_file()


def test_the_relative_path_resolves_against_the_root_it_was_written_under(
    tmp_path: Path,
) -> None:
    """root / recorded_path is the file the sink just wrote.

    Guards the round trip rather than the string: a relative path is only correct if the
    reader's join lands on the real bytes, and the reader joins against the repo root it
    was handed.
    """
    record = _write_one_batch(tmp_path)
    resolved = tmp_path / str(record["path"])
    assert resolved.is_file()
    assert resolved.stat().st_size > 0


def test_a_manifest_written_at_one_root_is_readable_at_another(tmp_path: Path) -> None:
    """The container/host handoff: /srv on the writing side, a checkout on the reading side.

    Two roots stand in for the two places this tree runs. The manifest written under
    `container_root` is read under `host_root` -- which is only possible because the path
    is relative. Asserting the read returns the batch is the test that fails loudly
    instead of producing a corpus that quietly counts zero files.
    """
    container_root = tmp_path / "container"
    host_root = tmp_path / "host"
    for root in (container_root, host_root):
        (root / "data" / "interim" / SOURCE).mkdir(parents=True)

    record = _write_one_batch(container_root)
    payload = _manifest_payload(record)
    _write_manifest(container_root, payload)
    # A bind mount puts the same files under both roots, so the reader sees the writer's
    # manifest and the writer's bytes side by side -- which is the whole point: the only
    # thing that can make this readable is the relative path in the manifest.
    _write_manifest(host_root, payload)

    # A bind mount is what puts the bytes where the reading side looks for them.
    (host_root / "data" / "interim" / SOURCE / f"{BATCH_ID}.parquet").write_bytes(
        (container_root / "data" / "interim" / SOURCE / f"{BATCH_ID}.parquet").read_bytes()
    )

    lineages = _read_lineage(host_root, sources=[SOURCE])
    assert len(lineages) == 1
    assert [b.batch_id for b in lineages[0].batches] == [BATCH_ID]
    assert lineages[0].batches[0].path.is_file()


def test_an_absolute_path_from_an_older_manifest_is_still_honoured(tmp_path: Path) -> None:
    """Manifests written before this fix still resolve instead of becoming unreadable.

    A relative-only reader would reject every manifest already on disk, which would be a
    migration nobody asked for. The absolute form is unambiguous, so it is taken as
    written; only the relative form is joined to the root.
    """
    record = _write_one_batch(tmp_path)
    batch = tmp_path / str(record["path"])
    _write_manifest(
        tmp_path,
        {**_manifest_payload(record), "batches": [{**record, "path": batch.as_posix()}]},
    )

    lineages = _read_lineage(tmp_path, sources=[SOURCE])
    assert lineages[0].batches[0].path == batch


def test_a_missing_batch_is_still_refused_rather_than_skipped(tmp_path: Path) -> None:
    """Resolving against the root must not weaken the truncated-corpus guard.

    Joining a relative path is a new way to end up pointing at a file that is not there, so
    this pins the two halves that matter. `_read_lineage` only parses: it must resolve the
    relative path against the root to the location the file *would* occupy, and the file
    must genuinely not be there. The existence guard itself lives one layer up, in
    `_load_canonical_events` (cli.py:594), which takes a `StageContext` rather than a root
    and is exercised by the stage tests -- so asserting it here would test the wrong
    function and pass for the wrong reason.
    """
    record = _write_one_batch(tmp_path)
    _write_manifest(
        tmp_path,
        {
            **_manifest_payload(record),
            "batches": [{**record, "path": f"data/interim/{SOURCE}/gone.parquet"}],
        },
    )

    resolved = _read_lineage(tmp_path, sources=[SOURCE])[0].batches[0].path

    assert resolved == tmp_path / "data" / "interim" / SOURCE / "gone.parquet"
    assert (
        not resolved.is_file()
    ), "the batch this manifest names exists, so the test is not posing a truncated corpus"


def test_repo_root_is_the_parent_of_the_interim_directory(tmp_path: Path) -> None:
    """The derivation answers for the shape the sink is actually constructed with.

    Callers build `CanonicalSink(interim_root=...)` directly as well as through
    `for_repo`, and the direct calls do not use a `data/interim` layout. The batch path
    is recorded relative to the directory *containing* the per-source subdirectories, so
    the answer is its parent -- anything keying off a literal directory name silently
    returns the wrong base for half the callers.
    """
    assert _repo_root_for(tmp_path / "data" / "interim") == tmp_path
    assert _repo_root_for(tmp_path / "interim") == tmp_path
    # A relocated interim directory outside data/ still resolves to the directory holding
    # it, which is the only guess available when the caller did not say where the repo is.
    assert _repo_root_for(tmp_path / "elsewhere" / "interim") == tmp_path / "elsewhere"


def test_a_path_outside_the_repo_falls_back_to_absolute(tmp_path: Path) -> None:
    """An artifact genuinely outside the repo is recorded absolutely, not as a broken `../..`.

    Relative would be shorter and would not resolve: the reader joins against the repo
    root, so a `../../elsewhere/x.parquet` only opens if the depth happens to line up.
    Absolute always opens on the machine that wrote it, which is the honest outcome for a
    path the repo does not contain.
    """
    outside = tmp_path / "outside" / "x.parquet"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"")
    root = tmp_path / "repo"

    assert _relative_to_root(outside, root) == outside.as_posix()
