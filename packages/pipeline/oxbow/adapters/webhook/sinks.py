"""Signed HTTP delivery for cases and notifications (02 §C, §E).

One attempt per call, and the signature computed over the **raw bytes actually
sent**. That distinction is the whole reason this adapter exists separately from a
generic POST helper: a client that serialises, mutates and re-serialises produces a
body whose signature verifies against nothing the receiver got.

Failure classes are separated because the outbox treats them differently (02 §E):

* 4xx → :class:`NonRetryableDeliveryError`. The consumer refused the payload.
  Retrying a validation error five times is a hammer, and plan §18 lists it as a
  rejection trigger.
* 5xx / transport → ``DeliveryError``, which the outbox retries on the jittered
  ladder and dead-letters after the fifth attempt.

The client is injectable so the contract test can run the real request path against
``httpx.MockTransport`` — same signing code, same status handling, no socket — and
the integration suite can point the same class at the Compose echo service.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from oxbow.adapters.io import dumps
from oxbow.adapters.retry import NonRetryableDeliveryError
from oxbow.adapters.signing import (
    IDEMPOTENCY_HEADER,
    SCHEMA_VERSION_HEADER,
    SIGNATURE_HEADER,
    sign_body,
)
from oxbow.ports.case_sink import CaseBundle, SinkReceipt, assert_self_describing
from oxbow.ports.notify import Notification


class DeliveryError(RuntimeError):
    """The consumer or the network refused, and a retry is legitimate."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HttpSink:
    """Shared transport for the HTTP sinks: sign once, post once, classify."""

    def __init__(
        self,
        *,
        url: str,
        secret: str,
        sink_id: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError(
                f"sink url must be http(s), got {url!r}. TLS always in transit; the plain "
                "path is permitted only against loopback in the demo compose stack (02 §E)."
            )
        if not secret:
            raise ValueError(
                f"{sink_id}: no signing secret. An unsigned delivery cannot be verified by "
                "the receiver, which makes it indistinguishable from a forged one."
            )
        self._url = url
        self._secret = secret
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

    def __enter__(self) -> HttpSink:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def post_signed(self, key: str, schema_version: str, payload: dict[str, Any]) -> SinkReceipt:
        """Transmit one payload and classify the response."""
        body = dumps(payload).encode("utf-8")
        signature, _ = sign_body(body, self._secret)
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: signature,
            IDEMPOTENCY_HEADER: key,
            SCHEMA_VERSION_HEADER: schema_version,
        }
        try:
            response = self._client.post(self._url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DeliveryError(
                f"{self._sink_id}: transport failure delivering {key}: {exc}"
            ) from exc

        if 200 <= response.status_code < 300:
            receipt = SinkReceipt(
                accepted=True,
                idempotency_key=key,
                consumer=self._sink_id,
                accepted_at=datetime.now(UTC),
            )
            self._delivered[key] = receipt
            return receipt
        if 400 <= response.status_code < 500:
            raise NonRetryableDeliveryError(
                f"{self._sink_id}: consumer refused {key} with {response.status_code}: "
                f"{response.text[:240]}. This is a payload problem; a retry cannot fix it."
            )
        raise DeliveryError(
            f"{self._sink_id}: {response.status_code} delivering {key}: {response.text[:240]}",
            status_code=response.status_code,
        )

    def has_delivered(self, idempotency_key: str) -> bool:
        """Whether this process already got a 2xx for this key.

        Honest about its own limits: the outbox row in Postgres is what the system
        trusts about delivery (02 §E). This is a within-process guard so a retry that
        follows an ambiguous timeout does not double-post a payload the consumer may
        already have taken.
        """
        return idempotency_key in self._delivered


class WebhookCaseSink(HttpSink):
    """POSTs a signed case bundle to an external consumer."""

    def __init__(self, *, url: str, secret: str, client: httpx.Client | None = None) -> None:
        super().__init__(url=url, secret=secret, sink_id="webhook", client=client)

    def emit(self, bundle: CaseBundle) -> SinkReceipt:
        if self.has_delivered(bundle.idempotency_key):
            return self._delivered[bundle.idempotency_key]
        payload = bundle.to_payload()
        assert_self_describing(payload)
        return self.post_signed(bundle.idempotency_key, bundle.schema_version, payload)


class WebhookNotifySink(HttpSink):
    """POSTs a signed notification to an HTTP consumer that is not Slack."""

    def __init__(self, *, url: str, secret: str, client: httpx.Client | None = None) -> None:
        super().__init__(url=url, secret=secret, sink_id="webhook-notify", client=client)

    def notify(self, notification: Notification) -> SinkReceipt:
        key = notification.notification_id
        if self.has_delivered(key):
            return self._delivered[key]
        payload = notification.validated_payload()
        return self.post_signed(key, notification.schema_version, payload)


__all__ = ["DeliveryError", "HttpSink", "WebhookCaseSink", "WebhookNotifySink"]
