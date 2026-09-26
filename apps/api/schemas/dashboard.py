"""Command-dashboard shapes: the top strip in currency, not in counts.

Plan §14 opens the dashboard with four money figures and one requirement that is
easy to miss — each of them carries its assumption line, and the loss-avoided figure
carries its ``r`` band *as part of the figure*. A single currency number with no
band is a rejection trigger (plan §18), so the model here cannot be constructed
without the band: ``loss_avoided`` is a :class:`BandedMoney`, and the band
coordinates are the configured three, not three numbers someone typed.

The dashboard is also where "no route renders a value that is not in the API
response" is easiest to break, so each tile names the table its number came from in
``source``. Nothing on this screen is composed in the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.common import AssumptionLine, Money

Band = Literal["A", "B", "C", "D", "E"]


class BandedMoney(BaseModel):
    """A point estimate plus the sensitivity band it is only meaningful with.

    ``over_band`` holds the figure re-priced at each configured recovery rate. It
    is stored money re-expressed, not a forecast: the label says which assumption
    moved, and ``basis`` names the config key that supplied the band.
    """

    model_config = ConfigDict(extra="forbid")

    point: Money
    over_band: list[Money] = Field(min_length=3, max_length=3)
    band_rates: list[float] = Field(min_length=3, max_length=3)
    basis: str = Field(
        description="Which config key the band came from, e.g. recovery.sensitivity_band."
    )
    note: str


class ModelChip(BaseModel):
    """One model-quality statistic with its delta against the stated baseline."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: float | None = None
    unit: str
    corpus: str | None = None
    baseline: float | None = None
    delta: float | None = None
    delta_note: str | None = None
    note: str | None = None
    undefined: bool = Field(
        default=False,
        description="True when the statistic has no value for this fold — reported as "
        "undefined with its alert count, never as zero (plan §12).",
    )


class BandDistribution(BaseModel):
    """How many accounts landed in each band, with the population behind it."""

    model_config = ConfigDict(extra="forbid")

    band: Band
    count: int
    observed_rate: float | None = None
    n_calibration: int | None = None
    action: str | None = None
    review_minutes: float | None = None


class PatternFeedItem(BaseModel):
    """The latest typed evidence, with the run and account that produced it."""

    model_config = ConfigDict(extra="forbid")

    occurred_at: datetime
    kind: str
    label: str
    account_key: str
    run_id: str
    rule_id: str | None = None
    typology: str | None = None
    case_id: str | None = None


class DashboardResponse(BaseModel):
    """Everything the command dashboard renders, in one response."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    run_state: str
    as_of: datetime
    currency: str
    expected_loss_avoided: BandedMoney
    benefit_per_analyst_hour: Money
    residual_exposure_es975: Money
    alerts_generated: int
    alerts_reviewed: int
    alerts_below_cutoff: int
    high_risk_networks: int
    high_risk_networks_basis: str = Field(
        description="How a 'network' was counted (community above a band threshold), so the "
        "tile can be checked rather than admired."
    )
    capacity_minutes: int
    cutoff_rank: int | None = None
    model_chips: list[ModelChip] = Field(default_factory=list)
    band_distribution: list[BandDistribution] = Field(default_factory=list)
    cumulative_benefit_curve: list[dict[str, Any]] = Field(default_factory=list)
    latest_patterns: list[PatternFeedItem] = Field(default_factory=list)
    dataset_badge: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[AssumptionLine] = Field(default_factory=list)


__all__ = [
    "BandDistribution",
    "BandedMoney",
    "DashboardResponse",
    "ModelChip",
    "PatternFeedItem",
]
