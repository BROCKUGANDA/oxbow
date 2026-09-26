"""The §10 scorecard guards, one named test per named behaviour.

WHAT THIS FILE OWNS. Plan §10 (P4: scorecard, GBM, calibration, fusion) names its
guardrails by behaviour rather than by module: no infinite WOE, unseen categories handled,
missing is a bin, the IV admission band, separation refused loudly, drift degrading the
score path, a zero-positive fold reported rather than dropped. The production code for every
one of them lives in ``oxbow.scoring`` and is well built. Until this file none of them had
test contact, which means none of them could be shown to still work: a guardrail nobody can
make fail is a guardrail that will quietly stop failing. Each test below was watched going
red against a flipped config flag or a removed guard row.

THE FIXTURES ARE SMALL ON PURPOSE. The gate must run in seconds, so features are a few
hundred synthetic rows drawn from one seed, and the frame-level guards run against a
three-feature synthetic registry rather than the 75-column published one: the contract in
``oxbow.scoring.frame`` is what the guards sit on, and it does not care how many columns
declare themselves. The wider end-to-end wiring runs are ``test_p4_scorer.py``'s.

SCOPE NOTE ON MONOTONICITY. ``test_monotonic_trend_enforced`` asserts that the declared flag
reaches the solver as the trend constraint in both directions, and that the direction
written into the artefact agrees with the bad rates the table actually holds.
``test_binning_reaches_the_mip_solver_and_not_the_quantile_fallback`` asserts the thing that
must be true before either of those means anything: that the mip solver ran. It was added
after the measurement that showed it did not -- every numeric feature fell into the quantile
fallback behind a swallowed ``TypeError``, so ``enforce_monotonic_trend`` had been
decorative for the whole life of the module.

DETERMINISM AND MONEY. Fixtures are seeded, no wall clock appears in an assertion, and no
ordering derives from set iteration. The per-row quantities are labels, float feature values
and counts: no amount, balance or minor-unit column appears anywhere in this file.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Final

import numpy as np
import polars as pl
import pytest
from optbinning import OptimalBinning

from oxbow.config import find_repo_root
from oxbow.models.evaluate import average_precision
from oxbow.models.folds import SKIP_ZERO_POSITIVES, fold_plan_from_frame
from oxbow.scoring.binning import (
    BOUNDARY_SOURCE_BINNING,
    KIND_CATEGORY_GROUP,
    KIND_MISSING,
    KIND_RANGE,
    KIND_UNSEEN,
    BinRow,
    FeatureBinning,
    assign_bins,
    fit_feature_binning,
    woe_of_labels,
)
from oxbow.scoring.config import (
    BinningConfig,
    FeatureDeclaration,
    FeatureGuard,
    FeatureRegistry,
    ScorecardConfig,
    load_scorecard_config,
)
from oxbow.scoring.drift import (
    ACTION_DEGRADED,
    ACTION_NORMAL,
    STATUS_ACTION,
    drift_decision,
    fit_reference_shares,
    per_feature_drift,
    psi_from_shares,
)
from oxbow.scoring.errors import DegenerateBinningError, SeparationDetectedError
from oxbow.scoring.frame import (
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    COL_FOLD,
    COL_LABEL,
    COL_LABEL_TYPOLOGY,
    COL_ROLE,
    COL_SPEC_HASH,
    PROVENANCE_GENERATED,
    ROLE_TEST,
    ROLE_TRAIN,
    ROLE_VALIDATION,
    TrainingFrame,
    build_training_frame,
)
from oxbow.scoring.model import fit_scorecard
from oxbow.scoring.selection import (
    DECISION_ADMITTED,
    DECISION_REFUSED_JUSTIFIED_LEAKAGE,
    DECISION_REFUSED_LOW_IV,
    DECISION_REFUSED_SUSPECTED_LEAKAGE,
    MIN_JUSTIFICATION_CHARACTERS,
    select_features,
)

SEED: Final = 1337
REPO_ROOT: Path = find_repo_root()

# The three-feature registry the frame-level guards are driven on. Declaration order is the
# canonical column order, so the tuple is the spec: one feature that says nothing, one that
# says something inside the IV band, and one that IS the label.
TINY_FEATURES: Final = ("quiet", "loud", "leaky")
TINY_SENTENCES: Final = {name: f"the {name} attribute sentence" for name in TINY_FEATURES}
SPEC_HASH: Final = "0123456789abcdef0123456789abcdef"
EPOCH: Final = datetime(2024, 1, 1, tzinfo=UTC)


def _scorecard() -> ScorecardConfig:
    return load_scorecard_config(REPO_ROOT)


def _rng() -> np.random.Generator:
    return np.random.default_rng(SEED)


def _numeric_binning(
    cfg: BinningConfig, feature: str, values: np.ndarray, labels: np.ndarray
) -> FeatureBinning:
    return fit_feature_binning(feature, values, labels, cfg, "numerical")


def _populated(rows: Sequence[BinRow], kind: str) -> list[BinRow]:
    return [row for row in rows if row.kind == kind and row.population > 0]


def _tiny_registry() -> FeatureRegistry:
    """A published registry of three float columns, and the digest the frame carries.

    ``build_training_frame`` compares a frame's stamp against ``registry.spec_hash``, which
    for the real file is computed by :func:`oxbow.features.registry.parse_registry`. Here the
    registry is hand-declared for a hand-built frame, so the digest is a literal the frame
    carries. The guard still bites: a frame stamped with anything else is refused.
    """
    return FeatureRegistry(
        declarations={
            name: FeatureDeclaration(
                name=name,
                group="velocity",
                sentence=TINY_SENTENCES[name],
                dtype="float64",
                role="feature",
                categories=None,
                null_policy="never_null",
            )
            for name in TINY_FEATURES
        },
        names=TINY_FEATURES,
        groups={"velocity": TINY_FEATURES},
        guards=FeatureGuard(
            max_abs_correlation_with_label=0.98,
            require_finite=True,
            missing_value_policy="explicit_bin",
        ),
        spec_version=1,
        max_lookback_days=30,
        attribute_labels=dict(TINY_SENTENCES),
        spec_hash=SPEC_HASH,
    )


def _tiny_frame(
    *,
    rows: int = 300,
    folds: int = 5,
    labels: np.ndarray | None = None,
    roles: Sequence[str] | None = None,
) -> TrainingFrame:
    """A contract-shaped training frame over the tiny registry.

    ``labels`` defaults to every fifth row being bad. ``roles`` defaults to a stamped column
    rather than the derived one, because two of these guards must place positives in
    particular roles, and neither may depend on a sampler for it.
    """
    rng = _rng()
    bad = (
        (np.arange(rows, dtype=np.int8) % 5 == 0).astype(np.int8)
        if labels is None
        else np.asarray(labels, dtype=np.int8)
    )
    values = rng.normal(0.0, 1.0, rows)
    fold_of_row = [index % folds for index in range(rows)]
    if roles is None:
        rows_per_fold = rows // folds
        roles = [
            ROLE_TEST
            if fold == folds - 1
            else (
                ROLE_VALIDATION
                if index % rows_per_fold >= int(rows_per_fold * 0.85)
                else ROLE_TRAIN
            )
            for index, fold in enumerate(fold_of_row)
        ]
    frame = pl.DataFrame(
        {
            COL_ACCOUNT_KEY: pl.Series(
                COL_ACCOUNT_KEY,
                [f"ACC{index:05d}" for index in range(rows)],
                dtype=pl.String,
            ),
            COL_AS_OF_TS: pl.Series(
                COL_AS_OF_TS,
                [EPOCH + timedelta(minutes=index) for index in range(rows)],
                dtype=pl.Datetime("us", "UTC"),
            ),
            COL_FOLD: pl.Series(COL_FOLD, fold_of_row, dtype=pl.Int64),
            COL_LABEL: pl.Series(COL_LABEL, bad.tolist(), dtype=pl.Int8),
            COL_LABEL_TYPOLOGY: pl.Series(
                COL_LABEL_TYPOLOGY,
                [("gather" if int(flag) == 1 else None) for flag in bad.tolist()],
                dtype=pl.String,
            ),
            "quiet": pl.Series("quiet", values.tolist(), dtype=pl.Float64),
            "loud": pl.Series("loud", (values + 0.6 * bad).tolist(), dtype=pl.Float64),
            # The leak: the column *is* the label, which is the only honest shape for the
            # thing the separation guard exists to refuse.
            "leaky": pl.Series("leaky", bad.astype(np.float64).tolist(), dtype=pl.Float64),
            COL_ROLE: pl.Series(COL_ROLE, list(roles), dtype=pl.String),
            COL_SPEC_HASH: pl.Series(COL_SPEC_HASH, [SPEC_HASH] * rows, dtype=pl.String),
        }
    )
    return build_training_frame(
        frame,
        _tiny_registry(),
        (),
        PROVENANCE_GENERATED,
        validation_fraction=0.15,
        n_folds=folds,
    )


def test_no_infinite_woe() -> None:
    """§10's literal gate clause: no bin has zero bads without a recorded merge or smoothing.

    A zero-bad bin makes ``ln((g_b/G) / (b_b/B))`` infinite and the points table nonsense, so
    the binning layer merges bins under the population floor into a neighbour and
    Laplace-smooths whatever survives. Both halves get asserted, on two fixtures:

    *THE SMOOTHER*, on a feature with no bads anywhere -- the ordinary shape of an AML
    corpus, and the reason label scarcity is a design input rather than a surprise. The
    table is one populated bin whose bad count is zero, which is exactly the bin whose WOE
    is infinite before something is done about it.

    *THE MERGE PASS*, on a feature whose bads sit in the low tail. That one does split into
    a real table (seven value bins with the shipped config), and the clause's job there is
    to leave no populated zero-bad bin standing without a trail -- either because the
    candidate was merged away, or because what survives records the smoother.

    Neither fixture is chosen to make the assertion easy: the first is the degenerate case
    the smoother exists for, and the second is checked against the number of bins, so a
    solver that stopped partitioning would fail this test rather than quietly vacate it.
    """
    binning_cfg = _scorecard().binning
    rng = _rng()
    rows = 600
    values = np.sort(rng.normal(0.0, 1.0, rows))

    no_bads = _numeric_binning(
        binning_cfg, "no_bads_at_all", values, np.zeros(rows, dtype=np.int32)
    )
    zero_bad_bins = [row for row in no_bads.value_rows if row.n_bad == 0 and row.population > 0]
    assert zero_bad_bins, (
        "the fixture produced no populated zero-bad bin, so every assertion below would pass "
        "without the gate clause being reached"
    )
    assert no_bads.bins_with_zero_bads >= len(zero_bad_bins)

    for row in zero_bad_bins:
        assert math.isfinite(row.woe), (
            f"bin {row.label!r} has {row.population} rows, 0 bads and WOE {row.woe}: an "
            "infinite weight of evidence is what the clause forbids"
        )
        assert row.merge_applied or (row.smoothing_applied and row.smoothing_reason), (
            f"bin {row.label!r} has zero bads with neither a recorded merge nor a recorded "
            "smoothing rule"
        )
        assert row.smoothing_applied, (
            f"bin {row.label!r} has zero bads, so alpha is load-bearing there and the flag "
            "must say so rather than leave the explanation to the merge trail"
        )
        assert "zero bads" in row.smoothing_reason, row.smoothing_reason
        assert f"laplace_alpha={binning_cfg.laplace_alpha}" in row.smoothing_reason

    tail_probabilities = np.clip(1.1 - 1.1 * values, 0.0, 0.95)
    labels = (rng.random(rows) < tail_probabilities).astype(np.int32)
    tailed = _numeric_binning(binning_cfg, "bads_in_the_low_tail", values, labels)
    assert len(tailed.value_rows) >= 2, (
        f"the tail fixture came back with {len(tailed.value_rows)} value bin(s) from "
        f"{tailed.boundary_source!r}, so the merge pass below has nothing to merge"
    )
    for row in tailed.value_rows:
        if row.n_bad == 0 and row.population > 0:
            assert row.merge_applied or row.smoothing_applied, (
                f"bin {row.label!r} survived with {row.population} rows and 0 bads and no "
                "trail of what made that acceptable"
            )
    for row in (*no_bads.rows, *tailed.rows):
        assert math.isfinite(row.woe), f"bin {row.label!r} left the table with WOE {row.woe}"
    assert math.isfinite(tailed.iv)

    # And the floor merge records which floor it breached and where the bin went.
    stricter = replace(binning_cfg, min_bin_count=150)
    merged = _numeric_binning(stricter, "low_tail_only", values, labels)
    floor_merges = [
        entry
        for row in merged.rows
        for entry in row.merge_applied
        if entry.startswith("floor_merge:")
    ]
    assert floor_merges, (
        f"raising binning.min_bin_count to 150 on a {rows}-row frame merged nothing, so the "
        "merge half of the clause is never exercised by any test in this phase"
    )
    for entry in floor_merges:
        assert "->merged_into[" in entry, entry
        assert "max(150," in entry, entry
    recorded = [entry for row in merged.rows for entry in row.merge_applied]
    assert len(recorded) == merged.merges_recorded, (
        f"the table holds {len(recorded)} merge records and the artefact reports "
        f"{merged.merges_recorded}"
    )
    assert [entry for entry in recorded if entry.startswith("floor_merge:")] == floor_merges
    assert len(merged.value_rows) < len(tailed.value_rows), "no bin was actually absorbed"
    for row in merged.value_rows:
        assert math.isfinite(row.woe), f"merged bin {row.label!r} went non-finite: {row.woe}"


def test_unseen_category_handled() -> None:
    """A category absent at fit time gets its own bin, inheriting the training tail."""
    binning_cfg = _scorecard().binning
    rng = _rng()
    rows = 500
    labels = np.zeros(rows, dtype=np.int32)
    labels[rng.integers(0, 300, 120)] = 1
    levels = ("c0", "c1", "c2", "c3", "c4")
    picks = rng.integers(0, len(levels), rows)
    values = np.array([levels[int(pick)] for pick in picks], dtype=object)

    fitted = fit_feature_binning("channel", values, labels, binning_cfg, "categorical")

    assert fitted.unseen_tail_source is not None, (
        "the artefact records no training tail, so an unseen category would have no named "
        "source for the weight of evidence it inherited"
    )
    tail = fitted.row_for_label(fitted.unseen_tail_source)
    assert tail.kind == KIND_CATEGORY_GROUP and tail.population > 0
    value_populations = [row.population for row in fitted.value_rows if row.population > 0]
    assert tail.population == min(value_populations), (
        f"{tail.label!r} holds {tail.population} rows against {min(value_populations)}: the "
        "tail is defined as the rarest populated value bin"
    )

    unseen_label = binning_cfg.unseen_bin
    probe = np.array(["c0", "z_never_seen_at_fit", None], dtype=object)
    assigned = assign_bins(probe, fitted, binning_cfg)

    assert assigned[2] == binning_cfg.missing_bin, "a null is not an unseen category"
    assert assigned[1] == unseen_label, (
        f"'z_never_seen_at_fit' landed in {assigned[1]!r}; folding it into an observed bin "
        "invents evidence for a value the fit never saw"
    )
    row = fitted.row_for_label(unseen_label)
    assert row.kind == KIND_UNSEEN
    assert row.woe == tail.woe, (
        f"the unseen bin carries WOE {row.woe} against the tail's {tail.woe}: the rule is "
        "that it inherits the tail, not that it guesses"
    )
    assert math.isfinite(row.woe)
    assert f"unseen_tail_source:{tail.label}" in row.merge_applied, row.merge_applied
    assert "inherits the training tail" in row.smoothing_reason

    weights = woe_of_labels(fitted, assigned)
    assert bool(np.all(np.isfinite(weights))), weights

    # Without that row in the table the guard refuses, rather than quietly mapping the
    # category onto the most common one -- which is the behaviour the plan names as wrong.
    table_without_unseen = replace(
        fitted, rows=tuple(r for r in fitted.rows if r.kind != KIND_UNSEEN)
    )
    with pytest.raises(DegenerateBinningError, match="most common category"):
        assign_bins(
            np.array(["z_never_seen_at_fit"], dtype=object), table_without_unseen, binning_cfg
        )


def test_missing_is_a_bin_not_a_mean() -> None:
    """Nulls carry their own evidence: their own bin, their own rate, never an imputed mean.

    The fixture is hand-computable. 500 rows on a ramp; the null block is rows 250-349 and
    holds 40 of the 120 bads, so the missing bin's observed bad rate is 40/100 = 0.40 while
    the non-null rows sit at 80/400 = 0.20. Mean imputation would dissolve those 100 rows
    into a value range near the middle of the ramp, and the 0.40 would never be seen again.
    """
    binning_cfg = _scorecard().binning
    rows = 500
    values = np.arange(rows, dtype=np.float64) + 1.0
    labels = np.zeros(rows, dtype=np.int32)
    labels[:80] = 1
    labels[250:290] = 1
    values[250:350] = np.nan

    fitted = _numeric_binning(binning_cfg, "flow", values, labels)

    missing_rows = [row for row in fitted.rows if row.kind == KIND_MISSING]
    assert len(missing_rows) == 1, [row.kind for row in fitted.rows]
    missing = missing_rows[0]
    assert missing.label == binning_cfg.missing_bin
    assert missing.population == 100, "the missing bin does not hold the null block it was fed"
    assert missing.n_bad == 40 and missing.n_good == 60
    assert missing.bad_rate == pytest.approx(0.40), missing.bad_rate
    assert fitted.missing_share == pytest.approx(0.20)
    assert missing.population_share == pytest.approx(100 / rows)
    assert math.isfinite(missing.woe)
    assert not missing.merge_applied, (
        f"the missing bin was merged into something: {missing.merge_applied}. Specials hold "
        "their own evidence and are never folded into a value range"
    )

    value_bins = _populated(fitted.rows, KIND_RANGE)
    assert value_bins
    non_missing_rate = sum(row.n_bad for row in value_bins) / sum(
        row.population for row in value_bins
    )
    assert non_missing_rate == pytest.approx(0.20), non_missing_rate
    assert abs(missing.bad_rate - non_missing_rate) > 0.1, (
        "this fixture cannot tell a missing bin from an imputation: the null block's rate is "
        "indistinguishable from the rest of the population"
    )
    mean_value_woe = sum(row.woe for row in value_bins) / len(value_bins)
    assert abs(missing.woe - mean_value_woe) > 1e-6, (
        f"the missing bin's WOE ({missing.woe}) sits on the mean of the value bins "
        f"({mean_value_woe}), which is what an imputed null would look like"
    )
    assert missing.woe < mean_value_woe, (
        "a 0.40 bad rate against a 0.20 population is riskier than average, so its evidence "
        "must read lower on a scale where points fall as risk rises"
    )

    # WOE is a log ratio of class shares over the smoothed totals of the table, so this bin's
    # value is derived from its own 60/40 counts and nobody else's rate. The smoothing set is
    # the populated rows: an empty special is appended afterwards from the tail and takes no
    # part in the totals.
    alpha = binning_cfg.laplace_alpha
    smoothing_set = [row for row in fitted.rows if row.population > 0]
    share_good = (missing.n_good + alpha) / (
        sum(row.n_good for row in smoothing_set) + alpha * len(smoothing_set)
    )
    share_bad = (missing.n_bad + alpha) / (
        sum(row.n_bad for row in smoothing_set) + alpha * len(smoothing_set)
    )
    assert missing.woe == pytest.approx(
        math.log(share_good / share_bad)
    ), "the missing bin's WOE is not the log ratio of its own smoothed shares"
    assert missing.woe != pytest.approx(0.0, abs=1e-9), "a WOE of zero carries no evidence"

    assigned = assign_bins(values, fitted, binning_cfg)
    assert assigned.count(binning_cfg.missing_bin) == 100
    assert all(
        (label == binning_cfg.missing_bin) == bool(np.isnan(value))
        for label, value in zip(assigned, values, strict=True)
    ), "a null landed in a value bin, or a value landed in the null bin"


def test_monotonic_trend_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared monotonic-trend flag is what constrains the solver, both directions.

    Asserted: config arms the guard, the solver is then asked for ``auto_asc_desc``, and
    turning the flag off changes the request to ``auto``. Also asserted: the direction
    written into the artefact agrees with the bin bad rates the table holds, so a table
    cannot describe a trend it does not have. See the module docstring for the guarantee
    this file deliberately does not claim.
    """
    cfg = _scorecard()
    binning_cfg = cfg.binning
    assert binning_cfg.enforce_monotonic_trend is True, (
        "config/scorecard.yaml no longer declares binning.enforce_monotonic_trend, so this "
        "test's subject has been switched off by a config edit"
    )

    requested: list[str] = []

    def _recording(**kwargs: object) -> OptimalBinning:
        requested.append(str(kwargs.get("monotonic_trend")))
        return OptimalBinning(**kwargs)

    monkeypatch.setattr("oxbow.scoring.binning.OptimalBinning", _recording)
    rng = _rng()
    rows = 500
    values = np.sort(rng.normal(0.0, 1.0, rows))
    # A noisy descending relationship, not a hard step. Measured on this host the mip
    # solver answers a deterministic step with `status='OPTIMAL', splits=[]` under
    # `auto_asc_desc` -- it would rather report one bin than violate the trend -- and a
    # one-bin table has no trend to record, which is the thing this test is about.
    labels = (values + rng.normal(0.0, 0.9, rows) < -0.2).astype(np.int32)

    fitted = _numeric_binning(binning_cfg, "monotone_signal", values, labels)
    assert requested and requested[-1] == "auto_asc_desc", (
        f"the solver was asked for monotonic_trend={requested[-1:]!r} while "
        "binning.enforce_monotonic_trend is true: the flag no longer reaches optbinning"
    )

    requested.clear()
    _numeric_binning(
        replace(binning_cfg, enforce_monotonic_trend=False), "monotone_signal", values, labels
    )
    assert requested and requested[-1] == "auto", (
        f"with the guard off the solver was still asked for {requested[-1:]!r}: the config "
        "key is decorative rather than load-bearing"
    )

    rates = [row.bad_rate for row in fitted.value_rows if row.population > 0]
    assert len(rates) >= 2, (
        f"the monotone feature came back as {len(rates)} bin, so there is no trend to record: "
        "optbinning returned no split and the boundary source is "
        f"{fitted.boundary_source!r}"
    )
    assert rates == sorted(
        rates, reverse=True
    ), f"a feature whose bads are its lowest values must read as a falling rate, got {rates}"
    assert (
        fitted.monotonic_direction == "descending"
    ), f"the artefact records {fitted.monotonic_direction!r} for the measured rates {rates}"

    noisy = _numeric_binning(binning_cfg, "noise", rng.normal(0.0, 1.0, rows), labels)
    noisy_rates = [row.bad_rate for row in noisy.value_rows if row.population > 0]
    rises = sum(1 for low, high in pairwise(noisy_rates) if high > low)
    falls = sum(1 for low, high in pairwise(noisy_rates) if high < low)
    # The direction label, counted off the measured rates rather than read back from the
    # function that writes it: a table that both rises and falls may not call itself monotone.
    expected_direction = (
        "single-bin"
        if len(noisy_rates) < 2
        else (
            "ascending"
            if rises and not falls
            else ("descending" if falls and not rises else "non-monotone-in-observed-rates")
        )
    )
    assert noisy.monotonic_direction == expected_direction, (
        f"rates {noisy_rates} rise {rises} times and fall {falls} times, which is "
        f"{expected_direction!r}, not {noisy.monotonic_direction!r}"
    )


