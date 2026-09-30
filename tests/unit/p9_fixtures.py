"""Shared P9 fixtures: a real graph artifact, a real chain, a real decision bundle.

Everything here is built with the producing layer's own code — the graph artifact is
``build_graph`` + ``write_graph``, the chain is ``append_row`` + ``FileAuditSink``, the
bundle is ``CaseBundle.to_payload`` — because a packet test that hand-wrote the
artifact formats would be testing the packet against its own invention. The
*content* of the fixture is authored by hand (six accounts, nine events, hand-computed
totals), so every expected number in ``test_p9_packet.py`` is arithmetic a human did,
not output the code produced (00 §B).

Determinism: run ids are ULIDs built from an explicit timestamp and fixed entropy,
instants are literal, and nothing reads the wall clock. Two renders of these fixtures
must be byte-identical, and that assertion is worthless if the fixture moved underneath
it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import polars as pl

from oxbow.adapters.file.audit import FileAuditSink
from oxbow.audit.chain import ChainRow, append_row
from oxbow.config import load_pipeline_config
from oxbow.graph import build_graph
from oxbow.graph.persist import write_graph
from oxbow.identity import new_ulid
from oxbow.ports.case_sink import (
    CalibrationReading,
    CaseBundle,
    DecisionRecord,
    EconomicsBlock,
    MonteCarloInterval,
    ScoreBlock,
)
from oxbow.quant.economics import AssumptionBlock, Economics, assumption_block

BASE: Final = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)

#: Deterministic run ids: fixed instant, fixed entropy, so ordering is stable and
#: ``RUN_B > RUN_A`` lexicographically the way a real ULID pair would be.
RUN_A: Final = new_ulid(int(BASE.timestamp() * 1000), entropy=b"\x01" * 10)
RUN_B: Final = new_ulid(int((BASE + timedelta(days=1)).timestamp() * 1000), entropy=b"\x02" * 10)

#: Six accounts, hand-chosen so the neighbourhood arithmetic is checkable by eye.
#: 12-character keys, because that is what ingest emits
#: (``config/sources.yaml :: deidentification.account_key.prefix_length``).
SUBJECT: Final = "a5b1e6000000"
BETA: Final = "b07a00000000"
GAMMA: Final = "c04500000000"
DELTA: Final = "d1e7a0000000"
RAIL: Final = "fa1100000000"
OUTSIDER: Final = "0f51d3000000"

CASE_ID: Final = "CASE-A5B1E6000000"
ACTOR: Final = "analyst-1@oxbow.dev"
SECOND_ACTOR: Final = "reviewer-2@oxbow.dev"

REASON: Final = (
    "Four transfers out of the subject in 40 minutes, each under the 5,000,000 review "
    "floor, converging on two accounts that immediately forward to a common beneficiary. "
    "Escalated for KYC refresh; no funds moved on my authority."
)

EVENT_SCHEMA: Final = {
    "txn_id": pl.Utf8,
    "event_ts_utc": pl.Datetime("us", "UTC"),
    "txn_type": pl.Utf8,
    "amount_minor": pl.Int64,
    "currency": pl.Utf8,
    "account_from": pl.Utf8,
    "account_to": pl.Utf8,
}


def event(
    txn_id: str,
    source: str,
    target: str,
    amount_minor: int,
    minutes: int,
    *,
    currency: str = "UGX",
    txn_type: str = "TRANSFER",
) -> dict[str, Any]:
    """One canonical event at ``BASE + minutes``."""
    return {
        "txn_id": txn_id,
        "event_ts_utc": BASE + timedelta(minutes=minutes),
        "txn_type": txn_type,
        "amount_minor": amount_minor,
        "currency": currency,
        "account_from": source,
        "account_to": target,
    }


#: Nine events. The subject sends four (a structuring-shaped burst), receives two, and
#: one leg is deliberately EUR so the packet's per-currency discipline has something to
#: refuse to add. Rail traffic is the bulk of the degree, which is what makes the rail
#: typing measurable rather than asserted.
EVENTS: Final[tuple[dict[str, Any], ...]] = (
    event("paysim:t-1001", SUBJECT, BETA, 1_250_000_00, 0),
    event("paysim:t-1002", SUBJECT, BETA, 1_100_000_00, 6),
    event("paysim:t-1003", SUBJECT, GAMMA, 900_000_00, 12),
    event("paysim:t-1004", SUBJECT, GAMMA, 4_800_000_00, 18),
    event("ibmaml:t-1005", BETA, DELTA, 2_000_000_00, 25, currency="EUR"),
    event("paysim:t-1006", GAMMA, SUBJECT, 300_000_00, 31),
    event("paysim:t-1007", SUBJECT, SUBJECT, 50_000_00, 33, txn_type="REVERSAL"),
    event("paysim:t-1008", DELTA, OUTSIDER, 1_800_000_00, 40),
    event("paysim:t-1009", RAIL, SUBJECT, 250_000_00, 44, txn_type="PAYMENT"),
)

RAIL_FAN_OUT: Final = 12


def rail_events() -> list[dict[str, Any]]:
    """Bulk merchant traffic, so the rail typing has a real high-degree node."""
    return [
        event(
            f"paysim:r-{index:04d}",
            RAIL,
            f"c0ffe{index:07d}",
            10_000_00 + index,
            index,
            txn_type="PAYMENT",
        )
        for index in range(RAIL_FAN_OUT)
    ]


def events_frame() -> pl.DataFrame:
    return pl.DataFrame([*EVENTS, *rail_events()], schema=EVENT_SCHEMA)  # type: ignore[arg-type]


def write_graph_artifact(directory: Path) -> Path:
    """Build and persist the graph with the graph layer's own writers."""
    config = load_pipeline_config()
    graph = build_graph(events_frame(), config)
    return write_graph(graph, directory)


