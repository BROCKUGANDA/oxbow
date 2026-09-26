"""Do time-respecting, value-retaining cycles actually exist in IBM-AML?

DEV-013 records the first of plan §4's two go/no-go conditions as measured:
median account degree 10.0 against a `> 2` threshold — pass. The second condition was
left as `PENDING the P3a cycle enumerator` because a degree distribution cannot
prove a loop closes. This script closes that gap, and it does so with the graph
layer's own enumerator rather than a reimplementation, so the number means something
about *our* R4 substrate and not about a convenient ad-hoc walk.

The check that makes this more than a count: `HI-Small_Patterns.txt` plants 287
CYCLE-typology rows independently of anything we compute (DEV-014). A cycle detector
that finds loops which never touch a planted attempt would be detecting something
else; overlap with the planted set is the external validation, and it is reported
separately from the raw count.

Two rules from the money contract shape the method, not just the code:
* **Per-currency builds.** The corpus carries 15 currencies and the graph layer
  refuses a mixed build. A loop that converts is an FX trade, so cycles are counted
  within each currency and the totals are reported per currency — never blended,
  which would be the implicit-FX error 01 §B forbids.
* **Self-loops excluded.** 11.6 % of this corpus is same-account movement; those are
  not hops of a round-trip. The graph layer already excludes them, and the count of
  rows dropped for it is printed so the exclusion is visible rather than assumed.

    uv run python scripts/measure_ibm_cycles.py

Writes `data/ibm_cycle_measurement.json`. Read-only over the raw corpus; it writes
nothing outside `data/`.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
from dataclasses import fields
from pathlib import Path
from typing import Final

import duckdb
import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "pipeline"))

from oxbow.config import load_pipeline_config  # noqa: E402
from oxbow.graph import AccountGraph, CycleSearch, build_graph  # noqa: E402
from oxbow.ingest.canonical import RunIdentity, account_key  # noqa: E402

TRANS_CSV: Final = REPO_ROOT / "data" / "raw" / "ibmaml" / "HI-Small_Trans.csv"
PATTERNS_TXT: Final = REPO_ROOT / "data" / "raw" / "ibmaml" / "HI-Small_Patterns.txt"
TYPOLOGIES: Final = REPO_ROOT / "data" / "processed" / "ibm_typologies.parquet"
OUT_JSON: Final = REPO_ROOT / "data" / "ibm_cycle_measurement.json"

# An annotation-file row is recognised by its timestamp PREFIX: the lines are whole
# CSV records, so an anchored `^...$` timestamp pattern would match nothing.
TS_LINE_RE: Final = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2},")

# Currency names in the file, mapped to ISO-4217 like the canonical contract wants.
CURRENCY_ISO: Final[dict[str, str]] = {
    "US Dollar": "USD",
    "Euro": "EUR",
    "Swiss Franc": "CHF",
    "Yuan": "CNY",
    "Shekel": "ILS",
    "Mexican Peso": "MXN",
    "Indian Rupee": "INR",
    "Canadian Dollar": "CAD",
    "Congo Franc": "CDF",
    "Malaysian": "MYR",
    "Brazilian Real": "BRL",
    "British Pound": "GBP",
    "Romanian Leu": "RON",
    "Turkish Lira": "TRY",
    "Vietnamese Dong": "VND",
    "Australian Dollar": "AUD",
    "Brazil Real": "BRL",
    "Ruble": "RUB",
    "Rupee": "INR",
    "Saudi Riyal": "SAR",
    "UK Pound": "GBP",
    "Yen": "JPY",
    "Bitcoin": "BTC",
}

# Deterministic connected-subcorpus selection (config.sampling.strategy). An account
# is in when the first hex nibble of sha256(salt|account) is below this cut, and an
# edge survives only when both endpoints are in — so the sample keeps loops whole
# instead of severing them, which row sampling would do.
ACCOUNT_INCLUSION_NIBBLES: Final = 3
MAX_EVENTS_PER_CURRENCY: Final = 120_000


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_rows() -> list[dict[str, object]]:
    """Read the raw corpus as text and normalise it into canonical-shaped dicts.

    Deliberately independent of the ingest adapter: this measurement is about the
    corpus, and binding it to an adapter under revision would make the number
    inherit the adapter's bugs. Money is converted from the decimal *string*, never
    through a float, so the minor units stay exact.
    """
    names = [
        "ts", "from_bank", "from_account", "to_bank", "to_account",
        "amount_received", "receiving_ccy", "amount_paid", "payment_ccy",
        "payment_format", "is_laundering",
    ]
    quoted = ", ".join(f"'{n}'" for n in names)
    types = ", ".join(["'VARCHAR'"] * len(names))
    con = duckdb.connect()
    sql = (
        "SELECT ts, from_account, to_account, amount_received, receiving_ccy, "
        "payment_format, is_laundering FROM read_csv('"
        + TRANS_CSV.as_posix()
        + "', header=true, names=["
        + quoted
        + "], types=["
        + types
        + "], ignore_errors=true, all_varchar=true) WHERE regexp_matches(ts, '"
        + r"^\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}$"
        + "')"
    )
    rows = con.execute(sql).fetchall()
    if not rows:
        die("zero transaction rows read; the corpus or its timestamp format changed")
    return [
        {
            "ts": str(r[0]).replace("/", "-"),
            "from_account": str(r[1]),
            "to_account": str(r[2]),
            "amount_received": str(r[3]),
            "ccy": str(r[4]),
            "payment_format": str(r[5]),
            "is_laundering": str(r[6]),
        }
        for r in rows
    ]


def minor_units(text: str) -> int:
    """Exact decimal string -> integer minor units, no float anywhere."""
    from decimal import ROUND_HALF_EVEN, Decimal

    return int((Decimal(text.strip()) * 100).to_integral_value(rounding=ROUND_HALF_EVEN))


def salt() -> str:
    """Reuse the run salt if the environment has one; otherwise a measurement salt.

    The salt is not committed either way. Determinism of *this* artifact needs a
    stable salt, so the fallback is a fixed literal; a real pipeline run supplies
    RUN_SALT from the environment instead (01 §A rule 8).
    """
    import os

    return os.environ.get("RUN_SALT", "oxbow-ibm-cycle-measurement-v1")


def selected(account: str, identity: RunIdentity) -> bool:
    """Whether an account is inside the deterministic connected subcorpus."""
    digest = hashlib.sha256(f"{identity.run_salt}|{account}".encode()).hexdigest()
    return int(digest[0], 16) < ACCOUNT_INCLUSION_NIBBLES


def build_events(
    rows: list[dict[str, object]],
    currency: str,
    identity: RunIdentity,
    *,
    whitelist: frozenset[str] | None = None,
) -> tuple[pl.DataFrame, int, int]:
    """Canonical-shaped events for one currency.

    With `whitelist`, selection is **component-intact**: a row survives only when both
    endpoints are inside the named set, so a loop inside the set keeps all its edges.
    Without it, the deterministic hash-of-account sampler is used, which is fine for
    degree and volume statistics and wrong for cycle counts, because each node is
    dropped independently and a four-node loop survives at roughly the cut to the
    fourth power. Both arms are reported side by side for exactly that reason.
    """
    in_ccy = [r for r in rows if CURRENCY_ISO.get(str(r["ccy"])) == currency]
    if whitelist is not None:
        sampled = [
            r
            for r in in_ccy
            if str(r["from_account"]) in whitelist and str(r["to_account"]) in whitelist
        ]
    else:
        accounts = {str(r["from_account"]) for r in in_ccy} | {
            str(r["to_account"]) for r in in_ccy
        }
        keep = {a for a in accounts if selected(a, identity)}
        sampled = [
            r
            for r in in_ccy
            if str(r["from_account"]) in keep and str(r["to_account"]) in keep
        ]
    # Stable, bounded, and time-ordered first: the cap must not depend on file order.
    sampled.sort(key=lambda r: (str(r["ts"]), str(r["from_account"]), str(r["to_account"])))
    truncated_by_cap = max(0, len(sampled) - MAX_EVENTS_PER_CURRENCY)
    sampled = sampled[:MAX_EVENTS_PER_CURRENCY]
    self_loops = sum(1 for r in sampled if r["from_account"] == r["to_account"])

    events: list[dict[str, object]] = []
    for position, row in enumerate(sampled):
        raw_id = f"{row['ts']}|{row['from_account']}|{row['to_account']}|{row['amount_received']}"
        txn_key = hashlib.sha256(raw_id.encode()).hexdigest()[:12]
        stamp = datetime.datetime.strptime(str(row["ts"]), "%Y-%m-%d %H:%M").replace(
            tzinfo=datetime.UTC
        )
        # The ordinal is part of the id deliberately. IBM's timestamps are
        # minute-precision and the corpus repeats (sender, receiver, amount) within a
        # minute, so a purely content-derived id collides for rows that are genuinely
        # different transactions -- and `require_events` rightly refuses a duplicate
        # txn_id, because the total order (event_ts_utc, txn_id) would be ambiguous.
        # This bundle carries no source transaction id to fall back on, so ordinal is
        # the only stable disambiguator; see DEV-015 for what the adapter must do.
        events.append(
            {
                "txn_id": f"ibmaml:{position:08d}:{txn_key}",
                "event_ts_utc": stamp,
                "event_date_local": stamp.date(),
                "local_hour": stamp.hour,
                "txn_type": str(row["payment_format"]),
                "channel": "bank",
                "amount_minor": minor_units(str(row["amount_received"])),
                "currency": currency,
                "account_from": account_key(str(row["from_account"]), identity),
                "account_to": account_key(str(row["to_account"]), identity),
                "src_balance_before_minor": None,
                "src_balance_after_minor": None,
                "dst_balance_before_minor": None,
                "dst_balance_after_minor": None,
                "label_is_fraud": int(str(row["is_laundering"]) or 0),
                "label_is_flagged": 0,
                "label_typology": None,
                "source_dataset": "ibmaml",
                "batch_id": identity.batch_id,
            }
        )
    frame = pl.DataFrame(events)
    return frame, self_loops, truncated_by_cap


def cycle_search_of(graph: AccountGraph) -> CycleSearch | None:
    """Find the CycleSearch member without hard-coding the attribute name."""
    for field in fields(graph):
        value = getattr(graph, field.name, None)
        if isinstance(value, CycleSearch):
            return value
    return None


def planted_cycle_accounts(identity: RunIdentity) -> tuple[set[str], set[str], dict[str, object]]:
    """Accounts inside the corpus's own labelled CYCLE attempts (DEV-014).

    Returns (keyed accounts, raw account names, diagnostics). The raw names are needed
    to select *whole* loops out of the CSV: filtering by a hash of each account
    independently severs a cycle's edges from each other, and a cycle count taken from
    such a sample measures the sampler, not the corpus.

    The typology artifact stores a *row ordinal*, so joining it back to the raw CSV
    depends on both sides enumerating rows the same way. That is exactly the kind of
    alignment that goes quietly wrong by one row and then reports a plausible-looking
    answer, so the join is checked rather than trusted: every transaction in a planted
    laundering attempt carries `Is Laundering = 1` in the source, so the share of
    joined rows with that flag is an independent alignment test.
    """
    if not TYPOLOGIES.is_file():
        return set(), set(), {"error": "typology artifact missing"}
    typ = pl.read_parquet(TYPOLOGIES)
    cycle = typ.filter(pl.col("typology") == "CYCLE")
    if cycle.is_empty():
        return set(), set(), {"error": "no CYCLE rows in the typology artifact"}

    names = [
        "ts", "from_bank", "from_account", "to_bank", "to_account",
        "amount_received", "receiving_ccy", "amount_paid", "payment_ccy",
        "payment_format", "is_laundering",
    ]
    quoted = ", ".join(f"'{n}'" for n in names)
    types = ", ".join(["'VARCHAR'"] * len(names))
    con = duckdb.connect()
    con.register(
        "cycle_ordinals",
        cycle.select(["txn_ordinal", "attempt_id"]).to_pandas(),
    )
    rows = con.execute(
        "SELECT a.from_account, a.to_account, a.is_laundering FROM ("
        "  SELECT ROW_NUMBER() OVER () - 1 AS ord, from_account, to_account, is_laundering"
        "  FROM read_csv('"
        + TRANS_CSV.as_posix()
        + "', header=true, names=["
        + quoted
        + "], types=["
        + types
        + "], ignore_errors=true, all_varchar=true)"
        "  WHERE regexp_matches(ts, '"
        + r"^\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}$"
        + "')) a JOIN cycle_ordinals c ON a.ord = c.txn_ordinal"
    ).fetchall()
    if not rows:
        return set(), set(), {"error": "ordinal join matched zero rows"}
    flagged = sum(1 for r in rows if str(r[2]) == "1")
    raw = {str(r[0]) for r in rows} | {str(r[1]) for r in rows}
    accounts = {account_key(name, identity) for name in raw}
    return accounts, raw, {
        "matched_rows": len(rows),
        "rows_with_laundering_flag": flagged,
        "alignment_share": round(flagged / len(rows), 4),
        "alignment_plausible": flagged / len(rows) > 0.95,
        "distinct_planted_accounts": len(raw),
    }


def labelled_cycle_anatomy() -> dict[str, object]:
    """Describe the corpus's own CYCLE annotations, independently of our detector.

    This is the diagnostic that turned "we found zero cycles" into a statement about
    the *definition* rather than a bug report: parse the planted CYCLE blocks and
    measure how many close, how many cross currencies, how many breach the retention
    floor and the non-increasing rule. Without it, a zero would have been read as
    "this corpus has no cycles", which is false — it has 54 labelled ones.
    """
    begin_prefix = "BEGIN LAUNDERING ATTEMPT - "
    blocks: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    with PATTERNS_TXT.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if line.startswith(begin_prefix):
                kind, _, desc = line[len(begin_prefix) :].partition(":")
                current = {"type": kind.strip(), "description": desc.strip(), "rows": []}
                blocks.append(current)
                continue
            if line.startswith("END LAUNDERING ATTEMPT - "):
                current = None
                continue
            if current is not None and TS_LINE_RE.match(line):
                fields = line.split(",")
                if len(fields) == 11:
                    current["rows"].append(fields)
    cycles = [b for b in blocks if b["type"] == "CYCLE"]
    closes = cross_ccy = low_retention = non_monotonic = two_leg = 0
    retentions: list[float] = []
    for block in cycles:
        rows = block["rows"]  # type: ignore[index]
        if len(rows) < 2:
            continue
        srcs = [r[2] for r in rows]  # type: ignore[index]
        dsts = [r[4] for r in rows]  # type: ignore[index]
        closes += int(dsts[-1] == srcs[0])
        cross_ccy += int(len({r[6] for r in rows}) > 1)  # type: ignore[index]
        two_leg += int(len(rows) == 2)
        amounts = [float(r[5]) for r in rows]  # type: ignore[index]
        if max(amounts) > 0:
            ratio = min(amounts) / max(amounts)
            retentions.append(round(ratio, 4))
            low_retention += int(ratio < 0.6)
        stamps = [str(r[0]) for r in rows]  # type: ignore[index]
        non_monotonic += int(any(stamps[i] > stamps[i + 1] for i in range(len(stamps) - 1)))
    return {
        "labelled_cycle_blocks": len(cycles),
        "blocks_that_close": closes,
        "blocks_cross_currency": cross_ccy,
        "blocks_below_0_6_retention": low_retention,
        "blocks_with_non_monotonic_timestamps": non_monotonic,
        "blocks_with_only_two_legs": two_leg,
        "retention_ratios": sorted(retentions)[:12],
        "reading": (
            "Most of the corpus's own cycles are cross-currency and breach the "
            "retention floor, so a single-currency, non-increasing, >=0.6-retention "
            "definition cannot fire on them. See DEV-015."
        ),
    }


def main() -> int:
    cfg = load_pipeline_config(REPO_ROOT)
    identity = RunIdentity(run_salt=salt(), batch_id=hashlib.sha256(b"ibm-cycles").hexdigest()[:12])
    rows = load_rows()
    planted, planted_raw, planted_diag = planted_cycle_accounts(identity)
    print(f"planted CYCLE accounts: {len(planted_raw):,}  alignment: {planted_diag}")

    # One-hop closure: the planted accounts plus every counterparty they have traded
    # with. Without the closure the arm tests only loops whose every node happens to be
    # annotated, which understates what the enumerator can find.
    closure: set[str] = set(planted_raw)
    for row in rows:
        if str(row["from_account"]) in planted_raw:
            closure.add(str(row["to_account"]))
        if str(row["to_account"]) in planted_raw:
            closure.add(str(row["from_account"]))
    print(f"one-hop closure around planted cycles: {len(closure):,} accounts")

    counts: dict[str, int] = {}
    for row in rows:
        iso = CURRENCY_ISO.get(str(row["ccy"]))
        if iso:
            counts[iso] = counts.get(iso, 0) + 1
    largest = sorted(counts, key=lambda c: -counts[c])

    arms = {
        "planted_component_intact": frozenset(closure),
        "hash_sample_loop_severing": None,
    }
    per_currency: dict[str, dict[str, object]] = {}
    for arm, whitelist in arms.items():
        for currency in largest[:3]:
            events, self_loops, capped = build_events(
                rows, currency, identity, whitelist=whitelist
            )
            key = f"{arm}/{currency}"
            if events.is_empty():
                per_currency[key] = {"error": "no events after sampling"}
                continue
            graph = build_graph(events, cfg)
            search = cycle_search_of(graph)
            if search is None:
                die(f"build_graph returned no CycleSearch for {key}; inspect AccountGraph")
            nodes = {n for cycle in search.cycles for n in cycle.path}
            per_currency[key] = {
                "events": events.height,
                "self_loop_rows": self_loops,
                "dropped_by_cap": capped,
                "cycles_found": len(search.cycles),
                "truncated": search.truncated,
                "truncation_reason": search.truncation_reason,
                "visits": search.visits,
                "components_searched": search.components_searched,
                "zero_value_loops_rejected": search.zero_value_loops_rejected,
                "rails_excluded": search.rails_excluded,
                "distinct_accounts_in_cycles": len(nodes),
                "overlap_with_planted_cycle_accounts": len(nodes & planted),
                "cycle_lengths": sorted({len(cycle.path) for cycle in search.cycles}),
                "sample_retentions": [
                    round(cycle.value_retention, 4) for cycle in search.cycles[:10]
                ],
            }
            print(
                f"  {key:<38} {len(search.cycles):>6,} cycles over {events.height:,} events"
                f"  (truncated={search.truncated}, overlap={per_currency[key]['overlap_with_planted_cycle_accounts']})"
            )

    out = {
        "command": "uv run python scripts/measure_ibm_cycles.py",
        "measured_at_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "method": "oxbow.graph.build_graph on a deterministic connected subcorpus, per currency",
        "account_inclusion_nibbles": ACCOUNT_INCLUSION_NIBBLES,
        "max_events_per_currency": MAX_EVENTS_PER_CURRENCY,
        "planted_cycle_accounts": len(planted),
        "planted_join_alignment": planted_diag,
        "per_currency": per_currency,
        "total_cycles_found": sum(
            int(v.get("cycles_found", 0)) for v in per_currency.values() if isinstance(v, dict)
        ),
        "total_planted_overlap": sum(
            int(v.get("overlap_with_planted_cycle_accounts", 0))
            for v in per_currency.values()
            if isinstance(v, dict)
        ),
    }
    out["pass_condition_non_trivial_cycle_count"] = out["total_cycles_found"] > 0
    out["caveat"] = (
        "The hash_sample arm selects accounts independently, so it severs the loops it "
        "is asked to count and its cycle total is an artefact of the sampler, not a "
        "property of the corpus. Only the planted_component_intact arm answers plan "
        "S4's second question. A population-wide cycle rate needs a component-aware "
        "connected subcorpus from the ingest layer (config.sampling)."
    )
    out["planted_join_alignment"] = planted_diag
    out["labelled_cycle_anatomy"] = labelled_cycle_anatomy()
    print(f"labelled cycle anatomy: {json.dumps(out['labelled_cycle_anatomy'])[:400]}")
    OUT_JSON.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"\ntotal cycles: {out['total_cycles_found']:,}  |  planted-attempt overlap: "
        f"{out['total_planted_overlap']:,}  |  -> {OUT_JSON.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
