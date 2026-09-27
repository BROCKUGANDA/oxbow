"""Schemathesis over the served OpenAPI document — §13's "Schemathesis clean" clause.

Plan §13 asks for two things that no other file in this repository provides. The first is
**generated requests**: every assertion in `tests/integration/test_p7_api.py` fires a request
someone chose, so the API is proven against the shapes its author thought of. Schemathesis
builds requests from the schema instead, which is the only way a router's dependency layer
meets an input nobody designed — a path parameter that is `"-"`, an integer query that arrives
as `2147483648`, a body field sent as an object. The failure it hunts is a router answering
one of those with a 500 or an HTML stack trace, because §13's clause is "a forced failure in
each router returns valid `problem+json` **with a run id**".

The second is **the document as the contract**: the typed error union that
`openapi-typescript` generates the client from exists only if every operation *declares* the
problem schema for its error statuses. A server can emit perfect `application/problem+json`
and still hand the client `any`, because the document never said so. That check needs no
requests at all, and it is the one the generated client actually depends on.

WHAT THIS RUNS AGAINST: the app in its null-adapter mode (`OXBOW_WAREHOUSE=null`), which §13's
day-10 gate requires to work with nothing else up. Requests here are unauthenticated, so the
protected surface answers 401 — and a 401 that is a well-formed problem document is precisely
what is being asserted. The authenticated, seeded-Postgres behaviour of these routes belongs to
`test_p7_api.py`; this file owns the envelope under hostile input and does not imitate that one.

The environment block is deliberately a copy of `test_p7_api.py::_configure_env`'s null-mode
half rather than an import of it: the salt and the two secrets are throwaway literals, and a
property test that cannot boot because a heavy sibling module pulled in a database driver is a
property test that quietly stops running.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import pytest
import schemathesis
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# Served as `uvicorn main:app --app-dir apps/api`, so the packages are `api.*` with `apps`
# on the path. Same bootstrap `tests/integration/test_p7_api.py` uses.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api.main import create_app  # noqa: E402
from api.problems import PROBLEM_MEDIA_TYPE, ProblemDetail  # noqa: E402
from api.schemas.common import ENVELOPE_KEYS  # noqa: E402
from api.settings import reset_settings_cache  # noqa: E402

TEST_SALT = "schemathesis-test-salt-not-the-real-one"
TEST_JWT_SECRET = "schemathesis-test-local-jwt-secret"
TEST_WEBHOOK_SECRET = "schemathesis-test-webhook-secret"

_NULL_MODE_ENV = {
    "RUN_SALT": TEST_SALT,
    "OXBOW_SEED": "1337",
    "OXBOW_LOCAL_JWT_SECRET": TEST_JWT_SECRET,
    "OXBOW_LOCAL_JWT_ENABLED": "true",
    "WEBHOOK_SIGNING_SECRET": TEST_WEBHOOK_SECRET,
    "WEBHOOK_ENDPOINT": "http://127.0.0.1:1/webhook",
    "OXBOW_OIDC_ISSUER": "",
    "OXBOW_OIDC_JWKS_URL": "",
    "OXBOW_S3_ENDPOINT_URL": "",
    "OXBOW_SLACK_WEBHOOK_URL": "",
    "OXBOW_REPO_ROOT": str(REPO_ROOT),
    "OXBOW_LOG_FORMAT": "console",
    "DATABASE_URL": "",
    # The null adapters are the demo default and the only mode that owes nothing to services.
    "OXBOW_WAREHOUSE": "null",
}


def _install_null_mode() -> None:
    for name, value in _NULL_MODE_ENV.items():
        os.environ[name] = value
    reset_settings_cache()


def _tear_down_null_mode() -> None:
    for name in _NULL_MODE_ENV:
        os.environ.pop(name, None)
    reset_settings_cache()


# The schema is built once, from the app that is about to be called, so the document under
# test is the document that would be served. `from_dict` rather than `from_asgi` because the
# app must already be in null mode when FastAPI freezes its schema.
#
# Open API 3.1 is why the experiment flag is here: FastAPI serves 3.1, and schemathesis 3.39
# refuses a 3.1 document unless the 3.1 experiment is enabled. The other way to get past the
# error is `force_schema_version="30"`, which reads a 3.1 schema as 3.0 and then generates
# requests from types it has misinterpreted — the wrong direction, because this file's whole
# point is that the document is the contract.
_install_null_mode()
schemathesis.experimental.OPEN_API_3_1.enable()
try:
    _APP = create_app()
    with TestClient(_APP) as _client:
        _DOCUMENT = _client.get("/openapi.json").json()
    SCHEMA = schemathesis.from_dict(_DOCUMENT, app=_APP, validate_schema=False)
    _BOOT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - a schema that will not build is a failure, not a skip
    SCHEMA = None
    _BOOT_ERROR = exc


@pytest.fixture(scope="module", autouse=True)
def _null_adapters_only() -> Any:
    if _BOOT_ERROR is not None:
        pytest.fail(
            f"the app could not be built in null-adapter mode for property testing: "
            f"{type(_BOOT_ERROR).__name__}: {_BOOT_ERROR}. §13's day-10 gate is that the null "
            "adapters run with nothing else up, so this is a result, not an environment note."
        )
    yield
    _tear_down_null_mode()


def _operations() -> list[dict[str, Any]]:
    """Every operation in the served document, flattened to the fields both checks need.

    Read off the raw document rather than schemathesis's parsed operations: the file is
    asserting what a client generator will see, and that is the unparse-assembled JSON.
    """
    found: list[dict[str, Any]] = []
    for path, item in (_DOCUMENT.get("paths") or {}).items():
        for method, operation in item.items():
            if method in {"parameters", "servers", "summary", "description"}:
                continue
            found.append(
                {
                    "path": path,
                    "method": method.upper(),
                    "responses": operation.get("responses") or {},
                }
            )
    return found


def test_every_operation_documents_its_errors_as_problem_json() -> None:
    """§13: the error schema is *present in the OpenAPI document*, not merely emitted.

    `openapi-typescript` turns a documented response model into a named type and an
    undocumented one into `any`. The gate is "a typed error union rather than `any`", so the
    claim is checked where the generator reads it.
    """
    offenders: list[str] = []
    for item in _operations():
        where = f"{item['method']} {item['path']}"
        responses = item["responses"]
        error_statuses = [code for code in responses if code[0] in "45"]
        if not error_statuses:
            offenders.append(f"{where}: documents no 4xx/5xx response at all")
            continue
        for code in error_statuses:
            content = (responses.get(code) or {}).get("content") or {}
            if PROBLEM_MEDIA_TYPE not in content:
                offenders.append(f"{where} {code}: no {PROBLEM_MEDIA_TYPE}")
                continue
            ref = str(content.get(PROBLEM_MEDIA_TYPE, {}).get("schema", {}).get("$ref", ""))
            if "ProblemDetail" not in ref:
                offenders.append(f"{where} {code}: {ref or 'an inline schema'}")

    assert not offenders, (
        f"{len(offenders)} operation(s) do not reference ProblemDetail under "
        f"{PROBLEM_MEDIA_TYPE} in the served document: {offenders[:8]}. A client generated "
        "against that document types the error branch as `any`, which is the outcome plan §13's "
        "gate names as the failure — the server can be correct and the consumer still blind."
    )


def test_the_document_is_the_one_the_app_serves() -> None:
    """Guard against the property suite drifting from the app it claims to describe.

    A schema built once and hand-copied into a fixture would keep passing after a router lost
    an endpoint. The count is read from the live app, so the suite has to be looking at this
    build.
    """
    with TestClient(_APP) as client:
        served = client.get("/openapi.json").json()

    assert _DOCUMENT is not None
    assert json.dumps(served, sort_keys=True) == json.dumps(_DOCUMENT, sort_keys=True), (
        "the document this file generated cases from is not the document the app serves now; "
        "whatever passed below passed against a stale contract"
    )


def _assert_problem_body(response: Any) -> None:
    """Every non-2xx answer is an RFC 9457 document this API's own model accepts.

    `ProblemDetail` is declared `extra="forbid"`, so validating through it also refuses a body
    that smuggles fields in beside the contract — which is how a `success: true` flag would
    find its way back into an API the envelope doctrine has banned it from.
    """
    assert response.status_code >= 400, "only error responses carry a problem document"
    assert response.headers.get("content-type", "").startswith(PROBLEM_MEDIA_TYPE), (
        f"a {response.status_code} came back as {response.headers.get('content-type')!r}; "
        "an error the client cannot parse as a problem is an error the UI renders as an empty "
        "state, which §18 lists as fatal"
    )
    body = response.json()
    problem = ProblemDetail.model_validate(body)
    assert problem.status == response.status_code, (
        f"the document says {problem.status} and the transport says {response.status_code}: "
        "the status is the machine-readable half of the contract and the two must agree"
    )
    # `request.url` is a string on the transport schemathesis uses here, so the path is
    # parsed rather than reached for. BOTH sides then go through one normal form, because
    # they disagree for reasons that are not the API's: the transport records the request
    # target as it built it, Starlette reports the path it actually routed, `_instance_of`
    # re-encodes that as a URI reference, and a raw control character in a request target is
    # not legal to send at all (a raw LF even terminates the request line, so the server
    # never sees it while the client's record still holds it). Normalising both sides —
    # decode, drop control characters, re-encode — keeps this assertion about the thing it
    # guards: a problem document naming some OTHER route than the one asked about.
    requested = _wire_form(urlsplit(str(response.request.url)).path)
    observed = None if problem.instance is None else _wire_form(problem.instance)
    assert observed in (None, requested), (
        "`instance` is defined as the request URI that produced the problem; naming another "
        f"path ({problem.instance!r} -> {observed!r} for {requested!r}) makes an investigation "
        "impossible"
    )
    if 400 <= response.status_code < 500:
        assert problem.retryable is False, (
            "a 4xx marked retryable instructs every honest client to hammer a permanent "
            "failure — plan §18's retry-loop-around-a-4xx trigger"
        )


def _documented_success(response: Any) -> None:
    """A 2xx carries the operational envelope: `data` + `meta`, and never a `success` flag."""
    body = response.json()
    assert isinstance(body, dict), f"a 2xx returned {type(body).__name__}, not an envelope"
    assert set(ENVELOPE_KEYS) <= set(body), (
        f"a 2xx from {response.request.method} {response.request.url.path} returned "
        f"{sorted(body)}; the envelope the client is generated from is {sorted(ENVELOPE_KEYS)}"
    )
    assert (
        "success" not in body
    ), "the `success` flag is banned by the envelope doctrine: status lives in the HTTP code"


# The property half. Scoped to read-only operations: generating bodies for the decision
# endpoints would exercise the same validation path twice while writing rows under a random
# seed, and §13's clause is about the envelope, not about allocation. `GET` still covers every
# path/query/header shape the document declares, including the 401 branch on all of them.
def _wire_form(path: str) -> str:
    """Decode, drop control characters, cut at the query or fragment, re-encode.

    The cut is not a relaxation, it is RFC 3986: a generated path parameter containing `?`
    or `#` starts the query or the fragment at that byte, and nothing after it is ever sent
    to the server. Without it this assertion faults the API for answering about the path it
    was actually given — measured here, where `/api/cases/<id>%3F/decisions` reaches the
    server as `/api/cases/<id>` and the problem document says so correctly.
    """
    decoded = re.sub(r"[\x00-\x1f\x7f]", "", unquote(path))
    return quote(re.split(r"[?#]", decoded, maxsplit=1)[0], safe="/")


def _unencodable_on_the_wire(error: BaseException) -> bool:
    """True only when every leaf of this exception is a header-encoding failure.

    RFC 9110 restricts field values to ASCII/latin-1, and so does the ASGI test transport:
    a generated header carrying U+0100 is not a request any client can send, so the server
    never sees it. Starlette raises that through a task group, which arrives here as an
    ExceptionGroup, so the leaves are walked rather than the top node inspected. Anything
    else — including a server-side 500 — is re-raised untouched: a property suite that
    swallows a broad exception to look green is the failure mode §18 names.
    """
    stack: list[BaseException] = [error]
    leaves: list[BaseException] = []
    while stack:
        current = stack.pop()
        children = list(getattr(current, "exceptions", ()) or ())
        if children:
            stack.extend(children)
        else:
            leaves.append(current)
    return bool(leaves) and all(isinstance(leaf, UnicodeEncodeError) for leaf in leaves)


def _envelope_of(case: Any) -> None:
    try:
        response = case.call()
    except BaseException as error:  # re-raised below unless it is only a wire-encoding fault
        if not _unencodable_on_the_wire(error):
            raise
        pytest.skip("generated request cannot be encoded as an HTTP header (latin-1 on the wire)")

    assert response.status_code < 500, (
        f"{case.formatted_path} answered {response.status_code} to a schema-generated request: "
        "an unhandled exception in the dependency or read-model layer, which the UI can only "
        "render as a blank pane"
    )
    if response.status_code >= 400:
        _assert_problem_body(response)
    else:
        _documented_success(response)


if SCHEMA is None:  # pragma: no cover - only when the app will not boot

    def test_the_app_never_built() -> None:
        pytest.fail(
            f"schemathesis has no schema to generate from because the app would not build in "
            f"null-adapter mode: {type(_BOOT_ERROR).__name__}: {_BOOT_ERROR}"
        )

    def test_generated_request_never_breaks_the_envelope() -> None:
        pytest.fail("the property half of this file never ran: see test_the_app_never_built")
else:

    @SCHEMA.parametrize(endpoint=r"^/api/")
    @settings(
        max_examples=12,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_generated_request_never_breaks_the_envelope(case: Any) -> None:
        """No generated input shape produces a 500, an HTML error page, or an undocumented body."""
        if str(case.method).upper() != "GET":
            pytest.skip(
                "mutating operations are covered by test_p7_api.py against a real warehouse"
            )
        _envelope_of(case)
