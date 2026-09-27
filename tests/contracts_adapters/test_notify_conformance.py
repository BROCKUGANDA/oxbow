"""Port conformance for the notify port, asserted against every delivery sink.

Plan §13 requires each port to carry a Protocol, a ``Null*`` implementation **and a
contract test both must pass**, and the P7 gate that checks it is
``pytest -q tests/contracts_adapters``. ``NotifySink`` is one of the eight ports that gate
runs over, and it is the port whose failures are hardest to see: a notification that never
arrived leaves no row, no problem document and no artifact. A person simply was not told,
and the case they were meant to open stays unreviewed. The port's own words are the rule
being enforced -- "a notification that silently did not arrive is worse than one that
visibly failed" (03 §A rule 1) -- so every promise below is stated as something a caller
can *observe*, not as something the adapter intended.

What the port forbids, and which test pins it:

* a ``Notification`` short a field the wire contract needs is refused before the transport
  is touched. Half a message reads like a complete one, which is worse than silence;
* idempotency is observable. ``has_delivered(key)`` must answer the only question the
  outbox asks -- "already sent?" -- and the destination must hold exactly one record per
  key however many deliveries it was handed (02 §E: at-least-once delivery with an
  idempotent consumer, and §13: "the database is the only source of truth about what has
  been sent");
* a delivery the destination refused is not remembered as a delivery, because a sink that
  files an attempt as a success never retries it and the message is simply gone;
* the receipt names the sink that produced it and no two sinks share a name, so a missing
  delivery can be attributed to a channel rather than to "the delivery layer";
* the disclaimer, the advisory flag, the run id and the basis of any money figure travel
  with the payload, and a payload assembled without them is refused rather than sent
  (plan §13: "a consumer cannot receive an OXBOW number without also receiving what it
  depends on").

Slack's incoming-webhook URL and the notify endpoint are not reachable on this host, so
both HTTP sinks run against ``httpx.MockTransport``: the real ``notify()``, the real status
classification, the real receipt path, with only the far end of the wire substituted. Their
URLs use the ``.invalid`` domain reserved by RFC 2606, so there is no name resolution left
to quietly succeed -- if a socket were ever opened here the test would error, not pass. No
adapter is skipped, and ``test_the_registry_still_covers_every_sink_in_the_tree`` is the
floor under that: a register that quietly emptied out would otherwise turn this file green on
zero implementations, which is the quieter twin of the bug where the gate collected no tests
at all.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from oxbow.adapters.file.sinks import FileNotifySink
from oxbow.adapters.null.sinks import NullNotifySink
from oxbow.adapters.slack.notify import ADVISORY_LINE, SlackNotifySink
from oxbow.adapters.webhook.sinks import DeliveryError, WebhookNotifySink
from oxbow.ports.case_sink import (
    OXBOW_DISCLAIMER,
    SelfDescribingPayloadError,
    SinkReceipt,
    assert_self_describing,
)
from oxbow.ports.notify import (
    ADVISORY_ONLY,
    NOTIFICATION_SEVERITIES,
    Notification,
    NotifySink,
)

JSON_SUFFIX: Final = ".json"

# Fixture values, hand-computed and written into this file rather than derived from the
# code under test. MONEY_MINOR is USD 24,823.31 in minor units, which is 2_482_331: money is
# integer minor units (01 B), and scripts/no_float_money fails the build when a ``*_minor``
# value is typed as a float, because a float drifts instead of crashing.
OCCURRED_AT: Final = datetime(2026, 9, 22, 8, 30, tzinfo=UTC)
OCCURRED_AT_Z: Final = "2026-09-22T08:30:00Z"
RUN_ID: Final = "RUN-2026-09-22-01"
NOTIFICATION_ID: Final = "NOTIF-2026-09-22-0001"
CASE_ID: Final = "01CASE00000000000000000001"
ACCOUNT_KEY: Final = "ACCTNOTIFICATION001"
MONEY_MINOR: Final = 2_482_331
CURRENCY: Final = "USD"
MODEL_VERSION: Final = "ob-2026-09-17"
ASSUMPTIONS: Final[Mapping[str, Any]] = {"recovery_rate": 0.85, "analyst_cost_minor": 42_000}
TITLE: Final = "Two accounts sit above the review budget"
BODY: Final = "Everything below the capacity line is being consciously left unreviewed."

# A fixture signing key for a substituted transport. The real webhook secret lives in the
# gitignored .env and appears nowhere in this file.
TEST_SIGNING_SECRET: Final = "contract_test_signing_secret"

# The payload the fixture notification must produce, assembled by hand from the port's wire
# form and the constants above. ``disclaimer`` is the port constant because the contract *is*
# "the payload carries exactly this sentence, verbatim".
EXPECTED_PAYLOAD: Final[dict[str, Any]] = {
    "schema_version": "1.0",
    "advisory_only": True,
    "disclaimer": OXBOW_DISCLAIMER,
    "notification_id": "NOTIF-2026-09-22-0001",
    "run_id": "RUN-2026-09-22-01",
    "case_id": "01CASE00000000000000000001",
    "account_key": "ACCTNOTIFICATION001",
    "severity": "warning",
    "title": TITLE,
    "body": BODY,
    "occurred_at": "2026-09-22T08:30:00Z",
    "requires_four_eyes": True,
    "expected_value_minor": 2_482_331,
    "currency": "USD",
    "assumptions": {"recovery_rate": 0.85, "analyst_cost_minor": 42_000},
    "model_version": "ob-2026-09-17",
}


def _notification(**overrides: Any) -> Notification:
    """The fixture notification: complete, money-bearing, still awaiting second review."""
    fields: dict[str, Any] = {
        "notification_id": NOTIFICATION_ID,
        "run_id": RUN_ID,
        "severity": "warning",
        "title": TITLE,
        "body": BODY,
        "occurred_at": OCCURRED_AT,
        "case_id": CASE_ID,
        "account_key": ACCOUNT_KEY,
        "requires_four_eyes": True,
        "money_minor": MONEY_MINOR,
        "currency": CURRENCY,
        "assumptions": dict(ASSUMPTIONS),
        "model_version": MODEL_VERSION,
    }
    fields.update(overrides)
    return Notification(**fields)


@dataclass(frozen=True, slots=True)
class Harness:
    """A sink, plus the only place its own record of a delivery can be read.

    ``documents`` is what the *destination* holds, one entry per delivery it observed: the
    JSON files on disk for the file and null sinks, the bodies the substituted far end
    received for the HTTP ones. That is the honest measure of "did it ship", because both
    media answer the same question -- how many records does the consumer have.

    ``refuse_next`` makes the next delivery of a given key fail at the destination rather
    than at the port, and ``refusal`` is the exception that destination raises when it does.
    The port says "transmit one notification, or raise", and each medium has its own way of
    raising, so the conformance assertion is about the three things that must *not* happen --
    a receipt, a remembered delivery, a landed document.
    """

    name: str
    sink: NotifySink
    documents: Callable[[], list[dict[str, Any]]]
    refuse_next: Callable[[str], None]
    refusal: type[BaseException]
    close: Callable[[], None]


SinkFactory = Callable[[Path], Harness]


def _nothing_to_close() -> None:
    """The disk sinks open no client and hold no socket."""


def _read_documents(base: Path) -> list[dict[str, Any]]:
    """Every landed document, in filename order, skipping anything that is not a file."""
    documents: list[dict[str, Any]] = []
    for path in sorted(base.glob(f"*{JSON_SUFFIX}")):
        if not path.is_file():
            continue
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            documents.append(parsed)
    return documents


def _disk_harness(name: str, sink: FileNotifySink, base: Path) -> Harness:
    """A disk landing site, plus the way a disk says no: the atomic rename cannot complete."""

    def refuse_next(key: str) -> None:
        # ``write_json_atomic`` stages into ``<key>.json.partial`` and renames it. Occupying
        # the staging path with a directory makes the write fail before anything lands --
        # and the obstacle does not match ``*.json``, so the dedupe check is not fooled into
        # reporting a delivery that never happened.
        (base / f"{key}{JSON_SUFFIX}.partial").mkdir()

    return Harness(
        name=name,
        sink=sink,
        documents=lambda: _read_documents(base),
        refuse_next=refuse_next,
        refusal=OSError,
        close=_nothing_to_close,
    )


class _Endpoint:
    """The far end of the wire for the two HTTP sinks: what it received, and whether it is up."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.unreachable = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("the consumer is refusing connections", request=request)
        parsed: Any = json.loads(request.content)
        self.received.append(parsed if isinstance(parsed, dict) else {"body": parsed})
        return httpx.Response(200, json={"accepted": True})

    def refuse_next(self, _key: str) -> None:
        self.unreachable = True


