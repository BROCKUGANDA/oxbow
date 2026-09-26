"""The §4 measurement: does PaySim actually contain a network?

This is the single highest-leverage number in the build. 01 P1, 03 §F and 00 §I.3
all ask it independently, and 00 §E day-4 makes the answer a go/no-go on the
entire network thesis:

    if originators are near-unique, the PaySim graph is star-shaped, there is
    no network, and all 12 rules, all graph features and the whole
    fraud-detection pillar have no substrate on the primary corpus.

The verdict is decided by two pre-committed conditions:

    median account degree > 2   AND   a non-trivial count of surviving
                                          time-respecting 3-to-6 cycles

It runs on the FULL corpus for the degree statistics, and on a 20k-row sample
for the cycle count, because enumerating cycles over 6.3M edges is not a thing
one does in a notebook cell. Both facts are printed and written to the dataset
card, so the decision is auditable rather than remembered.

Run:  uv run python scripts/measure_graph.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW = REPO_ROOT / "data" / "raw" / "paysim" / "PS_20174392719_1491204439457_log.csv"
OUT_JSON = REPO_ROOT / "data" / "graph_measurement.json"

# 00 E day-4 pass condition, pre-committed before the number was known.
MEDIAN_DEGREE_THRESHOLD = 2.0
CYCLE_SAMPLE_ROWS = 20_000
CYCLE_MIN_LENGTH = 3
CYCLE_MAX_LENGTH = 6
# "Non-trivial" has to mean something. Below this many surviving cycles in a
# 20k sample, the network thesis has no substrate and the fallback fires.
CYCLE_MIN_COUNT = 10

REQUIRED_COLUMNS = ("step", "nameOrig", "nameDest", "amount", "type")


def die(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def degree_stats(series: pl.Series) -> dict[str, Any]:
    """Median / p90 / p99 / max of an account's degree.

    Degree here is total incident edges, counting an account once as sender and
    once as receiver. The §4 condition is stated on that total.

    Polars types median() as Optional, so each value is narrowed explicitly: a
    silent None would become a nan in the verdict arithmetic and quietly decide
    the architecture. A missing statistic is a bug, not a zero.
    """
    if series.len() == 0:
        return {"accounts": 0, "median": 0.0, "p90": 0.0, "p99": 0.0, "max": 0}

    def required(value: Any, label: str) -> float:
        # Polars' stubs widen these to Any (int | float | Decimal | date | ...).
        # Every one of them except None is a real number here, and None is the
        # only value that would poison the verdict, so it is checked explicitly
        # rather than cast blindly.
        if value is None:
            die(f"{label} is None over {series.len():,} rows; refusing to report a verdict")
        return float(value)

    median = required(series.median(), "median degree")
    p90 = required(series.quantile(0.90), "p90 degree")
    p99 = required(series.quantile(0.99), "p99 degree")
    peak = required(series.max(), "max degree")
    return {
        "accounts": series.len(),
        "median": round(median, 4),
        "p90": round(p90, 4),
        "p99": round(p99, 4),
        "max": int(peak),
    }


def top_accounts(df: pl.DataFrame, column: str, n: int = 20) -> list[dict[str, Any]]:
    counts = (
        df.group_by(column)
        .len()
        .sort("len", descending=True)
        .head(n)
        .rename({column: "account", "len": "edges"})
    )
    return counts.to_dicts()


def count_cycles(df: pl.DataFrame) -> dict[str, Any]:
    """Count time-respecting directed cycles of length 3..6 in a sample.

    "Time-respecting" is the whole point. A cycle in a fraud graph is only
    evidence if the money moved in a feasible order, so every edge must be
    timestamped no earlier than the one before it. Counting order-free cycles
    would be the same bug as training on shuffled rows, and would produce a
    comfortable number that means nothing.
    """
    # Edge list with a timestamp, and value retention for the 0.6 threshold.
    edges = df.select(
        pl.col("nameOrig").cast(pl.Utf8).alias("src"),
        pl.col("nameDest").cast(pl.Utf8).alias("dst"),
        pl.col("step").cast(pl.Int64).alias("t"),
        pl.col("amount").cast(pl.Float64).alias("amt"),
    )

    # index = edge id; for each node, its outgoing edges sorted by time
    n_edges = edges.height
    src_list = edges["src"].to_list()
    dst_list = edges["dst"].to_list()
    t_list = edges["t"].to_list()
    amt_list = edges["amt"].to_list()

    out_edges: dict[str, list[int]] = {}
    node_amount: dict[str, list[float]] = {}
    for idx, (s, _d, a) in enumerate(zip(src_list, dst_list, amt_list, strict=True)):
        out_edges.setdefault(s, []).append(idx)
        node_amount.setdefault(s, []).append(a)
    for lst in out_edges.values():
        lst.sort(key=lambda i: t_list[i])

    found: set[tuple[int, ...]] = set()
    budget_exhausted = False

    def walk(start: str, node: str, path: list[int], visited: set[str], retain: float) -> None:
        nonlocal budget_exhausted
        if budget_exhausted or len(found) > 5000:
            budget_exhausted = True
            return
        for edge_idx in out_edges.get(node, ()):
            nxt = dst_list[edge_idx]
            if edge_idx in path:
                continue
            # Time-respecting: the closing edge must be no earlier than the
            # last edge in the path.
            if t_list[edge_idx] < t_list[path[-1]]:
                continue
            if nxt == start and len(path) >= CYCLE_MIN_LENGTH - 1:
                new_path = [*path, edge_idx]
                # Value retention: the cycle's minimum leg must carry at least
                # 60% of the maximum leg, the standard structuring tell.
                legs = [amt_list[i] for i in new_path]
                if max(legs) > 0 and (min(legs) / max(legs)) >= 0.6:
                    canonical = min(tuple(sorted(new_path)), tuple(reversed(new_path)))
                    found.add(canonical)
                continue
            if nxt in visited or len(path) >= CYCLE_MAX_LENGTH:
                continue
            walk(start, nxt, [*path, edge_idx], {*visited, nxt}, retain)

    for node, edges_out in out_edges.items():
        for first in edges_out:
            walk(node, dst_list[first], [first], {node, dst_list[first]}, 1.0)
            if budget_exhausted:
                break
        if budget_exhausted:
            break

    return {
        "sample_rows": df.height,
        "distinct_edges": n_edges,
        "cycles_found": len(found),
        "budget_exhausted": budget_exhausted,
        "min_len": CYCLE_MIN_LENGTH,
        "max_len": CYCLE_MAX_LENGTH,
        "value_retention_min": 0.6,
    }


def main() -> int:
    if not RAW.is_file():
        die(
            f"{RAW} not found. Run: uv run python scripts/download_data.py --source paysim"
        )

    print(f"reading {RAW.name} ({RAW.stat().st_size:,} bytes) ...", flush=True)
    # Read only what the measurement needs: the full corpus is 6.3M rows and
    # every extra column is memory this does not need.
    df = pl.read_csv(RAW, columns=list(REQUIRED_COLUMNS), infer_schema_length=10_000)
    n_rows = df.height
    print(f"  rows {n_rows:,}  columns {df.columns}", flush=True)

    src = df["nameOrig"].cast(pl.Utf8)
    dst = df["nameDest"].cast(pl.Utf8)
    n_src = src.n_unique()
    n_dst = dst.n_unique()
    reuse = 1.0 - (n_src / n_rows)

    print("\n--- reuse (full corpus) ---", flush=True)
    print(f"  n_rows                {n_rows:,}")
    print(f"  n_distinct_nameOrig   {n_src:,}")
    print(f"  n_distinct_nameDest   {n_dst:,}")
    print(f"  reuse ratio           {reuse:.6f}")

    # Degree = total incident edges per account. pl.concat of two Series yields a
    # Series, which has no group_by; the frame is what carries the operation.
    all_nodes = pl.DataFrame(
        {"acct": pl.concat([src, dst], how="vertical")}
    )
    deg = all_nodes.group_by("acct").len()["len"]
    deg_stats = degree_stats(deg)

    # Counterparty reuse: distinct partners per account, then the median across
    # accounts. This is the number the §4 condition is actually written on,
    # and it is stricter than raw degree.
    pairs = pl.DataFrame(
        {
            "acct": pl.concat([src, dst], how="vertical"),
            "cp": pl.concat([dst, src], how="vertical"),
        }
    )
    cp_per_acct = pairs.group_by("acct").agg(pl.col("cp").n_unique().alias("n"))["n"]
    cp_stats = degree_stats(cp_per_acct)

    print("\n--- degree distribution (total incident edges) ---", flush=True)
    for key, value in deg_stats.items():
        print(f"  {key:20} {value}")
    print("\n--- counterparty reuse (distinct partners per account) ---", flush=True)
    for key, value in cp_stats.items():
        print(f"  {key:20} {value}")

    top20 = top_accounts(df, "nameOrig")
    print("\n--- top 20 senders by edge count (00 E day-3 gate) ---", flush=True)
    for row in top20:
        print(f"  {row['account']:12} {row['edges']:>9,}")

    print("\n--- cycles on a 20k sample ---", flush=True)
    sample = df.head(CYCLE_SAMPLE_ROWS)
    cycles = count_cycles(sample)
    for key, value in cycles.items():
        print(f"  {key:20} {value}")

    median_degree = cp_stats["median"]
    verdict = (
        "NETWORK_THESIS_HOLDS"
        if (median_degree > MEDIAN_DEGREE_THRESHOLD and cycles["cycles_found"] >= CYCLE_MIN_COUNT)
        else "STAR_SHAPED_TRIGGER_DAY4_FALLBACK"
    )
    print(f"\n=== VERDICT: {verdict} ===")
    print(f"  median counterparty degree {median_degree} (needs > {MEDIAN_DEGREE_THRESHOLD})")
    print(f"  surviving cycles {cycles['cycles_found']} (needs >= {CYCLE_MIN_COUNT})")
    if verdict == "STAR_SHAPED_TRIGGER_DAY4_FALLBACK":
        print(
            "  ACTION: promote IBM-AML to primary for Module B, keep PaySIM as the\n"
            "  volume/topology corpus for the tabular module, and rewrite spec 3.1\n"
            "  framing rather than the code. Log DEV-011 with both numbers."
        )

    result = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus": "paysim",
        "file": RAW.name,
        "n_rows": n_rows,
        "n_distinct_nameOrig": n_src,
        "n_distinct_nameDest": n_dst,
        "reuse_ratio": round(reuse, 6),
        "degree_total": deg_stats,
        "counterparty_degree": cp_stats,
        "top20_senders": top20,
        "cycles": cycles,
        "thresholds": {
            "median_counterparty_degree_gt": MEDIAN_DEGREE_THRESHOLD,
            "cycles_min_count": CYCLE_MIN_COUNT,
        },
        "verdict": verdict,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_JSON.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
