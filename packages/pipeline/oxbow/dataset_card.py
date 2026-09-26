"""The dataset-card gate behind ``make eval``: verify ``data/DATASET_CARD.md``, never write it.

Why this exists. ``oxbow eval`` used to *render* the dataset card, which meant every run
overwrote authored judgement with whatever a script could derive -- and the last such run
deleted the PaySim reuse figure that decided the architecture, breaking
``tests/contracts/test_p1b_canonical_ingest.py::test_counterparty_reuse_reported``. A card
carries claims no artifact can generate (what a label means, which licence binds a
derivative, what was refused and why, which corpus owns which module), so it is authored,
and a script's job is to keep it honest rather than to write it.

How it works. Each check names one figure the card states, the pointer that owns it, and a
regex locating the stated text inside one section of the card. The expected value is read
from that owner -- a committed measurement artifact, ``config/``, a file's size on this
host, the typology parquet, the raw PaySim bytes, or the DECISIONS.md entry that is the
only home of a figure. Three outcomes, never two:

* ``PASS``    the stated text equals the owner's value.
* ``FAIL``    they differ, or the card no longer states the figure at all. The field, the
              owner and both values are named.
* ``SKIPPED`` the owner is not available here: no corpus bytes, no artifact, or a file too
              big for a documentation run to re-hash (``make data`` owns those digests). The
              path and the reason are printed, and a skipped check is never folded into the
              pass total -- "could not look" is not "looked and agreed", which is how
              ``scripts/verify.py`` treats a missing prerequisite.

A check whose section heading has disappeared from the card reports SKIPPED with the heading
named rather than passing, so restructuring the document surfaces here instead of quietly
switching the gate off.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import polars as pl
import yaml

CARD_RELPATH: Final = "data/DATASET_CARD.md"

#: Mirrors ``oxbow.eval.HASH_SIZE_LIMIT_BYTES``: digests below this are recomputed from the
#: bytes here; above it the digest belongs to ``make data``, and the check says so.
HASH_SIZE_LIMIT_BYTES: Final = 32 * 1024 * 1024

PASS: Final = "PASS"
FAIL: Final = "FAIL"
SKIPPED: Final = "SKIPPED"

PAYSIM = "data/graph_measurement.json"
IBM = "data/ibm_graph_measurement.json"
CYCLES = "data/ibm_cycle_measurement.json"
MANIFEST = "data/download_manifest.json"
TYPOLOGIES = "data/processed/ibm_typologies.parquet"
PAYSIM_CSV = "data/raw/paysim/PS_20174392719_1491204439457_log.csv"
IBM_DIR = "data/raw/ibmaml"

#: Figures whose owner is not a measurement artifact. Each entry states what the gate does
#: about it, so a reader is not left to work out which claims were checked and which were
#: repeated. Nothing here is silently counted as passing.
OWNERSHIP_NOTES: Final[Mapping[str, str]] = {
    "ibm.slug_total_bytes": (
        "8,176,169,418 bytes is Kaggle's own `datasets/list` figure. Its only home in this "
        "repository is DECISIONS.md DEV-013, so the check compares the card against that "
        "entry; no artifact this build writes can contradict it"
    ),
    "ibm.median_degree_including_self_loops": (
        "6.0 with self-loops counted is recorded in DECISIONS.md DEV-013 and emitted by no "
        "artifact: re-measuring it means rebuilding the degree distribution over 475 MB, "
        "which is `scripts/measure_ibm_graph.py`'s job, not a documentation gate's. NOT "
        "CHECKED"
    ),
    "ibm.labelled_rows_near_degree_1": (
        "'the laundering-flagged rows are near-degree-1 among themselves' is a DEV-013 "
        "footnote. NOT CHECKED, and the card says so"
    ),
    "sampling.four_node_cycle_survival": (
        "0.13 % is arithmetic on the stated 19 % node-sampling rate (0.19 ** 4), checked "
        "against that arithmetic rather than against an artifact"
    ),
}

# ---------------------------------------------------------------------------
# locating a figure inside the card
# ---------------------------------------------------------------------------

# A window is the text between two headings. They name structure, not wording.
_WINDOWS: Final[Mapping[str, tuple[str, str]]] = {
    "paysim": ("## 1. PaySim1", "## 2. IBM"),
    "ibm": ("## 2. IBM", "### 2a."),
    "typologies": ("### 2a. Typology ground truth", "**R4's definition"),
    "cycles": ("**R4's definition", "## 3. Cited"),
    "sampling": ("## 5. Sampling", "## 6. Artifact"),
}


def _sections(card: str) -> dict[str, str]:
    """The card cut into the windows checks search, empty when a heading has moved."""
    sections: dict[str, str] = {}
    for name, (start, end) in _WINDOWS.items():
        begin = card.find(start)
        stop = card.find(end, begin + len(start)) if begin >= 0 else -1
        sections[name] = card[begin:stop] if begin >= 0 and stop > begin else ""
    return sections


def _norm(text: str) -> str:
    """Comparison form: emphasis markers and dash spellings are formatting, not figures."""
    text = text.replace("`", "").replace("*", "")
    text = text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    return " ".join(text.split()).strip().rstrip(".")


def _equal(stated: str, expected: str) -> bool:
    return _norm(stated) == _norm(expected)


def _contains_all(stated: str, expected: str) -> bool:
    """Every ``·``-separated token of the owning value must appear in the card's text."""
    haystack = _norm(stated)
    tokens = [token for token in expected.split("\u00b7") if token.strip()]
    return bool(tokens) and all(_norm(token) in haystack for token in tokens)


def _name_counts(stated: str, expected: str) -> bool:
    """``Cheque 1,864,331 · ACH 600,797`` against the same mapping, any order."""

    def parse(text: str) -> dict[str, str]:
        pairs: dict[str, str] = {}
        for chunk in text.split("\u00b7"):
            match = re.match(r"([A-Za-z][A-Za-z ()/-]*?)\s+([\d.,]+)$", chunk.strip())
            if not match:
                return {}
            pairs[match.group(1).strip()] = _num(match.group(2))
        return pairs

    stated_map, expected_map = parse(stated), parse(expected)
    return bool(expected_map) and stated_map == expected_map


def _name_list(stated: str, expected: str) -> bool:
    """A comma-separated set of names, order-insensitive; anything before a colon is prose."""

    def parse(text: str) -> set[str]:
        tail = text.split(":", 1)[-1]
        return {part.strip(" .") for part in tail.split(",") if part.strip(" .")}

    stated_set, expected_set = parse(stated), parse(expected)
    return bool(expected_set) and stated_set == expected_set


def _float_seq(stated: str, expected: str) -> bool:
    """A ``·``-separated run of decimals, position by position."""

    def parse(text: str) -> list[str]:
        return [part for part in (chunk.strip() for chunk in text.split("\u00b7")) if part]

    stated_seq, expected_seq = parse(stated), parse(expected)
    return bool(expected_seq) and stated_seq == expected_seq


def _num(text: Any) -> str:
    """Canonical number text: ``6,362,620`` and ``6362620`` and ``14.0`` are one number."""
    cleaned = str(text).strip().replace(",", "").replace("_", "")
    try:
        value = float(cleaned)
    except ValueError:
        return str(text)
    return str(int(value)) if value.is_integer() else cleaned


def _int_text(value: Any) -> str:
    return f"{int(value):,}"


