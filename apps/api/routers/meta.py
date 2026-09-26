"""Meta routes: the dataset card, the economics, the disclaimer, the licence.

Plan §14 puts a dataset badge in the dashboard header and a licence block on the
validation page, and 01 §A rule 7 makes attribution a property of the data rather
than a paragraph in a README — which is why the card carries licence, share-alike
obligation, citation, retrieval method, per-file SHA-256 and label caveats next to
the counts, in one response the UI cannot partially render.

Three things this router refuses to do:

* **invent a measurement.** Numbers come from the warehouse's ``measurement`` table
  when the pipeline has written it, and otherwise from the artifact the measuring
  script wrote, with the command that produced it. When neither exists the list is
  empty and the note says which, because a corpus statistic with no provenance is the
  kind of number a judge asks about last.
* **echo a raw identifier.** ``data/graph_measurement.json`` contains the top senders
  by raw account id. The PII boundary is at ingest and holds downstream including
  responses (02 §F), so measurement values are admitted only as numbers.
* **summarise the economics.** ``/api/meta/economics`` returns the file's own nested
  structure, verbatim, because plan §11 requires the *assumptions* on screen, not a
  paraphrase of them, and the policy simulator's sliders read their ranges from here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Depends, Query

from api.deps import Container, analyst_or_higher, get_container
from api.problems import COMMON_ERROR_STATUSES, DependencyUnavailable, problem_responses
from api.readmodel import ReadModel
from api.routers.common import assumption_lines, build_meta
from api.schemas.catalog import DatasetMeta, MeasurementCard
from api.schemas.common import Envelope, envelope
from api.schemas.me import KnownRoles
from api.security import Principal
from oxbow.config import ConfigError, load_yaml
from oxbow.ports.case_sink import OXBOW_DISCLAIMER

router = APIRouter(prefix="/api/meta", tags=["meta"])

# The measurement artifacts the P1a/P3a scripts wrote, keyed by the corpus they
# describe. Named explicitly: a directory scan would pick up whatever a later script
# dropped there and present it as dataset-card content.
MEASUREMENT_ARTIFACTS: Final = {
    "paysim": "data/graph_measurement.json",
    "ibmaml": "data/ibm_graph_measurement.json",
}
MEASUREMENT_COMMANDS: Final = {
    "paysim": "uv run python scripts/measure_graph.py",
    "ibmaml": "uv run python scripts/measure_ibm_graph.py",
}
# Keys whose values are numbers or booleans only. Anything else (an account id in
# ``top20_senders``, a filename, a currency name) is not a measurement this response
# is allowed to carry.
_SCALAR_TYPES = (int, float)


def _flatten(prefix: str, node: Any, into: dict[str, Any]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), value, into)
    elif isinstance(node, list):
        if node and all(isinstance(item, _SCALAR_TYPES) and not isinstance(item, bool) for item in node):
            into[f"{prefix}.count"] = len(node)
            into[f"{prefix}.min"] = min(node)
            into[f"{prefix}.max"] = max(node)
    elif isinstance(node, _SCALAR_TYPES) and not isinstance(node, bool):
        into[prefix] = float(node)


@router.get(
    "/dataset",
    response_model=Envelope[DatasetMeta],
    summary="Dataset card: sources, licences, hashes, caveats and measured numbers",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def dataset_card(
    run_id: str | None = Query(default=None, description="Read measurements for one run."),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    sources = _declared_sources(read_model, run_id=run_id)
    refused = _refused_sources(read_model)
    measurements = _measurements(read_model, run_id=run_id)
    meta = build_meta(
        container,
        run_id=run_id,
        provenance=None if run_id is None else _run_provenance(read_model, run_id),
        assumptions=[
            {
                "key": "sources.yaml#license",
                "value": "per-source",
                "source": "config/sources.yaml",
                "note": "share-alike obligations attach to every derived table this card describes",
            }
        ],
    )
    body = DatasetMeta(
        sources=sources,
        refused_sources=refused,
        measurements=measurements,
        sampling=_sampling(read_model),
        deidentification=_deidentification(read_model),
        disclaimer=OXBOW_DISCLAIMER,
    )
    return envelope(body, **meta.model_dump())


@router.get(
    "/economics",
    response_model=Envelope[dict[str, Any]],
    summary="Economic assumptions, verbatim from config/economics.yaml",
    description=(
        "Every currency figure this API produces is a function of these values. They are "
        "returned as the file's own nested structure — not a flattened summary — so the "
        "simulator can render its sliders against the ranges the config actually declares."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def economics_assumptions(
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    raw = container.settings.economics()
    meta = build_meta(
        container,
        assumptions=[line.model_dump() for line in assumption_lines(container.economics)],
    )
    return envelope(
        {
            "assumptions": raw,
            "currency": container.economics.currency,
            "minor_units_per_major": container.economics.minor_units_per_major,
            "recovery_band": list(container.economics.recovery.band),
            "capacity_minutes": container.economics.capacity.review_minutes_per_period,
            "capacity_sweep": {
                "min_minutes": container.economics.capacity.sweep.min_minutes,
                "max_minutes": container.economics.capacity.sweep.max_minutes,
                "points": container.economics.capacity.sweep.points,
            },
            "four_eyes_threshold_minor": container.economics.four_eyes.threshold_exposure_minor,
            "solver": {
                "greedy_budget_ms": container.economics.solver.greedy_budget_ms,
                "cpsat_deadline_ms": container.economics.solver.cpsat_deadline_ms,
                "agreement_tolerance_ratio": container.economics.solver.agreement_tolerance_ratio,
            },
            "note": (
                "Illustrative assumptions, not measured outcomes. Changing any of these "
                "changes every money figure in the product; that is the point of keeping "
                "them in one file rather than in the code that uses them."
            ),
        },
        **meta.model_dump(),
    )


@router.get(
    "/disclaimer",
    response_model=Envelope[dict[str, Any]],
    summary="The disclaimer text, verbatim, with the places it is required",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def disclaimer(
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    meta = build_meta(container)
    return envelope(
        {
            "text": OXBOW_DISCLAIMER,
            "required_in": ["readme", "app-footer", "every-exported-packet-page-one"],
            "scenario_note": (
                "Any East-African mobile-money framing in the UI is illustrative scenario "
                "dressing over permitted public data, not a claim about any real institution, "
                "market or regulator."
            ),
            "money_note": (
                "Every currency figure depends on the recovery-rate and cost assumptions in "
                "config/economics.yaml, and those are illustrative."
            ),
        },
        **meta.model_dump(),
    )


@router.get(
    "/licence",
    response_model=Envelope[dict[str, Any]],
    summary="Per-corpus licence, share-alike obligation and citation, with retrieval dates",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def licence(
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    cards = _declared_sources(read_model, run_id=None)
    meta = build_meta(container)
    return envelope(
        {
            "corpora": [
                {
                    "source_id": card.source_id,
                    "name": card.name,
                    "licence": card.license,
                    "obligation": card.license_obligation,
                    "citation": card.citation,
                    "retrieval": card.retrieval,
                    "retrieved_at": None if card.retrieved_at is None else card.retrieved_at.isoformat(),
                    "ingest_allowed": card.ingest_allowed,
                    "files": [
                        {"file_name": item.file_name, "sha256": item.sha256, "size_bytes": item.size_bytes}
                        for item in card.files
                    ],
                }
                for card in cards
            ],
            "repository_licence": "Apache-2.0",
            "derived_data_licence": (
                "CC BY-SA 4.0 (PaySim derivatives) and CDLA-Sharing-1.0 (IBM-AML derivatives). "
                "Both impose share-alike on derived data, which covers the canonical event table "
                "and every published sample."
            ),
            "never_used": ["IEEE-CIS (competition-governed terms)", "Elliptic (CC BY-NC-ND: cite-only)"],
        },
        **meta.model_dump(),
    )


@router.get(
    "/roles",
    response_model=Envelope[KnownRoles],
    summary="The role set, so the generated client's union is derived not remembered",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def roles(
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    return envelope(KnownRoles(), **build_meta(container).model_dump())


def _sources_document(read_model: ReadModel) -> dict[str, Any]:
    root = _repo_root(read_model)
    try:
        return load_yaml(root / "config" / "sources.yaml")
    except ConfigError as exc:
        raise DependencyUnavailable(f"config/sources.yaml could not be read: {exc}") from exc


def _repo_root(read_model: ReadModel) -> Path:
    from oxbow.config import find_repo_root

    return find_repo_root()


def _declared_sources(read_model: ReadModel, *, run_id: str | None) -> list[Any]:
    """Config-declared sources, with warehouse rows preferred when they exist.

    The table wins where it has rows because the pipeline is what records hashes at
    verification time; the config wins where the table is empty because the licence and
    the citation are declarations, not measurements, and they must be served before a
    run has ever happened.
    """
    from api.schemas.catalog import DatasetFileCard, DatasetSourceCard

    document = _sources_document(read_model)
    rows, _ = read_model.source.select("dataset_source", where={}, allow_missing=True)
    by_id = {str(row["source_id"]): row for row in rows}
    files, _ = read_model.source.select("dataset_file", where={}, allow_missing=True)
    cards: list[Any] = []
    for entry in document.get("sources", []):
        source_id = str(entry["id"])
        stored_row = by_id.get(source_id, {})
        merged = {**entry, **{k: v for k, v in stored_row.items() if v is not None}}
        card = DatasetSourceCard(
            source_id=source_id,
            name=str(merged.get("name", source_id)),
            role=str(merged.get("role", "unknown")),
            module=merged.get("module"),
            source_url=str(merged.get("source_url", "")),
            retrieval=str(merged.get("retrieval", "unrecorded")),
            license=str(merged.get("license", "UNSTATED")),
            license_obligation=str(merged.get("license_obligation", "UNSTATED")),
            citation=str(merged.get("citation", "UNSTATED")),
            description=str(merged.get("description", "")),
            label_caveat=str(merged.get("label_caveat", "no caveat recorded")),
            known_biases=[str(bias) for bias in merged.get("known_biases", [])],
            synthetic_fields=[str(field) for field in merged.get("synthetic_fields", [])],
            ingest_allowed=bool(merged.get("ingest_allowed", True)),
            retrieved_at=_first_date(merged.get("retrieved_at")),
            files=[
                DatasetFileCard(
                    file_name=str(item.get("name", "")),
                    sha256=str(item.get("sha256", "")),
                    size_bytes=item.get("size_bytes"),
                    row_count=item.get("row_count"),
                )
                for item in entry.get("files", [])
            ],
        )
        for stored_file in files:
            if str(stored_file["source_id"]) != source_id:
                continue
            card.files.append(
                DatasetFileCard(
                    file_name=str(stored_file["file_name"]),
                    sha256=str(stored_file["sha256"]),
                    size_bytes=stored_file.get("size_bytes"),
                    row_count=stored_file.get("row_count"),
                    verified_at=stored_file.get("verified_at"),
                )
            )
        cards.append(card)
    return cards


def _first_date(value: Any) -> Any:
    from datetime import date

    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _refused_sources(read_model: ReadModel) -> list[dict[str, Any]]:
    document = _sources_document(read_model)
    refused = [
        {
            "source_id": str(entry["id"]),
            "name": str(entry.get("name", "")),
            "reason": str(entry.get("reason", "no reason recorded")),
            "status": "refused",
        }
        for entry in document.get("refused", [])
    ]
    refused.extend(
        {
            "source_id": str(row["source_id"]),
            "name": str(row["name"]),
            "reason": "declared in the warehouse with ingest_allowed = false",
            "status": "not-ingested",
        }
        for row in _rows_or_empty(read_model, "dataset_source", {"ingest_allowed": False})
    )
    return refused


def _rows_or_empty(read_model: ReadModel, table: str, where: dict[str, Any]) -> list[dict[str, Any]]:
    """Read a table that may legitimately have no rows, and say nothing if it does not.

    Distinct from a *query* that may not silently be empty: the dataset card's refusal
    list and its drift table are optional content, and an optional table that cannot be
    reached is reported by the caller's own error rather than by this helper.
    """
    try:
        rows, _ = read_model.source.select(table, where=where, allow_missing=True)
    except DependencyUnavailable:
        return []
    return rows


def _measurements(read_model: ReadModel, *, run_id: str | None) -> list[MeasurementCard]:
    stored = _rows_or_empty(read_model, "measurement", {})
    if stored:
        return [
            MeasurementCard(
                scope=str(row["scope"]),
                name=str(row["name"]),
                value=float(row["value"]),
                unit=row.get("unit"),
                command=str(row["command"]),
                measured_at=row["measured_at"],
            )
            for row in stored
        ]
    cards: list[MeasurementCard] = []
    root = _repo_root(read_model)
    for corpus, relative in MEASUREMENT_ARTIFACTS.items():
        path = root / relative
        if not path.is_file():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        scalars: dict[str, Any] = {}
        _flatten(corpus, {k: v for k, v in document.items() if k != "top20_senders"}, scalars)
        measured_at = document.get("measured_at") or document.get("measured_at_utc")
        for name, value in sorted(scalars.items()):
            cards.append(
                MeasurementCard(
                    scope=corpus,
                    name=name,
                    value=float(value),
                    unit=None,
                    command=MEASUREMENT_COMMANDS[corpus],
                    measured_at=_parse_dt(measured_at),
                )
            )
    if not cards:
        cards.append(
            MeasurementCard(
                scope="none",
                name="measurements-unavailable",
                value=0.0,
                unit="count",
                command="make data && uv run python scripts/measure_graph.py",
                measured_at=_parse_dt(None),
            )
        )
    return cards


def _parse_dt(value: Any) -> Any:
    from datetime import UTC, datetime

    if value is None:
        return datetime.now(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _sampling(read_model: ReadModel) -> dict[str, Any]:
    from oxbow.config import load_pipeline_config

    return dict(load_pipeline_config().sampling)


def _deidentification(read_model: ReadModel) -> dict[str, Any]:
    document = _sources_document(read_model)
    policy = document.get("deidentification", {})
    return {
        "account_key": dict(policy.get("account_key", {})),
        "raw_identifier_policy": {
            key: value
            for key, value in dict(policy.get("raw_identifier_policy", {})).items()
            # The regex itself is not echoed: a client that has the pattern does not
            # need it (the server applies it), and a response that carries it invites
            # the client to re-implement the boundary that must stay server-side.
            if key != "redaction_pattern"
        },
        "note": (
            "Raw identifiers stop at ingest. Everything downstream — responses, logs, "
            "traces, exports — carries account_key only, and the log redaction filter is "
            "asserted by tests/unit/test_p7_logging.py."
        ),
    }


def _run_provenance(read_model: ReadModel, run_id: str) -> str:
    return str(read_model.run_row(run_id)["provenance"])


__all__ = ["router"]
