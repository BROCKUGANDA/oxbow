"""Shared fixtures for the P6 tests: a fast config and a hand-built corpus.

The plan-§12 demonstration runs 8 variants x 5 folds with 10,000 Monte Carlo draws and
1,000 bootstrap resamples; a unit suite cannot wait on that and stay honest about the same
code paths. So the tests shrink the *expensive* knobs (draws, resamples) through
``dataclasses.replace`` while leaving every policy/metric/label rule at its configured
value. The reduced numbers are still real outputs of the same functions — they are just
fewer draws — and the full-scale figures live in ``python -m oxbow.backtest.run``.

The hand corpus carries small integer money so the realized economics and metrics can be
verified against arithmetic done on paper (00 §B: a fixture whose expected value came from
the code it tests proves nothing).
"""

from __future__ import annotations

from dataclasses import replace

import polars as pl

from oxbow.backtest import fakes
from oxbow.backtest.config_io import BacktestConfig, load_backtest_config
from oxbow.backtest.interfaces import (
    COL_ACCOUNT_KEY,
    COL_AMOUNT_MINOR,
    COL_AS_OF_TS,
    COL_EXPOSURE_MINOR,
    COL_LABEL,
    COL_LABEL_TYPOLOGY,
    COL_REVIEW_COST_MINOR,
    COL_REVIEW_MINUTES,
    COL_SPEC_HASH,
)


def fast_config() -> BacktestConfig:
    """The real config with only the Monte Carlo / bootstrap scale reduced."""
    config = load_backtest_config()
    return replace(
        config,
        mc_draws=250,
        bootstrap_resamples=60,
        stability_seeds=(config.seed,),
    )


def hand_corpus(
    rows: list[dict[str, object]],
    *,
    spec_hash: str = fakes.FAKE_SPEC_HASH,
) -> pl.DataFrame:
    """Build a per-account backtest corpus from hand-written rows.

    Each row needs the required money/label fields; ``account_key`` and the feature spec
    hash default so a test states only the numbers it is checking. The as-of column is
    cast to ``Datetime[us, UTC]`` because the harness reads it as timestamps for the embargo
    discipline check.
    """
    base: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        entry: dict[str, object] = {
            COL_ACCOUNT_KEY: row.get(COL_ACCOUNT_KEY, f"ACC-{index:04d}"),
            COL_AS_OF_TS: row[COL_AS_OF_TS],
            COL_LABEL: row[COL_LABEL],
            COL_SPEC_HASH: spec_hash,
            COL_EXPOSURE_MINOR: row[COL_EXPOSURE_MINOR],
            COL_REVIEW_COST_MINOR: row[COL_REVIEW_COST_MINOR],
            COL_REVIEW_MINUTES: row[COL_REVIEW_MINUTES],
            COL_AMOUNT_MINOR: row.get(COL_AMOUNT_MINOR, row[COL_EXPOSURE_MINOR]),
        }
        for optional in (
            COL_LABEL_TYPOLOGY,
            "account_age_days",
            "activity_volume",
            "community_size",
            "signal",
        ):
            if optional in row:
                entry[optional] = row[optional]
        base.append(entry)
    frame = pl.DataFrame(base)
    return frame.with_columns(pl.col(COL_AS_OF_TS).cast(pl.Datetime("us", "UTC")))


def as_of_us(day: int) -> int:
    """Microseconds at the start of day ``day`` from the fake epoch."""
    return fakes.FAKE_EPOCH_US + day * 86_400_000_000
