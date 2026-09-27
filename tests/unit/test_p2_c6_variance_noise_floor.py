"""C6: a variance residue destroyed by cancellation must answer NULL, never a zero.

WHY THIS FILE EXISTS ALONGSIDE ``test_p2_features.py``. The float-stat kernels compute
dispersion in one pass, as the difference of two running totals: ``Sxx - Sx*Sx/n`` subtracts
two quantities of size ``n*mean**2`` to recover a residue of size ``(n-1)*variance``, so it
keeps only the digits the operands do not share. Money is int64 minor units and never
touches float, but a *variance* has to: ``amount_minor**2`` for one PaySim transfer of 3.5e9
minor units is 1.225e19, past float64's 53-bit exact-integer range of 9.007e15, where one ulp
is 2048.0. An account whose recent amounts all sit near that magnitude with a spread of a few
minor units therefore has a residue that is entirely the subtraction's own rounding error,
and its sign is decided by round-to-nearest on terms nobody transacted.

The kernel used to answer that with ``.clip(0.0, None)``: a negative residue became sigma =
0.0, a finite number, so the ``_not_finite`` sweep downstream could not see it and the
scorecard read "this amount is perfectly typical" about a quantity that was never measured.
A positive residue was quieter and just as wrong: it published a sigma of pure noise as a
plausible score. These tests hold both arms shut, and hold open the cases the guard must NOT
touch: a real dispersion at the same magnitude, an exact zero dispersion, and every
arithmetically sound row in the shipped fixture corpora, byte for byte.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Final

import polars as pl
import pytest

from oxbow.config import PipelineConfig, find_repo_root, load_pipeline_config
from oxbow.features import kinds
from oxbow.features.build import FeatureTable, build_feature_table
from oxbow.features.fakes import canonical_event, canonical_frame, star_fixture, wide_fixture
from oxbow.features.kinds import EVENT_TS, TXN_ID
from oxbow.features.registry import FeatureRegistry, registry_from_config_dir

CONFIG_DIR: Final = find_repo_root() / "config"

# The magnitude the kernel's own `_running_totals` comment names: one PaySim transfer.
BIG: Final = 3_500_000_000
ROBUST_Z: Final = "amount_robust_z_last"


@pytest.fixture(scope="module")
def registry() -> FeatureRegistry:
    return registry_from_config_dir(CONFIG_DIR)


@pytest.fixture(scope="module")
def cfg() -> PipelineConfig:
    return load_pipeline_config(find_repo_root())


def _legacy_expression() -> pl.Expr:
    """The pre-fix expression, verbatim: clamp the negative residue, then take the root.

    Its only job is to be the thing the guard replaced. Every "this changed, and correctly"
    assertion runs the real kernel with this put back, so a reintroduced clamp cannot hide
    behind a test that merely checks for "no crash".
    """
    variance = (pl.col("squares") - (pl.col("total") * pl.col("total")) / pl.col("n")) / (
        pl.col("n") - 1
    )
    return pl.when(pl.col("n") > 1).then(variance.clip(0.0, None).sqrt()).otherwise(None)


def _legacy_sample_std(total: pl.Series, squares: pl.Series, counts: pl.Series) -> float | None:
    """`_sample_std` with the clamp restored."""
    frame = pl.DataFrame({"total": total, "squares": squares, "n": counts})
    return frame.select(_legacy_expression().alias("__value__"))["__value__"].item()


def _build_legacy(
    registry: FeatureRegistry, cfg: PipelineConfig, events: pl.DataFrame
) -> FeatureTable:
    """The real builder, run with the pre-fix std expression installed."""
    fixed = kinds._sample_std_expression
    kinds._sample_std_expression = _legacy_expression
    try:
        return build_feature_table(events, registry, cfg=cfg)
    finally:
        kinds._sample_std_expression = fixed


def _amount_events(amounts: list[int]) -> pl.DataFrame:
    """One account's own history, every event inside a single 30-day window.

    Spaced an hour apart so each row's trailing window is exactly the rows up to and
    including itself, which lets the expectation be computed from the same list the fixture
    declares.
    """
    return canonical_frame(
        [
            canonical_event(f"big{i}", i * 60, "acct_a", "acct_b", amount)
            for i, amount in enumerate(amounts)
        ]
    )


def _published_z(table: FeatureTable) -> list[float | None]:
    """The published `amount_robust_z_last`, in the sender's own row order."""
    rows = table.matrix.filter(pl.col("entity") == "acct_a").sort([EVENT_TS, TXN_ID])
    return rows[ROBUST_Z].to_list()


def _exact_z(amounts: list[int]) -> float:
    """``(x_last - mean) / sample_std`` over `amounts`, in exact rational arithmetic."""
    values = [Fraction(amount) for amount in amounts]
    count = len(values)
    mean = sum(values, Fraction(0)) / count
    variance = sum(((value - mean) ** 2 for value in values), Fraction(0)) / (count - 1)
    return float((values[-1] - mean) / math.sqrt(float(variance)))


def _totals(amounts: list[int]) -> tuple[pl.Series, pl.Series, pl.Series]:
    return (
        pl.Series([float(sum(amounts))]),
        pl.Series([float(sum(amount * amount for amount in amounts))]),
        pl.Series([float(len(amounts))]),
    )


# --- the corpus case: destroyed arithmetic reads as NULL -------------------


