"""Port conformance for the inbound source port, asserted against every adapter.

Plan §13 lists eight ports, each owed "a Protocol, a ``Null*`` implementation, **and a
contract test both must pass**". P7's gate runs ``pytest -q tests/contracts_adapters`` and
calls it "port conformance across every adapter". Before that directory held the watchlist
conformance test, the gate collected zero tests and exited 5; this file and its sibling
``test_objectstore_conformance.py`` are the two ingest-side ports of the eight, and they are
parametrised the same way for the same reason: a new source adapter that does not satisfy
the port has to fail *here*, where a port is named, rather than at the first call site in
production. Adding a class is one line in ``ADAPTERS`` — that is the intended way this file
grows.

What the port actually forbids (:mod:`oxbow.ports.source` and the Pandera contract in
``contracts/canonical_v1.py``), and which test below pins it:

* a source yields canonical rows **or raises** — it may not repair, coerce or drop
  (02 §D). The CSV adapter is the one that parses real text, so it is where "``12.50`` is
  refused, never rounded to 12" is asserted;
* an unmappable row goes to quarantine **with the constraint it failed**, and the count
  moves by exactly the number of rows lost, because the UI shows that number and a fraud
  system that quietly loses rows has a blind spot nobody can see (02 §D);
* the manifest names the batch the rows are actually in — count, window, source — because
  half a day of transactions looks like a quiet day and a quiet day looks normal (03 §B);
* the value crossing the boundary in an account column is the 12-hex digest and **never**
  the raw identifier, which is the PII boundary 02 §F puts at ingest itself, and money is
  integer minor units (01 §B).

Two honest asymmetries are stated rather than hidden. The null adapter has no parse step —
it yields what the caller hands it — so the quarantine promise reaches it through the port's
own ``quarantine()`` call, and ``test_quarantine_counts_a_reasonless_record_instead_of_
swallowing_it`` records that no implementation yet *refuses* such a record. And the port's
field list is the pre-P1b spelling (``ts_utc``/``src_account``) while ``canonical_v1.py``
declares the implemented one (``event_ts_utc``/``account_from``): both are asserted, each
adapter declares which it claims, and a *third* invention is what fails here.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from oxbow.adapters.file.source import CsvSourceAdapter, manifest_for_file
from oxbow.adapters.null.objectstore import NullObjectStore
from oxbow.adapters.null.source import NullSourceAdapter
from oxbow.adapters.s3.source import (
    SUCCESS_MARKER,
    MissingSuccessMarker,
    ObjectStoreZoneSource,
)
from oxbow.contracts.canonical_v1 import CANONICAL_COLUMNS
from oxbow.contracts.raw_ibm_aml import IBMAML_RAW_HEADER
from oxbow.ingest import paysim
from oxbow.ingest.canonical import RunIdentity
from oxbow.ingest.ibm_aml import (
    IBMAML_SOURCE_NAME,
    ORDINAL_COLUMN,
    IBMAMLAdapter,
    IBMAMLIngestPolicy,
)
from oxbow.ports.source import (
    CANONICAL_EVENT_FIELDS,
    BatchManifest,
    QuarantineRecord,
    SourceAdapter,
    StreamSourceAdapter,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- the fixture, hand-counted -------------------------------------------------
# Three usable rows, every value below written out rather than derived: the amounts
# already in minor units, the window as the minimum and maximum of the three stamps, the
# account keys as 48-bit digests. A cycle (A->B, B->C, C->A) so a downstream graph over the
# same fixture would see a real ring.

CSV_SOURCE = "file-drop"
ZONE_SOURCE = "zone-drop"
NULL_SOURCE = "null-declared"

RUN_ID = "01J6ZXN4T8V3WKQM5RPSGHYBED"  # 26 chars of Crockford base32, no I/L/O/U
BATCH_ID = "0f1e2d3c4b5a"  # 12 hex, the shape RunIdentity demands
INGESTED_AT = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)

ACCOUNT_A = "0a1b2c3d4e5f"
ACCOUNT_B = "f6e5d4c3b2a1"
ACCOUNT_C = "77aa55bb33cc"

CLEAN_ROWS = 3
BAD_ROWS = 2

WINDOW_START = datetime(2026, 3, 1, 9, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC)

EXPECTED_AMOUNTS_MINOR: tuple[int, ...] = (1_000_000, 250, 7)
EXPECTED_ACCOUNT_PAIRS: tuple[tuple[str, str], ...] = (
    (ACCOUNT_A, ACCOUNT_B),
    (ACCOUNT_B, ACCOUNT_C),
    (ACCOUNT_C, ACCOUNT_A),
)
EXPECTED_BALANCES: tuple[tuple[int, int, int, int], ...] = (
    (5_000_000, 4_000_000, 100, 1_000_100),
    (4_000_000, 3_999_750, 0, 250),
    (2_000, 1_993, 4_000_000, 4_000_007),
)
EXPECTED_LABEL_IS_FRAUD: tuple[int, ...] = (0, 0, 1)

# The port's field list, as a set, and the implemented one. Two spellings of one contract
# is a documented disagreement (see this module's docstring); a *third* is drift.
PORT_SPELLING = frozenset(CANONICAL_EVENT_FIELDS)
CONTRACT_SPELLING = frozenset(CANONICAL_COLUMNS)

TIMESTAMP_FIELDS = ("ts_utc", "event_ts_utc")
MONEY_FIELDS = (
    "amount_minor",
    "src_balance_before",
    "src_balance_after",
    "dst_balance_before",
    "dst_balance_after",
    "src_balance_before_minor",
    "src_balance_after_minor",
    "dst_balance_before_minor",
    "dst_balance_after_minor",
)
ACCOUNT_FIELDS = ("src_account", "dst_account", "account_from", "account_to")

# 03 §B fixes the account key at 48 bits, rendered lower-case; the display form is
# ``ACC-`` plus upper text and is a presentation concern, not a stored one.
ACCOUNT_KEY_PATTERN = re.compile(r"^[0-9a-f]{12}$")

# IBM-AML's own eleven columns, read off the bytes of ``HI-Small_Trans.csv``. The wall
# clock is Africa/Kampala (UTC+3, no DST), so 12:00 local is 09:00 UTC: the three rows
# below land in exactly the window above, which is why one expectation table serves four
# adapters. Amounts are in the currency's own minor exponent: 10000.00 / 2.50 / 0.07 USD
# -> 1000000 / 250 / 7.
IBM_GOOD_ROWS: tuple[tuple[str, ...], ...] = (
    (
        "2026/03/01 12:00",
        "010",
        "8000EBD30",
        "03208",
        "8000F4580",
        "10000.00",
        "US Dollar",
        "10000.00",
        "US Dollar",
        "Wire",
        "0",
    ),
    (
        "2026/03/01 12:30",
        "03208",
        "8000F4580",
        "021",
        "81AAA0001",
        "2.50",
        "US Dollar",
        "2.50",
        "US Dollar",
        "ACH",
        "0",
    ),
    (
        "2026/03/01 13:00",
        "021",
        "81AAA0001",
        "010",
        "8000EBD30",
        "0.07",
        "US Dollar",
        "0.07",
        "US Dollar",
        "Cash",
        "1",
    ),
)
IBM_BAD_ROWS: tuple[tuple[str, ...], ...] = (
    # Amount Paid is not a number: quarantined as ``amount_format``.
    (
        "2026/03/01 12:15",
        "010",
        "8000EBD30",
        "011",
        "8000EBD32",
        "not-a-number",
        "US Dollar",
        "not-a-number",
        "US Dollar",
        "Wire",
        "0",
    ),
    # ``Is Laundering`` outside {0, 1} is a different encoding, not a weaker label:
    # quarantined as ``label_not_binary``.
    (
        "2026/03/01 12:45",
        "007",
        "8000CC0001",
        "002",
        "8000DD0002",
        "100.00",
        "US Dollar",
        "100.00",
        "US Dollar",
        "Cash",
        "7",
    ),
)
IBM_RAW_IDENTIFIERS: tuple[str, ...] = (
    "8000EBD30",
    "8000F4580",
    "81AAA0001",
    "8000EBD32",
    "8000CC0001",
    "8000DD0002",
)
# Hand-computed, in file order, from the two rows above.
IBM_QUARANTINE_REASONS: frozenset[str] = frozenset({"amount_format", "label_not_binary"})

KAMPALA = ZoneInfo("Africa/Kampala")
IBMAML_POLICY = IBMAMLIngestPolicy(
    source_timezone=KAMPALA,
    timestamp_format="%Y/%m/%d %H:%M",
    typology_artifact=REPO_ROOT / "data" / "processed" / "ibm_typologies.parquet",
    deployment_timezone="Africa/Kampala",
)
NO_TYPOLOGY_JOIN = pl.DataFrame(
    {ORDINAL_COLUMN: [], "label_typology": []},
    schema={ORDINAL_COLUMN: pl.Int64, "label_typology": pl.String},
)


def drop_row(
    index: int,
    ts_utc: datetime,
    src: str,
    dst: str,
    amount: int,
    balances: tuple[int, int, int, int],
    fraud: int,
    source_id: str,
) -> dict[str, Any]:
    """One canonical row in the port's own field list, values already typed."""
    before_src, after_src, before_dst, after_dst = balances
    return {
        "txn_id": f"{source_id}:{index}",
        "ts_utc": ts_utc,
        "src_account": src,
        "dst_account": dst,
        "amount_minor": amount,
        "currency": "USD",
        "channel": "APP",
        "txn_type": "TRANSFER",
        "src_balance_before": before_src,
        "src_balance_after": after_src,
        "dst_balance_before": before_dst,
        "dst_balance_after": after_dst,
        "label_fraud": fraud,
        "label_typology": None,
        "source_dataset": source_id,
        "ingested_at": INGESTED_AT,
        "run_id": RUN_ID,
    }