class Skip(Exception):  # noqa: N818 - a control-flow signal, not a failure
    """A check's owner is not available on this host: report SKIPPED, never PASS.

    The name is deliberate: an ``...Error`` suffix would read as drift, and drift and
    "could not look" are the two states this gate must never confuse.
    """


# ---------------------------------------------------------------------------
# the owners
# ---------------------------------------------------------------------------


class Facts:
    """A lazy, read-only view of the artifacts, configs and bytes that own the figures."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._json: dict[str, dict[str, Any]] = {}
        self._yaml: dict[str, dict[str, Any]] = {}
        self._frames: dict[str, pl.DataFrame] = {}
        self._recounts: dict[str, str] = {}

    # --- committed measurement artifacts and config -----------------------
    def json(self, relpath: str) -> dict[str, Any]:
        if relpath in self._json:
            return self._json[relpath]
        path = self.root / relpath
        if not path.is_file():
            raise Skip(f"{relpath} is not on this host")
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise Skip(f"{relpath} does not parse: {exc}") from exc
        if not isinstance(parsed, dict):
            raise Skip(f"{relpath} does not hold an object")
        self._json[relpath] = parsed
        return parsed

    def config(self, name: str) -> dict[str, Any]:
        relpath = f"config/{name}.yaml"
        if relpath in self._yaml:
            return self._yaml[relpath]
        path = self.root / relpath
        if not path.is_file():
            raise Skip(f"{relpath} is not on this host")
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise Skip(f"{relpath} does not hold a mapping")
        self._yaml[relpath] = parsed
        return parsed

    def source(self, index: int, source_id: str) -> Mapping[str, Any]:
        """One declared source, addressed by index *and* id so a reordered config is noticed."""
        entries = self.config("sources").get("sources")
        if not isinstance(entries, list) or len(entries) <= index:
            raise Skip(f"config/sources.yaml#/sources/{index} does not exist")
        entry = entries[index]
        if not isinstance(entry, Mapping) or str(entry.get("id")) != source_id:
            raise Skip(f"config/sources.yaml#/sources/{index} is not {source_id!r}")
        return entry

    def field(self, relpath: str, pointer: str) -> Any:
        """One value out of one JSON artifact; a moved field is a Skip, never a guess."""
        document: Any = self.json(relpath)
        for part in pointer.split("/"):
            if not part:
                continue
            if isinstance(document, Mapping) and part in document:
                document = document[part]
            elif (
                isinstance(document, Sequence) and not isinstance(document, str) and part.isdigit()
            ):
                index = int(part)
                if index >= len(document):
                    raise Skip(f"{relpath}#{pointer}: index {index} is past the end")
                document = document[index]
            else:
                raise Skip(f"{relpath}#{pointer} does not resolve at {part!r}")
        return document

    def recorded(self, pattern: str) -> str:
        """A figure whose only home is a DECISIONS.md entry: compare the card against it."""
        path = self.root / "DECISIONS.md"
        if not path.is_file():
            raise Skip("DECISIONS.md is not on this host")
        match = re.search(pattern, path.read_text(encoding="utf-8"))
        if match is None:
            raise Skip(f"DECISIONS.md no longer states the figure matching {pattern!r}")
        return match.group("num")

    # --- bytes on this host ------------------------------------------------
    def path(self, relpath: str) -> Path:
        path = self.root / relpath
        if not path.is_file():
            raise Skip(f"{relpath} is not on this host")
        return path

    def size(self, relpath: str) -> str:
        return _int_text(self.path(relpath).stat().st_size)

    def digest(self, relpath: str) -> str:
        path = self.path(relpath)
        size = path.stat().st_size
        if size > HASH_SIZE_LIMIT_BYTES:
            raise Skip(
                f"{relpath} is {size:,} bytes, above the {HASH_SIZE_LIMIT_BYTES:,}-byte limit a "
                "documentation run hashes at; scripts/download_data.py --verify-only re-reads "
                "these bytes on every `make data`"
            )
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def declared_file(
        self, source_index: int, source_id: str, file_index: int
    ) -> Mapping[str, Any]:
        files = self.source(source_index, source_id).get("files")
        if not isinstance(files, list) or len(files) <= file_index:
            raise Skip(f"config/sources.yaml#/sources/{source_index}/files/{file_index} absent")
        entry = files[file_index]
        return entry if isinstance(entry, Mapping) else {}

    def declared_digest(self, source_index: int, source_id: str, file_index: int) -> str:
        declared = str(self.declared_file(source_index, source_id, file_index).get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", declared):
            raise Skip(
                f"config/sources.yaml#/sources/{source_index}/files/{file_index} holds no "
                f"measured digest (it says {declared!r})"
            )
        return declared

    def declared_name(self, source_index: int, source_id: str, file_index: int) -> str:
        return str(self.declared_file(source_index, source_id, file_index).get("name", ""))

    def code_constant(self, name: str) -> str:
        """A figure declared in code: read the line that pins it, no import of the module."""
        relpath = "packages/pipeline/oxbow/ingest/canonical.py"
        path = self.path(relpath)
        match = re.search(rf'{name}: Final = "(?P<value>[^"]+)"', path.read_text(encoding="utf-8"))
        if match is None:
            raise Skip(f"{name} is no longer a plain string constant in {relpath}")
        return match.group("value")

    # --- the typology table ------------------------------------------------
    def typologies(self) -> pl.DataFrame:
        if "typologies" in self._frames:
            return self._frames["typologies"]
        path = self.root / TYPOLOGIES
        if not path.is_file():
            raise Skip(f"{TYPOLOGIES} is not built (scripts/build_ibm_typologies.py)")
        frame = pl.read_parquet(path)
        self._frames["typologies"] = frame
        return frame

    def typology_counts(self) -> dict[str, int]:
        frame = self.typologies()
        if "typology" not in frame.columns:
            raise Skip(f"{TYPOLOGIES} has no typology column")
        return {
            str(row["typology"]): _as_int(row["count"])
            for row in frame["typology"].value_counts().to_dicts()
        }

    # --- the raw PaySim bytes ---------------------------------------------
    def paysim_raw(self) -> pl.DataFrame:
        """The corpus itself, five columns wide. ~3 s once per run, then cached."""
        if "paysim" in self._frames:
            return self._frames["paysim"]
        directory = self.root / "data/raw/paysim"
        matches = sorted(directory.glob("*.csv")) if directory.is_dir() else []
        if not matches:
            raise Skip(
                f"the raw PaySim CSV is not on this host ({PAYSIM_CSV}); `make data` acquires "
                "it, and data/graph_measurement.json still owns the row count"
            )
        frame = pl.read_csv(
            matches[0],
            columns=["step", "nameOrig", "nameDest", "isFraud", "isFlaggedFraud"],
            schema_overrides={"isFraud": pl.Int64, "isFlaggedFraud": pl.Int64},
        )
        self._frames["paysim"] = frame
        return frame

    def paysim_fraud_subset(self) -> pl.DataFrame:
        """The ``isFraud == 1`` rows DEV-011 measured its subgraph claim on."""
        if "paysim_fraud" in self._frames:
            return self._frames["paysim_fraud"]
        frame = self.paysim_raw().filter(pl.col("isFraud") == 1)
        self._frames["paysim_fraud"] = frame
        return frame

    def paysim_recount(self, what: str) -> str:
        if what in self._recounts:
            return self._recounts[what]
        raw = self.paysim_raw()
        fraud = self.paysim_fraud_subset
        if what == "rows":
            value = _int_text(raw.height)
        elif what == "fraud_rows":
            value = _int_text(fraud().height)
        elif what == "fraud_rate_pct":
            value = f"{100.0 * fraud().height / raw.height:.4f}"
        elif what == "flagged_rows":
            value = _int_text(_as_int(raw["isFlaggedFraud"].sum()))
        elif what == "step_range":
            value = f"{_as_int(raw['step'].min())}\u2013{_as_int(raw['step'].max())}"
        elif what == "fraud_reuse":
            rows, distinct = fraud().height, fraud()["nameOrig"].n_unique()
            value = f"{(0.0 if rows == 0 else 1 - distinct / rows):.1f}"
        elif what == "fraud_max_degree":
            degrees: Counter[str] = Counter()
            for sender, receiver in zip(
                fraud()["nameOrig"].to_list(), fraud()["nameDest"].to_list(), strict=True
            ):
                degrees[str(sender)] += 1
                degrees[str(receiver)] += 1
            value = _int_text(max(degrees.values())) if degrees else "0"
        else:  # pragma: no cover - a programming error, not a data state
            raise Skip(f"no re-count is implemented for {what!r}")
        self._recounts[what] = value
        return value


def _as_int(value: Any) -> int:
    """One number out of a polars aggregation, without mypy's union trailing the call site."""
    if value is None:
        raise Skip("a re-count over the corpus bytes came back null")
    return int(value)


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------

