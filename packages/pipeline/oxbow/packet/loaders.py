"""Readers that turn landed artifacts into a :class:`~oxbow.packet.model.PacketCase`.

The packet is a *document about artifacts*, so this module is where the document
meets the disk. Four rules, all of them refusal-shaped:

1. **Nothing is fetched from a live service.** The packet must render on the demo
   laptop with the database stopped (plan §15's offline path), and a document that
   quoted a table it re-read at render time could not claim to show the state at
   decision time. Every loader here takes a path and a ``run_id``.
2. **A missing artifact fails with its name.** No loader returns an empty table for
   a file that is not there; a packet that quietly omitted its evidence section
   would look like a case with no evidence (03 §A rule 2).
3. **The pinned run is threaded through.** Each snapshot carries the ``run_id`` it
   was read for, and :class:`~oxbow.packet.model.PacketCase` refuses a snapshot from
   any other run, which is what makes "opened under run A, decided after run B"
   render run A.
4. **Digests are verified on read where the artifact supports it** —
   :func:`oxbow.graph.persist.load_graph` checks every Parquet file against its own
   manifest, so a truncated graph fails here instead of drawing a smaller network.

The case bundle is the exception to rule 1 in the good direction: it is the payload
the decision transaction wrote, so it *is* the decision-time state, which is why it
is the packet's spine and everything else is checked against its ``run_id``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import polars as pl

from oxbow.audit.chain import ChainRow
from oxbow.graph.persist import GraphArtifacts, load_graph
from oxbow.packet.errors import (
    AuditMismatchError,
    MissingArtifactError,
    SubgraphArtifactError,
)
from oxbow.packet.model import (
    ContributionRow,
    EvidenceRow,
    EvidenceSnapshot,
    ExplanationSnapshot,
    PacketCase,
    RunRecord,
    RunRegistry,
    ScorecardPointRow,
    SubgraphEdge,
    SubgraphNode,
    SubgraphView,
)
from oxbow.ports.case_sink import (
    CASE_SCHEMA_VERSION,
    CalibrationReading,
    CaseBundle,
    DecisionRecord,
    EconomicsBlock,
    MonteCarloInterval,
    ScoreBlock,
    assert_self_describing,
)
from oxbow.quant.economics import AssumptionBlock, Economics

#: Landed case bundles: ``out/case_sink/<idempotency_key>.json`` (02 §E, plan §13).
CASE_SINK_DIRNAME: Final = "case_sink"
#: The audit chain written by ``oxbow.adapters.file.audit.FileAuditSink``.
AUDIT_CHAIN_FILENAME: Final = "audit.jsonl"
#: The run ledger rewritten whole by ``oxbow.adapters.null.warehouse``; the filename
#: is that module's ``_RUNS_FILE`` and is duplicated here on purpose — the packet may
#: not import an adapter to find out where the pipeline puts its files.
RUNS_FILENAME: Final = "runs.jsonl"
#: P4b's scored rows (``config/model.yaml :: reporting.artifact_dir``).
SCORED_ROWS_FILENAME: Final = "scored_rows.parquet"
#: The one node type that may sit in a drawn path but never be traversed *through*.
NODE_TYPE_RAIL: Final = "rail"


# --------------------------------------------------------------------------
# the bundle
# --------------------------------------------------------------------------


def case_bundle_from_payload(payload: Mapping[str, Any]) -> CaseBundle:
    """Rebuild the port's bundle from a landed document.

    The inverse of :meth:`CaseBundle.to_payload`, on the packet side rather than in
    ``ports/`` because a port must stay a dependency-free declaration (import-linter
    contract 2) and a packet must be renderable from a file with no API in reach.
    It re-runs :func:`~oxbow.ports.case_sink.assert_self_describing` on the way in:
    a bundle that would not have been *emitted* may not be *printed* either.
    """
    assert_self_describing(payload)
    if str(payload.get("schema_version", "")) != CASE_SCHEMA_VERSION:
        raise AuditMismatchError(
            f"this bundle declares {payload.get('schema_version')!r}; this renderer "
            f"reads {CASE_SCHEMA_VERSION!r}. Read forward deliberately, not by accident."
        )
    decision_raw = _mapping(payload.get("decision"), "decision")
    score_raw = _mapping(payload.get("score"), "score")
    economics_raw = _mapping(payload.get("economics"), "economics")
    calibration_raw = _mapping(score_raw.get("calibration"), "score.calibration")
    monte_carlo_raw = economics_raw.get("monte_carlo")
    decided_at = _instant(payload.get("decided_at"), "decided_at")
    return CaseBundle(
        run_id=_text(payload.get("run_id"), "run_id"),
        case_id=_text(payload.get("case_id"), "case_id"),
        account_key=_text(payload.get("account_key"), "account_key"),
        decided_at=decided_at,
        decision=DecisionRecord(
            decision_seq=_int(decision_raw.get("decision_seq"), "decision.decision_seq"),
            action=_text(decision_raw.get("action"), "decision.action"),
            reason=str(decision_raw.get("reason", "")),
            actor_id=_text(decision_raw.get("actor_id"), "decision.actor_id"),
            decided_at=_instant(decision_raw.get("decided_at"), "decision.decided_at"),
            four_eyes_confirmed_by=(
                None
                if decision_raw.get("four_eyes_confirmed_by") is None
                else _text(decision_raw.get("four_eyes_confirmed_by"), "four_eyes_confirmed_by")
            ),
            reversal_of_seq=(
                None
                if decision_raw.get("reversal_of_seq") is None
                else _int(decision_raw.get("reversal_of_seq"), "reversal_of_seq")
            ),
            decided_on_superseded_run=bool(decision_raw.get("decided_on_superseded_run", False)),
        ),
        score=ScoreBlock(
            fused_score=float(str(score_raw.get("fused_score"))),
            band=_text(score_raw.get("band"), "score.band"),
            scorecard_points=_int(score_raw.get("scorecard_points"), "score.scorecard_points"),
            reason_codes=tuple(str(item) for item in _items(score_raw.get("reason_codes"))),
            calibration=CalibrationReading(
                band=_text(calibration_raw.get("band"), "calibration.band"),
                observed_rate=float(str(calibration_raw.get("observed_rate"))),
                n=_int(calibration_raw.get("n"), "calibration.n"),
            ),
            model_version=_text(score_raw.get("model_version"), "score.model_version"),
            rule_ids=tuple(str(item) for item in _items(score_raw.get("rule_ids"))),
        ),
        economics=EconomicsBlock(
            currency=_text(economics_raw.get("currency"), "economics.currency"),
            exposure_minor=_int(economics_raw.get("exposure_minor"), "economics.exposure_minor"),
            expected_value_minor=_int(
                economics_raw.get("expected_value_minor"), "economics.expected_value_minor"
            ),
            recovery_rate=float(str(economics_raw.get("recovery_rate"))),
            analyst_cost_minor=_int(
                economics_raw.get("analyst_cost_minor"), "economics.analyst_cost_minor"
            ),
            friction_cost_minor=_int(
                economics_raw.get("friction_cost_minor"), "economics.friction_cost_minor"
            ),
            assumptions=_mapping(economics_raw.get("assumptions"), "economics.assumptions"),
            monte_carlo=None
            if not isinstance(monte_carlo_raw, Mapping)
            else MonteCarloInterval(
                runs=_int(monte_carlo_raw.get("runs"), "monte_carlo.runs"),
                seed=_int(monte_carlo_raw.get("seed"), "monte_carlo.seed"),
                p05_minor=_int(monte_carlo_raw.get("p05_minor"), "monte_carlo.p05_minor"),
                p50_minor=_int(monte_carlo_raw.get("p50_minor"), "monte_carlo.p50_minor"),
                p95_minor=_int(monte_carlo_raw.get("p95_minor"), "monte_carlo.p95_minor"),
                interval=tuple(float(str(v)) for v in _items(monte_carlo_raw.get("interval"))),
            ),
        ),
        evidence_refs=tuple(str(item) for item in _items(payload.get("evidence_refs"))),
        transaction_ids=tuple(str(item) for item in _items(payload.get("transaction_ids"))),
        provenance=dict(_mapping(payload.get("provenance"), "provenance")),
        schema_version=str(payload["schema_version"]),
    )


def load_case_bundle(path: Path) -> CaseBundle:
    """Read one landed bundle document."""
    source = Path(path)
    if not source.is_file():
        raise MissingArtifactError(
            f"{source} does not exist. A packet renders a decision that was made and "
            "landed; there is nothing to render without it."
        )
    parsed = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise AuditMismatchError(f"{source} is not a JSON object")
    return case_bundle_from_payload(parsed)


def landed_bundles(directory: Path) -> tuple[tuple[Path, CaseBundle], ...]:
    """Every landed bundle in ``directory``, ordered by case then decision sequence.

    Ordering is by ``(case_id, decision_seq, run_id)``, never by mtime or directory
    order: ``oxbow packet --all`` must produce the same set of files in the same
    order on two runs, and a reversal is a *new* decision, so both appear.
    """
    root = Path(directory)
    if not root.is_dir():
        raise MissingArtifactError(
            f"{root} is not a directory. Landed case bundles are written by the case "
            "sink at decision time (`out/case_sink/`); the packet has no other source "
            "for a decision."
        )
    found: list[tuple[Path, CaseBundle]] = []
    for path in sorted(root.glob("*.json")):
        if path.name == "index.json":
            continue
        found.append((path, load_case_bundle(path)))
    return tuple(
        sorted(
            found,
            key=lambda item: (
                item[1].case_id,
                item[1].decision.decision_seq,
                item[1].run_id,
                item[0].name,
            ),
        )
    )


# --------------------------------------------------------------------------
# the audit chain
# --------------------------------------------------------------------------


def chain_row_from_record(record: Mapping[str, Any]) -> ChainRow:
    """Rebuild one stored link. Mirrors the file audit sink's record shape exactly."""
    return ChainRow(
        seq=_int(record.get("seq"), "seq"),
        occurred_at=_instant(record.get("occurred_at"), "occurred_at"),
        actor_id=_text(record.get("actor_id"), "actor_id"),
        subject=_text(record.get("subject"), "subject"),
        action=_text(record.get("action"), "action"),
        payload=dict(_mapping(record.get("payload"), "payload")),
        prev_hash=_text(record.get("prev_hash"), "prev_hash"),
        row_hash=_text(record.get("row_hash"), "row_hash"),
    )


