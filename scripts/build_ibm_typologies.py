"""Turn IBM-AML's pattern annotations into per-typology ground truth.

`HI-Small_Patterns.txt` is not a table. It is the transaction stream interleaved
with section markers — `BEGIN LAUNDERING ATTEMPT - CYCLE:`, `... - FAN-IN:`,
`... - GATHER-SCATTER:` and so on — that group consecutive rows into labelled
laundering attempts. The corpus therefore carries, for free, exactly what plan §8
says the statistical metrics must prove: **per-typology recall**, which is the
difference between detecting a network and detecting expensive accounts.

PaySim offers no such thing. Its `isFraud` is one narrow behaviour with no
typology, which is the whole reason DEV-011 promoted IBM-AML to primary for
Module B.

Why a join on row content rather than a row offset: the pattern file is a filtered
projection of the transaction file, so positions drift. The composite of the
non-label columns identifies a transaction, and a FIFO per key handles the
duplicate rows honestly — an annotation is consumed by exactly one transaction, and
if a key runs out of unconsumed matches the build fails loudly rather than
guessing which row the label meant. `Is Laundering` is deliberately not part of the
key: it is the label, and keying on it would let a mislabelled row silently break
the join.

    uv run python scripts/build_ibm_typologies.py

Writes `data/processed/ibm_typologies.parquet`: one row per annotated transaction
with its attempt id, typology, and the row's ordinal in the source file.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Final

import duckdb
import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANS_CSV = REPO_ROOT / "data" / "raw" / "ibmaml" / "HI-Small_Trans.csv"
PATTERNS_TXT = REPO_ROOT / "data" / "raw" / "ibmaml" / "HI-Small_Patterns.txt"
OUT_PARQUET = REPO_ROOT / "data" / "processed" / "ibm_typologies.parquet"

# The header repeats the column name `Account` for sender and receiver; positional
# names are the only safe reading (DEV-013).
COLUMN_NAMES: Final[tuple[str, ...]] = (
    "ts",
    "from_bank",
    "from_account",
    "to_bank",
    "to_account",
    "amount_received",
    "receiving_ccy",
    "amount_paid",
    "payment_ccy",
    "payment_format",
    "is_laundering",
)

# Marker lines come in two observed shapes, with and without a trailing
# description: `BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 16-degree Fan-Out` and
# `BEGIN LAUNDERING ATTEMPT - STACK`. Parsing the prefix and partitioning on the
# first colon tolerates both, where one regex with an optional group would not.
BEGIN_PREFIX: Final = "BEGIN LAUNDERING ATTEMPT - "
END_PREFIX: Final = "END LAUNDERING ATTEMPT - "
# `HI-Small_Trans.csv` writes `2022/09/01 00:20`; `HI-Small_Patterns.txt` writes
# `2022-09-01 00:06`. The two files of one bundle disagree about the date separator,
# so a join on the raw text matches nothing. Both are accepted here and normalised
# to dashes on both sides — and `test_...` below keeps the rejection of any *third*
# format, because silently accepting a shape you have not looked at is how a join
# quietly returns zero rows and a run still goes green.
TS_PATTERN: Final = r"^\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}$"
TS_RE: Final = re.compile(TS_PATTERN)
# The annotation file is not column-addressable, so a row is recognised by its
# timestamp PREFIX. Applying the anchored column pattern to a whole line would
# match nothing, which is the failure this separate pattern exists to avoid.
TS_LINE_RE: Final = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2},")


def normalize_ts(text: str) -> str:
    """Canonicalise IBM's two timestamp spellings to one form."""
    return text.replace("/", "-")


# Columns that identify the transaction. `is_laundering` is excluded on purpose.
KEY_COLUMNS: Final[tuple[str, ...]] = COLUMN_NAMES[:10]


