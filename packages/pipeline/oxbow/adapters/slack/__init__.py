"""Slack notifications: the same payload, rendered for a channel (02 §A).

A human reads one line and acts on it, which is why the message carries the
advisory framing and the four-eyes state instead of a confident verb. Rendering
lives here; content and claims come from ``ports/notify.py``, so Slack and a
webhook cannot disagree about what OXBOW said.
"""

from __future__ import annotations

__all__: list[str] = []
