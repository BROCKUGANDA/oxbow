"""``GET /api/meta/run`` — the deployment's own state, served instead of remembered.

The header strip, the queue's timestamps and the validation page all read this payload:
timezone, currency, the economics assumptions verbatim, the corpora with their licences,
the model version, and whether what is on screen is demo data. Plan §14 puts those on
every screen because a figure rendered in an unknown zone, in an unknown currency, from
an unknown run, is not an auditable figure.

The rule this module exists to keep is narrower than the route, and stricter: **every
field is either a value the server measured or ``null`` with a reason in
``degradations``.** ``apps/web/src/lib/api/contract.ts`` decodes ``run_id``,
``dataset``, ``licence`` and ``model_version`` as *nullable* strings precisely so a
deployment with no run can say so; a route that padded them with ``""`` or ``0`` would
turn "this warehouse has no run record" into "this run's licence is called nothing", and
the second claim is the one a reader cannot dispute.

``spelling``: the per-source card calls its field ``licence`` rather than the config
file's ``license``, because within this one response the top-level contract field and
the list beside it must read the same way. ``/api/meta/dataset`` keeps the config
spelling for its own card, and this is not a rename of that route.
"""

from __future__ import annotations

from datetime import datetime
from typing import Union

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.catalog import RunStateName

# One value in the client's `Record<string, string | number>` for the economics map:
# an integer amount, a float rate, or the string form of something the record cannot
# hold structurally (a list joined with ", ", a boolean as "true"/"false").
EconomicsScalar = Union[int, float, str]  # noqa: UP007 - pydantic needs the typing form


class RunSourceFileCard(BaseModel):
    """One input file, hashed by the ingest that read it."""

    model_config = ConfigDict(extra="forbid")

    file_name: str
    sha256: str = Field(
        description="Recorded at verification time by the ingest run, not declared."
    )


class RunSourceCard(BaseModel):
    """One corpus this deployment actually ingested, as its run manifest recorded it.

    The values are copied out of ``data/interim/<source>/run_manifest.json``, which is
    the artifact the ingest stage writes at the moment it verifies the bytes. A source
    that is declared in ``config/sources.yaml`` but has no manifest is not listed here;
    it is reported by ``/api/meta/dataset`` as a declaration, which is what it is.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str
    name: str
    role: str | None
    licence: str
    licence_obligation: str | None = Field(
        default=None,
        description="The share-alike wording config/sources.yaml attaches to this licence, when it "
        "declares one. Null means unstated, never an empty obligation.",
    )
    citation: str | None
    source_url: str | None
    manifest_run_id: str | None = Field(
        default=None,
        description="The run id the manifest itself records. It is served as-is rather than "
        "assumed equal to this response's run_id: the corpora are ingested by their own runs, and "
        "a route that quietly matched them would be claiming a lineage the artifact does not state.",
    )
    ingested_at: datetime | None = None
    canonical_rows: int | None = Field(
        default=None, description="Rows written to the canonical table for this corpus."
    )
    window_start: datetime | None = None
    window_end: datetime | None = None
    files: list[RunSourceFileCard] = Field(default_factory=list)


class FieldDegradation(BaseModel):
    """Why one nullable field on this response is ``null``, naming the field.

    DESIGN.md §5 forbids an indefinite spinner and requires "degraded, not broken"; the
    client renders the null arm of each field, and this is the operator-facing record of
    what would have been there and from where.
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    reason: str
    would_come_from: str = Field(
        description="The artifact or table that holds this value when it exists."
    )


class RuntimeRun(BaseModel):
    """The payload of ``GET /api/meta/run``.

    The first ten fields are exactly the set
    ``apps/web/src/lib/api/contract.ts::RuntimeMetaDecoder`` names, in that order and no
    others: the client's object decoder drops any key it did not agree to, so the rest of
    this model is for an operator reading the response rather than for a component.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = Field(
        default=None,
        description="The newest complete run, or the one named by ?run_id. Null only when this "
        "warehouse holds no such run, which the header renders as 'no run recorded'.",
    )
    deployment_timezone: str = Field(
        description="IANA zone name. Always present: the run record carries it once a run exists, "
        "and before that config/pipeline.yaml declares the zone this deployment ingests in."
    )
    currency: str = Field(description="ISO 4217 code from config/economics.yaml.")
    minor_units_per_major: int = Field(ge=1, description="Minor units per major unit, from config.")
    economics_source: str = Field(
        description="The file every money figure on this response traces to, named as a repo path."
    )
    economics: dict[str, EconomicsScalar] = Field(
        description="config/economics.yaml flattened to dotted keys, in ascending key order. "
        "Integer minor units stay integers; a list is the comma-joined string of its members."
    )
    dataset: str | None = Field(
        default=None,
        description="The corpora this deployment ingested, joined from each manifest's own name. "
        "Null when no run manifest exists, because a dataset name with no artifact behind it is a "
        "claim about bytes this server has never read.",
    )
    licence: str | None = Field(
        default=None,
        description="The same join over each manifest's recorded licence. 01 §A rule 7 makes "
        "attribution a property of the data, so this is copied from the ingest record.",
    )
    model_version: str | None = Field(default=None, description="From the run record.")
    demo_data: bool = Field(
        description="True when the served run's provenance is not 'pipeline' — i.e. the numbers "
        "beside it came from a fixture or a demo snapshot. With no run at all nothing is being "
        "served, so the flag is false and run_id is the field that says why."
    )

    # --- the run record beside the contract fields --------------------------
    run_state: RunStateName | None = Field(default=None, description="The run row's own state.")
    provenance: str | None = Field(
        default=None,
        description="The run row's provenance, verbatim: pipeline | fixture | demo_snapshot.",
    )
    seed: int | None = Field(
        default=None, description="The run's seed; reproducibility, not decoration."
    )
    config_hash: str | None = Field(
        default=None,
        description="run.config_hash — the digest of the config directory that produced it.",
    )
    artifact_hashes: dict[str, str] | None = Field(
        default=None,
        description="run.artifact_hashes when the run recorded any. Null means the column holds "
        "nothing, which is a different fact from an empty map the route invented.",
    )
    timezone_source: str = Field(
        description="Which record this response read deployment_timezone from, so a reader can tell "
        "a measured zone from a declared one."
    )
    created_at: datetime | None = None
    finished_at: datetime | None = None
    warehouse_backend: str = Field(
        description="postgres or null-file, from the container that is answering."
    )
    sources: list[RunSourceCard] = Field(
        default_factory=list,
        description="Ascending by source_id — a total order, not directory order.",
    )
    degradations: list[FieldDegradation] = Field(
        default_factory=list,
        description="Ascending by field name. One entry per nullable field this deployment could "
        "not fill, naming the artifact that would hold it.",
    )


__all__ = [
    "EconomicsScalar",
    "FieldDegradation",
    "RunSourceCard",
    "RunSourceFileCard",
    "RuntimeRun",
]
