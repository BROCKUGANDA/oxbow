"""The case workspace: one account, one pinned run, everything the pane renders.

The case response is the object an analyst signs, so the structural rule here is that
**every figure names the table it came from**. The score is the ``score`` row; the
points are ``scorecard_point``; the money is ``economics``, whose ``assumptions`` column
is the run's own copy of the config rather than today's; the interval is the stored
Monte Carlo draw with its ``mc_runs`` and ``mc_seed``. Nothing is composed from two
tables into a third number, and nothing is recomputed on the way out (02 §B seam 5) —
the failure that prevents is the API and the pipeline giving an account different
numbers on the same day.

Three fields are here for a specific plan §15/§14 requirement:

* ``pinned_run_id`` + ``superseded`` — a case decided under run A and reopened after
  run B still shows run A's evidence, and the fact that B exists is visible;
* ``decision_history`` — append-only, oldest first, with each row's ``prev_hash`` and
  ``row_hash`` so the chain chips on the rail are the stored digests, not a rendering
  of an assumption;
* ``counterfactual`` — computed, and computed *only* from stored integer points and
  stored band boundaries. It is arithmetic over the scorecard the pipeline already
  fitted: the cheapest single attribute change that would move this account across a
  band line. It is not a second model, and its note says which of the two it is.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, Path, Query

from api.deps import Container, analyst_or_higher, get_container
from api.problems import (
    COMMON_ERROR_STATUSES,
    CaseNotFound,
    DependencyUnavailable,
    problem_responses,
)
from api.readmodel import ReadModel, money
from api.routers.common import (
    Pagination,
    assumption_lines,
    build_meta,
    build_page_meta,
    page_params,
)
from api.schemas.case import (
    CalibrationBand,
    CaseDetail,
    DecisionRecordRow,
    EconomicsBlock,
    EvidenceRow,
    MonteCarlo,
    RuleHitRow,
    ScorecardPointRow,
    ShapRow,
    WatchlistEnrichment,
)
from api.schemas.common import Envelope, PageEnvelope, envelope
from api.security import Principal

router = APIRouter(prefix="/api/cases", tags=["cases"])

CASE_SORTABLE: Final = frozenset({"event_ts_utc", "amount_minor", "txn_id"})
TRANSACTION_CAP: Final = 200
EVIDENCE_CAP: Final = 500

# A counterfactual may only consider attributes whose bins the run actually stored.
# Anything else would compare the account against a scorecard half of which the
# pipeline never fitted, and produce a confident wrong answer.
MIN_BINS_FOR_COUNTERFACTUAL: Final = 2


@router.get(
    "/{case_id}",
    response_model=Envelope[CaseDetail],
    summary="One case: score, points, reasons, SHAP, rules, evidence, decisions",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def case_detail(
    case_id: str = Path(min_length=26, max_length=26),
    transaction_limit: int = Query(default=TRANSACTION_CAP, ge=1, le=TRANSACTION_CAP),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    case = _case_row(container, case_id)
    run_id = str(case["run_id"])
    account_key = str(case["account_key"])
    run = read_model.run_row(run_id)
    score = read_model.score_row(run_id, account_key)
    source = read_model.source
    # The exponent, not the base: config declares minor_units_per_major (100) and
    # both this server and apps/web/src/lib/format/money.ts raise ten to whatever
    # arrives in a `decimals` field. Inherited from the read model, which converts
    # once and refuses a base that is not an exact power of ten.
    decimals = container.read_model.money_decimals

    economic_rows, _ = source.select(
        "economics", where={"run_id": run_id, "account_key": account_key}, limit=1
    )
    if not economic_rows:
        raise DependencyUnavailable(
            f"case {case_id} is pinned to an account with no stored economics row, so the "
            "money block, the four-eyes threshold and the ranking all have nothing to read",
        )
    economic = economic_rows[0]

    points, _ = source.select(
        "scorecard_point",
        where={"run_id": run_id, "account_key": account_key},
        order="points",
        descending=False,
        allow_missing=True,
    )
    shap, _ = source.select(
        "shap_contribution",
        where={"run_id": run_id, "account_key": account_key},
        order="rank",
        allow_missing=True,
    )
    rule_hits, _ = source.select(
        "rule_hit",
        where={"run_id": run_id, "account_key": account_key},
        order="rule_id",
        allow_missing=True,
    )
    evidence, _ = source.select(
        "evidence_event",
        where={"run_id": run_id, "account_key": account_key},
        order="occurred_at",
        descending=True,
        limit=EVIDENCE_CAP,
        allow_missing=True,
    )
    transactions, txn_total = _transactions(source, run_id, account_key, transaction_limit)
    allocation_rows, _ = source.select(
        "policy_allocation",
        where={"run_id": run_id, "account_key": account_key},
        limit=1,
        allow_missing=True,
    )
    decisions = _decisions(container, case_id)
    watchlist = _watchlist_enrichment(container, score=score, account_key=account_key)

    body = CaseDetail(
        case_id=str(case["case_id"]),
        pinned_run_id=run_id,
        run_state=str(run["state"]),
        superseded=str(run["run_id"])
        != str(read_model.resolve_run(None, state="complete")["run_id"]),
        account_key=account_key,
        band=str(score["band"]),
        fused_score=float(score["fused_score"]),
        scorecard_points_total=int(score["scorecard_points"]),
        scorecard_points=[ScorecardPointRow.model_validate(row) for row in points],
        # Passed through as stored, `None` included: `kind` decides whether the three
        # measurements are all present or all absent, and `CalibrationBand` refuses a
        # reading that contradicts its own kind. The `str()` coercion the old code applied
        # to each of them was safe only while the columns were NOT NULL; `f"{None}"`
        # would have turned an uncalibrated row into the string "None" on a signed case.
        calibration=CalibrationBand(
            kind=score["calibration_kind"],
            band=score["calibration_band"],
            observed_rate=score["observed_rate"],
            n=score["calibration_n"],
            note=score["calibration_note"],
        ),
        predicted_typology=score.get("predicted_typology"),
        model_version=str(score["model_version"]),
        reason_codes=list(score.get("reason_codes") or []),
        rule_ids=list(score.get("rule_ids") or []),
        economics=_economics_block(
            economic, decimals=decimals, assumptions_config=container.economics
        ),
        evidence=[EvidenceRow.model_validate(row) for row in evidence],
        transactions=[_transaction_row(row, decimals=decimals) for row in transactions],
        transaction_total=txn_total,
        shap=[ShapRow.model_validate(row) for row in shap],
        rule_hits=[RuleHitRow.model_validate(row) for row in rule_hits],
        watchlist=watchlist,
        decision_history=[DecisionRecordRow.model_validate(row) for row in decisions],
        case_version=int(case["version"]),
        status=str(case["status"]),
        rank_under_active_policy=None if not allocation_rows else int(allocation_rows[0]["rank"]),
        counterfactual=counterfactual(
            read_model, run_id=run_id, account_key=account_key, points=points
        ),
    )
    return envelope(
        body,
        **build_meta(
            container,
            run_id=run_id,
            model_version=str(score["model_version"]),
            provenance=str(run["provenance"]),
            assumptions=[line.model_dump() for line in assumption_lines(container.economics)],
        ).model_dump(),
    )


@router.get(
    "/{case_id}/transactions",
    response_model=PageEnvelope[dict[str, Any]],
    summary="A case's transactions, paged and sorted server-side",
    description=(
        "The SHAP cross-filter sends account keys back here rather than filtering the "
        "case payload client-side: the plan's rule is that every rendered figure is a "
        "response field, and a filter the client applies to a truncated list is not one."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def case_transactions(
    page: Pagination = Depends(page_params("event_ts_utc", CASE_SORTABLE)),
    case_id: str = Path(min_length=26, max_length=26),
    txns: str | None = Query(
        default=None, description="Comma-separated txn_ids, e.g. the evidence behind one SHAP bar."
    ),
    account_side: str | None = Query(default=None, pattern="^(src|dst|any)$"),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    case = _case_row(container, case_id)
    run_id, account_key = str(case["run_id"]), str(case["account_key"])
    wanted = None if txns is None else [part.strip() for part in txns.split(",") if part.strip()]
    rows, total = _transactions(
        container.read_model.source,
        run_id,
        account_key,
        page.limit,
        offset=page.offset,
        sort=page.sort,
        order=page.order,
        txn_ids=wanted,
        account_side=account_side or "any",
    )
    # The exponent, not the base: config declares minor_units_per_major (100) and
    # both this server and apps/web/src/lib/format/money.ts raise ten to whatever
    # arrives in a `decimals` field. Inherited from the read model, which converts
    # once and refuses a base that is not an exact power of ten.
    decimals = container.read_model.money_decimals
    body = [_transaction_row(row, decimals=decimals) for row in rows]
    return envelope(
        body,
        **build_page_meta(
            container,
            page,
            total,
            run_id=run_id,
            provenance=str(container.read_model.run_row(run_id)["provenance"]),
        ).model_dump(),
    )


@router.post(
    "",
    response_model=Envelope[dict[str, Any]],
    summary="Open the case for one account in one run (idempotent)",
    description=(
        "Opening a case is not a decision and carries no reason, so it is the one write "
        "here that is deliberately not hash-chained. Opening twice returns the same case."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def open_case(
    run_id: str = Query(min_length=26, max_length=26),
    account_key: str = Query(min_length=3, max_length=12),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    from api.decisions import open_case as _open

    session = container.new_session()
    try:
        case = _open(session, container.read_model, run_id=run_id, account_key=account_key)
        session.commit()
        payload = {
            "case_id": str(case.case_id),
            "run_id": str(case.run_id),
            "account_key": str(case.account_key),
            "status": str(case.status),
            "case_version": int(case.version),
        }
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    return envelope(payload, **build_meta(container, run_id=run_id).model_dump())


def _case_row(container: Container, case_id: str) -> dict[str, Any]:
    rows, _ = container.read_model.source.select("review_case", where={"case_id": case_id}, limit=1)
    if not rows:
        raise CaseNotFound(
            f"no case {case_id!r}. Cases exist only once opened from the queue, so an id typed "
            "from a deep link may predate a warehouse reload."
        )
    return rows[0]


def _decisions(container: Container, case_id: str) -> list[dict[str, Any]]:
    rows, _ = container.read_model.source.select(
        "decision", where={"case_id": case_id}, order="decision_seq", allow_missing=True
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "decision_id": row["decision_id"],
                "decision_seq": row["decision_seq"],
                "chain_seq": row["chain_seq"],
                "action": row["action"],
                "reason": row["reason"],
                "actor_id": row["actor_id"],
                "actor_roles": row["actor_roles"],
                "occurred_at": row["occurred_at"],
                "exposure": money(
                    int(row["exposure_minor"]),
                    str(row["currency"]),
                    decimals=container.read_model.money_decimals,
                ),
                "four_eyes_required": bool(row["four_eyes_required"]),
                "four_eyes_state": row["four_eyes_state"],
                "confirmed_by": row.get("confirmed_by"),
                "confirmed_at": row.get("confirmed_at"),
                "reversal_of_decision_id": row.get("reversal_of_decision_id"),
                "decided_on_superseded_run": bool(row["decided_on_superseded_run"]),
                "prev_hash": row["prev_hash"],
                "row_hash": row["row_hash"],
            }
        )
    return out


def _economics_block(
    economic: dict[str, Any], *, decimals: int, assumptions_config: Any
) -> EconomicsBlock:
    assumptions = dict(economic.get("assumptions") or {})
    return EconomicsBlock(
        exposure=money(
            int(economic["exposure_minor"]), str(economic["currency"]), decimals=decimals
        ),
        expected_value=money(
            int(economic["expected_value_minor"]), str(economic["currency"]), decimals=decimals
        ),
        loss_avoided=money(
            int(economic["loss_avoided_minor"]), str(economic["currency"]), decimals=decimals
        ),
        analyst_minutes=float(economic["analyst_minutes"]),
        analyst_cost=money(
            int(economic["analyst_cost_minor"]), str(economic["currency"]), decimals=decimals
        ),
        friction_cost=money(
            int(economic["friction_cost_minor"]), str(economic["currency"]), decimals=decimals
        ),
        recovery_rate=float(economic["recovery_rate"]),
        recovery_sensitivity_band=list(assumptions_config.recovery.band),
        assumptions=[
            {
                "key": str(key),
                "value": value,
                "source": f"economics row (run copy) / config key {key}",
                "note": "stored with the run so a later edit of economics.yaml cannot "
                "reinterpret this money",
            }
            for key, value in sorted(assumptions.items())
        ],
        monte_carlo=MonteCarlo(
            runs=int(economic["mc_runs"]),
            seed=int(economic["mc_seed"]),
            p05=money(int(economic["mc_p05_minor"]), str(economic["currency"]), decimals=decimals),
            p50=money(int(economic["mc_p50_minor"]), str(economic["currency"]), decimals=decimals),
            p95=money(int(economic["mc_p95_minor"]), str(economic["currency"]), decimals=decimals),
            interval=[float(value) for value in (economic["mc_interval"] or [])],
        ),
    )


def _transaction_row(row: dict[str, Any], *, decimals: int) -> dict[str, Any]:
    return {
        "txn_id": row["txn_id"],
        "event_ts_utc": row["event_ts_utc"],
        "local_hour": int(row["local_hour"]),
        "event_date_local": row["event_date_local"],
        "src_account_key": row.get("src_account_key"),
        "dst_account_key": row.get("dst_account_key"),
        "amount": money(int(row["amount_minor"]), str(row["currency"]), decimals=decimals),
        "txn_type": str(row["txn_type"]),
        "src_balance_before": row.get("src_balance_before"),
        "src_balance_after": row.get("src_balance_after"),
        "dst_balance_before": row.get("dst_balance_before"),
        "dst_balance_after": row.get("dst_balance_after"),
        "label_fraud": row.get("label_fraud"),
        "label_typology": row.get("label_typology"),
    }


def _transactions(
    source: Any,
    run_id: str,
    account_key: str,
    limit: int,
    *,
    offset: int = 0,
    sort: str = "event_ts_utc",
    order: str = "desc",
    txn_ids: list[str] | None = None,
    account_side: str = "any",
) -> tuple[list[dict[str, Any]], int]:
    """Transactions with either side this account, or only the one the filter names.

    Both sides because exposure and counterparty structure run in and out; the
    ``account_side`` filter exists so the transaction table can answer "show me what
    left this account" without the client guessing which column a row matched on.
    """
    if txn_ids:
        rows: list[dict[str, Any]] = []
        total = 0
        for txn_id in txn_ids:
            found, count = source.select(
                "transaction", where={"run_id": run_id, "txn_id": txn_id}, allow_missing=True
            )
            rows.extend(found)
            total += count
        return rows[offset : offset + limit], total
    if account_side == "src":
        return source.select(
            "transaction",
            where={"run_id": run_id, "src_account_key": account_key},
            order=sort,
            descending=order == "desc",
            limit=limit,
            offset=offset,
            allow_missing=True,
        )
    if account_side == "dst":
        return source.select(
            "transaction",
            where={"run_id": run_id, "dst_account_key": account_key},
            order=sort,
            descending=order == "desc",
            limit=limit,
            offset=offset,
            allow_missing=True,
        )
    inbound, inbound_total = source.select(
        "transaction",
        where={"run_id": run_id, "dst_account_key": account_key},
        order=sort,
        descending=order == "desc",
        limit=limit,
        offset=offset,
        allow_missing=True,
    )
    outbound, outbound_total = source.select(
        "transaction",
        where={"run_id": run_id, "src_account_key": account_key},
        order=sort,
        descending=order == "desc",
        limit=limit,
        offset=offset,
        allow_missing=True,
    )
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for row in sorted(
        [*inbound, *outbound],
        key=lambda item: str(item.get("event_ts_utc")),
        reverse=order == "desc",
    ):
        key = str(row["txn_id"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged[:limit], inbound_total + outbound_total


def _watchlist_enrichment(
    container: Container, *, score: dict[str, Any], account_key: str
) -> WatchlistEnrichment | None:
    """Screening results, advisory by construction, or ``None`` when unconfigured.

    ``None`` rather than an empty hit list, because "screened and clean" and "not
    screened" are different claims and the case rail has to be able to say which.
    """
    adapter = container.watchlist_factory()
    version = adapter.version()
    if version.record_count == 0:
        return None
    # Nothing downstream of ingest carries a display name — the pseudonymisation
    # boundary (02 §F) makes that deliberate, and the port refuses a query with no name
    # or identifier rather than answering "clean". So the honest answer here is
    # "configured, not screened", which is a different claim from a hit-free screen and
    # must not be collapsed into one. Screening itself is exercised against a named
    # query in tests/contracts_adapters/test_watchlist_conformance.py.
    return WatchlistEnrichment(
        list_name=version.list_name,
        list_version=version.version,
        record_count=version.record_count,
        hits=[],
        advisory_only=True,
        note=(
            f"Screening list {version.list_name!r} ({version.record_count} records) is "
            "configured but NOT screened for this case: downstream of ingest there is no "
            "display name or raw identifier to match, by design (02 §F). An empty hit list "
            "here means 'nothing was matched against', not 'clean'. Screening is enrichment "
            "only and never an automatic decision."
        ),
    )


def counterfactual(
    read_model: ReadModel, *, run_id: str, account_key: str, points: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The cheapest single stored-bin change that would move this account's band.

    Deliberately narrow, and the note says so: it reads ``scorecard_point`` (what this
    account earned per attribute) against ``scorecard_bin`` (what every other bin in
    that attribute earns) and reports the smallest integer-point change that crosses a
    ``band_definition`` boundary. That is arithmetic over the fitted scorecard, not a
    second model call, and it is exactly the counterfactual plan §14 asks the case page
    to read aloud.
    """
    if len(points) < 1:
        return None
    bins, _ = read_model.source.select(
        "scorecard_bin", where={"run_id": run_id}, allow_missing=True
    )
    bands, _ = read_model.source.select(
        "band_definition", where={"run_id": run_id}, order="lower_points", allow_missing=True
    )
    if not bins or not bands:
        return None
    by_attribute: dict[str, list[dict[str, Any]]] = {}
    for row in bins:
        by_attribute.setdefault(str(row["attribute"]), []).append(row)
    usable = {
        attribute: rows
        for attribute, rows in by_attribute.items()
        if len(rows) >= MIN_BINS_FOR_COUNTERFACTUAL
    }
    total = int(sum(int(row["points"]) for row in points))
    current_band = next(
        (
            row
            for row in bands
            if int(row["lower_points"]) <= total
            and (row["upper_points"] is None or total <= int(row["upper_points"]))
        ),
        None,
    )
    if current_band is None:
        return None
    candidates: list[dict[str, Any]] = []
    for row in points:
        attribute = str(row["attribute"])
        alternatives = usable.get(attribute) or []
        for alternative in alternatives:
            if str(alternative["label"]) == str(row["bin_label"]):
                continue
            delta = int(alternative["points"]) - int(row["points"])
            moved = total + delta
            target = next(
                (
                    band
                    for band in bands
                    if int(band["lower_points"]) <= moved
                    and (band["upper_points"] is None or moved <= int(band["upper_points"]))
                ),
                None,
            )
            if target is None or str(target["band"]) == str(current_band["band"]):
                continue
            candidates.append(
                {
                    "attribute": attribute,
                    "from_bin": str(row["bin_label"]),
                    "to_bin": str(alternative["label"]),
                    "points_delta": delta,
                    "resulting_points": moved,
                    "resulting_band": str(target["band"]),
                    "resulting_action": str(target["action"]),
                }
            )
    if not candidates:
        return {
            "possible": False,
            "current_band": str(current_band["band"]),
            "total_points": total,
            "note": (
                "no single stored bin change moves this account across a band boundary; the "
                "band is a consequence of several attributes at once, which is the point of a "
                "scorecard and not a gap in this response"
            ),
        }
    candidates.sort(key=lambda item: (abs(int(item["points_delta"])), str(item["attribute"])))
    return {
        "possible": True,
        "current_band": str(current_band["band"]),
        "current_action": str(current_band["action"]),
        "total_points": total,
        "cheapest_change": candidates[0],
        "alternatives": candidates[1:4],
        "basis": "stored scorecard_point vs scorecard_bin vs band_definition; integer points, "
        "no model called",
    }


__all__ = ["router"]