Expected = Callable[[Facts], str]


@dataclass(frozen=True, slots=True)
class Check:
    """One figure, the pointer that owns it, and the text the card must carry."""

    field: str
    window: str
    owner: str
    pattern: str
    expected: Expected
    equal: Callable[[str, str], bool] = _equal

    def stated_in(self, window_text: str) -> str | None:
        match = re.search(self.pattern, window_text)
        return None if match is None else match.group("value")


def _row(label: str, value: str = r"[\d,]+") -> str:
    """Match a table row's leading number, by the field label in its first column."""
    return rf"\|\s*{label}\s*\|\s*(?P<value>{value})"


def _pct(prefix: str) -> str:
    """Match ``<prefix> **NN.NN %**`` and hand back the bare number."""
    return rf"{prefix}\s*\*\*(?P<value>[\d.]+)\s*%\*\*"


def _declared_bias(source_index: int, source_id: str, bias_index: int) -> Expected:
    """One ``known_biases`` entry, flattened the way the card renders it.

    A factory rather than a lambda in the comprehension below: mypy cannot infer a lambda's
    parameters inside a generator, and a declared bias is text the card must carry verbatim.
    """

    def expected(facts: Facts) -> str:
        entry = facts.source(source_index, source_id)
        return " ".join(str(entry["known_biases"][bias_index]).split())

    return expected


