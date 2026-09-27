"""Policy routes: the active policy's cutoff, and a simulator that re-allocates.

Plan §14's rule for this screen is blunt: *"Nothing here is precomputed theatre — which
is why the allocation has to be fast."* So ``POST /api/policy/simulate`` runs
:func:`oxbow.quant.allocate.allocate` over the stored priced queue and returns what it
actually decided: the selection, the rank order, the minutes consumed, the value
forgone by the budget, the greedy-versus-exact gap, the frontier point the slider is
on, and what entered and left the review set.

The one thing this router will not do is present a fallback as a choice. When the
exact solver degrades — deadline exceeded or a solver error — the allocator's own
label says ``DEGRADED`` and ``meta.degraded`` is true, because "greedy got within
0.4 % of optimal" and "greedy because ortools was missing" are different findings that
a single curve would conflate.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.deps import Container, analyst_or_higher, get_container, reviewer_or_higher
from api.policy_engine import (
    run_simulation,
    stored_policies,
    stored_policy,
)
from api.problems import (
    COMMON_ERROR_STATUSES,
    DependencyUnavailable,
    NotFound,
    problem_responses,
)
from api.routers.common import assumption_lines, build_meta
from api.schemas.common import Envelope, envelope
from api.schemas.policy import (
    ActivePolicyResponse,
    AllocationRow,
    PolicySummaryView,
    PolicyView,
    SimulationRequest,
    SimulationResult,
)
from api.security import Principal

router = APIRouter(prefix="/api/policy", tags=["policy"])


@router.get(
    "",
    response_model=Envelope[ActivePolicyResponse],
    summary="The active policy, its summary, its rank list and its cutoff",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def active_policy(
    run_id: str | None = Query(default=None, min_length=26, max_length=26),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    run = read_model.resolve_run(run_id, state="complete" if run_id is None else None)
    rid = str(run["run_id"])
    if not container.write_path_enabled:
        raise DependencyUnavailable(
            "no active policy can be reported: the policy table is written by the API's own "
            "write path, which needs Postgres. The simulator still works here — it allocates "
            "over the stored scores — and it is at POST /api/policy/simulate."
        )
    session = container.new_session()
    try:
        active = stored_policy(session, policy_id=None)
        if active is None:
            raise NotFound(
                f"no policy is flagged active in run {rid}'s warehouse. Either the pipeline "
                "wrote none or the flag was never set; the queue cannot draw a cutoff line "
                "from an absent policy, and it will not guess one from config alone."
            )
        summaries, _ = read_model.source.select(
            "policy_summary", where={"run_id": rid, "policy_id": str(active["policy_id"])}, limit=1
        )
        allocation_rows, allocation_total = read_model.source.select(
            "policy_allocation",
            where={"run_id": rid, "policy_id": str(active["policy_id"])},
            order="rank",
            limit=200,
            allow_missing=True,
        )
        policies = stored_policies(session)
    finally:
        session.close()

    cutoff = max(
        (int(row["rank"]) for row in allocation_rows if bool(row["selected"])), default=None
    )
    # The exponent, not the base: config declares minor_units_per_major (100) and
    # both this server and apps/web/src/lib/format/money.ts raise ten to whatever
    # arrives in a `decimals` field. Inherited from the read model, which converts
    # once and refuses a base that is not an exact power of ten.
    decimals = container.read_model.money_decimals
    body = ActivePolicyResponse(
        run_id=rid,
        policy=PolicyView.model_validate(_policy_for_response(active, decimals)),
        summary=None
        if not summaries
        else PolicySummaryView.model_validate(_summary_for_response(summaries[0], decimals)),
        allocation=[
            AllocationRow.model_validate(_allocation_for_response(row, decimals))
            for row in allocation_rows
        ],
        cutoff_rank=cutoff,
        capacity_minutes=int(active["capacity_minutes"]),
        allocation_source="stored" if allocation_total else "none",
        policies=[
            PolicyView.model_validate(_policy_for_response(row, decimals)) for row in policies
        ],
    )
    return envelope(
        body,
        **build_meta(
            container,
            run_id=rid,
            assumptions=[line.model_dump() for line in assumption_lines(container.economics)],
        ).model_dump(),
    )


@router.post(
    "/simulate",
    response_model=Envelope[SimulationResult],
    summary="Re-allocate for real under new capacity, rate or solver",
    description=(
        "The allocator runs against the stored priced queue. Money figures are re-priced "
        "only over the configured recovery band, and the response says which solver "
        "produced it, in how many milliseconds, and what the budget cost in forgone value."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def simulate(
    body: SimulationRequest,
    run_id: str | None = Query(default=None, min_length=26, max_length=26),
    persist: bool = Query(
        default=False, description="Record the simulation in policy_simulation for later reading."
    ),
    container: Container = Depends(get_container),
    principal: Principal = Depends(reviewer_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    run = read_model.resolve_run(run_id, state="complete" if run_id is None else None)
    rid = str(run["run_id"])
    base = _base_policy(container, rid)
    payload = run_simulation(
        read_model=read_model,
        assumptions=container.economics,
        run_id=rid,
        request=body,
        base_policy=base,
    )
    simulation_id: str | None = None
    if persist and container.write_path_enabled:
        simulation_id = _record_simulation(
            container, run_id=rid, base=base, body=body, payload=payload
        )
    payload["simulation_id"] = simulation_id
    return envelope(
        SimulationResult.model_validate(payload),
        **build_meta(
            container,
            run_id=rid,
            provenance=str(run["provenance"]),
            model_version=str(run["model_version"]),
            assumptions=[line.model_dump() for line in assumption_lines(container.economics)],
            extra={
                "degraded": bool(payload["degraded"]) or container.status() != "ok",
                "degraded_reason": payload.get("degraded_reason"),
            },
        ).model_dump(),
    )


def _base_policy(container: Container, run_id: str) -> dict[str, Any] | None:
    if not container.write_path_enabled:
        return None
    session = container.new_session()
    try:
        return stored_policy(session, policy_id=None)
    finally:
        session.close()


def _record_simulation(
    container: Container,
    *,
    run_id: str,
    base: dict[str, Any] | None,
    body: SimulationRequest,
    payload: dict[str, Any],
) -> str:
    """Store one simulation, so the answer survives the tab and the demo."""
    from datetime import UTC, datetime

    from ulid import ULID

    from oxbow.adapters.warehouse.models import PolicySimulation

    simulation_id = f"sim_{ULID()!s}"
    session = container.new_session()
    try:
        session.add(
            PolicySimulation(
                simulation_id=simulation_id,
                run_id=run_id,
                base_policy_id=None if base is None else str(base["policy_id"]),
                request=body.model_dump(exclude_none=True),
                result={
                    key: value
                    for key, value in payload.items()
                    if key in {"selected_count", "cutoff_rank", "state", "solver", "message"}
                },
                entered=list(payload["entered"]),
                left=list(payload["left"]),
                elapsed_ms=int(payload["solve_ms"]),
                degraded=bool(payload["degraded"]),
                degraded_reason=payload.get("degraded_reason"),
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    return simulation_id


def _allocation_for_response(row: dict[str, Any], decimals: int) -> dict[str, Any]:
    """Stored ``*_minor`` columns into the ``Money`` shape the client renders."""
    from api.readmodel import money

    currency = str(row.get("currency") or "UGX")
    return {
        "account_key": str(row["account_key"]),
        "rank": int(row["rank"]),
        "selected": bool(row["selected"]),
        "beyond_capacity": bool(row["beyond_capacity"]),
        "expected_value": money(int(row["expected_value_minor"]), currency, decimals=decimals),
        "exposure": money(int(row["exposure_minor"]), currency, decimals=decimals),
        "analyst_minutes": float(row["analyst_minutes"]),
        "ev_density": float(row["ev_density"]),
    }


def _policy_for_response(row: dict[str, Any], decimals: int) -> dict[str, Any]:
    del decimals  # the policy row's money columns are already converted by policy_engine
    return {**row}


def _summary_for_response(row: dict[str, Any], decimals: int) -> dict[str, Any]:
    """Rename the stored ``*_minor`` columns into ``Money`` objects.

    The wire shape carries currency with every amount, so a client cannot print a
    minor-unit integer as if it were a major-unit one — the mistake that a bare
    ``net_benefit: 43000000`` invites.
    """
    from api.readmodel import money

    currency = str(row["currency"])

    def amount(field: str) -> dict[str, Any]:
        return money(int(row[field]), currency, decimals=decimals)

    return {
        "policy_id": str(row["policy_id"]),
        "currency": currency,
        "capacity_minutes": int(row["capacity_minutes"]),
        "selected_count": int(row["selected_count"]),
        "candidate_count": int(row["candidate_count"]),
        "loss_avoided": amount("loss_avoided_minor"),
        "analyst_cost": amount("analyst_cost_minor"),
        "friction_cost": amount("friction_cost_minor"),
        "net_benefit": amount("net_benefit_minor"),
        "benefit_per_analyst_hour": amount("benefit_per_analyst_hour_minor"),
        "max_drawdown": amount("max_drawdown_minor"),
        "zero_drawdown": bool(row["zero_drawdown"]),
        "zero_drawdown_note": (
            "drawdown is zero because this policy never lost money across these periods — "
            "stated rather than left as an empty chart (plan §12)"
        )
        if bool(row["zero_drawdown"])
        else None,
        "var_alpha": float(row["var_alpha"]),
        "var95": amount("var95_minor"),
        "es_alpha": float(row["es_alpha"]),
        "es975": amount("es975_minor"),
        "monte_carlo_runs": int(row["mc_runs"]),
        "monte_carlo_seed": int(row["mc_seed"]),
        "alerts_per_10k_accounts": float(row["alerts_per_10k_accounts"]),
        "risk_adjusted_benefit": float(row["risk_adjusted_benefit"]),
        "risk_adjusted_benefit_note": str(row["risk_adjusted_benefit_note"]),
        "cumulative_curve": list(row["cumulative_curve"] or []),
        "frontier": list(row["frontier"] or []),
        "baselines": dict(row["baselines"] or {}),
        "assumptions": dict(row["assumptions"] or {}),
    }


__all__ = ["router"]
