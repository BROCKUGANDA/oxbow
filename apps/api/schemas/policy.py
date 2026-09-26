"""Policy, allocation and simulator shapes — the queue's economics, on the wire.

Two rules shape this file more than any other:

* **the cutoff line is server state.** ``rank``, ``selected`` and ``beyond_capacity``
  come from the stored allocation the policy produced, and ``cutoff_rank`` rides on
  every row, because the capacity line in the UI has to be the same fact the ranking
  was built from. If the client derived it, the line and the order could disagree,
  and the disagreement is exactly the failure 02 §B seam 5 exists to prevent.
* **the simulator returns a real allocation.** Plan §14 forbids precomputed theatre:
  ``POST /api/policy/simulate`` runs the P5 allocator over the stored priced queue and
  returns what it decided, in which order, under which assumptions, in how many
  milliseconds — plus the greedy-vs-exact gap and the degraded label when the exact
  solver fell back. Nothing here is a curve fitted to a slider position.

Money is minor units plus currency throughout (DEV-005). ``ev_density`` is a *ratio*
of minor units per analyst-minute, which is why it is a float and why no money field
in this file is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.schemas.common import AssumptionLine, Money

SolverChoice = Literal["greedy_ev_density", "cpsat_exact"]
AllocationStateName = Literal["selected", "nothing_alerted", "nothing_pays", "no_capacity"]

# The simulator's guard rails, stated here so the boundary refuses a nonsense
# request with a 422 rather than feeding the allocator a value it would
# misinterpret. Capacity 0 is legitimate and documented ("no capacity at all");
# a negative budget is not.
CAPACITY_MINUTES_MAX = 1_000_000
ALLOCATION_ROWS_MAX = 500


class PolicyView(BaseModel):
    """One stored policy, with the solver that produced its allocation."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    policy_id: str
    name: str
    active: bool
    capacity_minutes: int
    recovery_rate: float
    recovery_sensitivity_band: list[float]
    analyst_cost_per_hour: Money
    min_review_minutes: float
    friction_cost: Money
    four_eyes_threshold_exposure: Money
    review_minutes_by_band: dict[str, float]
    currency: str
    solver: str
    degraded: bool
    degraded_reason: str | None
    solve_ms: int | None
    optimality_gap: Money | None
    created_at: datetime


class AllocationRow(BaseModel):
    """One account's position in the queue under one policy."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    account_key: str
    rank: int
    selected: bool
    beyond_capacity: bool
    expected_value: Money
    exposure: Money
    analyst_minutes: float
    ev_density: float = Field(
        description="Minor units of EV per analyst-minute — the ranking key, a ratio, "
        "and deliberately not a money field."
    )
    case_id: str | None = None
    case_status: str | None = None


class PolicySummaryView(BaseModel):
    """What one policy is worth over a period, including its tail.

    The tail figures travel with the mean because plan §12 requires policies to be
    compared by tail reduction: a policy that lowers average loss while fattening
    VaR95 is a bad policy, and a response that reported only the mean would let the
    UI make that mistake look like a win.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    policy_id: str
    currency: str
    capacity_minutes: int
    selected_count: int
    candidate_count: int
    loss_avoided: Money
    analyst_cost: Money
    friction_cost: Money
    net_benefit: Money
    benefit_per_analyst_hour: Money
    max_drawdown: Money
    zero_drawdown: bool
    zero_drawdown_note: str | None = Field(
        default=None,
        description="Present when drawdown is zero: the plan requires that case to be "
        "stated rather than left as an empty chart.",
    )
    var_alpha: float
    var95: Money
    es_alpha: float
    es975: Money
    monte_carlo_runs: int
    monte_carlo_seed: int
    alerts_per_10k_accounts: float
    risk_adjusted_benefit: float
    risk_adjusted_benefit_note: str = Field(
        description="Carried verbatim from the stored row: the label that says this is "
        "NOT a Sharpe ratio, with the formula (plan §12, test_label_not_sharpe)."
    )
    cumulative_curve: list[dict[str, Any]] = Field(default_factory=list)
    frontier: list[dict[str, Any]] = Field(default_factory=list)
    baselines: dict[str, Any] = Field(default_factory=dict)
    assumptions: dict[str, Any] = Field(default_factory=dict)


