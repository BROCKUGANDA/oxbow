"""The CaseSink port: what leaves the building when a case is decided (02 §A, §E).

This module is the wire contract for an outbound case, and it carries the rule
plan §13 states as a design constraint rather than decoration:

    "A consumer cannot receive an OXBOW number without also receiving what it
    depends on."

So the assumptions travel *with* the money figure, the model version travels with
the score, the calibration state travels with the probability, and the disclaimer
travels with the payload. That is enforced by
:func:`assert_self_describing` at emit time, not by a reviewer remembering to
fill in a field — a case that reaches a consumer without its recovery rate is a
number with no honest meaning, and the failure is invisible after the fact.

Everything here is pseudonymous. ``account_key`` is the only subject identifier
that exists downstream of ingest (03 §D), and this payload crosses an external
boundary, so the rule is absolute rather than a default.

``notify.py`` and ``report.py`` reuse :func:`assert_self_describing` and
:class:`SinkReceipt` from here: three sinks, one definition of what "self
describing" means, so the rule cannot be satisfied in one channel and broken in
another.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, runtime_checkable

# Additive-only within a major (02 §E). A consumer that pins 1.x must keep working
# when 1.1 adds a field, which is why nothing below is ever repurposed.
CASE_SCHEMA_VERSION: Final = "1.0"

# Verbatim from plan §15. One constant, asserted in the README, the app footer,
# every export packet and every outbound payload by test_disclaimer_present_everywhere.
OXBOW_DISCLAIMER: Final = (
    "OXBOW is a research prototype that analyzes historical, de-identified data only. It "
    "does not process live financial transactions, does not trade or advise on any financial "
    "instrument, does not make real financial decisions, and is not financial advice. Monetary "
    "figures are model estimates derived from stated assumptions, not measured outcomes. "
    "Results are not validated for operational use by any financial institution."
)

MONEY_FIELDS: Final = ("exposure_minor", "expected_value_minor", "loss_avoided_minor")

_ID_SEPARATOR: Final = "|"

# Every action a decision row may carry. Reversal is a new row, never an edit
# (plan §15: "mutation destroys the integrity claim").
DECISION_ACTIONS: Final = ("escalate", "dismiss", "review", "reverse")

# The two calibration states a payload may state, in the producer's own words: the
# ``kind`` field ``CalibrationResult.confidence_label`` emits in the models layer. They
# are literals here rather than an import, because contract 2 of ``.importlinter``
# forbids ``oxbow.ports`` from depending on ``oxbow.models`` — a port that imports a
# model has stopped being a port. The conformance suite pins the pair against the models
# layer, so the two spellings cannot drift apart in silence.
CALIBRATED_BAND: Final = "calibrated_band"
UNCALIBRATED: Final = "uncalibrated"
CALIBRATION_KINDS: Final = (CALIBRATED_BAND, UNCALIBRATED)


def build_idempotency_key(run_id: str, case_id: str, decision_seq: int) -> str:
    """``sha256(run_id + case_id + decision_seq)``, delimited (02 §E).

    The spec writes this as a concatenation. Concatenating raw is a collision:
    run ``ab``/case ``c`` and run ``a``/case ``bc`` would hash identically, and
    an idempotency key that collides silently drops a delivery. The separator is
    the minimum change that keeps the documented scheme and removes the defect.
    """
    material = f"{run_id}{_ID_SEPARATOR}{case_id}{_ID_SEPARATOR}{decision_seq}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def iso_z(value: datetime) -> str:
    """UTC ISO-8601 with a trailing Z, for every timestamp on the wire."""
    if value.tzinfo is None:
        raise ValueError(
            f"wire timestamps must be tz-aware, got naive {value}; a naive instant means a "
            "consumer has to guess the zone, and an hour-hunting bug is the result"
        )
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CalibrationReading:
    """The band a score sits in, with the observed rate and the population behind it.

    ``n`` is what stops "high confidence" being an adjective: a band with three
    members and a band with three thousand are not the same claim, and the
    consumer has to be able to tell (spec §12.4, case rail).

    ``kind`` says whether that claim was measured at all. A fold that refused
    calibration has a score and no rate, and the consumer must receive the refusal as a
    refusal: emitting ``observed_rate: null`` with no kind would leave an FIU to guess
    whether null means "zero positives" or "we never measured", which is the same
    ambiguity 03 §A rule 2 refuses inside the product. ``CalibrationResult.confidence_label``
    in the models layer emits exactly these two words; they are repeated as literals
    here rather than imported because ``oxbow.ports`` may not depend on ``oxbow.models``
    (import-linter contract 2 — a port that imports a model has stopped being a port).
    """

    kind: str
    band: str | None
    observed_rate: float | None
    n: int | None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in CALIBRATION_KINDS:
            raise ValueError(
                f"calibration kind {self.kind!r} is not one of {list(CALIBRATION_KINDS)}; "
                "the reading would arrive at a consumer labelled with a word that means "
                "nothing on the other side"
            )
        measured = (self.band, self.observed_rate, self.n)
        if self.kind == CALIBRATED_BAND:
            absent = [
                name
                for name, value in zip(("band", "observed_rate", "n"), measured, strict=True)
                if value is None
            ]
            if absent:
                raise ValueError(
                    f"kind='calibrated_band' but {absent} is absent: a consumer cannot "
                    "weigh a rate it did not receive, and cannot weigh an n either"
                )
            if self.n is not None and self.n <= 0:
                raise ValueError(
                    f"n={self.n} is not a population; a calibrated reading states a rate "
                    "measured over something"
                )
            if self.note is not None:
                raise ValueError(
                    "a calibrated reading carries no refusal note — the two kinds would "
                    "arrive together and the consumer has no way to choose between them"
                )
        elif self.note is None:
            raise ValueError(
                "kind='uncalibrated' requires note: the consumer is entitled to know why "
                "no rate was measured, not merely that one is missing"
            )
        else:
            present = [
                name
                for name, value in zip(("band", "observed_rate", "n"), measured, strict=True)
                if value is not None
            ]
            if present:
                raise ValueError(
                    f"an uncalibrated reading may not carry {present}: the fold refused to "
                    "measure them, and a refusal shipped beside a number reads as a number"
                )

    def to_payload(self) -> dict[str, Any]:
        """Wire form: the reading, or the refusal, in full.

        The keys are the same in both shapes on purpose. A consumer that reads
        ``observed_rate`` gets ``null`` and the ``kind`` that explains it on the same
        object, rather than a payload whose shape depends on which branch the model took.
        """
        return {
            "kind": self.kind,
            "band": self.band,
            "observed_rate": self.observed_rate,
            "n": self.n,
            "note": self.note,
        }

    @classmethod
    def from_payload(cls, calibration: Mapping[str, Any]) -> CalibrationReading:
        """Rebuild one reading from a stored payload — outbox row or case packet.

        A payload that carries ``kind`` is reconstructed exactly, and the constructor
        re-checks the pairing so a hand-edited or truncated JSON cannot arrive at a
        renderer as a well-formed object.

        A payload written before revision 0003 carries no ``kind``, and the only shape
        that build could emit was a measured one: an uncalibrated score row could not be
        landed at all, so it could never reach a decision. Reconstructing those as
        ``calibrated_band`` is therefore read off what the writer was capable of
        producing, not guessed from which fields happen to be set. A payload with
        neither a kind nor a measurement is refused — labelling it ``uncalibrated`` would
        assert a refusal the writer never recorded, and it would arrive with no note
        because the note field did not exist yet.
        """
        kind = calibration.get("kind")
        band = calibration.get("band")
        rate = calibration.get("observed_rate")
        size = calibration.get("n")
        note = calibration.get("note")
        if kind is None:
            if rate is None or size is None:
                raise ValueError(
                    "a stored calibration payload carries neither `kind` nor a measurement, "
                    "so it can be labelled neither calibrated nor uncalibrated; refusing to "
                    "invent one for a consumer to sign"
                )
            kind = CALIBRATED_BAND
        return cls(
            kind=str(kind),
            band=None if band is None else str(band),
            observed_rate=None if rate is None else float(str(rate)),
            n=None if size is None else int(str(size)),
            note=None if note is None else str(note),
        )


@dataclass(frozen=True, slots=True)
class MonteCarloInterval:
    """A distributional exposure estimate, with the seed and draw count recorded.

    ``seed`` and ``runs`` are payload fields, not comments: plan §12 requires a run
    to store them so a quoted interval can be reproduced or challenged.
    """

    runs: int
    seed: int
    p05_minor: int
    p50_minor: int
    p95_minor: int
    interval: Sequence[float] = (0.05, 0.95)


@dataclass(frozen=True, slots=True)
class EconomicsBlock:
    """Every money figure plus the assumptions it is a function of.

    ``assumptions`` is not optional and is not empty by kindness:
    :func:`assert_self_describing` refuses to emit a payload with money and no
    recovery rate, because that is the exact shape of a fabricated number.
    """

    currency: str
    exposure_minor: int
    expected_value_minor: int
    recovery_rate: float
    analyst_cost_minor: int
    friction_cost_minor: int
    assumptions: Mapping[str, Any]
    monte_carlo: MonteCarloInterval | None = None

    def to_payload(self) -> dict[str, Any]:
        """Wire form, with the sensitivity band next to the point estimate."""
        return {
            "currency": self.currency,
            "exposure_minor": self.exposure_minor,
            "expected_value_minor": self.expected_value_minor,
            "recovery_rate": self.recovery_rate,
            "analyst_cost_minor": self.analyst_cost_minor,
            "friction_cost_minor": self.friction_cost_minor,
            "assumptions": dict(self.assumptions),
            "monte_carlo": None
            if self.monte_carlo is None
            else {
                "runs": self.monte_carlo.runs,
                "seed": self.monte_carlo.seed,
                "p05_minor": self.monte_carlo.p05_minor,
                "p50_minor": self.monte_carlo.p50_minor,
                "p95_minor": self.monte_carlo.p95_minor,
                "interval": list(self.monte_carlo.interval),
            },
        }


@dataclass(frozen=True, slots=True)
class ScoreBlock:
    """The score as the pipeline computed it, with the model that produced it.

    The API never recomputes a score (02 §B seam 5); this block is a copy of a
    stored row, and ``model_version`` is what lets a consumer tell which artifact
    a number came out of.
    """

    fused_score: float
    band: str
    scorecard_points: int
    reason_codes: Sequence[str]
    calibration: CalibrationReading
    model_version: str
    rule_ids: Sequence[str] = ()

    def to_payload(self) -> dict[str, Any]:
        """Wire form."""
        return {
            "fused_score": self.fused_score,
            "band": self.band,
            "scorecard_points": self.scorecard_points,
            "reason_codes": list(self.reason_codes),
            "rule_ids": list(self.rule_ids),
            "model_version": self.model_version,
            # The reading serialises itself, because the pairing that makes it honest is
            # the reading's own invariant: a consumer must not be able to receive the
            # numbers without also receiving the state that says whether they were
            # measured, and that is one rule with one owner.
            "calibration": self.calibration.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One append-only decision, exactly as the audit chain recorded it.

    ``reversal_of_seq`` is how a reversal appears: a NEW row referencing the
    original, both in the timeline (plan §15).
    """

    decision_seq: int
    action: str
    reason: str
    actor_id: str
    decided_at: datetime
    four_eyes_confirmed_by: str | None = None
    reversal_of_seq: int | None = None
    decided_on_superseded_run: bool = False


