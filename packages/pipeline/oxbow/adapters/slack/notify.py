"""Slack notifications, rendered from the same payload every other sink sends.

Two deliberate differences from the webhook adapter, each stated in code as well as
here:

* **No HMAC header.** Slack's incoming-webhook URL *is* the credential — a signed
  body would be theatre, and the URL is treated as a secret: never logged, never
  echoed into an error message (02 §F). What the message keeps is the advisory line,
  because the consumer that reads it is a human on a phone.
* **A renderer, not a reformulation.** The text comes from the notification's own
  ``title``/``body``/money fields, so Slack and the webhook cannot disagree about
  what OXBOW claimed. If the payload carries money without assumptions, the port
  refuses it before it ever reaches this adapter.

Block Kit has a 3,000-character text limit per block; the truncation here is explicit
and says it truncated, because a silently clipped reason reads like a complete one.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from oxbow.adapters.retry import NonRetryableDeliveryError
from oxbow.adapters.webhook.sinks import DeliveryError
from oxbow.ports.case_sink import SinkReceipt, iso_z
from oxbow.ports.notify import Notification

SLACK_TEXT_LIMIT: Final = 2900
ADVISORY_LINE: Final = "Research prototype · historical de-identified data · not financial advice"
_SEVERITY_GLYPH: Final = {"info": "•", "warning": "▲", "critical": "■"}


def render_blocks(notification: Notification) -> list[dict[str, Any]]:
    """The Block Kit for one notification.

    Severity is carried by a glyph *and* the word, never by colour alone: the same
    rule the UI's band meter follows (00 §B non-negotiable), and a channel where
    "red means critical" is the only signal is unreadable in greyscale and to a
    screen reader.
    """
    glyph = _SEVERITY_GLYPH.get(notification.severity, "•")
    header = f"{glyph} {notification.severity.upper()} · {notification.title}"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": _plain(_clip(header, 150))},
        {"type": "section", "text": _plain(_clip(notification.body, SLACK_TEXT_LIMIT))},
    ]
    context_fields: list[dict[str, Any]] = [_plain(f"run `{notification.run_id}`")]
    if notification.account_key:
        context_fields.append(_plain(f"account `{notification.account_key}`"))
    if notification.case_id:
        context_fields.append(_plain(f"case `{notification.case_id}`"))
    context_fields.append(_plain(iso_z(notification.occurred_at)))
    blocks.append({"type": "context", "elements": context_fields})

    if notification.money_minor is not None:
        blocks.append(
            {
                "type": "section",
                "text": _plain(
                    f"Money figure: {notification.money_minor} {notification.currency} minor "
                    "units, under the assumptions carried in the payload "
                    f"({', '.join(f'{key}={value}' for key, value in sorted((notification.assumptions or {}).items()))})"
                ),
            }
        )
    if notification.requires_four_eyes:
        blocks.append(
            {
                "type": "section",
                "text": _plain(
                    "Awaiting second-reviewer confirmation (four-eyes). This decision has not "
                    "been released to the outbox."
                ),
            }
        )
    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [_plain(ADVISORY_LINE)]})
    return blocks


class SlackNotifySink:
    """Posts a rendered notification to a Slack incoming-webhook URL."""

    def __init__(
        self,
        *,
        webhook_url: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = 5.0,
        sink_id: str = "slack",
    ) -> None:
        if not webhook_url.startswith("https://"):
            raise ValueError(
                "a Slack incoming webhook must be https: the URL itself is the credential, so "
                "sending it in the clear is sending the secret."
            )
        self._url = webhook_url
        self._sink_id = sink_id
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._delivered: dict[str, SinkReceipt] = {}

    @property
    def sink_id(self) -> str:
        return self._sink_id

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def notify(self, notification: Notification) -> SinkReceipt:
        """Post to Slack, classifying failures the way the outbox needs them."""
        key = notification.notification_id
        if key in self._delivered:
            return self._delivered[key]
        # The call is the check, not the value: `validated_payload` raises unless the
        # notification is self-describing (02 §E), so an invalid one never reaches the
        # transport. Slack posts the rendered `blocks` below rather than the payload
        # dict, which is why the result is not bound to a name.
        notification.validated_payload()
        body = {
            "text": f"{notification.severity.upper()}: {notification.title}",
            "blocks": render_blocks(notification),
        }
        try:
            response = self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"slack: transport failure for {key}: {exc}") from exc
        if 200 <= response.status_code < 300:
            receipt = SinkReceipt(
                accepted=True,
                idempotency_key=key,
                consumer=self._sink_id,
                accepted_at=notification.occurred_at,
            )
            self._delivered[key] = receipt
            return receipt
        if 400 <= response.status_code < 500:
            raise NonRetryableDeliveryError(
                f"slack: refused {key} with {response.status_code}: {response.text[:200]}"
            )
        raise DeliveryError(
            f"slack: {response.status_code} for {key}: {response.text[:200]}",
            status_code=response.status_code,
        )

    def has_delivered(self, idempotency_key: str) -> bool:
        return idempotency_key in self._delivered


def _plain(text: str) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": text}


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 20] + "… [truncated by Slack]"


__all__ = ["ADVISORY_LINE", "SLACK_TEXT_LIMIT", "SlackNotifySink", "render_blocks"]