class SimulationRequest(BaseModel):
    """The simulator's sliders. Every field traces to a ``config/economics.yaml`` key.

    Recovery rate is not a free float: it must be one of the three configured band
    coordinates, because plan §11 renders money *over the band* and an off-band
    rate would produce a figure whose sensitivity nobody stated.
    """

    model_config = ConfigDict(extra="forbid")

    capacity_minutes: int = Field(ge=0, le=CAPACITY_MINUTES_MAX)
    recovery_rate: float | None = Field(
        default=None,
        description="Must be one of recovery.sensitivity_band. Omitted means the "
        "configured default rate.",
    )
    solver: SolverChoice = "greedy_ev_density"
    cpsat_deadline_ms: int | None = Field(default=None, ge=1, le=60_000)
    include_baselines: bool = True

    @model_validator(mode="after")
    def _deadline_needs_the_solver(self) -> SimulationRequest:
        if self.cpsat_deadline_ms is not None and self.solver != "cpsat_exact":
            raise ValueError(
                "cpsat_deadline_ms only means something to the exact solver; the greedy "
                "policy is a single pass with no internal budget to miss"
            )
        return self


class OptimalityGapView(BaseModel):
    """Greedy against exact, in money, with the denominator named.

    ``gap_ratio`` is relative to the *exact* total, because that is what the
    approximation is measured against; over the greedy total it would flatter the
    approximation by exactly its own shortfall.
    """

    model_config = ConfigDict(extra="forbid")

    approximate: Money
    exact: Money
    gap: Money
    gap_ratio: float
    within_tolerance: bool
    tolerance_ratio: float
    exact_proven: bool
    as_percent: str


class FrontierPoint(BaseModel):
    """One capacity on the efficient frontier.

    ``current_point`` marks the configured operating capacity so the UI can place a
    marker on the curve it was actually drawn from rather than eyeballing a
    position along the x-axis.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    capacity_minutes: int
    selected_count: int
    minutes_used: int
    net_benefit: Money
    loss_avoided: Money
    max_drawdown: Money
    var95: Money
    es975: Money
    current_point: bool = False


class SimulationResult(BaseModel):
    """One real re-allocation, with what it changed and how long it took."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    base_policy_id: str
    simulation_id: str | None = Field(
        default=None,
        description="Set when the allocation was recorded in ``policy_simulation`` so the "
        "answer survives a tab crash; null when the deployment cannot write there.",
    )
    request: dict[str, Any]
    solver: str
    allocator_label: str = Field(
        description="The sentence the UI renders next to the result, including the word "
        "DEGRADED when the exact solve fell back."
    )
    state: AllocationStateName
    message: str
    capacity_minutes: int
    minutes_used: int
    candidate_count: int
    selected_count: int
    cutoff_rank: int | None = Field(
        default=None,
        description="Last rank inside the budget. Null only when nothing was selected, "
        "which is a documented outcome and not the same claim as an empty query.",
    )
    allocation: list[AllocationRow]
    entered: list[str] = Field(default_factory=list)
    left: list[str] = Field(default_factory=list)
    totals: PolicySummaryView | None = None
    baselines: dict[str, Any] = Field(default_factory=dict)
    frontier: list[FrontierPoint] = Field(default_factory=list)
    optimality_gap: OptimalityGapView | None = None
    degraded: bool
    degraded_reason: str | None
    solve_ms: int
    currency: str
    assumptions: list[AssumptionLine]
    wrong_touch_expected: float = Field(
        description="Expected count of legitimate customers the reviewed set disturbs. "
        "A count, not money — the term that keeps the queue from being 'review everything'."
    )
    forgone_expected_value: Money


class ActivePolicyResponse(BaseModel):
    """The queue's economic frame: policy, summary and the cutoff line."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    policy: PolicyView
    summary: PolicySummaryView | None
    allocation: list[AllocationRow]
    cutoff_rank: int | None
    capacity_minutes: int
    allocation_source: str = Field(
        description="Where the ranks came from: 'stored' (the run's committed allocation) "
        "or 'reallocated' (computed now through the allocator because the run wrote none). "
        "A queue cannot be silent about which of the two a judge is looking at."
    )
    policies: list[PolicyView] = Field(default_factory=list)


__all__ = [
    "ALLOCATION_ROWS_MAX",
    "CAPACITY_MINUTES_MAX",
    "ActivePolicyResponse",
    "AllocationRow",
    "AllocationStateName",
    "FrontierPoint",
    "OptimalityGapView",
    "PolicySummaryView",
    "PolicyView",
    "SimulationRequest",
    "SimulationResult",
    "SolverChoice",
]
