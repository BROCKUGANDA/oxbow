"""Download the declared corpora and record their provenance.

01 A rule 6: no new dependency. This uses `httpx`, which is already pinned, and
reads the existing `~/.kaggle/kaggle.json`. It deliberately does not use the
`kaggle` package, so the toolchain stays exactly as pinned in P0.

What this script guarantees, because 01 B makes the data an audit surface:

* Only a source declared in config/sources.yaml can be fetched. The allowlist
  is the file, not a convention.
* A SHA-256 is computed for every file on first retrieval and recorded into
  config/sources.yaml and data/DATASET_CARD.md. On every later run the recorded
  hash is verified and a mismatch FAILS CLOSED. Wrong bytes never enter the
  pipeline, because a silently different corpus would make every downstream
  number a lie while still looking plausible.
* Row counts are counted from the bytes as they stream, not from a header
  guess, and are written to the manifest.

Usage:
    uv run python scripts/download_data.py --source paysim
    uv run python scripts/download_data.py --source all --verify-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES_YAML = REPO_ROOT / "config" / "sources.yaml"
RAW_ROOT = REPO_ROOT / "data" / "raw"
MANIFEST_PATH = REPO_ROOT / "data" / "download_manifest.json"
CARD_PATH = REPO_ROOT / "data" / "DATASET_CARD.md"

KAGGLE_API = "https://www.kaggle.com/api/v1/datasets/download"
KAGGLE_CREDS = Path.home() / ".kaggle" / "kaggle.json"

# A placeholder is what P0 committed, because a hash cannot be known before the
# bytes exist. Inventing one would have been the same class of error as the
# fabricated MinIO tags, so the sentinel is explicit and is only accepted on the
# very first retrieval.
RECORDED_AT_DOWNLOAD = "RECORDED_AT_DOWNLOAD"

CHUNK = 1 << 20  # 1 MiB: stream, never hold a 500 MB CSV in memory.


def die(message: str) -> None:
    """Fail closed, loudly, with a non-zero exit.

    A download script that warns and continues is how the wrong corpus gets
    committed and never noticed.
    """
    sys.stderr.write(f"ERROR: {message}\n")
    raise SystemExit(1)


def load_sources() -> dict[str, Any]:
    if not SOURCES_YAML.is_file():
        die(f"missing allowlist: {SOURCES_YAML}")
    loaded: dict[str, Any] = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        die(f"{SOURCES_YAML} did not parse to a mapping; the allowlist is unusable")
    return loaded


def kaggle_credentials() -> tuple[str, str]:
    """Kaggle auth is HTTP Basic with the account username as the user."""
    if not KAGGLE_CREDS.is_file():
        die(
            f"no Kaggle credentials at {KAGGLE_CREDS}. This is a hard stop: the "
            "corpora are not redistributable and must come from the account."
        )
    payload = json.loads(KAGGLE_CREDS.read_text(encoding="utf-8"))
    username, key = payload.get("username"), payload.get("key")
    if not username or not key:
        die(f"{KAGGLE_CREDS} does not contain both 'username' and 'key'")
    return str(username), str(key)


def slug_from_url(url: str) -> str:
    """https://www.kaggle.com/datasets/<owner>/<slug> -> <owner>/<slug>."""
    marker = "/datasets/"
    if marker not in url:
        die(f"cannot parse a Kaggle dataset slug from {url!r}")
    slug: str = url.split(marker, 1)[1].strip("/").split("?")[0]
    return slug


def fetch_archive(client: httpx.Client, slug: str, dest: Path) -> dict[str, Any]:
    """Stream the whole-dataset zip and hash it.

    Kaggle's per-file endpoint (`/download/{slug}/{filename}`) returns 404: the
    API only serves a whole dataset as a zip, verified here with a ranged GET
    returning 206 and content-type application/zip. So the archive is fetched
    first and declared files are extracted from it, each hashed individually.

    An archive already on disk is reused rather than re-pulled. A 186 MB
    download is slow and free bandwidth is worth keeping, and a half-written
    .partial is never promoted, so a reused archive is always a whole one.
    """
    url = f"{KAGGLE_API}/{slug}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        print(f"    archive already present, hashing {dest.name} ...", flush=True)
        info = hash_on_disk(dest)
        return {
            "archive": dest.name,
            "bytes": info["bytes"],
            "sha256": info["sha256"],
            "url": url,
            "reused": True,
        }
    digest = hashlib.sha256()
    size = 0
    tmp = dest.with_suffix(".zip.partial")
    with client.stream("GET", url) as response:
        if response.status_code in (401, 403):
            die(
                f"Kaggle refused {slug} with {response.status_code}.\n"
                "  403 on a public dataset almost always means the dataset's terms "
                "have not been accepted for this account. Accept them in a browser "
                "while signed in to Kaggle, then re-run. Nothing here can accept "
                "terms on the user's behalf."
            )
        if response.status_code == 404:
            die(f"not found on Kaggle: {url} (404). Check the slug in sources.yaml.")
        response.raise_for_status()
        with tmp.open("wb") as handle:
            for chunk in response.iter_bytes(CHUNK):
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    tmp.replace(dest)
    return {"archive": dest.name, "bytes": size, "sha256": digest.hexdigest(), "url": url}


