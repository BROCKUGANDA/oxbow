"""GOAML (ISO 20922) case export for an FIU reporting channel (02 §C).

OXBOW produces a well-formed report in the format a financial-intelligence unit
ingests, and it never submits one: the XML carries an explicit advisory marker and
the disclaimer, because filing a report is a human/legal act, not an output of a
prototype (02 §F: OXBOW informs a decision, it does not make one).
"""

from __future__ import annotations

__all__: list[str] = []
