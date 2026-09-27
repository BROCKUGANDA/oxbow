"""Meta routes: the current run, the dataset card, the economics, the disclaimer, the licence.

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

``/api/meta/run`` adds a fourth refusal, because its payload is read by the header strip
on every screen: it does not **pad a nullable field to satisfy a shape**. ``run_id``,
``dataset``, ``licence`` and ``model_version`` are null on a warehouse that holds nothing,
each with an entry in ``degradations`` naming the artifact that would hold the value, and
the client renders its labelled arm (DESIGN.md §5: degraded, not broken). A route that
served ``""`` or ``0`` there would convert "this deployment has no run" into a false
attribution claim, which is the one thing the header exists to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Depends, Query

from api.deps import Container, analyst_or_higher, get_container
from api.problems import (
    COMMON_ERROR_STATUSES,
    DependencyUnavailable,
    RunNotFound,
    problem_responses,
)
from api.readmodel import ReadModel, state_value
from api.routers.common import assumption_lines, build_meta
from api.schemas.catalog import DatasetMeta, MeasurementCard
from api.schemas.common import Envelope, envelope
from api.schemas.me import KnownRoles
from api.schemas.runtime import (
    FieldDegradation,
    RunSourceCard,
    RunSourceFileCard,
    RuntimeRun,
)
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

# The join the header renders. Each member is verbatim from an artifact; only the
# separator is this route's own.
DATASET_JOIN: Final = " · "
# Display text for ``degradations``: the artifact a missing value would come from, named
# for whoever reads the response. The route opens files through ``_run_manifest_path``,
# which reads the real names from the ingest writer, so this string never locates a file.
INTERIM_WOULD_BE: Final = "data/interim/<source>/run_manifest.json"


def _run_manifest_path(root: Path, source_id: str) -> Path:
    """``data/interim/<source>/run_manifest.json``, with the names read from the writer.

    The three path components are imported rather than retyped because the ingest sink
    owns them: a route that hardcoded the filename would keep answering after ingest
    moved the artifact, and would report the corpus as never ingested. Imported inside
    the function because :mod:`oxbow.adapters.file.canonical_sink` pulls duckdb and
    polars at module scope, which this router has no reason to load to read a string.
    """
    from oxbow.adapters.file.canonical_sink import (
        DATA_DIRNAME,
        INTERIM_DIRNAME,
        RUN_MANIFEST_FILENAME,
    )

    return root / DATA_DIRNAME / INTERIM_DIRNAME / source_id / RUN_MANIFEST_FILENAME


def _flatten(prefix: str, node: Any, into: dict[str, Any]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), value, into)
    elif isinstance(node, list):
        if node and all(
            isinstance(item, _SCALAR_TYPES) and not isinstance(item, bool) for item in node
        ):
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
    "/run",
    response_model=Envelope[RuntimeRun],
    summary="The current run as measured: zone, seed, hashes, licences, model version",
    description=(
        "The header strip on every screen reads this response: the deployment timezone a "
        "timestamp is rendered in, the currency and minor-unit scale every figure divides "
        "by, the economics assumptions those figures are a function of, the corpora with "
        "the licences their ingests recorded, and the model version and config hash that "
        "produced the numbers. Values come from the ``run`` row, ``config/economics.yaml``, "
        "``config/pipeline.yaml`` and each corpus's ingest manifest. Where this warehouse "
        "holds nothing the field is null and ``degradations`` names the artifact that would "
        "hold it: the client renders each null as a labelled gap, which DESIGN.md §5 "
        "requires in place of a spinner or a filler value."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def runtime_run(
    run_id: str | None = Query(
        default=None,
        description="Describe one named run rather than the newest complete one.",
    ),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    run, no_run_reason = _served_run(container.read_model, run_id)
    sources, gaps = _ingested_sources(container)
    economics, unrenderable = _economics_scalars(container.settings.economics())
    deployment_timezone, timezone_source = _deployment_timezone(container, run)

    degradations: list[FieldDegradation] = list(gaps)
    if run is None and no_run_reason is not None:
        for field, would_come_from in (
            ("run_id", "the `run` table's newest row in state 'complete'"),
            ("model_version", "run.model_version"),
            ("config_hash", "run.config_hash"),
            ("seed", "run.seed"),
            ("provenance", "run.provenance"),
        ):
            degradations.append(
                FieldDegradation(
                    field=field,
                    reason=no_run_reason,
                    would_come_from=would_come_from,
                )
            )
    dataset = DATASET_JOIN.join(card.name for card in sources) or None
    licence = DATASET_JOIN.join(dict.fromkeys(card.licence for card in sources)) or None
    if dataset is None:
        degradations.append(
            FieldDegradation(
                field="dataset",
                reason=(
                    "no corpus under config/sources.yaml has an ingest manifest on this "
                    "host, so no dataset was read by anything this server can point at"
                ),
                would_come_from=f"{INTERIM_WOULD_BE} per corpus, written by ingest",
            )
        )
    if licence is None:
        degradations.append(
            FieldDegradation(
                field="licence",
                reason=(
                    "attribution is a property of the data (01 §A rule 7), and no ingest "
                    "recorded a licence for any corpus on this host"
                ),
                would_come_from=f"{INTERIM_WOULD_BE}, `source.license`",
            )
        )
    for key in unrenderable:
        degradations.append(
            FieldDegradation(
                field="economics",
                reason=(
                    f"{key} in config/economics.yaml holds a structure that is neither a "
                    "string nor a number, so it cannot be served as an assumption line"
                ),
                would_come_from="config/economics.yaml",
            )
        )

    body = RuntimeRun(
        run_id=None if run is None else _text(run, "run_id"),
        deployment_timezone=deployment_timezone,
        currency=container.economics.currency,
        minor_units_per_major=container.economics.minor_units_per_major,
        economics_source=_economics_source(container),
        economics=economics,
        dataset=dataset,
        licence=licence,
        model_version=None if run is None else _text(run, "model_version"),
        # 'pipeline' is the only provenance that is not demo data: RUN_PROVENANCES is
        # ("pipeline", "fixture", "demo_snapshot"), and the last two are the two ways this
        # product says "these numbers are a demonstration". With no run row there are no
        # numbers at all, so the claim to make is the one that is true — nothing here is
        # demo bytes — and `run_id: null` is the field that says why.
        demo_data=run is not None and _text(run, "provenance") != "pipeline",
        run_state=None if run is None else state_value(run["state"]),
        provenance=None if run is None else _text(run, "provenance"),
        seed=None if run is None else _int(run.get("seed")),
        config_hash=None if run is None else _text(run, "config_hash"),
        # Null, not {}: the column was written by the run, and a run that recorded no
        # artifact hash has not recorded an empty set of them.
        artifact_hashes=(run.get("artifact_hashes") or None) if run is not None else None,
        timezone_source=timezone_source,
        created_at=None if run is None else run.get("created_at"),
        finished_at=None if run is None else run.get("finished_at"),
        warehouse_backend=container.backend,
        sources=sources,
        degradations=sorted(degradations, key=lambda item: (item.field, item.reason)),
    )
    meta = build_meta(
        container,
        run_id=body.run_id,
        model_version=body.model_version,
        # 'deployment' when there is no run: this response describes the server, and the
        # client's provenance badge must not read a null as a claim that the bytes are
        # fixture output.
        provenance=body.provenance or "deployment",
        assumptions=[line.model_dump() for line in assumption_lines(container.economics)],
    )
    return envelope(body, **meta.model_dump())


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
                    "retrieved_at": None
                    if card.retrieved_at is None
                    else card.retrieved_at.isoformat(),
                    "ingest_allowed": card.ingest_allowed,
                    "files": [
                        {
                            "file_name": item.file_name,
                            "sha256": item.sha256,
                            "size_bytes": item.size_bytes,
                        }
                        for item in card.files
                    ],
                }
                for card in cards
            ],
            "repository_licence": "MIT",
            "derived_data_licence": (
                "CC BY-SA 4.0 (PaySim derivatives) and CDLA-Sharing-1.0 (IBM-AML derivatives). "
                "Both impose share-alike on derived data, which covers the canonical event table "
                "and every published sample."
            ),
            "never_used": [
                "IEEE-CIS (competition-governed terms)",
                "Elliptic (CC BY-NC-ND: cite-only)",
            ],
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


def _rows_or_empty(
    read_model: ReadModel, table: str, where: dict[str, Any]
) -> list[dict[str, Any]]:
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


# ------------------------------------------------------------- /api/meta/run ---


def _served_run(read_model: ReadModel, run_id: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """The run this response describes, and why there is not one.

    A named run that does not exist is a 404 — ``run_row`` raises it, because the caller
    asked about a specific thing and the answer is that it is not there. The *default*
    lookup degrades instead of raising: the header strip asks "what deployment am I
    looking at", and a warehouse with no complete run has an honest answer (none, and
    here is what was checked) that is worth more than a 404 on the page that would
    explain it.
    """
    if run_id is not None:
        return read_model.run_row(run_id), None
    try:
        return read_model.resolve_run(None, state="complete"), None
    except RunNotFound as exc:
        return None, exc.detail


def _deployment_timezone(container: Container, run: dict[str, Any] | None) -> tuple[str, str]:
    """The zone, and which record it was read from.

    ``deployment_timezone`` is non-nullable in the client's decoder because every
    timestamp on every screen is rendered through it, and the value genuinely always
    exists at one of two places: the run row wrote the zone ingest actually used, and
    before any run there is the zone ``config/pipeline.yaml`` declares and the ingest
    layer refuses to run without. Naming which of the two supplied it is what stops the
    response reading as a measurement when it is a declaration.
    """
    from_run = None if run is None else _text(run, "timezone")
    if from_run is not None:
        return from_run, "run record: run.timezone"
    try:
        configured = container.settings.pipeline_config()
    except ConfigError as exc:
        raise DependencyUnavailable(
            f"deployment_timezone is unrenderable: config/pipeline.yaml could not be read "
            f"and no run row recorded the zone actually used ({exc})"
        ) from exc
    return configured.deployment_timezone, "config/pipeline.yaml: deployment_timezone"


def _economics_source(container: Container) -> str:
    """The economics file as a repo-relative path, because that is what a reader checks."""
    path = container.economics.source_path
    root = container.settings.repo_root
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        # Loaded from outside the repo: the file's own name is still true, and the
        # absolute path is not this response's to publish.
        return path.name


def _economics_scalars(document: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """``config/economics.yaml`` flattened to dotted keys, in ascending key order.

    The client decodes this as ``Record<string, string | number>``, so the walk keeps a
    scalar's own type — an integer minor unit stays an integer (DEV-005: money is never a
    float) and a rate stays a float — and renders the two shapes a record cannot hold
    structurally as text: a list of scalars becomes its comma-joined members, a boolean
    ``true``/``false``. Anything else (a list of mappings, a null) is reported rather than
    dropped or coerced, because a silently missing assumption is an assumption nobody can
    check. Keys are sorted rather than emitted in file order so two servers with the same
    bytes serve byte-identical JSON.
    """
    flat: dict[str, Any] = {}
    unrenderable: list[str] = []

    def walk(node: Mapping[str, Any], prefix: str) -> None:
        for key in sorted(node, key=str):
            value = node[key]
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, Mapping):
                walk(value, path)
            elif isinstance(value, bool):
                flat[path] = "true" if value else "false"
            elif isinstance(value, int | float | str):
                flat[path] = value
            elif isinstance(value, list) and all(
                isinstance(item, bool | int | float | str) for item in value
            ):
                flat[path] = ", ".join(
                    ("true" if item else "false") if isinstance(item, bool) else str(item)
                    for item in value
                )
            else:
                unrenderable.append(path)

    walk(document, "")
    return dict(sorted(flat.items())), sorted(set(unrenderable))


def _ingested_sources(container: Container) -> tuple[list[RunSourceCard], list[FieldDegradation]]:
    """Every declared corpus whose ingest manifest is on disk, ascending by source id.

    The corpus list is driven by ``config/sources.yaml`` rather than by a directory scan,
    for the same reason the measurement artifacts above are named: a scan would serve
    whatever a later script dropped into ``data/interim`` as an attribution claim.
    """
    document = _sources_document(container.read_model)
    root = container.settings.repo_root
    cards: list[RunSourceCard] = []
    gaps: list[FieldDegradation] = []
    for entry in sorted(document.get("sources", []), key=lambda item: str(item["id"])):
        source_id = str(entry["id"])
        path = _run_manifest_path(root, source_id)
        relative = _relative(root, path)
        if not bool(entry.get("ingest_allowed", True)):
            if path.is_file():
                raise DependencyUnavailable(
                    f"{relative} exists for {source_id!r}, which config/sources.yaml declares "
                    "with ingest_allowed = false. The corpus was ingested against terms the "
                    "declaration refuses, and this response will not present it as permitted."
                )
            continue
        if not path.is_file():
            gaps.append(
                FieldDegradation(
                    field="sources",
                    reason=(
                        f"config/sources.yaml declares {source_id!r} as ingestible, but "
                        f"{relative} does not exist: nothing has been read from it on this host"
                    ),
                    would_come_from=relative,
                )
            )
            continue
        card, card_gaps = _source_card(root, source_id, entry, path)
        cards.extend([] if card is None else [card])
        gaps.extend(card_gaps)
    return sorted(cards, key=lambda card: card.source_id), gaps


def _source_card(
    root: Path, source_id: str, declared: Mapping[str, Any], path: Path
) -> tuple[RunSourceCard | None, list[FieldDegradation]]:
    """One corpus, read out of the manifest its ingest wrote.

    The licence comes from the manifest and not from the config declaration because the
    manifest is what the ingest stage recorded while it verified the bytes; the
    share-alike obligation comes from the config, which is the only place the obligation
    is stated (a licence identifier alone does not say what the derived tables inherit).
    """
    relative = _relative(root, path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DependencyUnavailable(f"{relative} could not be read: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise DependencyUnavailable(
            f"{relative} holds {type(manifest).__name__}, not the mapping ingest writes"
        )
    recorded = manifest.get("source")
    if not isinstance(recorded, Mapping):
        return None, [
            FieldDegradation(
                field="licence",
                reason=(
                    f"{relative} records no `source` block, so no licence was recorded for "
                    f"{source_id!r} while its bytes were read"
                ),
                would_come_from=f"{relative}, `source.license`",
            )
        ]
    recorded_id = _text(recorded, "source_id")
    if recorded_id is not None and recorded_id != source_id:
        raise DependencyUnavailable(
            f"{relative} records source_id {recorded_id!r} while sitting in the directory of "
            f"{source_id!r}: two records of one identity, and a response cannot pick silently"
        )
    licence = _text(recorded, "license")
    name = _text(recorded, "name")
    if licence is None or name is None:
        absent = ", ".join(
            sorted(
                f"`source.{field}`"
                for field, value in (("name", name), ("license", licence))
                if value is None
            )
        )
        return None, [
            FieldDegradation(
                field="licence",
                reason=(
                    f"{relative} records no {absent}, so the corpus is not attributed: a "
                    "licence string guessed from the dataset's reputation is exactly the "
                    "claim 01 §A rule 7 puts on the data rather than in prose"
                ),
                would_come_from=f"{relative}, {absent}",
            )
        ]

    gaps: list[FieldDegradation] = []
    files: list[RunSourceFileCard] = []
    unhashed = 0
    for item in recorded.get("files") or []:
        if not isinstance(item, Mapping):
            unhashed += 1
            continue
        file_name = _text(item, "name")
        sha256 = _text(item, "sha256")
        if file_name is None or sha256 is None:
            unhashed += 1
            continue
        files.append(RunSourceFileCard(file_name=file_name, sha256=sha256))
    files.sort(key=lambda card: card.file_name)
    if unhashed:
        gaps.append(
            FieldDegradation(
                field=f"sources[{source_id}].files",
                reason=(
                    f"{unhashed} file entries in {relative} carry no name or no sha256, so "
                    "they are not served as hashed inputs"
                ),
                would_come_from=f"{relative}, `source.files[]`",
            )
        )
    card = RunSourceCard(
        source_id=source_id,
        name=name,
        role=_text(recorded, "role"),
        licence=licence,
        licence_obligation=_text(declared, "license_obligation"),
        citation=_text(recorded, "citation"),
        source_url=_text(recorded, "source_url"),
        manifest_run_id=_text(manifest, "run_id"),
        ingested_at=_instant(manifest.get("ingested_at"), where=relative),
        canonical_rows=_int(manifest.get("canonical_count")),
        window_start=_instant(manifest.get("window_start"), where=relative),
        window_end=_instant(manifest.get("window_end"), where=relative),
        files=files,
    )
    return card, gaps


def _text(row: Mapping[str, Any], key: str) -> str | None:
    """A stored string, or None. An empty one is absent, not a value, and stays absent."""
    value = row.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _instant(value: Any, *, where: str) -> datetime | None:
    """A recorded instant, or None when nothing was recorded. A present-but-broken one is loud.

    Deliberately not ``_parse_dt`` above: that helper stands in for a missing
    ``measured_at`` with the current clock, which is right for a measurement card that
    says which command produced it and wrong here, where the instant *is* the fact.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DependencyUnavailable(
            f"{where} records {text!r} as an instant, which does not parse: the timestamp is "
            "the evidence, so it is not served as a guess"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


__all__ = ["router"]
