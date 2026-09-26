"""Every economic assumption OXBOW makes, loaded, validated, and renderable verbatim.

Plan §11 makes ``config/economics.yaml`` the single source of every money figure,
and this module is the only reader of it. Two obligations shape the design, and
they are the reason the file is long rather than a dataclass with a loader:

1. **A money figure cannot be obtained without its assumptions.** §18 lists "a
   currency figure without its assumption line" as a rejection trigger, so the
   guarantee is structural, not stylistic: the only type that renders a
   major-unit amount is ``CurrencyFigure``, it holds an ``AssumptionBlock`` built
   from this module, and the sensitivity band over ``r`` is the figure's
   *coordinate system* — a single point estimate is not representable. The band
   is open-interval on ``r`` for the same reason: ``r = 0`` and ``r = 1`` make the
   ranking degenerate, so they are not admissible assumptions at all.
2. **Every key is owned by a named consumer.** The loader records each leaf it
   reads, and any leaf it did not read is a config error. An unread assumption is
   worse than no assumption — it looks like a knob, does nothing, and the next
   reader trusts it.

Validation follows ``oxbow.config``'s pattern exactly (fail loud at the boundary,
``ConfigError``, no silent default), and cross-checks the two keys
``config/pipeline.yaml`` also declares — the exposure window and hops, which the
graph layer reads, and the Monte Carlo seed. Duplicating a fact across two files
is only safe if somebody checks that they agree.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from oxbow.config import CONFIG_DIRNAME, ConfigError, find_repo_root, load_yaml
from oxbow.quant.money import CurrencyMismatchError, Money, QuantError, to_major_text

ECONOMICS_FILENAME: Final = "economics.yaml"
PIPELINE_FILENAME: Final = "pipeline.yaml"

# The band is three values by contract (plan §11: "the band always renders three
# values"), so its width is a constant rather than a length someone can change.
BAND_WIDTH: Final = 3

# The interval on the case page is described in copy as "90 %", so the configured
# quantile pair must span exactly that. A different width is a different claim.
MONTE_CARLO_INTERVAL_SPAN: Final = 0.90

DISCLAIMER_LINE: Final = (
    "Monetary figures are model estimates derived from the stated assumptions, not "
    "measured outcomes, and are not validated for operational use by any financial "
    "institution."
)


class MissingAssumptionsError(QuantError):
    """Raised when a caller asks for a currency figure without its block.

    This is the rejection trigger in exception form (plan §18), and it is a
    ``QuantError`` so a caller can catch it precisely instead of widening to
    ``Exception`` and swallowing a real fault (01 §G).
    """


class AssumptionBandError(QuantError):
    """Raised when a figure's ``r`` coordinates are not the configured band."""


@dataclass(frozen=True, slots=True)
class RecoveryAssumptions:
    """``r``: the share of exposure a timely intervention actually prevents."""

    rate: float
    band: tuple[float, float, float]
    lower_exclusive: float
    upper_exclusive: float

    def __post_init__(self) -> None:
        if len(self.band) != BAND_WIDTH:
            raise ConfigError(
                f"recovery.sensitivity_band must hold exactly {BAND_WIDTH} values, "
                f"got {len(self.band)}: {self.band}. Every headline figure is rendered "
                "over this band, so a shorter band silently removes the sensitivity."
            )
        ordered = tuple(sorted(self.band))
        if len(set(ordered)) != BAND_WIDTH:
            raise ConfigError(f"recovery.sensitivity_band has duplicates: {self.band}")
        if ordered != self.band:
            raise ConfigError(f"recovery.sensitivity_band must be ascending, got {self.band}")
        if self.band[1] != self.rate:
            raise ConfigError(
                f"recovery.rate {self.rate} is not the middle of its own sensitivity "
                f"band {self.band}. The band is the default's neighbourhood; a default "
                "outside it means one of the two was edited and the other was not."
            )
        for value in (self.rate, *self.band):
            if not self.lower_exclusive < value < self.upper_exclusive:
                raise ConfigError(
                    f"recovery rate {value} is outside the admissible open interval "
                    f"({self.lower_exclusive}, {self.upper_exclusive}). r = 0 prices "
                    "every exposure at nothing and r = 1 claims a perfect seizure; "
                    "both degenerate the ranking rather than merely flatter it."
                )

    def contains(self, value: float) -> bool:
        """Whether ``value`` is one of the three admissible band coordinates."""
        return value in self.band