def make_bundle(
    *,
    run_id: str = RUN_A,
    case_id: str = CASE_ID,
    account_key: str = SUBJECT,
    decision_seq: int = 1,
    reason: str = REASON,
    action: str = "escalate",
    decided_on_superseded_run: bool = False,
    decided_at: datetime = BASE + timedelta(minutes=90),
    block: AssumptionBlock,
    economics: Economics,
    transaction_ids: tuple[str, ...] = (),
    reversal_of_seq: int | None = None,
    monte_carlo: bool = True,
    explanation: dict[str, object] | None = None,
) -> CaseBundle:
    """A decision bundle as the API would have landed it.

    Every money figure here is hand-computed from ``config/economics.yaml`` so the
    packet test can assert the printed string against arithmetic a person did:

    * exposure ``E_i`` = 400,000,000 minor = **4,000,000.00 UGX** (observed);
    * band E ⇒ ``review_minutes_by_alert_class.E`` = 120 min at
      ``analyst.cost_per_minute_minor`` = 15,000 minor ⇒ ``c_i`` = 1,800,000 minor
      = **18,000.00 UGX**;
    * ``EV_i = p·E·r - c_i - (1-p)·f`` with p = 0.9, r = 0.35,
      f = 2,500,000 minor = 25,000.00 UGX ⇒
      1,260,000 - 18,000 - 2,500 = **1,239,500.00 UGX** = 123,950,000 minor;
    * ``E_i * r`` = 140,000,000 minor = **1,400,000.00 UGX**.

    The packet prints these as recorded and does not recompute them — which is why
    they have to be consistent with the config by hand, not by construction.
    """
    interval = (
        None
        if not monte_carlo
        else MonteCarloInterval(
            runs=economics.monte_carlo.runs,
            seed=economics.monte_carlo.seed,
            p05_minor=190_000_000,
            p50_minor=310_000_000,
            p95_minor=440_000_000,
            interval=(economics.monte_carlo.lower_quantile, economics.monte_carlo.upper_quantile),
        )
    )
    provenance: dict[str, object] = {
        "config_hash": "sha256:2f3c1e",
        "dataset_ref": "data/interim/paysim/run=" + run_id,
        "walk_forward_fold": 3,
    }
    if explanation is not None:
        provenance["explanation"] = explanation
    return CaseBundle(
        run_id=run_id,
        case_id=case_id,
        account_key=account_key,
        decided_at=decided_at,
        decision=DecisionRecord(
            decision_seq=decision_seq,
            action=action,
            reason=reason,
            actor_id=ACTOR if decision_seq == 1 else SECOND_ACTOR,
            decided_at=decided_at,
            four_eyes_confirmed_by=None if decision_seq == 1 else SECOND_ACTOR,
            reversal_of_seq=reversal_of_seq,
            decided_on_superseded_run=decided_on_superseded_run,
        ),
        score=ScoreBlock(
            fused_score=0.901234,
            band="E",
            scorecard_points=-48,
            reason_codes=[
                "Pass-through ratio in the top decile: minus 48 points",
                "Counterparty overlap with a flagged account in the same hour: minus 21 points",
            ],
            calibration=CalibrationReading(
                kind="calibrated_band", band="E", observed_rate=0.412, n=1_204
            ),
            model_version="lgbm-4.5.0+scorecard-woe-2024-03-01",
            rule_ids=("R4_CYCLE_MEMBER", "R2_FAN_OUT"),
        ),
        economics=EconomicsBlock(
            currency=economics.currency,
            exposure_minor=400_000_000,
            expected_value_minor=123_950_000,
            recovery_rate=economics.recovery.rate,
            analyst_cost_minor=1_800_000,
            friction_cost_minor=economics.friction_cost.minor,
            assumptions={
                "recovery_rate": economics.recovery.rate,
                "sensitivity_band": list(economics.recovery.band),
                "block": block.text,
            },
            monte_carlo=interval,
        ),
        evidence_refs=[f"evidence:{item['txn_id']}" for item in EVENTS],
        transaction_ids=transaction_ids
        or tuple(
            str(item["txn_id"])
            for item in EVENTS
            if account_key in (item["account_from"], item["account_to"])
        ),
        provenance=provenance,
    )


