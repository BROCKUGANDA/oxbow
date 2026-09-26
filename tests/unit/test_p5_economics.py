"""P5 economics: the assumption loader, the recovery band, and the renderer.

Plan §11 makes ``config/economics.yaml`` the only source of any number the product
asserts, and §18 makes "a currency figure without its assumption line" a rejection
trigger rather than a style complaint. Both are asserted here as executable
properties: the loader refuses an unowned or contradictory key, the band cannot be
skipped, and the renderer refuses to print money.

Where a test needs a value from the real config file it reads the real config file,
so the numbers asserted below are the numbers the product would ship.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from oxbow.config import ConfigError
from oxbow.quant.economics import (
    AssumptionBandError,
    AssumptionBlock,
    Economics,
    MissingAssumptionsError,
    RecoveryAssumptions,
    assumption_block,
    currency_figure,
    currency_figure_over_band,
    load_economics,
)
from oxbow.quant.exposure import CrossCurrencyExposureError, FlowEdge, exposure_at_risk
from oxbow.quant.money import (
    CurrencyMismatchError,
    Money,
    QuantError,
    ratio_to_micro,
    scale_div,
    to_major_text,
)
from tests.unit.p5_fixtures import hand_economics

REPO_ROOT = Path(__file__).resolve().parents[2]
QUANT_DIR = REPO_ROOT / "packages" / "pipeline" / "oxbow" / "quant"
ECONOMICS_YAML = REPO_ROOT / "config" / "economics.yaml"
PIPELINE_YAML = REPO_ROOT / "config" / "pipeline.yaml"

RAW_ECONOMICS = yaml.safe_load(ECONOMICS_YAML.read_text(encoding="utf-8"))
RAW_PIPELINE = yaml.safe_load(PIPELINE_YAML.read_text(encoding="utf-8"))


def write_config_tree(tmp_path: Path, economics: Mapping[str, object]) -> Path:
    """A throwaway ``config/`` tree, so a rejection can be tested as a real load."""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "economics.yaml").write_text(
        yaml.safe_dump(dict(economics)), encoding="utf-8"
    )
    (tmp_path / "config" / "pipeline.yaml").write_text(
        yaml.safe_dump(RAW_PIPELINE), encoding="utf-8"
    )
    return tmp_path


def leaf_paths(node: object, prefix: str = "") -> list[str]:
    """Every scalar leaf of a mapping, as a dotted/indexed path."""
    if isinstance(node, Mapping):
        return [
            leaf
            for key, value in node.items()
            for leaf in leaf_paths(value, f"{prefix}.{key}" if prefix else str(key))
        ]
    if isinstance(node, list):
        return [
            leaf
            for index, item in enumerate(node)
            for leaf in leaf_paths(item, f"{prefix}[{index}]")
        ]
    return [prefix]


def drop_leaf(tree: object, path: str) -> None:
    """Delete one leaf from a decoded YAML tree, in place."""
    container = tree
    parts = path.split(".")
    for position, part in enumerate(parts):
        key, _, index_text = part.partition("[")
        if position == len(parts) - 1:
            target = container[key] if isinstance(container, Mapping) else container
            if index_text:
                del target[int(index_text.rstrip("]"))]
            else:
                del container[key]
            return
        container = container[key]
        if index_text:
            container = container[int(index_text.rstrip("]"))]


@pytest.fixture(scope="module")
def cfg() -> Economics:
    return load_economics(REPO_ROOT)


# --- the recovery rate and its band --------------------------------------


def test_recovery_rate_bounds(cfg: Economics) -> None:
    """Plan §11's named test: the interval on ``r`` is open, and the band has three values.

    ``r = 0`` prices every exposure at nothing and ``r = 1`` claims a perfect
    seizure; both degenerate the ranking rather than merely flattering it, so they
    sit outside the admissible set rather than inside it at the edge.
    """
    assert cfg.recovery.lower_exclusive == 0.0
    assert cfg.recovery.upper_exclusive == 1.0
    assert cfg.recovery.lower_exclusive < cfg.recovery.rate < cfg.recovery.upper_exclusive
    assert len(cfg.recovery.band) == 3
    for rate in cfg.recovery.band:
        assert cfg.recovery.lower_exclusive < rate < cfg.recovery.upper_exclusive

    # Exclusive bounds: a rate *at* a bound is refused, not clamped. The bands below
    # are built so the middle equals the rate, otherwise the "default must be the
    # middle of its own band" check fires first and proves the wrong rule.
    with pytest.raises(ConfigError, match="open interval"):
        RecoveryAssumptions(
            rate=1.0, band=(0.5, 1.0, 1.5), lower_exclusive=0.0, upper_exclusive=1.0
        )
    with pytest.raises(ConfigError, match="open interval"):
        RecoveryAssumptions(
            rate=0.0, band=(-0.5, 0.0, 0.2), lower_exclusive=0.0, upper_exclusive=1.0
        )
    # And the default must be the middle of its own band, or the band is a footnote
    # about a different number.
    with pytest.raises(ConfigError, match="not the middle"):
        RecoveryAssumptions(
            rate=0.9, band=(0.2, 0.35, 0.5), lower_exclusive=0.0, upper_exclusive=1.0
        )


def test_recovery_band_always_renders_three_values(cfg: Economics) -> None:
    """Three values *in the rendered text*, not merely in the config list.

    Asserting on the output is the difference between checking the configuration and
    checking the claim: a renderer that printed only the default would still leave a
    three-value band sitting in the file looking compliant.
    """
    block = assumption_block(cfg)
    assert len(block.rates) == 3
    figure = currency_figure_over_band(
        "Exposure recovered", block, lambda rate: Money(int(1_000_000 * rate), cfg.currency)
    )
    first_line = figure.render().splitlines()[0]
    assert first_line.count(" at r=") == 3
    for rate in cfg.recovery.band:
        assert f"at r={rate:.2f}" in first_line


def test_a_figure_missing_a_band_coordinate_is_refused(cfg: Economics) -> None:
    """Two of three coordinates is a point estimate wearing a band."""
    block = assumption_block(cfg)
    partial = {rate: Money(1_000, cfg.currency) for rate in block.rates[:2]}
    with pytest.raises(AssumptionBandError, match="whole recovery band"):
        currency_figure("Partial", partial, block)


def test_band_coordinates_must_match_the_block() -> None:
    """A figure priced under one economy cannot travel with another's assumptions."""
    cfg = hand_economics()
    block = assumption_block(cfg)
    alien = {rate + 0.01: Money(100, cfg.currency) for rate in block.rates}
    with pytest.raises(AssumptionBandError, match="whole recovery band"):
        currency_figure("Mismatched", alien, block)


