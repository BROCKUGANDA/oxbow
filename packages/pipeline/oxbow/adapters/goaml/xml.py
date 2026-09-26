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

from oxbow.ports.case_sink import CaseBundle, iso_z
from oxbow.ports.report import ReportSubmission

GOAML_NAMESPACE: Final = "urn:oasis:names:tc:goaml-1-1"
GOAML_VERSION: Final = "1.1"
ADVISORY_NOTE: Final = (
    "OXBOW research prototype output. DRAFT ONLY - not a filed report and not a decision. "
    "A human at a reporting institution decides whether anything here is reportable. "
    "Monetary figures are model estimates under stated assumptions."
)
_SOURCE_SYSTEM: Final = "OXBOW"


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
    _el(element, "TransactionId", str(txn.get("txn_id", "")))
    amount = int(txn.get("amount_minor", 0))
    currency = str(txn.get("currency", "XXX"))
    _money(element, "TransactionAmount", amount, currency)
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
        f"{bundle.decision.reason} Band {bundle.score.band}; observed rate "
        f"{bundle.score.calibration.observed_rate:.4f} over n={bundle.score.calibration.n}; "
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
        _transaction(_el(activity, "FinancialTransaction"), row)
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