def die(message: str) -> None:
    """Fail closed: an unlabelled typology table is worse than no table."""
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def transaction_index() -> dict[tuple[str, ...], deque[int]]:
    """Map each transaction key to the queue of its row ordinals, in file order."""
    names = ",".join(f"'{n}'" for n in COLUMN_NAMES)
    types = ",".join(["'VARCHAR'"] * len(COLUMN_NAMES))
    con = duckdb.connect()
    # Built by concatenation, not an f-string: the regex contains quantifiers in
    # braces, which a formatted literal would try to interpret as a field.
    # The selected `ts` is normalised to dashes so it can meet the pattern file.
    query = (
        "SELECT regexp_replace(ts, '/', '-', 'g') AS ts, "
        + ", ".join(f'"{n}"' for n in KEY_COLUMNS[1:])
        + " FROM read_csv('"
        + TRANS_CSV.as_posix()
        + "', header=true, names=["
        + names
        + "], types=["
        + types
        + "], ignore_errors=true, all_varchar=true) WHERE regexp_matches(ts, '"
        + TS_PATTERN
        + "')"
    )
    try:
        rows = con.execute(query).fetchall()
    except duckdb.Error as exc:  # surfaced verbatim; a silent empty table is the failure
        die(f"could not read {TRANS_CSV.name}: {exc}")
    index: dict[tuple[str, ...], deque[int]] = defaultdict(deque)
    for ordinal, row in enumerate(rows):
        index[tuple(row)].append(ordinal)
    return index


def parse_patterns(index: dict[tuple[str, ...], deque[int]]) -> list[dict[str, object]]:
    """Walk the annotation file, consuming a matching transaction per annotated row."""
    out: list[dict[str, object]] = []
    attempt_ordinal = 0
    typology: str | None = None
    description = ""
    unmatched = 0

    with PATTERNS_TXT.open(encoding="utf-8", errors="replace") as handle:
        for line in (raw.strip() for raw in handle):
            if not line:
                continue
            if line.startswith(BEGIN_PREFIX):
                attempt_ordinal += 1
                typology, _, description = line[len(BEGIN_PREFIX) :].partition(":")
                typology = typology.strip()
                description = description.strip()
                if not typology:
                    die(f"BEGIN marker without a typology: {line[:120]!r}")
                continue
            if line.startswith(END_PREFIX):
                typology = None
                description = ""
                continue
            if TS_LINE_RE.match(line) is None:
                # A marker in a shape the regexes do not know. Failing here is the
                # point: an unparsed marker silently drops a labelled attempt.
                die(f"unrecognised annotation line, refusing to guess: {line[:120]!r}")
            if typology is None:
                die(f"transaction row outside any attempt block: {line[:120]!r}")
            fields = line.split(",")
            if len(fields) != len(COLUMN_NAMES):
                die(f"annotated row has {len(fields)} fields, expected 11: {line[:120]!r}")
            key = (normalize_ts(fields[0]), *fields[1 : len(KEY_COLUMNS)])
            queue = index.get(key)
            if not queue:
                # The pattern file and the transaction file disagree. Counted, not
                # dropped silently, and the run fails if any row is unmatched.
                unmatched += 1
                continue
            ordinal = queue.popleft()
            out.append(
                {
                    "txn_ordinal": int(ordinal),
                    "attempt_id": f"ibmaml:HI-Small:{attempt_ordinal:04d}",
                    "typology": typology,
                    "attempt_description": description,
                }
            )
    if unmatched:
        die(f"{unmatched} annotated row(s) matched no transaction; the two files disagree")
    if not out:
        die("no annotated transactions were parsed; the pattern file format changed")
    return out


def main() -> int:
    if not TRANS_CSV.is_file() or not PATTERNS_TXT.is_file():
        die(f"missing input: {TRANS_CSV.name} and {PATTERNS_TXT.name} must both be on disk")
    index = transaction_index()
    rows = parse_patterns(index)
    table = pl.DataFrame(rows).sort(["txn_ordinal", "attempt_id"])
    OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    table.write_parquet(OUT_PARQUET)

    counts = table["typology"].value_counts().sort("count", descending=True)
    print(f"wrote {OUT_PARQUET.relative_to(REPO_ROOT)}: {table.height} annotated rows")
    print(f"attempt blocks: {table['attempt_id'].n_unique()}  typologies: {counts.height}")
    for name, count in zip(counts["typology"].to_list(), counts["count"].to_list(), strict=True):
        print(f"  {name:<18} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
