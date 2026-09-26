"""structlog JSON with ``run_id`` and ``trace_id`` on every line, and a redaction filter.

02 §F puts the PII boundary at ingest and requires the boundary to hold in logs and
traces as well as in responses — a raw account id in a JSON log line that ships to a
log aggregator is the same disclosure as one in an API response, with a wider audience
and no auth check on the reader.

Two rules are in tension and both are honoured here:

* **never silent.** The filter does not drop a field quietly: it replaces the value
  with ``[redacted:<rule>]`` so the line still says a field was there and why it is
  not readable. A log that silently omits keys cannot be audited for correctness.
* **the pattern is scoped, not global.** ``config/sources.yaml`` gives
  ``redaction_pattern: ^(?!ACC-)[A-Za-z0-9_\\.]{6,}$``. Applied to every *value*, that
  regex matches ``escalate``, ``complete`` and ``Africa/Kampala`` and would redact the
  log into uselessness. So the pattern is applied to fields whose **name** marks them
  as identifiers, and the value-side check uses the shapes the two corpora actually
  use (PaySim ``C1234567``, bare numeric ids, IBM ``Account####``). The scope is a
  decision, and it is recorded in ``DECISIONS.md`` rather than implemented quietly.

The tests in ``tests/unit/test_p7_logging.py`` assert the filter fires on a raw id and
that an ``ACC-`` key survives it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any, Final

import structlog

# --- contextual keys, set by the middleware / worker wrapper ----------------

run_id_var: ContextVar[str | None] = ContextVar("oxbow_run_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("oxbow_trace_id", default=None)

# --- the redaction rules -----------------------------------------------------

RAW_FIELD_NAME_PATTERN: Final = re.compile(
    r"(^|_)(raw_)?(account_?id|customer_?id|client_?id|nameorig|namedest|pan|"
    r"email|phone|passport|national_?id|salt|secret|password|token|authorization)(_|$)",
    re.IGNORECASE,
)

# The identifier shapes the two permitted corpora actually use. Deliberately narrow:
# a general "looks like a token" heuristic either over-redacts or under-redacts, and
# the point is to stop the specific identifiers that cross the ingest boundary.
RAW_VALUE_PATTERNS: Final = (
    re.compile(r"^C\d{6,}$"),  # PaySim nameOrig / nameDest
    re.compile(r"^\d{9,}$"),  # bare numeric account identifiers
    re.compile(r"^Account\d+$", re.IGNORECASE),  # IBM-AML account ids
)

PSEUDONYMOUS_PREFIX: Final = "ACC-"
REDACTED: Final = "[redacted:{reason}]"

# Keys that are always safe by name, so a value-shape match on them stays visible.
SAFE_KEYS: Final = frozenset({"account_key", "case_id", "run_id", "txn_id", "trace_id", "id"})

_CONFIGURED_PATTERN_KEY: Final = "redaction_pattern"


def _configured_raw_pattern() -> re.Pattern[str] | None:
    """The pattern declared in ``config/sources.yaml``, if it parses.

    Read from config rather than hard-coded because 00 §G requires every tunable to
    live in ``config/``; a compiled-regex cache means the file is read once.
    """
    from oxbow.config import find_repo_root, load_yaml  # local: logging must not need config

    try:
        sources = load_yaml(find_repo_root() / "config" / "sources.yaml")
    except (OSError, ValueError):
        # A missing or malformed config file must not disable redaction; the value-side
        # check falls back to the corpus shapes alone, which still catches both
        # identifier formats this project is allowed to read.
        return None
    policy = (
        sources.get("deidentification", {})
        .get("raw_identifier_policy", {})
        .get(_CONFIGURED_PATTERN_KEY)
    )
    if not isinstance(policy, str):
        return None
    try:
        return re.compile(policy)
    except re.error:
        return None


_CONFIGURED_RAW_VALUE_PATTERN: re.Pattern[str] | None = None


def _configured_pattern() -> re.Pattern[str] | None:
    """The compiled config pattern, cached after first use."""
    global _CONFIGURED_RAW_VALUE_PATTERN
    if _CONFIGURED_RAW_VALUE_PATTERN is None:
        _CONFIGURED_RAW_VALUE_PATTERN = _configured_raw_pattern() or re.compile(r"(?!x)x")
    return _CONFIGURED_RAW_VALUE_PATTERN


def redact_value(key: str, value: Any) -> tuple[Any, str | None]:
    """Return ``(value, reason)`` — reason is ``None`` when nothing was redacted.

    Split out from the processor so the rule can be unit-tested without a logging
    pipeline, and so the API's response scrubber and the log filter cannot drift:
    both call this function.
    """
    if RAW_FIELD_NAME_PATTERN.search(key):
        return REDACTED.format(reason="field-name"), "field-name"
    if not isinstance(value, str) or key in SAFE_KEYS:
        return value, None
    if value.startswith(PSEUDONYMOUS_PREFIX):
        return value, None
    for pattern in RAW_VALUE_PATTERNS:
        if pattern.match(value):
            return REDACTED.format(reason="raw-identifier"), "raw-identifier"
    configured = _configured_pattern()
    if (
        configured is not None
        and configured.match(value)
        and any(char.isdigit() for char in value)
        and " " not in value
    ):
        # The configured regex matches any six-plus alphanumeric string, which includes
        # "escalate". Requiring a digit keeps it on identifier-shaped values -- C12345678,
        # Account99, 0001234567890 -- without swallowing the vocabulary the rest of the
        # log line is made of.
        return REDACTED.format(reason="configured-pattern"), "configured-pattern"
    return value, None


def redaction_processor(
    logger: object, method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Drop raw identifiers from every field, and say that it happened (03 §D)."""
    redacted_keys: list[str] = []
    for key, value in list(event_dict.items()):
        if key in {"event", "level", "timestamp", "_"}:
            continue
        new_value, reason = redact_value(key, value)
        if reason is not None:
            event_dict[key] = new_value
            redacted_keys.append(key)
        elif isinstance(value, Mapping):
            event_dict[key] = _redact_mapping(value)
            if event_dict[key] != value:
                redacted_keys.append(key)
    if redacted_keys:
        event_dict["redacted_fields"] = sorted(set(redacted_keys))
    return event_dict