def clean_rows(source_id: str) -> list[dict[str, Any]]:
    """The three usable rows, straight out of the tables above."""
    stamps = (WINDOW_START, datetime(2026, 3, 1, 9, 30, tzinfo=UTC), WINDOW_END)
    return [
        drop_row(
            index + 1,
            stamps[index],
            *EXPECTED_ACCOUNT_PAIRS[index],
            EXPECTED_AMOUNTS_MINOR[index],
            EXPECTED_BALANCES[index],
            EXPECTED_LABEL_IS_FRAUD[index],
            source_id,
        )
        for index in range(CLEAN_ROWS)
    ]


# The two rows the CSV drop carries in the mixed corpus. ``12.50`` is the interesting one:
# it is a currency value in the wrong unit, and rounding it at the boundary is the
# coercion ``config/pipeline.yaml`` sets ``allow_silent_coercion: false`` to prevent.
BAD_CSV_AMOUNTS: tuple[str, ...] = ("12.50", "N/A")


def _csv_line(row: Mapping[str, Any]) -> dict[str, str]:
    """The row as written to a batch drop: everything text, dates in ISO-Z."""
    text: dict[str, str] = {}
    for name in CANONICAL_EVENT_FIELDS:
        value = row[name]
        if isinstance(value, datetime):
            text[name] = value.isoformat().replace("+00:00", "Z")
        elif value is None:
            text[name] = ""
        else:
            text[name] = str(value)
    return text


def _json_line(row: Mapping[str, Any]) -> str:
    """The row as written to a drop-zone object: money stays a JSON number.

    Deliberately not ``default=str`` for everything — a producer that writes ``"1000"``
    for a minor-unit amount is the hole ``test_zone_source_passes_through...`` pins.
    """
    return json.dumps(
        {
            name: (
                value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value
            )
            for name, value in row.items()
        }
    )


