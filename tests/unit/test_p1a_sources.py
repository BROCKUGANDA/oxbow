"""P1a tests: the acquisition allowlist and the integrity pin.

01 B makes the data an audit surface, and 02 D makes a batch atomic. Both are
only worth anything if the guarantees are executable, so they are here rather
than in a README nobody re-reads before a demo.

These tests deliberately do NOT require the 493 MB corpus to be present. A
fresh clone has no data, and a test suite that fails on a clean checkout is a
test suite that gets skipped. The pin is asserted against config/sources.yaml
and the manifest; where the real file is present, the hash is verified too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_YAML = REPO_ROOT / "config" / "sources.yaml"
MANIFEST = REPO_ROOT / "data" / "download_manifest.json"
DECISIONS = REPO_ROOT / "DECISIONS.md"
MEASUREMENT = REPO_ROOT / "data" / "graph_measurement.json"
PAYSIM_CSV = REPO_ROOT / "data" / "raw" / "paysim" / "PS_20174392719_1491204439457_log.csv"
PLACEHOLDER = "RECORDED_AT_DOWNLOAD"


def load_sources() -> dict:
    loaded = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def source_by_id(source_id: str) -> dict:
    for source in load_sources()["sources"]:
        if source["id"] == source_id:
            return source
    pytest.fail(f"source {source_id!r} is not declared in sources.yaml")


# --- the allowlist --------------------------------------------------------


def test_every_source_carries_a_license_and_a_citation() -> None:
    """01 B: a source without license and citation is not a declared source.

    Checked including cite-only entries: being cited in a README is still
    attribution, and a licence typo in a doc is still a licence violation.
    """
    for source in load_sources()["sources"]:
        assert source.get("license"), f"{source['id']} has no license"
        assert source.get("citation"), f"{source['id']} has no citation"
        assert source.get("source_url", "").startswith(
            "https://"
        ), f"{source['id']} needs an https source_url"


def test_every_ingestable_source_states_its_obligation() -> None:
    """A licence name alone is not a licence.

    Share-alike and no-derivatives change what we may publish, so each source
    must say what it obliges us to do, in words an operator can act on.
    """
    for source in load_sources()["sources"]:
        if source.get("role") == "cite_only" or source.get("ingest_allowed") is False:
            continue
        obligation = source.get("license_obligation", "")
        assert len(obligation) > 40, f"{source['id']} does not explain its obligation"


def test_live_transaction_sources_are_refused_explicitly() -> None:
    """01 B: historical, public or de-identified data only.

    Asserted positively so that deleting the refusal is a visible test failure
    rather than the absence of a test nobody notices.
    """
    refused = {entry["id"] for entry in load_sources().get("refused", [])}
    assert "any_live_source" in refused, "live sources must stay on the refused list"
    assert "ieee_cis" in refused


def test_no_derived_source_is_ingested() -> None:
    """CC BY-NC-ND forbids derivatives outright.

    Elliptic is cited as related work and must never be read by the pipeline:
    a sampled derivative would violate the licence, which is a different and
    much worse problem than an unused citation.
    """
    elliptic = source_by_id("elliptic")
    assert elliptic.get("ingest_allowed") is False
    assert elliptic["role"] == "cite_only"
    assert "ND" in elliptic["license"] or "No-Derivatives" in elliptic["license"]


def test_sources_yaml_is_the_allowlist_not_a_convention() -> None:
    """The downloader reads this file to decide what it may fetch.

    So the file must be the single declaration point, and the id set must match
    what the script's own parser accepts.
    """
    sys_path = REPO_ROOT / "scripts" / "download_data.py"
    assert sys_path.is_file(), "scripts/download_data.py must exist"
    text = sys_path.read_text(encoding="utf-8")
    assert "SOURCES_YAML" in text
    assert "RECORDED_AT_DOWNLOAD" in text, "the not-yet-pinned sentinel must be explicit"
    # The script must not carry a hardcoded corpus that bypasses the allowlist.
    assert "ealaxi" not in text, "corpus identifiers belong in config, not in the script"
    assert "ealtman" not in text, "corpus identifiers belong in config, not in the script"


# --- the integrity pin ----------------------------------------------------


def test_retrieved_files_declare_a_real_sha256() -> None:
    """Every file that has actually been retrieved carries a real pin.

    Scoped to sources with a manifest entry rather than to every declared
    source, because a source that has not been downloaded yet cannot have a
    hash, and pretending otherwise would mean writing a hash we have never
    measured. `test_unpinned_sources_are_explicit_and_named` covers the rest.
    """
    if not MANIFEST.is_file():
        pytest.skip("no download run on this checkout")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    retrieved = {key for key in manifest if not key.startswith("_")}
    assert retrieved, "the manifest lists no retrieved source"

    for source_id in sorted(retrieved):
        declared = {f["name"]: f.get("sha256", "") for f in source_by_id(source_id)["files"]}
        for name, recorded in declared.items():
            assert recorded, f"{source_id}/{name} has no sha256"
            assert recorded != PLACEHOLDER, (
                f"{source_id}/{name} has been retrieved but still carries the "
                "placeholder: the pin is only real once the hash is recorded"
            )
            assert len(recorded) == 64, f"{name}: sha256 must be 64 hex chars"
            assert all(char in "0123456789abcdef" for char in recorded.lower())


def test_unpinned_sources_are_explicit_and_named() -> None:
    """A source with no pin yet is a known, named state, not an oversight.

    IBM-AML is unpinned because the 41.6 GB corpus does not fit alongside its
    extracted contents on this host. That is a decision awaiting a human, so the
    suite must name which sources are outstanding rather than passing in silence
    and letting a reader assume everything is pinned.
    """
    pending = {
        source["id"]
        for source in load_sources()["sources"]
        if source.get("role") != "cite_only" and source.get("ingest_allowed", True)
        for entry in source["files"]
        if entry.get("sha256", "") == PLACEHOLDER
    }
    assert len(pending) <= 2, f"too many sources are unpinned: {sorted(pending)}"
    if pending:
        result = (
            json.loads(MEASUREMENT.read_text(encoding="utf-8")) if MEASUREMENT.is_file() else {}
        )
        if result.get("verdict") == "STAR_SHAPED_TRIGGER_DAY4_FALLBACK":
            assert pending == {"ibmaml"}, (
                "with the day-4 fallback applied, IBM-AML is the one source that must "
                f"still be outstanding; got {sorted(pending)}"
            )


def test_declared_filenames_are_the_real_archive_members() -> None:
    """A tidy filename is a guess, and a wrong one fails the download.

    PaySim's real member carries the simulator run timestamp. P0 declared
    'PS_2014_01.csv', the downloader failed closed on it, and the config was
    corrected. This asserts the corrected name so it cannot silently revert to
    the plausible-looking wrong one.
    """
    paysim = source_by_id("paysim")
    names = [entry["name"] for entry in paysim["files"]]
    assert "PS_20174392719_1491204439457_log.csv" in names
    assert "PS_2014_01.csv" not in names, "that filename does not exist in the archive"


def test_kaggle_slugs_resolve_to_real_datasets() -> None:
    """The IBM slug ends in '-aml'.

    Without that suffix Kaggle answers 403, which reads like a credentials
    problem rather than a wrong slug, and would be debugged for hours.
    """
    ibm = source_by_id("ibmaml")
    assert ibm["source_url"].endswith("ibm-transactions-for-anti-money-laundering-aml")
    assert ibm["source_url"] != (
        "https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering"
    )


@pytest.mark.skipif(not MANIFEST.is_file(), reason="no download run on this checkout")
def test_manifest_agrees_with_the_declared_pin() -> None:
    """The recorded manifest and the declared pin must not disagree.

    Two records of the same fact, written by different code paths, is one too
    many; when they diverge, one of them is lying and nobody can tell which.
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for source_id, entry in manifest.items():
        if source_id.startswith("_"):
            continue
        declared = {f["name"]: f.get("sha256", "") for f in source_by_id(source_id)["files"]}
        for record in entry["files"]:
            assert record["file"] in declared, f"{record['file']} is not declared"
            assert declared[record["file"]] == record["sha256"], (
                f"{source_id}/{record['file']}: manifest says {record['sha256']}, "
                f"config says {declared[record['file']]}"
            )
            assert record["bytes"] > 0, "a zero-byte corpus is not a corpus"
            assert record["newlines"] > 1, "a file with no data rows is an error"