def decision_payload(bundle: CaseBundle) -> dict[str, object]:
    """What ``apps/api/decisions.py`` covers with the digest, in the same shape."""
    return {
        "run_id": bundle.run_id,
        "case_id": bundle.case_id,
        "account_key": bundle.account_key,
        "decision_seq": bundle.decision.decision_seq,
        "action": bundle.decision.action,
        "reason": bundle.decision.reason,
        "actor_id": bundle.decision.actor_id,
        "exposure_minor": bundle.economics.exposure_minor,
        "currency": bundle.economics.currency,
        "expected_value_minor": bundle.economics.expected_value_minor,
        "decided_on_superseded_run": bundle.decision.decided_on_superseded_run,
        "reversal_of_seq": bundle.decision.reversal_of_seq,
        "model_version": bundle.score.model_version,
        "band": bundle.score.band,
        "fused_score": bundle.score.fused_score,
        "scorecard_points": bundle.score.scorecard_points,
        "evidence_refs": list(bundle.evidence_refs),
        "txn_ids": list(bundle.transaction_ids),
        "trace_id": "trace-fixture-1",
    }


def write_runs_ledger(directory: Path, *, head: str = RUN_B, complete_head: bool = True) -> Path:
    """``out/warehouse/runs.jsonl``-shaped ledger, written by hand for the fixture."""
    directory.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "run_id": RUN_A,
            "state": "superseded" if head != RUN_A else "complete",
            "seed": 1337,
            "created_at": BASE.isoformat().replace("+00:00", "Z"),
            "finished_at": (BASE + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "model_version": "lgbm-4.5.0+scorecard-woe-2024-03-01",
        },
        {
            "run_id": head,
            "state": "complete" if complete_head else "running",
            "seed": 1337,
            "created_at": (BASE + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "finished_at": (BASE + timedelta(days=1, hours=2)).isoformat().replace("+00:00", "Z"),
            "model_version": "lgbm-4.5.0+scorecard-woe-2024-03-02",
        },
    ]
    path = directory / "runs.jsonl"
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def write_chain(
    audit_dir: Path,
    bundles: list[CaseBundle],
    *,
    start: datetime = BASE + timedelta(minutes=90),
    tamper: tuple[int, str, Any] | None = None,
) -> tuple[ChainRow, ...]:
    """Append one decision row per bundle through the real file audit sink.

    ``tamper=(seq, payload_key, value)`` rewrites a stored line *after* it was
    written, which is exactly the edit the chain exists to catch: the digest no
    longer matches the bytes, and verification has to name that sequence number.
    """
    sink = FileAuditSink(audit_dir)
    prev: ChainRow | None = None
    rows: list[ChainRow] = []
    for index, bundle in enumerate(bundles):
        pending = append_row(
            prev=prev,
            occurred_at=start + timedelta(minutes=index),
            actor_id=bundle.decision.actor_id,
            subject=f"case:{bundle.case_id}",
            action=f"decision:{bundle.decision.action}",
            payload=decision_payload(bundle),
        )
        prev = sink.append(pending)
        rows.append(prev)
    if tamper is not None:
        seq, key, value = tamper
        path = sink.path
        lines = path.read_text(encoding="utf-8").splitlines()
        for position, line in enumerate(lines):
            record = json.loads(line)
            if int(record["seq"]) == seq:
                record["payload"][key] = value
                lines[position] = json.dumps(record, sort_keys=True, ensure_ascii=False)
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return tuple(sink.load())