@dataclass(frozen=True, slots=True)
class Expectation:
    """What one adapter's clean corpus must produce, written by hand above."""

    source_id: str
    spelling: frozenset[str]
    row_count: int = CLEAN_ROWS
    txn_ids: tuple[str, ...] | None = None
    account_pairs: tuple[tuple[str, str], ...] | None = EXPECTED_ACCOUNT_PAIRS
    raw_identifiers: tuple[str, ...] = ()
    ledger: bool = True


@dataclass(frozen=True, slots=True)
class SourceCase:
    """An adapter plus the numbers its fixture was built to hit."""

    name: str
    adapter: SourceAdapter
    expect: Expectation


class Corpora:
    """The three inbound patterns, materialised once per module run.

    Each adapter family gets its own directory so a quarantine write in one test cannot
    be read back by another, and so this file never touches the repository's ``out/``.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.out = root / "out"

    def drop(self, *, mixed: bool) -> Path:
        """Pattern 1: a batch file drop, with or without the two unmappable rows."""
        path = self.root / ("drop-mixed.csv" if mixed else "drop.csv")
        if path.is_file():
            return path
        rows = clean_rows(CSV_SOURCE)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CANONICAL_EVENT_FIELDS))
            writer.writeheader()
            for row in rows:
                writer.writerow(_csv_line(row))
            if mixed:
                for index, amount in enumerate(BAD_CSV_AMOUNTS):
                    bad = _csv_line(clean_rows(CSV_SOURCE)[index])
                    bad["amount_minor"] = amount
                    writer.writerow(bad)
        return path

    def zone(self, *, mixed: bool) -> tuple[NullObjectStore, str]:
        """Pattern 2: an object-storage zone, complete with its ``_SUCCESS`` marker.

        The mixed corpus adds a second object holding exactly two lines: one truncated
        JSON document, and one well-formed object carrying two of the seventeen canonical
        fields. Two objects rather than one so a lost object is a different prefix listing
        and not a silently short read.
        """
        store = NullObjectStore(self.root / ("zone-mixed" if mixed else "zone"))
        prefix = "batch"
        rows = clean_rows(ZONE_SOURCE)
        first = "\n".join(_json_line(row) for row in rows) + "\n"
        store.put(f"{prefix}/part-0000.jsonl", first.encode("utf-8"), "application/x-ndjson")
        if mixed:
            truncated = '{"txn_id": "zone-drop:4", "ts_utc"'
            half_a_row = json.dumps({"txn_id": "zone-drop:5", "ts_utc": "2026-03-01T09:45:00Z"})
            store.put(
                f"{prefix}/part-0001.jsonl",
                f"{truncated}\n{half_a_row}\n".encode(),
                "application/x-ndjson",
            )
        store.put(f"{prefix}/{SUCCESS_MARKER}", b"", "application/octet-stream")
        return store, prefix

    def ibm(self, *, mixed: bool) -> Path:
        """The IBM-AML reader's corpus: the real header, a handful of hand-built rows."""
        path = self.root / ("HI-Small_Trans-mixed.csv" if mixed else "HI-Small_Trans.csv")
        if path.is_file():
            return path
        body = [*IBM_GOOD_ROWS, *(IBM_BAD_ROWS if mixed else ())]
        text = "\n".join([",".join(IBMAML_RAW_HEADER), *(",".join(row) for row in body)]) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
        return path


AdapterFactory = Callable[[Corpora], SourceAdapter]


def _csv_clean(corpora: Corpora) -> SourceAdapter:
    path = corpora.drop(mixed=False)
    return CsvSourceAdapter(
        source_id=CSV_SOURCE,
        path=path,
        run_id=RUN_ID,
        manifest=manifest_for_file(path, source_id=CSV_SOURCE, batch_id=BATCH_ID),
        root=corpora.out,
    )


def _csv_mixed(corpora: Corpora) -> SourceAdapter:
    """The same drop, two rows longer, with a manifest that names only the usable three.

    Declared rather than derived from ``manifest_for_file`` because that helper counts the
    file's data rows (five) and the port's own rule is that a batch whose usable rows do
    not reach its declared count is rejected whole. Three usable out of five is what a
    quarantine looks like from inside a run.
    """
    path = corpora.drop(mixed=True)
    manifest = BatchManifest(
        batch_id=f"{BATCH_ID}-mixed",
        row_count=CLEAN_ROWS,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        source_system=CSV_SOURCE,
    )
    return CsvSourceAdapter(
        source_id=CSV_SOURCE, path=path, run_id=RUN_ID, manifest=manifest, root=corpora.out
    )


def _zone_clean(corpora: Corpora) -> SourceAdapter:
    store, prefix = corpora.zone(mixed=False)
    return ObjectStoreZoneSource(
        store=store,
        prefix=prefix,
        source_id=ZONE_SOURCE,
        batch_id=BATCH_ID,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )


def _zone_mixed(corpora: Corpora) -> SourceAdapter:
    store, prefix = corpora.zone(mixed=True)
    return ObjectStoreZoneSource(
        store=store,
        prefix=prefix,
        source_id=ZONE_SOURCE,
        batch_id=f"{BATCH_ID}-mixed",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )


def _null(corpora: Corpora) -> SourceAdapter:
    return NullSourceAdapter(
        source_id=NULL_SOURCE,
        rows=clean_rows(NULL_SOURCE),
        manifest=BatchManifest(
            batch_id=BATCH_ID,
            row_count=CLEAN_ROWS,
            sha256="0" * 63 + "f",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            source_system=NULL_SOURCE,
        ),
        root=corpora.out,
    )


def _ibm_clean(corpora: Corpora) -> SourceAdapter:
    return _ibm(corpora, mixed=False)


def _ibm_mixed(corpora: Corpora) -> SourceAdapter:
    return _ibm(corpora, mixed=True)