def test_binning_reaches_the_mip_solver_and_not_the_quantile_fallback() -> None:
    """The guard behind §10's "monotonic optimal binning" is that the solver ran at all.

    This is a regression test for a defect that made the whole monotonic-trend promise
    decorative. ``binning.time_limit_seconds`` is a float in seconds because that is what
    a human tunes, but optbinning forwards it to ``pywraplp.Solver.SetTimeLimit``, whose
    argument is an ``int64_t`` of **milliseconds** -- so the constructor raised
    ``TypeError`` on every numeric feature. Two things then hid it: the ``except Exception``
    around the fit caught that TypeError exactly as it catches a solver that genuinely
    failed, and ``if binner.splits:`` raises ``ValueError: the truth value of an array
    with more than one element is ambiguous`` on any feature that found two or more
    boundaries, so the same fallback fired twice over. Every numeric bin table in this
    repository was therefore built from quantiles while the artifact said
    ``optbinning-mip`` would have been the honest label.

    Asserted here, in this order: the boundaries came from the solver, the record carries
    no unenforced-trend note, and the time limit reaches the solver as an integer number
    of milliseconds. A config value in seconds converted at the call site is the only
    place that unit difference can be settled, and it is where it was wrong.
    """
    cfg = _scorecard().binning
    rng = _rng()
    rows = 2000
    values = np.sort(rng.normal(0.0, 1.0, rows))
    labels = (values + rng.normal(0.0, 1.2, rows) > 1.2).astype(np.int32)

    requested: list[object] = []
    real = OptimalBinning

    def _recording(**kwargs: object) -> OptimalBinning:
        requested.append(kwargs.get("time_limit"))
        return real(**kwargs)  # type: ignore[call-arg]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("oxbow.scoring.binning.OptimalBinning", _recording)
        fitted = _numeric_binning(cfg, "monotone_signal", values, labels)

    assert fitted.boundary_source == BOUNDARY_SOURCE_BINNING, (
        f"numeric boundaries came from {fitted.boundary_source!r}, not the mip solver: the "
        "monotonic trend §10 promises is not being imposed, and the fallback's own note is "
        f"{[n for n in fitted.notes if 'quantile' in n]!r}"
    )
    assert not [note for note in fitted.notes if "could NOT be enforced" in note], fitted.notes
    assert requested, "the solver was never constructed, so nothing above was measured"
    limit = requested[-1]
    assert isinstance(limit, int) and not isinstance(limit, bool), (
        f"time_limit reached the solver as {limit!r} ({type(limit).__name__}); ortools' "
        "SetTimeLimit takes an int64_t of milliseconds and raises TypeError on a float, "
        "which the fallback used to swallow"
    )
    assert limit == int(
        round(cfg.time_limit_seconds * 1000)
    ), f"{limit} ms is not cfg.time_limit_seconds={cfg.time_limit_seconds} s"