def load_chain(path: Path) -> tuple[ChainRow, ...]:
    """The whole chain, in stored order.

    The full chain and not a per-case slice, because a hash chain only verifies as a
    chain: verifying ``[41, 42]`` in isolation would miss a deleted row 40, and a
    deleted row is plan §15's failure mode.
    """
    source = Path(path)
    if not source.is_file():
        raise MissingArtifactError(
            f"{source} does not exist. The packet verifies the audit chain on export; "
            "without a chain there is no integrity claim to print, and a document that "
            "asserts one it cannot check is worse than one that says nothing."
        )
    rows: list[ChainRow] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditMismatchError(
                f"{source}:{line_number} is not JSON. An unparseable audit line is not "
                "a skipped row; it is a broken link whose sequence cannot be named."
            ) from exc
        if not isinstance(record, dict):
            raise AuditMismatchError(f"{source}:{line_number} is not a JSON object")
        rows.append(chain_row_from_record(record))
    return tuple(rows)


def decision_chain_seq(chain: Sequence[ChainRow], bundle: CaseBundle) -> int:
    """The sequence number of the audit row covering ``bundle``'s decision.

    Found by payload content (case id and decision sequence), both of which the
    digest covers, and required to be unique: two rows covering one decision means
    one of them was written outside the decision transaction, and the packet cannot
    say which signature it is printing.
    """
    matches = [
        row.seq
        for row in chain
        if str(row.payload.get("case_id", "")) == bundle.case_id
        and row.payload.get("decision_seq") == bundle.decision.decision_seq
        and str(row.payload.get("run_id", "")) == bundle.run_id
    ]
    if not matches:
        raise AuditMismatchError(
            f"no audit row covers case {bundle.case_id} decision "
            f"{bundle.decision.decision_seq} under run {bundle.run_id}. The bundle and "
            "the chain disagree about whether this decision was ever signed."
        )
    if len(matches) > 1:
        raise AuditMismatchError(
            f"{len(matches)} audit rows (seq {matches}) cover case {bundle.case_id} "
            f"decision {bundle.decision.decision_seq}. The chain is append-only and "
            "unique per decision; a duplicate is a re-signed decision."
        )
    return matches[0]