def _ibm(corpora: Corpora, *, mixed: bool) -> SourceAdapter:
    return IBMAMLAdapter(
        corpora.ibm(mixed=mixed),
        RunIdentity(run_salt="oxbow-conformance-test-salt", batch_id=BATCH_ID),
        run_id=RUN_ID,
        deployment_tz=KAMPALA,
        policy=IBMAML_POLICY,
        typology=NO_TYPOLOGY_JOIN,
        ingested_at=INGESTED_AT,
    )


# Every SourceAdapter in the tree. A new implementation is added here — one line.
#
# ``ingest/paysim.py`` is NOT here, and says it is the SourceAdapter implementation for
# Module A: it exposes ``ingest_paysim()`` returning an ``IngestResult``, which has neither
# ``read_manifest`` nor ``iter_canonical``, so there is nothing to conform. That claim is
# pinned by ``test_paysim_does_not_yet_implement_the_port_it_claims`` rather than by a
# register entry.
ADAPTERS: dict[str, AdapterFactory] = {
    "csv": _csv_clean,
    "ibmaml": _ibm_clean,
    "null": _null,
    "zone": _zone_clean,
}

# The three that parse bytes into rows. The null adapter has no parse step, so a corpus
# with unmappable rows in it is not a thing it can meet; its quarantine path is exercised
# through the port API instead, in ``test_quarantine_counts_a_reasonless_record...``.
QUARANTINING: dict[str, AdapterFactory] = {
    "csv": _csv_mixed,
    "ibmaml": _ibm_mixed,
    "zone": _zone_mixed,
}

EXPECTATIONS: dict[str, Expectation] = {
    "csv": Expectation(
        source_id=CSV_SOURCE,
        spelling=PORT_SPELLING,
        txn_ids=tuple(f"{CSV_SOURCE}:{n}" for n in (1, 2, 3)),
    ),
    "zone": Expectation(
        source_id=ZONE_SOURCE,
        spelling=PORT_SPELLING,
        txn_ids=tuple(f"{ZONE_SOURCE}:{n}" for n in (1, 2, 3)),
    ),
    "null": Expectation(
        source_id=NULL_SOURCE,
        spelling=PORT_SPELLING,
        txn_ids=tuple(f"{NULL_SOURCE}:{n}" for n in (1, 2, 3)),
    ),
    "ibmaml": Expectation(
        source_id=IBMAML_SOURCE_NAME,
        spelling=CONTRACT_SPELLING,
        # ``txn_id`` carries a digest of the row's own key, so no person can write it out
        # by hand; the namespacing prefix and the uniqueness are what get asserted.
        txn_ids=None,
        account_pairs=None,
        raw_identifiers=IBM_RAW_IDENTIFIERS,
        # DEV-013: the corpus ships no balance ledger, so all four balance columns are null.
        ledger=False,
    ),
}


def _ids() -> list[str]:
    return list(ADAPTERS)


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Corpora:
    return Corpora(tmp_path_factory.mktemp("source-conformance"))


@pytest.fixture(params=sorted(ADAPTERS))
def source(request: pytest.FixtureRequest, corpora: Corpora) -> SourceCase:
    name = request.param
    assert name in EXPECTATIONS, f"{name} is in ADAPTERS but has no hand-written expectation"
    return SourceCase(name=name, adapter=ADAPTERS[name](corpora), expect=EXPECTATIONS[name])


@pytest.fixture(params=sorted(QUARANTINING))
def lossy(request: pytest.FixtureRequest, corpora: Corpora) -> SourceCase:
    name = request.param
    return SourceCase(name=name, adapter=QUARANTINING[name](corpora), expect=EXPECTATIONS[name])


# --- reading a row without assuming its spelling --------------------------------


def _moment(row: Mapping[str, Any]) -> datetime:
    """The row's UTC instant, read through whichever of the two names it carries.

    A string is parsed here rather than coerced in the adapter, because the assertion this
    feeds is about the *manifest's* window and a zone object legitimately holds JSON text.
    """
    present = [name for name in TIMESTAMP_FIELDS if name in row]
    assert len(present) == 1, (
        f"a canonical row must carry exactly one timestamp column, found {present}: "
        f"{sorted(TIMESTAMP_FIELDS)} are the two spellings in the tree and a third name is "
        "a rename nobody declared"
    )
    value: Any = row[present[0]]
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert isinstance(value, datetime), f"timestamp {value!r} is not an instant"
    assert value.tzinfo is not None, (
        f"{present[0]}={value!r} is naive: a timestamp without an offset is a claim about a "
        "timezone nobody agreed to, and every window comparison downstream becomes local"
    )
    return value.astimezone(UTC)


def _money(row: Mapping[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in MONEY_FIELDS if name in row}


def _accounts(row: Mapping[str, Any]) -> list[str]:
    return [row[name] for name in ACCOUNT_FIELDS if name in row]


# --- the port's structural promises --------------------------------------------


def test_satisfies_the_runtime_checkable_protocol(source: SourceCase) -> None:
    """A duck that does not satisfy the Protocol is not an implementation of the port."""
    adapter = source.adapter
    assert isinstance(adapter, SourceAdapter), (
        f"{source.name} does not satisfy SourceAdapter; a source wired through a protocol it "
        "does not implement fails at the call site, in production"
    )
    assert adapter.source_id == source.expect.source_id
    for member in ("read_manifest", "iter_canonical", "quarantine", "quarantine_count"):
        assert callable(getattr(adapter, member)), f"{source.name} has no callable {member}()"


def test_the_register_covers_both_inbound_patterns_built_so_far() -> None:
    """02 §D ships patterns 1 and 2; a register that quietly lost one re-marks the gate green.

    P7's gate is "port conformance across every adapter". Four names, two of which are the
    documented inbound patterns and two of which are the corpora, is the whole tree as it
    stands; a register that shrinks is the failure this asserts against.
    """
    assert sorted(_ids()) == ["csv", "ibmaml", "null", "zone"]
    assert set(QUARANTINING) <= set(ADAPTERS)


def test_manifest_is_internally_consistent_and_names_its_source(source: SourceCase) -> None:
    """A manifest is the batch's proof of completeness, so it has to survive its own check."""
    manifest = source.adapter.read_manifest()
    manifest.validate()

    assert isinstance(manifest, BatchManifest)
    assert manifest.source_system == source.adapter.source_id, (
        f"{source.adapter.source_id} returned a manifest for "
        f"{manifest.source_system!r}: the source a run reports is the one its lineage "
        "traces to, and a mismatch sends an analyst to the wrong corpus"
    )
    assert len(manifest.sha256) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", manifest.sha256), (
        f"sha256 {manifest.sha256!r} is not 64 hex characters; a digest that cannot be "
        "compared byte-for-byte is a decoration"
    )
    assert manifest.window_end >= manifest.window_start
    assert manifest.batch_id