def _http_harness(
    name: str, sink: NotifySink, endpoint: _Endpoint, client: httpx.Client
) -> Harness:
    return Harness(
        name=name,
        sink=sink,
        documents=lambda: list(endpoint.received),
        refuse_next=endpoint.refuse_next,
        refusal=DeliveryError,
        close=client.close,
    )


def _file(directory: Path) -> Harness:
    base = directory / "notify"
    return _disk_harness("file", FileNotifySink(base), base)


def _null(directory: Path) -> Harness:
    sink = NullNotifySink(directory)
    assert isinstance(sink, FileNotifySink), "the null sink is documented as a file sink"
    return _disk_harness("null", sink, sink.base_dir)


def _slack(_directory: Path) -> Harness:
    endpoint = _Endpoint()
    client = httpx.Client(transport=httpx.MockTransport(endpoint.handler))
    sink = SlackNotifySink(
        webhook_url="https://hooks.slack.invalid.test/services/T00000000/B00000000/XXXX",
        client=client,
    )
    return _http_harness("slack", sink, endpoint, client)


def _webhook(_directory: Path) -> Harness:
    endpoint = _Endpoint()
    client = httpx.Client(transport=httpx.MockTransport(endpoint.handler))
    sink = WebhookNotifySink(
        url="https://consumer.invalid.test/oxbow/notify",
        secret=TEST_SIGNING_SECRET,
        client=client,
    )
    return _http_harness("webhook", sink, endpoint, client)


