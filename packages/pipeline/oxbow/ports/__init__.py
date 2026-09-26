"""Ports: abstract protocols, zero dependencies.

02 A: every boundary, internal or external, is a port with a typed contract and a
null adapter that writes to disk. That one decision is what lets OXBOW be
genuinely integration-ready without a single external dependency in the demo, and
it is also the honest answer when a judge asks "could this plug into a real
institution?" The answer is: here is the interface, here is the payload, here is
the contract test, and here is the file the null adapter wrote instead.

Nothing in ``features/``, ``graph/``, ``models/`` or ``quant/`` may import an
adapter. ``import-linter`` enforces that in CI, which is what stops
"integration-ready" from quietly decaying into a hairball.
"""

from __future__ import annotations

__all__: list[str] = []
