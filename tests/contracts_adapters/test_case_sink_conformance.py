"""Port conformance for the case-sink port, asserted against every adapter.

plan §13 lists ``case_sink.py CaseSink`` among the seven ports that must each carry a
Protocol, a ``Null*`` implementation **and a contract test both must pass**, and it
labels that section "the part judges interrogate". P7's gate is
``pytest -q tests/contracts_adapters`` under the name "port conformance across every
adapter"; until this file existed the directory held the watchlist suite alone, so the
gate said nothing about the port the submission's money figures travel on.

This is a *conformance* suite, not a unit test of one adapter: the same assertions run
against every ``CaseSink`` in the tree, and what is asserted is what the port's own
docstrings claim. Adding a class is one line in ``ADAPTERS`` - that is the intended way
this file grows.

The §13 sentence these tests exist to make mechanical:

    "A consumer cannot receive an OXBOW number without also receiving what it
    depends on."

What each group below protects against:

* **the payload is the promise.** The assumptions block has to arrive *with* the money
  figure, the model version *with* the score, and the disclaimer *with* the document.
  Asserted against the bytes each destination actually holds, not against the dict the
  caller built, because a field dropped between the two is invisible afterwards: the
  consumer is left holding ``40000000`` with no recovery rate, which is the fabricated
  number plan §18 calls a lie (plan §13, 02 §E);
* **the refusal is the port.** A bundle that cannot satisfy the above must raise
  ``SelfDescribingPayloadError`` *and deliver nothing*. Accepting it is the one
  unforgivable act available here, and a sink that raises while still writing the file
  has accepted it;
* **calibration reports a population or it reports nothing.** ``n`` is what stops "high
  confidence" being an adjective (spec §12.4, case rail). It has no default, so a rate
  cannot be constructed without one, and the ``n`` that was measured is the ``n`` the
  consumer must read back - neither dropped nor inflated. Where the refusal of a
  zero-population rate actually lives is asserted too, and it is not at this boundary;
* **idempotency is at-least-once with a deduping consumer** (02 §E). Two deliveries of
  one decision must be one artifact at the destination, and the key must be the
  delimited digest the port documents: an undelimited concatenation collides
  (``ab``/``c`` against ``a``/``bc``) and a colliding key silently drops a case;
* **a receipt that names no sink cannot be audited.** ``sink_id`` is what the outbox
  ledger uses to record which destination delivered a case.

Nothing here needs a service up: the four implementations land in a temporary directory,
in an ``httpx.MockTransport`` at the far end of the real signing path, and in the null
object store. There is therefore no ``pytest.skip`` in this file - plan §13's "null
adapters run with nothing else up" is what makes that true - and
``test_the_registry_still_covers_every_implementation`` stops an emptied registry from
turning the gate green by exercising nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Final

import httpx
import pytest

from oxbow.adapters.file.sinks import FileCaseSink
from oxbow.adapters.goaml.sinks import GoamlCaseSink
from oxbow.adapters.io import dumps
from oxbow.adapters.null.objectstore import NullObjectStore
from oxbow.adapters.null.sinks import NullCaseSink
from oxbow.adapters.signing import (
    IDEMPOTENCY_HEADER,
    SCHEMA_VERSION_HEADER,
    SIGNATURE_HEADER,
    SignatureMismatchError,
    verify_signature,
)
from oxbow.adapters.webhook.sinks import WebhookCaseSink
from oxbow.ports.case_sink import (
    CASE_SCHEMA_VERSION,
    OXBOW_DISCLAIMER,
    CalibrationReading,
    CaseBundle,
    CaseSink,
    DecisionRecord,
    EconomicsBlock,
    MonteCarloInterval,
    ScoreBlock,
    SelfDescribingPayloadError,
    SinkReceipt,
    assert_self_describing,
    build_idempotency_key,
)

# --- the fixture case, with every figure written out by hand -------------------
#
# Money is integer minor units end-to-end (01 §B), so nothing below is a float and the
# arithmetic in the comments was done with a calculator, not produced by the code under
# test.
CURRENCY: Final = "UGX"
EXPOSURE_MINOR: Final = 40_000_000  # 400,000.00 UGX at two decimals
ANALYST_COST_MINOR: Final = 1_800_000  # band E: 120 min x 15,000 minor per minute
FRICTION_COST_MINOR: Final = 2_500_000  # 25,000.00 UGX
RECOVERY_RATE: Final = 0.35  # a ratio, not money
CALIBRATED_PROBABILITY: Final = 0.80
OBSERVED_RATE: Final = 0.412
CALIBRATION_N: Final = 1_204
# EV = p*E*r - c - (1-p)*f
#    = 0.80 x 40,000,000 x 0.35 - 1,800,000 - 0.20 x 2,500,000
#    = 11,200,000 - 1,800,000 - 500,000
#    = 8,900,000 minor = 89,000.00 UGX
EXPECTED_VALUE_MINOR: Final = 8_900_000

RUN_ID: Final = "01HX7N9F4RQ5C2Z8VW3JB6TY0M"
CASE_ID: Final = "CASE-7C2A9100B3D4"
ACCOUNT_KEY: Final = "7c2a9100b3d4"
DECISION_SEQ: Final = 3
LATER_DECISION_SEQ: Final = 4
ACTOR: Final = "analyst-3@oxbow.dev"
# Plain ASCII with no markup: the GOAML channel escapes text into XML, and every family
# assertion below compares against the bytes a consumer holds.
REASON: Final = "Four transfers out of the subject within forty minutes, under the floor"
# 09:34:56 in a +03:00 zone is 06:34:56Z, so a wire timestamp that lost the zone - or
# lost the conversion - cannot pass for the right answer.
DECIDED_AT: Final = datetime(2024, 6, 1, 9, 34, 56, tzinfo=timezone(timedelta(hours=3)))
DECIDED_AT_WIRE: Final = "2024-06-01T06:34:56Z"

BAND: Final = "E"
SCORECARD_POINTS: Final = 62
MODEL_VERSION: Final = "lgbm-4.5.0+woe-fold3-2024-06-01"
CALIBRATION: Final = CalibrationReading(band=BAND, observed_rate=OBSERVED_RATE, n=CALIBRATION_N)
REASON_CODES: Final = (
    "Pass-through ratio in the top decile",
    "Counterparty overlap with a flagged account in the same hour",
)
RULE_IDS: Final = ("R4_CYCLE_MEMBER", "R2_FAN_OUT")

ASSUMPTIONS: Final = {
    "recovery_rate": RECOVERY_RATE,
    "cost_basis": "config/economics.yaml",
    "horizon_days": 30,
}
PROVENANCE: Final = {
    "config_hash": "sha256:9b41c7",
    "dataset_ref": "data/interim/paysim/run=" + RUN_ID,
    "walk_forward_fold": 3,
}
MONTE_CARLO: Final = MonteCarloInterval(
    runs=10_000,
    seed=1_337,
    p05_minor=30_000_000,
    p50_minor=EXPOSURE_MINOR,
    p95_minor=55_000_000,
    interval=(0.05, 0.95),
)
EVIDENCE_REFS: Final = ("evidence:TXN-9001", "evidence:TXN-9002")
TRANSACTION_IDS: Final = ("TXN-9001", "TXN-9002")

#: ``sha256("01HX7N9F4RQ5C2Z8VW3JB6TY0M|CASE-7C2A9100B3D4|3")``, computed offline with a
#: one-line hashlib call and pasted here, so a key that stops covering one of its three
#: inputs cannot match it.
EXPECTED_KEY: Final = "93a82bc436dd73622468f68d919aeabf27372f5a91f1a2f5fd5daeb1b1bc4c23"
#: ``sha256("01HX7N9F4RQ5C2Z8VW3JB6TY0M|CASE-7C2A9100B3D4|4")`` - the same case's next
#: decision, which has to be a different delivery.
EXPECTED_KEY_LATER: Final = "24b4a98f41ad94337643bc2d2ea7405925c64aed584e03fcda5cca54ab83142f"
#: ``sha256("a|bc|7")``, from the collision the port's separator exists to remove.
COLLISION_RIGHT: Final = "46bebab7f2addc1772cd6dd1728b2d9e20f349baa09b2b1bc592b83d14569a04"

WEBHOOK_URL: Final = "https://consumer.test/cases"
# A test-only secret, explicit and synthetic; the repository's own signing secret is
# never read by this file.
WEBHOOK_SECRET: Final = "case-sink-conformance-test-secret"
GOAML_PREFIX: Final = "goaml/cases"


def _bundle(
    *,
    run_id: str = RUN_ID,
    case_id: str = CASE_ID,
    account_key: str = ACCOUNT_KEY,
    decided_at: datetime = DECIDED_AT,
    decision_seq: int = DECISION_SEQ,
    assumptions: Mapping[str, Any] = ASSUMPTIONS,
    model_version: str = MODEL_VERSION,
    calibration: CalibrationReading = CALIBRATION,
) -> CaseBundle:
    """One decided case, exactly as the decision transaction would have built it."""
    return CaseBundle(
        run_id=run_id,
        case_id=case_id,
        account_key=account_key,
        decided_at=decided_at,
        decision=DecisionRecord(
            decision_seq=decision_seq,
            action="escalate",
            reason=REASON,
            actor_id=ACTOR,
            decided_at=decided_at,
            four_eyes_confirmed_by=None,
        ),
        score=ScoreBlock(
            fused_score=CALIBRATED_PROBABILITY,
            band=BAND,
            scorecard_points=SCORECARD_POINTS,
            reason_codes=REASON_CODES,
            calibration=calibration,
            model_version=model_version,
            rule_ids=RULE_IDS,
        ),
        economics=EconomicsBlock(
            currency=CURRENCY,
            exposure_minor=EXPOSURE_MINOR,
            expected_value_minor=EXPECTED_VALUE_MINOR,
            recovery_rate=RECOVERY_RATE,
            analyst_cost_minor=ANALYST_COST_MINOR,
            friction_cost_minor=FRICTION_COST_MINOR,
            assumptions=assumptions,
            monte_carlo=MONTE_CARLO,
        ),
        evidence_refs=EVIDENCE_REFS,
        transaction_ids=TRANSACTION_IDS,
        provenance=PROVENANCE,
    )


# --- the observation rigs ------------------------------------------------------


class Delivery:
    """A sink plus an independent view of what its consumer actually received.

    The view is deliberately not the sink's own bookkeeping: the disk rigs re-read the
    directory, the webhook rig sees the request the transport received, the GOAML rig
    reads the object back out of the store. "The emit call returned" and "the
    destination holds a self-describing document" are different claims, and only the
    second is worth asserting (02 §E: the outbox owns delivery state, the sink does not).
    """

    #: What one delivery looks like at the destination: JSON text, or XML for GOAML. The
    #: family tests hold each channel to what its own format can carry, which is the
    #: honest form of one assertion across four adapters.
    document_kind: ClassVar[str] = "json"

    def __init__(self, sink: CaseSink) -> None:
        self.sink = sink

    def emit(self, bundle: CaseBundle) -> SinkReceipt:
        return self.sink.emit(bundle)

    def landed(self) -> dict[str, str]:
        """Every artifact at the destination, keyed by the idempotency key."""
        raise NotImplementedError


class _DiskDelivery(Delivery):
    """``file`` and ``null``: the artifacts are the JSON documents on disk."""

    def __init__(self, sink: CaseSink, directory: Path) -> None:
        super().__init__(sink)
        self._directory = directory

    def landed(self) -> dict[str, str]:
        return {
            path.name[: -len(".json")]: path.read_text(encoding="utf-8")
            for path in self._directory.glob("*.json")
            if path.name != "index.json"
        }


class _HttpDelivery(Delivery):
    """``webhook``: the artifacts are the signed requests the far end actually saw."""

    def __init__(self, sink: CaseSink, requests: list[httpx.Request]) -> None:
        super().__init__(sink)
        self.requests = requests

    def landed(self) -> dict[str, str]:
        # Keyed by the header the consumer dedupes on, not by a field inside the body:
        # a delivery the receiver cannot key is a delivery it will duplicate.
        return {
            str(request.headers[IDEMPOTENCY_HEADER]): request.content.decode("utf-8")
            for request in self.requests
        }


class _StoreDelivery(Delivery):
    """``goaml``: the artifacts are stored XML drafts, read back through the store."""

    document_kind: ClassVar[str] = "xml"

    def __init__(self, sink: CaseSink, store: NullObjectStore) -> None:
        super().__init__(sink)
        self._store = store

    def landed(self) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        for ref in self._store.list(GOAML_PREFIX):
            key = ref.key.rsplit("/", 1)[-1][: -len(".xml")]
            artifacts[key] = self._store.get(ref.key).decode("utf-8")
        return artifacts


SinkFactory = Callable[[Path], Delivery]


def _file(scratch: Path) -> Delivery:
    sink = FileCaseSink(scratch / "case_sink")
    return _DiskDelivery(sink, sink.base_dir)


def _null(scratch: Path) -> Delivery:
    sink = NullCaseSink(scratch)
    return _DiskDelivery(sink, sink.base_dir)


def _webhook(scratch: Path) -> _HttpDelivery:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"received": True})

    # The production sender, with the far end substituted at the transport: the same
    # signing code and status classification, no socket (02 §E).
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = WebhookCaseSink(url=WEBHOOK_URL, secret=WEBHOOK_SECRET, client=client)
    return _HttpDelivery(sink, requests)


def _goaml(scratch: Path) -> Delivery:
    store = NullObjectStore(scratch)
    return _StoreDelivery(GoamlCaseSink(store, prefix=GOAML_PREFIX), store)


# Every CaseSink in the tree. A new implementation is added here.
ADAPTERS: dict[str, SinkFactory] = {
    "file": _file,
    "goaml": _goaml,
    "null": _null,
    "webhook": _webhook,
}

#: The registered names, as a literal: the set of implementations exercised is itself
#: under test rather than an accident of discovery.
IMPLEMENTATIONS: Final = ("file", "goaml", "null", "webhook")

#: The ``sink_id`` each destination puts in the outbox ledger, also a literal.
SINK_IDS: Final = ("file", "goaml", "null", "webhook")


def _ids() -> list[str]:
    return list(ADAPTERS)


@pytest.fixture(params=sorted(ADAPTERS))
def delivery(request: pytest.FixtureRequest, tmp_path: Path) -> Delivery:
    return ADAPTERS[request.param](tmp_path / request.param)


# --- the port's structural promises -------------------------------------------


@pytest.mark.parametrize("name", _ids())
def test_satisfies_the_runtime_checkable_protocol(name: str, tmp_path: Path) -> None:
    """A duck that does not satisfy the Protocol is not an implementation of the port."""
    rig = ADAPTERS[name](tmp_path / name)
    assert isinstance(rig.sink, CaseSink), (
        f"{name} does not satisfy CaseSink; a case wired through a protocol the adapter "
        "does not implement fails at the drain, after the decision was already signed"
    )


@pytest.mark.parametrize("name", _ids())
def test_receipt_names_the_sink_that_produced_it(name: str, tmp_path: Path) -> None:
    """Attribution, because the outbox ledger answers "who delivered this case?".

    A receipt without a destination cannot be reconciled against the ``sink_id`` on its
    own outbox row, and that row is the only record of what left the building.
    """
    rig = ADAPTERS[name](tmp_path / name)
    assert rig.sink.sink_id, "an unnamed destination cannot appear in an audit trail"

    receipt = rig.emit(_bundle())

    assert isinstance(receipt, SinkReceipt), (
        "the port promises a receipt, not a bool: a bare True cannot say which key the "
        "consumer took the case under"
    )
    assert receipt.accepted is True
    assert receipt.idempotency_key == EXPECTED_KEY, (
        "the receipt does not repeat the key the delivery was promised under, so the "
        "outbox has no way to match a sent row back to its decision"
    )
    assert rig.sink.sink_id in receipt.consumer, (
        f"the receipt names {receipt.consumer!r}, which does not contain sink_id "
        f"{rig.sink.sink_id!r}: a delivery nobody can attribute is a delivery nobody can "
        "replay or dispute"
    )
    assert isinstance(receipt.accepted_at, datetime)
    assert receipt.accepted_at.tzinfo is not None, "an accepted_at without a tz is ambiguous"


# --- self-describing: the payload IS the contract ------------------------------


@pytest.mark.parametrize("name", _ids())
def test_the_consumer_receives_the_money_and_what_it_depends_on(name: str, tmp_path: Path) -> None:
    """plan §13, asserted against the bytes at the destination rather than the dict.

    Every figure below was written by hand into the fixture: the exposure, the expected
    value, the recovery rate that turns one into the other, the model version behind the
    score, the population behind the rate. If any of them can be dropped between
    ``to_payload`` and the wire, the consumer holds an OXBOW number with no stated basis
    - and nothing after the fact can tell that apart from an honest figure.
    """
    rig = ADAPTERS[name](tmp_path / name)
    bundle = _bundle()
    assert bundle.to_payload()["idempotency_key"] == EXPECTED_KEY

    rig.emit(bundle)

    artifacts = rig.landed()
    assert list(artifacts) == [EXPECTED_KEY], f"{name} landed {len(artifacts)} artifacts"
    text = artifacts[EXPECTED_KEY]

    assert str(EXPOSURE_MINOR) in text, "the exposure figure itself did not arrive"
    assert str(EXPECTED_VALUE_MINOR) in text, "the expected value did not arrive"
    assert f"{EXPOSURE_MINOR}.0" not in text, (
        f"{name} rendered a minor-unit amount with a decimal point: money is integer "
        "minor units end-to-end (01 §B), and a float total is the reconciliation failure "
        "that stays invisible until someone quotes it in a packet (03 §A rule 2)"
    )
    # Three pairs that must not be separable on the wire: money and its assumptions,
    # score and its model, rate and its population.
    assert str(RECOVERY_RATE) in text, (
        "an expected value arrived without its recovery rate, which is the exact shape "
        "of a fabricated number (plan §13)"
    )
    assert MODEL_VERSION in text, (
        "a score arrived without the model version that produced it, so no consumer can "
        "tell which artifact the number came out of (02 §B seam 5)"
    )
    assert str(OBSERVED_RATE) in text
    assert str(CALIBRATION_N) in text, "a rate arrived without the population behind it"
    assert ACCOUNT_KEY in text, "the subject identifier must arrive with its case"
    assert REASON in text, "the reviewer's reason is part of what the decision depends on"
    assert (
        "research prototype" in text
    ), f"{name} delivered a case with no statement that OXBOW is a research prototype"

    if rig.document_kind == "json":
        body = json.loads(text)
        assert body["disclaimer"] == OXBOW_DISCLAIMER, (
            "the JSON channels carry the disclaimer verbatim; a paraphrase is a second, "
            "different disclaimer, and plan §15 allows one"
        )
        assert body["advisory_only"] is True, (
            "advisory_only is the consumer's cue that nothing here is a decision; a "
            "payload that drops it reads as an instruction"
        )
    else:
        assert "not a filed report" in text and "model estimates" in text, (
            "the GOAML draft has to say it is a draft and that the money is an estimate; "
            "a regulator-facing document that omits both is the automated decision 02 §F "
            "reserves to a human"
        )


def test_no_caller_can_build_a_payload_that_omits_the_disclaimer() -> None:
    """The disclaimer is added by ``to_payload``, not passed in, so it has no input.

    A field a caller may leave empty is a field that will be left empty; this asserts
    the omission is not representable at all.
    """
    names = {field.name for field in fields(CaseBundle)}
    assert "disclaimer" not in names, (
        "CaseBundle grew a disclaimer field, so a builder can now construct a payload "
        "without one and the port's central claim is a convention again"
    )
    payload = _bundle().to_payload()
    assert payload["disclaimer"] == OXBOW_DISCLAIMER
    assert payload["advisory_only"] is True
    assert payload["schema_version"] == CASE_SCHEMA_VERSION
    assert payload["model_version"] == MODEL_VERSION == payload["score"]["model_version"]
    assert payload["economics"]["assumptions"] == dict(ASSUMPTIONS)


def test_a_payload_missing_any_pillar_of_self_description_is_refused() -> None:
    """The refusal is the whole point of the port, so each pillar is checked alone.

    One missing field and four missing fields must not raise the same opaque error: the
    operator reading it has to know which promise the payload broke.
    """
    full = _bundle().to_payload()
    assert_self_describing(full)

    for key in ("disclaimer", "advisory_only", "schema_version", "run_id"):
        stripped = {item: value for item, value in full.items() if item != key}
        with pytest.raises(SelfDescribingPayloadError) as excinfo:
            assert_self_describing(stripped)
        assert key in str(excinfo.value), (
            f"a payload with no {key} was refused without naming it; an error that does "
            "not say which promise broke turns one bug into an afternoon of grep"
        )

    # Money with no assumptions, and money with no model version, are the two shapes
    # that still read like real figures - which is why the check hunts exactly those.
    no_assumptions = json.loads(json.dumps(full))
    no_assumptions["economics"]["assumptions"] = {}
    with pytest.raises(SelfDescribingPayloadError, match="assumptions"):
        assert_self_describing(no_assumptions)

    no_version = json.loads(json.dumps(full))
    no_version.pop("model_version")
    no_version["score"]["model_version"] = ""
    with pytest.raises(SelfDescribingPayloadError, match="model_version"):
        assert_self_describing(no_version)

    assert issubclass(SelfDescribingPayloadError, ValueError), (
        "the drain treats a payload defect as a 4xx-shaped problem it must not retry; a "
        "refusal raised as something the handler does not catch turns one bad case into "
        "five attempts and a dead letter"
    )


@pytest.mark.parametrize("name", _ids())
def test_every_sink_refuses_a_case_that_cannot_explain_its_own_numbers(
    name: str, tmp_path: Path
) -> None:
    """The same rule at every boundary that has the bytes (plan §13).

    Asserted per adapter and twice over: it raises, *and* nothing reaches the
    destination. A sink that writes the artifact and then complains has still delivered
    the unsupported number.
    """
    rig = ADAPTERS[name](tmp_path / name)
    broken = (
        ("no assumptions behind the money", _bundle(assumptions={}), "assumptions"),
        ("no model version behind the score", _bundle(model_version=""), "model_version"),
    )

    for label, bundle, expected in broken:
        assert bundle.to_payload()["idempotency_key"] == EXPECTED_KEY
        with pytest.raises(SelfDescribingPayloadError) as excinfo:
            rig.emit(bundle)
        assert expected in str(excinfo.value), (
            f"{name} refused the case with {label} for some other reason; the message is "
            "the only thing a worker log has to go on"
        )
        assert rig.landed() == {}, (
            f"{name} raised on a case with {label} and delivered it anyway: the refusal "
            "is worth nothing if the unsupported number still reaches the consumer"
        )


# --- calibration honesty -------------------------------------------------------


def test_a_rate_cannot_be_constructed_without_a_population() -> None:
    """``n`` has no default and no ``None`` branch, so "no population" is unrepresentable.

    This is where the port refuses a bare confidence claim: at construction rather than
    at emit, which is the stricter place - there is no field left to forget to fill in.
    """
    with pytest.raises(TypeError):
        CalibrationReading(band=BAND, observed_rate=OBSERVED_RATE)  # type: ignore[call-arg]


@pytest.mark.parametrize("name", _ids())
def test_the_population_arrives_intact_rather_than_rounded(name: str, tmp_path: Path) -> None:
    """1,204 measured members is a different claim from "high confidence".

    The count has to survive the trip exactly. A sink that clamps, defaults or quietly
    converts a measurement into an adjective cannot be caught anywhere else, because the
    rate next to it still looks plausible.
    """
    rig = ADAPTERS[name](tmp_path / name)
    rig.emit(_bundle())

    text = rig.landed()[EXPECTED_KEY]
    assert (
        str(OBSERVED_RATE) in text and str(CALIBRATION_N) in text
    ), f"{name} delivered a rate whose population is not in the same document"

    if rig.document_kind == "json":
        calibration = json.loads(text)["score"]["calibration"]
        assert calibration == {"band": BAND, "observed_rate": OBSERVED_RATE, "n": CALIBRATION_N}, (
            f"{name} rewrote the calibration block: {calibration!r} is not the reading "
            "that was measured"
        )
        assert isinstance(calibration["n"], int), "a population came back as a float"
    else:
        assert f"n={CALIBRATION_N}" in text, "the GOAML narrative dropped the population"


def test_a_refused_calibration_arrives_as_a_zero() -> None:
    """Where the zero-population refusal actually lives, pinned so a move is noticed.

    ``adapters/warehouse/landing.py`` refuses such rows before they can be stored, and
    :func:`assert_self_describing` does not look at ``n`` at all - so all this boundary
    does with a refused calibration is deliver the zero honestly. It must not invent a
    population to make the payload look acceptable. If the port ever grows a refusal of
    its own, this test fails and the choice gets made deliberately.
    """
    bundle = _bundle(calibration=CalibrationReading(band=BAND, observed_rate=OBSERVED_RATE, n=0))
    payload = bundle.to_payload()

    assert payload["score"]["calibration"] == {
        "band": BAND,
        "observed_rate": OBSERVED_RATE,
        "n": 0,
    }, "the zero population was rewritten on the way out"
    assert_self_describing(payload)


# --- idempotency (02 §E) -------------------------------------------------------


def test_the_key_is_the_delimited_digest_of_run_case_and_sequence() -> None:
    """Three inputs, one digest, and a separator between them.

    The literals were computed offline, so a key that stops covering one of its inputs -
    a dropped separator, a dropped ``decision_seq`` - stops matching them. An
    idempotency key that collides does not fail loudly: it drops a delivery silently.
    """
    bundle = _bundle()
    assert bundle.idempotency_key == EXPECTED_KEY
    assert _bundle().idempotency_key == EXPECTED_KEY, (
        "the same decision rebuilt from the same fields yields a different key, so the "
        "outbox row and the sink no longer agree about which deliveries are one delivery"
    )
    assert _bundle(decision_seq=LATER_DECISION_SEQ).idempotency_key == EXPECTED_KEY_LATER
    assert _bundle(case_id="CASE-9F8E7D6C5B4A").idempotency_key != EXPECTED_KEY

    # The collision the port's docstring names: run ``ab``/case ``c`` against run
    # ``a``/case ``bc``, which an undelimited concatenation would hash identically.
    assert build_idempotency_key("a", "bc", 7) == COLLISION_RIGHT
    assert build_idempotency_key("a", "bc", 7) != build_idempotency_key("ab", "c", 7), (
        "two different decisions share one idempotency key, and the second is dropped as "
        "a duplicate without anyone being told"
    )


@pytest.mark.parametrize("name", _ids())
def test_a_repeated_delivery_reaches_the_consumer_once(name: str, tmp_path: Path) -> None:
    """At-least-once delivery with a deduping consumer, which is the documented deal.

    ``has_delivered`` answers before and after, the second emit is still accepted under
    the same key, and the destination still holds exactly one artifact whose bytes did
    not change. Double-notifying a reviewer is not a retry; on the GOAML channel it is a
    second report.
    """
    rig = ADAPTERS[name](tmp_path / name)
    bundle = _bundle()

    assert rig.sink.has_delivered(bundle.idempotency_key) is False, (
        f"{name} reports a delivery it has not made, so the outbox skips a case nobody "
        "was ever sent"
    )
    first = rig.emit(bundle)
    after_first = rig.landed()
    assert rig.sink.has_delivered(bundle.idempotency_key) is True, (
        "the sink cannot answer 'already sent' for a key it accepted, which leaves the "
        "defence against a duplicate notification to nobody in particular"
    )

    second = rig.emit(bundle)
    after_second = rig.landed()

    assert second.accepted is True
    assert second.idempotency_key == first.idempotency_key == EXPECTED_KEY
    assert list(after_second) == [EXPECTED_KEY], (
        f"{len(after_second)} artifacts for one decision: the port promises a consumer "
        "that dedupes by key, and one artifact per attempt is a duplicate report"
    )
    assert after_second == after_first, (
        f"{name} changed what the consumer holds on the second emit; a redelivery that "
        "mutates the bytes is a different case wearing the same key"
    )


@pytest.mark.parametrize("name", _ids())
def test_a_later_decision_on_the_same_case_is_a_separate_delivery(
    name: str, tmp_path: Path
) -> None:
    """A reversal is a new row, never an edit (plan §15), so it needs its own delivery.

    If the key stopped covering ``decision_seq`` the second decision would dedupe against
    the first and the reversal would never leave the building.
    """
    rig = ADAPTERS[name](tmp_path / name)

    assert rig.emit(_bundle()).idempotency_key == EXPECTED_KEY
    assert rig.emit(_bundle(decision_seq=LATER_DECISION_SEQ)).idempotency_key == (
        EXPECTED_KEY_LATER
    )
    assert sorted(rig.landed()) == sorted(
        {EXPECTED_KEY, EXPECTED_KEY_LATER}
    ), f"{name} collapsed two decisions of one case into a single artifact"
    assert rig.sink.has_delivered(EXPECTED_KEY) is True
    assert (
        rig.sink.has_delivered("0" * 64) is False
    ), "an unknown key reports as delivered, which is how a case disappears quietly"


# --- the payload as data -------------------------------------------------------


def test_payload_is_plain_json_and_round_trips_every_value_it_was_given() -> None:
    """Serialises with no ``default=`` hook, and comes back identical.

    A payload that needs a custom encoder is one a consumer in another language cannot
    read, and a round trip that changes a value is a number that means two things
    depending on which side of the wire you happen to be standing on.
    """
    payload = _bundle().to_payload()
    restored = json.loads(json.dumps(payload))

    assert restored == payload, (
        "the payload is not stable under JSON: something in it (a tuple, a datetime, a "
        "set) means one thing in Python and another on the wire"
    )

    economics = restored["economics"]
    assert economics["currency"] == CURRENCY
    assert economics["exposure_minor"] == EXPOSURE_MINOR
    assert economics["expected_value_minor"] == EXPECTED_VALUE_MINOR
    assert economics["analyst_cost_minor"] == ANALYST_COST_MINOR
    assert economics["friction_cost_minor"] == FRICTION_COST_MINOR
    for field in ("exposure_minor", "expected_value_minor", "analyst_cost_minor"):
        value = economics[field]
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"{field} came back as a {type(value).__name__}; minor units are integers and "
            "the first float is the first silent rounding error (01 §B)"
        )
    assert economics["recovery_rate"] == RECOVERY_RATE
    assert economics["assumptions"] == dict(ASSUMPTIONS)

    interval = economics["monte_carlo"]
    assert (interval["runs"], interval["seed"]) == (10_000, 1_337), (
        "the seed and draw count are payload fields, not comments: without them a quoted "
        "interval can be neither reproduced nor challenged (plan §12)"
    )
    assert (interval["p05_minor"], interval["p50_minor"], interval["p95_minor"]) == (
        30_000_000,
        EXPOSURE_MINOR,
        55_000_000,
    )
    for field in ("p05_minor", "p50_minor", "p95_minor"):
        assert isinstance(interval[field], int), f"{field} left the integer domain"
    assert interval["interval"] == [0.05, 0.95]

    score = restored["score"]
    assert score["fused_score"] == CALIBRATED_PROBABILITY, "a probability stopped being one"
    assert 0.0 <= score["fused_score"] <= 1.0
    assert score["band"] == BAND
    assert score["scorecard_points"] == SCORECARD_POINTS
    assert score["reason_codes"] == list(REASON_CODES)
    assert score["rule_ids"] == list(RULE_IDS)
    assert score["model_version"] == MODEL_VERSION
    assert score["calibration"] == {
        "band": BAND,
        "observed_rate": OBSERVED_RATE,
        "n": CALIBRATION_N,
    }

    assert restored["decided_at"] == DECIDED_AT_WIRE, (
        "a wire timestamp that lost its zone makes the consumer guess, and an "
        "hour-hunting bug is what the guess produces"
    )
    assert restored["decision"]["decided_at"] == DECIDED_AT_WIRE
    parsed = datetime.fromisoformat(DECIDED_AT_WIRE.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == DECIDED_AT, "the instant moved on the way to the wire"
    assert restored["account_key"] == ACCOUNT_KEY
    assert restored["evidence_refs"] == list(EVIDENCE_REFS)
    assert restored["transaction_ids"] == list(TRANSACTION_IDS)
    assert restored["provenance"] == dict(PROVENANCE)
    assert restored["idempotency_key"] == EXPECTED_KEY


def test_a_naive_instant_is_refused_before_the_payload_can_leave() -> None:
    """``iso_z`` is the only spelling of a wire timestamp, and it has no naive path.

    A naive instant in a case payload is an hour of ambiguity bolted to a money figure.
    """
    with pytest.raises(ValueError, match="naive"):
        _bundle(decided_at=datetime(2024, 6, 1, 9, 34, 56)).to_payload()


# --- the webhook channel, whose promise is half in a header --------------------


def test_the_webhook_signs_and_sends_the_bytes_the_consumer_dedupes_on(
    tmp_path: Path,
) -> None:
    """The signature is over the raw body actually posted, and the key is in both places.

    A client that serialises, mutates and re-serialises produces a body whose signature
    verifies against nothing the receiver got - and an ``Idempotency-Key`` header that
    disagrees with the payload's own key hands the consumer two answers to "have I seen
    this before?".
    """
    rig = _webhook(tmp_path / "webhook")
    bundle = _bundle()
    receipt = rig.emit(bundle)

    assert len(rig.requests) == 1
    request = rig.requests[0]
    assert str(request.url) == WEBHOOK_URL
    assert request.headers[IDEMPOTENCY_HEADER] == receipt.idempotency_key == EXPECTED_KEY
    assert (
        request.headers[SCHEMA_VERSION_HEADER] == CASE_SCHEMA_VERSION
    ), "a consumer that pins 1.x has to know what it was sent before it parses it"

    body = json.loads(request.content.decode("utf-8"))
    assert body == bundle.to_payload(), (
        "what left the process is not what the bundle described: a field rewritten after "
        "signing is a payload the receiver cannot verify and a reviewer cannot trust"
    )
    # The receiver's own half of the scheme, run over the bytes that arrived.
    verify_signature(request.headers[SIGNATURE_HEADER], request.content, WEBHOOK_SECRET)

    tampered = dumps({**body, "exposure_minor": EXPOSURE_MINOR + 1}).encode("utf-8")
    with pytest.raises(SignatureMismatchError, match="digest"):
        verify_signature(request.headers[SIGNATURE_HEADER], tampered, WEBHOOK_SECRET)


# --- the registry floor --------------------------------------------------------


def test_the_registry_still_covers_every_implementation() -> None:
    """The gate is only as wide as this dict, so the width itself is asserted.

    An emptied or silently narrowed registry turns P7's "port conformance across every
    adapter" into a suite that passes by exercising nothing - the failure mode this whole
    directory was created to end.
    """
    assert set(ADAPTERS) == set(IMPLEMENTATIONS), (
        f"the registry holds {sorted(ADAPTERS)} while the suite promises "
        f"{sorted(IMPLEMENTATIONS)}; a dropped implementation is a conformance claim that "
        "no longer covers it"
    )
    assert len(ADAPTERS) >= 4, (
        "fewer than the four sinks in the tree are registered, so the conformance claim "
        "is narrower than the sentence in the plan that asked for it"
    )


def test_every_registered_sink_advertises_its_own_ledger_name(tmp_path: Path) -> None:
    """``sink_id`` is the outbox ledger's only handle on a destination.

    Two sinks calling themselves the same name would leave a reader unable to tell which
    channel a case left by - the question the ledger row exists to answer.
    """
    ids = sorted(ADAPTERS[name](tmp_path / name).sink.sink_id for name in _ids())

    assert ids == sorted(SINK_IDS), (
        f"the registered sinks report {ids}; the outbox records sink_id per row, so a "
        "renamed or duplicated destination silently rewrites the audit trail"
    )