@dataclass(frozen=True, slots=True)
class AnalystAssumptions:
    """The price of an analyst-minute, and the floor on review effort."""

    cost_per_hour_minor: int
    cost_per_minute_minor: int
    hours_per_period: int
    min_review_minutes: int

    def __post_init__(self) -> None:
        if self.cost_per_hour_minor % 60:
            raise ConfigError(
                f"analyst.cost_per_hour_minor {self.cost_per_hour_minor} does not divide "
                "exactly by 60. Per-minute cost is derived from per-hour, and a "
                "non-integer result would put a rounding rule underneath every c_i."
            )
        if self.cost_per_hour_minor // 60 != self.cost_per_minute_minor:
            raise ConfigError(
                f"analyst.cost_per_minute_minor {self.cost_per_minute_minor} disagrees "
                f"with cost_per_hour_minor / 60 = {self.cost_per_hour_minor // 60}"
            )
        if self.cost_per_minute_minor < 1:
            raise ConfigError(
                "analyst.cost_per_minute_minor must be >= 1: a free review makes every "
                "alert profitable and the capacity constraint meaningless."
            )
        if self.min_review_minutes < 1:
            raise ConfigError(
                f"analyst.min_review_minutes is {self.min_review_minutes}. EV density is "
                "EV_i / m_i, so a zero-minute floor divides by zero at runtime; a floor "
                "of zero is rejected at config load instead (plan §11 "
                "test_ev_density_no_zero_divide)."
            )
        if self.hours_per_period < 1:
            raise ConfigError("analyst.hours_per_period must be >= 1")


@dataclass(frozen=True, slots=True)
class ExposureAssumptions:
    """The recovery window and hop radius that define ``E_i``."""

    window_hours: int
    downstream_hops: int

    def __post_init__(self) -> None:
        if self.window_hours < 1:
            raise ConfigError("exposure.window_hours must be >= 1")
        if self.downstream_hops < 0:
            raise ConfigError("exposure.downstream_hops must be >= 0")


@dataclass(frozen=True, slots=True)
class CapacitySweep:
    """The capacity range the efficient frontier is drawn over."""

    min_minutes: int
    max_minutes: int
    points: int

    def __post_init__(self) -> None:
        if self.min_minutes < 0:
            raise ConfigError(
                "capacity.sweep.min_minutes cannot be negative; zero is allowed and is "
                "the documented no-capacity end of the frontier."
            )
        if self.max_minutes <= self.min_minutes:
            raise ConfigError("capacity.sweep.max_minutes must exceed min_minutes")
        if self.points < 2:
            raise ConfigError("capacity.sweep.points must be >= 2 to draw a curve")


@dataclass(frozen=True, slots=True)
class CapacityAssumptions:
    """The review budget ``B`` for one period, and where the operating point sits."""

    review_minutes_per_period: int
    default_alerts_reviewed: int
    sweep: CapacitySweep

    def __post_init__(self) -> None:
        if self.review_minutes_per_period < 0:
            raise ConfigError("capacity.review_minutes_per_period cannot be negative")
        if self.default_alerts_reviewed < 1:
            raise ConfigError("capacity.default_alerts_reviewed must be >= 1")
        if not self.sweep.min_minutes <= self.review_minutes_per_period <= self.sweep.max_minutes:
            raise ConfigError(
                f"capacity.review_minutes_per_period {self.review_minutes_per_period} is "
                f"outside the sweep range [{self.sweep.min_minutes}, "
                f"{self.sweep.max_minutes}]: the operating point must be on the curve "
                "the UI draws, not off the end of it."
            )


