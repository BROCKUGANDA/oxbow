"""Port conformance for the report port, asserted against every delivery sink.

Plan §13 requires each port to carry a Protocol, a ``Null*`` implementation **and a
contract test both must pass**, and the P7 gate that checks it is
``pytest -q tests/contracts_adapters``. ``ReportSink`` is one of the eight ports that gate
runs over, and it is the port where a wrong number outlives its authors: a report whose row
totals disagree with its header is the discrepancy an auditor finds twelve months later, in
public, and the artifact that carries it says "OXBOW computed this".

So the port puts the arithmetic in one function, :meth:`ReportSubmission.verified`, and
every sink calls it before transmission. That is only worth anything if a test proves the
check can fail and that a sink refuses on it, which is what this file is for.

What the port actually forbids, and which test pins it:

* ``verified()`` is not decorative. A submission whose stated totals do not follow from its
  rows raises :class:`ReportIntegrityError` naming the key and both figures, and no sink
  transmits it -- including the shape that is easiest to ship by accident, a header full of
  money over zero rows;
* a report that is not self-describing is refused, not rounded down: money totals with no
  assumption set behind them are the fabricated-number shape plan §13 exists to stop;
* ``submit`` returns a receipt or raises. It never returns ``None``, never returns an
  ``accepted=False`` receipt for a delivery the destination refused, and never catches its
  way to a benign-looking result -- plan §18 lists "catching a broad exception and returning
  an empty list" as fatal because the UI then renders an empty state for a server bug, and
  the sink version is the same defect one layer closer to the wire;
* two submissions are two documents. The key is the ``report_id``, so distinct reports must
  not land on one record, and the receipt the outbox files must name the sink that produced
  it;
* the disclaimer, the run id, the totals and the assumption set travel with the payload, and
  the money stays integer minor units (01 B) all the way to what the destination holds.

All three sinks in the tree run on this host with nothing else up: the file and null sinks
land JSON in a temporary directory, and the GOAML sink writes through the null object store,
so no adapter is skipped and nothing here can go green on zero implementations --
``test_the_registry_still_covers_every_sink_in_the_tree`` is the floor under that claim.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from oxbow.adapters.file.sinks import FileReportSink
from oxbow.adapters.goaml.sinks import XML_CONTENT_TYPE, GoamlReportSink
from oxbow.adapters.null.objectstore import NullObjectStore
from oxbow.adapters.null.sinks import NullReportSink
from oxbow.ports.case_sink import (
    OXBOW_DISCLAIMER,
    SelfDescribingPayloadError,
    SinkReceipt,
)
from oxbow.ports.objectstore import ObjectConflict
from oxbow.ports.report import (
    MONEY_TOTAL_KEYS,
    REPORT_TYPES,
    ReportIntegrityError,
    ReportSink,
    ReportSubmission,
)

JSON_SUFFIX: Final = ".json"
REPORT_PREFIX: Final = "goaml/reports"
FOREIGN_XML: Final = b"<foreign-document>not-ours</foreign-document>"

# Period, identifiers and every money figure below are hand-computed and written into this
# file, not derived from the code under test. Money is integer minor units: 12_500 minor is
# USD 125.00, and the three rows sum to the totals the header states --
#   exposure:      12_500 + 7_500 + 3_000 = 23_000
#   loss avoided:   4_000 + 1_000 +     0 =  5_000
#   net benefit:    8_500 + 6_500 + 3_000 = 18_000   (= 23_000 - 5_000)
# so exactly one of these fixtures is arithmetically true, and every failing case below is
# one deliberate minor-unit error away from it.
PERIOD_START: Final = datetime(2026, 9, 1, tzinfo=UTC)
PERIOD_END: Final = datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
RUN_ID: Final = "RUN-2026-09-30-01"
MODEL_VERSION: Final = "ob-2026-09-17"
CURRENCY: Final = "USD"
REPORT_ID: Final = "RPT-2026-09"
OTHER_REPORT_ID: Final = "RPT-2026-08"

ROWS: Final[Sequence[Mapping[str, Any]]] = (
    {
        "bucket": "2026-W35",
        "cases": 41,
        "exposure_minor": 12_500,
        "loss_avoided_minor": 4_000,
        "net_benefit_minor": 8_500,
    },
    {
        "bucket": "2026-W36",
        "cases": 28,
        "exposure_minor": 7_500,
        "loss_avoided_minor": 1_000,
        "net_benefit_minor": 6_500,
    },
    {
        "bucket": "2026-W37",
        "cases": 13,
        "exposure_minor": 3_000,
        "loss_avoided_minor": 0,
        "net_benefit_minor": 3_000,
    },
)
TOTALS: Final[Mapping[str, Any]] = {
    "exposure_minor": 23_000,
    "loss_avoided_minor": 5_000,
    "net_benefit_minor": 18_000,
}
ASSUMPTIONS: Final[Mapping[str, Any]] = {
    "recovery_rate": 0.85,
    "analyst_cost_minor": 42_000,
    "friction_cost_minor": 9_000,
}

# The wire form a consumer receives, assembled by hand from the port's field list and the
# constants above. Nothing here is produced by the code under test, so a dropped or renamed
# field fails this file rather than a packet.
EXPECTED_PAYLOAD: Final[dict[str, Any]] = {
    "schema_version": "1.0",
    "advisory_only": True,
    "disclaimer": OXBOW_DISCLAIMER,
    "report_id": REPORT_ID,
    "report_type": "period_summary",
    "run_id": RUN_ID,
    "currency": "USD",
    "period_start": "2026-09-01T00:00:00Z",
    "period_end": "2026-09-30T23:59:59Z",
    "model_version": "ob-2026-09-17",
    "assumptions": {
        "recovery_rate": 0.85,
        "analyst_cost_minor": 42_000,
        "friction_cost_minor": 9_000,
    },
    "totals": {
        "exposure_minor": 23_000,
        "loss_avoided_minor": 5_000,
        "net_benefit_minor": 18_000,
    },
    "rows": [
        {
            "bucket": "2026-W35",
            "cases": 41,
            "exposure_minor": 12_500,
            "loss_avoided_minor": 4_000,
            "net_benefit_minor": 8_500,
        },
        {
            "bucket": "2026-W36",
            "cases": 28,
            "exposure_minor": 7_500,
            "loss_avoided_minor": 1_000,
            "net_benefit_minor": 6_500,
        },
        {
            "bucket": "2026-W37",
            "cases": 13,
            "exposure_minor": 3_000,
            "loss_avoided_minor": 0,
            "net_benefit_minor": 3_000,
        },
    ],
}


def _submission(**overrides: Any) -> ReportSubmission:
    """The reconciled period summary: totals that follow from the rows, assumptions attached."""
    fields: dict[str, Any] = {
        "report_id": REPORT_ID,
        "report_type": "period_summary",
        "run_id": RUN_ID,
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "rows": list(ROWS),
        "totals": dict(TOTALS),
        "assumptions": dict(ASSUMPTIONS),
        "model_version": MODEL_VERSION,
        "currency": CURRENCY,
    }
    fields.update(overrides)
    return ReportSubmission(**fields)


@dataclass(frozen=True, slots=True)
class Harness:
    """A sink, plus the only places its deliveries can honestly be counted.

    ``landed`` is the identities the destination holds, ``payloads`` the payload-form
    documents it holds, and ``text`` every landed body as text -- the medium-independent view
    of "what does the consumer actually have". ``payloads`` is empty for the GOAML sink
    because that destination holds an XML artifact, not the payload dict; asserting the
    payload there anyway would pass on nothing, so the XML gets its own test.

    ``refuse_next`` makes the next submit fail *at the destination* rather than at the port,
    and ``refusal`` is the exception that destination raises when it does. The port says
    "transmit one report, or raise" and each medium has its own way of raising, so the
    conformance assertion is about what must not come back: a receipt.
    """

    name: str
    sink: ReportSink
    landed: Callable[[], list[str]]
    payloads: Callable[[], list[dict[str, Any]]]
    text: Callable[[], str]
    refuse_next: Callable[[str], None]
    refusal: type[BaseException]


ReportFactory = Callable[[Path], Harness]


def _json_files(base: Path) -> list[Path]:
    return [path for path in sorted(base.glob(f"*{JSON_SUFFIX}")) if path.is_file()]


def _read_payloads(base: Path) -> list[dict[str, Any]]:
    """Every landed payload document, in filename order, skipping anything not a file."""
    documents: list[dict[str, Any]] = []
    for path in _json_files(base):
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            documents.append(parsed)
    return documents


def _disk_harness(name: str, sink: FileReportSink, base: Path) -> Harness:
    """A disk landing site, plus the way a disk says no: the atomic rename cannot complete."""

    def refuse_next(key: str) -> None:
        # ``write_json_atomic`` stages into ``<key>.json.partial`` and renames it onto the
        # final path. Occupying the staging path with a directory makes the write fail before
        # anything lands -- and the obstacle does not match ``*.json``, so nothing about it
        # can be mistaken for a delivered report.
        (base / f"{key}{JSON_SUFFIX}.partial").mkdir()

    return Harness(
        name=name,
        sink=sink,
        landed=lambda: [path.name[: -len(JSON_SUFFIX)] for path in _json_files(base)],
        payloads=lambda: _read_payloads(base),
        text=lambda: "\n".join(path.read_text(encoding="utf-8") for path in _json_files(base)),
        refuse_next=refuse_next,
        refusal=OSError,
    )


def _file(directory: Path) -> Harness:
    base = directory / "report"
    return _disk_harness("file", FileReportSink(base), base)


def _null(directory: Path) -> Harness:
    sink = NullReportSink(directory)
    assert isinstance(sink, FileReportSink), "the null sink is documented as a file sink"
    return _disk_harness("null", sink, sink.base_dir)


def _goaml(directory: Path) -> Harness:
    store = NullObjectStore(directory)
    sink = GoamlReportSink(store)

    def keys() -> list[str]:
        return sorted(ref.key for ref in store.list(REPORT_PREFIX))

    def refuse_next(key: str) -> None:
        # The null store is content-addressed and refuses to overwrite a key with different
        # bytes, so putting foreign bytes at the target key is how this destination says no.
        store.put(f"{REPORT_PREFIX}/{key}.xml", FOREIGN_XML, XML_CONTENT_TYPE)

    return Harness(
        name="goaml",
        sink=sink,
        landed=keys,
        # An XML destination holds no payload-form document; saying so here is the honest
        # answer, and the payload assertions run only against PAYLOAD_ADAPTERS.
        payloads=lambda: [],
        text=lambda: "\n".join(store.get(key).decode("utf-8") for key in keys()),
        refuse_next=refuse_next,
        refusal=ObjectConflict,
    )


# Every ReportSink in the tree. A new implementation is added here -- that is the intended
# way this file grows. There is no HTTP report sink yet: the webhook and Slack adapters
# deliver cases and notifications only, so nothing is registered here that does not exist.
ADAPTERS: dict[str, ReportFactory] = {
    "file": _file,
    "null": _null,
    "goaml": _goaml,
}

# The sinks whose destination is handed the payload document itself. GOAML renders XML, so
# what it must carry is asserted in its own test rather than by pretending it holds a dict.
PAYLOAD_ADAPTERS: Final = ("file", "null")

# Each case is (submission overrides, the figure the header states, the figure the rows
# actually sum to) -- one deliberate arithmetic error away from the fixture above.
INTEGRITY_FAILURES: Final = (
    pytest.param(
        {"totals": {**TOTALS, "exposure_minor": 23_001}},
        "totals.exposure_minor=23001",
        "rows sum to 23000",
        id="one-minor-unit-over",
    ),
    pytest.param(
        {"totals": {**TOTALS, "net_benefit_minor": 17_000}},
        "totals.net_benefit_minor=17000",
        "rows sum to 18000",
        id="benefit-overstated-by-a-thousand",
    ),
    pytest.param(
        {"rows": []},
        "totals.exposure_minor=23000",
        "rows sum to 0",
        id="a-header-of-money-over-no-rows",
    ),
)


def _ids() -> list[str]:
    return sorted(ADAPTERS)


@pytest.fixture(params=_ids())
def sink(request: pytest.FixtureRequest, tmp_path: Path) -> Harness:
    """Every ReportSink in the tree, against a destination nothing else has touched."""
    return ADAPTERS[request.param](tmp_path)


@pytest.fixture(params=PAYLOAD_ADAPTERS)
def payload_sink(request: pytest.FixtureRequest, tmp_path: Path) -> Harness:
    """The sinks a consumer receives the payload document from."""
    return ADAPTERS[request.param](tmp_path)


@pytest.fixture()
def goaml_sink(tmp_path: Path) -> Harness:
    """GOAML alone, for the assertions about the XML artifact an FIU would receive."""
    return _goaml(tmp_path)


# --- the port's structural promises -------------------------------------------


@pytest.mark.parametrize("name", _ids())
def test_satisfies_the_runtime_checkable_protocol(name: str, tmp_path: Path) -> None:
    """A duck that does not satisfy the Protocol is not an implementation of the port."""
    harness = ADAPTERS[name](tmp_path)
    assert isinstance(harness.sink, ReportSink), (
        f"{name} does not satisfy ReportSink; a report wired through a protocol the sink "
        "does not implement fails at the submit call site, in production, after the period "
        "it summarises has already been closed"
    )


def test_the_registry_still_covers_every_sink_in_the_tree() -> None:
    """The floor under this whole file: a register that emptied out tests nothing.

    P7's gate is ``pytest -q tests/contracts_adapters``. A register that quietly loses its
    entries is the same failure wearing a green checkmark -- the gate runs, collects a
    handful of tests against one adapter nobody broke, and reports conformance.
    """
    assert {"file", "null", "goaml"} <= set(ADAPTERS), (
        f"the register holds {sorted(ADAPTERS)}: a ReportSink dropped from ADAPTERS stops "
        "being checked here, and the port's contract becomes a docstring again"
    )
    assert set(PAYLOAD_ADAPTERS) <= set(ADAPTERS), (
        "a sub-register names an adapter that is not registered, so the payload assertions "
        "silently run against fewer sinks than this file claims to cover"
    )


def test_the_report_types_are_a_closed_set_and_every_money_key_is_checked() -> None:
    """The archive partitions on ``report_type``; the reconciliation loop is only as wide as
    ``MONEY_TOTAL_KEYS``, so both lists are pinned here before an adapter can widen either."""
    assert REPORT_TYPES == ("case_packet", "period_summary", "model_card_snapshot"), (
        "an open-ended report type splits the archive on a field whose spellings nobody "
        "agreed on, and a period report becomes invisible as a summary"
    )
    assert MONEY_TOTAL_KEYS == ("exposure_minor", "loss_avoided_minor", "net_benefit_minor"), (
        "a money total added to a report without being added to the keys verified() checks "
        "is a figure that leaves the building unchecked"
    )


# --- construction refusals ------------------------------------------------------


def test_an_unlisted_report_type_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="is not one of"):
        _submission(report_type="suspicious_activity_report")


def test_a_reversed_period_is_refused_at_construction() -> None:
    """A negative window is a bug, and the row sums would still reconcile -- that is the trap."""
    with pytest.raises(ValueError, match="period_end precedes period_start"):
        _submission(period_start=PERIOD_END, period_end=PERIOD_START)


# --- verified(): the arithmetic, asserted to be capable of failing ---------------


@pytest.mark.parametrize(("overrides", "stated", "summed"), INTEGRITY_FAILURES)
def test_verified_refuses_totals_that_do_not_follow_from_the_rows(
    overrides: dict[str, Any], stated: str, summed: str
) -> None:
    """The check the port exists for, and the error that names both numbers.

    An operator reading this has to see which figure and what the rows said, because the
    temptation at 6pm is to edit the header until it matches. The message tells them that is
    the wrong direction.
    """
    submission = _submission(**overrides)

    with pytest.raises(ReportIntegrityError) as excinfo:
        submission.verified()

    message = str(excinfo.value)
    assert stated in message, f"expected the stated figure in the error, got: {message}"
    assert summed in message, f"expected the recomputed figure in the error, got: {message}"
    assert "Fix the aggregation, do not adjust the total." in message, (
        "an integrity error that does not say which side is wrong gets resolved by editing "
        "the header, which is how a report starts lying"
    )
    # validated_payload() calls verified() first, so the refusal propagates to the wire gate.
    with pytest.raises(ReportIntegrityError):
        submission.validated_payload()


def test_verified_returns_the_submission_and_its_payload_says_the_values_hand_computed() -> None:
    """The good case, asserted against a literal written by hand rather than a round trip.

    If ``verified()`` were a no-op this test would still pass on its own -- which is exactly
    why the failing cases above are separate tests rather than an afterthought. Together the
    two prove the comparison happens: the same method raises on one minor unit of drift and
    returns here.
    """
    submission = _submission()

    returned = submission.verified()

    assert returned is submission, (
        "verified() is documented to return the submission so a sink can call it inline; "
        "returning None instead means the caller's next statement is an AttributeError "
        "wrapped in a 'delivery failed' message"
    )
    assert returned.to_payload() == EXPECTED_PAYLOAD, (
        "the payload is not the header-plus-rows the fixture says it is: a field dropped "
        "here is a field the consumer never receives, and a period report missing its "
        "assumptions is a set of money figures with no stated basis"
    )
    totals = returned.to_payload()["totals"]
    assert all(isinstance(totals[key], int) for key in MONEY_TOTAL_KEYS), (
        "a money total that reached the wire as a float is the drift 01 B and "
        "scripts/no_float_money exist to prevent (minor units are integers)"
    )


# --- what every sink must do ----------------------------------------------------


@pytest.mark.parametrize(("overrides", "stated", "_summed"), INTEGRITY_FAILURES)
def test_a_report_that_does_not_add_up_is_refused_before_the_destination_is_touched(
    sink: Harness, overrides: dict[str, Any], stated: str, _summed: str
) -> None:
    """The port's promise, checked at the boundary rather than on the dataclass.

    Every sink calls ``validated_payload()`` before it writes or posts, so a report whose
    totals disagree must never become a landed artifact. Sending it is the failure the whole
    check exists for: a reconciled-looking document carrying an unreconciled number.
    """
    with pytest.raises(ReportIntegrityError, match="rows sum to"):
        sink.sink.submit(_submission(**overrides))

    assert sink.landed() == [], f"something landed for {sink.name}: {stated} shipped anyway"


def test_a_report_of_money_with_no_assumption_set_behind_it_is_refused(
    sink: Harness,
) -> None:
    """plan §13 applied to a period: the same rule that governs a single case.

    The recovery rate, the analyst cost and the friction cost are what make a net-benefit
    figure mean anything. A submission with an empty assumption set *constructs* -- nothing
    in ``__post_init__`` stops it -- so the self-describing gate at submit time is the only
    thing between it and an FIU or an archive.
    """
    unattributed = _submission(assumptions={})

    with pytest.raises(SelfDescribingPayloadError, match="missing assumptions"):
        sink.sink.submit(unattributed)

    assert sink.landed() == []


def test_submit_returns_a_receipt_and_never_an_empty_success(sink: Harness) -> None:
    """§18's fatal pattern, tested from the caller's side: no receipt, or a fake one.

    An ``accepted=False`` receipt for a delivery that never happened would be indistinguishable
    downstream from a consumer's 4xx refusal, and the archive would render "no reports for
    this period" over a sink that simply failed.
    """
    assert sink.sink.sink_id, "an empty sink_id would make every ledger row collide"

    receipt = sink.sink.submit(_submission())

    assert receipt is not None and isinstance(receipt, SinkReceipt), (
        f"{sink.name} returned {receipt!r}: a caller that files the outbox row has nothing "
        "to write, and the delivery is unaccounted for"
    )
    assert receipt.accepted is True, (
        "a sink that accepts a report and returns accepted=False teaches the caller to "
        "retry a delivery that already landed"
    )
    assert receipt.idempotency_key == REPORT_ID, (
        "the key is the report id; a receipt naming anything else is a ledger row that "
        "cannot be matched to the artifact it claims to describe"
    )
    assert sink.sink.sink_id in receipt.consumer, (
        "the receipt has to name the sink that produced it, or a missing delivery cannot be "
        "attributed to a destination"
    )
    accepted_at = receipt.accepted_at
    assert accepted_at.tzinfo is not None, (
        "a receipt timestamp with no zone cannot be ordered against the period it covers, "
        "and an hour-hunting reconciliation bug is the result"
    )


def test_two_submissions_do_not_collide_on_one_key(sink: Harness) -> None:
    """September and August are different reports; one record for both is a lost period.

    The key is the ``report_id``, so distinct ids must produce distinct receipts *and* two
    distinct landed documents. A sink that folded them together would leave the archive
    holding one period while the ledger claimed two.
    """
    first = sink.sink.submit(_submission())
    second = sink.sink.submit(_submission(report_id=OTHER_REPORT_ID))
    landed = sink.landed()

    assert first.idempotency_key != second.idempotency_key, (
        f"{sink.name} gave two different reports the same key: one of them is now "
        "unaddressable, and a delivery deduped against it is a delivery never made"
    )
    assert len(landed) == 2, (
        f"{sink.name} holds {landed} after two submits: one period overwrote the other, and "
        "the missing one is invisible rather than reported as a failure"
    )
    for report_id in (REPORT_ID, OTHER_REPORT_ID):
        copies = sum(1 for identity in landed if report_id in identity)
        assert copies == 1, f"{report_id} appears in {copies} of {landed}, not exactly one"


def test_a_destination_that_refuses_is_not_reported_as_a_success(sink: Harness) -> None:
    """The failure the port cannot fake: transmit, or raise -- never both.

    The destination is made to refuse the bytes (a disk that cannot complete the atomic
    rename, a store that will not overwrite different bytes under one key). The sink must let
    that reach the caller, because the outbox's retry ladder is the only thing that will ever
    try again and it acts only on an exception. The snapshot is taken *after* the refusal is
    armed, so what is being compared is the destination's state across the submit.
    """
    sink.refuse_next(REPORT_ID)
    before = sink.landed()
    before_text = sink.text()

    with pytest.raises(sink.refusal):
        sink.sink.submit(_submission())

    assert sink.landed() == before, (
        f"{sink.name} recorded a document for a submit the destination refused: the retry "
        "would now be suppressed and the period silently never delivered"
    )
    assert sink.text() == before_text, (
        "the refused submit changed the bytes at the destination anyway, so the archive now "
        "holds a document no receipt accounts for"
    )


# --- per-destination: what the consumer actually holds --------------------------


def test_the_landed_document_is_the_payload_written_by_hand_here(
    payload_sink: Harness,
) -> None:
    """The archive reads these bytes, so byte-level agreement with the fixture is the contract.

    Asserted through the ``payload_sink`` fixture, which covers exactly the sinks whose
    destination holds the payload document. Running it against GOAML would compare an empty
    list against a literal and pass for the wrong reason.
    """
    payload_sink.sink.submit(_submission())

    assert payload_sink.payloads() == [EXPECTED_PAYLOAD], (
        "the landed report is not the totals-checked, self-describing payload: a period "
        "report that arrives without its run id, its model version or its assumptions is a "
        "sheet of numbers with no provenance (plan §13)"
    )
    assert json.loads(payload_sink.text())["currency"] == CURRENCY, (
        "every minor-unit figure in the report is meaningless without the currency it is "
        "denominated in"
    )


def test_the_goaml_draft_carries_the_money_with_its_scale_and_its_advisory_note(
    goaml_sink: Harness,
) -> None:
    """What an FIU-facing artifact must never lose, in the medium that has no payload dict.

    The GOAML renderer emits a DRAFT document with an advisory note rather than the payload
    dict, and it states minor-unit scale explicitly (DEV-005): an unlabelled ``23000`` reads
    as twenty-three thousand to a receiver that assumes decimal amounts, and as two hundred
    thirty when it does not. The sink validates the submission's self-describing payload and
    then renders from it, so these are the fields the render cannot afford to drop.

    Reported, not asserted: the rendered document carries the report id, the exposure total,
    the currency and the row count, and drops ``run_id``, ``model_version`` and the
    assumption set that the sink checked one line earlier. The artifact an FIU would file
    from therefore does not carry what its numbers depend on (plan §13), which the payload
    sinks above do guarantee. When the renderer is extended to carry them, extend this test
    to assert it.
    """
    goaml_sink.sink.submit(_submission())

    xml = goaml_sink.text()

    assert goaml_sink.landed() == [f"{REPORT_PREFIX}/{REPORT_ID}.xml"]
    assert 'Action="DRAFT"' in xml, (
        "a GOAML document that does not say DRAFT is a document that reads like a filing, "
        "and 02 §F keeps the reporting decision on the human side of that line"
    )
    assert "23000" in xml, "the exposure total has to reach the artifact, not just the receipt"
    assert "ValueScale" in xml and 'currency="USD"' in xml, (
        "a minor-unit figure with no scale and no currency is a number someone will "
        "re-interpret, badly (DEV-005)"
    )
    assert "OXBOW-REPORT-RPT-2026-09" in xml
    # The report's rows are period aggregates, and rendering them as transactions used to invent
    # three fields per bucket: a blank TransactionId, an amount of 0 and ISO 4217 'XXX' (no
    # currency). Asserting their absence is what keeps the fix from quietly reversing.
    assert "XXX" not in xml, "ISO 4217 'no currency' must not appear in a document of USD totals"
    assert (
        "<TransactionId></TransactionId>" not in xml and "TransactionId" not in xml
    ), "the report has no transactions to identify; an empty id is a fabricated one"
    for figure in ("12500", "4000", "8500", "7500", "3000"):
        assert figure in xml, f"bucket {figure} never reached the artifact: {xml[:400]}"
    assert (
        xml.count('currency="USD"') == 10
    ), "three money totals for each of three buckets, plus the branch summary total"
    assert "ReportedActivityBucket" in xml and "PeriodicCompletion" in xml
    assert "not a filed report and not a decision" in xml, (
        "the advisory note is the disclaimer's form in this medium; a report artifact "
        "without it is more confident than the JSON behind it"
    )
    assert "<Rowcount>3</Rowcount>" in xml, (
        "three rows are what the fixture has; a rowcount that disagrees with the totals is "
        "the discrepancy this file's other tests refuse to ship"
    )
