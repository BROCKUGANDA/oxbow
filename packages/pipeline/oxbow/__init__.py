"""OXBOW - quantitative risk scoring and financial-crime intelligence.

A research prototype. It analyses historical, de-identified data only. It does
not process live financial transactions, does not trade or advise on any
financial instrument, does not make real financial decisions, and is not
financial advice. Monetary figures are model estimates derived from stated
assumptions, not measured outcomes.

The package is a pipeline behind ports: ``features/``, ``graph/``, ``models/``,
``quant/`` and ``rules/`` may never import an adapter. ``import-linter`` enforces
that contract, which is what stops "integration-ready" from quietly decaying into
a hairball.
"""

from __future__ import annotations

__version__ = "0.1.0"

# The disclaimer travels with every exported packet, the README and the app
# footer. It is defined once, here, so a test can assert all three carry the
# identical string rather than three near-copies drifting apart.
DISCLAIMER = (
    "OXBOW is a research prototype that analyzes historical, de-identified data only. "
    "It does not process live financial transactions, does not trade or advise on any "
    "financial instrument, does not make real financial decisions, and is not financial "
    "advice. Monetary figures are model estimates derived from stated assumptions, not "
    "measured outcomes. Results are not validated for operational use by any financial "
    "institution."
)

__all__ = ["DISCLAIMER", "__version__"]