@dataclass(frozen=True, slots=True)
class SolverAssumptions:
    """Latency budget, exact-solve deadline, and the agreement tolerance."""

    greedy_budget_ms: int
    cpsat_deadline_ms: int
    cpsat_workers: int
    agreement_tolerance_ratio: float

    def __post_init__(self) -> None:
        if self.greedy_budget_ms < 1:
            raise ConfigError("solver.greedy_budget_ms must be >= 1")
        if self.cpsat_deadline_ms < 1:
            raise ConfigError("solver.cpsat_deadline_ms must be >= 1")
        if self.cpsat_workers != 1:
            raise ConfigError(
                f"solver.cpsat_workers is {self.cpsat_workers}, must be 1. A portfolio "
                "solve returns whichever incumbent races home first, so two runs of "
                "`make verify-determinism` could not be compared (01 A rule 4)."
            )
        if not 0.0 < self.agreement_tolerance_ratio < 1.0:
            raise ConfigError(
                "solver.agreement_tolerance_ratio must lie in (0, 1); it is the "
                "greedy-vs-exact agreement gate, stated as a fraction of total EV."
            )


@dataclass(frozen=True, slots=True)
class MonteCarloAssumptions:
    """Run count, depth cap, seed and interval width for exposure propagation."""

    runs: int
    max_depth: int
    seed: int
    lower_quantile: float
    upper_quantile: float

    def __post_init__(self) -> None:
        if self.runs < 100:
            raise ConfigError(
                f"monte_carlo.runs is {self.runs}. Below a hundred draws the quantile "
                "is a single observation and the interval is theatre."
            )
        if self.max_depth < 1:
            raise ConfigError("monte_carlo.max_depth must be >= 1")
        if not 0.0 < self.lower_quantile < self.upper_quantile < 1.0:
            raise ConfigError(
                f"monte_carlo.interval {self.lower_quantile}..{self.upper_quantile} must "
                "be an increasing pair inside (0, 1)"
            )
        span = self.upper_quantile - self.lower_quantile
        if abs(span - MONTE_CARLO_INTERVAL_SPAN) > 1e-12:
            raise ConfigError(
                f"monte_carlo.interval spans {span}, must span "
                f"{MONTE_CARLO_INTERVAL_SPAN}: the case page copy says '90 % interval', "
                "and a different width is a different claim."
            )


@dataclass(frozen=True, slots=True)
class FourEyesPolicy:
    """Exposure above which a second reviewer must confirm (02 §F, wired in P7)."""

    threshold_exposure_minor: int

    def __post_init__(self) -> None:
        if self.threshold_exposure_minor < 0:
            raise ConfigError("four_eyes.threshold_exposure_minor cannot be negative")


@dataclass(frozen=True, slots=True)
class TailRiskLevels:
    """VaR / ES confidence levels for the residual, unreviewed exposure (P6)."""

    var_alpha: float
    es_alpha: float

    def __post_init__(self) -> None:
        if not 0.0 < self.var_alpha < self.es_alpha < 1.0:
            raise ConfigError(
                f"tail_risk alphas must satisfy 0 < var {self.var_alpha} < es "
                f"{self.es_alpha} < 1: expected shortfall is the worse tail."
            )


