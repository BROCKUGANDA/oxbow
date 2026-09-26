"""One ingest run: admitted sources, adapters, artifacts, and the numbers that get printed.

The CLI is the composition root and this module is the stage it calls. Nothing here
imports an adapter: the sink arrives as an injected object whose methods return plain
dicts, so the stage computes and reports while the landing of bytes stays on the other
side of the boundary (02 §A). No print statement lives here either — the report carries
the numbers and the CLI renders them, so a test asserts on the same object the operator
reads.

FOUR THINGS THIS MODULE IS ANSWERABLE FOR.

* **Admission.** A source is read only if it is declared in ``config/sources.yaml``
  and its declaration permits ingestion (01 §B: "ingest REFUSES any source not declared
  here"). A refusal is returned with a named reason, never as a silent skip: a source
  that quietly disappeared from a run is the blind spot 02 §D is about.
* **Streaming.** PaySim arrives batch by batch through ``on_batch``, and each batch is
  written and then dropped. Holding six million canonical rows beside a
  six-million-row raw frame is how a run becomes a paging stall on this host, and the
  aggregate counters do not need the rows resident to be exact.
* **Fail-closed exit.** Any quarantine record, any empty batch, or any source that
  produced no events makes the run failed. A run that lost rows without saying so is
  indistinguishable downstream from a run that legitimately had none.
* **Printed numbers are the artifact's numbers.** Row counts and base rates accumulate
  over the batches that were landed, and the same values go into the run manifest, so
  the printed table and ``data/interim/<source>/run_manifest.json`` cannot tell two
  different stories.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol
from zoneinfo import ZoneInfo

import polars as pl

from oxbow.contracts.canonical_v1 import (
    PERSISTED_CANONICAL_COLUMNS,
    assert_balance_provenance,
    assert_label_provenance,
    utc_now_us,
)
from oxbow.ingest.canonical import (
    ACCOUNT_KEY_LENGTH,
    CanonicalizationError,
    RunIdentity,
    new_run_identity,
)

if TYPE_CHECKING:
    from oxbow.ingest.paysim import IngestResult, QuarantineRecord

SOURCES_FILENAME: Final = "sources.yaml"
RAW_DIRNAME: Final = "raw"

# A declared file entry whose hash was never recorded. ``config/sources.yaml`` keeps
# this marker on the two IBM members the served slug does not actually contain, so an
# entry carrying it is an unresolved declaration rather than a file to read.
PLACEHOLDER_SHA: Final = "RECORDED_AT_DOWNLOAD"

# The label columns whose prevalence is reported as a base rate, taken from the
# contract's own names so a new label column is reported the moment it joins canonical v1.
LABEL_RATE_COLUMNS: Final[tuple[str, ...]] = ("label_is_fraud", "label_is_flagged")

# Per-corpus roles that make a source readable. ``cite_only`` exists precisely so that
# a corpus is never read (Elliptic is CC BY-NC-ND: a derived sample violates the licence).
INGESTABLE_ROLES: Final[frozenset[str]] = frozenset({"primary", "secondary"})

# The corpus whose canonical rows carry typologies. PaySim has no typology taxonomy at
# all, so a non-null ``label_typology`` there is an invented label (DEV-013, DEV-014).
TYPOLOGY_BEARING_SOURCES: Final[frozenset[str]] = frozenset({"ibmaml"})


class BatchWriter(Protocol):
    """What the stage needs of the sink. Every return is a plain dict, so nothing in
    ``ingest/`` has to know the sink's types; the values go straight into the manifest.
    """

    def write_batch(
        self, source_dataset: str, batch_id: str, events: pl.DataFrame
    ) -> dict[str, object]: ...

    def write_quarantine(
        self, source_dataset: str, batch_id: str, records: Sequence[QuarantineRecord]
    ) -> str: ...

    def write_run_manifest(self, source_dataset: str, payload: Mapping[str, object]) -> str: ...

    def register_views(
        self,
        *,
        canonical_files: Mapping[str, Sequence[str]],
        quarantine_files: Sequence[str],
    ) -> list[dict[str, object]]: ...


class SourceDeclarationError(RuntimeError):
    """Raised when ``config/sources.yaml`` cannot be honoured as written."""


@dataclass(frozen=True, slots=True)
class DeclaredSource:
    """One entry from ``config/sources.yaml``, as ingestion needs to see it."""

    source_id: str
    name: str
    role: str
    ingest_allowed: bool
    files: tuple[tuple[str, str], ...]
    license: str
    citation: str
    source_url: str

    @property
    def readable(self) -> bool:
        return self.ingest_allowed and self.role in INGESTABLE_ROLES

    def refusal_reason(self) -> str:
        """Why this declared source is never read. Empty when it is readable."""
        if not self.ingest_allowed:
            return "ingest_allowed is false in config/sources.yaml"
        if self.role not in INGESTABLE_ROLES:
            return f"role {self.role!r} is declared for citation only and is never ingested"
        return ""

    def as_dict(self) -> dict[str, object]:
        """Lineage for the manifest: licence and citation travel with the data."""
        return {
            "source_id": self.source_id,
            "name": self.name,
            "role": self.role,
            "source_url": self.source_url,
            "license": self.license,
            "citation": self.citation,
            "files": [{"name": name, "sha256": sha} for name, sha in self.files],
        }


@dataclass(slots=True)
class SourceRun:
    """What happened to one source in one run, and the artifacts it produced."""

    source: DeclaredSource
    raw_path: str = ""
    rows_read: int = 0
    canonical_count: int = 0
    quarantine_count: int = 0
    duplicates_dropped: int = 0
    money_texts_expanded: int = 0
    rounding_decisions: int = 0
    label_counts: dict[str, int] = field(default_factory=dict)
    base_rates: dict[str, str] = field(default_factory=dict)
    typology_nulls: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    batches: list[dict[str, object]] = field(default_factory=list)
    quarantine_files: list[str] = field(default_factory=list)
    quarantine_reasons: dict[str, int] = field(default_factory=dict)
    views: list[dict[str, object]] = field(default_factory=list)
    manifest_path: str = ""
    failures: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def failed(self) -> bool:
        return bool(self.failures) or self.quarantine_count > 0

    def as_dict(self, run_id: str, ingested_at: datetime) -> dict[str, object]:
        """The manifest body. Every number here is the number that was printed."""
        return {
            "run_id": run_id,
            "ingested_at": ingested_at.isoformat(),
            "source": self.source.as_dict(),
            "raw_path": self.raw_path,
            "rows_read": self.rows_read,
            "canonical_count": self.canonical_count,
            "quarantine_count": self.quarantine_count,
            "duplicates_dropped": self.duplicates_dropped,
            "money_texts_expanded": self.money_texts_expanded,
            "rounding_decisions": self.rounding_decisions,
            "label_counts": dict(sorted(self.label_counts.items())),
            "base_rates": dict(sorted(self.base_rates.items())),
            "typology_nulls": self.typology_nulls,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "quarantine_reasons": dict(sorted(self.quarantine_reasons.items())),
            "quarantine_files": sorted(self.quarantine_files),
            "batches": list(self.batches),
            "duckdb_views": list(self.views),
            "failures": list(self.failures),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "columns_written": list(PERSISTED_CANONICAL_COLUMNS),
            "sidecar_columns_in_manifest_only": ["ingested_at", "run_id"],
        }


@dataclass(slots=True)
class RunReport:
    """Every source run in one pass, plus the run identity that stamps them."""

    run_id: str
    ingested_at: datetime
    runs: list[SourceRun] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return not self.runs or any(source_run.failed for source_run in self.runs)

    def failure_messages(self) -> list[str]:
        """The reasons a run is red, in source order, each naming its own source."""
        messages: list[str] = []
        for source_run in self.runs:
            messages.extend(source_run.failures)
            if source_run.quarantine_count:
                messages.append(
                    f"{source_run.source.source_id}: {source_run.quarantine_count} row(s) "
                    f"quarantined, by reason {dict(sorted(source_run.quarantine_reasons.items()))}"
                )
        if not self.runs:
            messages.append("no source was ingested; an empty run is not a successful one")
        return messages


def declared_sources(config_dir: Path) -> dict[str, DeclaredSource]:
    """Read every declared source from ``config/sources.yaml``, in declaration order.

    The file is the allowlist (01 §B), so this is the only place a source id can come
    from. That is what makes ``--source madeup`` a refusal rather than a zero-row run.
    """
    from oxbow.config import load_yaml

    path = config_dir / SOURCES_FILENAME
    raw = load_yaml(path)
    entries = raw.get("sources")
    if not isinstance(entries, list) or not entries:
        raise SourceDeclarationError(f"{path} declares no sources; ingestion has no allowlist")
    declared: dict[str, DeclaredSource] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise SourceDeclarationError(f"{path} has a sources entry that is not a mapping")
        source_id = str(entry.get("id", ""))
        if not source_id:
            raise SourceDeclarationError(f"{path} has a source with no id")
        if source_id in declared:
            raise SourceDeclarationError(f"{path} declares source {source_id!r} twice")
        files = entry.get("files") or []
        resolved: list[tuple[str, str]] = []
        for item in files if isinstance(files, list) else []:
            if isinstance(item, dict) and item.get("name"):
                resolved.append((str(item["name"]), str(item.get("sha256", ""))))
        declared[source_id] = DeclaredSource(
            source_id=source_id,
            name=str(entry.get("name", source_id)),
            role=str(entry.get("role", "")),
            ingest_allowed=bool(entry.get("ingest_allowed", True)),
            files=tuple(resolved),
            license=str(entry.get("license", "")),
            citation=str(entry.get("citation", "")),
            source_url=str(entry.get("source_url", "")),
        )
    return declared


def raw_file_for(source: DeclaredSource, root: Path) -> Path:
    """The first declared file present on disk with a recorded hash.

    Declaration order is the priority, so ``config/sources.yaml`` stays the authority on
    which member of a bundle is the transaction file. An entry still carrying
    ``RECORDED_AT_DOWNLOAD`` is skipped: a file whose hash was never recorded is a file
    nobody has read, and guessing at a filename is how the first IBM contract in this
    repo came to describe fourteen columns that do not exist (DEV-013).
    """
    directory = root / "data" / RAW_DIRNAME / source.source_id
    attempted: list[str] = []
    for name, sha in source.files:
        attempted.append(name)
        if not sha or sha == PLACEHOLDER_SHA:
            continue
        path = directory / name
        if path.is_file():
            return path
    raise SourceDeclarationError(
        f"source {source.source_id!r}: none of the declared files {attempted} is present "
        f"under {directory} with a recorded SHA-256. Run `make data` first; ingest will not "
        "read an undeclared path."
    )


def base_rate(count: int, total: int, *, label: str) -> str:
    """A prevalence percentage to four decimal places, computed on Decimals.

    A ratio rather than money, so the integer-minor-unit rule does not bind — but it is
    still not a float division, because the dataset card has to quote this exact string
    and a number printed from a binary float cannot be re-derived by hand.
    """
    if total <= 0:
        raise CanonicalizationError(f"{label}: base rate over {total} rows is undefined")
    percent = (Decimal(count) * Decimal(100) / Decimal(total)).quantize(Decimal("0.0001"))
    return f"{percent} %"


def _fold_batch(state: SourceRun, result: IngestResult, writer: BatchWriter | None) -> None:
    """Fold one batch into the running counters, and land it when a sink was supplied.

    The order matters: the counters read the frame the batch produced, and the artifact
    is written from that same frame, so a mismatch between printed rows and landed rows
    is not expressible.
    """
    events = result.events
    if events.height:
        projection = events.select(list(PERSISTED_CANONICAL_COLUMNS))
        sums = projection.select(
            *[
                pl.col(column).sum().cast(pl.Int64).alias(column)
                for column in LABEL_RATE_COLUMNS
            ],
            pl.col("label_typology").null_count().cast(pl.Int64).alias("typology_nulls"),
        ).row(0, named=True)
        for column in LABEL_RATE_COLUMNS:
            state.label_counts[column] = state.label_counts.get(column, 0) + int(sums[column] or 0)
        state.typology_nulls += int(sums["typology_nulls"] or 0)
        state.canonical_count += projection.height
        if state.window_start is None:
            state.window_start = result.window_start
        if result.window_end is not None:
            state.window_end = result.window_end
        assert_label_provenance(
            projection,
            source_carries_typology=state.source.source_id in TYPOLOGY_BEARING_SOURCES,
        )
        assert_balance_provenance(projection)
        if writer is not None:
            state.batches.append(
                writer.write_batch(state.source.source_id, result.batch_id, projection)
            )
    if result.quarantined and writer is not None:
        state.quarantine_count += len(result.quarantined)
        for record in result.quarantined:
            state.quarantine_reasons[record.reason] = (
                state.quarantine_reasons.get(record.reason, 0) + 1
            )
        state.quarantine_files.append(
            writer.write_quarantine(state.source.source_id, result.batch_id, result.quarantined)
        )


def _finish_rates(state: SourceRun) -> None:
    """Turn the accumulated label counts into the printed base rates."""
    total = state.canonical_count
    if total == 0:
        return
    for column, count in sorted(state.label_counts.items()):
        state.base_rates[column] = base_rate(count, total, label=column)
    state.base_rates["label_typology_null"] = base_rate(
        state.typology_nulls, total, label="label_typology_null"
    )


def _count_inline_quarantine(state: SourceRun, records: Sequence[QuarantineRecord]) -> None:
    """Count quarantines when no sink was supplied, so nothing could be landed."""
    state.quarantine_count += len(records)
    for record in records:
        state.quarantine_reasons[record.reason] = state.quarantine_reasons.get(record.reason, 0) + 1


def run_paysim(
    source: DeclaredSource,
    *,
    root: Path,
    config: Mapping[str, object],
    identity: RunIdentity,
    limit: int | None,
    batch_rows: int,
    ingested_at: datetime,
    writer: BatchWriter | None,
    state: SourceRun,
) -> None:
    """Ingest PaySim, folding each batch into ``state`` as it lands.

    ``writer=None`` runs the identical computation and lands nothing, which is how the
    contract tests exercise the stage without writing to the repository.

    The adapter is imported here rather than at module scope on purpose: one corpus's
    adapter being unimportable must not stop the other corpus from being read. The
    failure belongs to the source it came from, and that is where it gets reported.
    """
    from oxbow.ingest.paysim import ingest_paysim

    block = _section(config, "paysim")
    offset = block["intra_step_offset"]
    if not isinstance(offset, dict):
        raise SourceDeclarationError("config/pipeline.yaml paysim.intra_step_offset is missing")
    path = raw_file_for(source, root)
    state.raw_path = path.as_posix()
    epoch = datetime.fromisoformat(str(block["epoch_utc"]).replace("Z", "+00:00")).astimezone(UTC)
    ingest_block = _section(config, "ingest")
    callback = (lambda result: _fold_batch(state, result, writer)) if writer is not None else None
    result = ingest_paysim(
        path,
        identity,
        deployment_tz=ZoneInfo(str(config["deployment_timezone"])),
        epoch_utc=epoch,
        step_hours=int(block["step_hours"]),
        offset_modulus_us=int(offset["modulus_us"]),
        offset_salt=str(offset["salt"]),
        batch_rows=batch_rows,
        limit=limit,
        future_tolerance_hours=int(ingest_block["future_timestamp_tolerance_hours"]),
        ingested_at=ingested_at,
        on_batch=callback,
        keep_events=writer is None,
    )
    if writer is None:
        _fold_batch(state, result, None)
        _count_inline_quarantine(state, result.quarantined)
    state.rows_read = result.rows_read
    state.duplicates_dropped = result.duplicates_dropped
    state.money_texts_expanded = result.scientific_amounts_expanded
    state.rounding_decisions = result.rounding_applied
    if state.window_start is None:
        state.window_start = result.window_start
        state.window_end = result.window_end


def run_ibm_aml(
    source: DeclaredSource,
    *,
    root: Path,
    config: Mapping[str, object],
    identity: RunIdentity,
    limit: int | None,
    batch_rows: int,
    ingested_at: datetime,
    writer: BatchWriter | None,
    state: SourceRun,
) -> None:
    """Ingest IBM-AML through its own adapter, or report the refusal its bytes deserve.

    The adapter checks the declared header before parsing a single row and returns a
    quarantine record rather than raising, so a contract that does not match the file
    arrives here as one ``schema_mismatch`` naming both headers. That is reported as a
    failure and nothing is written: the alternative would be a canonical artifact built
    from a fiction, and DECISIONS DEV-013 already records which fourteen columns those
    were.

    Imported inside the function for the same reason as PaySim's adapter, and the reason
    is live rather than theoretical: while ``contracts/raw_ibm_aml.py`` is being corrected
    against the real header, this module's names do not resolve, and a PaySim run must not
    inherit that as an ImportError.
    """
    from oxbow.ingest.ibm_aml import ingest_ibm_aml, policy_from_config

    block = _section(config, "ibmaml")
    path = raw_file_for(source, root)
    state.raw_path = path.as_posix()
    deployment_tz = str(config["deployment_timezone"])
    result = ingest_ibm_aml(
        path,
        identity,
        run_id=identity.run_id,
        deployment_tz=ZoneInfo(deployment_tz),
        policy=policy_from_config(block, repo_root=root, deployment_timezone=deployment_tz),
        batch_rows=batch_rows,
        limit=limit,
        ingested_at=ingested_at,
    )
    state.rows_read = result.rows_read
    state.duplicates_dropped = result.duplicates_dropped
    state.money_texts_expanded = result.scientific_amounts_expanded
    state.rounding_decisions = result.rounding_applied
    _fold_batch(state, result, writer)
    if writer is None and result.quarantined:
        _count_inline_quarantine(state, result.quarantined)
    if state.canonical_count == 0 and not state.failures:
        detail = result.quarantined[0].detail if result.quarantined else "no rows were read"
        # Long enough to carry both header lists whole: the mismatch between a declared
        # header and a measured one is the message, and truncating it mid-column is the
        # worst place to cut.
        state.failures.append(f"{source.source_id}: produced no canonical events: {detail[:900]}")


ADAPTERS: Final[dict[str, Callable[..., None]]] = {
    "paysim": run_paysim,
    "ibmaml": run_ibm_aml,
}


def _section(config: Mapping[str, object], name: str) -> dict[str, object]:
    """One config block, or a failure that names it. No defaults are substituted."""
    block = config.get(name)
    if not isinstance(block, dict):
        raise SourceDeclarationError(f"config/pipeline.yaml has no `{name}` block")
    return {str(key): value for key, value in block.items()}


def resolve_scope(
    requested: Sequence[str], declared: Mapping[str, DeclaredSource], *, default: Sequence[str]
) -> list[DeclaredSource]:
    """Turn ``--source`` into the sources this run will read.

    ``all`` means every declared, ingestion-allowed source. An unknown id is refused by
    name, and so is a declared-but-cite-only id: both fail loudly, because a typo in a
    source id that silently read nothing looks exactly like a source with no rows today.
    """
    wanted = list(requested or default)
    if "all" in wanted:
        scope = [source_id for source_id, source in declared.items() if source.readable]
    else:
        scope = []
        for item in wanted:
            if item not in declared:
                raise SourceDeclarationError(
                    f"source {item!r} is not declared in config/sources.yaml. Ingest refuses "
                    "any source not declared there (01 §B): add it with a licence, a "
                    "citation and a measured SHA-256, or name one that already exists."
                )
            scope.append(item)
    selected: list[DeclaredSource] = []
    for source_id in scope:
        source = declared[source_id]
        if not source.readable:
            raise SourceDeclarationError(
                f"source {source_id!r} is declared but not ingestable: {source.refusal_reason()}"
            )
        if source_id not in ADAPTERS:
            raise SourceDeclarationError(
                f"source {source_id!r} is ingestable but no adapter is wired for it; "
                f"implemented ids are {sorted(ADAPTERS)}"
            )
        selected.append(source)
    return selected


def content_batch_id(source: DeclaredSource) -> str:
    """A stable 12-hex id naming one source's declaration for this run.

    ``RunIdentity`` requires a batch id and DEV-012 requires it content-derived rather
    than random. The per-batch ids written into the Parquet are digested from the rows
    by the adapter; this one only stamps the manifest, so it is digested from the
    declared filenames and hashes — a fact about the bytes rather than a wish about them.
    """
    digest = hashlib.sha256()
    digest.update(source.source_id.encode("utf-8"))
    for name, sha in source.files:
        digest.update(f"|{name}:{sha}".encode())
    return digest.hexdigest()[:ACCOUNT_KEY_LENGTH]


def run_ingest(
    *,
    root: Path,
    config: Mapping[str, object],
    run_salt: str,
    scope: Sequence[DeclaredSource],
    declared: Mapping[str, DeclaredSource],
    writer: BatchWriter | None = None,
    limit: int | None = None,
    batch_rows: int | None = None,
    run_id: str | None = None,
    ingested_at: datetime | None = None,
) -> RunReport:
    """Ingest every source in ``scope``, landing artifacts when a writer is supplied.

    One ``RunIdentity`` per source, all sharing one ``run_id``: the batch seed is
    content-derived from that source's declaration, so two corpora in one run do not
    contend for the same id, and the run id stays the single lineage key the manifest
    and the warehouse tables join on (DEV-003).
    """
    stamp = ingested_at or utc_now_us()
    report = RunReport(
        run_id=run_id or "",
        ingested_at=stamp,
        refused=[
            (source_id, source.refusal_reason())
            for source_id, source in declared.items()
            if not source.readable
        ],
    )
    if not scope:
        return report
    ingest_block = _section(config, "ingest")
    rows_per_batch = batch_rows if batch_rows is not None else int(ingest_block.get("batch_rows", 100_000))
    if rows_per_batch <= 0:
        raise SourceDeclarationError("ingest.batch_rows must be positive")
    if bool(ingest_block.get("allow_silent_coercion", False)):
        raise SourceDeclarationError(
            "ingest.allow_silent_coercion is true. This stage quarantines or fails on every "
            "coercion; it will not run under a config that permits them silently."
        )
    for source in scope:
        adapter = ADAPTERS[source.source_id]
        identity = new_run_identity(run_salt, content_batch_id(source))
        report.run_id = report.run_id or identity.run_id
        state = SourceRun(source=source)
        started = time.perf_counter()
        try:
            adapter(
                source,
                root=root,
                config=config,
                identity=identity,
                limit=limit,
                batch_rows=rows_per_batch,
                ingested_at=stamp,
                writer=writer,
                state=state,
            )
        except (CanonicalizationError, SourceDeclarationError, ImportError) as exc:
            # ImportError is in scope deliberately: while another phase is correcting the
            # IBM raw contract its adapter does not import, and that belongs on the
            # source's own line rather than crashing the stage for every other corpus.
            state.failures.append(f"{source.source_id}: {type(exc).__name__}: {exc}")
        state.elapsed_seconds = time.perf_counter() - started
        _finish_rates(state)
        if state.rows_read == 0 and not state.failures:
            state.failures.append(
                f"{source.source_id}: read zero rows. ingest.allow_empty_batch is false, so a "
                "source that delivers nothing is an error and not a clean run"
            )
        elif state.canonical_count == 0 and not state.failures:
            state.failures.append(
                f"{source.source_id}: read {state.rows_read} row(s) and canonicalised none: "
                "ingest.allow_empty_batch is false, so this is an error, not a clean run"
            )
        if writer is not None:
            if state.batches:
                state.views = writer.register_views(
                    canonical_files={source.source_id: [str(b["path"]) for b in state.batches]},
                    quarantine_files=state.quarantine_files,
                )
            state.manifest_path = writer.write_run_manifest(
                source.source_id, state.as_dict(report.run_id, stamp)
            )
        report.runs.append(state)
    return report


__all__ = [
    "ADAPTERS",
    "LABEL_RATE_COLUMNS",
    "PLACEHOLDER_SHA",
    "BatchWriter",
    "DeclaredSource",
    "RunReport",
    "SourceDeclarationError",
    "SourceRun",
    "base_rate",
    "content_batch_id",
    "declared_sources",
    "raw_file_for",
    "resolve_scope",
    "run_ibm_aml",
    "run_ingest",
    "run_paysim",
]