# Every NotifySink in the tree. A new implementation is added here -- that is the intended
# way this file grows. (The GOAML and S3 families carry cases and reports, not
# notifications, so none of them appears.)
ADAPTERS: dict[str, SinkFactory] = {
    "file": _file,
    "null": _null,
    "slack": _slack,
    "webhook": _webhook,
}

# The three sinks whose destination is handed ``validated_payload()`` itself. Slack renders
# Block Kit text for a human instead, so what it must carry is asserted in its own test --
# quietly folding Slack in here would have this file assert a property Slack does not ship.
PAYLOAD_ADAPTERS: Final = ("file", "null", "webhook")


def _ids() -> list[str]:
    return sorted(ADAPTERS)


def _open(name: str, tmp_path: Path) -> Iterator[Harness]:
    harness = ADAPTERS[name](tmp_path)
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture(params=_ids())
def sink(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    """Every NotifySink in the tree, against a destination nothing else has touched."""
    yield from _open(request.param, tmp_path)


@pytest.fixture(params=PAYLOAD_ADAPTERS)
def payload_sink(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    """The sinks a consumer receives the payload bytes from."""
    yield from _open(request.param, tmp_path)


@pytest.fixture()
def slack_sink(tmp_path: Path) -> Iterator[Harness]:
    """Slack alone, for the assertions about what a person reading the message can see."""
    yield from _open("slack", tmp_path)


# --- the port's structural promises -------------------------------------------


@pytest.mark.parametrize("name", _ids())
def test_satisfies_the_runtime_checkable_protocol(name: str, tmp_path: Path) -> None:
    """A duck that does not satisfy the Protocol is not an implementation of the port."""
    harness = ADAPTERS[name](tmp_path)
    try:
        assert isinstance(harness.sink, NotifySink), (
            f"{name} does not satisfy NotifySink; a decision wired through a protocol the "
            "sink does not implement fails at the notify call site, in production, after "
            "the audit row is already committed"
        )
    finally:
        harness.close()


def test_the_registry_still_covers_every_sink_in_the_tree() -> None:
    """The floor under this whole file: a register that emptied out tests nothing.

    P7's gate is ``pytest -q tests/contracts_adapters``. When the directory held one fixture
    and no tests it collected nothing and exited 5; a register that quietly loses its entries
    is the same failure wearing a green checkmark.
    """
    assert {"file", "null", "slack", "webhook"} <= set(ADAPTERS), (
        f"the register holds {sorted(ADAPTERS)}: a NotifySink dropped from ADAPTERS stops "
        "being checked here, and the port's contract becomes a docstring again"
    )
    assert set(PAYLOAD_ADAPTERS) <= set(ADAPTERS), (
        "a sub-register names an adapter that is not registered, so the payload assertions "
        "silently run against fewer sinks than this file claims to cover"
    )


def test_the_severity_set_is_closed_and_the_advisory_flag_is_not_configurable() -> None:
    """02 §F: a notification informs, it never decides, and the queue sorts on severity."""
    assert NOTIFICATION_SEVERITIES == ("info", "warning", "critical"), (
        "an open-ended severity string becomes five spellings of the same urgency across "
        "three adapters, and the queue sorts them into an order nobody chose"
    )
    assert ADVISORY_ONLY is True, (
        "advisory_only is a constant the payload adds, never a field a caller can set to "
        "False; a notification that stops saying it is advisory has begun pretending to be "
        "a decision"
    )


# --- the refusals: no half-message ships ---------------------------------------


def test_an_unknown_severity_is_refused_at_construction() -> None:
    """``severity`` is a closed set, checked where the message is built, not at the wire."""
    with pytest.raises(ValueError, match="is not one of"):
        _notification(severity="urgent")


def test_a_blank_title_or_body_is_refused_at_construction() -> None:
    """Empty text is a bug wearing a message's clothes (02 §A)."""
    for blank in ({"title": "   "}, {"body": ""}, {"title": "", "body": ""}):
        with pytest.raises(ValueError, match="title and a body"):
            _notification(**blank)


def test_a_money_figure_without_its_currency_and_assumptions_is_refused() -> None:
    """plan §13 at the port's own front door: a number with no stated basis."""
    with pytest.raises(ValueError, match="currency and assumptions"):
        _notification(currency=None, assumptions=None)
    with pytest.raises(ValueError, match="currency and assumptions"):
        _notification(assumptions=None)


def test_a_money_figure_with_no_model_behind_it_never_reaches_the_transport(
    sink: Harness,
) -> None:
    """The second gate, and the one that catches what the first lets through.

    ``__post_init__`` demands currency and assumptions for a money figure but not the model
    version, so this notification *constructs*. It is refused at ``validated_payload()``,
    which every sink calls before it touches a wire or a disk. Without that second gate a
    Slack message would carry "2482331 USD" with nothing saying which model estimated it --
    the fabricated-number shape 03 §A rule 2 is about.
    """
    orphaned = _notification(model_version=None)
    with pytest.raises(SelfDescribingPayloadError, match="missing model_version"):
        orphaned.validated_payload()

    with pytest.raises(SelfDescribingPayloadError, match="missing model_version"):
        sink.sink.notify(orphaned)

    assert sink.documents() == [], "the refusal has to happen before anything ships"
    assert not sink.sink.has_delivered(NOTIFICATION_ID)


def test_a_notification_with_no_run_id_is_refused_rather_than_sent(sink: Harness) -> None:
    """A message with no run id cannot be traced back to the artifact that produced it.

    ``run_id`` is unchecked at construction and enforced by the same self-describing gate,
    so an unattributable notification is refused instead of delivered, and a reviewer
    asking "which run said this?" still gets an answer.
    """
    unattributed = _notification(run_id="")
    with pytest.raises(SelfDescribingPayloadError, match="missing run_id"):
        sink.sink.notify(unattributed)

    assert sink.documents() == []


def test_a_payload_assembled_without_the_disclaimer_is_refused() -> None:
    """The gate, tested on its own, because ``to_payload`` adds the disclaimer by design.

    No caller can build a ``Notification`` that omits it, which means the only way to learn
    whether the gate would catch one is to hand it the payload a sloppy re-serialiser would
    produce. This is the rule all three payload-bearing sinks share, so it is asserted once
    here instead of three times against three adapters that all call the same function.
    """
    stripped = dict(EXPECTED_PAYLOAD)
    stripped.pop("disclaimer")
    with pytest.raises(SelfDescribingPayloadError, match="missing disclaimer"):
        assert_self_describing(stripped)

    no_basis = dict(EXPECTED_PAYLOAD)
    no_basis["assumptions"] = {}
    with pytest.raises(SelfDescribingPayloadError, match="missing assumptions"):
        assert_self_describing(no_basis)


# --- the promises a caller can observe -----------------------------------------


def test_the_receipt_names_the_sink_that_produced_it(sink: Harness) -> None:
    """Attribution: a delivery ledger that cannot say which channel answered is useless."""
    assert sink.sink.sink_id, "an empty sink_id would make every ledger row collide"

    receipt = sink.sink.notify(_notification())

    assert isinstance(receipt, SinkReceipt), (
        "notify returned something other than a receipt, so the outbox has no row to write "
        "and the delivery is unaccounted for"
    )
    assert receipt.accepted is True
    assert receipt.consumer == sink.sink.sink_id, (
        f"{sink.name} filed a receipt under {receipt.consumer!r} while calling itself "
        f"{sink.sink.sink_id!r}: a ledger row that cannot be traced to a channel cannot be "
        "re-delivered through one"
    )
    assert receipt.idempotency_key == NOTIFICATION_ID, (
        "the key on the receipt is the key has_delivered is asked about; if they differ, "
        "the ledger records one delivery and dedupes another"
    )
    assert receipt.accepted_at.tzinfo is not None, (
        "a receipt timestamp with no zone cannot be ordered against the outbox row that "
        "scheduled it"
    )


def test_idempotency_is_observable_through_has_delivered(sink: Harness) -> None:
    """§13: the outbox must be able to answer "already sent", and the record must say once.

    An at-least-once sink may ship twice; what the port owes the caller is a *detectable*
    second delivery. So: False before, True after, the receipt's own key is the key the
    check reads, and however many deliveries the destination was handed it holds one record
    for that key. Otherwise "sent" and "sent twice" are the same row -- and, in the other
    direction, a retry gets suppressed because the ledger could not see the first send.
    """
    assert sink.sink.has_delivered(NOTIFICATION_ID) is False, (
        "a sink that reports every key as already delivered suppresses the retry, and the "
        "notification is lost with no error anywhere"
    )

    receipt = sink.sink.notify(_notification())
    assert sink.sink.has_delivered(receipt.idempotency_key) is True, (
        "has_delivered stayed False after a completed delivery, so the outbox cannot tell "
        "an unsent notification from one already shipped"
    )

    sink.sink.notify(_notification())

    assert len(sink.documents()) == 1, (
        "the destination holds more than one record for one idempotency key: at-least-once "
        "has become at-least-twice-visible, and a human cannot tell a re-send from a new "
        "event"
    )
    assert sink.sink.has_delivered(NOTIFICATION_ID) is True


def test_a_delivery_the_destination_refused_is_not_recorded_as_sent(sink: Harness) -> None:
    """03 §A rule 1, made observable. The failure must be a raise, never a receipt.

    This is the assertion that keeps a transport error from being turned into an "empty
    success": swallow it and the ledger shows a sent row, the retry ladder has nothing to
    retry, and the person who was never told is the only witness. plan §18 lists catching a
    broad exception and returning a benign-looking result as fatal, and the notification
    version is that same defect one layer out.
    """
    key = "NOTIF-DESTINATION-REFUSED"
    sink.refuse_next(key)

    with pytest.raises(sink.refusal):
        sink.sink.notify(_notification(notification_id=key))

    assert sink.sink.has_delivered(key) is False, (
        "a failed delivery was recorded as delivered; the outbox will never retry it and "
        "the notification is gone without a trace"
    )
    assert sink.documents() == []


def test_the_document_at_the_destination_is_the_payload_written_by_hand_here(
    payload_sink: Harness,
) -> None:
    """What a consumer actually receives, field for field, against a hand-written literal.

    Not a round trip through the code under test: EXPECTED_PAYLOAD was assembled from the
    port's wire contract and the fixture constants, so a dropped field, a renamed one or a
    silently reformatted timestamp fails here rather than in a packet twelve months later.
    This assertion is what "the disclaimer and the run id travel with the payload" means in
    practice -- and the refusal upstream is a hard one, not a warning, when they do not.
    """
    payload_sink.sink.notify(_notification())

    documents = payload_sink.documents()
    assert documents == [EXPECTED_PAYLOAD], (
        "the delivered document is not the self-describing payload: a money figure that "
        "reaches a consumer without its assumptions, its model version or the run it came "
        "from is a number with no honest meaning (plan §13)"
    )

    delivered = documents[0]["expected_value_minor"]
    assert isinstance(delivered, int) and not isinstance(delivered, float), (
        f"{MONEY_MINOR} minor units arrived as a float: 01 B money is integer minor units, "
        "and a float drifts instead of crashing, which is why scripts/no_float_money is a "
        "build gate rather than a style preference"
    )
    delivered_at = documents[0]["occurred_at"]
    assert delivered_at == OCCURRED_AT_Z, (
        "a wire timestamp that is not UTC-with-a-Z leaves the consumer to guess the zone, "
        "and iso_z is the one function that says so"
    )


def test_the_slack_message_carries_the_advisory_framing_a_human_needs(
    slack_sink: Harness,
) -> None:
    """Slack is read on a phone, without the case open, so the framing must be in the text.

    The sink posts rendered Block Kit rather than the payload dict, and its URL is itself the
    credential, so this asserts what a person sees: the short-form advisory line, the run it
    came from, the money figure in minor units with its assumptions beside it, and the
    four-eyes truth. 02 §F's warning about a notification that says "escalated" about a
    decision still awaiting second review lives or dies here.
    """
    slack_sink.sink.notify(_notification())

    documents = slack_sink.documents()
    assert len(documents) == 1
    # ``ensure_ascii=False`` because the advisory line and the severity glyph are non-ASCII:
    # what is being asserted is the string a human reads, not its escaped transport form.
    text = json.dumps(documents, ensure_ascii=False)

    assert ADVISORY_LINE in text, (
        "a Slack message with no advisory line is a confident one-line instruction, read "
        "against model estimates that are not advice"
    )
    assert RUN_ID in text
    assert "▲ WARNING" in text, "severity must be a word and a glyph, never colour alone"
    assert "Money figure: 2482331 USD minor units" in text, (
        "the money block must quote integer minor units; rendering 24823.31 would be a "
        "different number from the one the warehouse holds (01 B)"
    )
    assert "24823.31" not in text
    assert "recovery_rate=0.85" in text, (
        "the assumptions have to be legible next to the figure, not only inside a payload "
        "nobody opened"
    )
    assert "Awaiting second-reviewer confirmation (four-eyes)" in text