@dataclass(frozen=True, slots=True)
class Economics:
    """The validated assumption set. Frozen: read once at the boundary (00 §G).

    ``seed`` and ``order_columns`` are carried from ``pipeline.yaml`` so a quant
    artefact states the same determinism contract as the rest of the run rather
    than trusting the caller to line the two files up.
    """

    source_path: Path
    currency: str
    minor_units_per_major: int
    recovery: RecoveryAssumptions
    analyst: AnalystAssumptions
    friction_cost: Money
    review_minutes_by_alert_class: Mapping[str, int]
    exposure: ExposureAssumptions
    capacity: CapacityAssumptions
    solver: SolverAssumptions
    monte_carlo: MonteCarloAssumptions
    four_eyes: FourEyesPolicy
    tail_risk: TailRiskLevels
    seed: int
    order_columns: tuple[str, str]

    @property
    def alert_classes(self) -> tuple[str, ...]:
        """Configured alert classes in ascending name order (deterministic)."""
        return tuple(sorted(self.review_minutes_by_alert_class))

    def minutes_for(self, alert_class: str) -> int:
        """``m_i`` for one alert class, failing loud on an unknown class."""
        try:
            return self.review_minutes_by_alert_class[alert_class]
        except KeyError as exc:
            raise ConfigError(
                f"no review minutes configured for alert class {alert_class!r}; configured "
                f"classes are {list(self.alert_classes)}. Guessing a default here would "
                "price an alert class nobody reviewed."
            ) from exc

    def sweep_capacities(self) -> tuple[int, ...]:
        """Integer analyst-minute capacities to sweep, ascending and deduplicated.

        The configured operating point is forced into the grid so the frontier
        always contains the point the UI marks; a curve that does not pass through
        the operating point cannot support the marker on it.
        """
        sweep = self.capacity.sweep
        span = sweep.max_minutes - sweep.min_minutes
        step = span / (sweep.points - 1)
        grid = {sweep.min_minutes + round(index * step) for index in range(sweep.points)}
        grid.add(sweep.min_minutes)
        grid.add(sweep.max_minutes)
        grid.add(self.capacity.review_minutes_per_period)
        return tuple(sorted(grid))

    def money(self, minor: int) -> Money:
        """Money in the assumption currency — the only currency this file prices in."""
        return Money(minor, self.currency)


def _leaf_paths(node: Mapping[str, object] | Sequence[object] | object, prefix: str) -> list[str]:
    """Every scalar leaf under ``node``, as a dotted path."""
    if isinstance(node, Mapping):
        return [
            leaf
            for key in node
            for leaf in _leaf_paths(node[key], f"{prefix}.{key}" if prefix else str(key))
        ]
    if isinstance(node, list):
        return [
            leaf
            for index, item in enumerate(node)
            for leaf in _leaf_paths(item, f"{prefix}[{index}]")
        ]
    return [prefix]