def test_the_manifest_describes_the_rows_that_actually_arrive(source: SourceCase) -> None:
    """The version the manifest names is the version the rows are in.

    Row count, window and corpus are all cross-read from the two halves of the port, so a
    manifest that agrees with itself but not with its rows — the truncated drop, the stale
    window — fails here (03 §B).
    """
    manifest = source.adapter.read_manifest()
    rows = list(source.adapter.iter_canonical())
    moments = [_moment(row) for row in rows]

    assert len(rows) == source.expect.row_count == manifest.row_count, (
        f"{source.name}: manifest declares {manifest.row_count} rows and "
        f"{len(rows)} arrived. A batch that is short and calls itself complete is a quiet "
        "period, and a quiet period is normal behaviour (03 §B)"
    )
    assert manifest.window_start == min(moments), (
        f"{source.name}: window starts {manifest.window_start} but the earliest row is "
        f"{min(moments)}; every windowed rule reads history inside that window"
    )
    assert manifest.window_end == max(moments)
    assert (manifest.window_start, manifest.window_end) == (WINDOW_START, WINDOW_END), (
        "the window this fixture was hand-computed to produce has moved: expected "
        f"{WINDOW_START}..{WINDOW_END}"
    )


# --- the rows themselves -------------------------------------------------------


def test_rows_carry_exactly_the_declared_canonical_field_set(source: SourceCase) -> None:
    """No missing field, and no third spelling of the contract.

    ``contracts/canonical_v1.py`` exists because three modules each carried their own
    column list; ``strict=True`` there means an undeclared column is schema drift. This is
    the per-row version of that, at the boundary where a rename actually happens.
    """
    rows = list(source.adapter.iter_canonical())
    assert rows, f"{source.name} yielded nothing from a corpus with {CLEAN_ROWS} rows in it"
    for row in rows:
        assert set(row) == source.expect.spelling, (
            f"{source.name}'s rows are not the field set {source.name} declares: missing "
            f"{sorted(source.expect.spelling - set(row))}, unexpected "
            f"{sorted(set(row) - source.expect.spelling)}"
        )


def test_money_crosses_the_boundary_as_integer_minor_units(source: SourceCase) -> None:
    """01 §B, and the AST gate that enforces it in annotations.

    A float that survives to this point has already been multiplied by 100 somewhere, and
    ``coerce=False`` in the contract means the schema refuses it three stages later — in a
    run log, not at the boundary that caused it. A *string* is the other quiet failure: it
    sums in some readers and not in others.
    """
    seen_money = 0
    for row in list(source.adapter.iter_canonical()):
        money = _money(row)
        assert "amount_minor" in money, f"{source.name} yielded a row with no amount_minor"
        for name, value in money.items():
            if value is None:
                assert not source.expect.ledger, (
                    f"{name} is null on a source that ships a ledger: a half-populated "
                    "balance column is a parse that lost rows (DEV-013)"
                )
                continue
            assert isinstance(value, int) and not isinstance(
                value, bool
            ), f"{name}={value!r} is {type(value).__name__}, not integer minor units"
            assert value >= 0, f"{name}={value} is negative: the source changed encoding"
            seen_money += 1
    assert seen_money, f"{source.name} carried no money column at all"


def test_the_account_key_is_a_digest_and_never_the_raw_identifier(source: SourceCase) -> None:
    """02 §F's PII boundary, at the only place it can still be enforced cheaply.

    Raw ids never leave ingest. A key that leaked out of the hasher still joins, still
    groups, and silently re-identifies the account it was meant to hide — so both the shape
    and the absence of the source's own identifier strings are asserted.
    """
    rows = list(source.adapter.iter_canonical())
    for row in rows:
        accounts = _accounts(row)
        assert len(accounts) == 2, (
            f"{source.name} exposed {len(accounts)} account columns; a canonical row names "
            "both endpoints, and a row that cannot is not joinable to a graph"
        )
        for value in accounts:
            assert ACCOUNT_KEY_PATTERN.fullmatch(value), (
                f"account key {value!r} is not 12 lower-case hex characters: it is either a "
                "raw identifier or a digest someone re-cased, and both re-identify accounts "
                "downstream of ingest where the PII boundary says they cannot"
            )
        for identifier in source.expect.raw_identifiers:
            leaked = [name for name, value in row.items() if value == identifier]
            assert not leaked, (
                f"raw identifier {identifier!r} reached the canonical row in "
                f"{leaked}: the salted digest is the only permitted form outside ingest"
            )
    if source.expect.account_pairs is not None:
        assert [tuple(_accounts(row)) for row in rows] == list(source.expect.account_pairs), (
            f"{source.name} swapped or re-ordered the endpoint digests this fixture names "
            "row by row; a transaction whose direction flipped is a different transaction"
        )