@dataclass(frozen=True, slots=True)
class CaseBundle:
    """Everything a consumer needs to interpret one case, in one payload.

    Built inside the decision transaction and serialised into the outbox row, so
    what is delivered later is what was true at decision time — not a re-read of
    tables the pipeline has since overwritten with a newer run.
    """

    run_id: str
    case_id: str
    account_key: str
    decided_at: datetime
    decision: DecisionRecord
    score: ScoreBlock
    economics: EconomicsBlock
    evidence_refs: Sequence[str]
    transaction_ids: Sequence[str]
    provenance: Mapping[str, Any]
    schema_version: str = CASE_SCHEMA_VERSION

    @property
    def idempotency_key(self) -> str:
        """The dedupe key for this exact decision (02 §E)."""
        return build_idempotency_key(self.run_id, self.case_id, self.decision.decision_seq)

    def to_payload(self) -> dict[str, Any]:
        """The JSON body an external consumer receives.

        The disclaimer and ``advisory_only`` are added here rather than passed in,
        so no caller can build a payload that omits them.
        """
        return {
            "schema_version": self.schema_version,
            "advisory_only": True,
            "disclaimer": OXBOW_DISCLAIMER,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "account_key": self.account_key,
            "decided_at": iso_z(self.decided_at),
            "decision": {
                "decision_seq": self.decision.decision_seq,
                "action": self.decision.action,
                "reason": self.decision.reason,
                "actor_id": self.decision.actor_id,
                "decided_at": iso_z(self.decision.decided_at),
                "four_eyes_confirmed_by": self.decision.four_eyes_confirmed_by,
                "reversal_of_seq": self.decision.reversal_of_seq,
                "decided_on_superseded_run": self.decision.decided_on_superseded_run,
            },
            "score": self.score.to_payload(),
            "economics": self.economics.to_payload(),
            "model_version": self.score.model_version,
            "evidence_refs": list(self.evidence_refs),
            "transaction_ids": list(self.transaction_ids),
            "provenance": dict(self.provenance),
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class SinkReceipt:
    """What any sink says back: accepted, and under which key.

    ``accepted`` is not a success boolean for the transport — the outbox owns
    delivery state. It answers the narrower question "is this a payload this
    consumer understands", which is a 4xx-shaped answer, not a delivery result.
    """

    accepted: bool
    idempotency_key: str
    consumer: str
    accepted_at: datetime


class SelfDescribingPayloadError(ValueError):
    """A payload tried to leave with a number that has no stated basis."""


def _contains_money(payload: Mapping[str, Any]) -> bool:
    """Whether any minor-unit figure appears anywhere in the body."""
    if any(key in payload for key in MONEY_FIELDS):
        return True
    for value in payload.values():
        if isinstance(value, Mapping) and _contains_money(value):
            return True
        if (
            isinstance(value, Sequence)
            and not isinstance(value, str | bytes)
            and any(isinstance(item, Mapping) and _contains_money(item) for item in value)
        ):
            return True
    return False


def _has_assumptions(payload: Mapping[str, Any]) -> bool:
    assumptions = payload.get("assumptions")
    if isinstance(assumptions, Mapping) and assumptions:
        return True
    return any(isinstance(value, Mapping) and _has_assumptions(value) for value in payload.values())


def _has_model_version(payload: Mapping[str, Any]) -> bool:
    if payload.get("model_version"):
        return True
    return any(
        isinstance(value, Mapping) and _has_model_version(value) for value in payload.values()
    )


def assert_self_describing(payload: Mapping[str, Any]) -> None:
    """Every money figure carries assumptions, a model version and the disclaimer.

    This is the mechanical form of plan §13's "the case payload travels with its
    assumptions". Called by every sink before it transmits, so the constraint
    holds for the null adapter writing a file and the webhook posting to an FIU
    alike — one rule, enforced at the boundary that has the bytes.
    """
    missing: list[str] = []
    if payload.get("disclaimer") != OXBOW_DISCLAIMER:
        missing.append("disclaimer")
    if payload.get("advisory_only") is not True:
        missing.append("advisory_only")
    if not payload.get("schema_version"):
        missing.append("schema_version")
    if not payload.get("run_id"):
        missing.append("run_id")

    if _contains_money(payload):
        if not _has_assumptions(payload):
            missing.append("assumptions (recovery rate and cost basis)")
        if not _has_model_version(payload):
            missing.append("model_version")

    # The same rule one field over: a consumer cannot receive an OXBOW probability without
    # the state that says whether it was measured. `CalibrationReading` refuses a
    # half-populated reading in Python, but a payload can arrive here from a stored outbox
    # row written by an older build, so the boundary checks the bytes too.
    calibration = payload.get("score", {}).get("calibration") if "score" in payload else None
    if isinstance(calibration, Mapping):
        kind = calibration.get("kind")
        if kind not in CALIBRATION_KINDS:
            missing.append("score.calibration.kind (calibrated_band | uncalibrated)")
        elif kind == UNCALIBRATED and not calibration.get("note"):
            missing.append("score.calibration.note (why no rate was measured)")

    if missing:
        raise SelfDescribingPayloadError(
            f"outbound payload is not self-describing; missing {', '.join(missing)}. "
            "A money figure may not leave OXBOW without the assumptions it is a "
            "function of (plan §13)."
        )


@runtime_checkable
class CaseSink(Protocol):
    """Delivers a decided case to one destination.

    Sinks are idempotent by ``idempotency_key`` (02 §E: at-least-once delivery
    with idempotent consumers, documented plainly). A sink that has already seen
    the key returns a receipt for the earlier delivery rather than re-emitting.
    """

    @property
    def sink_id(self) -> str:
        """Which destination this is, for the outbox ledger."""
        ...

    def emit(self, bundle: CaseBundle) -> SinkReceipt:
        """Transmit one case payload, or raise. Never swallow."""
        ...

    def has_delivered(self, idempotency_key: str) -> bool:
        """Whether this sink already holds this exact key."""
        ...


__all__ = [
    "CALIBRATED_BAND",
    "CALIBRATION_KINDS",
    "CASE_SCHEMA_VERSION",
    "DECISION_ACTIONS",
    "MONEY_FIELDS",
    "OXBOW_DISCLAIMER",
    "UNCALIBRATED",
    "CalibrationReading",
    "CaseBundle",
    "CaseSink",
    "DecisionRecord",
    "EconomicsBlock",
    "MonteCarloInterval",
    "ScoreBlock",
    "SelfDescribingPayloadError",
    "SinkReceipt",
    "assert_self_describing",
    "build_idempotency_key",
    "iso_z",
]
