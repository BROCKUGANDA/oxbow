"""GOAML sinks: an adapter composed over a port, not over another adapter's guts.

Both classes take an :class:`~oxbow.ports.objectstore.ObjectStoreAdapter` and write
the rendered XML through it. That composition is the design point: the same code path
lands in ``out/goaml/`` when the demo runs on null adapters and in the MinIO bucket
when it does not, and neither the GOAML renderer nor the object store has to know
about the other.

The delivery is a *draft artefact*, and the sink never sends it anywhere by itself.
Whether a reportable event exists is a legal judgement at a reporting institution,
and 02 §F keeps OXBOW on the informing side of that line.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from oxbow.adapters.goaml.xml import render_case_bundle, render_report
from oxbow.ports.case_sink import CaseBundle, SinkReceipt, assert_self_describing
from oxbow.ports.objectstore import ObjectNotFound, ObjectStoreAdapter
from oxbow.ports.report import ReportSubmission

XML_CONTENT_TYPE = "application/xml"

TransactionsProvider = Callable[[CaseBundle], Sequence[Mapping[str, Any]]]


def _no_transactions(_: CaseBundle) -> Sequence[Mapping[str, Any]]:
    """The default provider: render no transaction elements.

    Explicit rather than optional, because an absent evidence list changes what the
    document asserts. A caller who has the transactions injects a provider; a caller
    who does not gets a summary-shaped draft and can see that it is one.
    """
    return ()


class GoamlCaseSink:
    """Renders a decided case as GOAML 1.1 and stores the draft."""

    def __init__(
        self,
        store: ObjectStoreAdapter,
        *,
        prefix: str = "goaml/cases",
        transactions: TransactionsProvider = _no_transactions,
        sink_id: str = "goaml",
    ) -> None:
        self._store = store
        self._prefix = prefix
        self._transactions = transactions
        self._sink_id = sink_id

    @property
    def sink_id(self) -> str:
        return self._sink_id

    def key_for(self, bundle: CaseBundle) -> str:
        """The object key is the idempotency key.

        Same convention as the file sinks: a second delivery of the same decision
        names the same path with the same bytes, so the store's no-overwrite rule is
        the dedupe and there is no index to keep in step with it.
        """
        return f"{self._prefix}/{bundle.idempotency_key}.xml"

    def emit(self, bundle: CaseBundle) -> SinkReceipt:
        payload = bundle.to_payload()
        assert_self_describing(payload)
        xml = render_case_bundle(bundle, self._transactions(bundle))
        self._store.put(self.key_for(bundle), xml, XML_CONTENT_TYPE)
        return SinkReceipt(
            accepted=True,
            idempotency_key=bundle.idempotency_key,
            consumer=f"{self._sink_id}:{self._store.store_id}",
            accepted_at=datetime.now(UTC),
        )

    def has_delivered(self, idempotency_key: str) -> bool:
        """The store is the memory. No sidecar index to fall out of step with it."""
        try:
            self._store.head(f"{self._prefix}/{idempotency_key}.xml")
        except ObjectNotFound:
            return False
        return True

    def read(self, bundle: CaseBundle) -> bytes:
        """The stored draft, for the packet renderer and the contract test."""
        return self._store.get(self.key_for(bundle))


class GoamlReportSink:
    """Renders a period's report as GOAML 1.1 and stores the draft."""

    def __init__(
        self,
        store: ObjectStoreAdapter,
        *,
        prefix: str = "goaml/reports",
        sink_id: str = "goaml-report",
    ) -> None:
        self._store = store
        self._prefix = prefix
        self._sink_id = sink_id

    @property
    def sink_id(self) -> str:
        return self._sink_id

    def key_for(self, report: ReportSubmission) -> str:
        return f"{self._prefix}/{report.report_id}.xml"

    def submit(self, report: ReportSubmission) -> SinkReceipt:
        payload = report.validated_payload()
        assert_self_describing(payload)
        xml = render_report(report)
        self._store.put(self.key_for(report), xml, XML_CONTENT_TYPE)
        return SinkReceipt(
            accepted=True,
            idempotency_key=report.report_id,
            consumer=f"{self._sink_id}:{self._store.store_id}",
            accepted_at=datetime.now(UTC),
        )

    def read(self, report: ReportSubmission) -> bytes:
        return self._store.get(self.key_for(report))


__all__ = ["XML_CONTENT_TYPE", "GoamlCaseSink", "GoamlReportSink"]