def test_iv_admission_band() -> None:
    """0.02 <= IV <= 0.5: below the floor is excluded, above the ceiling is suspected leakage.

    The ceiling is the interesting half. Above 0.5 a feature says too much, and in
    financial-crime data the ordinary explanation is that the label leaked into it, so the
    default refusal is the guard and the only way through is a written justification long
    enough to argue with -- ``MIN_JUSTIFICATION_CHARACTERS``, not a word count.
    """
    cfg = _scorecard()
    admission = cfg.admission
    assert (admission.iv_min, admission.iv_max) == (0.02, 0.5), admission
    assert admission.above_max_action == "require_written_justification"
    assert admission.below_min_action == "exclude_and_record"

    rng = _rng()
    rows = 600
    labels = np.zeros(rows, dtype=np.int32)
    labels[rng.integers(0, 300, 120)] = 1
    shapes = {
        "flat": np.full(rows, 5.0),
        "mid": rng.normal(0.0, 1.0, rows) + 0.4 * labels,
        "high": rng.normal(0.0, 1.0, rows) + 1.2 * labels,
        "leak": rng.normal(0.0, 1.0, rows) + 3.0 * labels,
    }
    binnings: dict[str, FeatureBinning] = {}
    woe_columns: dict[str, np.ndarray] = {}
    for feature, values in shapes.items():
        fitted = _numeric_binning(cfg.binning, feature, values, labels)
        binnings[feature] = fitted
        woe_columns[feature] = woe_of_labels(fitted, assign_bins(values, fitted, cfg.binning))

    assert binnings["flat"].iv < admission.iv_min, binnings["flat"].iv
    assert admission.iv_min <= binnings["mid"].iv <= admission.iv_max, binnings["mid"].iv
    assert binnings["high"].iv > admission.iv_max, binnings["high"].iv

    outcome = select_features(
        binnings, woe_columns, labels, admission, cfg.fit.max_abs_correlation_with_label
    )
    decisions = {decision.feature: decision for decision in outcome.decisions}

    assert decisions["flat"].decision == DECISION_REFUSED_LOW_IV
    assert "iv_bounds.min" in decisions["flat"].rule
    assert "flat" not in outcome.admitted
    assert decisions["mid"].decision == DECISION_ADMITTED
    assert "mid" in outcome.admitted

    leakage = decisions["leak"]
    assert leakage.decision == DECISION_REFUSED_SUSPECTED_LEAKAGE, (
        f"IV {leakage.iv} above the ceiling was admitted as {leakage.decision!r} without a "
        "written justification"
    )
    assert "suspected leakage" in leakage.rule
    assert admission.above_max_action in leakage.rule
    assert leakage.justification is None
    assert "leak" not in outcome.admitted
    assert [decision.feature for decision in outcome.suspected_leakage()] == ["high", "leak"]

    written = "derived from a list sealed before the label window opens, not from the label"
    assert len(written) >= MIN_JUSTIFICATION_CHARACTERS
    justified = select_features(
        binnings,
        woe_columns,
        labels,
        admission,
        cfg.fit.max_abs_correlation_with_label,
        {"leak": written},
    )
    leak_row = {decision.feature: decision for decision in justified.decisions}["leak"]
    assert leak_row.decision == DECISION_REFUSED_JUSTIFIED_LEAKAGE, leak_row.decision
    assert "leak" in justified.admitted
    assert leak_row.justification == written, "the artefact must carry the text verbatim"
    assert (
        "high" not in justified.admitted
    ), "a justification written for one feature admitted another above the ceiling too"

    one_short = written[: MIN_JUSTIFICATION_CHARACTERS - 1]
    refused = select_features(
        binnings,
        woe_columns,
        labels,
        admission,
        cfg.fit.max_abs_correlation_with_label,
        {"leak": one_short},
    )
    assert "leak" not in refused.admitted, (
        f"a {len(one_short)}-character justification admitted a feature above the IV ceiling; "
        f"the declared floor is {MIN_JUSTIFICATION_CHARACTERS}"
    )

    payload = outcome.to_dict()
    assert payload["suspected_leakage_count"] == 2, payload["suspected_leakage_count"]