# --------------------------------------------------------------------------
# evidence and subgraph, from one graph artifact directory
# --------------------------------------------------------------------------


def load_graph_frames(artifact_dir: Path) -> GraphArtifacts:
    """The persisted graph frames, manifest-verified.

    ``load_graph`` refuses a Parquet file whose bytes do not match its manifest,
    which is the check that matters for an exhibit: a truncated artifact would
    otherwise draw a smaller network and print it as the whole one.
    """
    directory = Path(artifact_dir)
    if not directory.is_dir():
        raise MissingArtifactError(
            f"{directory} is not a directory. The packet draws its subgraph and its "
            "evidence table from the persisted graph artifact of the pinned run; there "
            "is no second source for either."
        )
    return load_graph(directory)


def evidence_from_graph_artifact(
    artifact_dir: Path,
    *,
    run_id: str,
    account_key: str,
    transaction_ids: Sequence[str] = (),
    limit: int = 25,
) -> EvidenceSnapshot:
    """Evidence rows for one account, from the pinned run's graph artifact.

    ``transaction_ids`` is the bundle's own list — the transactions the decision was
    argued from — and when it is non-empty the packet prints exactly those rows,
    because plan §15's packet is an exhibit for *this* decision rather than a
    sample of whatever the account did. When the bundle names none, the most recent
    ``limit`` events in the pinned window are printed, and the ordering key is the
    run's documented total order ``(event_ts_utc, txn_id)`` so the table is
    identical between renders.
    """
    frames = load_graph_frames(artifact_dir)
    edges = frames.edges
    touched = edges.filter(
        (pl.col("account_from") == account_key) | (pl.col("account_to") == account_key)
    )
    if touched.height == 0:
        raise SubgraphArtifactError(
            f"{account_key} has no events in {artifact_dir}. An empty evidence table "
            "would render as 'this account moved nothing', which is a claim about the "
            "artifact's scope being printed as a finding about the account."
        )
    frame = touched
    wanted = [str(item) for item in transaction_ids]
    if wanted:
        frame = touched.filter(pl.col("txn_id").is_in(wanted))
        missing = sorted(set(wanted) - {str(item) for item in frame["txn_id"].to_list()})
        if missing:
            raise SubgraphArtifactError(
                f"the decision for {account_key} names transactions {missing}, which are "
                f"not events of this account in {artifact_dir}. The evidence the "
                "reviewer argued from must be printable; a packet that silently dropped "
                "a named transaction would change what the exhibit says."
            )
    frame = frame.sort(["event_ts_utc", "txn_id"])
    rows = _evidence_rows(frame, account_key=account_key, limit=limit)
    if wanted:
        rows = tuple(sorted(rows, key=lambda row: (row.occurred_at, row.txn_id)))
    if not rows:
        raise SubgraphArtifactError(
            f"no evidence rows for {account_key} in {artifact_dir} after filtering"
        )
    instants = [row.occurred_at for row in rows]
    return EvidenceSnapshot(
        run_id=run_id,
        account_key=account_key,
        rows=rows,
        source=f"{artifact_dir}/edges.parquet",
        window_start=min(instants),
        window_end=max(instants),
    )


