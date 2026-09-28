"""GOAML 1.1 rendering for cases and reports (02 §C: FIU reporting channel).

GOAML (ISO 20922) is the XML a financial institution hands an intelligence unit, so
this adapter exists to prove the case payload has a shape a real regulator-facing
system could consume — not to file anything. Two things are structural rather than
cosmetic:

* the document is emitted as a **draft with an advisory note**. ``MsgNote`` says
  OXBOW is a research prototype and that a human at a reporting institution decides
  whether to file. A prototype that produced a submission-ready filing without a
  human in the loop would be automating the one decision 02 §F reserves to a person.
* every money value carries its currency as an ISO 4217 attribute and its minor-unit
  scale in a ``ValueScale`` element, because the ISO format assumes a decimal amount
  and OXBOW's money is integer minor units (DEV-005). Rendering it without saying so
  would invite someone to read ``5000000`` as five million shillings rather than
  fifty thousand.

Conformance asserted here is to the documented structural subset (namespace, root,
header, report, transaction elements) plus well-formedness. A full ISO 20922 XSD
validation is an integration task with the receiving FIU's schema, and the test names
that limit rather than implying more than it checks.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from oxbow.ports.case_sink import CALIBRATED_BAND, CalibrationReading, CaseBundle, iso_z
from oxbow.ports.report import ReportSubmission

GOAML_NAMESPACE: Final = "urn:oasis:names:tc:goaml-1-1"
GOAML_VERSION: Final = "1.1"
ADVISORY_NOTE: Final = (
    "OXBOW research prototype output. DRAFT ONLY - not a filed report and not a decision. "
    "A human at a reporting institution decides whether anything here is reportable. "
    "Monetary figures are model estimates under stated assumptions."
)
_SOURCE_SYSTEM: Final = "OXBOW"


class GoamlMoneyError(ValueError):
    """A transaction the document cannot state as money, named by its transaction id.

    ISO 4217's ``XXX`` means "no currency", and a missing amount is not zero shillings. Both
    defaults used to be invented here, which would put a fabricated figure in a regulator-facing
    draft — the one error class the whole no-implicit-FX rule (DEV-005) exists to stop. The
    canonical contract declares both fields non-nullable, so reaching this is a boundary failure
    and 03 §A rule 1 wants it loud.
    """


def _el(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, f"{{{GOAML_NAMESPACE}}}{tag}", dict(attrs))
    if text is not None:
        element.text = text
    return element


def _money(parent: ET.Element, tag: str, minor: int, currency: str) -> ET.Element:
    """A money element plus the scale that makes ``5000000`` mean fifty thousand."""
    element = _el(parent, tag, str(minor), currency=currency.upper().strip())
    _el(element, "ValueScale", "-2")
    return element


def _msg_header(root: ET.Element, *, doc_id: str, doc_type: str, created_at: datetime) -> None:
    header = _el(root, "MsgHeader")
    _el(header, "MsgGUID", doc_id)
    _el(header, "SendingSystem", _SOURCE_SYSTEM)
    _el(header, "MsgCreationDateTime", iso_z(created_at))
    doc_headers = _el(header, "DocHeaders")
    _el(doc_headers, "DocumentId", doc_id)
    _el(doc_headers, "DocVersion", "1")
    _el(doc_headers, "DocSubType", "FULL")
    _el(doc_headers, "DocType", doc_type)
    org = _el(header, "SendingOrg")
    _el(org, "OrgName", "OXBOW research prototype")
    _el(org, "OrgUnit", "NOT A FILING ENTITY - draft output only")
    _el(header, "MsgNote", ADVISORY_NOTE)


def _transaction(element: ET.Element, txn: Mapping[str, Any]) -> None:
    txn_id = str(txn.get("txn_id", ""))
    _el(element, "TransactionId", txn_id)
    minor = txn.get("amount_minor")
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise GoamlMoneyError(
            f"transaction {txn_id or '<missing id>'}: amount_minor is {minor!r}, not an integer "
            "minor-unit amount. A missing amount rendered as 0 would state a payment that did not "
            "happen (DEV-005)."
        )
    currency = txn.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        raise GoamlMoneyError(
            f"transaction {txn_id or '<missing id>'}: currency is {currency!r}. ISO 4217 has no "
            "code for 'unknown' that is not 'XXX' = no currency, and a filing that says a "
            "transaction had no currency is a claim nobody measured."
        )
    _money(element, "TransactionAmount", minor, currency)
    when = txn.get("event_ts_utc")
    if isinstance(when, datetime):
        _el(element, "TransactionDateTime", iso_z(when))
    elif when:
        _el(element, "TransactionDateTime", str(when))
    _el(element, "TransactionTypeCode", str(txn.get("txn_type", "")))
    for role, key in (
        ("Debitor", "src_account_key"),
        ("Creditor", "dst_account_key"),
    ):
        account = txn.get(key)
        if account:
            party = _el(element, role)
            identifiers = _el(party, "IdentifyingInfo")
            _el(identifiers, "IdentifierTypeCode", "AccountKey")
            _el(identifiers, "DataIdentifier", str(account))


#: The report's money rows, and the element each becomes. ``ReportSubmission.rows`` are
#: "already-aggregated figures from the warehouse" (``ports/report.py``), not transactions: they
#: carry a period label, a case count and three minor-unit totals, and no transaction id, amount
#: or type at all.
REPORT_BUCKET_MONEY: Final = (
    ("exposure_minor", "ExposureAmountTotalMinorUnits"),
    ("loss_avoided_minor", "LossAvoidedAmountTotalMinorUnits"),
    ("net_benefit_minor", "NetBenefitAmountTotalMinorUnits"),
)


def _report_bucket(parent: ET.Element, row: Mapping[str, Any], *, currency: str) -> None:
    """One activity bucket of the periodic report, stated as the aggregate it is.

    These rows used to be rendered through :func:`_transaction`, and the defaults there meant what
    reached the document was a blank ``TransactionId``, an amount of ``0`` and ISO 4217 ``XXX``
    ("no currency") for every bucket — three invented fields in the one artifact an FIU would act
    on, in the module whose own header promises every money value carries its currency. A bucket
    now says its period, its case count and its three totals, each with the report's declared
    currency and minor-unit scale.
    """
    element = _el(parent, "ReportedActivityBucket")
    bucket = row.get("bucket")
    if not isinstance(bucket, str) or not bucket.strip():
        raise GoamlMoneyError(
            f"report bucket {bucket!r}: a periodic report row has to name the period it totals, "
            "or its money belongs to no time window"
        )
    _el(element, "PeriodLabel", bucket)
    cases = row.get("cases")
    if isinstance(cases, bool) or not isinstance(cases, int):
        raise GoamlMoneyError(f"report bucket {bucket}: cases is {cases!r}, not a count of cases")
    _el(element, "CaseCount", str(cases))
    for key, tag in REPORT_BUCKET_MONEY:
        minor = row.get(key)
        if isinstance(minor, bool) or not isinstance(minor, int):
            raise GoamlMoneyError(
                f"report bucket {bucket}: {key} is {minor!r}, not an integer minor-unit amount "
                "(DEV-005); an absent total rendered as zero would report that nothing was exposed"
            )
        _money(element, tag, minor, currency)


def _confidence_clause(calibration: CalibrationReading) -> str:
    """The confidence sentence of a narrative, in words for either state.

    A GOAML narrative is the text a financial institution files, so the sentence has to
    survive both shapes of the stored pairing without inventing a number. Formatting an
    uncalibrated reading with ``:.4f`` raises ``TypeError`` on ``None`` — the render dies
    on the way out — and defaulting it would print "observed rate 0.0000 over n=None"
    into a document that leaves the building, which is the fabricated zero at the worst
    possible place. The words are the pipeline's own (``confidence_label``'s
    ``uncalibrated`` state), and the fold's reason travels with them.
    """
    if calibration.kind == CALIBRATED_BAND:
        return f"observed rate {calibration.observed_rate:.4f} over n={calibration.n}"
    # The note is the fold's own sentence and it already opens with "probabilities are
    # uncalibrated", because that phrase is the pipeline's vocabulary rather than this
    # renderer's; restating it here would print it twice in a filed document.
    return f"confidence: {calibration.note}"


def render_case_bundle(bundle: CaseBundle, transactions: Sequence[Mapping[str, Any]] = ()) -> bytes:
    """A GOAML 1.1 draft report for one decided case.

    ``transactions`` are the evidence rows behind the case, already pseudonymous: the
    account keys inside them are the only identifiers this document can carry, which
    is what keeps 03 §D's boundary true on a channel designed to carry account data.
    """
    payload = bundle.to_payload()
    root = ET.Element(
        f"{{{GOAML_NAMESPACE}}}GOAML", {"MsgDocVersion": GOAML_VERSION, "Action": "DRAFT"}
    )
    _msg_header(
        root,
        doc_id=f"OXBOW-CASE-{bundle.case_id}-{bundle.decision.decision_seq}",
        doc_type="SAR",
        created_at=bundle.decided_at,
    )
    report = _el(root, "Report")
    activities = _el(report, "Activities")
    activity = _el(activities, "FinancialInstTransactionReport")
    _el(activity, "ActivitySubType", "SUSPICIOUS_TRANSACTION_REPORT")
    _el(activity, "ActivityDateTime", iso_z(bundle.decided_at))

    subject = _el(activity, "Subject")
    _el(subject, "SubjectType", "INSTRUMENT")
    role = _el(subject, "RoleInActivity", "SubjectRole")
    _el(role, "ActivityRole", "SUSPECT")
    identifiers = _el(subject, "IdentifyingInfo")
    _el(identifiers, "IdentifierTypeCode", "AccountKey")
    _el(identifiers, "DataIdentifier", bundle.account_key)

    narrative = _el(activity, "NarrativeText")
    narrative.text = (
        f"Action: {bundle.decision.action}. Reason as recorded by the reviewer: "
        f"{bundle.decision.reason} Band {bundle.score.band}; "
        f"{_confidence_clause(bundle.score.calibration)}; "
        f"model version {bundle.score.model_version}. "
        f"Recovery rate assumption {bundle.economics.recovery_rate}. "
        f"{ADVISORY_NOTE}"
    )

    for txn in transactions:
        _transaction(_el(activity, "FinancialTransaction"), txn)

    bank = _el(activity, "InstBranch")
    _el(bank, "CountryCode", str(payload.get("provenance", {}).get("country_code", "XX")))
    _money(
        bank,
        "TransactionAmountTotalMinorUnits",
        bundle.economics.exposure_minor,
        bundle.economics.currency,
    )
    _el(bank, "ReportedTotalMinorUnits", str(bundle.economics.expected_value_minor))
    _el(
        root,
        "MsgNote",
        f"schema_version={payload['schema_version']} idempotency_key={bundle.idempotency_key}",
    )
    return _to_xml(root)


def render_report(submission: ReportSubmission) -> bytes:
    """A GOAML 1.1 periodic summary: totals plus one transaction element per row."""
    root = ET.Element(
        f"{{{GOAML_NAMESPACE}}}GOAML", {"MsgDocVersion": GOAML_VERSION, "Action": "DRAFT"}
    )
    _msg_header(
        root,
        doc_id=f"OXBOW-REPORT-{submission.report_id}",
        doc_type="SAR",
        created_at=submission.period_end,
    )
    report = _el(root, "Report")
    activities = _el(report, "Activities")
    activity = _el(activities, "FinancialInstTransactionReport")
    _el(activity, "ActivitySubType", "CUMULATIVE_REPORT")
    _el(activity, "ActivityDateTime", iso_z(submission.period_start))
    _el(activity, "PeriodicCompletion", "TRUE")
    for row in submission.rows:
        _report_bucket(activity, row, currency=submission.currency)
    summary = _el(activity, "InstBranch")
    _money(
        summary,
        "TransactionAmountTotalMinorUnits",
        int(submission.totals.get("exposure_minor", 0)),
        submission.currency,
    )
    _el(summary, "Rowcount", str(len(submission.rows)))
    _el(root, "MsgNote", ADVISORY_NOTE)
    return _to_xml(root)


def _to_xml(root: ET.Element) -> bytes:
    ET.register_namespace("", GOAML_NAMESPACE)
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


__all__ = [
    "ADVISORY_NOTE",
    "GOAML_NAMESPACE",
    "GOAML_VERSION",
    "render_case_bundle",
    "render_report",
]