def test_txn_ids_are_namespaced_unique_and_stamped_with_one_run(source: SourceCase) -> None:
    """DEV-004/C4 and DEV-003: one table, two corpora, and one identity per run.

    A bare integer id would merge PaySim row 41 with an IBM-AML row 41 into one account's
    history; a row carrying somebody else's run id means the artifact was assembled from two
    runs and no downstream check can tell.
    """
    rows = list(source.adapter.iter_canonical())
    ids = [row["txn_id"] for row in rows]
    assert len(set(ids)) == len(ids), f"{source.name} yielded a duplicate txn_id"
    for row in rows:
        assert row["txn_id"].startswith(f"{row['source_dataset']}:")
        assert row["source_dataset"] == source.adapter.source_id
        assert row["run_id"] == RUN_ID, (
            f"row {row['txn_id']!r} names run {row['run_id']!r}, not the run this batch "
            "was read under: two runs in one artifact is a lineage nobody can audit"
        )
    if source.expect.txn_ids is not None:
        assert tuple(ids) == source.expect.txn_ids


def test_iter_canonical_is_lazy_and_reusable_per_adapter(source: SourceCase) -> None:
    """Two reads of the same declared batch give the same rows.

    The port returns an iterator, so an implementation that consumed a file handle into
    exhaustion would pass a single-read test and fail the stage that reads the manifest
    after the rows.
    """
    first = [row["txn_id"] for row in source.adapter.iter_canonical()]
    second = [row["txn_id"] for row in source.adapter.iter_canonical()]
    assert first == second, f"{source.name} returns a different batch on the second read"


# --- quarantine: the count is the contract -------------------------------------


def test_a_bad_row_is_quarantined_not_dropped_and_the_good_rows_still_land(
    lossy: SourceCase,
) -> None:
    """02 §D's one sentence, asserted with a hand-counted corpus.

    Every fixture here carries exactly two rows that cannot be mapped. Dropping them would
    leave the good three in place and the count at zero, which is the blind spot the port
    exists to prevent; coercing them would put a fabricated number into evidence.
    """
    adapter = lossy.adapter
    assert adapter.quarantine_count() == 0, (
        f"{lossy.name} had already quarantined {adapter.quarantine_count()} rows before "
        "reading anything: a count that starts non-zero cannot be read as a loss rate"
    )
    landed = list(adapter.iter_canonical())

    assert len(landed) == CLEAN_ROWS, (
        f"{lossy.name} landed {len(landed)} of {CLEAN_ROWS} mappable rows: the good rows "
        "must arrive even when a neighbour does not"
    )
    assert adapter.quarantine_count() == BAD_ROWS, (
        f"{lossy.name} lost {BAD_ROWS} unmappable rows and counted "
        f"{adapter.quarantine_count()}: silently dropped rows are how fraud systems develop "
        "blind spots nobody can see (02 §D)"
    )
    if lossy.expect.txn_ids is not None:
        assert [row["txn_id"] for row in landed] == list(lossy.expect.txn_ids), (
            f"{lossy.name} landed a different three rows than the fixture names: an "
            "unmappable row that displaces a mappable one is a drop with a count attached"
        )


def test_a_decimal_amount_is_refused_not_rounded(lossy: SourceCase) -> None:
    """``allow_silent_coercion: false``, at the one boundary that meets decimal text.

    For the CSV and zone adapters the bad rows are the decimal ones; for IBM-AML the amount
    is non-numeric text and the second loss is a label outside {0, 1}. Either way nothing
    that looks like 12 or 13 minor units may appear in a landed row.
    """
    rows = list(lossy.adapter.iter_canonical())
    amounts = [row["amount_minor"] for row in rows]
    assert amounts == list(EXPECTED_AMOUNTS_MINOR), (
        f"{lossy.name} landed {amounts}; the fixture's three usable rows are "
        f"{list(EXPECTED_AMOUNTS_MINOR)} and a decimal or rounded value in there means the "
        "boundary repaired data it was told to quarantine"
    )


def test_quarantine_counts_a_reasonless_record_instead_of_swallowing_it(
    source: SourceCase,
) -> None:
    """The gap, named: nothing in the tree refuses a quarantine with no reason.

    02 §D says a quarantined row is kept *with the constraint it failed*, and the read model
    agrees — ``quarantine.failing_constraint`` is ``NOT NULL`` in the warehouse migration —
    but the port's ``QuarantineRecord`` is an unvalidated dataclass and every implementation
    appends whatever it is handed. A silent quarantine is the failure this port exists to
    prevent, so the missing check is the one assertion here that documents a hole instead of
    closing one. It fails if the record is dropped quietly, and it fails, usefully, the day
    ``quarantine()`` starts refusing these: at that point delete this test and assert the
    refusal instead.
    """
    adapter = source.adapter
    before = adapter.quarantine_count()
    adapter.quarantine(
        QuarantineRecord(
            batch_id=BATCH_ID,
            source_dataset=adapter.source_id,
            original_row={"amount_minor": "12.50"},
            failing_constraint="",
            detail="",
        )
    )
    assert adapter.quarantine_count() == before + 1, (
        f"{source.name} accepted a reasonless quarantine and did not count it: that is a "
        "dropped row wearing the paperwork of a kept one"
    )


def test_the_reason_an_adapter_records_survives_to_the_port_ledger(corpora: Corpora) -> None:
    """A quarantine that only says "invalid" is an outage; one that says "amount_format, 2
    rows" is a work item. Asserted where the adapter persists its own records: the file-drop
    writes one JSON document per quarantined row under ``out/sources/<id>/``.

    Its own batch id, so the documents counted here are the ones this test's run wrote.
    """
    ledger_batch = f"{BATCH_ID}-ledger"
    drop = corpora.drop(mixed=True)
    adapter = CsvSourceAdapter(
        source_id=CSV_SOURCE,
        path=drop,
        run_id=RUN_ID,
        manifest=BatchManifest(
            batch_id=ledger_batch,
            row_count=CLEAN_ROWS,
            sha256=hashlib.sha256(drop.read_bytes()).hexdigest(),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            source_system=CSV_SOURCE,
        ),
        root=corpora.out,
    )
    assert len(list(adapter.iter_canonical())) == CLEAN_ROWS

    written = sorted(
        (corpora.out / "sources" / CSV_SOURCE).glob(f"quarantine-{ledger_batch}-*.json")
    )
    assert len(written) == BAD_ROWS, (
        f"{len(written)} quarantine documents written for {BAD_ROWS} lost rows: the count and "
        "the evidence have to be the same number, or the UI is showing a figure nobody can "
        "open"
    )
    records = [json.loads(path.read_text(encoding="utf-8")) for path in written]
    for record in records:
        assert record["failing_constraint"], "quarantined with no constraint named"
        assert record["detail"], "quarantined with no reason recorded"
        assert record["batch_id"] == ledger_batch
        assert record["original_row"], "the original bytes were not kept, so triage re-runs blind"
        assert datetime.fromisoformat(record["quarantined_at"].replace("Z", "+00:00")).tzinfo
    assert {"12.50", "N/A"} == {record["original_row"]["amount_minor"] for record in records}, (
        "the two bad rows this fixture wrote are 12.50 and N/A; a record whose payload is not "
        "the row that failed is evidence pointing at the wrong transaction"
    )