# --- money is integer minor units ----------------------------------------


def test_money_rejects_a_float_amount() -> None:
    """DEV-005 at the type boundary.

    The AST hook catches annotated float money; this runtime check catches it
    arriving through a mapping or a JSON payload, where there is no annotation for
    the hook to see.
    """
    with pytest.raises(CurrencyMismatchError, match="integer minor units"):
        Money(12.5, "UGX")
    with pytest.raises(CurrencyMismatchError, match="3-letter"):
        Money(100, "ugx funds")
    assert str(Money(500, "UGX")) == "500 minor UGX"


def test_cross_currency_sum_raises(cfg: Economics) -> None:
    """Plan §11 and 02 money rules: group by currency or fail, never silently sum.

    The message has to name both currencies, because the realistic cause is one row
    in a corpus with a different code and a reader has to find it.
    """
    with pytest.raises(CurrencyMismatchError, match="no implicit FX"):
        Money(1_000, "UGX") + Money(1_000, "EUR")
    with pytest.raises(CurrencyMismatchError, match=r"cannot add UGX 5000 to EUR -1"):
        Money(5_000, cfg.currency) - Money(1, "EUR")


def test_exposure_of_a_mixed_currency_cluster_raises(cfg: Economics) -> None:
    """The cap is a ``min()`` over two totals, and across currencies it is meaningless.

    A cluster funded in one money and paying out in another has two exposures, not
    one sum. Each side is internally pure here, so this is the case the per-side
    guards cannot see — which is why it has its own check.
    """
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    edges = [
        FlowEdge("txn:1", "ACC-A", "ACC-Z", origin, Money(5_000, "EUR")),
        FlowEdge("txn:2", "ACC-W", "ACC-A", origin, Money(4_000, "UGX")),
    ]
    with pytest.raises(CrossCurrencyExposureError, match="different currencies"):
        exposure_at_risk(edges, "ACC-A", origin, 24, 0, cfg)