def test_separation_detected() -> None:
    """A feature that perfectly separates the classes fails the fit and names itself."""
    cfg = _scorecard()
    assert cfg.fit.detect_separation is True
    assert cfg.fit.separation_action == "fail_named"
    assert cfg.fit.separation_auc_threshold == 1.0

    frame = _tiny_frame()
    assert int(frame.data.get_column(COL_LABEL).sum()) > 0
    labels = frame.data.get_column(COL_LABEL).to_numpy()
    leaky = frame.data.get_column("leaky").to_numpy()
    assert np.array_equal(leaky, labels.astype(np.float64)), (
        "the fixture's leaky column must be the label itself: that is the shape the guard "
        "exists to refuse, and nothing subtler"
    )

    with pytest.raises(SeparationDetectedError, match="separation detected") as excinfo:
        fit_scorecard(frame, cfg, _tiny_registry())

    assert excinfo.value.feature_names == ("leaky",), excinfo.value.feature_names
    assert "leaky" in str(excinfo.value)
    message = str(excinfo.value)
    assert "fit.separation_auc_threshold=1.0" in message, message
    assert "AUC=" in message, message


def test_psi_action_degrades_scoring() -> None:
    """At the action threshold the score path drops to rules-plus-scorecard, with a banner."""
    cfg = _scorecard()
    drift_cfg = cfg.drift
    assert (drift_cfg.psi_watch, drift_cfg.psi_action) == (0.10, 0.25), drift_cfg
    assert drift_cfg.on_action == "degrade_to_rules_plus_scorecard"

    # Hand-computed on two bins: (0.2-0.5)*ln(0.2/0.5) + (0.8-0.5)*ln(0.8/0.5)
    # = 0.2748872196 + 0.1410010888 = 0.4158883083.
    psi, shifts = psi_from_shares({"a": 0.5, "b": 0.5}, {"a": 0.2, "b": 0.8})
    assert psi == pytest.approx(0.4158883083, abs=1e-9), psi
    assert [shift.bin_label for shift in shifts] == ["a", "b"]

    degraded = drift_decision(psi, (), drift_cfg)
    assert degraded.action == drift_cfg.on_action, degraded.action
    assert degraded.mode == ACTION_DEGRADED
    assert "DEGRADED" in degraded.banner
    assert f"{psi:.3f}" in degraded.banner, "the banner must quote the PSI that fired it"
    assert str(drift_cfg.psi_action) in degraded.banner
    assert "scorecard" in degraded.banner.lower()
    assert "gbm" in degraded.banner.lower()
    assert degraded.thresholds["psi_action"] == drift_cfg.psi_action
    assert degraded.expected_reference == drift_cfg.expected_reference

    watched = drift_decision(0.12, (), drift_cfg)
    assert watched.mode == ACTION_NORMAL, (
        f"a watch-level PSI of 0.12 degraded the run to {watched.mode!r}; the plan's "
        "degradation belongs at the action threshold only"
    )
    assert watched.action == "keep_full_model_stack"
    assert "DRIFT WATCH" in watched.banner and "DEGRADED" not in watched.banner
    assert "Full model stack remains in use" in watched.banner

    stable = drift_decision(0.04, (), drift_cfg)
    assert stable.mode == ACTION_NORMAL
    assert "No drift action" in stable.banner
    assert str(drift_cfg.psi_watch) in stable.banner

    # A feature-level breach degrades on its own: a stable aggregate over a bin that emptied
    # is precisely the drift a total PSI averages away.
    rng = _rng()
    rows = 600
    values = np.sort(rng.normal(0.0, 1.0, rows))
    # Noisy, so the mip solver actually partitions: the drift is measured across the bins a
    # fitted table holds, and a table the solver left as one bin has no bin to empty.
    labels = (values + rng.normal(0.0, 1.0, rows) > 0.8).astype(np.int32)
    fitted = _numeric_binning(cfg.binning, "flow", values, labels)
    assert len([row for row in fitted.value_rows if row.population > 0]) >= 2, (
        f"the drift fixture fitted to {fitted.boundary_source!r} with no partition to move "
        "between; the assertion below would then measure PSI over a single bin"
    )
    reference = fit_reference_shares(fitted)
    populated_labels = [row.label for row in fitted.rows if row.population > 0]
    collapsed = [populated_labels[0]] * (len(populated_labels) * 20)
    feature_drift = per_feature_drift(
        {"flow": fitted}, {"flow": reference}, {"flow": {1: collapsed}}, drift_cfg
    )
    assert feature_drift[0].psi >= drift_cfg.psi_action, feature_drift[0].psi
    assert feature_drift[0].status == STATUS_ACTION
    by_feature = drift_decision(0.0, feature_drift, drift_cfg)
    assert by_feature.mode == ACTION_DEGRADED
    assert by_feature.to_dict()["features_at_action"] == ["flow"]
    worst = feature_drift[0].max_csi_bin
    assert worst is not None and worst.bin_label == populated_labels[0], worst


