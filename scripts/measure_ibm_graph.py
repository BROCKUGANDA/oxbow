"""Measure the network thesis on the IBM-AML corpus.

Plan §4 makes one number the gate for the entire network pillar: **is the median
account degree greater than 2?** PaySim failed it (median 1.0, zero surviving
cycles — DEV-011), which promoted IBM-AML to primary for Module B. This script is
the same question asked of the corpus that answer depends on, so the fallback is
backed by a measurement rather than by an assumption that the second corpus is
better.

Run: `uv run python scripts/measure_ibm_graph.py` → `data/ibm_graph_measurement.json`

Three hazards in these bytes are handled here and recorded in DEV-013, because each
one silently corrupts the answer if ignored:
* the header repeats the column name `Account` for sender and receiver, so the file
  is read positionally;
* `HI-Small_Trans.csv` timestamps use `/` separators while the pattern file uses
  `-`, so any text join across the two matches nothing;
* 591,212 rows are self-loops (sender account == receiver account, mostly
  `Reinvestment`). They are counted separately and excluded from the edge and
  counterparty numbers, because a self-loop gives an account degree without giving
  it a counterparty.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Final

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANS_CSV: Final = REPO_ROOT / "data" / "raw" / "ibmaml" / "HI-Small_Trans.csv"
OUT_JSON: Final = REPO_ROOT / "data" / "ibm_graph_measurement.json"

# Positional names: the real header is
# `Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,
#  Amount Paid,Payment Currency,Payment Format,Is Laundering`
# and two of those columns are both called `Account`.
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

TS_PATTERN: Final = r"^\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}$"

# The degree and counterparty queries both need "every endpoint with its
# multiplicity", and the self-loop exclusion belongs in the one place that defines
# it. This is a WITH prefix: each caller adds its own final SELECT.
ENDPOINT_CTE: Final = """
WITH d AS (
    SELECT acct, sum(n) AS degree
    FROM (
        SELECT from_account AS acct, count(*) AS n FROM t
          WHERE from_account <> to_account GROUP BY 1
        UNION ALL
        SELECT to_account AS acct, count(*) AS n FROM t
          WHERE from_account <> to_account GROUP BY 1
    )
    GROUP BY acct
)
"""


def source_table(con: duckdb.DuckDBPyConnection) -> int:
    """Load the CSV as all-text and materialise the transaction rows.

    Everything is read as VARCHAR on purpose: a money column inferred as float would
    violate the int64-minor-units rule at the reader (01 §B), and this script only
    ever counts and groups, so a float here would be a defect with no upside.
    """
    names = ", ".join(f"'{n}'" for n in COLUMN_NAMES)
    types = ", ".join(["'VARCHAR'"] * len(COLUMN_NAMES))
    # Concatenated rather than formatted: TS_PATTERN contains brace quantifiers that
    # an f-string would read as replacement fields.
    con.execute(
        "CREATE TABLE t AS SELECT regexp_replace(ts, '/', '-', 'g') AS ts, * "
        "EXCLUDE (ts) FROM read_csv('"
        + TRANS_CSV.as_posix()
        + "', header=true, names=["
        + names
        + "], types=["
        + types
        + "], ignore_errors=true, all_varchar=true) WHERE regexp_matches(ts, '"
        + TS_PATTERN
        + "')"
    )
    return int(con.sql("SELECT count(*) FROM t").fetchone()[0])


def measure(con: duckdb.DuckDBPyConnection) -> dict[str, object]:
    """Run every thesis question and return the answers as plain values."""

    def one(sql: str) -> object:
        return con.sql(sql).fetchone()[0]

    def pairs(sql: str) -> list[list[object]]:
        return [list(row) for row in con.sql(sql).fetchall()]

    median_degree = one(f"{ENDPOINT_CTE} SELECT median(degree) FROM d")
    return {
        "source": "ibmaml HI-Small bundle (data/raw/ibmaml/HI-Small_Trans.csv)",
        "command": "uv run python scripts/measure_ibm_graph.py",
        "measured_at_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "n_transaction_rows": one("SELECT count(*) FROM t"),
        "n_self_loop_rows": one("SELECT count(*) FROM t WHERE from_account = to_account"),
        "n_distinct_accounts": one(
            "SELECT count(*) FROM (SELECT from_account a FROM t UNION SELECT to_account FROM t)"
        ),
        "n_directed_edges_excl_self_loops": one(
            "SELECT count(*) FROM (SELECT DISTINCT from_account, to_account FROM t"
            " WHERE from_account <> to_account)"
        ),
        "degree_median": median_degree,
        "degree_p90": one(f"{ENDPOINT_CTE} SELECT quantile_cont(degree, 0.9) FROM d"),
        "degree_p99": one(f"{ENDPOINT_CTE} SELECT quantile_cont(degree, 0.99) FROM d"),
        "degree_max": one(f"{ENDPOINT_CTE} SELECT max(degree) FROM d"),
        "top20_degrees": [
            row[0]
            for row in con.sql(
                f"{ENDPOINT_CTE} SELECT degree FROM d" " ORDER BY degree DESC LIMIT 20"
            ).fetchall()
        ],
        "median_counterparties_per_account": one(
            "WITH c AS (SELECT a acct, count(DISTINCT b) k FROM ("
            "SELECT from_account a, to_account b FROM t WHERE from_account<>to_account"
            " UNION ALL SELECT to_account a, from_account b FROM t"
            " WHERE from_account<>to_account) GROUP BY 1) SELECT median(k) FROM c"
        ),
        "accounts_with_more_than_2_counterparties": one(
            "WITH c AS (SELECT a acct, count(DISTINCT b) k FROM ("
            "SELECT from_account a, to_account b FROM t WHERE from_account<>to_account"
            " UNION ALL SELECT to_account a, from_account b FROM t"
            " WHERE from_account<>to_account) GROUP BY 1) SELECT count(*) FROM c WHERE k > 2"
        ),
        "laundering_rows": one(
            "SELECT count(*) FROM t WHERE TRY_CAST(is_laundering AS TINYINT) = 1"
        ),
        "laundering_rate_pct": one(
            "SELECT round(100 * avg(TRY_CAST(is_laundering AS TINYINT)), 4) FROM t"
        ),
        "n_currencies": one("SELECT count(DISTINCT receiving_ccy) FROM t"),
        "currencies": pairs("SELECT receiving_ccy, count(*) FROM t GROUP BY 1 ORDER BY 2 DESC"),
        "payment_formats": pairs(
            "SELECT payment_format, count(*) FROM t GROUP BY 1 ORDER BY 2 DESC"
        ),
        "temporal_range": pairs("SELECT min(ts), max(ts) FROM t")[0],
        "pass_condition_median_degree_gt_2": bool(median_degree and float(median_degree) > 2),
        "typology_ground_truth": (
            "data/processed/ibm_typologies.parquet — 370 labelled attempt blocks "
            "(scripts/build_ibm_typologies.py)"
        ),
        "cycle_count_time_respecting_3_6": "PENDING the P3a cycle enumerator",
        "notes": [
            "Timestamps in this file use '/' separators; the pattern file uses '-'.",
            "The header repeats the column name 'Account' for sender and receiver.",
            "Self-loops are excluded from degree, edge and counterparty figures.",
        ],
    }


def main() -> int:
    if not TRANS_CSV.is_file():
        print(f"ERROR: {TRANS_CSV} is not on disk", file=sys.stderr)
        return 1
    con = duckdb.connect()
    rows = source_table(con)
    if rows == 0:
        print("ERROR: zero transaction rows parsed; the timestamp pattern changed", file=sys.stderr)
        return 1
    result = measure(con)
    OUT_JSON.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"measured {rows:,} transaction rows -> {OUT_JSON.relative_to(REPO_ROOT)}")
    for key in (
        "n_distinct_accounts",
        "n_directed_edges_excl_self_loops",
        "degree_median",
        "median_counterparties_per_account",
        "laundering_rate_pct",
        "pass_condition_median_degree_gt_2",
    ):
        print(f"  {key}: {result[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