CHECKS: Final[tuple[Check, ...]] = (
    # --- PaySim1: what the slug served, and what the bytes say -------------
    Check(
        "paysim.source_url",
        "paysim",
        "config/sources.yaml#/sources/0/source_url",
        r"\|\s*Source\s*\|\s*`?(?P<value>https://[^|`\s]+)",
        lambda f: str(f.source(0, "paysim")["source_url"]),
    ),
    Check(
        "paysim.retrieved_at",
        "paysim",
        f"{MANIFEST}#/paysim/files/0/retrieved_at",
        r"\|\s*Retrieved\s*\|\s*(?P<value>\d{4}-\d\d-\d\dT[\d:]+Z)",
        lambda f: str(f.field(MANIFEST, "/paysim/files/0/retrieved_at")).replace("+00:00", "Z"),
    ),
    Check(
        "paysim.archive_bytes",
        "paysim",
        f"{MANIFEST}#/paysim/archive/bytes",
        r"\|\s*Archive\s*\|\s*`paysim\.zip`,\s*(?P<value>[\d,]+)\s*bytes",
        lambda f: _int_text(f.field(MANIFEST, "/paysim/archive/bytes")),
    ),
    Check(
        "paysim.archive_sha256",
        "paysim",
        f"{MANIFEST}#/paysim/archive/sha256",
        r"\|\s*Archive\s*\|\s*.*?SHA-256\s*`?(?P<value>[0-9a-f]{64})",
        lambda f: str(f.field(MANIFEST, "/paysim/archive/sha256")),
    ),
    Check(
        "paysim.file_name",
        "paysim",
        f"{PAYSIM}#/file",
        _row(r"File read", r"[^|]+"),
        lambda f: str(f.field(PAYSIM, "/file")),
        _contains_all,
    ),
    Check(
        "paysim.file_bytes",
        "paysim",
        f"{MANIFEST}#/paysim/files/0/bytes",
        _row("File bytes"),
        lambda f: _int_text(f.field(MANIFEST, "/paysim/files/0/bytes")),
    ),
    Check(
        "paysim.bytes_on_host",
        "paysim",
        f"os.stat({PAYSIM_CSV})",
        _row("File bytes"),
        lambda f: f.size(PAYSIM_CSV),
    ),
    Check(
        "paysim.sha256_declared",
        "paysim",
        "config/sources.yaml#/sources/0/files/0/sha256",
        r"\|\s*SHA-256\s*\|\s*`?(?P<value>[0-9a-f]{64})",
        lambda f: f.declared_digest(0, "paysim", 0),
    ),
    Check(
        "paysim.sha256_in_manifest",
        "paysim",
        f"{MANIFEST}#/paysim/files/0/sha256",
        r"\|\s*SHA-256\s*\|\s*`?(?P<value>[0-9a-f]{64})",
        lambda f: str(f.field(MANIFEST, "/paysim/files/0/sha256")),
    ),
    Check(
        "paysim.rows",
        "paysim",
        f"{PAYSIM}#/n_rows",
        _row("Rows"),
        lambda f: _int_text(f.field(PAYSIM, "/n_rows")),
    ),
    Check(
        "paysim.rows_recounted",
        "paysim",
        f"row count re-taken from {PAYSIM_CSV}",
        _row("Rows"),
        lambda f: f.paysim_recount("rows"),
    ),
    Check(
        "paysim.distinct_nameOrig",
        "paysim",
        f"{PAYSIM}#/n_distinct_nameOrig",
        r"\|\s*Distinct `nameOrig`\s*\|\s*(?P<value>[\d,]+)",
        lambda f: _int_text(f.field(PAYSIM, "/n_distinct_nameOrig")),
    ),
    Check(
        "paysim.distinct_nameDest",
        "paysim",
        f"{PAYSIM}#/n_distinct_nameDest",
        r"\|\s*Distinct `nameDest`\s*\|\s*(?P<value>[\d,]+)",
        lambda f: _int_text(f.field(PAYSIM, "/n_distinct_nameDest")),
    ),
    Check(
        "paysim.distinct_accounts",
        "paysim",
        f"{PAYSIM}#/counterparty_degree/accounts",
        _row(r"Distinct accounts[^\|]*"),
        lambda f: _int_text(f.field(PAYSIM, "/counterparty_degree/accounts")),
    ),
    Check(
        "paysim.reuse_ratio",
        "paysim",
        f"{PAYSIM}#/reuse_ratio",
        r"Sender reuse ratio[^|]*\|\s*\*\*(?P<value>0\.\d+)\*\*",
        lambda f: f"{float(f.field(PAYSIM, '/reuse_ratio')):.6f}",
    ),
    Check(
        "paysim.reuse_ratio_arithmetic",
        "paysim",
        f"1 - {PAYSIM}#/n_distinct_nameOrig / {PAYSIM}#/n_rows",
        r"Sender reuse ratio[^|]*\|\s*\*\*(?P<value>0\.\d+)\*\*",
        lambda f: f"{1 - float(f.field(PAYSIM, '/n_distinct_nameOrig')) / float(f.field(PAYSIM, '/n_rows')):.6f}",
    ),
    Check(
        "paysim.isfraud_rows",
        "paysim",
        f"sum(isFraud) over {PAYSIM_CSV}",
        r"\|\s*`isFraud`\s*\|\s*(?P<value>[\d,]+)\s*rows",
        lambda f: f.paysim_recount("fraud_rows"),
    ),
    Check(
        "paysim.isfraud_rate_pct",
        "paysim",
        f"sum(isFraud) / rows, both re-taken from {PAYSIM_CSV}",
        _pct(r"\|\s*`isFraud`\s*\|\s*[\d,]+\s*rows\s*=\s*"),
        lambda f: f.paysim_recount("fraud_rate_pct"),
    ),
    Check(
        "paysim.isflaggedfraud_rows",
        "paysim",
        f"sum(isFlaggedFraud) over {PAYSIM_CSV}",
        r"\|\s*`isFlaggedFraud`\s*\|\s*\*\*(?P<value>[\d,]+)\s+rows in",
        lambda f: f.paysim_recount("flagged_rows"),
    ),
    Check(
        "paysim.isflaggedfraud_denominator",
        "paysim",
        f"{PAYSIM}#/n_rows",
        r"`isFlaggedFraud`[^|]*\|\s*\*\*[\d,]+ rows in\s*(?P<value>[\d,]+)\*\*",
        lambda f: _int_text(f.field(PAYSIM, "/n_rows")),
    ),
    Check(
        "paysim.step_range",
        "paysim",
        f"min/max(step) over {PAYSIM_CSV}",
        r"\|\s*`step` range\s*\|\s*(?P<value>[\d,]+\s*[\u2013-]\s*[\d,]+)",
        lambda f: f.paysim_recount("step_range"),
    ),
    Check(
        "paysim.step_hours",
        "paysim",
        "config/pipeline.yaml#/paysim/step_hours",
        r"Synthetic timeline[^|]*\|\s*each `step`\s*[^|]*?(?P<value>\d+)\s*h from",
        lambda f: str(int(f.config("pipeline")["paysim"]["step_hours"])),
    ),
    Check(
        "paysim.epoch_utc",
        "paysim",
        "config/pipeline.yaml#/paysim/epoch_utc",
        r"Synthetic timeline\s*\|[^|]*h from\s*`?(?P<value>\d{4}-\d\d-\d\dT[\d:]+Z)",
        lambda f: str(f.config("pipeline")["paysim"]["epoch_utc"]),
    ),
    Check(
        "paysim.currency",
        "paysim",
        "packages/pipeline/oxbow/ingest/canonical.py :: PAYSIM_CURRENCY",
        _row("Currency", r"[A-Z]{3}"),
        lambda f: f.code_constant("PAYSIM_CURRENCY"),
    ),
    Check(
        "paysim.median_total_degree",
        "paysim",
        f"{PAYSIM}#/degree_total/median",
        _row("Median total degree", r"[\d.]+"),
        lambda f: f"{float(f.field(PAYSIM, '/degree_total/median')):.1f}",
    ),
    Check(
        "paysim.median_counterparty_degree",
        "paysim",
        f"{PAYSIM}#/counterparty_degree/median",
        _row("Median counterparty degree", r"[\d.]+"),
        lambda f: f"{float(f.field(PAYSIM, '/counterparty_degree/median')):.1f}",
    ),
    Check(
        "paysim.counterparty_degree_threshold",
        "paysim",
        f"{PAYSIM}#/thresholds/median_counterparty_degree_gt",
        r"Median counterparty degree[^|]*\|\s*[\d.]+[^|]*condition\s*\*{2}>\s*(?P<value>\d+)\*{2}",
        lambda f: _num(f.field(PAYSIM, "/thresholds/median_counterparty_degree_gt")),
    ),
    Check(
        "paysim.degree_p90_p99_max",
        "paysim",
        f"{PAYSIM}#/counterparty_degree",
        r"\|\s*Degree p90 / p99 / max\s*\|\s*(?P<value>[\d.,]+ / [\d.,]+ / [\d.,]+)",
        lambda f: "{} / {} / {}".format(
            _num(f.field(PAYSIM, "/counterparty_degree/p90")),
            _num(f.field(PAYSIM, "/counterparty_degree/p99")),
            _int_text(f.field(PAYSIM, "/counterparty_degree/max")),
        ),
    ),
    Check(
        "paysim.top_sender_edges",
        "paysim",
        f"{PAYSIM}#/top20_senders",
        r"\|\s*Highest-degree `nameOrig`\s*\|\s*(?P<value>[\d,]+)\s*edges",
        lambda f: _int_text(max(int(item["edges"]) for item in f.field(PAYSIM, "/top20_senders"))),
    ),
    Check(
        "paysim.cycles_found",
        "paysim",
        f"{PAYSIM}#/cycles/cycles_found",
        r"\|\s*Time-respecting 3[\u2013-]6 cycles\s*\|\s*(?P<value>[\d,]+),",
        lambda f: _int_text(f.field(PAYSIM, "/cycles/cycles_found")),
    ),
    Check(
        "paysim.cycles_min_count",
        "paysim",
        f"{PAYSIM}#/thresholds/cycles_min_count",
        r"Time-respecting 3[\u2013-]6 cycles[^|]*\|\s*[\d,]+,[^|]*condition\s*\*{2}[\u2265>]=?\s*(?P<value>[\d,]+)\*{2}",
        lambda f: _int_text(f.field(PAYSIM, "/thresholds/cycles_min_count")),
    ),
    Check(
        "paysim.cycle_sample_rows",
        "paysim",
        f"{PAYSIM}#/cycles/sample_rows",
        r"Time-respecting 3[\u2013-]6 cycles[^|]*\|\s*[\d,]+,[^|]*?(?P<value>[\d,]+)-row sample",
        lambda f: _int_text(f.field(PAYSIM, "/cycles/sample_rows")),
    ),
    Check(
        "paysim.verdict",
        "paysim",
        f"{PAYSIM}#/verdict",
        r"\|\s*Verdict\s*\|\s*`?(?P<value>[A-Z0-9_]+)",
        lambda f: str(f.field(PAYSIM, "/verdict")),
    ),
    Check(
        "paysim.fraud_subset_reuse",
        "paysim",
        f"1 - distinct(nameOrig) / rows over isFraud==1 in {PAYSIM_CSV}",
        r"the sender reuse ratio is exactly\s*\*{2}(?P<value>[\d.]+)\*{2}",
        lambda f: f.paysim_recount("fraud_reuse"),
    ),
    Check(
        "paysim.fraud_subset_max_degree",
        "paysim",
        f"max total degree over isFraud==1 in {PAYSIM_CSV}",
        r"maximum\s+total degree is\s*\*{2}(?P<value>\d+)\*{2}",
        lambda f: f.paysim_recount("fraud_max_degree"),
    ),
    Check(
        "paysim.license",
        "paysim",
        "config/sources.yaml#/sources/0/license",
        r"\|\s*Licence\s*\|\s*\*{2}(?P<value>[A-Z0-9 .\-]+)\*{2}",
        lambda f: str(f.source(0, "paysim")["license"]),
    ),
    Check(
        "paysim.citation",
        "paysim",
        "config/sources.yaml#/sources/0/citation",
        _row("Citation", r"[^|]+"),
        lambda f: " ".join(str(f.source(0, "paysim")["citation"]).split()),
    ),
    Check(
        "paysim.label_caveat",
        "paysim",
        "config/sources.yaml#/sources/0/label_caveat",
        _row("Label caveat", r"[^|]+"),
        lambda f: " ".join(str(f.source(0, "paysim")["label_caveat"]).split()),
    ),
    Check(
        "paysim.synthetic_fields",
        "paysim",
        "config/sources.yaml#/sources/0/synthetic_fields",
        _row("Synthetic fields", r"[^|]+"),
        lambda f: ", ".join(str(name) for name in f.source(0, "paysim")["synthetic_fields"]),
    ),
    *(
        Check(
            f"paysim.known_bias_{index}",
            "paysim",
            f"config/sources.yaml#/sources/0/known_biases/{index}",
            _row(f"Known bias {index + 1}", r"[^|]+"),
            _declared_bias(0, "paysim", index),
        )
        for index in range(3)
    ),
    # --- IBM-AML, HI-Small only -------------------------------------------
    Check(
        "ibm.source_url",
        "ibm",
        "config/sources.yaml#/sources/1/source_url",
        r"\|\s*Source\s*\|\s*`?(?P<value>https://[^|`\s]+)",
        lambda f: str(f.source(1, "ibmaml")["source_url"]),
    ),
    Check(
        "ibm.slug_total_bytes",
        "ibm",
        "DECISIONS.md DEV-013 (Kaggle `datasets/list` totalBytes)",
        _row(r"Size of what the slug serves", r"[\d,]+") + r"\s*bytes",
        lambda f: f.recorded(r"totalBytes\s*=\s*(?P<num>[\d,]+)"),
    ),
    Check(
        "ibm.acquisition_scope",
        "ibm",
        "config/sources.yaml#/sources/1/acquisition_scope",
        _row("Acquired", r"[^|]+"),
        lambda f: " \u00b7 ".join(
            re.findall(r"~[\d.]+\s*(?:MB|GB)", str(f.source(1, "ibmaml")["acquisition_scope"]))
        ),
        _contains_all,
    ),
    Check(
        "ibm.bundle",
        "ibm",
        f"{IBM}#/source",
        _row("Acquired", r"[^|]+"),
        lambda f: _bundle_name(str(f.field(IBM, "/source"))),
        _contains_all,
    ),
    Check(
        "ibm.trans_bytes",
        "ibm",
        f"os.stat({IBM_DIR}/HI-Small_Trans.csv)",
        r"\|\s*`HI-Small_Trans\.csv`\s*\|\s*(?P<value>[\d,]+)\s*bytes",
        lambda f: f.size(f"{IBM_DIR}/HI-Small_Trans.csv"),
    ),
    Check(
        "ibm.trans_sha256_declared",
        "ibm",
        "config/sources.yaml#/sources/1/files/0/sha256",
        r"`HI-Small_Trans\.csv`[^|]*\|\s*[\d,]+\s*bytes,\s*SHA-256\s*`?(?P<value>[0-9a-f]{64})",
        lambda f: f.declared_digest(1, "ibmaml", 0),
    ),
    Check(
        "ibm.trans_sha256_on_host",
        "ibm",
        f"sha256({IBM_DIR}/HI-Small_Trans.csv)",
        r"`HI-Small_Trans\.csv`[^|]*\|\s*[\d,]+\s*bytes,\s*SHA-256\s*`?(?P<value>[0-9a-f]{64})",
        lambda f: f.digest(f"{IBM_DIR}/HI-Small_Trans.csv"),
    ),
    Check(
        "ibm.accounts_bytes",
        "ibm",
        f"os.stat({IBM_DIR}/HI-Small_accounts.csv)",
        r"\|\s*`HI-Small_accounts\.csv`\s*\|\s*(?P<value>[\d,]+)\s*bytes",
        lambda f: f.size(f"{IBM_DIR}/HI-Small_accounts.csv"),
    ),
    Check(
        "ibm.accounts_sha256_declared",
        "ibm",
        "config/sources.yaml#/sources/1/files/1/sha256",
        r"`HI-Small_accounts\.csv`[^|]*\|\s*[\d,]+\s*bytes,\s*SHA-256\s*`?(?P<value>[0-9a-f]{64})",
        lambda f: f.declared_digest(1, "ibmaml", 1),
    ),
    Check(
        "ibm.accounts_sha256_on_host",
        "ibm",
        f"sha256({IBM_DIR}/HI-Small_accounts.csv)",
        r"`HI-Small_accounts\.csv`[^|]*\|\s*[\d,]+\s*bytes,\s*SHA-256\s*`?(?P<value>[0-9a-f]{64})",
        lambda f: f.digest(f"{IBM_DIR}/HI-Small_accounts.csv"),
    ),
    Check(
        "ibm.patterns_bytes",
        "ibm",
        f"os.stat({IBM_DIR}/HI-Small_Patterns.txt)",
        r"\|\s*`HI-Small_Patterns\.txt`\s*\|\s*(?P<value>[\d,]+)\s*bytes",
        lambda f: f.size(f"{IBM_DIR}/HI-Small_Patterns.txt"),
    ),
    Check(
        "ibm.patterns_sha256_declared",
        "ibm",
        "config/sources.yaml#/sources/1/files/2/sha256",
        r"`HI-Small_Patterns\.txt`[^|]*\|\s*[\d,]+\s*bytes,\s*SHA-256\s*`?(?P<value>[0-9a-f]{64})",
        lambda f: f.declared_digest(1, "ibmaml", 2),
    ),
    Check(
        "ibm.patterns_sha256_recomputed",
        "ibm",
        f"sha256({IBM_DIR}/HI-Small_Patterns.txt), recomputed by `make eval`",
        r"`HI-Small_Patterns\.txt`[^|]*\|\s*[\d,]+\s*bytes,\s*SHA-256\s*`?(?P<value>[0-9a-f]{64})",
        lambda f: f.digest(f"{IBM_DIR}/HI-Small_Patterns.txt"),
    ),
    Check(
        "ibm.rows",
        "ibm",
        f"{IBM}#/n_transaction_rows",
        _row("Rows"),
        lambda f: _int_text(f.field(IBM, "/n_transaction_rows")),
    ),
    Check(
        "ibm.distinct_accounts",
        "ibm",
        f"{IBM}#/n_distinct_accounts",
        _row("Distinct accounts"),
        lambda f: _int_text(f.field(IBM, "/n_distinct_accounts")),
    ),
    Check(
        "ibm.directed_edges_excl_self_loops",
        "ibm",
        f"{IBM}#/n_directed_edges_excl_self_loops",
        _row(r"Directed edges[^\|]*"),
        lambda f: _int_text(f.field(IBM, "/n_directed_edges_excl_self_loops")),
    ),
    Check(
        "ibm.median_degree",
        "ibm",
        f"{IBM}#/degree_median",
        r"\|\s*\*{2}Median account degree\*{2}\s*\|\s*\*{2}(?P<value>[\d.]+)\*{2}",
        lambda f: f"{float(f.field(IBM, '/degree_median')):.1f}",
    ),
    Check(
        "ibm.degree_p90_p99_max",
        "ibm",
        f"{IBM}#/degree_p90, #/degree_p99, #/degree_max",
        r"\|\s*Degree p90 / p99 / max\s*\|\s*(?P<value>[\d.,]+ / [\d.,]+ / [\d.,]+)",
        lambda f: "{} / {} / {}".format(
            _num(f.field(IBM, "/degree_p90")),
            _num(f.field(IBM, "/degree_p99")),
            _int_text(f.field(IBM, "/degree_max")),
        ),
    ),
    Check(
        "ibm.median_counterparties",
        "ibm",
        f"{IBM}#/median_counterparties_per_account",
        _row("Median counterparties per account", r"[\d.]+"),
        lambda f: f"{float(f.field(IBM, '/median_counterparties_per_account')):.1f}",
    ),
    Check(
        "ibm.accounts_over_2_counterparties",
        "ibm",
        f"{IBM}#/accounts_with_more_than_2_counterparties",
        _row(r"Accounts with > 2 counterparties"),
        lambda f: _int_text(f.field(IBM, "/accounts_with_more_than_2_counterparties")),
    ),
    Check(
        "ibm.self_loop_rows",
        "ibm",
        f"{IBM}#/n_self_loop_rows",
        r"\|\s*Self-edge rows\s*\|\s*(?P<value>[\d,]+)\s*=",
        lambda f: _int_text(f.field(IBM, "/n_self_loop_rows")),
    ),
    Check(
        "ibm.self_loop_pct",
        "ibm",
        f"{IBM}#/n_self_loop_rows / {IBM}#/n_transaction_rows",
        _pct(r"Self-edge rows[^|]*\|\s*[\d,]+\s*=\s*"),
        lambda f: f"{100.0 * float(f.field(IBM, '/n_self_loop_rows')) / float(f.field(IBM, '/n_transaction_rows')):.1f}",
    ),
    Check(
        "ibm.laundering_rows",
        "ibm",
        f"{IBM}#/laundering_rows",
        r"\|\s*`Is Laundering`\s*\|\s*(?P<value>[\d,]+)\s*rows",
        lambda f: _int_text(f.field(IBM, "/laundering_rows")),
    ),
    Check(
        "ibm.laundering_rate_pct",
        "ibm",
        f"{IBM}#/laundering_rate_pct",
        _pct(r"`Is Laundering`[^|]*\|\s*[\d,]+\s*rows\s*=\s*"),
        lambda f: _num(f.field(IBM, "/laundering_rate_pct")),
    ),
    Check(
        "ibm.temporal_range",
        "ibm",
        f"{IBM}#/temporal_range",
        r"\|\s*Temporal range\s*\|\s*(?P<value>\d{4}-\d\d-\d\d [\d:]+ [^\|]+)",
        lambda f: " \u2192 ".join(str(item) for item in f.field(IBM, "/temporal_range")),
    ),
    Check(
        "ibm.window_days",
        "ibm",
        f"{IBM}#/temporal_range, derived",
        r"\|\s*Span\s*\|\s*(?P<value>[\d.]+)\s*days",
        lambda f: str(_window_days([str(x) for x in f.field(IBM, "/temporal_range")])),
    ),
    Check(
        "ibm.n_currencies",
        "ibm",
        f"{IBM}#/n_currencies",
        r"\|\s*Currencies\s*\|\s*(?P<value>\d+),",
        lambda f: _int_text(f.field(IBM, "/n_currencies")),
    ),
    Check(
        "ibm.currency_names",
        "ibm",
        f"{IBM}#/currencies",
        _row("Currencies", r"[^|]+"),
        lambda f: ", ".join(str(name) for name, _ in f.field(IBM, "/currencies")),
        _name_list,
    ),
    Check(
        "ibm.payment_formats",
        "ibm",
        f"{IBM}#/payment_formats",
        _row("Payment formats", r"[^|]+"),
        lambda f: " \u00b7 ".join(
            f"{name} {_int_text(count)}" for name, count in f.field(IBM, "/payment_formats")
        ),
        _name_counts,
    ),
    Check(
        "ibm.measured_date",
        "ibm",
        f"{IBM}#/measured_at_utc",
        r"\|\s*Measured\s*\|\s*(?P<value>\d{4}-\d\d-\d\d)\s+by",
        lambda f: str(f.field(IBM, "/measured_at_utc"))[:10],
    ),
    Check(
        "ibm.license",
        "ibm",
        "config/sources.yaml#/sources/1/license",
        r"\|\s*Licence\s*\|\s*\*{2}(?P<value>[A-Za-z0-9 .\-]+)\*{2}",
        lambda f: str(f.source(1, "ibmaml")["license"]),
    ),
    Check(
        "ibm.citation",
        "ibm",
        "config/sources.yaml#/sources/1/citation",
        _row("Citation", r"[^|]+"),
        lambda f: " ".join(str(f.source(1, "ibmaml")["citation"]).split()),
    ),
    Check(
        "ibm.label_caveat",
        "ibm",
        "config/sources.yaml#/sources/1/label_caveat",
        _row("Label caveat", r"[^|]+"),
        lambda f: " ".join(str(f.source(1, "ibmaml")["label_caveat"]).split()),
    ),
    Check(
        "ibm.source_timezone_assumption",
        "ibm",
        "config/pipeline.yaml#/ibmaml/source_timezone_assumption",
        _row("Timestamp assumption", r"[^|]+"),
        lambda f: str(f.config("pipeline")["ibmaml"]["source_timezone_assumption"]),
        _contains_all,
    ),
    *(
        Check(
            f"ibm.known_bias_{index}",
            "ibm",
            f"config/sources.yaml#/sources/1/known_biases/{index}",
            _row(f"Known bias {index + 1}", r"[^|]+"),
            _declared_bias(1, "ibmaml", index),
        )
        for index in range(5)
    ),
    # --- typology ground truth ----------------------------------------------
    Check(
        "typologies.annotated_transactions",
        "typologies",
        f"{TYPOLOGIES}#rows",
        _row("Annotated transactions"),
        lambda f: _int_text(f.typologies().height),
    ),
    Check(
        "typologies.attempt_blocks",
        "typologies",
        f"{TYPOLOGIES}#attempt_id(n_unique)",
        _row("Labelled attempt blocks"),
        lambda f: _int_text(f.typologies()["attempt_id"].n_unique()),
    ),
    Check(
        "typologies.typology_count",
        "typologies",
        f"{TYPOLOGIES}#typology(n_unique)",
        _row("Typologies"),
        lambda f: _int_text(len(f.typology_counts())),
    ),
    Check(
        "typologies.per_typology",
        "typologies",
        f"{TYPOLOGIES}#typology(value_counts)",
        _row("Per typology", r"[^|]+"),
        lambda f: " \u00b7 ".join(
            f"{name} {_int_text(count)}" for name, count in sorted(f.typology_counts().items())
        ),
        _name_counts,
    ),
    Check(
        "typologies.cycle_rows",
        "typologies",
        f"{TYPOLOGIES}#typology=CYCLE",
        _row("CYCLE rows"),
        lambda f: _int_text(f.typology_counts().get("CYCLE", 0)),
    ),
    Check(
        "typologies.fan_rows",
        "typologies",
        f"{TYPOLOGIES}#typology=FAN-OUT + FAN-IN",
        _row(r"Fan rows[^\|]*"),
        lambda f: _int_text(
            f.typology_counts().get("FAN-OUT", 0) + f.typology_counts().get("FAN-IN", 0)
        ),
    ),
    Check(
        "typologies.negative_control",
        "typologies",
        f"{TYPOLOGIES}#typology=RANDOM",
        _row(r"Negative control[^\|]*"),
        lambda f: _int_text(f.typology_counts().get("RANDOM", 0)),
    ),
    # --- DEV-015: the spec'd cycle definition against the labelled cycles ---
    Check(
        "cycles.labelled_blocks",
        "cycles",
        f"{CYCLES}#/labelled_cycle_anatomy/labelled_cycle_blocks",
        _row("Labelled CYCLE blocks"),
        lambda f: _int_text(f.field(CYCLES, "/labelled_cycle_anatomy/labelled_cycle_blocks")),
    ),
    Check(
        "cycles.close_as_directed_loops",
        "cycles",
        f"{CYCLES}#/labelled_cycle_anatomy/blocks_that_close",
        _row("Close as directed loops"),
        lambda f: _int_text(f.field(CYCLES, "/labelled_cycle_anatomy/blocks_that_close")),
    ),
    Check(
        "cycles.cross_currency",
        "cycles",
        f"{CYCLES}#/labelled_cycle_anatomy/blocks_cross_currency",
        _row("Cross-currency"),
        lambda f: _int_text(f.field(CYCLES, "/labelled_cycle_anatomy/blocks_cross_currency")),
    ),
    Check(
        "cycles.below_retention_floor",
        "cycles",
        f"{CYCLES}#/labelled_cycle_anatomy/blocks_below_0_6_retention",
        r"\|\s*Below the [\d.]+ value-retention floor\s*\|\s*(?P<value>[\d,]+)",
        lambda f: _int_text(f.field(CYCLES, "/labelled_cycle_anatomy/blocks_below_0_6_retention")),
    ),
    Check(
        "cycles.value_retention_floor",
        "cycles",
        "config/pipeline.yaml#/graph/cycles/value_retention_floor",
        r"\|\s*Below the (?P<value>[\d.]+) value-retention floor",
        lambda f: _num(_graph_floor(f)),
    ),
    Check(
        "cycles.two_leg_round_trips",
        "cycles",
        f"{CYCLES}#/labelled_cycle_anatomy/blocks_with_only_two_legs",
        _row("Two-leg round-trips"),
        lambda f: _int_text(f.field(CYCLES, "/labelled_cycle_anatomy/blocks_with_only_two_legs")),
    ),
    Check(
        "cycles.not_time_monotonic",
        "cycles",
        f"{CYCLES}#/labelled_cycle_anatomy/blocks_with_non_monotonic_timestamps",
        _row("Not time-monotonic"),
        lambda f: _int_text(
            f.field(CYCLES, "/labelled_cycle_anatomy/blocks_with_non_monotonic_timestamps")
        ),
    ),
    Check(
        "cycles.worst_twelve_retentions",
        "cycles",
        f"{CYCLES}#/labelled_cycle_anatomy/retention_ratios",
        r"\|\s*Worst twelve retention ratios\s*\|\s*(?P<value>[\d.]+\s*[\u2013-]\s*[\d.]+)",
        lambda f: _worst_twelve_text(f.field(CYCLES, "/labelled_cycle_anatomy/retention_ratios")),
    ),
    # --- sampling rule and split boundaries --------------------------------
    Check(
        "sampling.interactive_txn_target",
        "sampling",
        "config/pipeline.yaml#/sampling/interactive_txn_target",
        r"\|\s*Interactive target\s*\|\s*(?P<value>[\d,]+)\s*transactions",
        lambda f: _int_text(f.config("pipeline")["sampling"]["interactive_txn_target"]),
    ),
    Check(
        "sampling.strategy",
        "sampling",
        "config/pipeline.yaml#/sampling/strategy",
        r"\|\s*Sampling strategy\s*\|\s*`?(?P<value>[A-Za-z_]+)",
        lambda f: str(f.config("pipeline")["sampling"]["strategy"]),
    ),
    Check(
        "sampling.seed",
        "sampling",
        "config/pipeline.yaml#/seed",
        r"\|\s*Seed\s*\|\s*(?P<value>[\d,]+),\s*propagated",
        lambda f: _int_text(f.config("pipeline")["seed"]),
    ),
    Check(
        "splits.n_folds",
        "sampling",
        "config/splits.yaml#/walk_forward/n_folds",
        r"\|\s*Folds\s*\|\s*(?P<value>\d+),",
        lambda f: _int_text(f.config("splits")["walk_forward"]["n_folds"]),
    ),
    Check(
        "splits.scheme",
        "sampling",
        "config/splits.yaml#/walk_forward/scheme",
        r"\|\s*Folds\s*\|\s*\d+,\s*(?P<value>[A-Za-z ]+),",
        lambda f: str(f.config("splits")["walk_forward"]["scheme"]).replace("_", " "),
    ),
    Check(
        "splits.shuffle",
        "sampling",
        "config/splits.yaml#/walk_forward/shuffle",
        r"shuffle:\s*`?(?P<value>true|false)",
        lambda f: str(bool(f.config("splits")["walk_forward"]["shuffle"])).lower(),
    ),
    Check(
        "splits.embargo_and_purge_days",
        "sampling",
        "config/splits.yaml#/walk_forward/embargo_days, #/purge_days",
        r"\|\s*Embargo / purge\s*\|\s*(?P<value>\d+ days / \d+ days?)",
        lambda f: "{} days / {} day{}".format(
            f.config("splits")["walk_forward"]["embargo_days"],
            f.config("splits")["walk_forward"]["purge_days"],
            "" if int(f.config("splits")["walk_forward"]["purge_days"]) == 1 else "s",
        ),
    ),
    Check(
        "splits.train_end_fracs",
        "sampling",
        "config/splits.yaml#/walk_forward/folds",
        r"\|\s*Split boundaries, `train_end`\s*\|\s*(?P<value>[\d.\u00b7 ]+)",
        lambda f: " \u00b7 ".join(f"{float(fold['train_end_frac']):.2f}" for fold in _folds(f)),
        _float_seq,
    ),
    Check(
        "splits.test_end_fracs",
        "sampling",
        "config/splits.yaml#/walk_forward/folds",
        r"\|\s*Split boundaries, `test_end`\s*\|\s*(?P<value>[\d.\u00b7 ]+)",
        lambda f: " \u00b7 ".join(f"{float(fold['test_end_frac']):.2f}" for fold in _folds(f)),
        _float_seq,
    ),
    Check(
        "splits.validation_fraction",
        "sampling",
        "config/splits.yaml#/validation/fraction_of_train",
        r"\|\s*Validation slice\s*\|\s*(?P<value>[\d.]+)\s*of each training period",
        lambda f: f"{float(f.config('splits')['validation']['fraction_of_train']):.2f}",
    ),
    Check(
        "splits.entity_disjoint",
        "sampling",
        "config/splits.yaml#/entity_disjoint/method, #/holdout_fraction, #/report_as",
        _row("Entity-disjoint control", r"[^|]+"),
        lambda f: " \u00b7 ".join(
            (
                str(f.config("splits")["entity_disjoint"]["method"]),
                f"{float(f.config('splits')['entity_disjoint']['holdout_fraction']):.2f}",
                str(f.config("splits")["entity_disjoint"]["report_as"]),
            )
        ),
        _contains_all,
    ),
    Check(
        "sampling.four_node_cycle_survival",
        "sampling",
        "arithmetic on the stated 19 % per-node sampling rate: 0.19 ** 4",
        r"node sampling about\s*(?P<value>[\d.]+)\s*% of the time",
        lambda _: f"{0.19 ** 4 * 100:.2f}",
    ),
)