def write_scored_rows(path: Path, *, run_id: str, account_key: str) -> Path:
    """One ``out/p4/scored_rows.parquet``-shaped row, with persisted SHAP."""
    contributions = [
        {"feature": "pass_through_ratio", "value": 0.8125},
        {"feature": "counterparty_flagged_overlap_1h", "value": 0.4375},
        {"feature": "outflow_degree_24h", "value": -0.1875},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "run_id": [run_id],
            "account_key": [account_key],
            "shap_json": [json.dumps(contributions, separators=(",", ":"))],
            "explanation_source": ["shap-tree-explainer"],
            "shap_base_value": [-2.5],
        }
    ).write_parquet(path, compression="zstd", row_group_size=100_000)
    return path


def packet_fixture(
    root: Path,
    *,
    economics: Economics,
    block: AssumptionBlock,
    bundles: list[CaseBundle] | None = None,
    tamper: tuple[int, str, Any] | None = None,
    with_scored_rows: bool = True,
) -> dict[str, Any]:
    """Land a whole packet workspace under ``root`` and return every path plus bundles.

    ``bundles`` defaults to the two-decision case (escalate, then a reversal that
    references it), which is what lets the timeline test see append-only behaviour
    rather than assert it.
    """
    case_sink = root / "case_sink"
    case_sink.mkdir(parents=True, exist_ok=True)
    graph_dir = write_graph_artifact(root / "graph")
    chosen = bundles if bundles is not None else _default_bundles(economics=economics, block=block)
    for bundle in chosen:
        payload = bundle.to_payload()
        (case_sink / f"{bundle.idempotency_key}.json").write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
    chain = write_chain(root / "audit", chosen, tamper=tamper)
    write_runs_ledger(root / "warehouse")
    scored = (
        write_scored_rows(
            root / "p4" / "scored_rows.parquet", run_id=chosen[0].run_id, account_key=SUBJECT
        )
        if with_scored_rows
        else None
    )
    return {
        "root": root,
        "case_sink": case_sink,
        "graph_dir": graph_dir,
        "audit_path": root / "audit" / "audit.jsonl",
        "warehouse": root / "warehouse",
        "scored_rows": scored,
        "bundles": chosen,
        "chain": chain,
    }


def _default_bundles(*, economics: Economics, block: AssumptionBlock) -> list[CaseBundle]:
    first = make_bundle(economics=economics, block=block)
    second = make_bundle(
        decision_seq=2,
        action="reverse",
        reason="Withdrawn: the beneficiary was a registered agent with a documented offset.",
        reversal_of_seq=1,
        decided_at=BASE + timedelta(minutes=91),
        economics=economics,
        block=block,
    )
    return [first, second]


def assumption_fixture(root: Path | None = None) -> tuple[Economics, AssumptionBlock]:
    """The committed economics file, loaded through its own validated reader."""
    economics = load_economics_for(root)
    return economics, assumption_block(economics)


def load_economics_for(root: Path | None) -> Economics:
    from oxbow.quant.economics import load_economics

    return load_economics(root)


__all__ = [
    "ACTOR",
    "BASE",
    "BETA",
    "CASE_ID",
    "DELTA",
    "EVENTS",
    "GAMMA",
    "OUTSIDER",
    "RAIL",
    "RAIL_FAN_OUT",
    "REASON",
    "RUN_A",
    "RUN_B",
    "SECOND_ACTOR",
    "SUBJECT",
    "assumption_fixture",
    "decision_payload",
    "event",
    "events_frame",
    "make_bundle",
    "packet_fixture",
    "rail_events",
    "write_chain",
    "write_graph_artifact",
    "write_runs_ledger",
    "write_scored_rows",
]
