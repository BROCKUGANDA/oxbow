"""DEV-023: the edge is the same-origin seam, so a wrong routing fact is an auth failure.

`apps/web/src/lib/api/transport.ts` sends relative `/api` paths from the page because a
cross-origin POST cannot ride a simple request: Chrome blocks it on the preflight, no demo token
is minted, every later GET answers 401, and the envelope decoder reports that as a contract
failure rather than as wiring. `config/caddy/Caddyfile` now owns that promise, which puts four
facts in a file no other gate reads:

  * both upstreams are named, or one half of the stack is unreachable through the edge;
  * `/api/*` is passed through unstripped, because `apps/api/main.py` mounts its routes under
    `/api` and a `handle_path` would deliver `/runs` to a router expecting `/api/runs`;
  * the run-progress SSE is flushed per frame, because a buffering proxy turns a live run banner
    into one block of text arriving at the end -- which is the thing the demo is about;
  * the port Caddy binds is the port Compose publishes, or the mapped port answers nothing.

Every assertion here runs against comment-stripped text. The first draft asserted on the raw file
and passed by reading the prose that explains why a thing is NOT done -- "Probing /api/* here
would mark the edge unhealthy" tripping the check for a /api probe. A gate that matches its own
comments is a gate that cannot fail.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = REPO_ROOT / "config" / "caddy" / "Caddyfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"

_COMMENT = re.compile(r"^\s*(?:#|//).*$", re.MULTILINE)


def _code(text: str) -> str:
    """Operative lines only: comments are where the reasons live, not the routing."""
    return _COMMENT.sub("", text)


def _caddyfile_code() -> str:
    return _code(CADDYFILE.read_text(encoding="utf-8"))


def _api_route_body(text: str) -> str:
    """The body of the `handle /api/*` block, so route-scoped claims stay route-scoped.

    A whole-file search would let `flush_interval -1` satisfy the SSE check from the web route,
    where it does nothing for the run stream.
    """
    match = re.search(r"handle /api/\* \{(?P<body>.*?)(?:\n\t\}|\n\t\t\})", text, re.DOTALL)
    assert match, (
        "no `handle /api/* { ... }` block was matched -- either the Caddyfile changed shape or "
        "this check stopped recognising the thing it exists to check"
    )
    return match.group("body")


def _caddy_service() -> str:
    """The `caddy:` block of docker-compose.yml, up to the top-level volumes key."""
    text = COMPOSE.read_text(encoding="utf-8")
    assert "  caddy:" in text, "no caddy service in docker-compose.yml"
    return text.split("  caddy:", 1)[1].split("\nvolumes:", 1)[0]


def test_caddyfile_exists_and_is_not_a_stub() -> None:
    assert CADDYFILE.is_file(), "config/caddy/Caddyfile is missing"
    code = _caddyfile_code()
    assert len(code) > 120, "the Caddyfile has no operative routing in it"
    assert "reverse_proxy" in code, "no reverse_proxy: a config file that proxies nothing"


def test_both_upstreams_are_named_and_service_scoped() -> None:
    """`api:8000` and `web:3000`, by compose service name -- never a loopback address.

    Inside the caddy container 127.0.0.1 is the caddy container. The web service's own API origin
    was written that way and pointed at itself, so the failure looked like a broken page rather
    than a broken address.
    """
    code = _caddyfile_code()
    assert re.search(r"reverse_proxy\s+api:8000", code), "no API upstream in the Caddyfile"
    assert re.search(r"reverse_proxy\s+web:3000", code), "no web upstream in the Caddyfile"
    assert "127.0.0.1" not in code, "an upstream addressed through loopback reaches only the proxy"
    assert not re.search(r"reverse_proxy\s+localhost", code), "same, by name"


def test_api_paths_reach_the_api_unstripped() -> None:
    code = _caddyfile_code()
    assert "handle_path" not in code, (
        "`handle_path` strips the matcher prefix, and apps/api/main.py mounts its routes under "
        "/api, so /api/runs would arrive as /runs and answer 404"
    )
    assert "strip_prefix" not in code, "the /api prefix must not be stripped"


def test_the_api_route_flushes_every_frame() -> None:
    """SSE unbuffered on the route that serves it, not merely mentioned in the file.

    apps/api/events.py answers GET /api/runs/{run_id}/events/stream as text/event-stream,
    resumable with Last-Event-ID. Caddy auto-detects that content type today, but auto-detection
    is an implementation detail of a proxy version, and a silent regression there turns the live
    pipeline banner into one block of text at the end of the run.
    """
    assert "flush_interval -1" in _api_route_body(_caddyfile_code()), (
        "the /api route has no `flush_interval -1`, so run-progress frames may be buffered"
    )


def test_the_route_scoped_checks_reject_the_shapes_they_exist_to_catch() -> None:
    """Mutation cases, so a check that matches nothing cannot pass for the wrong reason."""
    buffered = "handle /api/* {\n\t\treverse_proxy api:8000\n\t}\nhandle {\n\t\tflush_interval -1\n\t}\n"
    body = _api_route_body(buffered)
    assert "flush_interval -1" not in body, (
        "the per-route check fell back to reading the whole file, which is the bug it guards"
    )

    stripped = "handle_path /api/* {\n\t\treverse_proxy api:8000\n\t}\n"
    assert "handle_path" in _code(stripped), "the strip detector no longer sees a stripped route"

    loopback = "handle /api/* {\n\t\treverse_proxy 127.0.0.1:8000\n\t}\n"
    assert "127.0.0.1" in loopback, "the loopback detector has no teeth"

    unmatched = "route {\n\treverse_proxy api:8000\n}\n"
    try:
        _api_route_body(unmatched)
    except AssertionError:
        pass
    else:
        raise AssertionError("_api_route_body matched a file with no /api handle block")


def test_compose_publishes_the_port_caddy_binds() -> None:
    """One variable for both halves, or `OXBOW_EDGE_PORT=9080` maps a port nothing listens on."""
    service = _caddy_service()
    assert "{$OXBOW_EDGE_PORT:8080}" in _caddyfile_code(), "the Caddyfile hard-codes its listen port"
    assert "OXBOW_EDGE_PORT: ${OXBOW_EDGE_PORT:-8080}" in service, (
        "the caddy service must pass the same variable the Caddyfile interpolates"
    )
    assert re.search(
        r'ports:\s*\["\$\{OXBOW_EDGE_PORT:-8080\}:\$\{OXBOW_EDGE_PORT:-8080\}"\]', service
    ), "host port and container port must both come from OXBOW_EDGE_PORT"


def test_the_caddyfile_is_mounted_read_only_and_the_image_is_digest_pinned() -> None:
    """02 §F applies to the edge like every other image: tag *and* digest.

    Read-only matters too: a writable mount lets a running container rewrite the routing a
    reviewer is about to audit, and the audit would then describe different bytes than the commit.
    """
    service = _caddy_service()
    assert re.search(r"image:\s*caddy:[^\s@]+@sha256:[0-9a-f]{64}", service), (
        "the caddy image is not pinned by tag and digest"
    )
    assert re.search(r"config/caddy/Caddyfile:/etc/caddy/Caddyfile:ro", service), (
        "the Caddyfile is not mounted, or is mounted writable"
    )


def test_the_edge_healthcheck_does_not_borrow_an_upstreams_health() -> None:
    """A proxy that is up must not be restarted because an upstream is deliberately degraded.

    03 J asks for DEGRADED MODE with a banner naming what is unavailable. If the edge's healthcheck
    fetched an API path, an API down would make the edge unhealthy, compose would restart the one
    component still able to serve the app, and the banner would never get to paint.
    """
    service = _caddy_service()
    assert "healthcheck" in service, "every service in this stack has a healthcheck (03 J)"
    probe = re.search(r"test:\s*(\[.*?\])", service, re.DOTALL)
    assert probe, "the caddy healthcheck has no `test:` entry to inspect"
    assert "2019" in probe.group(1), (
        "the probe should hit Caddy's own admin endpoint, which answers only when a config loaded"
    )
    assert "/api" not in probe.group(1), "the edge's own health must not depend on the API's health"
