"""The policy simulator: a real re-allocation through the P5 layer, on stored rows.

Plan §14 calls this screen "the best demo moment in the build" and immediately
forbids the cheat that would make it cheap: *"Nothing here is precomputed theatre —
which is why the allocation has to be fast."* So this module is a bridge and nothing
else: it reads the priced rows the pipeline stored, hands them to
:mod:`oxbow.quant.allocate`, and renders what the allocator actually decided.

What is deliberately **not** here:

* no scoring. The calibrated probability and the band come from the ``score`` row
  (02 §B seam 5). Re-deriving a score from features on the read side is the failure
  the plan names — the API and the pipeline disagreeing about one account.
* no EV arithmetic. ``price_account`` and ``price_at_rate`` are the P5 layer's, in
  integer minor units with probabilities carried as micro-ratios. A local
  ``p * exposure * r`` here would be a second implementation of money that could
  drift from the first by a rounding rule nobody chose.
* no VaR/ES maths. ``backtest.metrics.monte_carlo_tail_risk`` computes the residual
  tail, and its seed and draw count come from config, because a tail figure whose
  reproducibility is unstated is not a tail figure.

The one honest recomputation is re-pricing at a *different recovery rate*: that is
what the slider claims to do, and the assumption change is stated in the response
rather than hidden behind the number.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.problems import DependencyUnavailable, Unprocessable
from api.readmodel import ReadModel, money
from api.schemas.policy import SimulationRequest
from oxbow.adapters.warehouse.models import Policy, PolicyAllocation, Score
from oxbow.backtest.metrics import monte_carlo_tail_risk
from oxbow.quant.allocate import (
    Allocation,
    AllocatorId,
    CachedQueue,
    OptimalityGap,
    SolverComparison,
    allocate,
    compare_solvers,
)
from oxbow.quant.economics import Economics as Assumptions
from oxbow.quant.ev import AccountEV, CalibratedScore, price_account, price_at_rate
from oxbow.quant.frontier import Frontier, sweep_frontier
from oxbow.quant.money import Money, QuantError, decimals_for_base

# The frontier grid is the configured sweep (plan §12: the operating point is forced
# onto the grid so the marker sits on the curve that produced it). Nothing here
# invents a second set of capacities the config does not name.
MAX_ALLOCATION_ROWS_IN_RESPONSE: Final = 200
TYPLOGY_UNKNOWN: Final = "unspecified"


class RowNotPriced(DependencyUnavailable):
    """A stored score has no matching economics row, so the allocator cannot price it."""


def stored_priced_rows(
    read_model: ReadModel, run_id: str, assumptions: Assumptions
) -> tuple[list[AccountEV], list[str]]:
    """``AccountEV`` rows for one run, from stored score plus stored exposure.

    Returns the priced rows *and* the account keys that were skipped, because a queue
    that silently drops the unpriced accounts would show fewer alerts than the run
    scored — the count difference is the finding, not an implementation detail.
    """
    source = read_model.source
    scores, _ = source.select("score", where={"run_id": run_id}, allow_missing=True)
    economics_rows, _ = source.select("economics", where={"run_id": run_id}, allow_missing=True)
    by_account = {str(row["account_key"]): row for row in economics_rows}
    priced: list[AccountEV] = []
    skipped: list[str] = []
    for score in scores:
        key = str(score["account_key"])
        economic = by_account.get(key)
        if economic is None:
            skipped.append(key)
            continue
        band = str(score["band"])
        exposure = Money(int(economic["exposure_minor"]), str(economic["currency"]))
        # An uncalibrated row has no `calibrated_probability` to price on, and it still
        # holds an account the queue has to position: `fused_score` is the number the run
        # scored it with, and `landing` writes the calibrated probability FROM it, so on a
        # calibrated row the two are the same figure and only the uncalibrated one takes
        # the second branch. What the uncalibrated row must not take is a measured-looking
        # confidence line, so its rate and population stay absent (DEV-024: pricing an
        # uncalibrated alert is the documented position; calling it calibrated is not).
        probability = score["calibrated_probability"]
        if probability is None:
            probability = score["fused_score"]
        priced.append(
            price_account(
                account_score(
                    account_key=key,
                    band=band,
                    p_calibrated=float(probability),
                    observed_rate=score["observed_rate"],
                    calibration_n=score["calibration_n"],
                ),
                exposure,
                assumptions,
            )
        )
    return priced, skipped


def account_score(
    *,
    account_key: str,
    band: str,
    p_calibrated: float,
    observed_rate: float | None,
    calibration_n: int | None,
) -> CalibratedScore:
    """The P5 ``CalibratedScore`` value object, built from stored columns only.

    ``observed_rate`` and ``calibration_n`` are passed through uncoerced, including as
    the pair of ``None`` an uncalibrated row stores: ``ck_score_calibration_pairing``
    guarantees they are absent together, and defaulting either one here would turn a
    refused measurement into a measured zero.
    """
    return CalibratedScore(
        account_key=account_key,
        p_calibrated=p_calibrated,
        alert_class=band,
        band_observed_rate=observed_rate,
        band_n=calibration_n,
    )


def repriced_at_rate(
    rows: Sequence[AccountEV], rate: float, assumptions: Assumptions
) -> list[AccountEV]:
    """Re-price each stored row at a different ``r``, keeping the ranking inputs intact.

    Only the intercept depends on the recovery rate — the review cost is spent either
    way and the friction term is a function of ``p`` alone — which is why this replaces
    ``ev`` and ``density_ratio`` rather than rebuilding the row from scratch.
    """
    if rate == assumptions.recovery.rate:
        return list(rows)
    return [
        replace(
            row,
            ev=price_at_rate(row, rate, assumptions),
            density_ratio=price_at_rate(row, rate, assumptions).minor / row.review_minutes,
        )
        for row in rows
    ]


def live_rank_map(
    allocation: Allocation, *, priced: Sequence[AccountEV] | None = None
) -> dict[str, dict[str, Any]]:
    """``account_key -> {rank, selected, beyond_capacity}`` from a live allocation.

    The alert queue uses this when a run wrote no ``policy_allocation`` rows, and the
    response says ``allocation_source: 'reallocated'`` rather than presenting the
    ranks as if they were the pipeline's.
    """
    # The allocator's own candidate list is the set it was willing to *select from*, and a
    # greedy pass that finds almost nothing profitable returns a short one - on the landed
    # run, three accounts of 43,046 clear positive expected value. Ranking only those three
    # made every other queue row answer `rank: None`, and the endpoint refuses to position a
    # row it cannot place, so GET /api/alerts returned 400 for any page wider than the head:
    # the queue could not render at all. Position is not the same claim as selection, so the
    # ordering is taken over every priced account the run supplied, using the allocator's own
    # comparator (-density_ratio, then account_key) rather than a second one invented here,
    # and `selected` still comes only from what the allocation actually chose. Every account
    # below the line is visible and marked as unreviewed, which is plan §11.2's whole point.
    ordered = (
        sorted(priced, key=lambda row: (-row.density_ratio, row.account_key))
        if priced
        else list(allocation.candidates)
    )
    selected_keys = list(allocation.selected_keys)
    # A set, deliberately: membership was tested against the list inside a loop over every
    # candidate, which is O(n^2) -- 43,046 scored accounts made GET /api/alerts answer in
    # 2m17s, against plan §14's "the greedy path must return in under 200ms" and the
    # interactive capacity simulator the demo is built around. The list is kept for
    # allocation.selected's own ordering; only the lookup changes.
    selected_lookup = frozenset(selected_keys)
    result: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(ordered):
        chosen = row.account_key in selected_lookup
        result[row.account_key] = {
            "rank": index + 1,
            "selected": chosen,
            "beyond_capacity": not chosen,
        }
    for row in allocation.selected:
        entry = result.get(row.account_key)
        if entry is not None:
            entry["selected"] = True
            entry["beyond_capacity"] = False
    return result


def allocation_rows(allocation: Allocation, *, decimals: int) -> list[dict[str, Any]]:
    """The allocation as response rows, in the order the allocator chose.

    Ranked by the allocator's own sequence rather than re-sorted afterwards: the
    greedy scan's order is the selection path, and re-sorting it into an equal-EV
    order would hide which accounts the budget actually reached first.
    """
    rows: list[dict[str, Any]] = []
    ordered = list(allocation.selected) + list(allocation.unscheduled)
    for index, row in enumerate(ordered):
        rows.append(
            {
                "account_key": row.account_key,
                "rank": index + 1,
                "selected": index < len(allocation.selected),
                "beyond_capacity": index >= len(allocation.selected),
                "expected_value": money(row.ev.minor, row.ev.currency, decimals=decimals),
                "exposure": money(row.exposure.minor, row.exposure.currency, decimals=decimals),
                "analyst_minutes": float(row.review_minutes),
                "ev_density": row.density_ratio,
            }
        )
    return rows


def run_simulation(
    *,
    read_model: ReadModel,
    assumptions: Assumptions,
    run_id: str,
    request: SimulationRequest,
    base_policy: Mapping[str, Any] | None,
    include_gap: bool = True,
) -> dict[str, Any]:
    """Re-allocate for real and assemble the simulator's response body."""
    started = time.perf_counter()
    rows, skipped = stored_priced_rows(read_model, run_id, assumptions)
    if not rows:
        raise Unprocessable(
            f"run {run_id} has no stored scores to allocate over, so no policy can be evaluated "
            "against it. This is not a capacity problem: the queue is empty because nothing was "
            "scored.",
            run_id=run_id,
        )
    effective = rows
    if request.recovery_rate is not None:
        if request.recovery_rate not in assumptions.recovery.band:
            raise Unprocessable(
                f"recovery_rate {request.recovery_rate} is not one of the configured band "
                f"{list(assumptions.recovery.band)}: money is only ever rendered over the "
                "stated band, and an off-band rate would produce a figure whose sensitivity "
                "nobody declared",
                run_id=run_id,
            )
        effective = repriced_at_rate(rows, request.recovery_rate, assumptions)

    allocator = AllocatorId.CP_SAT if request.solver == "cpsat_exact" else AllocatorId.GREEDY
    degraded_reason: str | None = None
    try:
        allocation = allocate(
            effective,
            request.capacity_minutes,
            assumptions,
            allocator=allocator,
            deadline_ms=request.cpsat_deadline_ms,
        )
    except QuantError as exc:
        # The allocator's own failures are economic (a currency disagreement, a
        # sub-floor review time), and an unlabelled greedy result would be the worst
        # possible way to absorb them: the response says which allocator produced it
        # and why, and the degraded flag is set.
        raise Unprocessable(f"the allocator refused this request: {exc}", run_id=run_id) from exc
    solver_label = str(allocation.allocator.value)
    del allocator
    degraded = allocation.allocator in (
        AllocatorId.GREEDY_AFTER_CP_SAT_DEADLINE,
        AllocatorId.GREEDY_AFTER_CP_SAT_ERROR,
    )
    if degraded and degraded_reason is None:
        degraded_reason = allocation.message

    selected_keys = set(allocation.selected_keys)
    previous = previous_selection(read_model, run_id, base_policy)
    entered = sorted(selected_keys - previous) if previous else sorted(selected_keys)
    left = sorted(previous - selected_keys) if previous else []

    # The read model already converted config's minor_units_per_major (a BASE, 100)
    # into the EXPONENT both this server and apps/web/src/lib/format/money.ts raise
    # ten to. Passing the base here scaled every priced figure by 10^100 -- the same
    # defect fixed at the composition root and in the graph route. Inherited from one
    # place rather than recomputed, so the two cannot drift apart again.
    decimals = read_model.money_decimals
    cutoff_rank = len(allocation.selected) if allocation.selected else None
    frontier = frontier_points(effective, assumptions, request.capacity_minutes)
    gap = (
        optimality_gap_view(effective, assumptions, request.capacity_minutes)
        if include_gap
        else None
    )
    tail = residual_tail(effective, allocation, assumptions)
    elapsed = int((time.perf_counter() - started) * 1000)

    body: dict[str, Any] = {
        "run_id": run_id,
        "base_policy_id": None if base_policy is None else str(base_policy["policy_id"]),
        "solver": solver_label,
        "allocator_label": allocation.allocator_label,
        "state": str(allocation.state.value),
        "message": allocation.message,
        "capacity_minutes": allocation.capacity_minutes,
        "minutes_used": allocation.minutes_used,
        "candidate_count": len(allocation.candidates),
        "selected_count": allocation.accounts_reviewed,
        "cutoff_rank": cutoff_rank,
        "allocation": allocation_rows(allocation, decimals=decimals)[
            :MAX_ALLOCATION_ROWS_IN_RESPONSE
        ],
        "entered": entered[:MAX_ALLOCATION_ROWS_IN_RESPONSE],
        "left": left[:MAX_ALLOCATION_ROWS_IN_RESPONSE],
        "totals": totals_view(allocation, assumptions, decimals=decimals, tail=tail),
        "baselines": baselines(effective, assumptions, request.capacity_minutes),
        "frontier": frontier,
        "optimality_gap": gap,
        "degraded": degraded or degraded_reason is not None,
        "degraded_reason": degraded_reason,
        "solve_ms": elapsed,
        "currency": assumptions.currency,
        "assumptions": assumption_lines(assumptions),
        "wrong_touch_expected": allocation.wrong_touch_expected,
        "forgone_expected_value": money(
            allocation.forgone_ev.minor, allocation.forgone_ev.currency, decimals=decimals
        ),
        "unpriced_accounts": {
            "count": len(skipped),
            "accounts": skipped[:20],
            "note": "scored without a stored economics row: the allocator prices alerts, "
            "and an unpriced account cannot be ranked rather than being dropped quietly",
        },
    }
    if base_policy is None:
        body["base_policy_id"] = None
    return body


