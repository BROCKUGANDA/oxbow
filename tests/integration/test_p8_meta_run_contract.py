"""``GET /api/meta/run``, and the problem document for a path that has no route.

Both halves of this file came out of one measured failure. The web app's live mode died on
its first API call because ``/api/meta/run`` had never been implemented, and the answer it
got was:

    {"type":"https://oxbow.dev/problems/bad-request","title":"Bad request","status":400,
     "detail":"Not Found", ...}

Two defects in one body. The route was missing, and a missing route was reported as a
*malformed request*: a 400 tells an operator to fix the client when the truth is that the
server has no such path, which is a deployment fact. ``apps/api/problems.py`` now carries
that mapping, and the last two tests here pin it.

WHY THE FIRST TEST READS THE CLIENT'S SOURCE FILE. ``apps/web/src/lib/api/contract.ts`` is
the contract: ``RuntimeMetaDecoder`` names the ten fields ``Shell.tsx``, the queue and the
provenance panes are allowed to read, and its ``object`` decoder drops anything else. A
Python-side list of "the fields the client wants", typed by hand into this file, would go
stale the first time the client edited its decoder and would then pass against a route that
had stopped matching. So the field list and each field's decoder are parsed out of
``contract.ts`` here and the served body is checked against what the parse found. The
literal copy below is deliberate: it fails when the client changes, which is the moment
someone has to decide whether the route changes with it.

WHAT THESE TESTS REFUSE TO ACCEPT FROM THE SERVER. A nullable field padded to satisfy a
shape — an empty string where a licence belongs, a zero where a seed belongs, an invented
run id — is the failure this route exists to avoid, so the empty-warehouse test asserts the
null *and* the ``degradations`` entry naming the artifact the value would come from. Money
stays integer minor units, and every list served here has one total deterministic order.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]

# Served as `uvicorn main:app --app-dir apps/api`, so the packages are `api.*` with `apps`
# on the path — the same bootstrap tests/integration/test_p7_api.py uses.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api.deps import Container, build_container  # noqa: E402
from api.main import create_app  # noqa: E402
from api.problems import PROBLEM_MEDIA_TYPE, PROBLEM_TYPE_BASE  # noqa: E402
from api.settings import reset_settings_cache  # noqa: E402

CONTRACT_FILE: Final = REPO_ROOT / "apps" / "web" / "src" / "lib" / "api" / "contract.ts"
RUN_ROUTE: Final = "/api/meta/run"

# Explicit test secrets. The repository's own RUN_SALT is gitignored, is never read here,
# and is never printed by anything in this file.
TEST_RUN_SALT: Final = "p8-meta-run-salt-not-the-real-one"
TEST_JWT_SECRET: Final = "p8-meta-run-local-jwt-secret"
TEST_WEBHOOK_SECRET: Final = "p8-meta-run-webhook-secret"

ANALYST: Final = "analyst@oxbow.dev"

# The decoder as it stands in contract.ts. If the client adds, removes or re-kinds a
# field, this fails and says which one: that is the drift the route has to follow or
# argue with, and a contract test that never notices a change is not a contract test.
RUNTIME_META_DECODER: Final = {
    "run_id": "nullable(string)",
    "deployment_timezone": "string",
    "currency": "string",
    "minor_units_per_major": "integer",
    "economics_source": "string",
    "economics": "record(scalarOrNumber)",
    "dataset": "nullable(string)",
    "licence": "nullable(string)",
    "model_version": "nullable(string)",
    "demo_data": "boolean",
}

# The subset of `MetaDecoder` whose decoders are written inline, so they are checkable
# without resolving a name to a decoder declared elsewhere in the file.
META_INLINE_DECODER: Final = {
    "run_id": "nullable(string)",
    "trace_id": "nullable(string)",
    "model_version": "nullable(string)",
    "provenance": "nullable(string)",
    "generated_at": "nullable(string)",
    "degraded": "boolean",
    "degraded_reason": "nullable(string)",
    "disclaimer": "string",
}

_OBJECT_BLOCK: Final = re.compile(
    r"export const (?P<name>\w+)\s*:\s*Decoder<[^>]+>\s*=\s*object\(\s*'(?P<kind>[^']+)'"
    r"\s*,\s*\{(?P<body>.*?)\n\}\);",
    re.DOTALL,
)


def decoder_fields(name: str) -> dict[str, str]:
    """``{field: decoder-expression}`` for one ``object(...)`` decoder in contract.ts.

    An unparseable line inside the block is a failure rather than a skip: a contract check
    that silently reads fewer fields than the client declares looks like coverage and is
    worse than none.
    """
    text = CONTRACT_FILE.read_text(encoding="utf-8")
    for match in _OBJECT_BLOCK.finditer(text):
        if match.group("name") != name:
            continue
        fields: dict[str, str] = {}
        for line in match.group("body").splitlines():
            stripped = line.strip().rstrip(",")
            if not stripped or stripped.startswith(("//", "*", "/*")):
                continue
            key, separator, decoder = stripped.partition(":")
            key, decoder = key.strip(), decoder.strip()
            if not separator or not re.fullmatch(r"\w+", key):
                raise AssertionError(
                    f"{name}: could not parse {line!r} out of {CONTRACT_FILE.name}. Fix the "
                    "parser or the client file — do not let this read fewer fields."
                )
            fields[key] = decoder
        assert fields, f"{name} parsed to zero fields"
        return fields
    raise AssertionError(f"{name} is gone from {CONTRACT_FILE}; this test names the contract")


def assert_decodes(value: Any, decoder: str, path: str) -> None:
    """The Python reading of the client's decoder, for the kinds it uses inline.

    ``nullable`` on the client accepts a null *or* an absent key; this accepts the first
    and refuses the second, because a route that drops the field in one branch and sends
    null in another has made the payload's shape branch-dependent. The null arm is what
    ``nullable`` is for.
    """
    if decoder.startswith("nullable(") and decoder.endswith(")"):
        if value is None:
            return
        assert_decodes(value, decoder[len("nullable(") : -1], path)
        return
    if decoder == "string":
        assert isinstance(
            value, str
        ), f"{path}: the client decodes string, the server sent {type(value).__name__}"
        assert value.strip(), f"{path}: an empty string is a padded field, not a value"
        return
    if decoder == "integer":
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"{path}: the client decodes integer (Number.isSafeInteger), the server sent "
            f"{type(value).__name__} {value!r} — a float minor unit is DEV-005"
        )
        return
    if decoder == "boolean":
        assert isinstance(value, bool), f"{path}: the client decodes boolean, got {value!r}"
        return
    if decoder == "record(scalarOrNumber)":
        assert isinstance(
            value, dict
        ), f"{path}: the client decodes an object, the server sent {value!r}"
        for key, item in value.items():
            assert isinstance(item, str) or (
                isinstance(item, int | float) and not isinstance(item, bool)
            ), f"{path}.{key}: {item!r} is neither a string nor a number"
        return
    if decoder.startswith("array("):
        assert isinstance(value, list), f"{path}: the client decodes an array, got {value!r}"
        return
    raise AssertionError(
        f"{path}: this checker does not know the decoder {decoder!r}; extend the checker "
        "rather than skipping the field"
    )


def _configure(monkeypatch: pytest.MonkeyPatch, repo_root: Path | None) -> None:
    """One environment, built explicitly. ``OXBOW_REPO_ROOT`` is the only lever the tests
    pull: the null warehouse root is derived from it (``resolve_out_root(repo_root/out)``
    in ``api.deps``), and so is where the route looks for ``data/interim``.
    """
    env = {
        "RUN_SALT": TEST_RUN_SALT,
        "OXBOW_SEED": "1337",
        "OXBOW_LOCAL_JWT_SECRET": TEST_JWT_SECRET,
        "OXBOW_LOCAL_JWT_ENABLED": "true",
        "WEBHOOK_SIGNING_SECRET": TEST_WEBHOOK_SECRET,
        "WEBHOOK_ENDPOINT": "http://127.0.0.1:1/webhook",
        "OXBOW_OIDC_ISSUER": "",
        "OXBOW_OIDC_JWKS_URL": "",
        "OXBOW_S3_ENDPOINT_URL": "",
        "OXBOW_SLACK_WEBHOOK_URL": "",
        "OXBOW_REPO_ROOT": str(repo_root or REPO_ROOT),
        "OXBOW_LOG_FORMAT": "console",
        "DATABASE_URL": "",
        "OXBOW_WAREHOUSE": "null",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()


def _client(monkeypatch: pytest.MonkeyPatch, repo_root: Path | None) -> Iterator[TestClient]:
    """The real app on the null-file warehouse, over a container built for this environment.

    The lifespan builds its own container from the ambient environment, so the one this
    file configured is installed after startup — the order tests/integration/test_p7_api.py
    uses, for the same reason.
    """
    _configure(monkeypatch, repo_root)
    container: Container = build_container()
    assert (
        container.backend == "null-file"
    ), f"expected the null-file warehouse, got {container.backend}"
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.container = container
        yield client
    container.close()
    reset_settings_cache()


@pytest.fixture()
def null_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The deployment as this checkout actually is: the file warehouse under ``out/``."""
    yield from _client(monkeypatch, None)


