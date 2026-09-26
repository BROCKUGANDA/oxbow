"""Offline SDN-snapshot screening: enrichment, never an automatic decision.

The snapshot shipped here is a small, clearly-labelled sample of fictional entries
so the matcher is exercised end to end without claiming to be the live OFAC list.
Replacing it is an operator step (point ``OXBOW_WATCHLIST_PATH`` at the published
SDN.CSV), and ``automatic_decision_authority`` is ``False`` in both cases — the
contract test asserts it on every watchlist adapter (02 §F).
"""

from __future__ import annotations

__all__: list[str] = []