def test_scale_div_rounds_half_up_deterministically() -> None:
    """One stated rounding rule for every scaled amount.

    ``round`` is banker's rounding and ``int`` truncates, so without a single rule
    two accounts with identical inputs can differ by a minor unit and the queue
    total stops matching the sum of its rows.
    """
    assert scale_div(5, 2) == 3
    assert scale_div(-5, 2) == -2
    assert scale_div(4, 2) == 2
    assert scale_div(1, 3) == 0
    with pytest.raises(QuantError, match="non-zero"):
        scale_div(1, 0)
    with pytest.raises(QuantError, match="finite"):
        ratio_to_micro(float("nan"))


def test_rate_to_micro_is_the_only_float_entry_point(cfg: Economics) -> None:
    """A rate becomes an integer before it touches money, and the tie rule is stated.

    Half-up micro conversion is what makes ``0.125`` and ``0.375`` behave alike;
    banker's rounding would land one tie on an even micro-count and the other on an
    odd one, so identical structure would price differently.
    """
    assert ratio_to_micro(0.35) == 350_000
    assert ratio_to_micro(1.0) == 1_000_000
    assert ratio_to_micro(0.125) == 125_000
    assert ratio_to_micro(0.0) == 0
    amount = Money(1_000_000_001, cfg.currency)
    assert amount.scaled_by_micro(ratio_to_micro(0.5)).minor == 500_000_001
    with pytest.raises(CurrencyMismatchError, match="micro-units"):
        amount.scaled_by_micro(0.5)


# --- every key owned, nothing silently defaulted --------------------------


def test_every_assumption_key_is_required_by_a_consumer(tmp_path: Path) -> None:
    """Delete any required leaf of ``economics.yaml`` and the load must fail.

    The file's header comment claims a named consumer for every key. This is what
    makes that claim executable rather than aspirational. The exception is the
    individual entries under ``review_minutes_by_alert_class``, which are members of
    a mapping rather than required keys: which alert classes exist is the scorecard's
    decision (plan §10 cuts bands A–E on validation), so insisting on one specific
    set here would couple the economics file to a phase that has not landed yet. What
    *is* enforced is that the mapping is never empty and that every class in it clears
    the minutes floor, both tested separately below.
    """
    sweep_root = tmp_path / "sweep"
    declared = [
        path
        for path in leaf_paths(RAW_ECONOMICS)
        if not path.startswith("review_minutes_by_alert_class.")
    ]
    assert len(declared) >= 25, f"only {len(declared)} leaves found; the file was emptied"
    for path in declared:
        case = sweep_root / path.replace(".", "_").replace("[", "").replace("]", "")
        truncated = copy.deepcopy(RAW_ECONOMICS)
        drop_leaf(truncated, path)
        with pytest.raises(ConfigError, match=path.split(".")[-1].split("[")[0]):
            load_economics(write_config_tree(case, truncated))


def test_alert_classes_cannot_be_emptied_or_dropped_wholesale(tmp_path: Path) -> None:
    """The mapping itself is required, even though its keys belong to the scorecard.

    Without review minutes there is no ``m_i``, so there is no EV density, no
    capacity consumption and no knapsack: the whole queue ranking collapses.
    """
    emptied = copy.deepcopy(RAW_ECONOMICS)
    emptied["review_minutes_by_alert_class"] = {}
    with pytest.raises(ConfigError, match="non-empty mapping"):
        load_economics(write_config_tree(tmp_path / "empty", emptied))

    missing = copy.deepcopy(RAW_ECONOMICS)
    del missing["review_minutes_by_alert_class"]
    with pytest.raises(ConfigError, match="review_minutes_by_alert_class"):
        load_economics(write_config_tree(tmp_path / "missing", missing))