def test_zero_positive_fold_reported() -> None:
    """A fold with no positives is skipped with a named reason, not dropped and not scored 0.

    Cross-reference: the same property at the metric primitive is already pinned by
    ``tests/unit/test_p6_metrics.py::test_pr_auc_undefined_with_zero_positives``, which
    asserts the backtest helper raises. This is the fold-plan half -- the walk-forward table
    a reader sees must still contain the fold, and must say why it carries no number.
    """
    rows_per_fold = 60
    folds = 5
    rows = rows_per_fold * folds
    fold_of_row = [index % folds for index in range(rows)]
    within_fold = [index % rows_per_fold for index in range(rows)]
    labels = np.zeros(rows, dtype=np.int8)
    # One positive in each fold's test slice -- except fold 3, whose test slice is clean.
    for index in range(rows):
        if within_fold[index] >= 55 and fold_of_row[index] != 3:
            labels[index] = 1
    assert int(labels.sum()) == 4 * folds, int(labels.sum())
    roles = [
        ROLE_TEST if position >= 51 else (ROLE_VALIDATION if position >= 45 else ROLE_TRAIN)
        for position in within_fold
    ]

    frame = _tiny_frame(rows=rows, folds=folds, labels=labels, roles=roles)
    plans = fold_plan_from_frame(frame, ROLE_TEST, embargo_days=30)

    assert len(plans.plans) == folds, "a fold vanished from the plan instead of being reported"
    fold_three = next(plan for plan in plans.plans if plan.fold == 3)
    assert fold_three.evaluation_rows > 0, "the slice is empty; that is a different reason"
    assert fold_three.evaluation_positives == 0
    assert fold_three.skipped
    assert fold_three.skip_reason == SKIP_ZERO_POSITIVES
    assert "0 positives" in (fold_three.skip_detail or ""), fold_three.skip_detail
    assert "undefined" in (fold_three.skip_detail or "")
    assert [plan.skip_reason for plan in plans.skipped] == [SKIP_ZERO_POSITIVES]
    assert [plan.fold for plan in plans.evaluated] == [0, 1, 2, 4]

    payload = plans.to_dict()
    assert payload["folds"] == folds
    assert payload["evaluated_folds"] == 4
    assert payload["skipped_fold_count"] == 1
    assert payload["skipped_folds"][0]["fold"] == 3
    assert payload["skipped_folds"][0]["skip_reason"] == SKIP_ZERO_POSITIVES

    # And the metric that would have been reported is undefined, not zero: 0.0 reads as a
    # terrible model, and this slice is an unmeasurable one.
    scores = np.linspace(0.1, 0.9, 20)
    assert math.isnan(average_precision(np.zeros(20, dtype=np.int32), scores)), (
        "average_precision returned a number for a zero-positive slice, so the fold's cell in "
        "the report would be 0.0 rather than absent"
    )
    assert fold_three.base_rate == 0.0