def test_near_identical_large_amounts_publish_null_not_a_clamped_zero(
    registry: FeatureRegistry, cfg: PipelineConfig
) -> None:
    """Five transfers near 3.5e9 minor units: sigma is unresolvable, so the score is null.

    The amounts are 3,500,000,000, +2, +4, +6, +8 -- a real spread of 8 minor units, so the
    honest z of the last row against a sample standard deviation of 3.1623 is 1.2649. The
    residue the one-pass formula recovers is 8192.0 against a rounding floor of about 4.4e5,
    i.e. noise alone, and the pre-fix kernel turned it into 0.0884: a confident score 14x off
    that no finiteness sweep can flag, because a clamped zero is finite.
    """
    amounts = [BIG + 2 * i for i in range(5)]
    events = _amount_events(amounts)
    published = _published_z(build_feature_table(events, registry, cfg=cfg))

    assert published, "the fixture produced no rows, which proves nothing"
    assert all(
        value is None for value in published
    ), f"a destroyed residue leaked into {ROBUST_Z}: {published}"
    # The three impostors a "fixed" kernel could still emit, named rather than implied.
    leaked = [value for value in published if value is not None]
    assert 0.0 not in leaked, "a clamped zero is the defect, not an acceptable answer"
    assert not any(math.isnan(value) for value in leaked)
    assert not any(math.isinf(value) for value in leaked)

    # The witness that this test fails before the fix: the same kernel with the clamp
    # restored publishes a finite, plausible, wrong number on the identical fixture.
    legacy = _published_z(_build_legacy(registry, cfg, events))
    assert legacy[-1] == 0.08838834764831843, f"pre-fix witness moved: {legacy[-1]!r}"
    assert legacy[-1] != _exact_z(amounts)

    # And in its barest form, at the std kernel itself (`agg='std'`): the clamp answered a
    # finite sigma for a window whose dispersion was never measured.
    total, squares, counts = _totals(amounts)
    assert kinds._sample_std(total, squares, counts).item() is None
    assert _legacy_sample_std(total, squares, counts) == 45.254833995939045


# --- the guard must be a guard, not a blackout -----------------------------


def test_a_real_dispersion_at_the_same_magnitude_survives_the_guard(
    registry: FeatureRegistry, cfg: PipelineConfig
) -> None:
    """Same 1e9-plus magnitudes, genuine spread: the score is published and it is correct.

    Without this arm the null test is worthless -- nulling every window would satisfy it.
    Here the residue dwarfs its own rounding floor, so the honest answer is a number, and
    that number is checked against exact rational arithmetic instead of a constant that
    would drift with the fixture.
    """
    amounts = [BIG, 7_000_000_000, 5_250_000_001]
    events = _amount_events(amounts)
    published = _published_z(build_feature_table(events, registry, cfg=cfg))
    last = published[-1]

    assert last is not None, "the guard nulled a window whose arithmetic is sound"
    assert math.isfinite(last)
    assert last == pytest.approx(_exact_z(amounts), rel=1e-9)
    # And it is exactly the value the pre-fix formula produced: the guard only ever removes
    # arithmetic that could not be trusted, it never re-derives one that could.
    assert repr(_published_z(_build_legacy(registry, cfg, events))[-1]) == repr(last)


def test_an_exact_zero_dispersion_is_still_a_measured_zero(
    registry: FeatureRegistry, cfg: PipelineConfig
) -> None:
    """Identical small amounts: every intermediate is exact, so 0.0 remains 0.0.

    This is the arm that keeps the guard honest in the other direction. Inside float64's
    exact-integer range the residue *is* the integer value, so a zero dispersion is a
    measurement and the std kernel must still answer 0.0 -- nulling it would mean the fix
    was "give up on variance", which is neither the finding nor the fix. The same shape past
    2**53 is a different fact: there a zero residue cannot be told from a destroyed one.
    """
    small = [50_000] * 6
    assert kinds._sample_std(*_totals(small)).item() == 0.0

    huge = [BIG] * 4
    assert kinds._sample_std(*_totals(huge)).item() is None

    # The published feature is null for both -- zero dispersion has nothing to compare
    # against, which is the policy `_kind_float_stat`'s docstring already states. So this
    # asymmetry lives in the std kernel, where `agg='std'` would publish it.
    assert set(_published_z(build_feature_table(_amount_events(small), registry, cfg=cfg))) == {
        None
    }


# --- determinism: sound rows are untouched, bit for bit ---------------------


@pytest.mark.parametrize("fixture", ["star", "wide"])
def test_sound_rows_are_byte_identical_to_the_pre_fix_formula(
    registry: FeatureRegistry,
    cfg: PipelineConfig,
    fixture: str,
) -> None:
    """Every float column of the shipped fixture corpora is unchanged by the guard.

    The claim determinism requires is the narrow one: the guard may only remove values the
    arithmetic could not support, never re-derive values it could. So the whole matrix is
    rebuilt with the clamp restored and compared cell by cell on ``repr`` -- the exact float
    -- because a last-bit nudge in a mean or a std would show up here and would move a
    downstream EV by more than the minor units a packet prints.
    """
    events = star_fixture() if fixture == "star" else wide_fixture(400, accounts=24, seed=1337)
    before = _build_legacy(registry, cfg, events).matrix
    after = build_feature_table(events, registry, cfg=cfg).matrix

    assert before.columns == after.columns
    float_columns = [name for name in after.columns if after[name].dtype == pl.Float64]
    assert float_columns, "no float columns to compare, which would make this test vacuous"
    for name in float_columns:
        old, new = before[name].to_list(), after[name].to_list()
        drift = [
            (index, old_value, new_value)
            for index, (old_value, new_value) in enumerate(zip(old, new, strict=True))
            if repr(old_value) != repr(new_value)
        ]
        assert not drift, f"{fixture}/{name}: {len(drift)} sound cell(s) changed, first={drift[0]}"
