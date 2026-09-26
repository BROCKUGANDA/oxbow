"""Deterministic serialisation of backtest results to JSON bytes.

WHY A DEDICATED MODULE: plan §12's gate is "results serialise to JSON that the
validation page reads directly", and the determinism rule (01 §A4) is stricter than
"valid JSON" — a fixed input must produce *identical bytes* on every run, because
``make verify-determinism`` compares artifact digests, and a floating-point value
printed as ``0.30000000000000004`` in one run and ``0.3`` in the next would fail a
reproduction that is actually a reproduction.

THE RULES THAT MAKE BYTES STABLE:
  * keys sorted, so dictionary iteration order never leaks into the output;
  * every float rounded to a fixed number of decimals before serialisation, so IEEE
    representation drift cannot appear;
  * money is emitted as an integer minor-unit field with an explicit ``_minor``
    suffix and a ``currency`` beside it — never a rendered string, so the number a
    page reads is the number arithmetic produced (DEV-005);
  * ``None`` is preserved as JSON ``null`` (undefined precision/recall), never coerced
    to 0 — that coercion is exactly what ``test_no_alerts_fold_undefined_not_zero``
    forbids.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FLOAT_DECIMALS = 8
DISCLAIMER = (
    "OXBOW is a research prototype that analyzes historical, de-identified data only. "
    "It does not process live financial transactions, does not trade or advise on any "
    "financial instrument, does not make real financial decisions, and is not financial "
    "advice. Monetary figures are model estimates derived from stated assumptions, not "
    "measured outcomes. Results are not validated for operational use by any financial "
    "institution."
)


def _stabilize(value: Any) -> Any:
    """Recursively coerce a result tree into byte-stable JSON primitives."""
    if isinstance(value, dict):
        return {str(key): _stabilize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_stabilize(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # Round first, then normalise -0.0 to 0.0, so the printed form is canonical.
        rounded = round(value, FLOAT_DECIMALS)
        return 0.0 if rounded == 0 else rounded
    if value is None:
        return None
    return str(value)


def stable_json(payload: dict[str, Any]) -> str:
    """Serialise to a canonical JSON string: sorted keys, fixed separators."""
    return json.dumps(_stabilize(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_sha256(payload: dict[str, Any]) -> str:
    """Content hash of the canonical JSON, for ``make verify-determinism``."""
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def write_json(payload: dict[str, Any], path: Path) -> str:
    """Write the payload as indented, byte-stable JSON and return the resolved path.

    The file is newline-terminated and UTF-8 with ASCII escapes so the same bytes
    appear on the Windows console host as on CI's Linux runner — a codepage-dependent
    write is not a reproducible artifact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(_stabilize(payload), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    path.write_text(rendered, encoding="utf-8")
    return str(path.resolve())


__all__ = ["DISCLAIMER", "stable_json", "stable_sha256", "write_json"]
