"""``oxbow eval`` — regenerate every published number, render the docs from them, verify the card.

Plan §15: "Docs: README, ARCHITECTURE, LIMITATIONS, MODEL_CARD, ECONOMICS_CARD,
DATASET_CARD — rendered from `make eval` output, not written from intent." This module
makes that mechanical for the five documents whose every sentence is derivable: it reads
the artifacts the pipeline actually wrote, assembles them into ``data/processed/eval.json``,
then renders those documents **from that JSON**. Nothing here restates what the build was
supposed to achieve; every figure carries a ``source:`` pointer into a file on disk, and
every section whose artifact does not exist yet says so by name.

The dataset card is the exception, and plan §15's sentence cannot survive contact with it.
A card carries authored judgement — what a label means, which licence binds a derivative,
what was refused and why, which corpus owns which module — and a generator can only
reproduce what it can derive, so rendering it destroyed the parts that mattered. An
earlier ``make eval`` overwrote the authored card and dropped the PaySim reuse figure
that decided the architecture. ``oxbow eval`` therefore *verifies* the card, figure by
figure, through :mod:`oxbow.dataset_card`, and never writes it.

Four invariants, enforced rather than intended:

1. **A number without a source is a bug.** Every published value is a :class:`Metric`
   carrying ``source`` — a path plus a JSON pointer or a config key — so "where did
   that come from" is answered inside the document, and a stale doc is detectable from
   the artifact digests it prints.
2. **A missing artifact fails by name; it never gets a plausible default.** Required
   artifacts (the corpus measurements, the typology join, the download manifest) abort
   the run if absent. Not-yet-produced artifacts (P4b's scored rows, P5's capacity
   sweep, P7's audit chain) render a line naming the path. Emitting a number for a run
   that has not happened is plan §18's rejection trigger, and it is also the one
   failure mode that survives review, because the document looks complete.
3. **Provenance travels with the number.** The backtest artifacts on this host were
   produced by ``oxbow.backtest.fakes`` — a hand-computed harness self-check, not a
   corpus result — and say so in their own ``provenance`` field. That field is copied
   onto every metric derived from them and printed in every document. A harness that
   verifies itself is a result; a harness number dressed as a model result is a lie.
4. **An authored document is checked, not regenerated.** The card's figures are compared
   against the artifacts that own them, and a check whose artifact is absent reports
   SKIPPED with its reason rather than passing quietly.

Output is deterministic for unchanged artifacts: no wall-clock stamps (the dates
printed are the ones the measuring scripts recorded inside the artifacts), sorted keys,
and every list ordered by a documented key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import polars as pl
import yaml

from oxbow.dataset_card import CardAudit, verify_dataset_card
from oxbow.dataset_card import safe as _safe
from oxbow.packet.model import money_line
from oxbow.ports.case_sink import OXBOW_DISCLAIMER
from oxbow.quant.economics import (
    AssumptionBlock,
    Economics,
    assumption_block,
    load_economics,
)

EVAL_VERSION: Final = "oxbow-eval-v1"
EVAL_JSON_RELPATH: Final = "data/processed/eval.json"

#: Artifacts below this size are digest-hashed so a stale document is detectable from
#: its own provenance table. The raw corpora are hundreds of megabytes and are verified
#: by ``make data`` (``scripts/download_data.py --verify``) against the hashes recorded
#: in ``config/sources.yaml``; re-hashing them on every documentation run would make
#: ``make eval`` slower than the pipeline it documents.
HASH_SIZE_LIMIT_BYTES: Final = 32 * 1024 * 1024

#: Plan §15's README requirements beyond the metrics: the scenario-dressing statement
#: and the PaySim citation, both rendered from ``config/sources.yaml``.
SCENARIO_DRESSING: Final = (
    "Any East-African mobile-money framing in OXBOW's interface, copy or packets is "
    "illustrative scenario dressing over permitted public data, not a claim about any "
    "real institution, market or regulator. The currency codes and account-key formats "
    "come from the corpora and the de-identification rules; no real operator's data "
    "appears anywhere in this repository."
)

NO_NUMBER: Final = "not yet published"

PROVENANCE_MEASURED: Final = "measured"
PROVENANCE_RECORDED: Final = "recorded in DECISIONS.md"
PROVENANCE_CONFIG: Final = "declared in config/"
PROVENANCE_FAKE: Final = "fake_harness"

#: How many limitations plan §15 demands. Asserted by the tests, and the section
#: builder fails the run if it renders fewer.
MINIMUM_LIMITATIONS: Final = 8

# --------------------------------------------------------------------------
# metric plumbing
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metric:
    """One published number, plus where it came from and how far to trust it."""

    value: Any
    source: str
    provenance: str = PROVENANCE_MEASURED
    unit: str | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "value": self.value,
            "source": self.source,
            "provenance": self.provenance,
        }
        if self.unit:
            payload["unit"] = self.unit
        if self.note:
            payload["note"] = self.note
        return payload


def metric(
    value: Any,
    source: str,
    *,
    provenance: str = PROVENANCE_MEASURED,
    unit: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """A leaf in ``eval.json``: ``{"value", "source", "provenance", …}``.

    ``source`` is ``path#pointer`` — ``out/backtest/model_card.json#/headline/pr_auc`` —
    or a config key. It has to be resolvable by a human, which is the test for whether
    the number is a measurement or a memory.
    """
    return Metric(value, source, provenance, unit, note).to_json()


def from_artifact(
    document: Mapping[str, Any],
    pointer: str,
    *,
    artifact: str,
    unit: str | None = None,
    note: str | None = None,
    provenance: str | None = None,
) -> dict[str, Any]:
    """Read one value out of a loaded artifact and carry the artifact's provenance.

    A missing pointer is an error, not a blank: an artifact whose field moved means the
    document would otherwise print yesterday's number under today's label.
    """
    value: Any = document
    for part in pointer.split("/"):
        if not part:
            continue
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(
                f"{artifact}#{pointer} does not resolve at {part!r}. The artifact's shape "
                "moved under the generator, which is a stale document waiting to be "
                "published; this generator treats that as a failure."
            )
    resolved = provenance or (
        PROVENANCE_FAKE if _declares_fake(document, value) else PROVENANCE_MEASURED
    )
    return metric(value, f"{artifact}#{pointer}", provenance=resolved, unit=unit, note=note)


def _declares_fake(document: Mapping[str, Any], value: Any) -> bool:
    """Whether the artifact — or the value's own record — says it is a harness check."""
    if isinstance(value, Mapping) and str(value.get("provenance", "")) == PROVENANCE_FAKE:
        return True
    if "fake" in str(document.get("provenance_note", "")).lower():
        return True
    return str(document.get("provenance", "")) == PROVENANCE_FAKE


# --------------------------------------------------------------------------
# artifacts on disk
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Artifact:
    """A file the generator publishes from, and who is expected to produce it."""

    name: str
    relpath: str
    producer: str
    stage: str
    required: bool

    def path(self, root: Path) -> Path:
        return root / self.relpath


ARTIFACTS: Final[tuple[Artifact, ...]] = (
    Artifact(
        "paysim_measurement",
        "data/graph_measurement.json",
        "scripts/measure_graph.py",
        "P1a",
        True,
    ),
    Artifact(
        "ibm_graph_measurement",
        "data/ibm_graph_measurement.json",
        "scripts/measure_ibm_graph.py",
        "P1a",
        True,
    ),
    Artifact(
        "ibm_cycle_measurement",
        "data/ibm_cycle_measurement.json",
        "scripts/measure_ibm_cycles.py",
        "P3a",
        True,
    ),
    Artifact(
        "ibm_typologies",
        "data/processed/ibm_typologies.parquet",
        "scripts/build_ibm_typologies.py",
        "P1b",
        True,
    ),
    Artifact(
        "download_manifest",
        "data/download_manifest.json",
        "scripts/download_data.py",
        "P0",
        True,
    ),
    Artifact(
        "backtest_model_card",
        "out/backtest/model_card.json",
        "oxbow.backtest.run",
        "P6",
        False,
    ),
    Artifact(
        "backtest_ablation",
        "out/backtest/ablation_results.json",
        "oxbow.backtest.run",
        "P6",
        False,
    ),
    Artifact(
        "scored_rows",
        "out/p4/scored_rows.parquet",
        "oxbow.models via config/model.yaml :: reporting.artifact_dir",
        "P4b",
        False,
    ),
    Artifact(
        "drift_periods",
        "out/warehouse/drift_period",
        "oxbow.scoring.drift via the warehouse handoff",
        "P4b",
        False,
    ),
    Artifact(
        "capacity_curve",
        "out/warehouse/curve_point",
        "oxbow.quant.frontier via the warehouse handoff",
        "P5",
        False,
    ),
    Artifact(
        "audit_chain",
        "out/audit/audit.jsonl",
        "oxbow.adapters.file.audit.FileAuditSink",
        "P7",
        False,
    ),
    Artifact(
        "run_ledger",
        "out/warehouse/runs.jsonl",
        "oxbow.adapters.null.warehouse.NullWarehouse",
        "P7",
        False,
    ),
)

ARTIFACT_BY_NAME: Final = {artifact.name: artifact for artifact in ARTIFACTS}


class _Root:
    """The repo root for the artifact helpers, set once per run.

    A module-level mutable because threading it through nine section builders would be
    nine parameters that all say the same thing; it is assigned at the top of
    :func:`build_eval_payload` and nowhere else.
    """

    _path: Path | None = None

    def set(self, value: Path) -> None:
        type(self)._path = value

    def get(self) -> Path:
        if self._path is None:
            raise RuntimeError("`make eval` has not resolved a repository root yet")
        return self._path


_ROOT = _Root()


def _read_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} must hold a JSON object")
    return parsed


def _read_yaml(path: Path) -> dict[str, Any]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} must hold a mapping")
    return parsed


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_descriptors(root: Path) -> dict[str, dict[str, Any]]:
    """Existence, size and digest of every artifact the docs publish from.

    This is the staleness detector: the provenance table in each generated document
    carries the digest of the exact bytes its numbers came from, so a reader who re-runs
    ``make eval`` and gets a different digest knows the document in their hand is out of
    date whether or not any number visibly moved.
    """
    descriptors: dict[str, dict[str, Any]] = {}
    for artifact in ARTIFACTS:
        path = artifact.path(root)
        exists = path.exists()
        entry: dict[str, Any] = {
            "name": artifact.name,
            "path": artifact.relpath,
            "producer": artifact.producer,
            "stage": artifact.stage,
            "required": artifact.required,
            "exists": exists,
        }
        if exists and path.is_file():
            size = path.stat().st_size
            entry["bytes"] = size
            if size <= HASH_SIZE_LIMIT_BYTES:
                entry["sha256"] = _digest(path)
            else:
                entry["note"] = (
                    "not hashed by `make eval` (above the size limit); `make data` verifies "
                    "it against config/sources.yaml"
                )
        elif exists:
            files = sorted(item.name for item in path.iterdir() if item.is_file())
            entry["files"] = len(files)
            entry["sample"] = files[:3]
        descriptors[artifact.name] = entry
    return descriptors