# ---------------------------------------------------------------------------
# shared derivations
# ---------------------------------------------------------------------------


def _bundle_name(source: str) -> str:
    """The scenario bundle a measurement's ``source`` line names, e.g. ``HI-Small``."""
    match = re.search(r"\b[A-Z]{2}-[A-Za-z]+\b", source)
    if match is None:
        raise Skip(f"{IBM}#/source names no scenario bundle: {source!r}")
    return match.group(0)


def _window_days(range_: Sequence[str]) -> float | int:
    moments = [datetime.fromisoformat(text.replace(" ", "T")) for text in range_]
    if len(moments) != 2:
        raise Skip(f"a temporal range needs two endpoints, found {len(moments)}")
    days = round((moments[1] - moments[0]).total_seconds() / 86400.0, 2)
    return int(days) if days.is_integer() else days


def _worst_twelve_text(ratios: Sequence[Any]) -> str:
    if len(ratios) < 12:
        raise Skip(f"the cycle artifact lists {len(ratios)} retention ratios, not twelve")
    return f"{float(ratios[0]):.4f} \u2013 {float(ratios[11]):.4f}"


def _graph_floor(facts: Facts) -> Any:
    try:
        return facts.config("pipeline")["graph"]["cycles"]["value_retention_floor"]
    except (KeyError, TypeError) as exc:
        raise Skip("config/pipeline.yaml has no graph.cycles.value_retention_floor") from exc