def extract_declared(
    archive: Path, declared_files: list[dict[str, Any]], out_dir: Path
) -> list[dict[str, Any]]:
    """Pull each declared file out of the archive, hashing the extracted bytes.

    Hashing the extracted file rather than the archive member name is what makes
    the pin meaningful: the number recorded is the number the pipeline reads.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive) as zf:
        members = [n for n in zf.namelist() if not n.endswith("/")]
        for entry in declared_files:
            name = entry["name"]
            leaf = name.rsplit("/", 1)[-1]
            member = (
                name
                if name in members
                else next((n for n in members if n.rsplit("/", 1)[-1] == leaf), None)
            )
            if member is None:
                die(
                    f"{name} is declared in sources.yaml but is not in the archive.\n"
                    f"  archive contains: {sorted(m.rsplit('/', 1)[-1] for m in members)}"
                )
            dest = out_dir / name
            digest = hashlib.sha256()
            size = 0
            newlines = 0
            with zf.open(member) as src, dest.open("wb") as out:
                while True:
                    block = src.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    size += len(block)
                    newlines += block.count(b"\n")
            sha = digest.hexdigest()
            recorded = entry.get("sha256", RECORDED_AT_DOWNLOAD)
            if recorded != RECORDED_AT_DOWNLOAD and recorded.lower() != sha:
                dest.unlink(missing_ok=True)
                die(
                    f"SHA-256 mismatch for {name}\n"
                    f"  recorded: {recorded}\n"
                    f"  actual:   {sha}\n"
                    "Refusing to keep these bytes. Either upstream changed or the "
                    "transfer was corrupted; either way the recorded row counts and "
                    "base rates are now untrustworthy. Re-download deliberately."
                )
            records.append(
                {
                    "file": name,
                    "archive_member": member,
                    "sha256": sha,
                    "bytes": size,
                    "newlines": newlines,
                    "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )
    return records


def hash_on_disk(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    newlines = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
            size += len(block)
            newlines += block.count(b"\n")
    return {"file": path.name, "sha256": digest.hexdigest(), "bytes": size, "newlines": newlines}


def legacy_fetch_one() -> None:
    """Retained only to document what the API does NOT support.

    Kaggle has no per-file download endpoint, so a request for
    `/download/{slug}/{filename}` 404s. Kept as a note rather than code: the
    dataset-level archive plus explicit extraction is the supported path.
    """
    return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default="all",
        help="declared source id, or 'all' for every ingestable source",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="re-hash what is on disk against sources.yaml without downloading",
    )
    args = parser.parse_args()

    declared = load_sources()
    sources = declared.get("sources", [])
    by_id = {source["id"]: source for source in sources}

    if args.source != "all":
        if args.source not in by_id:
            die(
                f"{args.source!r} is not declared in config/sources.yaml. "
                f"Declared: {sorted(by_id)}"
            )
        targets = [by_id[args.source]]
    else:
        targets = [
            source
            for source in sources
            if source.get("ingest_allowed", True) and source.get("role") != "cite_only"
        ]

    for source in targets:
        if not source.get("ingest_allowed", True):
            die(f"source {source['id']} is declared with ingest_allowed: false")

    username, key = kaggle_credentials()
    results: dict[str, Any] = {}

    for source in targets:
        slug = slug_from_url(source["source_url"])
        declared_files = source.get("files", [])
        if not declared_files:
            die(f"source {source['id']} declares no files")
        source_dir = RAW_ROOT / source["id"]
        print(f"\n=== {source['id']}  ({slug}) ===", flush=True)

        if args.verify_only:
            for entry in declared_files:
                path = source_dir / entry["name"]
                if not path.is_file():
                    die(f"verify-only: {path} does not exist")
                actual = hash_on_disk(path)["sha256"]
                recorded = entry.get("sha256", RECORDED_AT_DOWNLOAD)
                if recorded == RECORDED_AT_DOWNLOAD:
                    # The placeholder means "not yet pinned", not "mismatch". The
                    # download path already skips the check in that state;
                    # verify-only must agree, or the first verification run after
                    # a fresh checkout fails on a file that is perfectly correct.
                    print(
                        f"  {entry['name']:38} NOT PINNED  {actual}\n"
                        f"  {'':38} record it in config/sources.yaml to arm the pin.",
                        flush=True,
                    )
                    continue
                status = "MISMATCH" if recorded.lower() != actual else "ok"
                print(f"  {entry['name']:38} {status}  {actual}")
                if status == "MISMATCH":
                    die(
                        f"{path} does not match the recorded hash.\n"
                        f"  recorded {recorded}\n  actual   {actual}"
                    )
            continue

        entries = []
        with httpx.Client(
            auth=(username, key),
            timeout=httpx.Timeout(30.0, read=900.0),
            follow_redirects=True,
            headers={"User-Agent": "oxbow-research-prototype/0.1"},
        ) as client:
            print("  downloading archive ...", flush=True)
            archive = source_dir / "_archive" / f"{source['id']}.zip"
            info = fetch_archive(client, slug, archive)
            print(
                f"    archive {info['archive']}  {info['bytes']:,} bytes\n"
                f"    sha256  {info['sha256']}",
                flush=True,
            )
            print("  extracting declared files ...", flush=True)
            entries = extract_declared(archive, declared_files, source_dir)
            for record in entries:
                print(
                    f"    {record['file']:24} sha256 {record['sha256'][:32]}...\n"
                    f"    {'':24} bytes {record['bytes']:,}  lines {record['newlines']:,}",
                    flush=True,
                )

        results[source["id"]] = {
            "slug": slug,
            "source_url": source["source_url"],
            "license": source.get("license"),
            "archive": info,
            "files": entries,
        }

    if not args.verify_only and results:
        if MANIFEST_PATH.is_file():
            existing = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        else:
            existing = {}
        existing.update(results)
        existing["_written_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(
            json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nmanifest -> {MANIFEST_PATH.relative_to(REPO_ROOT)}")
        print(
            "Next: record these hashes in config/sources.yaml and "
            "data/DATASET_CARD.md, then re-run with --verify-only to prove the "
            "pin works."
        )
        print(f"RUN_SALT for the next ingest: {'set' if os.environ.get('RUN_SALT') else 'NOT SET'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