def test_load_refuses_an_assumption_no_consumer_reads(tmp_path: Path) -> None:
    """The other direction: an unread key is refused at load, not left to be believed."""
    orphan = copy.deepcopy(RAW_ECONOMICS)
    orphan["an_unread_assumption"] = 7
    with pytest.raises(ConfigError, match="no consumer reads"):
        load_economics(write_config_tree(tmp_path, orphan))


def test_zero_review_minutes_floor_is_rejected_at_load(tmp_path: Path) -> None:
    """Plan §11: a zero ``m_i`` is a config error, never a runtime division.

    EV density is ``EV_i / m_i``, so a zero-minute alert scores an infinite density
    and owns the queue forever. That is a wrong *ranking* rather than a visible
    crash, which is precisely why it is refused at the boundary.
    """
    broken = copy.deepcopy(RAW_ECONOMICS)
    broken["analyst"]["min_review_minutes"] = 0
    with pytest.raises(ConfigError, match="min_review_minutes"):
        load_economics(write_config_tree(tmp_path / "floor", broken))

    below_floor = copy.deepcopy(RAW_ECONOMICS)
    below_floor["review_minutes_by_alert_class"]["E"] = 1
    with pytest.raises(ConfigError, match="falls below"):
        load_economics(write_config_tree(tmp_path / "class", below_floor))


def test_window_and_seed_must_agree_with_pipeline_yaml(tmp_path: Path) -> None:
    """Two files holding one fact must be checked against each other.

    ``config/pipeline.yaml`` owns the exposure window for the graph layer and this
    file owns it for pricing. If they drift, the graph walks a 48-hour cluster while
    the EV figures price a 24-hour one, and every number downstream stays plausible.
    """
    drift = copy.deepcopy(RAW_ECONOMICS)
    drift["exposure"]["window_hours"] = 48
    with pytest.raises(ConfigError, match="window_hours"):
        load_economics(write_config_tree(tmp_path / "window", drift))

    hops = copy.deepcopy(RAW_ECONOMICS)
    hops["exposure"]["downstream_hops"] = 2
    with pytest.raises(ConfigError, match="downstream_hops"):
        load_economics(write_config_tree(tmp_path / "hops", hops))

    seeded = copy.deepcopy(RAW_ECONOMICS)
    seeded["monte_carlo"]["seed"] = 9999
    with pytest.raises(ConfigError, match="monte_carlo.seed"):
        load_economics(write_config_tree(tmp_path / "seed", seeded))


def test_parallel_cpsat_is_refused_because_it_is_not_reproducible(tmp_path: Path) -> None:
    """OR-Tools portfolio solving races, so a second worker makes the answer machine-dependent.

    ``make verify-determinism`` compares two runs of one input; a solver whose
    incumbent depends on which worker found it first cannot pass that check, and the
    config would otherwise advertise a knob that silently voids a gate.
    """
    raced = copy.deepcopy(RAW_ECONOMICS)
    raced["solver"]["cpsat_workers"] = 4
    with pytest.raises(ConfigError, match="must be 1"):
        load_economics(write_config_tree(tmp_path, raced))


def test_cost_per_minute_must_divide_out_of_the_hourly_rate(tmp_path: Path) -> None:
    """A per-minute price derived with rounding would put a rounding rule under every c_i."""
    skewed = copy.deepcopy(RAW_ECONOMICS)
    skewed["analyst"]["cost_per_minute_minor"] = 15_001
    with pytest.raises(ConfigError, match="disagrees"):
        load_economics(write_config_tree(tmp_path / "minute", skewed))

    indivisible = copy.deepcopy(RAW_ECONOMICS)
    indivisible["analyst"]["cost_per_hour_minor"] = 250_000
    with pytest.raises(ConfigError, match="does not divide"):
        load_economics(write_config_tree(tmp_path / "hour", indivisible))


def test_the_90_percent_interval_is_the_width_the_copy_claims(tmp_path: Path) -> None:
    """The case page says "90 % interval", so a wider pair is a different claim."""
    widened = copy.deepcopy(RAW_ECONOMICS)
    widened["monte_carlo"]["interval"] = [0.025, 0.975]
    with pytest.raises(ConfigError, match="must span"):
        load_economics(write_config_tree(tmp_path, widened))


