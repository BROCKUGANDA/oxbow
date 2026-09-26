"""Signed HTTP delivery: the outbound half of the outbox (02 §E).

Every request carries ``X-OXBOW-Signature: t=<unix>,v1=<hex>`` over
``t + \".\" + raw_body`` with HMAC-SHA256, a 300-second replay window and a
constant-time compare on the receiving side. Retries are the outbox's, not this
adapter's: it makes one attempt and reports what happened.
"""

from __future__ import annotations

__all__: list[str] = []