# --- per-implementation promises -----------------------------------------------


def test_null_source_refuses_to_invent_a_manifest(corpora: Corpora) -> None:
    """The null adapter's whole reason to exist (02 §A).

    A synthesised manifest would turn a missing drop into a successful empty run, which is
    the clean-run-that-scored-nothing failure ``config/pipeline.yaml`` calls an error.
    """
    adapter = NullSourceAdapter(source_id=NULL_SOURCE, root=corpora.out)

    assert isinstance(adapter, SourceAdapter)
    assert adapter.quarantine_count() == 0
    with pytest.raises(ValueError, match="no manifest"):
        adapter.read_manifest()
    assert list(adapter.iter_canonical()) == []


def test_null_source_does_not_smuggle_a_malformed_row(corpora: Corpora) -> None:
    """Rows are checked against the port's own guards, so the null path cannot be used to
    get a bad row past the contract test the real adapters have to pass.
    """
    float_money = dict(clean_rows(NULL_SOURCE)[0], amount_minor=12.50)
    adapter = NullSourceAdapter(source_id=NULL_SOURCE, rows=[float_money], root=corpora.out)
    with pytest.raises(TypeError, match="float"):
        list(adapter.iter_canonical())

    short = {"txn_id": f"{NULL_SOURCE}:x"}
    missing = NullSourceAdapter(source_id=NULL_SOURCE, rows=[short], root=corpora.out)
    with pytest.raises(ValueError, match="missing required fields"):
        list(missing.iter_canonical())


def test_a_short_batch_is_rejected_whole(corpora: Corpora) -> None:
    """03 §B, pattern 1: the declared count is the agreement, not a hint.

    A truncated drop is self-consistent if it is counted from itself, which is why
    ``manifest_for_file`` measures the bytes and the adapter still compares against the
    manifest it was handed.
    """
    path = corpora.drop(mixed=False)
    declared = manifest_for_file(path, source_id=CSV_SOURCE, batch_id=BATCH_ID)
    overstated = BatchManifest(
        batch_id=declared.batch_id,
        row_count=declared.row_count + 1,
        sha256=declared.sha256,
        window_start=declared.window_start,
        window_end=declared.window_end,
        source_system=CSV_SOURCE,
    )
    adapter = CsvSourceAdapter(
        source_id=CSV_SOURCE, path=path, run_id=RUN_ID, manifest=overstated, root=corpora.out
    )
    with pytest.raises(ValueError, match="rejected whole"):
        list(adapter.iter_canonical())

    with pytest.raises(FileNotFoundError):
        list(
            CsvSourceAdapter(
                source_id=CSV_SOURCE,
                path=corpora.root / "never-dropped.csv",
                run_id=RUN_ID,
                manifest=overstated,
                root=corpora.out,
            ).iter_canonical()
        )