def _evidence_rows(frame: pl.DataFrame, *, account_key: str, limit: int) -> tuple[EvidenceRow, ...]:
    """Frame rows as evidence, most recent first then truncated to ``limit``."""
    records = frame.to_dicts()
    ordered = sorted(
        records,
        key=lambda record: (str(record["event_ts_utc"]), str(record["txn_id"])),
        reverse=True,
    )[: max(1, limit)]
    return tuple(
        EvidenceRow(
            txn_id=str(record["txn_id"]),
            occurred_at=_instant(str(record["event_ts_utc"]), "edges.event_ts_utc"),
            txn_type=str(record["txn_type"]),
            amount_minor=int(record["amount_minor"]),
            currency=str(record["currency"]),
            account_from=str(record["account_from"]),
            account_to=str(record["account_to"]),
            subject_account=account_key,
            column_pointer=f"edges.parquet#txn_id={record['txn_id']}",
        )
        for record in ordered
    )


def subgraph_from_graph_artifact(
    artifact_dir: Path,
    *,
    run_id: str,
    account_key: str,
    hops: int,
    cap: int | None = None,
) -> SubgraphView:
    """The drawn neighbourhood, from the pinned run's graph artifact.

    A packet-side re-implementation of the *read* half of
    :func:`oxbow.graph.subgraph.neighbourhood`, because the explorer's function
    needs a built :class:`~oxbow.graph.model.AccountGraph` and the packet must work
    off a persisted artifact while the pipeline is asleep. The two rules that matter
    are kept identical: rails are endpoints, never traversed *through*, and the
    traversal order is sorted at every step so the same artifact yields the same
    node set twice.
    """
    frames = load_graph_frames(artifact_dir)
    nodes, edges = frames.nodes, frames.edges
    node_index = {str(record["account"]): record for record in nodes.to_dicts()}
    if account_key not in node_index:
        raise SubgraphArtifactError(
            f"{account_key} is not a node in {artifact_dir}. Drawing an empty network "
            "for an account the pinned graph never saw would turn the artifact's scope "
            "into a finding about the account."
        )
    adjacency: dict[str, set[str]] = {}
    for record in edges.to_dicts():
        source, target = str(record["account_from"]), str(record["account_to"])
        if source == target:
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)

    depths: dict[str, int] = {account_key: 0}
    frontier = [account_key]
    for hop in range(max(hops, 0)):
        nxt: list[str] = []
        for node in sorted(frontier):
            if node != account_key and _is_rail(node_index.get(node)):
                continue
            for partner in sorted(adjacency.get(node, set())):
                if partner not in depths:
                    depths[partner] = hop + 1
                    nxt.append(partner)
        frontier = nxt

    members = sorted(depths)
    limit = len(members) if cap is None else max(1, cap)
    truncated = len(members) > limit
    kept = members[:limit] if truncated else members
    kept_set = set(kept)
    drawn_edges: list[SubgraphEdge] = []
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for record in edges.to_dicts():
        source, target = str(record["account_from"]), str(record["account_to"])
        if source not in kept_set or target not in kept_set:
            continue
        grouped.setdefault((source, target, str(record["currency"])), []).append(record)
    for (source, target, currency), records in sorted(grouped.items()):
        drawn_edges.append(
            SubgraphEdge(
                account_from=source,
                account_to=target,
                currency=currency,
                edge_count=len(records),
                total_value_minor=sum(int(item["amount_minor"]) for item in records),
                first_ts_us=min(int(item["ts_us"]) for item in records),
                last_ts_us=max(int(item["ts_us"]) for item in records),
            )
        )
    drawn_nodes = tuple(
        SubgraphNode(
            node_id=account,
            hop=int(depths[account]),
            node_type=str(node_index[account]["node_type"]),
            degree=int(node_index[account]["total_degree"]),
            member_count=None,
        )
        for account in kept
        if account in node_index
    )
    return SubgraphView(
        run_id=run_id,
        seed=account_key,
        hops=hops,
        nodes=drawn_nodes,
        edges=tuple(sorted(drawn_edges, key=lambda e: (e.account_from, e.account_to, e.currency))),
        source=f"{artifact_dir}/nodes.parquet + edges.parquet",
        truncated=truncated,
        truncation_reason=(
            f"{len(members)} accounts reachable in {hops} hops, cap {limit}: "
            f"{len(members) - limit} not drawn (deterministic account-key order)"
            if truncated
            else None
        ),
        reachable_before_cap=len(members),
    )