def _folds(facts: Facts) -> list[Mapping[str, Any]]:
    folds = facts.config("splits")["walk_forward"]["folds"]
    if not isinstance(folds, list) or not folds:
        raise Skip("config/splits.yaml#/walk_forward/folds is empty")
    return [fold for fold in folds if isinstance(fold, Mapping)]


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    """One check's verdict, with both values wherever both exist."""

    field: str
    owner: str
    status: str
    stated: str = ""
    expected: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CardAudit:
    """The whole verification: every check that ran, and what it found."""

    card_path: str
    outcomes: tuple[Outcome, ...]

    def _with_status(self, status: str) -> tuple[Outcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.status == status)

    @property
    def failures(self) -> tuple[Outcome, ...]:
        return self._with_status(FAIL)

    @property
    def skipped(self) -> tuple[Outcome, ...]:
        return self._with_status(SKIPPED)

    @property
    def passed(self) -> tuple[Outcome, ...]:
        return self._with_status(PASS)

    @property
    def ok(self) -> bool:
        return not self.failures

    def counts(self) -> str:
        return (
            f"{len(self.passed)} passed, {len(self.failures)} failed, "
            f"{len(self.skipped)} skipped, {len(self.outcomes)} checks"
        )

    def failure_lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        for outcome in self.failures:
            if outcome.stated == "<absent>":
                lines.append(
                    f"  {outcome.field}: the card no longer states this figure, which "
                    f"{outcome.owner} owns"
                )
            else:
                lines.append(
                    f"  {outcome.field}: card states {outcome.stated.strip()!r}, "
                    f"{outcome.owner} says {outcome.expected!r}"
                )
        return tuple(lines)

    def skipped_lines(self) -> tuple[str, ...]:
        return tuple(
            f"  {outcome.field} [{outcome.owner}]: {outcome.reason}" for outcome in self.skipped
        )

    def summary(self) -> str:
        lines = [f"dataset card {self.card_path}: {self.counts()}"]
        if self.failures:
            lines.append("DRIFT, the card and the artifacts that own these figures disagree:")
            lines.extend(self.failure_lines())
        lines.append("checks that could not run, and are therefore not claimed as passing:")
        lines.extend(self.skipped_lines() or ["  (none)"])
        lines.extend(
            f"  {field} -- owned by prose, not an artifact: {why}"
            for field, why in sorted(OWNERSHIP_NOTES.items())
        )
        return "\n".join(lines)


