"""P7 — ``GET /api/graph/subgraph``: what one request costs, and what it may claim.

Three properties of that route are pinned here, all of them against the real ASGI app on
the real ``null-file`` backend. The warehouse is synthetic and lives in a temp directory,
because the repository's own ``out/warehouse`` holds no per-run graph tables at all —
``graph_edge`` has zero rows in every backend of this checkout, which is precisely why the
route has never been executed against a populated one and each defect below survived.

1. **Cost.** The route renders at most ``graph.subgraph_node_cap`` nodes, so it must not
   read every account the run scored. Every read is counted through a wrapper on the read
   model's own ``select``: a row count, not a wall clock, because a timing gate on a shared
   machine measures the other tenants. The edge table is counted too and deliberately not
   gated as a fix — hop 2 cannot be computed from hop 1's rows, and that seam is
   ``ReadModel.subgraph`` in ``apps/api/readmodel.py``, outside this route's fence. Its size
   is asserted below as a measurement.

2. **The cap.** ``truncated=True`` alongside ``len(nodes) > node_cap`` is a response
   reporting a ceiling it did not apply. The collapse seeded the inclusion set with the
   seed's entire community without testing the cap against it.

3. **Money.** ``Money.decimals`` is the EXPONENT whose base is config's
   ``minor_units_per_major`` (100); both ``apps/api/schemas/common.py`` and
   ``apps/web/src/lib/format/money.ts`` compute ``10 ** decimals``. A route that hands the
   base to that field divides every figure it serves by 10**100. And a currency
   ``community`` does not carry is not UGX-by-default: it is a refusal.

Money figures are asserted on the served body and against the composed container, never on
the helper that computes the scale — the first version of the 10**100 guard called the
converter, stayed green, and missed the defect it was written for.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]

# Served as `uvicorn main:app --app-dir apps/api`, so `api.*` needs `apps/api` on the path.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api.deps import build_container  # noqa: E402
from api.main import create_app  # noqa: E402
from api.problems import PROBLEM_MEDIA_TYPE  # noqa: E402
from api.readmodel import FileWarehouseSource, ReadModel  # noqa: E402
from api.settings import reset_settings_cache  # noqa: E402

from oxbow.adapters.warehouse.postgres import new_run_id  # noqa: E402
from oxbow.config import load_yaml  # noqa: E402
from oxbow.ports.warehouse import is_ulid  # noqa: E402

TEST_JWT_SECRET = "graph-route-test-local-jwt-secret"
TEST_RUN_SALT = "graph-route-test-salt-not-the-real-one"

# The policy under test is read from config rather than repeated as a literal the config
# could silently disagree with.
NODE_CAP: int = int(load_yaml(REPO_ROOT / "config" / "pipeline.yaml")["graph"]["subgraph_node_cap"])

TS = datetime(2026, 9, 1, 12, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
EXPOSURE_MINOR = 12_345_678
EDGE_TOTAL_MINOR = 123_45
COLLAPSED_TOTAL_MINOR = 9_876_544


def _key(label: str) -> str:
    """A pseudonymous 12-hex account key, derived rather than typed."""
    return hashlib.sha256(f"graph-route-fixture::{label}".encode()).hexdigest()[:12]


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return len(rows)


class GraphRun:
    """One complete run: four disconnected components and a large unreachable crowd.

    ``wide`` — a 301-node star under the cap. Everything reachable must be rendered, and
    every table read must come back no larger than the picture it serves.

    ``oversized`` — the seed's own community holds more members than the cap, which is the
    case the collapse used to add without testing. The graph layer's rule that the seed's
    community is never collapsed still holds; the overflow is now cut by hop distance and
    the cut is named.

    ``collapsible`` — the seed plus one over-cap community whose stored row is complete. Its
    meta-node must state the stored size (40,000), not the 1,600 members reached here.

    ``uncosted`` — identical except that ``community.currency`` is null while
    ``total_minor`` is not, so the figure has no currency to travel with.

    The 4,000 background accounts have no edges and never enter a traversal. They are what
    the whole-run selects used to materialise on every request.
    """

    wide_leaves = NODE_CAP // 5
    overflow = NODE_CAP + 100
    background_rows = 4000
    stored_size_of_collapsed = 40_000

    def __init__(self, root: Path) -> None:
        self.root = root
        self.run_id = new_run_id()
        assert is_ulid(self.run_id), self.run_id
        self.wide_seed = _key("wide-seed")
        self.wide_community = 11
        self.oversized_seed = _key("oversized-seed")
        self.oversized_community = 7
        self.collapsible_seed = _key("collapsible-seed")
        self.collapsible_community = 12
        self.uncosted_seed = _key("uncosted-seed")
        self.uncosted_community = 13
        # A community that membership references but `community` has no row for: the old
        # renderer labelled that meta-node with the reachable count as if it were the size.
        self.orphan_seed = _key("orphan-seed")
        self.orphan_community = 14
        self.sizes: dict[str, int] = {}
        self.leaves: dict[int, list[str]] = {}

    def _star(self, seed: str, community: int, count: int) -> list[dict[str, Any]]:
        leaves = [_key(f"{community}-leaf-{index}") for index in range(count)]
        self.leaves[community] = leaves
        return [
            {
                "run_id": self.run_id,
                "src_account_key": seed,
                "dst_account_key": leaf,
                "txn_count": 3,
                "total_minor": EDGE_TOTAL_MINOR,
                "currency": "UGX",
                "first_ts": TS,
                "last_ts": TS,
                "is_rail": False,
                "flags": ["fan_in"],
            }
            for leaf in leaves
        ]

    def _membership(
        self, key: str, community: int, degree: int, rank: float = 0.0002
    ) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "account_key": key,
            "community_id": community,
            "degree": degree,
            "pagerank": rank,
        }

    def write(self) -> dict[str, int]:
        warehouse = self.root / "warehouse"
        self.sizes["run"] = _jsonl(
            warehouse / "runs.jsonl",
            [
                {
                    "run_id": self.run_id,
                    "created_at": TS,
                    "finished_at": TS,
                    "state": "complete",
                    "seed": 1337,
                    "timezone": "UTC",
                    "provenance": "fixture",
                    "model_version": "oxbow/fixture-1",
                    "config_hash": "0" * 64,
                    "dataset_ref": "fixture",
                    "artifact_hashes": {},
                }
            ],
        )

        edges = [
            *self._star(self.wide_seed, self.wide_community, self.wide_leaves),
            *self._star(self.oversized_seed, self.oversized_community, self.overflow),
            *self._star(self.collapsible_seed, self.collapsible_community, self.overflow),
            *self._star(self.uncosted_seed, self.uncosted_community, self.overflow),
            *self._star(self.orphan_seed, self.orphan_community, self.overflow),
        ]

        memberships: list[dict[str, Any]] = [
            self._membership(self.wide_seed, self.wide_community, self.wide_leaves),
            *(
                self._membership(leaf, self.wide_community, 2)
                for leaf in self.leaves[self.wide_community]
            ),
            # The oversized star: seed and every peer share one community.
            self._membership(self.oversized_seed, self.oversized_community, self.overflow),
            *(
                self._membership(leaf, self.oversized_community, 1)
                for leaf in self.leaves[self.oversized_community]
            ),
        ]
        for seed, own in (
            (self.collapsible_seed, self.collapsible_community),
            (self.uncosted_seed, self.uncosted_community),
            (self.orphan_seed, self.orphan_community),
        ):
            # The seed sits in a community of its own that is small enough to keep; the
            # leaves form the over-cap community that collapses.
            memberships.append(self._membership(seed, self.wide_community, self.overflow))
            memberships.extend(self._membership(leaf, own, 1) for leaf in self.leaves[own])
        memberships.extend(
            self._membership(_key(f"background-{index}"), 3000 + (index % 1000), 1, 0.0001)
            for index in range(self.background_rows)
        )

        scored = sorted({str(row["account_key"]) for row in memberships})
        seeds = {
            self.wide_seed,
            self.oversized_seed,
            self.collapsible_seed,
            self.uncosted_seed,
        }
        scores = [
            {
                "run_id": self.run_id,
                "account_key": key,
                "band": "E" if key in seeds else "B",
                "fused_score": 0.5,
            }
            for key in scored
        ]
        economics = [
            {
                "run_id": self.run_id,
                "account_key": key,
                "currency": "UGX",
                "exposure_minor": EXPOSURE_MINOR,
            }
            for key in scored
        ]

        def community(
            index: int,
            size: int,
            density: float,
            total_minor: int,
            currency: str | None,
        ) -> dict[str, Any]:
            return {
                "run_id": self.run_id,
                "canonical_index": index,
                "raw_label": f"community-{index}",
                "size": size,
                "density": density,
                "total_minor": total_minor,
                "currency": currency,
                "algorithm": "leiden",
                "seed": 1337,
            }

        communities = [community(index, 4, 0.1, 400, "UGX") for index in range(3000, 4000)] + [
            community(self.wide_community, self.wide_leaves + 1, 0.9, 500, "UGX"),
            community(self.oversized_community, self.overflow + 1, 0.4, 600, "UGX"),
            community(
                self.collapsible_community,
                self.stored_size_of_collapsed,
                0.2,
                COLLAPSED_TOTAL_MINOR,
                "UGX",
            ),
            community(
                self.uncosted_community,
                self.overflow + 1,
                0.2,
                COLLAPSED_TOTAL_MINOR,
                # A stored total with no currency of its own: CHAR(3) and nullable, so this
                # is a shape the warehouse can hold and the renderer has to answer for.
                None,
            ),
        ]

        self.sizes["graph_edge"] = _jsonl(
            warehouse / "graph_edge" / f"run={self.run_id}.jsonl", edges
        )
        self.sizes["account_membership"] = _jsonl(
            warehouse / "account_membership" / f"run={self.run_id}.jsonl", memberships
        )
        self.sizes["score"] = _jsonl(warehouse / "score" / f"run={self.run_id}.jsonl", scores)
        self.sizes["economics"] = _jsonl(
            warehouse / "economics" / f"run={self.run_id}.jsonl", economics
        )
        self.sizes["community"] = _jsonl(
            warehouse / "community" / f"run={self.run_id}.jsonl", communities
        )
        return self.sizes


@pytest.fixture(scope="module")
def graph_run(tmp_path_factory: pytest.TempPathFactory) -> GraphRun:
    run = GraphRun(tmp_path_factory.mktemp("graph-warehouse"))
    run.write()
    return run


@pytest.fixture()
def harness(graph_run: GraphRun, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """The real app on the fixture run, with a row counter on the read model's ``select``.

    The counter replaces the bound method on the source instance, so it sees every read the
    route and the read model make — including the ``edges_for`` calls the route cannot see
    and this fence cannot bound.
    """
    env = {
        "RUN_SALT": TEST_RUN_SALT,
        "OXBOW_SEED": "1337",
        "OXBOW_LOCAL_JWT_SECRET": TEST_JWT_SECRET,
        "OXBOW_LOCAL_JWT_ENABLED": "true",
        "OXBOW_OIDC_ISSUER": "",
        "OXBOW_OIDC_JWKS_URL": "",
        "OXBOW_S3_ENDPOINT_URL": "",
        "OXBOW_SLACK_WEBHOOK_URL": "",
        "WEBHOOK_SIGNING_SECRET": "",
        "WEBHOOK_ENDPOINT": "",
        "OXBOW_REPO_ROOT": str(REPO_ROOT),
        "OXBOW_OUT_ROOT": str(graph_run.root),
        "DATABASE_URL": "",
        "OXBOW_WAREHOUSE": "null",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()

    container = build_container()
    assert container.backend == "null-file", container.backend
    # ``build_container`` resolves the null warehouse from ``repo_root/out`` and hands that
    # path to ``resolve_out_root`` as an explicit argument, which wins over ``OXBOW_OUT_ROOT``
    # (apps/api/deps.py:587). So a synthetic warehouse is bound by replacing the source with
    # the real ``FileWarehouseSource`` over the fixture root — the same class, the same read
    # model, the same money exponent the composition root derived from config. Only the
    # directory changes.
    container.read_model = ReadModel(
        FileWarehouseSource(graph_run.root),
        money_decimals=container.read_model.money_decimals,
    )

    source = container.read_model.source
    rows: dict[str, int] = defaultdict(int)
    reads: list[tuple[str, dict[str, Any]]] = []
    bound_select = source.select

    def counting_select(table: str, **kwargs: Any) -> Any:
        result = bound_select(table, **kwargs)
        rows[table] += len(result[0])
        reads.append((table, kwargs))
        return result

    monkeypatch.setattr(source, "select", counting_select)

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        # The lifespan builds its own container from the ambient environment; the counter
        # has to live on the one the requests actually use.
        app.state.container = container
        minted = client.post(
            "/api/auth/demo-token",
            json={"subject": "graph-route-analyst", "roles": ["analyst"]},
        )
        assert minted.status_code == 200, minted.text
        client.headers.update({"Authorization": f"Bearer {minted.json()['data']['access_token']}"})
        yield {
            "client": client,
            "rows": rows,
            "reads": reads,
            "container": container,
            "run": graph_run,
        }
    container.close()
    reset_settings_cache()


def _subgraph(harness: dict[str, Any], **params: Any) -> Any:
    harness["rows"].clear()
    harness["reads"].clear()
    return harness["client"].get("/api/graph/subgraph", params=params)


def _data(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"data", "meta"}, sorted(body)
    assert "success" not in body, sorted(body)
    return dict(body["data"])


def test_every_account_reachable_under_the_cap_is_rendered(harness: dict[str, Any]) -> None:
    """A graph that fits is the whole graph — not the seed on its own.

    ``included`` was seeded with ``{account_key}`` and only replaced when the cap was
    exceeded, so any traversal under 1,500 nodes served exactly one node; and because a kept
    edge needs both endpoints inside that set, it served no edges either.
    """
    run = harness["run"]
    data = _data(_subgraph(harness, account_key=run.wide_seed))
    assert data["run_id"] == run.run_id, "the route served a run other than the fixture"
    assert data["truncated"] is False, data["truncation_reason"]
    assert data["hops"] == 2, data["hops"]
    assert len(data["nodes"]) == run.wide_leaves + 1, len(data["nodes"])
    assert {node["id"] for node in data["nodes"]} == {
        run.wide_seed,
        *run.leaves[run.wide_community],
    }
    assert len(data["edges"]) == run.wide_leaves, len(data["edges"])
    # A total deterministic order, never set iteration order.
    assert [node["id"] for node in data["nodes"]] == sorted(node["id"] for node in data["nodes"])
    assert [(edge["source"], edge["target"]) for edge in data["edges"]] == sorted(
        (edge["source"], edge["target"]) for edge in data["edges"]
    ), "the served edge list has no total order"


def test_one_request_materialises_no_more_accounts_than_it_renders(harness: dict[str, Any]) -> None:
    """The cap bounds the reads, not only the response.

    Each overlay table was selected with ``where={"run_id": rid}`` and nothing else, so a
    301-node picture materialised every row the run wrote into ``account_membership``,
    ``score``, ``economics`` and ``community``, converting each row to a dict on the way.
    """
    run = harness["run"]
    nodes = _data(_subgraph(harness, account_key=run.wide_seed))["nodes"]
    assert nodes
    assert run.sizes["score"] > 4 * len(nodes), run.sizes
    for table in ("account_membership", "score", "economics", "community"):
        assert harness["rows"][table] <= len(nodes), (
            f"one request materialised {harness['rows'][table]} {table} rows to render "
            f"{len(nodes)} nodes; the whole-run read is {run.sizes[table]} rows"
        )


def test_edge_traversal_cost_is_measured_and_reported(harness: dict[str, Any]) -> None:
    """What is still read per request, because hop 2 cannot be derived from hop 1's rows.

    ``ReadModel.subgraph`` asks for every stored edge touching each hop's frontier, and the
    null-file ``edges_for`` answers by reading the whole per-run ``graph_edge`` table and
    filtering it in Python once per hop. That seam is outside this route's fence, so the
    figure printed here is a measurement of a cost that remains, not a claim it was fixed.
    """
    run = harness["run"]
    data = _data(_subgraph(harness, account_key=run.wide_seed))
    edge_reads = [kwargs for table, kwargs in harness["reads"] if table == "graph_edge"]
    assert len(edge_reads) == data["hops"], edge_reads
    assert all("limit" not in kwargs for kwargs in edge_reads), edge_reads
    assert harness["rows"]["graph_edge"] == run.sizes["graph_edge"] * data["hops"], harness["rows"]
    print(
        f"\nrows materialised per {data['hops']}-hop subgraph request: "
        f"{json.dumps(dict(sorted(harness['rows'].items())), sort_keys=True)}; "
        f"per-run graph_edge table = {run.sizes['graph_edge']} rows"
    )


def test_the_cap_is_enforced_when_the_seeds_own_community_exceeds_it(
    harness: dict[str, Any],
) -> None:
    """``truncated=True`` must never accompany more nodes than ``node_cap``.

    The seed's community was added to the inclusion set with no cap test at all. It is still
    never collapsed — that is the graph layer's own policy — but the overflow is now cut by
    hop distance and the cut is named in ``truncation_reason``.
    """
    run = harness["run"]
    data = _data(_subgraph(harness, account_key=run.oversized_seed, hops=1))
    reachable = run.overflow + 1
    assert reachable > NODE_CAP, "the fixture stopped exceeding the cap"
    assert data["node_cap"] == NODE_CAP
    assert data["truncated"] is True
    assert len(data["nodes"]) <= data["node_cap"], (
        f"truncated=True with {len(data['nodes'])} nodes over a cap of {data['node_cap']}: "
        "the response reports a ceiling it did not apply"
    )
    assert [node["id"] for node in data["nodes"] if node["is_seed"]] == [run.oversized_seed]
    assert "not rendered" in str(data["truncation_reason"]), data["truncation_reason"]
    assert data["collapsed_communities"] == []
    assert len(data["nodes"]) == NODE_CAP, data["node_cap"]


def test_a_collapsed_community_is_labelled_with_its_stored_size(harness: dict[str, Any]) -> None:
    """40,000 members are stored; 1,600 are reachable. The meta-node says 40,000.

    The renderer fell back to the reachable count whenever the stored row was absent, which
    labels a cluster with a smaller number than the cluster has — the understatement plan §18
    calls fatal.
    """
    run = harness["run"]
    data = _data(_subgraph(harness, account_key=run.collapsible_seed, hops=1))
    assert data["truncated"] is True
    meta = data["collapsed_communities"]
    assert len(meta) == 1, meta
    assert meta[0]["community_id"] == run.collapsible_community
    assert meta[0]["member_count"] == run.stored_size_of_collapsed, meta[0]
    assert meta[0]["member_count"] > run.overflow, meta[0]
    assert meta[0]["total"] == {
        "minor": COLLAPSED_TOTAL_MINOR,
        "currency": "UGX",
        "decimals": 2,
    }, meta[0]
    assert meta[0]["representative_account_key"] == min(run.leaves[run.collapsible_community])
    assert [node["id"] for node in data["nodes"]] == [run.collapsible_seed]


def test_a_collapsed_community_with_no_stored_row_is_refused(harness: dict[str, Any]) -> None:
    """Membership names community 14; ``community`` has no row for it. Refused, not guessed.

    The meta-node used to fall back to ``len(members)`` — the count this traversal reached —
    and put it on the wire as the community's member count. On this fixture that renders
    "1,600 accounts" for a community the run never wrote a size for, which is the number a
    fraud exhibit would be challenged on.
    """
    run = harness["run"]
    response = _subgraph(harness, account_key=run.orphan_seed, hops=1)
    assert response.status_code == 503, response.text
    problem = response.json()
    assert str(run.orphan_community) in problem["detail"], problem["detail"]
    assert "member" in problem["detail"], problem["detail"]


def test_a_total_without_a_stored_currency_is_refused_not_defaulted(
    harness: dict[str, Any],
) -> None:
    """``community.currency`` null beside a real ``total_minor`` is a 503, never UGX.

    config/economics.yaml does say UGX and this corpus is priced in it, so a defaulted
    currency would have rendered as a plausible figure rather than as a defect — which is
    why the absence has to be a refusal and not a fallback.
    """
    run = harness["run"]
    response = _subgraph(harness, account_key=run.uncosted_seed, hops=1)
    assert response.status_code == 503, response.text
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE), response.headers
    problem = response.json()
    assert problem["status"] == 503, problem
    assert "currency" in problem["detail"], problem["detail"]
    assert "UGX" not in json.dumps(problem), problem


def test_money_decimals_is_the_containers_exponent_and_never_the_base(
    harness: dict[str, Any],
) -> None:
    """Every figure this route serves carries ``decimals=2``, the converted exponent.

    The route passed ``container.economics.minor_units_per_major`` — the BASE, 100 — into
    ``Money.decimals``, the EXPONENT, so each figure rendered as ``minor / 10**100``. It now
    goes through ``ReadModel.money``, which holds the exponent the composition root derived
    from config, so this router cannot state a scale at all.
    """
    run = harness["run"]
    container = harness["container"]
    assert container.economics.minor_units_per_major == 100
    assert container.read_model.money_decimals == 2
    data = _data(_subgraph(harness, account_key=run.wide_seed))
    figures = [node["exposure"] for node in data["nodes"] if node["exposure"]] + [
        edge["total"] for edge in data["edges"]
    ]
    assert figures, "no money figure reached the response, so this test asserted nothing"
    assert {figure["decimals"] for figure in figures} == {2}, sorted(
        {figure["decimals"] for figure in figures}
    )
    assert {figure["minor"] for figure in figures} == {EXPOSURE_MINOR, EDGE_TOTAL_MINOR}
    assert all(
        isinstance(figure["minor"], int) and not isinstance(figure["minor"], bool)
        for figure in figures
    )
    assert {figure["currency"] for figure in figures} == {"UGX"}


def test_served_overlays_stay_derived_from_stored_columns(harness: dict[str, Any]) -> None:
    """The node overlays are read, not recomputed — and they survive the bounded reads.

    ``dense_community`` comes from ``community.density`` (0.9 here) and ``flagged`` from the
    stored ``score.band`` of E, both of which used to be computed against whole-run reads. If
    a projection drops a column the overlay needs, this is the test that notices.
    """
    run = harness["run"]
    data = _data(_subgraph(harness, account_key=run.wide_seed))
    seed = next(node for node in data["nodes"] if node["id"] == run.wide_seed)
    assert seed["band"] == "E"
    assert seed["community_id"] == run.wide_community
    assert seed["degree"] == run.wide_leaves
    assert seed["is_seed"] is True
    assert {"flagged", "dense_community", "fan_in"} <= set(seed["flags"]), seed["flags"]
    leaf = next(node for node in data["nodes"] if node["id"] != run.wide_seed)
    assert leaf["band"] == "B", leaf
    assert "flagged" not in leaf["flags"], leaf
    assert harness["rows"]["community"] == 1, dict(harness["rows"])