def _is_rail(record: Mapping[str, Any] | None) -> bool:
    return bool(record) and (
        bool(record.get("is_rail")) or str(record.get("node_type")) == NODE_TYPE_RAIL
    )


# --------------------------------------------------------------------------
# explanation
# --------------------------------------------------------------------------

_EXPLANATION_KEYS: Final = ("explanation", "shap")


def explanation_for_bundle(
    bundle: CaseBundle, *, scored_rows: Path | None = None
) -> ExplanationSnapshot:
    """The explanation the decision was argued from, or the labelled fallback.

    Preference order is a provenance order, not a convenience one: the copy embedded
    in the decision bundle by the API is the one the reviewer saw and the digest
    covers, so it wins. ``out/p4/scored_rows.parquet`` for the pinned run is the
    fallback, and it is only consulted for the same ``run_id``. If neither exists the
    loader fails naming both, because "SHAP: none" in a signed exhibit reads as "there
    was no evidence", which is a lie about a detection system (plan §10).
    """
    embedded = _embedded_explanation(bundle)
    if embedded is not None:
        return embedded
    path = Path(scored_rows) if scored_rows is not None else None
    if path is None or not path.is_file():
        raise MissingArtifactError(
            f"no SHAP contributions for {bundle.account_key} in run {bundle.run_id}: "
            f"the decision bundle carries no embedded explanation and "
            f"{path or 'out/p4/scored_rows.parquet'} is not on disk. Plan §10 persists "
            "TreeExplainer values per scored row so this section is a read, never a "
            "recompute; with nothing to read the packet refuses."
        )
    return explanation_from_scored_rows(path, run_id=bundle.run_id, account_key=bundle.account_key)


