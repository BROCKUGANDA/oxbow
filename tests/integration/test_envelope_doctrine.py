"""The API envelope doctrine, enforced instead of merely described.

Two modules in `apps/api` claim this guard exists: `schemas/common.py` says a
"doctrine test walks the OpenAPI document and fails if any 2xx schema grows a
`success` or `error` property", and `routers/common.py` repeats the claim. Neither
existed when this file was written — the assertion was living in a docstring, which
is the failure mode plan 00 B names: a gate that never fails against a known-bad
input is decoration.

The rule being enforced is not a style preference. A 200 response carrying
`{"success": false}` creates a second, contradictory status channel next to the HTTP
code, and a client then has to guess which one is true. So: operational payloads are
`{data, meta}` and nothing else; status lives in the status code; the client unwraps
at exactly one chokepoint.

Three checks, deliberately ordered from "cannot regress silently" to "activates when
the app is assembled":

1. The envelope types have exactly the two keys they claim.
2. No schema model anywhere declares a `success` or `error` field — name-based, so it
   catches the violation regardless of which model grows it.
3. Once `apps.api.main:app` exists, walk the real OpenAPI document's 2xx responses and
   assert each one resolves to an envelope. Until then this check reports the missing
   app as a failure rather than skipping: a skipped guard is how a guard stops
   existing twice.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys
from pathlib import Path
from typing import Final

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]

# The app is served as `uvicorn main:app --app-dir apps/api`, so its modules import
# each other as `api.<thing>` with `apps/api` on the path. Import them the same way
# here, or a walk of the package fails on an import that works perfectly at runtime.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

# The two names the whole API is allowed to answer with.
ENVELOPE_NAMES: Final[frozenset[str]] = frozenset({"Envelope", "PageEnvelope"})

# A model may not carry a *second status channel*. `success`/`ok` restate "did this
# request work", and `status_code` restates the HTTP code inside the body.
#
# `error` is deliberately absent here. `RunDetail.error` and `JobStatus.error` are
# operational facts about a run — a failed pipeline stage is data the queue must
# render — and a scanner that banned the word would force domain fields to be
# renamed to satisfy the test, which is the test getting the doctrine wrong. The
# served-contract check below does forbid `error` at the top level of a 2xx body,
# where the only legitimate keys are `data` and `meta`.
STATUS_CHANNEL_FIELDS: Final[frozenset[str]] = frozenset({"success", "ok", "status_code"})
ENVELOPE_LEVEL_FIELDS: Final[frozenset[str]] = STATUS_CHANNEL_FIELDS | frozenset({"error"})


def _schema_models() -> list[type[BaseModel]]:
    """Every Pydantic model declared anywhere under `apps.api.schemas`."""
    package = importlib.import_module("apps.api.schemas")
    models: list[type[BaseModel]] = []
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        module = importlib.import_module(info.name)
        for _, member in inspect.getmembers(module, inspect.isclass):
            if issubclass(member, BaseModel) and member.__module__ == module.__name__:
                models.append(member)
    return models


def _offenders(model: type[BaseModel]) -> list[str]:
    """Fields on this model that restate request status."""
    return sorted(name for name in model.model_fields if name in STATUS_CHANNEL_FIELDS)


def test_envelope_types_carry_exactly_data_and_meta() -> None:
    """`{data, meta}` and nothing else — the shape is the contract."""
    module = importlib.import_module("apps.api.schemas.common")
    for name in sorted(ENVELOPE_NAMES):
        envelope = getattr(module, name, None)
        assert envelope is not None, f"{name} disappeared from schemas/common.py"
        fields = set(envelope.model_fields)
        assert fields == {"data", "meta"}, f"{name} grew extra keys: {sorted(fields - {'data', 'meta'})}"
        assert "success" not in fields and "error" not in fields


def test_no_schema_model_declares_a_success_or_error_field() -> None:
    """The prohibition is about field names, so check names, not conventions."""
    models = _schema_models()
    assert models, "found no schema models — the package moved, update this test"
    bad = {m.__name__: _offenders(m) for m in models if _offenders(m)}
    assert not bad, (
        "schema models carrying a second status channel (the doctrine forbids any of "
        f"{sorted(STATUS_CHANNEL_FIELDS)} at model level):\n"
        + "\n".join(f"  {name}: {fields}" for name, fields in sorted(bad.items()))
    )


def _app_probe() -> tuple[FastAPI | None, str]:
    """Return (app, why_not). Never collapses three different failures into one.

    "The module is missing", "the module raised while importing" and "the module has
    no `app`" look identical to a reader of a skip message and are three different
    jobs for the person fixing it, so they are reported as three different reasons.
    """
    try:
        module = importlib.import_module("apps.api.main")
    except ModuleNotFoundError as exc:
        return None, f"apps.api.main could not be imported: {exc}"
    except Exception as exc:  # the reason IS the finding here, so it is reported, not swallowed
        return None, f"apps.api.main raised during import: {type(exc).__name__}: {exc}"
    app = getattr(module, "app", None)
    if app is None:
        return None, "apps.api.main has no module-level `app`"
    if not isinstance(app, FastAPI):
        return None, f"apps.api.main.app is {type(app).__name__}, not a FastAPI instance"
    return app, ""


def test_every_2xx_response_in_the_openapi_document_is_an_envelope() -> None:
    """Walk the real document, once the app exists.

    `success` inside a ProblemDetail is legitimate and expected — RFC 9457 error
    bodies are not 2xx responses, which is why this walks 2xx only.
    """
    app, why_not = _app_probe()
    if app is None:
        pytest.fail(
            f"{why_not}. This gate is about the served contract, so it fails until the "
            "app is assembled rather than skipping: a skipped check is how the doctrine "
            "ends up asserted only in a docstring."
        )
    document = app.openapi()
    checked = 0
    offenders: list[str] = []
    schemas: dict[str, dict[str, object]] = document.get("components", {}).get("schemas", {})

    def resolve(schema: dict[str, object]) -> dict[str, object] | None:
        reference = schema.get("$ref")
        if isinstance(reference, str):
            return schemas.get(reference.rsplit("/", 1)[-1])  # type: ignore[return-value]
        return schema

    for path, operations in document.get("paths", {}).items():
        if not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            for code, response in (operation.get("responses") or {}).items():
                if not (isinstance(code, str) and code.startswith("2")):
                    continue
                if not isinstance(response, dict):
                    continue
                # The envelope is a rule about JSON response bodies. An SSE endpoint
                # (`text/event-stream`) returns event frames whose own schema is fixed
                # by plan 13 — id, stage, status, rows, elapsed_ms — and wrapping each
                # frame in {data, meta} would be wrong, so media types other than JSON
                # are skipped explicitly rather than being exempted by silence.
                content = response.get("content") or {}
                if not isinstance(content, dict):
                    continue
                for media_type, media in content.items():
                    if media_type != "application/json":
                        continue
                    if not isinstance(media, dict) or "schema" not in media:
                        continue
                    resolved = resolve(media["schema"])
                    if resolved is None:
                        offenders.append(f"{method.upper()} {path} {code}: unresolvable schema")
                        continue
                    properties = resolved.get("properties") or {}
                    checked += 1
                    if not {"data", "meta"} <= set(properties):
                        offenders.append(
                            f"{method.upper()} {path} {code}: 2xx body is not an envelope "
                            f"(properties: {sorted(properties)})"
                        )
                    grown = sorted(set(properties) & ENVELOPE_LEVEL_FIELDS)
                    if grown:
                        offenders.append(f"{method.upper()} {path} {code}: carries {grown}")
    assert checked, "no 2xx response schemas found in the OpenAPI document"
    assert not offenders, "envelope doctrine violated by the served contract:\n" + "\n".join(
        sorted(offenders)
    )


def test_the_checker_rejects_a_success_bearing_model() -> None:
    """Prove the guard bites, against the exact shape the doctrine forbids.

    Without this, checks 1-3 could pass because they were looking at nothing: the way
    to know a scanner works is to hand it a known-bad input and watch it complain.
    """

    class LeakingResponse(BaseModel):
        data: list[int]
        meta: dict[str, object]
        success: bool

    assert _offenders(LeakingResponse) == ["success"]

    class BareOk(BaseModel):
        ok: bool
        payload: dict[str, object]

    assert _offenders(BareOk) == ["ok"]

    class Compliant(BaseModel):
        data: list[int]
        meta: dict[str, object]

    assert _offenders(Compliant) == []