def test_capacity_sweep_must_contain_the_operating_point(tmp_path: Path) -> None:
    """A frontier that does not pass through the operating point cannot mark it."""
    narrow = copy.deepcopy(RAW_ECONOMICS)
    narrow["capacity"]["sweep"]["max_minutes"] = 6_000
    with pytest.raises(ConfigError, match="outside the sweep range"):
        load_economics(write_config_tree(tmp_path, narrow))


# --- the renderer's refusal ----------------------------------------------


def test_currency_requires_assumptions(cfg: Economics) -> None:
    """Plan §11's named test: the renderer refuses to emit money without its block.

    Four holes are closed at once: no block, an empty block, a block that does not
    name its source file, and any second producer of major-unit strings in the
    package. The last is what turns this from a convention into a dependency — with
    no other way to render money, a figure without assumptions has nowhere to come
    from.
    """
    block = assumption_block(cfg)
    values = {rate: Money(1_000, cfg.currency) for rate in block.rates}

    with pytest.raises(MissingAssumptionsError, match="without its assumption block"):
        currency_figure("Naked figure", values, None)

    with pytest.raises(MissingAssumptionsError, match="cannot be empty"):
        AssumptionBlock(
            text="   ", rates=block.rates, currency=block.currency, per_major=100, source_path="x"
        )

    with pytest.raises(MissingAssumptionsError, match="name the file"):
        AssumptionBlock(
            text="some figures that do not cite their source",
            rates=block.rates,
            currency=block.currency,
            per_major=100,
            source_path="config/economics.yaml",
        )

    figure = currency_figure("Exposure recovered", values, block)
    rendered = figure.render()
    assert "config/economics.yaml" in rendered
    assert rendered.splitlines()[0].count(" at r=") == 3
    assert str(figure) == rendered

    offenders = [
        module.name
        for module in sorted(QUANT_DIR.glob("*.py"))
        if module.name not in {"money.py", "economics.py"}
        and "to_major_text(" in module.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"{offenders} render money without going through the renderer"


def test_assumption_block_names_every_governing_value(cfg: Economics) -> None:
    """The block is the assumption *line*, so it must contain the things that decide it.

    Checked against the loaded config rather than literals, so retuning an
    assumption cannot silently leave the block describing the old economy.
    """
    text = assumption_block(cfg).text
    for rate in cfg.recovery.band:
        assert f"{rate:.2f}" in text
    assert f"{cfg.exposure.window_hours} h" in text
    assert f"{cfg.exposure.downstream_hops}-hop" in text
    assert "EV_i = p_i * E_i * r" in text
    assert "not measured outcomes" in text
    for name in cfg.alert_classes:
        assert f"{name}={cfg.minutes_for(name)} min" in text
    assert to_major_text(cfg.friction_cost.minor, cfg.currency, cfg.minor_units_per_major) in text
    assert f"{cfg.capacity.review_minutes_per_period:,}" in text


def test_missing_alert_class_is_refused_not_defaulted(cfg: Economics) -> None:
    """Guessing a review time for an unknown class would invent a price for an alert."""
    with pytest.raises(ConfigError, match="no review minutes configured"):
        cfg.minutes_for("Z")


def test_figures_cannot_marry_two_currencies(cfg: Economics) -> None:
    """A figure under UGX assumptions cannot carry a EUR amount at any coordinate."""
    block = assumption_block(cfg)
    values = {rate: Money(1_000, "EUR") for rate in block.rates}
    with pytest.raises(CurrencyMismatchError, match="no implicit FX"):
        currency_figure("Wrong money", values, block)


def test_rendered_amounts_come_from_unrounded_minor_units(cfg: Economics) -> None:
    """Totals are computed in minor units and divided at render time only.

    Three tenths must total 0.30. Computed from rendered floats the sum is
    0.30000000000000004 and prints as something the rows do not add up to, which is
    the reconciliation failure 03 §A rule 2 is written against.
    """
    total = Money(sum(10 for _ in range(3)), cfg.currency)
    block = assumption_block(cfg)
    figure = currency_figure("Thirds", {rate: total for rate in block.rates}, block)
    assert to_major_text(total.minor, cfg.currency, cfg.minor_units_per_major) in figure.render()
    assert "0.30" in figure.render()