def explanation_from_scored_rows(
    path: Path, *, run_id: str, account_key: str, max_rows: int = 12
) -> ExplanationSnapshot:
    """One scored row's persisted explanation."""
    source = Path(path)
    if not source.is_file():
        raise MissingArtifactError(f"{source} does not exist; no explanation can be read.")
    frame = pl.read_parquet(source)
    missing = [
        column
        for column in ("run_id", "account_key", "shap_json", "explanation_source")
        if column not in frame.columns
    ]
    if missing:
        raise MissingArtifactError(
            f"{source} has no {missing} column(s); it is not a scored-row artifact of "
            "the shape P4b writes. The packet will not guess which column held the "
            "contributions."
        )
    rows = frame.filter(
        (pl.col("run_id") == run_id) & (pl.col("account_key") == account_key)
    ).to_dicts()
    if not rows:
        raise MissingArtifactError(
            f"{source} holds no scored row for {account_key} under run {run_id}. The "
            "decision was scored somewhere; printing another run's SHAP values would "
            "attribute one model's reasoning to another's decision."
        )
    if len(rows) > 1:
        raise AuditMismatchError(
            f"{source} holds {len(rows)} rows for {account_key} under run {run_id}; "
            "one scored row per account per run is the join contract."
        )
    record = rows[0]
    contributions = json.loads(str(record["shap_json"]))
    source_name = str(record["explanation_source"])
    pairs: list[ContributionRow] = []
    for item in contributions:
        pairs.append(
            ContributionRow(
                feature=str(item["feature"]),
                contribution=float(item["value"]),
                unit=(
                    "logit-space SHAP value"
                    if source_name == "shap-tree-explainer"
                    else "scorecard points"
                ),
            )
        )
    pairs = sorted(pairs, key=lambda row: (-abs(row.contribution), row.feature))[:max_rows]
    base_column = _first_present(record, ("shap_base_value", "expected_value"))
    return ExplanationSnapshot(
        run_id=run_id,
        account_key=account_key,
        source=source_name,
        base_value=float(str(record[base_column])) if base_column else 0.0,
        base_unit=("expected model output (logit)" if base_column else "base value not recorded"),
        rows=tuple(pairs),
        source_pointer=f"{source}#run_id={run_id},account_key={account_key}",
        note=(
            "contributions ordered by absolute magnitude, then feature name; the "
            f"top {len(pairs)} of {len(contributions)} are shown"
        ),
        fallback_reason=(
            None
            if source_name == "shap-tree-explainer"
            else str(record.get("explanation_fallback_reason") or "tree explanation unavailable")
        ),
    )


