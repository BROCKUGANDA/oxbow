"""The ReportSink port: a period's worth of findings, as one artifact (02 §A, §C).

Reports are the case payload's aggregate sibling, and they fail differently: a
report whose row totals disagree with its header is the kind of discrepancy that
gets noticed by an auditor twelve months later, in public. So the totals are
computed against the rows here, once, by a function, rather than assembled by
hand at each call site, and the sink refuses a report that does not add up.

The economic figures in a report are all assumptions-dependent (recovery rate,
analyst cost, friction cost), so :class:`ReportSubmission` carries the assumption
set it was computed under — the same rule that governs a single case, applied to
a period (plan §13, spec §6.1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

from oxbow.ports.case_sink import (
    CASE_SCHEMA_VERSION,
    OXBOW_DISCLAIMER,
    SinkReceipt,
    assert_self_describing,
    iso_z,
)

# The report kinds OXBOW emits. A closed set for the same reason as notification
# severities: the archive partitions on it.
REPORT_TYPES: Final = ("case_packet", "period_summary", "model_card_snapshot")

MONEY_TOTAL_KEYS: Final = ("exposure_minor", "loss_avoided_minor", "net_benefit_minor")


class ReportIntegrityError(ValueError):
    """The report's stated totals do not follow from its rows.

    A boundary failure, raised before transmission: sending a report whose
    numbers do not reconcile is worse than sending none.
    """


@dataclass(frozen=True, slots=True)
class ReportSubmission:
    """A period's findings, its money totals, and the assumptions behind them.

    ``rows`` are already-aggregated figures from the warehouse; nothing here is
    recomputed at submit time, which keeps the report and the UI in agreement
    about the same period (02 §B seam 5).
    """

    report_id: str
    report_type: str
    run_id: str
    period_start: datetime
    period_end: datetime
    rows: Sequence[Mapping[str, Any]]
    totals: Mapping[str, Any]
    assumptions: Mapping[str, Any]
    model_version: str
    currency: str
    schema_version: str = CASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.report_type not in REPORT_TYPES:
            raise ValueError(
                f"report_type {self.report_type!r} is not one of {REPORT_TYPES}"
            )
        if self.period_end < self.period_start:
            raise ValueError("period_end precedes period_start; a negative window is a bug")

    def verified(self) -> ReportSubmission:
        """Check the money totals against the rows, then return self.

        Called by every sink before transmission. Summing here rather than
        trusting a caller's arithmetic is the point: the discrepancy this catches
        is exactly the one a reviewer would find later.
        """
        for key in MONEY_TOTAL_KEYS:
            if key not in self.totals:
                continue
            recomputed = sum(int(row[key]) for row in self.rows if key in row)
            stated = int(self.totals[key])
            if recomputed != stated:
                raise ReportIntegrityError(
                    f"report {self.report_id}: totals.{key}={stated} but the rows sum to "
                    f"{recomputed}. Fix the aggregation, do not adjust the total."
                )
        return self

    def to_payload(self) -> dict[str, Any]:
        """Wire form: header, body rows, and the disclaimer that governs both."""
        return {
            "schema_version": self.schema_version,
            "advisory_only": True,
            "disclaimer": OXBOW_DISCLAIMER,
            "report_id": self.report_id,
            "report_type": self.report_type,
            "run_id": self.run_id,
            "currency": self.currency,
            "period_start": iso_z(self.period_start),
            "period_end": iso_z(self.period_end),
            "model_version": self.model_version,
            "assumptions": dict(self.assumptions),
            "totals": dict(self.totals),
            "rows": [dict(row) for row in self.rows],
        }

    def validated_payload(self) -> dict[str, Any]:
        """Totals-checked, self-describing payload, ready to transmit."""
        self.verified()
        payload = self.to_payload()
        assert_self_describing(payload)
        return payload


@runtime_checkable
class ReportSink(Protocol):
    """Delivers a finished report to one destination (FIU system, archive, store)."""

    @property
    def sink_id(self) -> str:
        """Which destination this is."""
        ...

    def submit(self, report: ReportSubmission) -> SinkReceipt:
        """Transmit one report, or raise."""
        ...


__all__ = [
    "MONEY_TOTAL_KEYS",
    "REPORT_TYPES",
    "ReportIntegrityError",
    "ReportSink",
    "ReportSubmission",
]