def _redact_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        new_value, reason = redact_value(key, value)
        if reason is None and isinstance(value, Mapping):
            new_value = _redact_mapping(value)
        out[key] = new_value
    return out


def add_run_and_trace(
    logger: object, method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Stamp ``run_id`` and ``trace_id`` on every line, from the context vars.

    A line without them is not wrong, it is unattributable, and an investigation tool
    whose failures cannot be traced to a run is the thing plan §14 says it must not be.
    """
    event_dict.setdefault("run_id", run_id_var.get())
    event_dict.setdefault("trace_id", trace_id_var.get() or new_trace_id())
    return event_dict


def new_trace_id() -> str:
    """A trace id for a request or job that arrived without one."""
    return uuid.uuid4().hex


def configure_logging(*, json_output: bool = True, level: str = "INFO") -> None:
    """Install the processor chain. Called once at import of the app and the worker."""
    shared: list[Callable[..., Any]] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_run_and_trace,
        redaction_processor,
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_level_to_int(level)),
        cache_logger_on_first_use=True,
    )


def _level_to_int(level: str) -> int:
    return {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}.get(
        level.upper(), 20
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


__all__ = [
    "PSEUDONYMOUS_PREFIX",
    "RAW_FIELD_NAME_PATTERN",
    "RAW_VALUE_PATTERNS",
    "REDACTED",
    "add_run_and_trace",
    "configure_logging",
    "get_logger",
    "new_trace_id",
    "redact_value",
    "redaction_processor",
    "run_id_var",
    "trace_id_var",
]