def _embedded_explanation(bundle: CaseBundle) -> ExplanationSnapshot | None:
    """A decision-time explanation carried in the bundle's provenance, if present."""
    raw: Any = None
    for key in _EXPLANATION_KEYS:
        candidate = bundle.provenance.get(key)
        if isinstance(candidate, Mapping):
            raw = candidate
            break
    if raw is None or not isinstance(raw.get("rows"), list | tuple):
        return None
    source_name = str(raw.get("source", ""))
    unit = str(raw.get("unit", "contribution"))
    return ExplanationSnapshot(
        run_id=bundle.run_id,
        account_key=bundle.account_key,
        source=source_name,
        base_value=float(str(raw.get("base_value", 0.0))),
        base_unit=str(raw.get("base_unit", "expected model output (logit)")),
        rows=tuple(
            sorted(
                (
                    ContributionRow(
                        feature=str(item["feature"]),
                        contribution=float(str(item["value"])),
                        unit=unit,
                        display_value=(
                            None
                            if item.get("display_value") is None
                            else str(item["display_value"])
                        ),
                    )
                    for item in raw["rows"]
                ),
                key=lambda row: (-abs(row.contribution), row.feature),
            )
        ),
        source_pointer=f"decision bundle provenance.{_EXPLANATION_KEYS[0]} (case {bundle.case_id})",
        note=str(raw.get("note", "carried in the decision bundle at decision time")),
        fallback_reason=None if raw.get("fallback_reason") is None else str(raw["fallback_reason"]),
    )


def _first_present(record: Mapping[str, Any], names: Iterable[str]) -> str | None:
    for name in names:
        if name in record:
            return name
    return None


# --------------------------------------------------------------------------
# scorecard points
# --------------------------------------------------------------------------


def scorecard_points_for_bundle(
    bundle: CaseBundle, *, warehouse_dir: Path | None = None
) -> tuple[ScorecardPointRow, ...]:
    """The per-attribute point table for the pinned run, if the artifact exists.

    The reason-code *sentences* always come from the bundle and always print; this is
    the finer table beneath them, which only exists once P4b lands its warehouse
    handoff. Absent, the renderer prints a named gap rather than an empty table, and
    the gap names the path a reader can go and produce.
    """
    directory = Path(warehouse_dir) if warehouse_dir is not None else None
    if directory is None or not directory.is_dir():
        return ()
    candidates = sorted(directory.glob(f"run={bundle.run_id}*"))
    records: list[Mapping[str, Any]] = []
    for path in candidates:
        if path.suffix == ".jsonl" and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    if isinstance(record, dict):
                        records.append(record)
    rows = [
        ScorecardPointRow(
            feature=str(record.get("feature", "")),
            bin_display=str(record.get("bin_display", record.get("bin_label", ""))),
            points=int(str(record.get("points", 0))),
            text=str(record.get("text", "")),
        )
        for record in records
        if str(record.get("account_key", "")) == bundle.account_key
    ]
    return tuple(
        sorted(
            rows,
            key=lambda row: (row.points, row.feature, row.bin_display),
        )
    )


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


