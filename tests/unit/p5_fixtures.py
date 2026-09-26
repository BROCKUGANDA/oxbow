"""Hand-scale economic assumptions shared by the P5 tests.

The gate requires a fixture whose arithmetic can be checked on paper, and
``config/economics.yaml`` deliberately holds real-scale numbers (a 15,000-minor-unit
analyst minute, a 2,500,000-minor-unit friction cost). Multiplying those by hand
produces a test that asserts one number against another number, which proves
nothing about either.

So the tests that check arithmetic build this economy instead: every quantity is a
small integer, ``r = 0.5`` halves cleanly, and the per-minute price divides the
per-hour price exactly, which is the same constraint the loader enforces on the
real file. Tests that check *the product's* numbers — the latency budget, the
Monte Carlo settings, the sweep grid — read ``config/economics.yaml`` directly and
say so.
"""

from __future__ import annotations

from pathlib import Path

from oxbow.quant.economics import (
    AnalystAssumptions,
    CapacityAssumptions,
    CapacitySweep,
    Economics,
    ExposureAssumptions,
    FourEyesPolicy,
    MonteCarloAssumptions,
    RecoveryAssumptions,
    SolverAssumptions,
    TailRiskLevels,
)
from oxbow.quant.money import Money

CURRENCY = "UGX"
PER_MAJOR = 100
COST_PER_MINUTE = 100
FRICTION = 400
DEFAULT_RATE = 0.5
BAND = (0.2, 0.5, 0.8)

# Alert classes chosen so that each hand-computed fixture can ask for the minutes
# it needs by name. The mapping is a fixture, not a fact about the product: the
# product's classes and minutes come from config/economics.yaml.
MINUTES_BY_CLASS = {"A": 5, "B": 6, "C": 9, "D": 10, "E": 15, "F": 20}


def hand_economics(**overrides: object) -> Economics:
    """The paper-checkable economy, with named overrides for one-off variants."""
    values: dict[str, object] = {
        "source_path": Path("config/economics.yaml"),
        "currency": CURRENCY,
        "minor_units_per_major": PER_MAJOR,
        "recovery": RecoveryAssumptions(
            rate=DEFAULT_RATE, band=BAND, lower_exclusive=0.0, upper_exclusive=1.0
        ),
        "analyst": AnalystAssumptions(
            cost_per_hour_minor=COST_PER_MINUTE * 60,
            cost_per_minute_minor=COST_PER_MINUTE,
            hours_per_period=1,
            min_review_minutes=5,
        ),
        "friction_cost": Money(FRICTION, CURRENCY),
        "review_minutes_by_alert_class": dict(MINUTES_BY_CLASS),
        "exposure": ExposureAssumptions(window_hours=24, downstream_hops=1),
        "capacity": CapacityAssumptions(
            review_minutes_per_period=25,
            default_alerts_reviewed=3,
            sweep=CapacitySweep(min_minutes=0, max_minutes=30, points=7),
        ),
        "solver": SolverAssumptions(
            greedy_budget_ms=200,
            cpsat_deadline_ms=1_000,
            cpsat_workers=1,
            agreement_tolerance_ratio=0.02,
        ),
        "monte_carlo": MonteCarloAssumptions(
            runs=2_000,
            max_depth=4,
            seed=1337,
            lower_quantile=0.05,
            upper_quantile=0.95,
        ),
        "four_eyes": FourEyesPolicy(threshold_exposure_minor=1_000_000_000),
        "tail_risk": TailRiskLevels(var_alpha=0.95, es_alpha=0.975),
        "seed": 1337,
        "order_columns": ("ts_utc", "txn_id"),
    }
    values.update(overrides)
    return Economics(**values)  # type: ignore[arg-type]


def money(minor: int, currency: str = CURRENCY) -> Money:
    """Shorthand for the fixture currency."""
    return Money(minor, currency)