@pytest.fixture()
def empty_deployment_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    """The same app over a repository root with no runs and no ingested corpus.

    The two config files the route reads are copied in, because they are what makes this
    a deployment rather than an empty directory, and the route's config half is still
    supposed to answer. What is absent is ``out/`` (no run row) and ``data/interim`` (no
    ingest manifest) — the state the nullable arms of the decoder exist for. Without this
    case the route could pass by always having a run row and two manifests available on a
    developer's machine.
    """
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    for name in ("economics.yaml", "pipeline.yaml"):
        (tmp_path / "config" / name).write_bytes((REPO_ROOT / "config" / name).read_bytes())
    yield from _client(monkeypatch, tmp_path)


def _token(client: TestClient) -> dict[str, str]:
    """Mint through the real demo-token route, not by calling ``mint_local_token`` directly."""
    response = client.post(
        "/api/auth/demo-token",
        json={"subject": ANALYST, "roles": ["analyst"], "display_name": ANALYST},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _run_payload(client: TestClient) -> dict[str, Any]:
    """A 200 whose body is an envelope, or a failure that says what arrived instead."""
    response = client.get(RUN_ROUTE, headers=_token(client))
    assert (
        response.status_code == 200
    ), f"{RUN_ROUTE} answered {response.status_code}: {response.text[:400]}"
    body = response.json()
    assert set(body) == {
        "data",
        "meta",
    }, f"the envelope is {{data, meta}} and nothing else; the route served {sorted(body)}"
    for name in ("success", "error", "status_code"):
        assert name not in body, f"a second status channel ({name}) in a 2xx body"
    return body


def _assert_problem(response: Any, *, status: int, where: str) -> dict[str, Any]:
    assert (
        response.status_code == status
    ), f"{where}: expected {status}, got {response.status_code} {response.text[:300]}"
    assert response.headers["content-type"].startswith(
        PROBLEM_MEDIA_TYPE
    ), f"{where}: {response.headers.get('content-type')!r} is not {PROBLEM_MEDIA_TYPE!r}"
    body = response.json()
    for name in ("type", "title", "status"):
        assert name in body, f"{where}: RFC 9457 member {name} missing from {body}"
    assert body["status"] == status, f"{where}: body status {body['status']} != HTTP {status}"
    assert body["detail"], f"{where}: an empty detail explains nothing"
    assert str(body["instance"]).startswith("/"), f"{where}: instance {body['instance']!r}"
    assert body["trace_id"], f"{where}: no trace id to find the log line by"
    return body


# ------------------------------------------------- the client's field list -----


def test_the_field_list_this_route_is_checked_against_is_the_clients_own() -> None:
    """Parse ``contract.ts`` and pin what it currently says."""
    assert decoder_fields("RuntimeMetaDecoder") == RUNTIME_META_DECODER
    meta_fields = decoder_fields("MetaDecoder")
    inline = {key: meta_fields[key] for key in sorted(META_INLINE_DECODER)}
    assert (
        inline == META_INLINE_DECODER
    ), f"meta's inline decoders moved, so this checker's copy is wrong: {inline}"


def test_served_run_envelope_decodes_field_by_field(null_client: TestClient) -> None:
    """Every field the client names arrives with the type its decoder requires.

    This is the assertion the P8 spec failed: the route existed only in the client, so in
    live mode nothing decoded at all. It checks served JSON rather than a Python model, so
    a route that builds the right dict and then loses a key on the way out still fails.
    """
    fields = decoder_fields("RuntimeMetaDecoder")
    payload = _run_payload(null_client)
    data = payload["data"]
    missing = sorted(set(fields) - set(data))
    assert not missing, f"the client names {missing}; the response does not carry them"
    for name, decoder in fields.items():
        assert_decodes(data[name], decoder, f"data.{name}")

    meta = payload["meta"]
    for name, decoder in META_INLINE_DECODER.items():
        assert name in meta, f"meta.{name} is absent, and every response carries it"
        assert_decodes(meta[name], decoder, f"meta.{name}")


# --------------------------------------- the values are the server's own -------


def test_every_served_value_traces_to_something_the_server_holds(null_client: TestClient) -> None:
    """No field is a placeholder: each equals the artifact it is read from.

    The run id, seed, config hash, model version and timezone are compared against the
    ``run`` row; the licence join against what each corpus's ingest manifest recorded; the
    currency and its minor-unit scale against ``config/economics.yaml``. A route that
    started inventing any of them fails here on the value, not on its type.
    """
    data = _run_payload(null_client)["data"]
    container: Container = null_client.app.state.container  # type: ignore[attr-defined]
    run = container.read_model.resolve_run(None, state="complete")
    economics = container.settings.economics()

    assert data["run_id"] == run["run_id"]
    assert data["seed"] == run["seed"]
    assert data["config_hash"] == run["config_hash"]
    assert data["model_version"] == run["model_version"]
    assert data["deployment_timezone"] == run["timezone"]
    assert data["timezone_source"] == "run record: run.timezone"
    assert data["provenance"] == run["provenance"]
    assert data["run_state"] == run["state"]
    assert data["demo_data"] is (run["provenance"] != "pipeline")

    assert data["currency"] == economics["currency"]
    assert data["minor_units_per_major"] == economics["minor_units_per_major"]
    assert data["economics_source"] == "config/economics.yaml"
    assert data["economics"]["recovery.rate"] == economics["recovery"]["rate"]
    assert (
        data["economics"]["analyst.cost_per_minute_minor"]
        == economics["analyst"]["cost_per_minute_minor"]
    )
    assert data["economics"]["recovery.sensitivity_band"] == ", ".join(
        str(item) for item in economics["recovery"]["sensitivity_band"]
    )

    recorded = [(card["source_id"], card["name"], card["licence"]) for card in data["sources"]]
    assert recorded == sorted(recorded), f"sources must ascend by source_id, got {recorded}"
    assert recorded, "the file warehouse has ingested corpora, so the join must not be null"
    assert data["dataset"] == " · ".join(name for _, name, _ in recorded)
    assert data["licence"] == " · ".join(dict.fromkeys(licence for _, _, licence in recorded))
    interim = REPO_ROOT / "data" / "interim"
    for source_id, name, licence in recorded:
        manifest = json.loads(
            (interim / source_id / "run_manifest.json").read_text(encoding="utf-8")
        )
        assert (
            manifest["source"]["license"] == licence
        ), f"{source_id}: the served licence is not the one ingest recorded"
        assert manifest["source"]["name"] == name, f"{source_id}: the name is not the manifest's"


def test_money_keys_stay_integer_minor_units(null_client: TestClient) -> None:
    """``*_minor`` is a count of minor units: an int, never a float, never a string.

    ``_economics_scalars`` flattens on purpose, and DEV-005 is the rule a flattening can
    break — an amount rendered through a float is the defect the AST gate exists for.
    """
    data = _run_payload(null_client)["data"]
    money_keys = sorted(key for key in data["economics"] if key.endswith("_minor"))
    assert money_keys, "config/economics.yaml declares no amount, so this check is vacuous"
    for key in money_keys:
        value = data["economics"][key]
        assert isinstance(value, int) and not isinstance(
            value, bool
        ), f"economics.{key} = {value!r} is not an integer minor unit"


def test_every_served_list_has_one_total_order(null_client: TestClient) -> None:
    """Determinism: nothing here rides on dict or set iteration order.

    The route is asked twice because dict insertion order is stable within a process,
    which is precisely what lets an unordered payload pass a single-request test. Each
    order must also equal the sort of its own key, so "stable" cannot be satisfied by
    whatever order the walk happened to produce.
    """
    first = _run_payload(null_client)["data"]
    second = _run_payload(null_client)["data"]
    assert list(first["economics"]) == list(second["economics"]) == sorted(first["economics"])
    assert [card["source_id"] for card in first["sources"]] == sorted(
        card["source_id"] for card in first["sources"]
    )
    for card in first["sources"]:
        names = [item["file_name"] for item in card["files"]]
        assert names == sorted(names), f"{card['source_id']}: files in artifact order {names}"
    fields = [entry["field"] for entry in first["degradations"]]
    assert fields == sorted(fields), f"degradations in build order {fields}"


# ------------------------------------------------------- the degraded half -----


def test_an_empty_deployment_says_so_instead_of_padding(
    empty_deployment_client: TestClient,
) -> None:
    """Nulls the client can render, each with the artifact the value would come from.

    DESIGN.md §5 forbids an indefinite content spinner and requires degraded-not-broken.
    The client's arms are ``run_id: null`` → "No run_id: this failed before the request
    reached the pipeline", and ``dataset: null`` → "no dataset reported". Those have to be
    reachable from a real server. ``deployment_timezone`` is non-nullable in the decoder,
    so it is asserted as a value here rather than excused — the zone a timestamp would be
    rendered in always exists, because config/pipeline.yaml declares it and ingest refuses
    to run without it.
    """
    payload = _run_payload(empty_deployment_client)
    data = payload["data"]
    # Checked first, because it is the claim the rest of this test only refines: the
    # payload a degraded deployment serves must still decode. A field padded to keep a
    # shape alive ("" where a licence belongs) fails here rather than downstream.
    for name, decoder in decoder_fields("RuntimeMetaDecoder").items():
        assert_decodes(data[name], decoder, f"data.{name}")
    for name in ("run_id", "model_version", "config_hash", "seed", "provenance", "run_state"):
        assert data[name] is None, f"{name} = {data[name]!r} with no run in the warehouse"
    assert data["artifact_hashes"] is None, "an empty map would be an invented record"
    assert (
        data["dataset"] is None and data["licence"] is None
    ), "nothing was ingested here, so neither string may be composed from the declarations"
    assert data["sources"] == []
    assert data["deployment_timezone"], "the zone is non-nullable: config/pipeline.yaml supplies it"
    assert data["timezone_source"] == "config/pipeline.yaml: deployment_timezone"
    assert data["demo_data"] is False, "with no run nothing is served, so nothing is demo bytes"
    # The half the deployment *does* hold still answers, which is the difference between
    # degraded and broken: this is a 200 with real config, not an error surface.
    assert data["currency"] == "UGX" and data["minor_units_per_major"] == 100
    assert data["economics"]["analyst.cost_per_hour_minor"] == 900_000

    named = {entry["field"] for entry in data["degradations"]}
    for name in ("run_id", "model_version", "config_hash", "seed", "provenance"):
        assert name in named, f"{name} is null with no degradation naming it: {sorted(named)}"
    assert {"dataset", "licence", "sources"} <= named, sorted(named)
    for entry in data["degradations"]:
        assert entry["would_come_from"], f"{entry['field']}: the entry names no artifact"
        assert len(entry["reason"]) > 30, f"{entry['field']}: the reason is a stub"
    assert payload["meta"]["run_id"] is None
    assert payload["meta"]["provenance"] == "deployment"


def test_a_named_run_that_does_not_exist_is_a_404_not_a_quiet_default(
    null_client: TestClient,
) -> None:
    """``?run_id`` is a question about one thing, and the answer when it is absent is 404.

    The default lookup degrades; a named one must not, or a mistyped id would render the
    newest run's licence and model version under the id the analyst actually asked about.
    """
    response = null_client.get(RUN_ROUTE, params={"run_id": "0" * 26}, headers=_token(null_client))
    body = _assert_problem(response, status=404, where="GET /api/meta/run?run_id=<unknown>")
    assert body["type"] == f"{PROBLEM_TYPE_BASE}run-not-found", body["type"]
    assert body["title"] == "Run not found", body["title"]
    assert "0" * 26 in body["detail"], body["detail"]


def test_a_named_run_that_exists_is_the_one_described(null_client: TestClient) -> None:
    """The route answers for the run named, not for the newest one, when asked."""
    container: Container = null_client.app.state.container  # type: ignore[attr-defined]
    rows, _ = container.read_model.source.select("run", where={}, limit=None)
    newest = _run_payload(null_client)["data"]
    other = next(
        (
            row
            for row in sorted(rows, key=lambda item: str(item["run_id"]), reverse=True)
            if row["run_id"] != newest["run_id"]
        ),
        None,
    )
    assert other is not None, "the file warehouse holds one run only, so this is unexercised"
    response = null_client.get(
        RUN_ROUTE, params={"run_id": other["run_id"]}, headers=_token(null_client)
    )
    assert response.status_code == 200, response.text
    served = response.json()["data"]
    assert served["run_id"] == other["run_id"]
    assert served["config_hash"] == other["config_hash"]
    assert served["model_version"] == other["model_version"]
    assert served["run_state"] == other["state"]


# --------------------------------------------------------- the 404 shape -------


def test_an_unregistered_path_answers_404_problem_json(null_client: TestClient) -> None:
    """No route is a 404. A malformed request is what a 400 is for.

    This asserted nothing before, and the live P8 run answered the missing
    ``/api/meta/run`` with a 400 whose detail was the string "Not Found" — the body the
    ticket was filed against. Status, title, type URI and detail all had to move
    together: a document that says "Bad request" over a 404 is the same misdiagnosis with
    a different number, and the misdiagnosis is the cost.
    """
    response = null_client.get("/api/no-such-route-here")
    body = _assert_problem(response, status=404, where="an unregistered path")
    assert body["type"] == f"{PROBLEM_TYPE_BASE}not-found", body["type"]
    assert body["title"] == "Not found", body["title"]
    assert "no route registered" in body["detail"], body["detail"]
    assert "/api/no-such-route-here" in body["detail"], body["detail"]
    assert body["retryable"] is False, "a missing route does not appear because one retried"
    assert "run_id" not in body, "no run was involved; even a null run id is a claim here"

    authorised = null_client.get("/api/also/not/a/route", headers=_token(null_client))
    assert authorised.status_code == 404, (
        "authenticating must not turn a routing miss into a different diagnosis: "
        f"{authorised.text[:200]}"
    )

    wrong_method = null_client.post(RUN_ROUTE, headers=_token(null_client))
    body = _assert_problem(wrong_method, status=405, where="POST to a GET-only route")
    assert body["title"] == "Method Not Allowed", body["title"]
    assert body["type"] == f"{PROBLEM_TYPE_BASE}method-not-allowed", body["type"]


def test_a_genuine_bad_request_is_still_a_400(null_client: TestClient) -> None:
    """The fix must not swallow the tier it replaced.

    ``/api/runs?sort=`` refuses an unsortable column with a 400 naming the allowed set.
    If the mapping had become "anything unfamiliar is a 404", that honest diagnosis would
    be the thing that broke, and an operator would go looking for a missing route.
    """
    response = null_client.get(
        "/api/runs", params={"sort": "not_a_column"}, headers=_token(null_client)
    )
    body = _assert_problem(response, status=400, where="an unsortable sort column")
    assert body["type"] == f"{PROBLEM_TYPE_BASE}bad-request", body["type"]
    assert "not_a_column" in body["detail"], body["detail"]