def verify_dataset_card(root: Path, *, card_text: str | None = None) -> CardAudit:
    """Compare every figure the card states against the artifact or config that owns it.

    ``card_text`` verifies a *proposed* card rather than the one on disk, which is what lets
    the mutation test prove this gate bites on a changed number instead of trusting that it
    would.
    """
    root = Path(root)
    text = card_text if card_text is not None else (root / CARD_RELPATH).read_text(encoding="utf-8")
    sections = _sections(text)
    facts = Facts(root)
    outcomes: list[Outcome] = []
    for check in CHECKS:
        window = sections.get(check.window, "")
        if not window:
            start, end = _WINDOWS[check.window]
            outcomes.append(
                Outcome(
                    check.field,
                    check.owner,
                    SKIPPED,
                    reason=f"the card has no {check.window!r} window between {start!r} and "
                    f"{end!r}, so this figure cannot be located",
                )
            )
            continue
        stated = check.stated_in(window)
        if stated is None:
            outcomes.append(
                Outcome(
                    check.field,
                    check.owner,
                    FAIL,
                    stated="<absent>",
                    reason="the card no longer states this figure",
                )
            )
            continue
        try:
            expected = check.expected(facts)
        except Skip as exc:
            outcomes.append(
                Outcome(check.field, check.owner, SKIPPED, stated=stated, reason=str(exc))
            )
            continue
        except OSError as exc:
            outcomes.append(
                Outcome(
                    check.field,
                    check.owner,
                    SKIPPED,
                    stated=stated,
                    reason=f"the owner could not be read: {exc}",
                )
            )
            continue
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            outcomes.append(
                Outcome(
                    check.field,
                    check.owner,
                    FAIL,
                    stated=stated,
                    reason=f"the owning value could not be read: {type(exc).__name__}: {exc}",
                )
            )
            continue
        if check.equal(stated, expected):
            outcomes.append(
                Outcome(check.field, check.owner, PASS, stated=stated, expected=expected)
            )
        else:
            outcomes.append(
                Outcome(
                    check.field,
                    check.owner,
                    FAIL,
                    stated=stated,
                    expected=expected,
                    reason="the stated figure and the owning value differ",
                )
            )
    return CardAudit(card_path=CARD_RELPATH, outcomes=tuple(outcomes))


def safe(text: str) -> str:
    """Console-safe text: Windows' default stdout codec cannot encode an en dash."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


__all__ = [
    "CARD_RELPATH",
    "CHECKS",
    "FAIL",
    "HASH_SIZE_LIMIT_BYTES",
    "OWNERSHIP_NOTES",
    "PASS",
    "SKIPPED",
    "CardAudit",
    "Check",
    "Facts",
    "Outcome",
    "Skip",
    "safe",
    "verify_dataset_card",
]