@pytest.mark.skipif(
    not PAYSIM_CSV.is_file() or not MANIFEST.is_file(), reason="corpus not downloaded"
)
def test_corpus_on_disk_still_matches_the_pin() -> None:
    """The real guarantee: a 493 MB file whose bytes are checked, not trusted.

    This is the check that stops a silently different corpus entering the
    pipeline while every downstream number still looks plausible.
    """
    import hashlib

    declared = {f["name"]: f["sha256"] for f in source_by_id("paysim")["files"]}
    expected = declared[PAYSIM_CSV.name]
    digest = hashlib.sha256()
    with PAYSIM_CSV.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    assert digest.hexdigest() == expected, (
        f"{PAYSIM_CSV.name} on disk does not match the declared pin.\n"
        f"  declared {expected}\n  on disk  {digest.hexdigest()}"
    )


def test_verify_only_treats_the_placeholder_as_unpinned_not_mismatched() -> None:
    """The first verification run after a fresh checkout must not fail.

    The download path skips the check when the pin is a placeholder, so
    verify-only has to agree. When it did not, a correct file was reported as
    MISMATCH, which would train everyone to ignore the one error that matters.
    """
    text = (REPO_ROOT / "scripts" / "download_data.py").read_text(encoding="utf-8")
    verify_block = text.split("if args.verify_only:", 1)[1]
    assert (
        "NOT PINNED" in verify_block
    ), "verify-only must distinguish 'not yet pinned' from 'mismatch'"
    assert "if recorded == RECORDED_AT_DOWNLOAD:" in verify_block, (
        "verify-only must skip the comparison while the pin is a placeholder; "
        "otherwise a correct file reports MISMATCH on a fresh checkout, which "
        "teaches everyone to ignore the one error that matters"
    )