class _AssumptionFile:
    """A YAML mapping that records which leaves the loader actually read.

    ``ConfigError`` on an unread key is what keeps the comment in
    ``economics.yaml`` that names a consumer honest: a key nobody reads cannot be
    added without also adding the consumer that reads it.
    """

    def __init__(self, raw: Mapping[str, object], name: str) -> None:
        self._raw = raw
        self._name = name
        self._consumed: set[str] = set()

    def _resolve(self, path: str) -> object:
        node: object = self._raw
        for part in path.split("."):
            key, _, index_text = part.partition("[")
            if not isinstance(node, Mapping) or key not in node:
                raise ConfigError(f"{self._name} is missing required key {path!r}")
            value = node[key]
            if index_text:
                index = int(index_text.rstrip("]"))
                if not isinstance(value, list) or index >= len(value):
                    raise ConfigError(f"{self._name}::{path} is not a scalar leaf")
                node = value[index]
            else:
                node = value
        self._consumed.add(path)
        return node

    def integer(self, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
        value = self._resolve(path)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(
                f"{self._name}::{path} must be an integer (money and counts are integer "
                f"minor units or whole minutes), got {type(value).__name__} {value!r}"
            )
        self._check_range(path, value, minimum, maximum)
        return value

    def number(
        self, path: str, *, minimum: float | None = None, maximum: float | None = None
    ) -> float:
        value = self._resolve(path)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(
                f"{self._name}::{path} must be a real-valued ratio, got "
                f"{type(value).__name__} {value!r}"
            )
        result = float(value)
        self._check_range(path, result, minimum, maximum)
        return result

    def text(self, path: str) -> str:
        value = self._resolve(path)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{self._name}::{path} must be a non-empty string")
        return value

    def scalars(self, path: str, *, expected: int | None = None) -> tuple[float | int | str, ...]:
        """A list of scalars, with each element recorded as consumed.

        ``expected`` is checked here rather than by the caller unpacking the
        result, because a wrong-length list must be a boundary failure naming the
        key, not a ``ValueError`` about tuple unpacking from inside a loader.
        """
        value = self._resolve(path)
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{self._name}::{path} must be a non-empty list")
        if expected is not None and len(value) != expected:
            raise ConfigError(
                f"{self._name}::{path} must hold exactly {expected} values, got {len(value)}: "
                f"{value}"
            )
        leaves: list[float | int | str] = []
        for index, item in enumerate(value):
            if isinstance(item, bool) or not isinstance(item, int | float | str):
                raise ConfigError(f"{self._name}::{path}[{index}] must be a scalar")
            self._consumed.add(f"{path}[{index}]")
            leaves.append(item)
        return tuple(leaves)

    def mapping(self, path: str) -> dict[str, int]:
        """A string-keyed mapping of integers (alert class -> review minutes)."""
        value = self._resolve(path)
        if not isinstance(value, Mapping) or not value:
            raise ConfigError(f"{self._name}::{path} must be a non-empty mapping")
        result: dict[str, int] = {}
        for key, item in value.items():
            if isinstance(item, bool) or not isinstance(item, int):
                raise ConfigError(f"{self._name}::{path}.{key} must be an integer count of minutes")
            self._consumed.add(f"{path}.{key}")
            result[str(key)] = item
        return result

    def unconsumed(self) -> list[str]:
        """Leaf paths present in the file but never read by a consumer."""
        return sorted(set(_leaf_paths(self._raw, "")) - self._consumed)

    @staticmethod
    def _check_range(path: str, value: float, minimum: float | None, maximum: float | None) -> None:
        if minimum is not None and value < minimum:
            raise ConfigError(f"{path} is {value}, must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ConfigError(f"{path} is {value}, must be <= {maximum}")


def load_economics(root: Path | None = None) -> Economics:
    """Load and validate ``config/economics.yaml``, cross-checked against the pipeline.

    The cross-checks are the point of doing this at load rather than at first use:
    a window of 24 h in one file and 48 h in the other would produce exposures
    that the graph layer and the pricing layer disagree about, and every money
    figure downstream would look plausible while resting on two definitions.
    """
    repo_root = (root or find_repo_root()).resolve()
    econ_path = repo_root / CONFIG_DIRNAME / ECONOMICS_FILENAME
    raw = load_yaml(econ_path)
    source = _AssumptionFile(raw, ECONOMICS_FILENAME)

    currency = source.text("currency")
    if len(currency) != 3 or not currency.isalpha():
        raise ConfigError(f"currency must be a 3-letter ISO code, got {currency!r}")

    minor_units_per_major = source.integer("minor_units_per_major", minimum=1)
    cost_per_hour = source.integer("analyst.cost_per_hour_minor", minimum=1)
    analyst = AnalystAssumptions(
        cost_per_hour_minor=cost_per_hour,
        cost_per_minute_minor=source.integer("analyst.cost_per_minute_minor", minimum=1),
        hours_per_period=source.integer("analyst.hours_per_period", minimum=1),
        min_review_minutes=source.integer("analyst.min_review_minutes", minimum=0),
    )
    band_values = source.scalars("recovery.sensitivity_band", expected=BAND_WIDTH)
    lower_bound, upper_bound = source.scalars("recovery.bounds_exclusive", expected=2)
    band_low, band_mid, band_high = (float(value) for value in band_values)
    recovery = RecoveryAssumptions(
        rate=source.number("recovery.rate", minimum=0.0, maximum=1.0),
        band=(band_low, band_mid, band_high),
        lower_exclusive=float(lower_bound),
        upper_exclusive=float(upper_bound),
    )
    minutes_by_class = source.mapping("review_minutes_by_alert_class")
    floor = analyst.min_review_minutes
    too_easy = {name: value for name, value in minutes_by_class.items() if value < floor}
    if too_easy:
        raise ConfigError(
            f"review_minutes_by_alert_class {too_easy} falls below analyst.min_review_minutes "
            f"= {floor}. m_i is the EV-density denominator, so an unfloored class is a "
            "divide-by-zero waiting for a band name nobody added to the floor."
        )

    exposure = ExposureAssumptions(
        window_hours=source.integer("exposure.window_hours", minimum=1),
        downstream_hops=source.integer("exposure.downstream_hops", minimum=0),
    )
    capacity = CapacityAssumptions(
        review_minutes_per_period=source.integer("capacity.review_minutes_per_period", minimum=0),
        default_alerts_reviewed=source.integer("capacity.default_alerts_reviewed", minimum=1),
        sweep=CapacitySweep(
            min_minutes=source.integer("capacity.sweep.min_minutes", minimum=0),
            max_minutes=source.integer("capacity.sweep.max_minutes", minimum=1),
            points=source.integer("capacity.sweep.points", minimum=2),
        ),
    )
    solver = SolverAssumptions(
        greedy_budget_ms=source.integer("solver.greedy_budget_ms", minimum=1),
        cpsat_deadline_ms=source.integer("solver.cpsat_deadline_ms", minimum=1),
        cpsat_workers=source.integer("solver.cpsat_workers", minimum=1),
        agreement_tolerance_ratio=source.number("solver.agreement_tolerance_ratio"),
    )
    interval = source.scalars("monte_carlo.interval", expected=2)
    monte_carlo = MonteCarloAssumptions(
        runs=source.integer("monte_carlo.runs", minimum=1),
        max_depth=source.integer("monte_carlo.max_depth", minimum=1),
        seed=source.integer("monte_carlo.seed", minimum=0),
        lower_quantile=float(interval[0]),
        upper_quantile=float(interval[1]),
    )
    four_eyes = FourEyesPolicy(
        threshold_exposure_minor=source.integer("four_eyes.threshold_exposure_minor", minimum=0)
    )
    tail_risk = TailRiskLevels(
        var_alpha=source.number("tail_risk.var_alpha"),
        es_alpha=source.number("tail_risk.es_alpha"),
    )
    friction = Money(source.integer("friction_cost_minor", minimum=0), currency)

    pipeline = _AssumptionFile(
        load_yaml(repo_root / CONFIG_DIRNAME / PIPELINE_FILENAME), PIPELINE_FILENAME
    )
    seed = pipeline.integer("seed", minimum=0)
    order = pipeline.scalars("determinism.sort_keys", expected=2)
    first_order, second_order = (str(value) for value in order)
    _cross_check(exposure.window_hours, "exposure.window_hours", pipeline)
    _cross_check(exposure.downstream_hops, "exposure.downstream_hops", pipeline)
    if monte_carlo.seed != seed:
        raise ConfigError(
            f"monte_carlo.seed is {monte_carlo.seed} but the run seed in pipeline.yaml is "
            f"{seed}. Two seed sources is how an interval stops reproducing (01 A rule 4)."
        )

    economics = Economics(
        source_path=econ_path,
        currency=currency,
        minor_units_per_major=minor_units_per_major,
        recovery=recovery,
        analyst=analyst,
        friction_cost=friction,
        review_minutes_by_alert_class=MappingProxyType(minutes_by_class),
        exposure=exposure,
        capacity=capacity,
        solver=solver,
        monte_carlo=monte_carlo,
        four_eyes=four_eyes,
        tail_risk=tail_risk,
        seed=seed,
        order_columns=(first_order, second_order),
    )
    orphans = source.unconsumed()
    if orphans:
        raise ConfigError(
            f"{ECONOMICS_FILENAME} carries assumptions no consumer reads: {orphans}. "
            "Either wire the consumer or delete the key; an unread assumption still "
            "gets believed by the next reader."
        )
    return economics


def _cross_check(value: int, key: str, pipeline: _AssumptionFile) -> None:
    """Fail loud when the same fact is configured differently in two files."""
    pipeline_value = pipeline.integer(key, minimum=0)
    if value != pipeline_value:
        raise ConfigError(
            f"{ECONOMICS_FILENAME}::{key} = {value} but {PIPELINE_FILENAME}::{key} = "
            f"{pipeline_value}. Exposure would then mean two things at once, and every "
            "money figure would silently rest on whichever reader ran first."
        )


# --- the assumption block -------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssumptionBlock:
    """The verbatim text a currency figure must travel with.

    Constructed only by :func:`assumption_block`. It carries its own ``rates`` and
    currency so a figure can be checked against it without the loader in hand, and
    ``__post_init__`` refuses an empty body, which is what makes
    ``test_currency_requires_assumptions`` bite.
    """

    text: str
    rates: tuple[float, ...]
    currency: str
    per_major: int
    source_path: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise MissingAssumptionsError(
                "an assumption block cannot be empty: a currency figure with no "
                "assumptions is the rejection trigger in plan §18."
            )
        if len(self.rates) != BAND_WIDTH:
            raise AssumptionBandError(
                f"an assumption block must carry the {BAND_WIDTH}-value recovery band, "
                f"got {self.rates}"
            )
        if self.per_major < 1:
            raise MissingAssumptionsError(
                f"minor_units_per_major must be >= 1, got {self.per_major}"
            )
        if self.source_path not in self.text:
            raise MissingAssumptionsError(
                f"the assumption block must name the file it came from ({self.source_path!r} "
                "is missing from its text), so a reader can go and change it"
            )


def _format_rate(value: float) -> str:
    return f"{value:.2f}"


def assumption_block(economics: Economics) -> AssumptionBlock:
    """Render the assumption set verbatim, as one string with newlines.

    The wording is deliberately fixed: the same text is what the UI renders under a
    currency figure, what the exported packet carries, and what the tests assert,
    so a figure can never meet a shorter version of its own assumptions in one
    surface than another.
    """
    recovery = economics.recovery
    analyst = economics.analyst
    band = " / ".join(_format_rate(value) for value in recovery.band)
    classes = ", ".join(
        f"{name}={economics.minutes_for(name)} min" for name in economics.alert_classes
    )
    per_minute = to_major_text(
        analyst.cost_per_minute_minor, economics.currency, economics.minor_units_per_major
    )
    per_hour = to_major_text(
        analyst.cost_per_hour_minor, economics.currency, economics.minor_units_per_major
    )
    friction = to_major_text(
        economics.friction_cost.minor, economics.currency, economics.minor_units_per_major
    )
    capacity_minutes = economics.capacity.review_minutes_per_period
    source_name = ECONOMICS_FILENAME
    text = "\n".join(
        (
            f"Assumptions behind this figure - config/{source_name}, illustrative model "
            "estimates, not measured outcomes:",
            f"  recovery rate r swept over {band} (default {_format_rate(recovery.rate)}; "
            f"admissible open interval ({_format_rate(recovery.lower_exclusive)}, "
            f"{_format_rate(recovery.upper_exclusive)}))",
            f"  exposure at risk E_i: value leaving the account and its "
            f"{economics.exposure.downstream_hops}-hop downstream within "
            f"{economics.exposure.window_hours} h of the first triggering event, capped at "
            "inflow observed in the same window",
            f"  review cost c_i at {per_minute} per analyst-minute ({per_hour} per hour fully "
            f"loaded); minutes per alert class {classes}; minimum {analyst.min_review_minutes} min",
            f"  friction cost f of wrongly touching a legitimate customer: {friction} per alert",
            f"  capacity B = {capacity_minutes:,} analyst-minutes per period "
            f"(default {economics.capacity.default_alerts_reviewed:,} alerts reviewed)",
            "  EV_i = p_i * E_i * r - c_i - (1 - p_i) * f, ranked by EV density EV_i / m_i",
            f"  {DISCLAIMER_LINE}",
        )
    )
    return AssumptionBlock(
        text=text,
        rates=recovery.band,
        currency=economics.currency,
        per_major=economics.minor_units_per_major,
        source_path=f"config/{source_name}",
    )


# --- currency figures -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrencyFigure:
    """One labelled money figure, over the recovery band, with its assumptions.

    There is no constructor path that yields a single number: the value is a
    tuple keyed by the band's three ``r`` coordinates, the block is required at
    construction, and ``block`` carries the currency and the minor-unit divisor.
    That is the enforcement of "a caller cannot get a money figure without the
    band" — plan §11 calls the band part of the definition, not a footnote.
    """

    label: str
    rows: tuple[tuple[float, Money], ...]
    block: AssumptionBlock
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise MissingAssumptionsError("a currency figure needs a label to mean anything")
        rates = tuple(rate for rate, _ in self.rows)
        if rates != tuple(sorted(rates)):
            raise AssumptionBandError(f"figure rows must be ordered by r, got {rates}")
        if len(rates) != BAND_WIDTH:
            raise AssumptionBandError(
                f"a currency figure must carry all {BAND_WIDTH} recovery-band values, "
                f"got {rates}. A point estimate hides the assumption that decides it."
            )
        expected = tuple(sorted(self.block.rates))
        if rates != expected:
            raise AssumptionBandError(
                f"figure coordinates {rates} are not the configured band {expected}"
            )
        for _, amount in self.rows:
            if amount.currency != self.block.currency:
                raise CurrencyMismatchError(
                    f"{amount.currency} figure under {self.block.currency} assumptions: "
                    "costs and exposure must be in one currency, and there is no implicit FX."
                )

    @property
    def default(self) -> Money:
        """The figure at the configured default ``r`` — never rendered alone.

        Exposed for arithmetic (a table row, a comparison), and the rendered form
        always prints all three coordinates, so a caller cannot accidentally show
        this one by itself.
        """
        return self.rows[BAND_WIDTH // 2][1]

    def value_at(self, rate: float) -> Money:
        """The figure at one band coordinate."""
        for candidate, amount in self.rows:
            if candidate == rate:
                return amount
        raise AssumptionBandError(
            f"{rate} is not a coordinate of this figure; this one is defined on "
            f"{tuple(rate for rate, _ in self.rows)}"
        )

    def render(self) -> str:
        """Figure and assumptions as one string. The only producer of this text."""
        amounts = " | ".join(
            f"{to_major_text(amount.minor, amount.currency, self.block.per_major)} "
            f"at r={_format_rate(rate)}"
            for rate, amount in self.rows
        )
        parts = [f"{self.label}: {amounts}", *self.provenance, self.block.text]
        return "\n".join(parts)

    def __str__(self) -> str:
        return self.render()


def currency_figure(
    label: str,
    values: Mapping[float, Money],
    block: AssumptionBlock | None,
    *,
    provenance: Sequence[str] = (),
) -> CurrencyFigure:
    """Build a figure from ``{r: Money}`` and a block, refusing a missing block.

    ``block`` is typed ``AssumptionBlock | None`` with no default precisely so the
    failure is a runtime error naming the rule rather than a ``TypeError`` about a
    missing argument: the tempting bug is passing ``None`` because the caller has
    no economics file, and that is the one thing this layer must not do.
    """
    if block is None:
        raise MissingAssumptionsError(
            f"refusing to render {label!r}: a currency figure without its assumption block "
            "is a plan §18 rejection trigger. Load config/economics.yaml, call "
            "assumption_block(), and pass the result."
        )
    expected = tuple(sorted(block.rates))
    given = tuple(sorted(values))
    if given != expected:
        raise AssumptionBandError(
            f"{label!r} must be valued over the whole recovery band {expected}, got {given}"
        )
    return CurrencyFigure(
        label=label,
        rows=tuple((rate, values[rate]) for rate in expected),
        block=block,
        provenance=tuple(provenance),
    )


def currency_figure_over_band(
    label: str,
    block: AssumptionBlock,
    value_at: Callable[[float], Money],
    *,
    provenance: Sequence[str] = (),
) -> CurrencyFigure:
    """Evaluate ``value_at`` once per band coordinate.

    The intended entry point: the caller supplies the economics-dependent
    computation, the block supplies the coordinates, and there is no way to ask
    for one number.
    """
    values = {rate: value_at(rate) for rate in block.rates}
    return currency_figure(label, values, block, provenance=provenance)


__all__ = [
    "BAND_WIDTH",
    "DISCLAIMER_LINE",
    "ECONOMICS_FILENAME",
    "AnalystAssumptions",
    "AssumptionBandError",
    "AssumptionBlock",
    "CapacityAssumptions",
    "CapacitySweep",
    "CurrencyFigure",
    "Economics",
    "ExposureAssumptions",
    "FourEyesPolicy",
    "MissingAssumptionsError",
    "MonteCarloAssumptions",
    "RecoveryAssumptions",
    "SolverAssumptions",
    "TailRiskLevels",
    "assumption_block",
    "currency_figure",
    "currency_figure_over_band",
    "load_economics",
]
