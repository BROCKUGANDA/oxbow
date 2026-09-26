"""The NotifySink port: a human told something happened (02 §A, §C).

Notifications are the least trustworthy thing OXBOW sends, because a person reads
a one-line message and acts on it without opening the case. Three consequences
shape this port:

* the payload carries the same disclaimer and assumptions as the case payload, so
  a Slack message cannot be more confident than the JSON behind it;
* ``requires_four_eyes`` travels with the message: a notification that says
  "escalated" about a decision still awaiting second review is a lie by omission
  (02 §F);
* the sink raises on delivery failure. A notification that silently did not
  arrive is worse than one that visibly failed (03 §A rule 1).
"""

from __future__ import annotations

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

# Severities are a closed set: an open-ended string here becomes five spellings of
# the same urgency in three adapters, and the UI has to sort on it.
NOTIFICATION_SEVERITIES: Final = ("info", "warning", "critical")

# A notification is never a decision. Enforced at the payload level so no channel
# can quietly imply one (02 §F: OXBOW informs a human, it does not act as one).
ADVISORY_ONLY: Final = True


@dataclass(frozen=True, slots=True)
class Notification:
    """One message about one case or run, channel-agnostic.

    Channel-specific rendering belongs to the adapter; the *content* and the
    advisory framing belong here, so Slack and a webhook cannot disagree about
    what was claimed.
    """

    notification_id: str
    run_id: str
    severity: str
    title: str
    body: str
    occurred_at: datetime
    case_id: str | None = None
    account_key: str | None = None
    requires_four_eyes: bool = False
    money_minor: int | None = None
    currency: str | None = None
    assumptions: dict[str, Any] | None = None
    model_version: str | None = None
    schema_version: str = CASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.severity not in NOTIFICATION_SEVERITIES:
            raise ValueError(
                f"severity {self.severity!r} is not one of {NOTIFICATION_SEVERITIES}. "
                "A queue that sorts on an unknown severity silently sorts last."
            )
        if not self.title.strip() or not self.body.strip():
            raise ValueError(
                "a notification must carry a title and a body; empty text is a bug, not a message"
            )
        if self.money_minor is not None and not (self.currency and self.assumptions):
            raise ValueError(
                "a money figure in a notification must carry its currency and assumptions "
                "(plan §13: a consumer cannot receive an OXBOW number without what it depends on)"
            )

    def to_payload(self) -> dict[str, Any]:
        """Wire form. Disclaimer and advisory flags are added, never accepted as input."""
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "advisory_only": ADVISORY_ONLY,
            "disclaimer": OXBOW_DISCLAIMER,
            "notification_id": self.notification_id,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "account_key": self.account_key,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "occurred_at": iso_z(self.occurred_at),
            "requires_four_eyes": self.requires_four_eyes,
        }
        if self.money_minor is not None:
            payload["expected_value_minor"] = self.money_minor
            payload["currency"] = self.currency
            payload["assumptions"] = dict(self.assumptions or {})
            payload["model_version"] = self.model_version
        return payload

    def validated_payload(self) -> dict[str, Any]:
        """The payload after the self-describing check, for sinks to transmit."""
        payload = self.to_payload()
        assert_self_describing(payload)
        return payload


@runtime_checkable
class NotifySink(Protocol):
    """Delivers a notification to one human-facing channel."""

    @property
    def sink_id(self) -> str:
        """Which channel this is, for the delivery ledger."""
        ...

    def notify(self, notification: Notification) -> SinkReceipt:
        """Transmit one notification, or raise. Never swallow a failure."""
        ...

    def has_delivered(self, idempotency_key: str) -> bool:
        """Dedupe check; the key is the notification id (02 §E)."""
        ...


__all__ = [
    "ADVISORY_ONLY",
    "NOTIFICATION_SEVERITIES",
    "Notification",
    "NotifySink",
]