def load_run_registry(warehouse_dir: Path, *, pinned_run_id: str) -> RunRegistry:
    """The run ledger, so the cover can say which run is current.

    Reads ``out/warehouse/runs.jsonl`` — the file the pipeline rewrites as run state
    changes. A missing ledger fails: a cover page that claimed "run 01…" with nothing
    to corroborate the run's existence would be a label, not a provenance line.
    """
    source = Path(warehouse_dir) / RUNS_FILENAME
    if not source.is_file():
        raise MissingArtifactError(
            f"{source} does not exist, so this packet cannot state which run is "
            "current. The superseded-run stamp comes from the audit row; the context "
            "line under it comes from the run ledger, and both are printed together."
        )
    records: list[RunRecord] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise AuditMismatchError(f"{source} holds a non-object line")
        records.append(
            RunRecord(
                run_id=_text(payload.get("run_id"), "run.run_id"),
                state=_text(payload.get("state"), "run.state"),
                seed=None if payload.get("seed") is None else _int(payload.get("seed"), "run.seed"),
                started_at=_optional_instant(payload.get("created_at")),
                finished_at=_optional_instant(payload.get("finished_at")),
            )
        )
    if not records:
        raise MissingArtifactError(f"{source} is empty: no run has been opened.")
    ordered = sorted(records, key=lambda record: record.run_id)
    return RunRegistry(
        pinned_run_id=pinned_run_id,
        head_run_id=max(
            (record for record in ordered if record.state == "complete"),
            key=lambda record: record.run_id,
            default=ordered[-1],
        ).run_id,
        runs=tuple(ordered),
    )


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def build_packet_case(
    bundle_path: Path,
    *,
    graph_artifact_dir: Path,
    audit_chain_path: Path,
    warehouse_dir: Path,
    economics: Economics,
    assumption_block: AssumptionBlock,
    deployment_timezone: str,
    scored_rows: Path | None = None,
    hops: int = 2,
    evidence_limit: int = 25,
) -> PacketCase:
    """Everything the renderer needs, read from disk, cross-checked, once.

    The arguments are explicit rather than defaulted to repo paths so a test can
    point the whole reader at a fixture directory and the CLI can point it at the
    real ``out/`` tree. Cross-run mixing is impossible from here: every snapshot is
    built with ``bundle.run_id`` and validated again in
    :class:`~oxbow.packet.model.PacketCase`.
    """
    bundle = load_case_bundle(bundle_path)
    chain = load_chain(audit_chain_path)
    return PacketCase(
        bundle=bundle,
        evidence=evidence_from_graph_artifact(
            graph_artifact_dir,
            run_id=bundle.run_id,
            account_key=bundle.account_key,
            transaction_ids=bundle.transaction_ids,
            limit=evidence_limit,
        ),
        explanation=explanation_for_bundle(bundle, scored_rows=scored_rows),
        subgraph=subgraph_from_graph_artifact(
            graph_artifact_dir,
            run_id=bundle.run_id,
            account_key=bundle.account_key,
            hops=hops,
        ),
        scorecard_points=scorecard_points_for_bundle(bundle, warehouse_dir=warehouse_dir),
        chain=chain,
        decision_chain_seq=decision_chain_seq(chain, bundle),
        runs=load_run_registry(warehouse_dir, pinned_run_id=bundle.run_id),
        economics=economics,
        assumption_block=assumption_block,
        deployment_timezone=deployment_timezone,
    )


# --------------------------------------------------------------------------
# scalar coercion — every one of these names the field it failed on
# --------------------------------------------------------------------------


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuditMismatchError(f"{label} should be an object, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _items(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise AuditMismatchError(f"expected a list, got {value!r}")
    return tuple(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuditMismatchError(f"{label} must be a non-empty string, got {value!r}")
    return value


def _int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise AuditMismatchError(f"{label} must be an integer, got a bool: {value!r}")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise AuditMismatchError(f"{label} is {value!r}, which is not an integer") from exc


def _optional_instant(value: Any) -> datetime | None:
    return None if value in (None, "") else _instant(value, "run timestamp")


def _instant(value: Any, label: str) -> datetime:
    """Parse a recorded instant. Naive stamps are refused, never assumed to be UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise AuditMismatchError(
                f"{label} is a naive datetime ({value}). Assuming a zone here would put "
                "an unlogged guess on a signed document."
            )
        return value.astimezone(UTC)
    text = str(value).strip()
    if not text:
        raise AuditMismatchError(f"{label} is empty")
    normalised = text
    if normalised.endswith("Z"):
        normalised = normalised[:-1] + "+00:00"
    if " " in normalised and "T" not in normalised:
        normalised = normalised.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise AuditMismatchError(f"{label} is {value!r}, not an ISO-8601 instant") from exc
    if parsed.tzinfo is None:
        raise AuditMismatchError(
            f"{label} {value!r} has no UTC offset. The audit chain, the case payload and "
            "this packet all require an explicit offset."
        )
    return parsed.astimezone(UTC)


__all__ = [
    "AUDIT_CHAIN_FILENAME",
    "CASE_SINK_DIRNAME",
    "RUNS_FILENAME",
    "SCORED_ROWS_FILENAME",
    "build_packet_case",
    "case_bundle_from_payload",
    "chain_row_from_record",
    "decision_chain_seq",
    "evidence_from_graph_artifact",
    "explanation_for_bundle",
    "explanation_from_scored_rows",
    "landed_bundles",
    "load_case_bundle",
    "load_chain",
    "load_graph_frames",
    "load_run_registry",
    "scorecard_points_for_bundle",
    "subgraph_from_graph_artifact",
]