def test_a_zone_prefix_without_its_marker_is_not_a_batch(corpora: Corpora) -> None:
    """02 §D pattern 2, in one line: never read a prefix without its ``_SUCCESS`` marker.

    Its whole value is that it turns "the transfer job was still uploading" into an error
    instead of a short run that reads as a quiet day.
    """
    store, prefix = corpora.zone(mixed=False)
    body = store.get(f"{prefix}/part-0000.jsonl")
    incomplete = NullObjectStore(corpora.root / "zone-no-marker")
    incomplete.put(f"{prefix}/part-0000.jsonl", body, "application/x-ndjson")

    def unmarked() -> ObjectStoreZoneSource:
        return ObjectStoreZoneSource(
            store=incomplete,
            prefix=prefix,
            source_id=ZONE_SOURCE,
            batch_id=BATCH_ID,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    with pytest.raises(MissingSuccessMarker):
        unmarked().read_manifest()
    with pytest.raises(MissingSuccessMarker):
        list(unmarked().iter_canonical())

    marked = _zone_clean(corpora)
    assert isinstance(marked, SourceAdapter)
    assert len(list(marked.iter_canonical())) == CLEAN_ROWS


def test_zone_source_passes_through_the_json_types_it_was_handed(corpora: Corpora) -> None:
    """A hole, asserted rather than assumed away.

    ``ObjectStoreZoneSource`` checks field *presence* and the float-money rule and then
    yields ``json.loads``'s own types. A drop whose amount is the text ``"1000.50"`` — one
    cent off a currency value, in the wrong unit — is neither missing nor a float, so it
    crosses the boundary as a string row that canonical v1 would refuse. The fixtures here
    therefore write JSON numbers; the fix belongs in the adapter (validate the row, do not
    launder it), not in this file.
    """
    store = NullObjectStore(corpora.root / "zone-string-money")
    row = clean_rows(ZONE_SOURCE)[0]
    store.put(
        "dirty/part-0000.jsonl",
        _json_line(dict(row, amount_minor="1000.50")).encode("utf-8"),
        "application/x-ndjson",
    )
    store.put(f"dirty/{SUCCESS_MARKER}", b"", "application/octet-stream")
    adapter = ObjectStoreZoneSource(
        store=store,
        prefix="dirty",
        source_id=ZONE_SOURCE,
        batch_id=BATCH_ID,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    rows = list(adapter.iter_canonical())

    assert len(rows) == 1, "the row was refused, so this test's premise has changed: delete it"
    assert rows[0]["amount_minor"] == "1000.50"
    assert adapter.quarantine_count() == 0, (
        "the zone adapter now quarantines untyped money, which is the behaviour this "
        "characterisation test expected and never had"
    )


def test_ibmaml_quarantine_reasons_are_machine_codes(corpora: Corpora) -> None:
    """02 §D's triage contract: a code a run can group by, not a sentence it must read.

    The two bad rows in this fixture are hand-built to fail two different rules, so the
    reason set is exactly ``{amount_format, label_not_binary}`` and not one code repeated.
    """
    adapter: Any = _ibm_mixed(corpora)
    assert len(list(adapter.iter_canonical())) == CLEAN_ROWS
    assert frozenset(record.reason for record in adapter.quarantine_records) == (
        IBM_QUARANTINE_REASONS
    ), f"reasons were {sorted({r.reason for r in adapter.quarantine_records})}"
    # ``row_index`` is the zero-based data row of the source file: the three good rows are
    # 0, 1 and 2, so the two unmappable ones are 3 and 4 and a triage run can find the bytes.
    assert [record.row_index for record in adapter.quarantine_records] == [
        CLEAN_ROWS,
        CLEAN_ROWS + 1,
    ], (
        f"row indexes were {[r.row_index for r in adapter.quarantine_records]}; the offsets "
        "this fixture hand-counted are 3 and 4, and an index into whichever chunk happened to "
        "carry the row points at the wrong line"
    )


def test_batch_manifest_validation_refuses_the_three_ways_a_hand_typed_one_goes_wrong() -> None:
    """The manifest check is only worth having if it bites on each of its three rules."""
    with pytest.raises(ValueError, match="row_count cannot be negative"):
        BatchManifest(
            batch_id=BATCH_ID,
            row_count=-1,
            sha256="0" * 64,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            source_system=CSV_SOURCE,
        ).validate()
    with pytest.raises(ValueError, match="window_end precedes window_start"):
        BatchManifest(
            batch_id=BATCH_ID,
            row_count=1,
            sha256="0" * 64,
            window_start=WINDOW_END,
            window_end=WINDOW_START,
            source_system=CSV_SOURCE,
        ).validate()
    with pytest.raises(ValueError, match="64 hex"):
        BatchManifest(
            batch_id=BATCH_ID,
            row_count=1,
            sha256="deadbeef",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            source_system=CSV_SOURCE,
        ).validate()


# --- what the port declares and the tree has not built -------------------------


def test_paysim_does_not_yet_implement_the_port_it_claims() -> None:
    """A claim with no adapter behind it, pinned so it cannot stay silent.

    ``ingest/paysim.py``'s docstring says it *is* the ``SourceAdapter`` implementation for
    Module A — the primary corpus. What the module exports is ``ingest_paysim()``, returning
    an ``IngestResult``: a stage result with no ``source_id``, no ``read_manifest``, no
    ``iter_canonical`` and no ``quarantine``, so there is nothing for plan §13's contract test
    to run against, and the register above covers the other corpora only. When the adapter
    lands this test fails, and its replacement is one more ``ADAPTERS`` entry.
    """
    port_members = (
        "source_id",
        "read_manifest",
        "iter_canonical",
        "quarantine",
        "quarantine_count",
    )
    absent = [member for member in port_members if not hasattr(paysim.IngestResult, member)]
    assert "read_manifest" in absent and "iter_canonical" in absent, (
        f"IngestResult now carries {sorted(set(port_members) - set(absent))} of the port: if it "
        "grew the whole surface, it belongs in ADAPTERS and this test goes"
    )

    claiming = [
        name
        for name, member in vars(paysim).items()
        if isinstance(member, type)
        and all(hasattr(member, attribute) for attribute in port_members)
    ]
    assert claiming == [], (
        f"{claiming} satisfies SourceAdapter: register it in ADAPTERS and delete this test, or "
        "the P7 gate is one adapter short again — for the primary corpus, which is the one the "
        "demo runs"
    )


def test_the_streaming_port_is_declared_and_deliberately_not_built() -> None:
    """02 §C: pattern 3 is designed, not built, and that has to be true rather than stated.

    A streaming path would force incremental graph maintenance — a quarter of work, not a
    week — so the protocol exists to be filled in later and nothing implements it now. The
    day somebody adds one, this fails and the honest answer is a register entry plus the
    same contract tests, not a new exception path.
    """
    root = REPO_ROOT / "packages" / "pipeline" / "oxbow"
    implementations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "source.py" and path.parent.name == "ports":
            continue
        if "def subscribe(" in path.read_text(encoding="utf-8"):
            implementations.append(path.relative_to(REPO_ROOT).as_posix())

    assert implementations == [], (
        f"{implementations} implement StreamSourceAdapter.subscribe, which plan §13 and "
        "02 §C record as deliberately unbuilt: cover it in this file or remove it"
    )
    # The protocol is importable and runtime-checkable, so a Kafka consumer that lands fails
    # the assertion above rather than quietly joining the register.
    assert hasattr(StreamSourceAdapter, "subscribe")


def test_no_batch_adapter_doubles_as_a_streaming_one(source: SourceCase) -> None:
    """Pattern 3 is not pattern 1 with a shorter sleep.

    A batch adapter that also answered ``subscribe`` would advertise near-real-time
    ingestion while it is re-reading a file, and 02 §C's argument for refusing that — the
    detection thesis is windowed — only holds while the two stay separate types.
    """
    assert not isinstance(source.adapter, StreamSourceAdapter), (
        f"{source.name} satisfies StreamSourceAdapter as well as SourceAdapter: it has grown "
        "a subscribe() that the plan records as deliberately unbuilt"
    )