# --- the §4 measurement ---------------------------------------------------


@pytest.mark.skipif(not MEASUREMENT.is_file(), reason="measurement not run yet")
def test_measurement_ran_on_the_full_corpus() -> None:
    """§4 is stated on the full corpus, not a sample.

    A reuse ratio measured on the first 20k rows would understate reuse and
    could turn a star-shaped corpus into a passing one.
    """
    result = json.loads(MEASUREMENT.read_text(encoding="utf-8"))
    assert (
        result["n_rows"] == 6_362_620
    ), f"measurement covered {result['n_rows']:,} rows; the corpus is 6,362,620"
    assert result["cycles"]["sample_rows"] == 20_000, "the cycle count uses a 20k sample"


@pytest.mark.skipif(not MEASUREMENT.is_file(), reason="measurement not run yet")
def test_verdict_matches_the_precommitted_thresholds() -> None:
    """The verdict is arithmetic on the thresholds, not a judgement call.

    Recomputed here from the recorded numbers so that a hand-edited verdict in
    the JSON cannot survive.
    """
    result = json.loads(MEASUREMENT.read_text(encoding="utf-8"))
    thresholds = result["thresholds"]
    median = result["counterparty_degree"]["median"]
    cycles = result["cycles"]["cycles_found"]
    expected = (
        "NETWORK_THESIS_HOLDS"
        if median > thresholds["median_counterparty_degree_gt"]
        and cycles >= thresholds["cycles_min_count"]
        else "STAR_SHAPED_TRIGGER_DAY4_FALLBACK"
    )
    assert result["verdict"] == expected, (
        f"recorded verdict {result['verdict']} contradicts median={median}, "
        f"cycles={cycles} against {thresholds}"
    )


@pytest.mark.skipif(not MEASUREMENT.is_file(), reason="measurement not run yet")
def test_fallback_is_logged_as_a_decision() -> None:
    """00 §A: a spec-contradicting finding is logged, not silently applied.

    The §4 rule pre-commits the fallback, and applying it is required. What is
    equally required is that the contradiction is written down where a judge
    can find it.
    """
    result = json.loads(MEASUREMENT.read_text(encoding="utf-8"))
    if result["verdict"] != "STAR_SHAPED_TRIGGER_DAY4_FALLBACK":
        pytest.skip("fallback not triggered")
    assert DECISIONS.is_file(), "DECISIONS.md must exist"
    text = DECISIONS.read_text(encoding="utf-8")
    assert "DEV-011" in text, "the fallback must be logged as a numbered decision"
    # The decision must carry the numbers that caused it, not just its name.
    assert (
        "6,353,307" in text or "0.001464" in text
    ), "the decision must record the measurement that caused it"