def _load_available(root: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    for name, relpath in (
        ("paysim_measurement", "data/graph_measurement.json"),
        ("ibm_graph_measurement", "data/ibm_graph_measurement.json"),
        ("ibm_cycle_measurement", "data/ibm_cycle_measurement.json"),
        ("download_manifest", "data/download_manifest.json"),
        ("backtest_model_card", "out/backtest/model_card.json"),
        ("backtest_ablation", "out/backtest/ablation_results.json"),
    ):
        path = root / relpath
        loaded[name] = _read_json(path) if path.is_file() else None
    typologies = root / "data/processed/ibm_typologies.parquet"
    loaded["ibm_typologies"] = pl.read_parquet(typologies) if typologies.is_file() else None
    for config_name in ("sources", "pipeline", "model", "rules", "features", "splits"):
        path = root / "config" / f"{config_name}.yaml"
        loaded[f"config_{config_name}"] = _read_yaml(path) if path.is_file() else None
    return loaded


# --------------------------------------------------------------------------
# section builders
# --------------------------------------------------------------------------


def corpus_section(loaded: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """What is actually on disk: read by the measuring scripts, sized by this one."""
    paysim = loaded["paysim_measurement"]
    ibm = loaded["ibm_graph_measurement"]
    sources = loaded["config_sources"]
    declared = _source_entries(sources)
    files = _file_sizes(root, declared)
    manifest = loaded["download_manifest"]
    self_loops = int(ibm["n_self_loop_rows"])
    return {
        "paysim": {
            "file": metric(
                paysim["file"], "data/graph_measurement.json#/file", provenance=PROVENANCE_CONFIG
            ),
            "rows": metric(paysim["n_rows"], "data/graph_measurement.json#/n_rows", unit="rows"),
            "distinct_senders": metric(
                paysim["n_distinct_nameOrig"], "data/graph_measurement.json#/n_distinct_nameOrig"
            ),
            "distinct_receivers": metric(
                paysim["n_distinct_nameDest"], "data/graph_measurement.json#/n_distinct_nameDest"
            ),
            "measured_at": metric(
                paysim["measured_at"], "data/graph_measurement.json#/measured_at"
            ),
            "sha256_recorded": metric(
                _paysim_sha(manifest),
                "data/download_manifest.json#/paysim/files/0/sha256",
                provenance=PROVENANCE_RECORDED,
                note="verified by `make data` against config/sources.yaml",
            ),
            "bytes_on_disk": files.get(str(paysim["file"])),
            "license": metric(
                declared["paysim"]["license"],
                "config/sources.yaml#/sources/0/license",
                provenance=PROVENANCE_CONFIG,
            ),
            "license_obligation": metric(
                _flat(declared["paysim"]["license_obligation"]),
                "config/sources.yaml#/sources/0/license_obligation",
                provenance=PROVENANCE_CONFIG,
            ),
            "citation": metric(
                _flat(declared["paysim"]["citation"]),
                "config/sources.yaml#/sources/0/citation",
                provenance=PROVENANCE_CONFIG,
            ),
            "label_caveat": metric(
                _flat(declared["paysim"]["label_caveat"]),
                "config/sources.yaml#/sources/0/label_caveat",
                provenance=PROVENANCE_CONFIG,
            ),
            "known_biases": [
                metric(
                    _flat(bias),
                    f"config/sources.yaml#/sources/0/known_biases/{index}",
                    provenance=PROVENANCE_CONFIG,
                )
                for index, bias in enumerate(declared["paysim"]["known_biases"])
            ],
        },
        "ibmaml": {
            "bundle": metric(ibm["source"], "data/ibm_graph_measurement.json#/source"),
            "transaction_rows": metric(
                ibm["n_transaction_rows"],
                "data/ibm_graph_measurement.json#/n_transaction_rows",
                unit="rows",
            ),
            "distinct_accounts": metric(
                ibm["n_distinct_accounts"], "data/ibm_graph_measurement.json#/n_distinct_accounts"
            ),
            "directed_edges_excl_self_loops": metric(
                ibm["n_directed_edges_excl_self_loops"],
                "data/ibm_graph_measurement.json#/n_directed_edges_excl_self_loops",
                unit="edges",
            ),
            "self_loop_rows": metric(
                self_loops,
                "data/ibm_graph_measurement.json#/n_self_loop_rows",
                unit="rows",
                note=f"{_pct(self_loops, int(ibm['n_transaction_rows']))} of the corpus; "
                "excluded from cycle and fan evidence, kept as a feature (plan §7)",
            ),
            "laundering_rows": metric(
                ibm["laundering_rows"], "data/ibm_graph_measurement.json#/laundering_rows"
            ),
            "laundering_rate_pct": metric(
                ibm["laundering_rate_pct"],
                "data/ibm_graph_measurement.json#/laundering_rate_pct",
                unit="%",
            ),
            "n_currencies": metric(
                ibm["n_currencies"], "data/ibm_graph_measurement.json#/n_currencies"
            ),
            "currencies": metric(
                [[str(name), int(count)] for name, count in ibm["currencies"]],
                "data/ibm_graph_measurement.json#/currencies",
            ),
            "payment_formats": metric(
                [[str(name), int(count)] for name, count in ibm["payment_formats"]],
                "data/ibm_graph_measurement.json#/payment_formats",
            ),
            "temporal_range": metric(
                list(ibm["temporal_range"]), "data/ibm_graph_measurement.json#/temporal_range"
            ),
            "window_days": metric(
                _window_days(ibm["temporal_range"]),
                "data/ibm_graph_measurement.json#/temporal_range",
                unit="days",
                note="the whole acquired corpus spans this long",
            ),
            "schema_notes": [
                metric(
                    _flat(note),
                    f"data/ibm_graph_measurement.json#/notes/{index}",
                    provenance=PROVENANCE_CONFIG,
                )
                for index, note in enumerate(ibm["notes"])
            ],
            "measured_at": metric(
                ibm["measured_at_utc"], "data/ibm_graph_measurement.json#/measured_at_utc"
            ),
            "license": metric(
                declared["ibmaml"]["license"],
                "config/sources.yaml#/sources/1/license",
                provenance=PROVENANCE_CONFIG,
            ),
            "license_obligation": metric(
                _flat(declared["ibmaml"]["license_obligation"]),
                "config/sources.yaml#/sources/1/license_obligation",
                provenance=PROVENANCE_CONFIG,
            ),
            "citation": metric(
                _flat(declared["ibmaml"]["citation"]),
                "config/sources.yaml#/sources/1/citation",
                provenance=PROVENANCE_CONFIG,
            ),
            "label_caveat": metric(
                _flat(declared["ibmaml"]["label_caveat"]),
                "config/sources.yaml#/sources/1/label_caveat",
                provenance=PROVENANCE_CONFIG,
            ),
            "declared_files": metric(
                {
                    str(item["name"]): str(item["sha256"])[:16] + "…"
                    for item in declared["ibmaml"]["files"]
                },
                "config/sources.yaml#/sources/1/files",
                provenance=PROVENANCE_CONFIG,
            ),
            "files_on_disk": files,
        },
        "refused_sources": [
            metric(
                f"{item['id']} ({item['name']}): {_flat(item['reason'])}",
                f"config/sources.yaml#/refused/{index}",
                provenance=PROVENANCE_CONFIG,
            )
            for index, item in enumerate(sources["refused"])
        ],
        "cite_only": [
            metric(
                f"{item['name']} — {item['license']}: {_flat(item['license_obligation'])}",
                f"config/sources.yaml#/sources/{index}",
                provenance=PROVENANCE_CONFIG,
            )
            for index, item in enumerate(sources["sources"])
            if item.get("role") == "cite_only"
        ],
    }


def graph_thesis_section(loaded: Mapping[str, Any]) -> dict[str, Any]:
    """DEV-011 and DEV-013: the measurement that decided which corpus feeds which module."""
    paysim = loaded["paysim_measurement"]
    ibm = loaded["ibm_graph_measurement"]
    thresholds = paysim["thresholds"]
    top_sender = max(int(item["edges"]) for item in paysim["top20_senders"])
    return {
        "pass_condition": {
            "median_counterparty_degree_gt": metric(
                thresholds["median_counterparty_degree_gt"],
                "data/graph_measurement.json#/thresholds/median_counterparty_degree_gt",
                provenance=PROVENANCE_CONFIG,
            ),
            "cycles_min_count": metric(
                thresholds["cycles_min_count"],
                "data/graph_measurement.json#/thresholds/cycles_min_count",
                provenance=PROVENANCE_CONFIG,
            ),
        },
        "paysim": {
            "verdict": metric(paysim["verdict"], "data/graph_measurement.json#/verdict"),
            "median_total_degree": metric(
                paysim["degree_total"]["median"], "data/graph_measurement.json#/degree_total/median"
            ),
            "median_counterparty_degree": metric(
                paysim["counterparty_degree"]["median"],
                "data/graph_measurement.json#/counterparty_degree/median",
            ),
            "sender_reuse_ratio": metric(
                paysim["reuse_ratio"], "data/graph_measurement.json#/reuse_ratio"
            ),
            "max_total_degree": metric(
                paysim["degree_total"]["max"], "data/graph_measurement.json#/degree_total/max"
            ),
            "highest_sender_edges": metric(
                top_sender,
                "data/graph_measurement.json#/top20_senders",
                unit="edges",
                note="the most-repeated originator in the whole corpus",
            ),
            "cycles_in_sample": metric(
                paysim["cycles"]["cycles_found"],
                "data/graph_measurement.json#/cycles/cycles_found",
                note=f"{paysim['cycles']['sample_rows']:,}-row sample, lengths "
                f"{paysim['cycles']['min_len']}-{paysim['cycles']['max_len']}, retention floor "
                f"{paysim['cycles']['value_retention_min']}",
            ),
            "sample_rows": metric(
                paysim["cycles"]["sample_rows"], "data/graph_measurement.json#/cycles/sample_rows"
            ),
        },
        "ibmaml": {
            "median_degree": metric(
                ibm["degree_median"], "data/ibm_graph_measurement.json#/degree_median"
            ),
            "median_counterparties": metric(
                ibm["median_counterparties_per_account"],
                "data/ibm_graph_measurement.json#/median_counterparties_per_account",
            ),
            "accounts_over_2_counterparties": metric(
                ibm["accounts_with_more_than_2_counterparties"],
                "data/ibm_graph_measurement.json#/accounts_with_more_than_2_counterparties",
            ),
            "max_degree": metric(
                ibm["degree_max"],
                "data/ibm_graph_measurement.json#/degree_max",
                note="one rail: a hub with this many edges is payment infrastructure, not a mule",
            ),
            "degree_p90": metric(ibm["degree_p90"], "data/ibm_graph_measurement.json#/degree_p90"),
            "degree_p99": metric(ibm["degree_p99"], "data/ibm_graph_measurement.json#/degree_p99"),
            "pass_condition_met": metric(
                ibm["pass_condition_median_degree_gt_2"],
                "data/ibm_graph_measurement.json#/pass_condition_median_degree_gt_2",
            ),
        },
    }


def typologies_section(loaded: Mapping[str, Any]) -> dict[str, Any]:
    """DEV-014, measured live from the joined typology table."""
    frame: pl.DataFrame | None = loaded["ibm_typologies"]
    if frame is None:
        artifact = ARTIFACT_BY_NAME["ibm_typologies"]
        return {
            "status": "missing_artifact",
            "artifact": artifact.relpath,
            "produced_by": artifact.producer,
            "stage": artifact.stage,
        }
    counts = frame["typology"].value_counts().sort("typology")
    by_typology = {str(row["typology"]): int(row["count"]) for row in counts.to_dicts()}
    return {
        "status": "present",
        "annotated_transactions": metric(
            frame.height, "data/processed/ibm_typologies.parquet#rows"
        ),
        "attempt_blocks": metric(
            frame["attempt_id"].n_unique(),
            "data/processed/ibm_typologies.parquet#attempt_id(n_unique)",
        ),
        "typologies": metric(
            frame["typology"].n_unique(),
            "data/processed/ibm_typologies.parquet#typology(n_unique)",
        ),
        "by_typology": {
            name: metric(count, "data/processed/ibm_typologies.parquet#typology(value_counts)")
            for name, count in sorted(by_typology.items())
        },
        "cycle_rows": metric(
            by_typology.get("CYCLE", 0),
            "data/processed/ibm_typologies.parquet#typology=CYCLE",
        ),
        "fan_rows": metric(
            by_typology.get("FAN-OUT", 0) + by_typology.get("FAN-IN", 0),
            "data/processed/ibm_typologies.parquet#typology=FAN-*",
        ),
        "negative_control": metric(
            by_typology.get("RANDOM", 0),
            "data/processed/ibm_typologies.parquet#typology=RANDOM",
            note="planted laundering-attempt traffic with no typology: the population a rule "
            "that fires on a third of accounts would wrongly catch",
        ),
    }


def cycle_reality_section(loaded: Mapping[str, Any]) -> dict[str, Any]:
    """DEV-015: the spec'd cycle definition against the only labelled cycles there are."""
    cycles = loaded["ibm_cycle_measurement"]
    per_currency: Mapping[str, Any] = cycles["per_currency"]
    intact = {key: value for key, value in per_currency.items() if key.startswith("planted_")}
    total_intact = sum(int(value["cycles_found"]) for value in intact.values())
    alignment: Mapping[str, Any] = cycles["planted_join_alignment"]
    return {
        "command": metric(cycles["command"], "data/ibm_cycle_measurement.json#/command"),
        "measured_at": metric(
            cycles["measured_at_utc"], "data/ibm_cycle_measurement.json#/measured_at_utc"
        ),
        "total_cycles_found": metric(
            cycles["total_cycles_found"], "data/ibm_cycle_measurement.json#/total_cycles_found"
        ),
        "component_intact_cycles_found": metric(
            total_intact,
            "data/ibm_cycle_measurement.json#/per_currency/planted_component_intact",
            note=f"whole components over the {cycles['planted_cycle_accounts']} planted-cycle "
            "accounts plus their one-hop closure, so the sampler cannot sever the loops it "
            "is asked to count",
        ),
        "truncated": metric(
            any(bool(value["truncated"]) for value in intact.values()),
            "data/ibm_cycle_measurement.json#/per_currency",
            note="False rules out the search budget as the explanation",
        ),
        "pass_condition_non_trivial_cycle_count": metric(
            cycles["pass_condition_non_trivial_cycle_count"],
            "data/ibm_cycle_measurement.json#/pass_condition_non_trivial_cycle_count",
        ),
        "planted_join_alignment": metric(
            alignment["alignment_share"],
            "data/ibm_cycle_measurement.json#/planted_join_alignment/alignment_share",
            note=f"{alignment['matched_rows']} matched CYCLE rows, "
            f"{alignment['rows_with_laundering_flag']} of them laundering-labelled",
        ),
        "labelled_cycle_anatomy": {
            "labelled_blocks": metric(
                54, "DECISIONS.md :: DEV-015", provenance=PROVENANCE_RECORDED, unit="blocks"
            ),
            "close_as_directed_loops": metric(
                54, "DECISIONS.md :: DEV-015", provenance=PROVENANCE_RECORDED
            ),
            "cross_currency": metric(
                38,
                "DECISIONS.md :: DEV-015",
                provenance=PROVENANCE_RECORDED,
                note="excluded by the one-currency-per-loop rule until a declared "
                "reference-rate block lands",
            ),
            "below_retention_floor": metric(
                25,
                "DECISIONS.md :: DEV-015",
                provenance=PROVENANCE_RECORDED,
                note="excluded by config/pipeline.yaml :: graph.cycles.value_retention_floor",
            ),
            "not_time_monotonic": metric(
                5, "DECISIONS.md :: DEV-015", provenance=PROVENANCE_RECORDED
            ),
        },
    }


def model_section(loaded: Mapping[str, Any]) -> dict[str, Any]:
    """The validation page's content: headline, ablation, leakage control, fairness."""
    card = loaded["backtest_model_card"]
    ablation = loaded["backtest_ablation"]
    if card is None or ablation is None:
        return {
            "status": "missing_artifact",
            "artifact": "out/backtest/model_card.json + out/backtest/ablation_results.json",
            "produced_by": "`make backtest` (oxbow.backtest.run)",
            "stage": "P6",
        }
    variants = {str(variant["row_id"]): variant for variant in ablation["variants"]}
    rows: list[dict[str, Any]] = []
    for entry in card["ablation_table"]:
        row_id = str(entry["row_id"])
        variant = variants.get(row_id)
        policies = (variant or {}).get("policies", {})
        rows.append(
            {
                "row_id": row_id,
                "label": entry["label"],
                "question": entry["question"],
                "corpus": entry["corpus"],
                "is_control": bool(entry.get("is_control", False)),
                "control_note": entry.get("control_note"),
                "pr_auc": metric(
                    entry["pr_auc"],
                    f"out/backtest/model_card.json#/ablation_table/{row_id}/pr_auc",
                    provenance=str(entry.get("provenance", PROVENANCE_MEASURED)),
                ),
                "pr_auc_ci": [entry["pr_auc_ci_low"], entry["pr_auc_ci_high"]],
                "auroc_comparability_only": entry["auroc_comparability_only"],
                "brier": entry["brier"],
                "net_benefit_total_minor": entry["net_benefit_total_minor"],
                "currency": entry["currency"],
                "model_version": (variant or {}).get("model_version"),
                "feature_spec_hash": (variant or {}).get("feature_spec_hash"),
                "fold_count": (variant or {}).get("fold_count"),
                "embargo_days": (variant or {}).get("embargo_days"),
                "base_rate": (variant or {}).get("base_rate"),
                "seed_stability": (variant or {}).get("seed_stability"),
                "policies": {
                    name: _policy_row(policy, f"{row_id}/{name}")
                    for name, policy in sorted(policies.items())
                },
                "provenance": str(entry.get("provenance", PROVENANCE_MEASURED)),
            }
        )
    headline = card["headline"]
    drift = _drift_section()
    return {
        "status": "present",
        "headline": {
            "pr_auc": from_artifact(
                card, "headline/pr_auc", artifact="out/backtest/model_card.json"
            ),
            "pr_auc_ci_low": from_artifact(
                card, "headline/pr_auc_ci_low", artifact="out/backtest/model_card.json"
            ),
            "pr_auc_ci_high": from_artifact(
                card, "headline/pr_auc_ci_high", artifact="out/backtest/model_card.json"
            ),
            "corpus": from_artifact(
                card, "headline/corpus", artifact="out/backtest/model_card.json"
            ),
            "model_version": from_artifact(
                card, "headline/model_version", artifact="out/backtest/model_card.json"
            ),
            "feature_spec_hash": from_artifact(
                card, "headline/feature_spec_hash", artifact="out/backtest/model_card.json"
            ),
            "band_note": from_artifact(
                card, "headline/band_note", artifact="out/backtest/model_card.json"
            ),
            "net_benefit_total_minor": from_artifact(
                card, "headline/net_benefit_total_minor", artifact="out/backtest/model_card.json"
            ),
            "provenance": str(headline.get("provenance", PROVENANCE_MEASURED)),
        },
        "metrics_note": from_artifact(
            card, "metrics_note", artifact="out/backtest/model_card.json"
        ),
        "ablation_rows": rows,
        "expected_ablation_row_ids": list(card["expected_ablation_row_ids"]),
        "leakage_control": {
            "detected": from_artifact(
                ablation, "leakage_control/detected", artifact="out/backtest/ablation_results.json"
            ),
            "control_pr_auc": from_artifact(
                ablation,
                "leakage_control/control_pr_auc",
                artifact="out/backtest/ablation_results.json",
            ),
            "best_honest_pr_auc": from_artifact(
                ablation,
                "leakage_control/best_honest_pr_auc",
                artifact="out/backtest/ablation_results.json",
            ),
            "message": from_artifact(
                ablation, "leakage_control/message", artifact="out/backtest/ablation_results.json"
            ),
        },
        "walk_forward": {
            "scheme": from_artifact(
                card, "walk_forward/scheme", artifact="out/backtest/model_card.json"
            ),
            "shuffle": from_artifact(
                card, "walk_forward/shuffle", artifact="out/backtest/model_card.json"
            ),
            "embargo_days": from_artifact(
                card, "embargo_days", artifact="out/backtest/model_card.json"
            ),
            "max_lookback_days": from_artifact(
                card, "max_lookback_days", artifact="out/backtest/model_card.json"
            ),
            "corpus_span_days": from_artifact(
                card,
                "walk_forward/corpus_feasibility/corpus_span_days",
                artifact="out/backtest/model_card.json",
            ),
            "folds_supplied": from_artifact(
                card,
                "walk_forward/corpus_feasibility/folds_supplied",
                artifact="out/backtest/model_card.json",
            ),
            "supports_literal_walk_forward": from_artifact(
                card,
                "walk_forward/corpus_feasibility/supports_literal_walk_forward",
                artifact="out/backtest/model_card.json",
            ),
            "feasibility_note": from_artifact(
                card,
                "walk_forward/corpus_feasibility/note",
                artifact="out/backtest/model_card.json",
            ),
            "which_split_optimised_on": from_artifact(
                card, "which_split_was_optimised_on", artifact="out/backtest/model_card.json"
            ),
        },
        "per_typology_recall": {
            "recall_by_typology": from_artifact(
                card,
                "per_typology_recall/recall_by_typology_mean_over_folds",
                artifact="out/backtest/model_card.json",
            ),
            "join_source": from_artifact(
                card, "per_typology_recall/source", artifact="out/backtest/model_card.json"
            ),
            "negative_control_note": from_artifact(
                card,
                "per_typology_recall/negative_control_note",
                artifact="out/backtest/model_card.json",
            ),
        },
        "fairness": {
            "protected_attributes_note": from_artifact(
                card,
                "fairness/protected_attributes_note",
                artifact="out/backtest/model_card.json",
            ),
            "axes": [
                {
                    "axis": str(axis["axis"]),
                    "available": bool(axis["available"]),
                    "buckets": len(axis.get("buckets", [])),
                    "reason": str(axis.get("reason", "")),
                }
                for axis in card["fairness"]["axes"]
            ],
        },
        "perturbations": from_artifact(
            card, "perturbations", artifact="out/backtest/model_card.json"
        ),
        "risk_adjusted_benefit_ratio": card["benefit_ratio"],
        "overfitting_controls": card["overfitting_controls"],
        "mlflow": card["mlflow"],
        "provenance_note": from_artifact(
            ablation, "provenance_note", artifact="out/backtest/ablation_results.json"
        ),
        "drift": drift,
    }


def _drift_section() -> dict[str, Any]:
    """The PSI/CSI drift table, or the artifact that has to exist for it to be real."""
    artifact = ARTIFACT_BY_NAME["drift_periods"]
    path = artifact.path(_ROOT.get())
    if not path.is_dir() or not any(path.iterdir()):
        return {
            "status": "missing_artifact",
            "artifact": artifact.relpath,
            "produced_by": artifact.producer,
            "stage": artifact.stage,
            "what_will_go_here": [
                "PSI and per-bin CSI per feature per period (oxbow.scoring.drift)",
                "score PSI by period, and the worst value that triggers the action",
                f"the degrade threshold from config/model.yaml :: drift.psi_action "
                f"({_psi_action(_ROOT.get())})",
                "the named reason the run degraded to rules-plus-scorecard",
            ],
        }
    files = sorted(item.name for item in path.iterdir() if item.is_file())
    return {
        "status": "present",
        "artifact": artifact.relpath,
        "files": len(files),
        "sample": files[:3],
    }


def _psi_action(root: Path) -> Any:
    raw = _read_yaml(root / "config/model.yaml")
    drift = raw.get("drift") or {}
    return drift.get("psi_action", NO_NUMBER)


def _policy_row(policy: Mapping[str, Any], pointer: str) -> dict[str, Any]:
    """One allocation-policy row, money fields included as stored minor units."""
    return {
        "allocator": str(policy.get("allocator_label", "")),
        "pr_auc": policy.get("pr_auc"),
        "pr_auc_ci": policy.get("pr_auc_ci"),
        "brier": policy.get("brier"),
        "auroc_comparability_only": policy.get("auroc_comparability_only"),
        "mean_precision_at_budget": policy.get("mean_precision_at_budget"),
        "mean_recall_at_budget": policy.get("mean_recall_at_budget"),
        "mean_alerts_per_10k_accounts": policy.get("mean_alerts_per_10k_accounts"),
        "net_benefit_total_minor": policy.get("net_benefit_total_minor"),
        "benefit_per_analyst_hour_minor": policy.get("benefit_per_analyst_hour_minor"),
        "max_drawdown_minor": policy.get("max_drawdown_minor"),
        "zero_drawdown_labelled": policy.get("zero_drawdown_labelled"),
        "var95_mean_minor": policy.get("var95_mean_minor"),
        "es975_mean_minor": policy.get("es975_mean_minor"),
        "optimality_gap_minor": policy.get("optimality_gap_minor"),
        "risk_adjusted_benefit_ratio": policy.get("risk_adjusted_benefit_ratio"),
        "cumulative_benefit_minor": policy.get("cumulative_benefit_minor"),
        "reliability_curve": policy.get("reliability_curve"),
        "source": f"out/backtest/ablation_results.json#variants/{pointer}",
    }


def economics_section(
    loaded: Mapping[str, Any], economics: Economics, block: AssumptionBlock
) -> dict[str, Any]:
    """Every assumption verbatim, plus the policy comparison those assumptions price."""
    card = loaded["backtest_model_card"]
    ablation = loaded["backtest_ablation"]
    comparison: dict[str, Any] = {
        "status": "missing_artifact",
        "artifact": "out/backtest/ablation_results.json",
        "produced_by": "`make backtest`",
    }
    if ablation is not None:
        variants = {str(variant["row_id"]): variant for variant in ablation["variants"]}
        pivot = variants.get("threshold_vs_ev") or variants.get("full_calibrated")
        if pivot is not None:
            comparison = {
                "status": "present",
                "variant": str(pivot["label"]),
                "corpus": str(pivot["corpus"]),
                "policies": {
                    name: _policy_row(policy, f"threshold_vs_ev/policies/{name}")
                    for name, policy in sorted(pivot["policies"].items())
                },
                "provenance": str(pivot.get("provenance", PROVENANCE_MEASURED)),
                "seed": metric(pivot.get("seed_stability"), "ablation row seed_stability"),
            }
    return {
        "assumptions_verbatim": block.text,
        "assumption_source": metric(
            "config/economics.yaml", "config/economics.yaml", provenance=PROVENANCE_CONFIG
        ),
        "currency": metric(
            economics.currency, "config/economics.yaml#/currency", provenance=PROVENANCE_CONFIG
        ),
        "minor_units_per_major": metric(
            economics.minor_units_per_major,
            "config/economics.yaml#/minor_units_per_major",
            provenance=PROVENANCE_CONFIG,
        ),
        "recovery": {
            "rate": metric(
                economics.recovery.rate,
                "config/economics.yaml#/recovery.rate",
                provenance=PROVENANCE_CONFIG,
            ),
            "sensitivity_band": metric(
                list(economics.recovery.band),
                "config/economics.yaml#/recovery.sensitivity_band",
                provenance=PROVENANCE_CONFIG,
            ),
            "bounds_exclusive": metric(
                [economics.recovery.lower_exclusive, economics.recovery.upper_exclusive],
                "config/economics.yaml#/recovery.bounds_exclusive",
                provenance=PROVENANCE_CONFIG,
            ),
        },
        "analyst": {
            "cost_per_hour_minor": metric(
                economics.analyst.cost_per_hour_minor,
                "config/economics.yaml#/analyst.cost_per_hour_minor",
                provenance=PROVENANCE_CONFIG,
            ),
            "cost_per_minute_minor": metric(
                economics.analyst.cost_per_minute_minor,
                "config/economics.yaml#/analyst.cost_per_minute_minor",
                provenance=PROVENANCE_CONFIG,
            ),
            "hours_per_period": metric(
                economics.analyst.hours_per_period,
                "config/economics.yaml#/analyst.hours_per_period",
                provenance=PROVENANCE_CONFIG,
            ),
            "min_review_minutes": metric(
                economics.analyst.min_review_minutes,
                "config/economics.yaml#/analyst.min_review_minutes",
                provenance=PROVENANCE_CONFIG,
            ),
        },
        "friction_cost_minor": metric(
            economics.friction_cost.minor,
            "config/economics.yaml#/friction_cost_minor",
            provenance=PROVENANCE_CONFIG,
            unit="minor",
        ),
        "review_minutes_by_alert_class": metric(
            dict(economics.review_minutes_by_alert_class),
            "config/economics.yaml#/review_minutes_by_alert_class",
            provenance=PROVENANCE_CONFIG,
        ),
        "exposure": metric(
            {
                "window_hours": economics.exposure.window_hours,
                "downstream_hops": economics.exposure.downstream_hops,
            },
            "config/economics.yaml#/exposure",
            provenance=PROVENANCE_CONFIG,
        ),
        "capacity": metric(
            {
                "review_minutes_per_period": economics.capacity.review_minutes_per_period,
                "default_alerts_reviewed": economics.capacity.default_alerts_reviewed,
                "sweep_min_minutes": economics.capacity.sweep.min_minutes,
                "sweep_max_minutes": economics.capacity.sweep.max_minutes,
                "sweep_points": economics.capacity.sweep.points,
            },
            "config/economics.yaml#/capacity",
            provenance=PROVENANCE_CONFIG,
        ),
        "monte_carlo": metric(
            {
                "runs": economics.monte_carlo.runs,
                "max_depth": economics.monte_carlo.max_depth,
                "seed": economics.monte_carlo.seed,
                "interval": [
                    economics.monte_carlo.lower_quantile,
                    economics.monte_carlo.upper_quantile,
                ],
            },
            "config/economics.yaml#/monte_carlo",
            provenance=PROVENANCE_CONFIG,
        ),
        "solver": metric(
            {
                "greedy_budget_ms": economics.solver.greedy_budget_ms,
                "cpsat_deadline_ms": economics.solver.cpsat_deadline_ms,
                "agreement_tolerance_ratio": economics.solver.agreement_tolerance_ratio,
            },
            "config/economics.yaml#/solver",
            provenance=PROVENANCE_CONFIG,
        ),
        "four_eyes_threshold_minor": metric(
            economics.four_eyes.threshold_exposure_minor,
            "config/economics.yaml#/four_eyes.threshold_exposure_minor",
            provenance=PROVENANCE_CONFIG,
            unit="minor",
        ),
        "tail_risk": metric(
            {"var_alpha": economics.tail_risk.var_alpha, "es_alpha": economics.tail_risk.es_alpha},
            "config/economics.yaml#/tail_risk",
            provenance=PROVENANCE_CONFIG,
        ),
        "policy_comparison": comparison,
        "model_card_economics": (
            {}
            if card is None
            else {
                "benefit_per_analyst_hour_minor": from_artifact(
                    card,
                    "benefit_per_analyst_hour_minor",
                    artifact="out/backtest/model_card.json",
                ),
                "assumption_line": from_artifact(
                    card, "economics_assumption_line", artifact="out/backtest/model_card.json"
                ),
            }
        ),
        "capacity_sweep": _capacity_sweep(economics),
    }


def _capacity_sweep(economics: Economics) -> dict[str, Any]:
    """The frontier rows, or the named artifact that has to exist for them to be real."""
    artifact = ARTIFACT_BY_NAME["capacity_curve"]
    path = artifact.path(_ROOT.get())
    if not path.is_dir() or not any(path.iterdir()):
        return {
            "status": "missing_artifact",
            "artifact": artifact.relpath,
            "produced_by": artifact.producer,
            "stage": artifact.stage,
            "what_will_go_here": [
                "capacity B swept from "
                f"{economics.capacity.sweep.min_minutes} to "
                f"{economics.capacity.sweep.max_minutes} analyst-minutes in "
                f"{economics.capacity.sweep.points} points "
                "(config/economics.yaml :: capacity.sweep)",
                "achievable accounts reviewed, expected loss avoided and customers wrongly "
                "touched at each point",
                f"the operating point at capacity.review_minutes_per_period = "
                f"{economics.capacity.review_minutes_per_period:,}",
                "one dominated policy on the same axes, so the frontier means something visually",
            ],
        }
    files = sorted(item.name for item in path.iterdir() if item.is_file())
    return {
        "status": "present",
        "artifact": artifact.relpath,
        "files": len(files),
        "sample": files[:3],
    }


def integrity_section(loaded: Mapping[str, Any]) -> dict[str, Any]:
    """The audit-chain and packet contracts, as they stand on this host right now."""
    root = _ROOT.get()
    chain_path = ARTIFACT_BY_NAME["audit_chain"].path(root)
    runs_path = ARTIFACT_BY_NAME["run_ledger"].path(root)
    chain_rows = (
        sum(1 for line in chain_path.read_text(encoding="utf-8").splitlines() if line.strip())
        if chain_path.is_file()
        else NO_NUMBER
    )
    del loaded
    return {
        "chain_artifact": metric(
            chain_path.relative_to(root).as_posix(),
            "scripts/verify_audit.py",
            provenance=PROVENANCE_CONFIG,
        ),
        "chain_rows": metric(
            chain_rows,
            "out/audit/audit.jsonl",
            note="written by oxbow.adapters.file.audit.FileAuditSink; walk it with "
            "`make verify-audit`",
        ),
        "run_ledger_present": metric(runs_path.is_file(), "out/warehouse/runs.jsonl"),
        "hash_chain_contract": metric(
            'sha256("oxbow-audit-v1" | seq | occurred_at | actor | subject | action | '
            "canonical(payload) | prev_hash)",
            "packages/pipeline/oxbow/audit/chain.py",
            provenance=PROVENANCE_CONFIG,
        ),
        "packet_contract": metric(
            "oxbow/packet verifies the chain on export and a broken link names its "
            "sequence number; the packet renders from the pinned run and stamps "
            "decided_on_superseded_run from the audit payload",
            "packages/pipeline/oxbow/packet/render.py",
            provenance=PROVENANCE_CONFIG,
        ),
        "erasure": metric(
            "the salt mapping is destroyed, the chain survives, the subject is unrecoverable",
            "packages/pipeline/oxbow/audit/erasure.py",
            provenance=PROVENANCE_CONFIG,
        ),
        "append_only": metric(
            "a reversal is a new row referencing the original; both appear in the timeline",
            "DECISIONS.md :: DEV-006 + apps/api/alembic/versions/0002_integrity_triggers.py",
            provenance=PROVENANCE_RECORDED,
        ),
    }


def limitations_section(loaded: Mapping[str, Any], economics: Economics) -> list[dict[str, Any]]:
    """Plan §15's honest weaknesses, each traceable to a measurement.

    A limitation that reads like a disclaimer is decoration, so every entry states a
    number and points at the artifact it came from. The numbers are looked up from the
    loaded artifacts here rather than typed into prose: if a measurement changes, the
    sentence changes with it, and a limitation that has outlived its evidence is a bug
    in this generator.
    """
    paysim = loaded["paysim_measurement"]
    ibm = loaded["ibm_graph_measurement"]
    cycles = loaded["ibm_cycle_measurement"]
    card = loaded["backtest_model_card"]
    ablation = loaded["backtest_ablation"]
    typologies: pl.DataFrame | None = loaded["ibm_typologies"]
    cycle_section = cycle_reality_section(loaded)
    anatomy = cycle_section["labelled_cycle_anatomy"]
    block = assumption_block(economics)
    sources = _source_entries(loaded["config_sources"])
    flagged = _flagged_fraud_count()

    items: list[dict[str, Any]] = [
        {
            "id": "label_quality_is_flagged_fraud_is_not_a_target",
            "claim": (
                f"`isFlaggedFraud` fires {flagged:,} times in {_fmt(paysim['n_rows'])} PaySim "
                "rows. That is not a thin label, it is no label: at that density any model "
                "can fit it by ignoring everything. `isFraud` is the only viable target on "
                "this corpus, and it covers one narrow behaviour — an account takeover drained "
                "via TRANSFER then CASH-OUT — so it says nothing about layering or network "
                "fraud, which is the crime this product is built to find."
            ),
            "evidence": [
                metric(
                    flagged,
                    "DECISIONS.md :: DEV-011 (label-quality finding measured by scripts/measure_graph.py)",
                    provenance=PROVENANCE_RECORDED,
                    unit="rows",
                ),
                metric(paysim["n_rows"], "data/graph_measurement.json#/n_rows", unit="rows"),
                metric(
                    _flat(sources["paysim"]["label_caveat"]),
                    "config/sources.yaml#/sources/0/label_caveat",
                    provenance=PROVENANCE_CONFIG,
                ),
            ],
            "consequence": (
                "A high recall against `isFraud` is a statement about simulated account "
                "takeover, not about financial crime."
            ),
        },
        {
            "id": "simulator_bias",
            "claim": (
                "PaySim is an agent-based simulator calibrated on one month of real logs, so "
                "fraud prevalence is a generator parameter rather than a measured rate and the "
                "balance columns are known to disagree with the amounts. Every base rate, "
                "precision-at-budget and alerts-per-10k figure computed on this corpus is a "
                "property of the simulator's settings."
            ),
            "evidence": [
                metric(
                    _flat(sources["paysim"]["description"]),
                    "config/sources.yaml#/sources/0/description",
                    provenance=PROVENANCE_CONFIG,
                ),
                *[
                    metric(
                        _flat(bias),
                        f"config/sources.yaml#/sources/0/known_biases/{index}",
                        provenance=PROVENANCE_CONFIG,
                    )
                    for index, bias in enumerate(sources["paysim"]["known_biases"])
                ],
            ],
            "consequence": (
                "Rates are reported as illustrations of what a 0.129 % base rate does to a "
                "ranking, never as an operator's expected alert volume."
            ),
        },
        {
            "id": "captured_value_is_an_estimate_under_stated_assumptions",
            "claim": (
                "Captured value is an estimate, not a measurement. It is "
                f"E_i x r with r swept over {_band_text(economics)} — the recovery rate is a "
                f"declared assumption, and so is the review price of "
                f"{_money(economics.analyst.cost_per_minute_minor, economics, block)} per "
                f"analyst-minute and the friction cost of "
                f"{_money(economics.friction_cost.minor, economics, block)} per wrongly-touched "
                "customer. No seizure, no recovery and no complaint in this document was "
                "observed by anyone."
            ),
            "evidence": [
                metric(
                    economics.recovery.rate,
                    "config/economics.yaml#/recovery.rate",
                    provenance=PROVENANCE_CONFIG,
                ),
                metric(
                    list(economics.recovery.band),
                    "config/economics.yaml#/recovery.sensitivity_band",
                    provenance=PROVENANCE_CONFIG,
                ),
                metric(
                    economics.friction_cost.minor,
                    "config/economics.yaml#/friction_cost_minor",
                    provenance=PROVENANCE_CONFIG,
                    unit="minor",
                ),
                metric(
                    economics.analyst.cost_per_minute_minor,
                    "config/economics.yaml#/analyst.cost_per_minute_minor",
                    provenance=PROVENANCE_CONFIG,
                    unit="minor",
                ),
            ],
            "consequence": (
                "Change one assumption and every headline moves, which is why the UI, the "
                "packet and these documents print the assumption block beside the figure "
                "instead of in an appendix nobody opens."
            ),
        },
        {
            "id": "paysim_has_no_network",
            "claim": (
                "PaySim has no network to detect. Median counterparty degree is "
                f"{_fmt(paysim['counterparty_degree']['median'])} against a pre-committed pass "
                f"condition of > 2, the sender reuse ratio is {paysim['reuse_ratio']}, the most "
                f"repeated originator in {_fmt(paysim['n_rows'])} rows has "
                f"{max(int(item['edges']) for item in paysim['top20_senders'])} edges, and "
                f"{paysim['cycles']['cycles_found']} time-respecting "
                f"{paysim['cycles']['min_len']}-{paysim['cycles']['max_len']} cycles survived a "
                f"{_fmt(paysim['cycles']['sample_rows'])}-row sample. The corpus the network "
                "thesis was written against cannot carry it (DEV-011)."
            ),
            "evidence": [
                metric(
                    paysim["counterparty_degree"]["median"],
                    "data/graph_measurement.json#/counterparty_degree/median",
                ),
                metric(paysim["reuse_ratio"], "data/graph_measurement.json#/reuse_ratio"),
                metric(
                    paysim["cycles"]["cycles_found"],
                    "data/graph_measurement.json#/cycles/cycles_found",
                ),
                metric(paysim["verdict"], "data/graph_measurement.json#/verdict"),
            ],
            "consequence": (
                "Module B — rules, graph features, network detection — runs on IBM-AML under "
                "the day-4 fallback that plan §4 pre-committed before the number was known."
            ),
        },
        {
            "id": "r4_cycle_definition_excludes_the_labelled_cycles",
            "claim": (
                "The cycle rule as specified fires on none of the corpus's own labelled "
                f"cycles. The enumerator returns {cycles['total_cycles_found']} on components "
                f"selected whole, with the search budget hit: "
                f"{'yes' if cycle_section['truncated']['value'] else 'no'} "
                f"({_src(cycle_section['component_intact_cycles_found'])}) — while of the "
                f"{anatomy['labelled_blocks']['value']} blocks the corpus labels CYCLE, "
                f"{anatomy['cross_currency']['value']} are cross-currency, "
                f"{anatomy['below_retention_floor']['value']} breach the retention floor and "
                f"{anatomy['not_time_monotonic']['value']} are not time-monotonic (DEV-015)."
            ),
            "evidence": [
                metric(
                    cycles["total_cycles_found"],
                    "data/ibm_cycle_measurement.json#/total_cycles_found",
                ),
                cycle_section["component_intact_cycles_found"],
                anatomy["cross_currency"],
                anatomy["below_retention_floor"],
                anatomy["not_time_monotonic"],
            ],
            "consequence": (
                "R4 is reported against the labelled blocks as ground truth, and the fix is a "
                "documented currency and retention policy plus a separate two-leg round-trip "
                "pattern — not a quiet change to a default."
            ),
        },
        {
            "id": "only_the_hi_small_ibm_bundle_was_acquired",
            "claim": (
                "The network evidence comes from one scenario bundle. IBM-AML publishes "
                f"several; {_flat(ibm['source'])} was fetched, and the density reported here "
                f"(median degree {ibm['degree_median']}) is a property of a high-intensity "
                "small-scale scenario, not of the corpus family. The full archive download was "
                "refused by the CDN on this host (DEV-013)."
            ),
            "evidence": [
                metric(ibm["source"], "data/ibm_graph_measurement.json#/source"),
                metric(ibm["degree_median"], "data/ibm_graph_measurement.json#/degree_median"),
                metric(
                    ibm["n_directed_edges_excl_self_loops"],
                    "data/ibm_graph_measurement.json#/n_directed_edges_excl_self_loops",
                ),
                metric(
                    ibm["median_counterparties_per_account"],
                    "data/ibm_graph_measurement.json#/median_counterparties_per_account",
                    note="the honest companion figure: connected, but a hub-and-spoke world",
                ),
            ],
            "consequence": (
                "Per-typology recall and the graph-features ablation row describe HI-Small. LI, "
                "Medium and Large bundles are untested here."
            ),
        },
        {
            "id": "the_corpus_window_is_18_days",
            "claim": (
                "The acquired IBM window spans "
                f"{ibm['temporal_range'][0]} to {ibm['temporal_range'][1]} — about "
                f"{_window_days(ibm['temporal_range'])} days — while the walk-forward embargo "
                f"must equal the longest feature lookback ({_fmt(30)} days). Five "
                "expanding-window folds with a 30-day embargo and a 30-day test window do not "
                "fit inside the observation period, so the fold design has to be shortened "
                "against the lookback, and that trade-off is stated rather than buried."
            ),
            "evidence": [
                metric(
                    list(ibm["temporal_range"]), "data/ibm_graph_measurement.json#/temporal_range"
                ),
                _window_days_metric(ibm["temporal_range"]),
                metric(ibm["measured_at_utc"], "data/ibm_graph_measurement.json#/measured_at_utc"),
            ],
            "consequence": (
                "Fold counts and lookbacks are reported with the harness's own feasibility "
                "verdict, never as a walk-forward claim the timeline cannot support."
            ),
        },
        {
            "id": "there_are_no_protected_attributes_to_test_fairness_on",
            "claim": (
                "No demographic fairness claim can be made, because neither corpus carries a "
                "protected attribute: no gender, age band, ethnicity, nationality or "
                "account-holder type exists, and identifiers are salted hashes before they "
                "reach the model. The fairness check runs against over-flagging proxies — "
                "amount, account age, activity volume and co-tenancy — and is labelled as a "
                "proxy on the page."
            ),
            "evidence": [
                (
                    metric(
                        _flat(card["fairness"]["protected_attributes_note"]),
                        "out/backtest/model_card.json#/fairness/protected_attributes_note",
                        provenance=_card_provenance(card),
                    )
                    if card
                    else metric(NO_NUMBER, "out/backtest/model_card.json#/fairness")
                ),
                metric(
                    [axis["axis"] for axis in (card["fairness"]["axes"] if card else [])],
                    "out/backtest/model_card.json#/fairness/axes",
                    provenance=_card_provenance(card) if card else PROVENANCE_RECORDED,
                ),
            ],
            "consequence": (
                "Unverifiable claims are not made. Proxy false-positive-rate spread is what "
                "ships, and demographic parity is stated as unmeasurable rather than as clean."
            ),
        },
        {
            "id": "per_corpus_fee_and_spread_are_dropped_from_the_canonical_event",
            "claim": (
                "The canonical event keeps one amount and a currency tag, so IBM's "
                "amount-received / amount-paid pair collapses and the fee or FX spread between "
                f"them is lost at ingest. On a corpus with {ibm['n_currencies']} currencies in "
                "one bundle, that discarded spread is precisely the signal that distinguishes a "
                "conversion from a transfer — and cross-currency layering is the mechanism "
                "DEV-015 says R4 must stop filtering out."
            ),
            "evidence": [
                metric(ibm["n_currencies"], "data/ibm_graph_measurement.json#/n_currencies"),
                metric(
                    [[str(name), int(count)] for name, count in ibm["currencies"][:3]],
                    "data/ibm_graph_measurement.json#/currencies",
                    note="currency names rather than ISO-4217 codes in the raw bytes",
                ),
                metric(
                    "amount_received and amount_paid collapse to amount_minor plus a currency tag",
                    "DECISIONS.md :: DEV-013 (header read from the bytes)",
                    provenance=PROVENANCE_RECORDED,
                ),
            ],
            "consequence": (
                "Value-retention arithmetic across a currency hop cannot be done honestly "
                "without a declared reference rate, which is why DEV-015's fix adds one marked "
                "illustrative instead of letting the code imply a rate."
            ),
        },
        {
            "id": "single_deployment_rbac_is_not_multi_tenant_isolation",
            "claim": (
                "Roles and four-eyes ship as decision policy for one investigating team. There "
                "is a single Keycloak realm, one Postgres, no row-level security and no "
                "per-tenant key, so this is not a platform two organisations could share, and "
                '"SSO + RBAC" in the README means decision provenance, not tenancy (DEV-006).'
            ),
            "evidence": [
                metric(
                    "roles + four-eyes above the declared exposure threshold; tenancy explicitly not built",
                    "DECISIONS.md :: DEV-006",
                    provenance=PROVENANCE_RECORDED,
                ),
                metric(
                    economics.four_eyes.threshold_exposure_minor,
                    "config/economics.yaml#/four_eyes.threshold_exposure_minor",
                    provenance=PROVENANCE_CONFIG,
                    unit="minor",
                ),
            ],
            "consequence": (
                "Building tenancy to hold one tenant was rejected on purpose; the limitation is "
                "recorded so nobody reads the auth section as a multi-tenancy claim."
            ),
        },
    ]

    harness_claim = (
        "The statistical and economic numbers currently published come from the "
        "hand-computed fake harness, not from a scored corpus. They exist to prove the "
        "harness computes what it claims — including that a deliberately leaking "
        "configuration beats every honest one, which is the leak detector working — and "
        "they must be replaced by a real `make backtest` run before any of them is "
        "quoted as a result."
    )
    if card is not None and ablation is not None:
        note = str(ablation["provenance_note"])
        harness_claim = (
            f"{harness_claim} The artifact says: \"{note}\" Model version on the headline "
            f"row: \"{card['headline']['model_version']}\"."
        )
    items.append(
        {
            "id": "published_metrics_are_harness_self_checks_until_p6_runs_for_real",
            "claim": harness_claim,
            "evidence": [
                (
                    metric(
                        note,
                        "out/backtest/ablation_results.json#/provenance_note",
                        provenance=PROVENANCE_FAKE,
                    )
                    if card is not None and ablation is not None
                    else metric(
                        NO_NUMBER,
                        "out/backtest/ablation_results.json",
                        provenance=PROVENANCE_RECORDED,
                        note="no backtest artifact exists on this host, so no statistical or "
                        "economic figure is published",
                    )
                ),
                _drift_status_metric(),
            ],
            "consequence": (
                "Every generated document prints each number's provenance and the digests of "
                "the artifacts behind it, so a harness figure cannot quietly become a model "
                "result, and a stale document is detectable rather than arguable."
            ),
        }
    )
    if typologies is not None:
        items.append(
            {
                "id": "typology_ground_truth_is_an_annotation_not_a_case",
                "claim": (
                    f"The typology labels are research annotations: {_fmt(typologies.height)} "
                    f"transactions in {typelines_attempts(typologies)} labelled laundering "
                    "attempt blocks were planted by the corpus authors and recovered from a "
                    "text file. Per-typology recall therefore measures agreement with those "
                    "annotations, not detection of real laundering (DEV-014)."
                ),
                "evidence": [
                    metric(typologies.height, "data/processed/ibm_typologies.parquet#rows"),
                    metric(
                        typologies["attempt_id"].n_unique(),
                        "data/processed/ibm_typologies.parquet#attempt_id(n_unique)",
                    ),
                    metric(
                        _flat(sources["ibmaml"]["label_caveat"]),
                        "config/sources.yaml#/sources/1/label_caveat",
                        provenance=PROVENANCE_CONFIG,
                    ),
                ],
                "consequence": (
                    "The join is FIFO per column-key and fails closed when an annotation matches "
                    "nothing, because a silently mis-ordered annotation would mislabel the "
                    "positive set the whole network argument is graded against."
                ),
            }
        )
    if len(items) < MINIMUM_LIMITATIONS:
        raise AssertionError(
            f"plan §15 requires at least {MINIMUM_LIMITATIONS} named limitations; this "
            f"generator produced {len(items)}"
        )
    return items


def typelines_attempts(frame: pl.DataFrame) -> str:
    return _fmt(frame["attempt_id"].n_unique())


def _drift_status_metric() -> dict[str, Any]:
    drift = _drift_section()
    return metric(
        drift["status"],
        drift.get("artifact", "out/warehouse/drift_period"),
        note=str(drift.get("produced_by", "")),
    )


def _card_provenance(card: Mapping[str, Any]) -> str:
    return str(card.get("headline", {}).get("provenance", PROVENANCE_MEASURED))


def _flagged_fraud_count() -> int:
    """`isFlaggedFraud` positives across the corpus, as DEV-011 measured them."""
    return 16


# --------------------------------------------------------------------------
# assembly


def build_eval_payload(root: Path) -> dict[str, Any]:
    """Assemble ``data/processed/eval.json`` from the artifacts on disk.

    Raises :class:`FileNotFoundError` naming every missing *required* artifact. The
    corpus measurements, the typology join and the download manifest are what the
    dataset card and the limitations list are built from; a documentation run that went
    ahead without them would be writing prose, not publishing measurements.
    """
    resolved = Path(root).resolve()
    _ROOT.set(resolved)
    missing = [
        f"  {artifact.relpath}  (produced by {artifact.producer}, stage {artifact.stage})"
        for artifact in ARTIFACTS
        if artifact.required and not artifact.path(resolved).exists()
    ]
    if missing:
        raise FileNotFoundError(
            "`make eval` will not publish from intent. Required artifacts missing:\n"
            + "\n".join(missing)
        )
    loaded = _load_available(resolved)
    economics = load_economics(resolved)
    block = assumption_block(economics)
    pipeline_raw = loaded["config_pipeline"] or {}
    payload: dict[str, Any] = {
        "eval_version": EVAL_VERSION,
        "disclaimer": OXBOW_DISCLAIMER,
        "scenario_dressing": SCENARIO_DRESSING,
        "repository": {
            "seed": metric(
                economics.seed, "config/pipeline.yaml#/seed", provenance=PROVENANCE_CONFIG
            ),
            "deployment_timezone": metric(
                str(pipeline_raw.get("deployment_timezone", NO_NUMBER)),
                "config/pipeline.yaml#/deployment_timezone",
                provenance=PROVENANCE_CONFIG,
            ),
            "interactive_txn_target": metric(
                (pipeline_raw.get("sampling") or {}).get("interactive_txn_target", NO_NUMBER),
                "config/pipeline.yaml#/sampling/interactive_txn_target",
                provenance=PROVENANCE_CONFIG,
            ),
            "sampling_strategy": metric(
                (pipeline_raw.get("sampling") or {}).get("strategy", NO_NUMBER),
                "config/pipeline.yaml#/sampling/strategy",
                provenance=PROVENANCE_CONFIG,
            ),
        },
        "artifacts": artifact_descriptors(resolved),
        "corpus": corpus_section(loaded, resolved),
        "graph_thesis": graph_thesis_section(loaded),
        "typologies": typologies_section(loaded),
        "cycle_reality": cycle_reality_section(loaded),
        "model": model_section(loaded),
        "economics": economics_section(loaded, economics, block),
        "integrity": integrity_section(loaded),
        "limitations": limitations_section(loaded, economics),
        "missing_artifacts": [
            {
                "name": artifact.name,
                "path": artifact.relpath,
                "stage": artifact.stage,
                "producer": artifact.producer,
            }
            for artifact in ARTIFACTS
            if not artifact.required and not artifact.path(resolved).exists()
        ],
        "provenance_summary": {
            "harness_provenance": loaded["backtest_model_card"] is not None,
            "statement": (
                "Figures derived from the backtest artifacts carry the provenance those "
                "artifacts declared for themselves; where that was a fake-harness "
                "self-check, the documents say so beside the number."
            ),
            "hashed_artifacts": {
                name: str(
                    entry.get(
                        "sha256", entry.get("note", "directory" if entry["exists"] else "absent")
                    )
                )
                for name, entry in sorted(artifact_descriptors(resolved).items())
            },
        },
    }
    return payload


def _dump(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_eval_json(root: Path, payload: Mapping[str, Any]) -> Path:
    target = root / EVAL_JSON_RELPATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_dump(payload), encoding="utf-8", newline="\n")
    return target


def render_documents(root: Path, payload: Mapping[str, Any]) -> tuple[Path, ...]:
    """Render the published documents from ``eval.json``.

    ``data/DATASET_CARD.md`` is deliberately absent from this map. The card carries
    authored judgement — what a label means, which licence binds a derivative, what was
    refused and why — and a generator can only reproduce what it is able to derive, so
    rendering it destroyed the parts that mattered. :func:`verify_dataset_card` checks it
    against the artifacts instead, and the sections it used to invent keep their text in
    the card itself.
    """
    writers = {
        "README.md": render_readme,
        "ARCHITECTURE.md": render_architecture,
        "MODEL_CARD.md": render_model_card,
        "ECONOMICS_CARD.md": render_economics_card,
        "LIMITATIONS.md": render_limitations,
    }
    written: list[Path] = []
    for relpath, writer in writers.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(writer(payload), encoding="utf-8", newline="\n")
        written.append(target)
    return tuple(written)


def run_eval(root: Path, *, write_docs: bool = True) -> dict[str, Any]:
    """The verb behind ``make eval``: build, write, render, verify, report.

    The returned report lists every section still without an artifact and the dataset
    card's verdict. The CLI exits non-zero on either, because documentation that presents
    itself as finished while a pipeline stage has never run is one failure, and a dataset
    card whose figures no longer match the artifacts that own them is another — worse,
    since it looks measured.
    """
    payload = build_eval_payload(root)
    eval_path = write_eval_json(root, payload)
    documents = render_documents(root, payload) if write_docs else ()
    card = verify_dataset_card(root)
    return {
        "eval_json": eval_path,
        "documents": list(documents),
        "gaps": _section_gaps(payload),
        "missing_artifacts": payload["missing_artifacts"],
        "limitation_count": len(payload["limitations"]),
        "harness_provenance": payload["provenance_summary"]["harness_provenance"],
        "card_audit": card,
        "card_ok": card.ok,
        "payload": payload,
    }


def _section_gaps(payload: Mapping[str, Any]) -> list[str]:
    gaps = [
        f"{item['path']} — stage {item['stage']}, produced by {item['producer']}"
        for item in payload["missing_artifacts"]
    ]
    return gaps


# --------------------------------------------------------------------------
# markdown helpers


def _v(item: Any) -> str:
    """A metric's value as text; lists and mappings expanded readably."""
    value = item["value"] if isinstance(item, Mapping) else item
    if isinstance(value, list):
        return "; ".join(_scalar(entry) for entry in value)
    if isinstance(value, Mapping):
        return ", ".join(f"{key}={_scalar(entry)}" for key, entry in sorted(value.items()))
    return _scalar(value)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _fmt(value: Any) -> str:
    return _scalar(value)


def _flat(value: Any) -> str:
    """YAML block scalars arrive with newlines; documents want one paragraph."""
    return " ".join(str(value).split())


def _src(item: Any) -> str:
    source = item["source"] if isinstance(item, Mapping) else str(item)
    provenance = item.get("provenance", "") if isinstance(item, Mapping) else ""
    suffix = f" · provenance: `{provenance}`" if provenance else ""
    return f"source: `{source}`{suffix}"


def _value_with_source(item: Any) -> str:
    return f"{_v(item)} ({_src(item)})"


def _money(minor: int, economics: Economics, block: AssumptionBlock) -> str:
    """A minor-unit figure as a currency figure, through the packet's own renderer.

    Reused rather than re-implemented: ``money_line`` refuses to build a figure without
    its assumption block, and ``quant/money.to_major_text`` divides at render time, so a
    document cannot produce a money number that the product itself would reject.
    """
    return money_line(
        "published figure",
        int(minor),
        block=block,
        basis="stored in integer minor units, divided at render time",
        currency=economics.currency,
    ).render()


def _band_text(economics: Economics) -> str:
    return " / ".join(f"{rate:.2f}" for rate in economics.recovery.band)


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.3f}%" if whole else "n/a"


def _window_days(rng: Sequence[Any]) -> float:
    start = datetime.fromisoformat(str(rng[0]).replace(" ", "T"))
    end = datetime.fromisoformat(str(rng[1]).replace(" ", "T"))
    return round((end - start).total_seconds() / 86400.0, 2)


def _window_days_metric(rng: Sequence[Any]) -> dict[str, Any]:
    return metric(_window_days(rng), "data/ibm_graph_measurement.json#/temporal_range", unit="days")


def _source_entries(sources: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(entry["id"]): entry for entry in sources["sources"]}


def _file_sizes(root: Path, declared: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Sizes of the raw files the corpus section cites, measured where they exist."""
    found: dict[str, Any] = {}
    for entry in declared.values():
        for item in entry.get("files", []):
            name = str(item["name"])
            candidates = sorted((root / "data/raw").glob(f"**/{name}"))
            found[name] = (
                metric(candidates[0].stat().st_size, f"data/raw/**/{name}", unit="bytes")
                if candidates
                else metric(
                    NO_NUMBER,
                    f"data/raw/**/{name}",
                    unit="bytes",
                    note="declared in config/sources.yaml; not present on this host",
                )
            )
    return found


def _paysim_sha(manifest: Mapping[str, Any]) -> str:
    return str(manifest["paysim"]["files"][0]["sha256"])


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = [f"| {' | '.join(headers)} |", f"| {'| '.join(['---'] * len(headers))} |"]
    out += [f"| {' | '.join(row)} |" for row in rows]
    return out


def _banner(title: str, payload: Mapping[str, Any]) -> list[str]:
    return [
        f"# {title}",
        "",
        "<!-- Generated by `make eval` (oxbow eval). Do not edit by hand — the next run",
        "     overwrites this file. Every number carries a `source:` pointer into an",
        "     artifact on disk; if a pointer does not resolve, this document is stale. -->",
        "",
        f"Evaluator `{payload['eval_version']}`, from `data/processed/eval.json`. "
        "Regenerate with `make eval`.",
        "",
    ]


def _provenance_table(payload: Mapping[str, Any]) -> list[str]:
    lines = [
        "## Artifact digests behind this document",
        "",
        "A stale document is detectable: run `make eval` and diff these digests.",
        "",
    ]
    rows = []
    for entry in payload["artifacts"].values():
        digest = (
            entry.get("sha256")
            or entry.get("note")
            or ("directory" if entry["exists"] else "absent")
        )
        short = str(digest)
        size = entry.get("bytes")
        rows.append(
            [
                f"`{entry['path']}`",
                entry["stage"],
                "present" if entry["exists"] else "**absent**",
                f"{size:,}" if isinstance(size, int) else f"{entry.get('files', '-')} files",
                f"`{short[:16]}…`" if len(short) > 16 else f"`{short}`",
            ]
        )
    lines += _table(["artifact", "stage", "state", "bytes", "sha256"], rows)
    lines.append("")
    return lines


# --------------------------------------------------------------------------
# documents


def render_readme(payload: Mapping[str, Any]) -> str:
    corpus = payload["corpus"]
    thesis = payload["graph_thesis"]
    economics = payload["economics"]
    integrity = payload["integrity"]
    limitations = payload["limitations"]
    lines = _banner("OXBOW", payload)
    lines += [
        "> " + payload["disclaimer"],
        "",
        payload["scenario_dressing"],
        "",
        "OXBOW is a quantitative risk-scoring and financial-crime network-intelligence",
        "prototype for mobile-money monitoring. Two models that disagree in public — a WOE",
        "scorecard whose points a human can argue with, and a gradient-boosted model whose",
        "SHAP values explain it — twelve typology rules reading a time-respecting directed",
        "multigraph, and an allocation layer that prices the review queue against a capacity",
        "budget instead of assuming the budget is infinite. Historical, public, de-identified",
        "data only: no live rail is connected, and no decision here moves money.",
        "",
        "## What the data actually is",
        "",
        f"- **PaySim** `{corpus['paysim']['file']['value']}`: "
        f"{_value_with_source(corpus['paysim']['rows'])}, "
        f"{_value_with_source(corpus['paysim']['distinct_senders'])} distinct originators, "
        f"licence {_value_with_source(corpus['paysim']['license'])}. SHA-256 recorded at "
        f"download: `{_v(corpus['paysim']['sha256_recorded'])}`; `make data` re-verifies it "
        "against `config/sources.yaml`.",
        f"- **IBM-AML**: {_value_with_source(corpus['ibmaml']['bundle'])}. "
        f"{_value_with_source(corpus['ibmaml']['transaction_rows'])} transaction rows, "
        f"{_value_with_source(corpus['ibmaml']['distinct_accounts'])} accounts, "
        f"{_value_with_source(corpus['ibmaml']['directed_edges_excl_self_loops'])} directed "
        f"edges excluding self-loops, of which "
        f"{_value_with_source(corpus['ibmaml']['self_loop_rows'])} rows are self-loops. "
        f"Laundering-labelled: {_value_with_source(corpus['ibmaml']['laundering_rate_pct'])} "
        f"({_value_with_source(corpus['ibmaml']['laundering_rows'])} rows).",
        f"- **PaySim citation:** {corpus['paysim']['citation']['value']}",
        f"- **IBM-AML citation:** {_value_with_source(corpus['ibmaml']['citation'])}",
        f"- **Licence obligations on derived data.** PaySim "
        f"({_v(corpus['paysim']['license'])}): {corpus['paysim']['license_obligation']['value']} "
        f"IBM-AML ({_v(corpus['ibmaml']['license'])}): "
        f"{corpus['ibmaml']['license_obligation']['value']} That covers the canonical event "
        "table, the feature tables and any published sample.",
        "- **Elliptic** is cite-only and **IEEE-CIS** is refused outright; the reasons are in "
        "`data/DATASET_CARD.md`, and both are declared in `config/sources.yaml` so ingest "
        "cannot read them by accident.",
        "",
        "## The measurement that changed the architecture",
        "",
        "PaySim has no network. Median counterparty degree "
        f"{_value_with_source(thesis['paysim']['median_counterparty_degree'])} against a "
        f"pre-committed pass condition of > "
        f"{_v(thesis['pass_condition']['median_counterparty_degree_gt'])}, sender reuse ratio "
        f"{_value_with_source(thesis['paysim']['sender_reuse_ratio'])}, and "
        f"{_value_with_source(thesis['paysim']['cycles_in_sample'])} surviving "
        f"3-6 time-respecting cycles in a "
        f"{_v(thesis['paysim']['sample_rows'])}-row sample. The recorded verdict is "
        f"`{_v(thesis['paysim']['verdict'])}` ({_src(thesis['paysim']['verdict'])}).",
        "",
        f"IBM-AML does have one: median account degree "
        f"{_value_with_source(thesis['ibmaml']['median_degree'])} excluding self-loops, with "
        f"{_value_with_source(thesis['ibmaml']['accounts_over_2_counterparties'])} accounts "
        "above two counterparties, but only "
        f"{_value_with_source(thesis['ibmaml']['median_counterparties'])} median counterparties "
        "per account — a hub-and-spoke world plus a connected tail, not a uniform mesh. So "
        "Module A (tabular risk, volume) is trained on PaySim and Module B (rules, graph "
        "features, network detection) runs on IBM-AML. The pipeline is corpus-agnostic; only "
        "the assignment and the prose changed (DEV-011, DEV-013).",
        "",
        "## Every currency figure is a function of one file",
        "",
        f"All money derives from `config/economics.yaml` plus observed data: recovery rate "
        f"{_value_with_source(economics['recovery']['rate'])} swept over "
        f"{_value_with_source(economics['recovery']['sensitivity_band'])}; exposure over "
        f"{_v(economics['exposure'])}; review priced at "
        f"{_value_with_source(economics['analyst']['cost_per_minute_minor'])} minor units per "
        f"analyst-minute ({_v(economics['currency'])}, "
        f"{_v(economics['minor_units_per_major'])} minor per major unit, integer minor units "
        "end to end, divided only at render time); friction "
        f"{_value_with_source(economics['friction_cost_minor'])} minor; capacity "
        f"{_v(economics['capacity'])}. **These are illustrative assumptions**, and the UI, the "
        "packet and these documents print the assumption block beside the figure rather than "
        "summarising it.",
        "",
        "## Commands",
        "",
        "| command | what it does |",
        "| --- | --- |",
        "| `make bootstrap` | uv sync, pnpm install, pre-commit hooks |",
        "| `make up` | Postgres 16, Redis 7, MinIO, MLflow, Keycloak, the webhook echo service |",
        "| `make data` | download or verify the corpora against the recorded SHA-256 |",
        "| `make pipeline` | ingest → graph → score → backtest, streaming stage events over SSE |",
        "| `make eval` | regenerate `data/processed/eval.json` and render every document here |",
        "| `make packet` | render every pinned case packet to `out/packets/` |",
        "| `make verify-audit` | walk the decision hash chain, naming the first broken link |",
        "| `make demo` | restore `data/snapshots/demo.dump` and boot offline inside 90 s |",
        "| `make verify` | every gate from every completed phase, run as commands |",
        "| `make verify-determinism` | run the pipeline twice and diff artifact digests |",
        "| `make lint` / `make test` | ruff, mypy, biome, import-linter / pytest, vitest, playwright |",
        "",
        "## Integrity, in one paragraph",
        "",
        f"Each decision writes three rows in one transaction: the decision, its audit-chain "
        f"row, and the outbox row carrying the case bundle. The chain is "
        f"`{_v(integrity['hash_chain_contract'])}` ({_src(integrity['hash_chain_contract'])}). "
        f"`make verify-audit` walks it and names the sequence number of the first broken link; "
        f"the case packet re-walks it on export and refuses to render otherwise "
        f"({_src(integrity['packet_contract'])}). Reversals are new rows, never edits. "
        f"Audit rows on this host right now: {_value_with_source(integrity['chain_rows'])}.",
        "",
        f"## What this cannot do",
        "",
        f"{len(limitations)} named weaknesses, each with the measurement that shows it, in "
        "[LIMITATIONS.md](LIMITATIONS.md) — label quality, simulator bias, captured value as an "
        "estimate, PaySim's absent network, R4's disagreement with the labelled cycles, the "
        "single acquired IBM bundle, the 18-day window, the missing protected attributes, the "
        "dropped fee/spread, and single-tenant RBAC. Read that file before quoting any number "
        "from this one.",
        "",
        "## Model and economics",
        "",
        f"- [MODEL_CARD.md](MODEL_CARD.md) — headline PR-AUC, the ablation table, calibration, "
        "per-typology recall, fairness proxies, and what was *not* measured.",
        f"- [ECONOMICS_CARD.md](ECONOMICS_CARD.md) — the assumptions verbatim, the EV formula, "
        "threshold vs EV policy in money, and the frontier section that is waiting for its "
        "artifact.",
        f"- [ARCHITECTURE.md](ARCHITECTURE.md) — stages, seams, and where measured reality beat "
        "the spec.",
        f"- [data/DATASET_CARD.md](data/DATASET_CARD.md) — sources, licences, hashes, retrieval "
        "and measurement dates, typology ground truth, sampling policy.",
        "",
    ]
    lines += _provenance_table(payload)
    return "\n".join(lines) + "\n"


def render_architecture(payload: Mapping[str, Any]) -> str:
    corpus = payload["corpus"]
    thesis = payload["graph_thesis"]
    cycles = payload["cycle_reality"]
    integrity = payload["integrity"]
    lines = _banner("ARCHITECTURE", payload)
    lines += [
        "One Python workspace, four pipeline stages, one API deployable, one Next.js app. The",
        "seams are declared rather than emergent: `ports/` holds the protocols, `adapters/`",
        "holds every IO implementation, and import-linter contract 1 fails the build if a",
        "feature, graph, model, scoring, quant or backtest module imports an adapter. That one",
        "rule is what makes the offline demo path real instead of aspirational.",
        "",
        "## The four stages, and why the graph comes second",
        "",
        "```",
        "ingest    contracts (Pandera) -> canonical event v1 -> Parquet + DuckDB   [P1b]",
        "graph     time-stamped directed multigraph, rails, Leiden, cycles          [P3a]",
        "score     rules R1-R12, WOE scorecard, LightGBM, IFusion, calibration      [P4]",
        "backtest  purged walk-forward, 30-day embargo, EV vs threshold, ablation   [P6]",
        "```",
        "",
        "Execution order is `P0 → P1a → P1b → P3a → P2 → P3b → …` (DEV-008): a degree feature",
        "cannot be computed from a graph that does not exist yet.",
        "",
        "## Where measured reality overrode the spec",
        "",
        f"- **PaySim is star-shaped** (DEV-011): median counterparty degree "
        f"{_v(thesis['paysim']['median_counterparty_degree'])}, reuse ratio "
        f"{_v(thesis['paysim']['sender_reuse_ratio'])}, max sender edges "
        f"{_v(thesis['paysim']['highest_sender_edges'])}. Module B therefore reads IBM-AML, "
        f"whose measured median degree is {_v(thesis['ibmaml']['median_degree'])} "
        f"({_src(thesis['ibmaml']['median_degree'])}).",
        f"- **The IBM header is eleven columns with `Account` twice** (DEV-013), timestamps are "
        f"minute-precision with no zone, and {corpus['ibmaml']['self_loop_rows']['value']:,} "
        f"rows ({_pct(int(corpus['ibmaml']['self_loop_rows']['value']), int(corpus['ibmaml']['transaction_rows']['value']))}) "
        "are self-loops — so the schema is read from the bytes, not recalled.",
        f"- **The acquired window spans {_v(corpus['ibmaml']['window_days'])} days** "
        f"({_src(corpus['ibmaml']['window_days'])}) against a 30-day embargo, so fold design is "
        "reported with the harness's feasibility verdict rather than by shortening lookbacks.",
        f"- **The spec'd cycle definition found {cycles['total_cycles_found']['value']} cycles** "
        f"on components selected whole "
        f"({_src(cycles['component_intact_cycles_found'])}), with "
        f"truncation={cycles['truncated']['value']}, against "
        f"{cycles['labelled_cycle_anatomy']['labelled_blocks']['value']} blocks the corpus "
        f"labels as cycles; {cycles['labelled_cycle_anatomy']['cross_currency']['value']} of "
        "them are cross-currency (DEV-015). Detection definitions are graded against the "
        "labels, and the disagreement is published rather than tuned away.",
        "",
        "## Decision path (P7)",
        "",
        "A decision writes `decision`, its audit row and its outbox row in one transaction, and",
        "the case bundle is assembled inside that transaction. That is why a packet rendered",
        "months later still shows the evidence as it stood at decision time:",
        "",
        f"- chain: `{_v(integrity['hash_chain_contract'])}`",
        f"- append-only: {_v(integrity['append_only'])} ({_src(integrity['append_only'])})",
        f"- empty decision reasons are refused by the database constraint *and* defensively by "
        f"the packet renderer ({_src(integrity['packet_contract'])})",
        f"- deciding on a superseded run is permitted and stamps `decided_on_superseded_run` "
        "into the digest-covered payload and onto the packet cover",
        f"- erasure: {_v(integrity['erasure'])} — the chain survives, the subject does not",
        "",
        "## Seams and adapters",
        "",
        "| seam | port | adapters |",
        "| --- | --- | --- |",
        "| 1 source | `ports/source.py` | paysim, ibm-aml, S3/MinIO, file, null |",
        "| 2 warehouse | `ports/warehouse.py` | Postgres, file/Parquet, null |",
        "| 3 audit | `ports/audit.py` | Postgres, JSONL file, null |",
        "| 4 model registry | MLflow 2.x, `run_id` + model version on every scored row | mlflow, null |",
        "| 5 sinks | `ports/{case_sink,notify,report}.py` | file, webhook, Slack, GOAML, null |",
        "",
        "Every port has a null adapter writing under `out/<port>/`, which is simultaneously the",
        "offline demo path and the integration-test substrate. `assert_self_describing()` runs",
        "at each sink, so a consumer cannot receive a money figure without the assumptions it",
        "is a function of — the same rule the packet and the UI obey, enforced at the boundary",
        "that has the bytes.",
        "",
        "## Front end",
        "",
        "Seven screens, one design system. `apps/web/src/design/tokens.css` is the value source",
        "and `DESIGN.md` the rule source: bands carry a letter *and* a five-segment meter glyph",
        "(never colour alone), matched-geometry skeletons hold CLS at zero, motion explains",
        "causality and nothing else, the four empty states each name a different reason, and",
        "long jobs get a streaming ledger rather than a spinner. No route may render a value",
        "that is not in an API response.",
        "",
        "## Reproducibility",
        "",
        f"- seed `{_v(payload['repository']['seed'])}` from `config/pipeline.yaml`, propagated "
        "to NumPy, LightGBM, Optuna and the Monte Carlo engine;",
        f"- deployment timezone `{_v(payload['repository']['deployment_timezone'])}`, so a "
        "timestamp is shown with the zone the analyst is standing in;",
        f"- interactive subcorpus target {_v(payload['repository']['interactive_txn_target'])} "
        f"transactions, strategy `{_v(payload['repository']['sampling_strategy'])}` — a "
        "connected subcorpus, because random rows would delete the structure being detected;",
        "- `make verify-determinism` re-runs a completed pipeline and diffs artifact digests.",
        "",
    ]
    lines += _provenance_table(payload)
    return "\n".join(lines) + "\n"


def render_model_card(payload: Mapping[str, Any]) -> str:
    model = payload["model"]
    economics = payload["economics"]
    economics_config = load_economics(_ROOT.get())
    lines = _banner("MODEL CARD", payload)
    lines += ["> " + payload["disclaimer"], ""]
    if str(model["status"]) != "present":
        lines += [
            "## No backtest artifact exists on this host",
            "",
            f"This card cannot be written from intent. Required artifact: "
            f"`{model['artifact']}`, produced by `{model['produced_by']}` (stage "
            f"{model['stage']}). Until it exists this page publishes no performance number, "
            "because any figure it printed would be invented.",
            "",
            "What the card will contain, in the order it will appear:",
            "",
            "- headline PR-AUC with its bootstrap 95 % CI, per corpus and never averaged;",
            "- the ablation table, including the graph-features row the thesis turns on;",
            "- the leakage control that must win, which is the harness proving it can see a leak;",
            "- the reliability curve, Brier, per-typology recall against the joined labels;",
            "- seed stability as mean ± sd across five seeds, not a lucky run;",
            "- fairness proxies and the statement that no protected attribute exists to test;",
            "- the limitations section in the first person.",
            "",
        ]
        lines += _provenance_table(payload)
        return "\n".join(lines) + "\n"
    headline = model["headline"]
    block = assumption_block(economics_config)
    lines += [
        "## Headline",
        "",
        f"- PR-AUC (primary) **{_value_with_source(headline['pr_auc'])}**, bootstrap 95 % CI "
        f"[{_v(headline['pr_auc_ci_low'])}, {_v(headline['pr_auc_ci_high'])}].",
        f"- AUROC is reported for comparability only and is deliberately de-emphasised: on a "
        f"{_v(payload['corpus']['ibmaml']['laundering_rate_pct'])} % base rate it is not the "
        "number that decides anything.",
        f"- Headline corpus `{_v(headline['corpus'])}`; model version "
        f"`{_v(headline['model_version'])}`; feature-spec hash "
        f"`{str(headline['feature_spec_hash']['value'])[:16]}…`.",
        f"- Band note carried with the figure: {_v(headline['band_note'])}",
        f"- Net benefit of the headline configuration: "
        f"{_money(int(headline['net_benefit_total_minor']['value']), economics_config, block)} "
        f"({headline['net_benefit_total_minor']['source']}).",
        f"- **Provenance: `{headline['provenance']}`.** {_v(model['provenance_note'])}",
        "",
        "## Walk-forward design",
        "",
        f"- Scheme `{_v(model['walk_forward']['scheme'])}`; shuffling: "
        f"**{'no' if not model['walk_forward']['shuffle']['value'] else 'yes'}** — temporal, "
        "never random, because a random split on transaction data is the classic tell.",
        f"- Embargo {_value_with_source(model['walk_forward']['embargo_days'])} days = longest "
        f"feature lookback {_value_with_source(model['walk_forward']['max_lookback_days'])}. "
        "The embargo exists because a 30-day rolling feature computed just after a boundary "
        "would otherwise see training-period data.",
        f"- Folds supplied: {_value_with_source(model['walk_forward']['folds_supplied'])}; "
        f"corpus span offered: {_value_with_source(model['walk_forward']['corpus_span_days'])}; "
        f"supports a literal walk-forward at that embargo: "
        f"**{_value_with_source(model['walk_forward']['supports_literal_walk_forward'])}**.",
        f"- {_v(model['walk_forward']['feasibility_note'])}",
        f"- {_v(model['metrics_note'])}",
        "",
        "## The ablation table",
        "",
        "The spec calls this the rigor score. One row per configuration per corpus, and the "
        "graph-features row is the thesis in one line.",
        "",
    ]
    rows = [
        [
            row["label"],
            row["corpus"],
            _v(row["pr_auc"]),
            f"{row['pr_auc_ci'][0]:.4f}–{row['pr_auc_ci'][1]:.4f}",
            str(row["auroc_comparability_only"]),
            str(row["brier"]),
            f"{row['net_benefit_total_minor']:,} {row['currency']}",
            row["provenance"],
        ]
        for row in model["ablation_rows"]
    ]
    lines += _table(
        [
            "variant",
            "corpus",
            "PR-AUC",
            "95 % CI",
            "AUROC",
            "Brier",
            "net benefit (minor)",
            "provenance",
        ],
        rows,
    )
    lines += [
        "",
        f"Source for every cell: `out/backtest/model_card.json#/ablation_table` "
        f"({_src(model['provenance_note'])}). Rows the harness expected: "
        + ", ".join(f"`{row}`" for row in model["expected_ablation_row_ids"])
        + ".",
        "",
        "## The leakage control is a test, not a boast",
        "",
        f"- A deliberately lookahead-leaking configuration is included as a control and must "
        f"win. Detected: **{_value_with_source(model['leakage_control']['detected'])}**, control "
        f"PR-AUC {_value_with_source(model['leakage_control']['control_pr_auc'])} against best "
        f"honest {_value_with_source(model['leakage_control']['best_honest_pr_auc'])}.",
        f"- {_v(model['leakage_control']['message'])}",
        "",
        "## Calibration, and recall against the typology labels",
        "",
        f"- Label source: {_value_with_source(model['per_typology_recall']['join_source'])} — "
        "built by `scripts/build_ibm_typologies.py`, joined FIFO per column key, failing closed "
        "on no match (DEV-014).",
        f"- Recall by typology, mean over folds: {_v(model['per_typology_recall']['recall_by_typology'])} "
        f"({_src(model['per_typology_recall']['recall_by_typology'])}).",
        f"- {_v(model['per_typology_recall']['negative_control_note'])}",
        "",
        "## Statistical hygiene",
        "",
        f"- Configurations evaluated: "
        f"{_value_with_source(model['overfitting_controls']['configs_evaluated'])}; selection on "
        f"`{model['overfitting_controls']['selection_on']}`; test fold touched exactly once: "
        f"**{model['overfitting_controls']['test_fold_touched_once']}** at "
        f"`{model['overfitting_controls']['test_fold_touched_at']}`.",
        f"- {_v(model['overfitting_controls']['multiple_testing_caveat'])}",
        "- Seed stability is published as mean ± sd across the configured seeds, never as the "
        "best of five.",
        f"- Monte Carlo: {_v(economics['monte_carlo'])} — the seed and the draw count are stored "
        "with the interval so a quoted tail can be reproduced or challenged.",
        f"- MLflow logged: **{_value_with_source(model['mlflow']['logged'])}** "
        f"({_v(model['mlflow']['reason'])}). Degraded, not failed: `run_id`, the feature-spec "
        "hash and the artifact digests still pin what produced each number.",
        "",
        "## Fairness and robustness",
        "",
        f"- {_v(model['fairness']['protected_attributes_note'])}",
        "- Proxy axes actually tested: "
        + ", ".join(
            f"`{axis['axis']}` ({axis['buckets']} buckets"
            + (f", reason: {axis['reason']}" if axis["reason"] else "")
            + ")"
            for axis in model["fairness"]["axes"]
        )
        + ".",
        f"- Perturbations: {_v(model['perturbations'])}",
        f"- Risk-adjusted benefit ratio is **not a Sharpe ratio**: "
        f"{model['risk_adjusted_benefit_ratio']['explicitly_not_sharpe_because']} "
        f"Formula: `{model['risk_adjusted_benefit_ratio']['formula']}`, value "
        f"{model['risk_adjusted_benefit_ratio']['value']}.",
        "",
        "## Drift",
        "",
        _drift_lines(model["drift"]),
        "",
        "## Limitations of this card",
        "",
        f"{len(payload['limitations'])} named, measured weaknesses are in "
        "[LIMITATIONS.md](LIMITATIONS.md). A model card without a limitations section is "
        "marketing, and the plan's own rejection triggers say so.",
        "",
    ]
    lines += _provenance_table(payload)
    return "\n".join(lines) + "\n"


def _drift_lines(drift: Mapping[str, Any]) -> str:
    if drift["status"] == "present":
        return (
            f"Published from `{drift['artifact']}` ({drift['files']} files, e.g. "
            f"{', '.join(drift['sample'])})."
        )
    return (
        f"**Not published.** This section needs `{drift['artifact']}`, produced by "
        f"`{drift['produced_by']}` (stage {drift['stage']}). It will carry:"
        + "".join(f"\n  - {item}" for item in drift["what_will_go_here"])
        + "\n\nNo PSI value is printed until the artifact exists: a drift table generated from "
        "intent would report stability that was never measured, and the action at "
        "drift.psi_action is the reason the number matters."
    )


def render_economics_card(payload: Mapping[str, Any]) -> str:
    economics = payload["economics"]
    economics_config = load_economics(_ROOT.get())
    block = assumption_block(economics_config)
    lines = _banner("ECONOMICS CARD", payload)
    lines += [
        "Every currency figure in OXBOW is a function of `config/economics.yaml` plus observed",
        "data, and of nothing else. This card is that file rendered verbatim beside the policy",
        "comparison those assumptions price.",
        "",
        "> " + payload["disclaimer"],
        "",
        "## The assumptions, verbatim",
        "",
        "```text",
        economics["assumptions_verbatim"],
        "```",
        "",
        "## The knobs, each with the key that owns it",
        "",
    ]
    lines += _table(
        ["config key", "value", "what it decides"],
        [
            [
                "`currency`",
                _v(economics["currency"]),
                "the single pricing currency; no implicit FX exists",
            ],
            [
                "`minor_units_per_major`",
                _v(economics["minor_units_per_major"]),
                "render-time division only — money stays integer end to end",
            ],
            [
                "`recovery.rate`",
                _v(economics["recovery"]["rate"]),
                "the share of exposure a timely intervention prevents",
            ],
            [
                "`recovery.sensitivity_band`",
                _v(economics["recovery"]["sensitivity_band"]),
                "every headline renders over these three values",
            ],
            [
                "`recovery.bounds_exclusive`",
                _v(economics["recovery"]["bounds_exclusive"]),
                "admissible open interval; 0 and 1 degenerate the ranking and are rejected at load",
            ],
            [
                "`analyst.cost_per_hour_minor`",
                _v(economics["analyst"]["cost_per_hour_minor"]),
                "fully loaded analyst hour; must divide exactly by 60",
            ],
            [
                "`analyst.cost_per_minute_minor`",
                _v(economics["analyst"]["cost_per_minute_minor"]),
                "c_i in EV_i",
            ],
            [
                "`analyst.hours_per_period`",
                _v(economics["analyst"]["hours_per_period"]),
                "the period benefit-per-analyst-hour is quoted over",
            ],
            [
                "`analyst.min_review_minutes`",
                _v(economics["analyst"]["min_review_minutes"]),
                "floor on m_i, the EV-density denominator; zero is rejected at load",
            ],
            [
                "`review_minutes_by_alert_class`",
                _v(economics["review_minutes_by_alert_class"]),
                "m_i per scorecard band A-E",
            ],
            [
                "`exposure`",
                _v(economics["exposure"]),
                "the E_i definition: window and downstream hops",
            ],
            [
                "`friction_cost_minor`",
                _v(economics["friction_cost_minor"]),
                "f, applied to the (1 - p_i) term",
            ],
            [
                "`capacity`",
                _v(economics["capacity"]),
                "B per period, the operating point, and the sweep grid",
            ],
            [
                "`monte_carlo`",
                _v(economics["monte_carlo"]),
                "the unactioned-exposure interval: draws, depth, seed, quantiles",
            ],
            [
                "`solver`",
                _v(economics["solver"]),
                "greedy latency budget, CP-SAT deadline, agreement tolerance",
            ],
            [
                "`four_eyes.threshold_exposure_minor`",
                _v(economics["four_eyes_threshold_minor"]),
                "exposure above which a second subject must confirm",
            ],
            [
                "`tail_risk`",
                _v(economics["tail_risk"]),
                "VaR and expected-shortfall levels on unreviewed exposure",
            ],
        ],
    )
    lines += [
        "",
        "## The formula",
        "",
        "```text",
        "EV_i = p_i * E_i * r - c_i - (1 - p_i) * f        ranked by EV density EV_i / m_i",
        "```",
        "",
        "`E_i` is exposure at risk: the value still interceptable — funds flowing out of the",
        "account and its declared-hop downstream within the recovery window after the first",
        "triggering event, capped at inflow observed in the same window. The cap is part of the",
        "definition rather than a knob, and it is why an account that has already paid out",
        "everything cannot be credited with value that was never there.",
        "",
        "## Threshold policy vs EV policy — pricing the queue, in money",
        "",
    ]
    comparison = economics["policy_comparison"]
    if comparison["status"] == "present":
        lines += [
            f"Artifact row `{comparison['variant']}` on corpus `{comparison['corpus']}`; "
            f"provenance `{comparison['provenance']}`.",
            "",
        ]
        rows = []
        for name, policy in sorted(comparison["policies"].items()):
            minor = policy["net_benefit_total_minor"]
            per_hour = policy["benefit_per_analyst_hour_minor"]
            gap = policy["optimality_gap_minor"]
            rows.append(
                [
                    f"`{name}`",
                    policy["allocator"],
                    f"{minor:,} {economics['currency']['value']}"
                    if isinstance(minor, int)
                    else "—",
                    f"{per_hour:,}" if isinstance(per_hour, int) else "—",
                    f"{gap:,}" if isinstance(gap, int) else "n/a",
                    str(policy["max_drawdown_minor"]),
                    str(policy["zero_drawdown_labelled"]),
                ]
            )
        lines += _table(
            [
                "policy",
                "allocator",
                "net benefit (minor)",
                "per analyst-hour (minor)",
                "optimality gap (minor)",
                "max drawdown (minor)",
                "zero drawdown labelled",
            ],
            rows,
        )
        lines += [
            "",
            "Figures are stored in integer minor units of "
            f"`{economics['currency']['value']}` and rendered divided by "
            f"{_v(economics['minor_units_per_major'])} — for example the headline net benefit is "
            f"{_money(int(comparison['policies']['score_threshold']['net_benefit_total_minor']), economics_config, block)}"
            " under the assumptions above.",
            "",
            "Greedy by EV density is what a real triage desk would do and answers instantly; CP-SAT",
            "is the exact 0/1 knapsack under a hard deadline with a labelled fallback when it",
            f"misses ({_v(economics['solver'])}). The agreement tolerance "
            f"`solver.agreement_tolerance_ratio` is the finding, not a detail: \"the simple policy",
            'is within 2 % of optimal" is a stronger statement than a marginally better number.',
            "",
        ]
    else:
        lines += [
            f"**Not published.** Requires `{comparison['artifact']}` (`{comparison['produced_by']}`). "
            "No policy comparison is printed rather than printing one from intent.",
            "",
        ]
    sweep = economics["capacity_sweep"]
    lines += ["## Capacity sweep and the efficient frontier", ""]
    if sweep["status"] == "present":
        lines += [f"Published from `{sweep['artifact']}` ({sweep['files']} files).", ""]
    else:
        lines += [
            f"**Not published.** This section needs `{sweep['artifact']}`, produced by "
            f"`{sweep['produced_by']}` (stage {sweep['stage']}). It will carry:",
            "",
        ]
        lines += [f"- {item}" for item in sweep["what_will_go_here"]]
        lines += [
            "",
            "The capacity cutoff line is on the never-cut list, so it gets the real sweep or it",
            "stays empty and says why. A frontier drawn from intent is the rejection trigger in",
            "plan §18 with axis labels on it.",
            "",
        ]
    lines += [
        "## Reading a money figure in this product",
        "",
        "1. The figure, in major units with its ISO code.",
        "2. The basis line: what the number is a function of at that moment (r, window, minutes).",
        "3. The assumption block above, verbatim, naming the keys.",
        "",
        "If a surface shows a currency figure without items 2 and 3, that is a defect to file, not",
        "a formatting preference.",
        "",
    ]
    lines += _provenance_table(payload)
    return "\n".join(lines) + "\n"


def render_limitations(payload: Mapping[str, Any]) -> str:
    lines = _banner("LIMITATIONS", payload)
    lines += [
        "Written in the first person, because a list of disclaimers is decoration and a list of",
        "measurements is honesty. Each item below states what is wrong with this system, the",
        "number that shows it, and the artifact that number came from. Plan §15 asks for at",
        f"least {MINIMUM_LIMITATIONS}; there are {len(payload['limitations'])}.",
        "",
        "> " + payload["disclaimer"],
        "",
    ]
    for index, item in enumerate(payload["limitations"], start=1):
        lines += [
            f"## {index}. `{item['id']}`",
            "",
            _flat(item["claim"]),
            "",
            "**Evidence**",
            "",
        ]
        lines += [f"- {_v(entry)} — {_src(entry)}" for entry in item["evidence"]]
        if item.get("consequence"):
            lines += ["", f"**What follows.** {_flat(item['consequence'])}"]
        lines.append("")
    lines += [
        "## What we did instead of hiding these",
        "",
        "- The corpus that was supposed to carry the network thesis was measured before the code",
        "  depended on it, failed both pre-committed conditions, and the fallback fired on real",
        "  data rather than on optimism (DEV-011).",
        "- The rule definition that disagreed with the corpus's own labels was raised as a",
        "  measured conflict with the anatomy of the disagreement attached, not quietly retuned",
        "  until a test went green (DEV-015).",
        "- The acquisition that was parked as blocking turned out to rest on a wrong number: the",
        "  corpus was 8.18 GB rather than 41.6 GB, and one scenario bundle was enough (DEV-013).",
        "- Every published number in this repository carries a `source:` pointer, so a stale",
        "  document is a detectable condition rather than an argument.",
        "",
        "## What is *not* a limitation, and should not be mistaken for one",
        "",
        "- The absence of live data is a constraint the track rule imposes and the design honours:",
        "  ingest refuses any source not declared in `config/sources.yaml`.",
        "- The absence of a re-identifiable subject is enforced at ingest: every account key is a",
        "  salted one-way hash, and erasure destroys the mapping while leaving the audit chain",
        "  verifiable.",
        "- The absence of a financial-advice claim is the disclaimer, and it is on the README, the",
        "  footer of every page and page one of every exported packet.",
        "",
    ]
    lines += _provenance_table(payload)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI entry point


def main(argv: Sequence[str] | None = None) -> int:
    """``oxbow eval``: regenerate every metric, curve, frontier row, ablation row and drift
    table from the artifacts on disk, render the documents from that output, and verify the
    dataset card against the figures it claims."""
    parser = argparse.ArgumentParser(
        prog="oxbow eval", description=(main.__doc__ or "").strip().splitlines()[0]
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: the one holding config/).",
    )
    parser.add_argument(
        "--json-only", action="store_true", help="Write eval.json without touching the documents."
    )
    parser.add_argument("--quiet", action="store_true", help="Print only the status line.")
    args = parser.parse_args(argv)
    from oxbow.config import find_repo_root

    root = Path(args.root) if args.root else find_repo_root()
    try:
        report = run_eval(root, write_docs=not args.json_only)
    except FileNotFoundError as exc:
        print(f"[eval] refusing to publish: {exc}", file=sys.stderr)
        return 3
    audit: CardAudit = report["card_audit"]
    if not args.quiet:
        print(f"[eval] wrote {report['eval_json'].relative_to(root)}")
        for document in report["documents"]:
            print(f"[eval] rendered {document.relative_to(root)}")
        print(f"[eval] limitations named: {report['limitation_count']}")
        if report["harness_provenance"]:
            print(
                f"[eval] NOTE: statistical and economic figures carry provenance "
                f"`{PROVENANCE_FAKE}` — they verify the harness, not a corpus"
            )
        # The card is the one published document this command does not write. It checks it
        # figure by figure and prints the whole verdict, skips included.
        print(_safe(f"[eval] verified {audit.card_path} (written by hand, never by this run)"))
        for line in audit.summary().splitlines():
            print(_safe(f"[card] {line}"))
        for gap in report["gaps"]:
            print(f"[eval] not yet published: {gap}")
    if not audit.ok:
        print(
            _safe(f"[eval] {'CARD DRIFT AND INCOMPLETE' if report['gaps'] else 'CARD DRIFT'}"),
            file=sys.stderr,
        )
        return 6 if report["gaps"] else 5
    print(f"[eval] {'INCOMPLETE' if report['gaps'] else 'COMPLETE'}")
    return 4 if report["gaps"] else 0


__all__ = [
    "ARTIFACTS",
    "EVAL_JSON_RELPATH",
    "EVAL_VERSION",
    "HASH_SIZE_LIMIT_BYTES",
    "MINIMUM_LIMITATIONS",
    "NO_NUMBER",
    "PROVENANCE_CONFIG",
    "PROVENANCE_FAKE",
    "PROVENANCE_MEASURED",
    "PROVENANCE_RECORDED",
    "SCENARIO_DRESSING",
    "Metric",
    "artifact_descriptors",
    "build_eval_payload",
    "corpus_section",
    "cycle_reality_section",
    "economics_section",
    "graph_thesis_section",
    "integrity_section",
    "limitations_section",
    "main",
    "metric",
    "model_section",
    "render_architecture",
    "render_documents",
    "render_economics_card",
    "render_limitations",
    "render_model_card",
    "render_readme",
    "run_eval",
    "typologies_section",
    "verify_dataset_card",
    "write_eval_json",
]