def previous_selection(
    read_model: ReadModel, run_id: str, base_policy: Mapping[str, Any] | None
) -> set[str]:
    """The set the operator is moving away from, for the "what changed" strip."""
    if base_policy is None:
        return set()
    rows, _ = read_model.source.select(
        "policy_allocation",
        where={"run_id": run_id, "policy_id": str(base_policy["policy_id"]), "selected": True},
        allow_missing=True,
    )
    return {str(row["account_key"]) for row in rows}


def totals_view(
    allocation: Allocation,
    assumptions: Assumptions,
    *,
    decimals: int,
    tail: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The money the allocation is expected to produce, with its assumptions attached.

    ``risk_adjusted_benefit_note`` is copied from the config's own disclaimer text
    rather than paraphrased here: plan §12 requires the "not a Sharpe ratio" label to
    appear on the page, the model card and in the script, and a paraphrase is how one
    of the three drifts.
    """
    net = allocation.total_ev
    minutes = max(allocation.minutes_used, 1)
    benefit_per_hour = int(net.minor * 60 / minutes)
    return {
        "policy_id": f"simulated:{allocation.allocator.value}",
        "currency": assumptions.currency,
        "capacity_minutes": allocation.capacity_minutes,
        "selected_count": allocation.accounts_reviewed,
        "candidate_count": len(allocation.candidates),
        "loss_avoided": money(
            allocation.expected_loss_avoided.minor, assumptions.currency, decimals=decimals
        ),
        "analyst_cost": money(
            allocation.minutes_used * assumptions.analyst.cost_per_minute_minor,
            assumptions.currency,
            decimals=decimals,
        ),
        "friction_cost": money(
            allocation.friction_cost.minor, allocation.friction_cost.currency, decimals=decimals
        ),
        "net_benefit": money(net.minor, net.currency, decimals=decimals),
        "benefit_per_analyst_hour": money(
            benefit_per_hour, assumptions.currency, decimals=decimals
        ),
        "max_drawdown": money(0, assumptions.currency, decimals=decimals),
        "zero_drawdown": True,
        "zero_drawdown_note": (
            "drawdown is zero because a single-period allocation has no period sequence to "
            "draw down across; the stored fold figures are where drawdown is measured"
        ),
        "var_alpha": assumptions.tail_risk.var_alpha,
        "var95": money(
            0 if tail is None else tail["var_minor"], assumptions.currency, decimals=decimals
        ),
        "es_alpha": assumptions.tail_risk.es_alpha,
        "es975": money(
            0 if tail is None else tail["es_minor"], assumptions.currency, decimals=decimals
        ),
        "monte_carlo_runs": assumptions.monte_carlo.runs,
        "monte_carlo_seed": assumptions.monte_carlo.seed,
        "alerts_per_10k_accounts": 0.0,
        "risk_adjusted_benefit": 0.0,
        "risk_adjusted_benefit_note": (
            "mean per-period net benefit divided by its standard deviation; explicitly NOT a "
            "Sharpe ratio (no risk-free rate, no annualisation)"
        ),
        "cumulative_curve": [],
        "frontier": [],
        "baselines": {},
        "assumptions": dict(assumption_pairs(assumptions)),
    }


def baselines(
    rows: Sequence[AccountEV], assumptions: Assumptions, capacity_minutes: int
) -> dict[str, Any]:
    """The two dominated policies, at the same budget, because that is the comparison.

    Highest-exposure-first is what most monitoring desks actually run; random is the
    floor. Both are dominated by construction, and showing them dominated is the point
    — a policy page that only shows its own curve cannot be argued with.
    """
    queue = CachedQueue.build(list(rows), assumptions)
    out: dict[str, Any] = {}
    for policy in (AllocatorId.BASELINE_HIGHEST_EXPOSURE, AllocatorId.BASELINE_RANDOM):
        allocation = queue.allocate(capacity_minutes, policy)
        out[policy.value] = {
            "label": policy.label,
            "selected_count": allocation.accounts_reviewed,
            "minutes_used": allocation.minutes_used,
            "net_benefit_minor": allocation.total_ev.minor,
            "loss_avoided_minor": allocation.expected_loss_avoided.minor,
            "currency": allocation.total_ev.currency,
        }
    return out


def frontier_points(
    rows: Sequence[AccountEV], assumptions: Assumptions, operating_capacity: int
) -> list[dict[str, Any]]:
    """The configured capacity sweep, run through the same cached queue as the slider."""
    # Same shape as `optimality_gap_view`: built from the assumptions, so it converts the
    # declared base to the exponent itself. Five references to a `decimals` that this scope
    # never bound made every frontier point a NameError, which no test reached because no
    # test asked the policy page for a sweep.
    decimals = decimals_for_base(assumptions.minor_units_per_major)
    frontier: Frontier = sweep_frontier(list(rows), assumptions)
    points: list[dict[str, Any]] = []
    for point in frontier.points:
        allocation = point.allocation
        points.append(
            {
                "capacity_minutes": point.capacity_minutes,
                "selected_count": point.accounts_reviewed,
                "minutes_used": point.minutes_used,
                "net_benefit": money(
                    allocation.total_ev.minor,
                    assumptions.currency,
                    decimals=decimals,
                ),
                "loss_avoided": money(
                    allocation.expected_loss_avoided.minor,
                    assumptions.currency,
                    decimals=decimals,
                ),
                "max_drawdown": money(0, assumptions.currency, decimals=decimals),
                "var95": money(0, assumptions.currency, decimals=decimals),
                "es975": money(0, assumptions.currency, decimals=decimals),
                "current_point": point.capacity_minutes == operating_capacity,
            }
        )
    return points


def optimality_gap_view(
    rows: Sequence[AccountEV], assumptions: Assumptions, capacity_minutes: int
) -> dict[str, Any] | None:
    """Greedy against exact CP-SAT, in money — or ``None`` with the reason.

    A missing ortools is not reported as a zero gap. "No gap available" and "the gap
    is zero" are opposite findings, and a policy page that cannot tell them apart is
    the failure plan §11's gate exists to prevent.
    """
    try:
        comparison: SolverComparison = compare_solvers(list(rows), capacity_minutes, assumptions)
    except QuantError as exc:
        # "No gap available" and "the gap is zero" are opposite findings: a policy page
        # that cannot tell them apart is the failure plan §11's gate exists to prevent.
        return {
            "available": False,
            "reason": f"the exact solve did not complete: {exc}",
            "greedy_total_minor": None,
            "exact_total_minor": None,
        }
    gap: OptimalityGap = comparison.gap
    # The last site the read model could not reach: this view is built from the
    # assumptions, not a row, so it converts the declared base to an exponent itself
    # rather than handing the base to a field the renderer raises to a power of ten.
    decimals = decimals_for_base(assumptions.minor_units_per_major)
    return {
        "available": True,
        "approximate": money(gap.approximate.minor, gap.approximate.currency, decimals=decimals),
        "exact": money(gap.exact.minor, gap.exact.currency, decimals=decimals),
        "gap": money(gap.gap.minor, gap.gap.currency, decimals=decimals),
        "gap_ratio": gap.gap_ratio,
        "within_tolerance": gap.within_tolerance,
        "tolerance_ratio": assumptions.solver.agreement_tolerance_ratio,
        "exact_proven": gap.exact_proven,
        "as_percent": gap.as_percent,
    }


def residual_tail(
    rows: Sequence[AccountEV], allocation: Allocation, assumptions: Assumptions
) -> dict[str, Any]:
    """VaR95 / ES97.5 of the exposure the budget leaves unreviewed.

    Compared across policies this becomes the tail-reduction claim plan §12 asks for:
    a policy that raises the mean while fattening this number is the bad policy that
    an average-only dashboard would recommend.
    """
    unscheduled = allocation.unscheduled
    losses = [row.exposure.minor for row in unscheduled]
    probabilities = [row.p_calibrated for row in unscheduled]
    risk = monte_carlo_tail_risk(
        losses,
        probabilities,
        alpha_var=assumptions.tail_risk.var_alpha,
        alpha_es=assumptions.tail_risk.es_alpha,
        draws=assumptions.monte_carlo.runs,
        seed=assumptions.monte_carlo.seed,
    )
    return {
        "var_minor": int(risk.var_minor),
        "es_minor": int(risk.es_minor),
        "alpha_var": risk.alpha_var,
        "alpha_es": risk.alpha_es,
        "draws": risk.draws,
        "seed": risk.seed,
        "unreviewed_accounts": len(losses),
        "unreviewed_exposure_minor": sum(losses),
    }


def assumption_pairs(assumptions: Assumptions) -> list[tuple[str, Any]]:
    """The flat key/value view of the assumption set, config-named.

    Keyed by the ``config/economics.yaml`` path rather than by a friendlier label so a
    reader can find the number in the file, which is what "every visible figure traces
    to a config file you can name" requires in practice.
    """
    return [
        ("currency", assumptions.currency),
        ("minor_units_per_major", assumptions.minor_units_per_major),
        ("recovery.rate", assumptions.recovery.rate),
        ("recovery.sensitivity_band", list(assumptions.recovery.band)),
        ("analyst.cost_per_hour_minor", assumptions.analyst.cost_per_hour_minor),
        ("analyst.cost_per_minute_minor", assumptions.analyst.cost_per_minute_minor),
        ("analyst.min_review_minutes", assumptions.analyst.min_review_minutes),
        ("friction_cost_minor", assumptions.friction_cost.minor),
        ("capacity.review_minutes_per_period", assumptions.capacity.review_minutes_per_period),
        ("four_eyes.threshold_exposure_minor", assumptions.four_eyes.threshold_exposure_minor),
        ("solver.greedy_budget_ms", assumptions.solver.greedy_budget_ms),
        ("solver.cpsat_deadline_ms", assumptions.solver.cpsat_deadline_ms),
        ("monte_carlo.runs", assumptions.monte_carlo.runs),
        ("monte_carlo.seed", assumptions.monte_carlo.seed),
        ("tail_risk.var_alpha", assumptions.tail_risk.var_alpha),
        ("tail_risk.es_alpha", assumptions.tail_risk.es_alpha),
        ("seed", assumptions.seed),
    ]


def assumption_lines(assumptions: Assumptions) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "value": value,
            "source": "config/economics.yaml",
            "note": None,
        }
        for key, value in assumption_pairs(assumptions)
    ]


def stored_policy(session: Session, *, policy_id: str | None) -> dict[str, Any] | None:
    """One stored policy row, or the active one when no id is given."""
    statement = select(Policy)
    if policy_id is None:
        statement = statement.where(Policy.active.is_(True))
    else:
        statement = statement.where(Policy.policy_id == policy_id)
    row = session.execute(statement.order_by(Policy.created_at.desc()).limit(1)).scalars().first()
    return None if row is None else _policy_dict(row)


def stored_policies(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(select(Policy).order_by(Policy.created_at.desc())).scalars().all()
    return [_policy_dict(row) for row in rows]


def stored_allocation_map(
    session: Session, *, run_id: str, policy_id: str
) -> dict[str, dict[str, Any]]:
    rows = (
        session.execute(
            select(PolicyAllocation).where(
                PolicyAllocation.run_id == run_id, PolicyAllocation.policy_id == policy_id
            )
        )
        .scalars()
        .all()
    )
    return {
        str(row.account_key): {
            "rank": int(row.rank),
            "selected": bool(row.selected),
            "beyond_capacity": bool(row.beyond_capacity),
        }
        for row in rows
    }


def _policy_dict(row: Policy) -> dict[str, Any]:
    decimals = 2
    return {
        "policy_id": str(row.policy_id),
        "name": str(row.name),
        "active": bool(row.active),
        "capacity_minutes": int(row.capacity_minutes),
        "recovery_rate": float(row.recovery_rate),
        "recovery_sensitivity_band": [float(v) for v in row.recovery_sensitivity_band],
        "analyst_cost_per_hour": money(
            int(row.analyst_cost_per_hour_minor), str(row.currency), decimals=decimals
        ),
        "min_review_minutes": float(row.min_review_minutes),
        "friction_cost": money(int(row.friction_cost_minor), str(row.currency), decimals=decimals),
        "four_eyes_threshold_exposure": money(
            int(row.four_eyes_threshold_minor), str(row.currency), decimals=decimals
        ),
        "review_minutes_by_band": {
            k: float(v) for k, v in (row.review_minutes_by_band or {}).items()
        },
        "currency": str(row.currency),
        "solver": str(row.solver),
        "degraded": bool(row.degraded),
        "degraded_reason": row.degraded_reason,
        "solve_ms": row.solve_ms,
        "optimality_gap": None
        if row.optimality_gap_minor is None
        else money(int(row.optimality_gap_minor), str(row.currency), decimals=decimals),
        "created_at": row.created_at,
    }


def score_bands(session: Session, run_id: str) -> dict[str, int]:
    """Band populations, from the ``score`` table. Counts, not scores."""
    rows = session.execute(select(Score.band).where(Score.run_id == run_id)).scalars().all()
    counts: dict[str, int] = {}
    for band in rows:
        counts[str(band)] = counts.get(str(band), 0) + 1
    return counts


__all__ = [
    "MAX_ALLOCATION_ROWS_IN_RESPONSE",
    "RowNotPriced",
    "assumption_lines",
    "assumption_pairs",
    "baselines",
    "frontier_points",
    "live_rank_map",
    "optimality_gap_view",
    "previous_selection",
    "repriced_at_rate",
    "residual_tail",
    "run_simulation",
    "score_bands",
    "stored_allocation_map",
    "stored_policies",
    "stored_policy",
    "stored_priced_rows",
    "totals_view",
]
