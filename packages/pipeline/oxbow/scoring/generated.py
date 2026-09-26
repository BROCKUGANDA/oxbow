"""A deterministic training frame built from the declared feature registry.

WHY THIS EXISTS AND WHAT IT IS NOT. The real feature table is P2's output and is not
on disk yet, so P4 cannot be wired against it in this session. Rather than stub the
layer, this module builds a frame that satisfies the *agreed input contract* -- the
75 published entries of ``config/features.yaml``, their declared dtypes, the metadata
columns, the fold structure and the feature-spec hash -- so the fit, the guards, the
bands, the calibration branches and the fusion are exercised end to end and their
assertions are real.

It is a wiring instrument, not a corpus. Every statistic it produces is labelled
``generated_frame_seed_1337`` by the frame itself and travels with that label into
the artefacts; no number from here may be reported as measured on PaySim (plan §19
rule 7, and the honest-reporting rule of this phase). The headline metrics land in
P6 when the real feature table exists.

Four properties are deliberate because they are what the guards are for:

* **Graph-derived features are null for 85 % of rows.** That is DEV-011: PaySim is
  star-shaped, so on the primary tabular corpus "missing is a bin" is the normal
  case, not an edge case. A generator that filled those columns in would hide the
  one behaviour this phase most needs to prove. The rate is taken from each entry's
  declared ``null_policy``, not invented here.
* **Money columns are integer minor units** because DEV-005 says a frame carrying a
  float ``*_minor`` is a frame the contracts layer rejects. Generating them as floats
  would train this layer against a shape it can never legally see.
* **Two features carry enough signal that IV breaches 0.5**, so the admission ceiling
  visibly refuses them in the emitted artefact.
* **Part of the label is an interaction** between dormancy and a velocity spike. A
  linear WOE scorecard cannot represent it and a tree can, which makes the day-7
  "beats the scorecard" comparison a real comparison rather than a rigged one: on a
  corpus whose truth is linear, the GBM should *not* win.

``inject_separating_feature`` exists so a guard can be *proven to bite* (00 §B: a test
that does not fail when a deliberately leaking feature is introduced is decoration).
It is off by default, named in the frame's own notes when used, and no reported
number comes from a frame built with it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import numpy as np
import polars as pl

from oxbow.scoring.config import FeatureDeclaration, FeatureRegistry
from oxbow.scoring.frame import (
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    COL_FOLD,
    COL_LABEL,
    COL_LABEL_TYPOLOGY,
    COL_SPEC_HASH,
    PROVENANCE_GENERATED,
    canonical_spec_hash,
)

SEED: Final = 1337
TYPOLOGIES: Final = (
    "mule_layering",
    "gather",
    "scatter",
    "circular",
    "smurfing",
    "behaviour_break",
    "placement_extraction",
    "network_expansion",
    "layering_chain",
)

# DEV-011: the network substrate is absent on the primary tabular corpus. The rate
# comes from the entry's declared null_policy rather than from a guess here, so the
# generated frame answers the same question the real frame will answer.
MISSING_RATE_BY_POLICY: Final[dict[str, float]] = {
    "never_null": 0.0,
    "null_when_unobserved": 0.85,
    "null_when_no_rule_hit": 0.88,
    "null_when_zero_denominator": 0.18,
    "null_when_window_empty": 0.22,
    "null_when_indeterminate": 0.12,
    "null_without_prior_event": 0.10,
    "null_when_balance_absent": 0.25,
}

# Feature weights on the latent risk. Anything absent is pure noise, and the IV floor
# is expected to refuse it: a scorecard that admits noise is one nobody can defend
# when the coefficient wanders between runs.
SIGNAL_WEIGHTS: Final[dict[str, float]] = {
    "rule_r1_pass_through_severity": 1.15,
    "rule_r10_fast_cash_out_severity": 0.85,
    "rule_r2_fan_in_severity": 0.6,
    "rule_r3_fan_out_severity": 0.5,
    "rule_r4_cycle_severity": 0.55,
    "rule_r5_structuring_severity": 0.4,
    "rule_r7_dormant_severity": 0.45,
    "rule_r12_chain_severity": 0.35,
    "rule_severity_max": 1.45,
    "rule_hit_count": 1.05,
    "txn_count_in_24h": 0.5,
    "amount_in_24h_minor": 0.45,
    "net_flow_30d_minor": 0.3,
    "amount_robust_z_last": 0.5,
    "benford_deviation_30d": 0.3,
    "amount_repetition_30d_bps": 0.45,
    "drained_balance_share_30d_bps": 0.5,
    "balance_mismatch_share_30d_bps": 0.35,
    "new_counterparty_share_30d_bps": 0.4,
    "overnight_share_30d_bps": 0.2,
    "extract_velocity_bps": 0.6,
    "extractable_share_24h_bps": 0.5,
    "downstream_outflow_24h_minor": 0.4,
    "graph_effective_fan_in_30d": 0.4,
    "graph_effective_fan_out_30d": 0.35,
    "graph_cycle_participation_count": 0.3,
    "graph_local_density_30d_bps": 0.3,
    "graph_pagerank": 0.25,
    "graph_longest_chain_length": 0.3,
    "hours_since_last_drain": -0.55,
    "account_age_days": -0.4,
    "active_days_30d": -0.3,
    "interarrival_mean_30d_s": -0.25,
    "weekend_share_30d_bps": -0.2,
    "top_counterparty_share_30d_bps": -0.25,
    "repeat_visit_share_30d_bps": -0.2,
    "txns_per_active_day_30d_bps": 0.35,
}

# The interaction pair: the label depends on the product of these two, which a linear
# WOE scorecard cannot express. Each carries its own idiosyncratic component (zA/zB)
# as well as the shared latent, so the interaction is recoverable from the features.
INTERACTION_FEATURES: Final = ("dormancy_days_before_now", "rule_r6_velocity_severity")


@dataclass(frozen=True, slots=True)
class GeneratedFrameSpec:
    """Every knob of the generator, so a run is reproducible from the artefact."""

    seed: int
    n_accounts: int
    n_folds: int
    base_rate: float
    epoch_utc: datetime
    fold_stride_days: int
    interaction_share: float
    inject_separating_feature: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "n_accounts": self.n_accounts,
            "n_folds": self.n_folds,
            "base_rate": self.base_rate,
            "epoch_utc": self.epoch_utc.isoformat(),
            "fold_stride_days": self.fold_stride_days,
            "interaction_share": self.interaction_share,
            "inject_separating_feature": self.inject_separating_feature,
            "provenance": PROVENANCE_GENERATED,
        }


def default_spec(
    n_accounts: int = 4000,
    base_rate: float = 0.05,
    interaction_share: float = 0.55,
    inject_separating_feature: str | None = None,
) -> GeneratedFrameSpec:
    """The spec the P4 wiring runs use: seed 1337, five folds, 90-day steps."""
    return GeneratedFrameSpec(
        seed=SEED,
        n_accounts=n_accounts,
        n_folds=5,
        base_rate=base_rate,
        epoch_utc=datetime(2014, 1, 1, tzinfo=UTC),
        fold_stride_days=90,
        interaction_share=interaction_share,
        inject_separating_feature=inject_separating_feature,
    )


def _family(declaration: FeatureDeclaration, is_categorical: bool) -> str:
    """Which distribution family a declared entry is drawn from.

    Money-bearing entries come out as integer minor units (DEV-005) and basis-point
    shares as integers, because P2's registry declares them that way: generating a
    float ``amount_in_24h_minor`` would train this layer against a frame shape the
    contracts layer refuses at the boundary.
    """
    if is_categorical or declaration.categories is not None:
        return "categorical"
    if declaration.dtype == "bool":
        return "flag"
    if declaration.is_money:
        return "amount_minor"
    if declaration.is_basis_points:
        return "basis_points"
    if declaration.dtype == "float64":
        return "score" if declaration.name.startswith("rule_") else "float"
    return "count"


def _zero_inflated(name: str, family: str) -> float:
    """Structural-zero share: "no events of this kind happened" is a real answer.

    The plan is explicit that a structural zero is not a small number, so the
    generated frame must put exact zeros in front of the ``__zero__`` bin on the
    features where zero is the normal case rather than an edge case.
    """
    if family in {"score", "flag", "categorical", "float"}:
        return 0.0
    if name.startswith("rule_") or name in {
        "graph_cycle_participation_count",
        "graph_longest_chain_length",
        "drained_balance_count_30d",
        "reversal_linked_count_30d",
        "counterparties_first_seen_30d",
        "graph_self_transfer_count_30d",
        "zero_value_count_30d",
    }:
        return 0.5
    return 0.12


def generate_training_frame(
    registry: FeatureRegistry,
    categorical_features: tuple[str, ...],
    spec: GeneratedFrameSpec | None = None,
) -> pl.DataFrame:
    """Build a contract-shaped frame deterministically.

    The random stream is one ``numpy.random.Generator`` consumed in a fixed order
    (accounts, folds, then features in registry declaration order), so two calls with
    the same spec produce byte-identical parquet. Reordering this function's draws
    changes the artefact hash, which is the intended behaviour: the hash is the guard
    against an accidental change of the generator.
    """
    frame_spec = spec or default_spec()
    rng = np.random.default_rng(frame_spec.seed)
    n_accounts = frame_spec.n_accounts
    n_folds = frame_spec.n_folds
    rows = n_accounts * n_folds

    account_keys = [
        "ACC-" + hashlib.sha256(f"oxbow-generated:{index}".encode()).hexdigest()[:12].upper()
        for index in range(n_accounts)
    ]
    account_offsets = rng.random(n_accounts)

    # Latent per-account risk plus a per-fold shock: the drift guard needs the
    # distribution to move across periods, and here it moves by construction rather
    # than by luck.
    latent = rng.normal(0.0, 1.0, n_accounts)
    fold_shift = np.linspace(0.0, 0.45, n_folds)
    latent_col = np.repeat(latent, n_folds) + fold_shift[np.tile(np.arange(n_folds), n_accounts)]
    account_col = np.repeat(np.asarray(account_keys, dtype=object), n_folds)
    fold_col = np.tile(np.arange(n_folds, dtype=np.int64), n_accounts)

    z_first = rng.normal(0.0, 1.0, rows)
    z_second = rng.normal(0.0, 1.0, rows)
    interaction = frame_spec.interaction_share * np.maximum(z_first, 0.0) * np.maximum(z_second, 0.0)

    linear_part = np.zeros(rows, dtype=np.float64)
    for name in registry.names:
        linear_part += SIGNAL_WEIGHTS.get(name, 0.0) * latent_col

    noise = rng.normal(0.0, 1.0, rows)
    combined = linear_part + interaction + noise
    # The threshold is solved on the frame's own logits so the realised base rate lands
    # near spec.base_rate. Calibration branch selection depends on the positive count,
    # so a base rate that drifted with the noise would make the wiring run's branch an
    # accident rather than a design.
    threshold = float(np.quantile(combined, 1.0 - frame_spec.base_rate))
    labels = (combined > threshold).astype(np.int8)

    as_of = [
        frame_spec.epoch_utc
        + timedelta(days=int(frame_spec.fold_stride_days) * int(fold_value))
        + timedelta(microseconds=int(86_400_000_000 * (float(offset_value) % 1.0)))
        for offset_value, fold_value in zip(np.tile(account_offsets, n_folds), fold_col, strict=True)
    ]

    columns: dict[str, pl.Series] = {
        COL_ACCOUNT_KEY: pl.Series(COL_ACCOUNT_KEY, list(account_col), dtype=pl.String),
        COL_AS_OF_TS: pl.Series(COL_AS_OF_TS, as_of, dtype=pl.Datetime("us", "UTC")),
        COL_FOLD: pl.Series(COL_FOLD, fold_col, dtype=pl.Int64),
        COL_LABEL: pl.Series(COL_LABEL, labels, dtype=pl.Int8),
    }
    typology_picks = rng.integers(0, len(TYPOLOGIES), rows)
    columns[COL_LABEL_TYPOLOGY] = pl.Series(
        COL_LABEL_TYPOLOGY,
        [
            TYPOLOGIES[int(pick)] if int(flag) == 1 else None
            for pick, flag in zip(typology_picks, labels, strict=True)
        ],
        dtype=pl.String,
    )

    categorical_set = set(categorical_features)
    for name in registry.names:
        declaration = registry.declaration(name)
        family = _family(declaration, name in categorical_set)
        weight = SIGNAL_WEIGHTS.get(name, 0.0)
        idiosyncratic = rng.normal(0.0, 1.0, rows)
        if name == INTERACTION_FEATURES[0]:
            idiosyncratic = 1.25 * z_first + 0.4 * idiosyncratic
        elif name == INTERACTION_FEATURES[1]:
            idiosyncratic = 1.25 * z_second + 0.4 * idiosyncratic
        driven = 1.15 * weight * latent_col + idiosyncratic + rng.normal(0.0, 0.35, rows)

        if family == "categorical":
            columns[name] = _categorical_series(name, declaration, driven, rng, rows)
            continue

        if family == "flag":
            values: np.ndarray = (
                rng.random(rows) < np.clip(0.06 + 0.08 * driven, 0.001, 0.9)
            ).astype(np.int8)
        elif family == "count":
            values = np.maximum(np.round(3.0 + 4.0 * driven), 0.0).astype(np.int64)
        elif family == "amount_minor":
            values = np.maximum(np.round(np.exp(np.clip(9.0 + 0.85 * driven, 0.0, 20.0))), 0).astype(
                np.int64
            )
        elif family == "basis_points":
            values = np.clip(np.round(np.exp(np.clip(6.0 + 0.9 * driven, 0.0, 14.0))), 0, 10_000).astype(
                np.int64
            )
        elif family == "score":
            values = np.clip(np.exp(np.clip(0.85 * driven - 2.2, -12.0, 2.0)), 0.0, 1.0)
        else:
            values = np.clip(np.exp(np.clip(1.0 * driven - 2.0, -12.0, 6.0)), 0.0, 12.0)

        zero_share = _zero_inflated(name, family)
        if zero_share:
            values = np.where(rng.random(rows) < zero_share, 0, values)

        missing_rate = MISSING_RATE_BY_POLICY.get(declaration.null_policy, 0.06)
        if missing_rate:
            drop = rng.random(rows) < missing_rate
            boxed = np.asarray(values, dtype=object)
            boxed[drop] = None
            columns[name] = pl.Series(name, list(boxed))
        else:
            columns[name] = pl.Series(name, values.tolist())

    if frame_spec.inject_separating_feature is not None:
        target = frame_spec.inject_separating_feature
        if target not in registry.names:
            raise ValueError(
                f"inject_separating_feature {target!r} is not a published registry entry"
            )
        # A deliberate leak, and the only honest shape for it: the column *is* the
        # label. The separation guard must name it, so the test can prove the gate
        # bites rather than assert that it would.
        columns[target] = pl.Series(target, labels.astype(np.float64).tolist())

    columns[COL_SPEC_HASH] = pl.Series(
        COL_SPEC_HASH,
        [canonical_spec_hash(registry)] * rows,
        dtype=pl.String,
    )
    ordered = [COL_ACCOUNT_KEY, COL_AS_OF_TS, COL_FOLD, COL_LABEL, COL_LABEL_TYPOLOGY, *registry.names, COL_SPEC_HASH]
    return pl.DataFrame({name: columns[name] for name in ordered})


def _categorical_series(
    name: str, declaration: FeatureDeclaration, driven: np.ndarray, rng: np.random.Generator, rows: int
) -> pl.Series:
    """Sample a declared category, skewed by the latent so the category has evidence.

    Only declared values are emitted, in the declared dtype's type: an invented
    sentinel here would put a value in the frame that the real registry cannot
    produce. Unseen categories are introduced by the tests that exercise that guard,
    at scoring time, which is where they occur in production. The last declared level
    is drawn rarely on purpose, so the training tail the unseen bin inherits from is
    a real thin bin rather than an unreachable branch.
    """
    categories = list(declaration.categories) if declaration.categories else [0, 1, 2, 3]
    n_levels = len(categories)
    propensity = np.exp(np.clip(0.8 * driven, -6.0, 6.0))
    picks: list[object] = []
    for position in range(rows):
        probabilities = np.full(n_levels, 0.20 / max(n_levels - 1, 1))
        hot = int(np.clip(propensity[position] % (n_levels - 1), 0, n_levels - 2))
        probabilities[hot] += 0.80
        probabilities[-1] = 0.002
        picks.append(categories[int(rng.choice(n_levels, p=probabilities / probabilities.sum()))])
    return pl.Series(name, picks)


def generated_spec_payload(spec: GeneratedFrameSpec) -> dict[str, object]:
    """The generator's own settings, for the artefact's provenance block."""
    return spec.to_dict()


__all__ = [
    "INTERACTION_FEATURES",
    "MISSING_RATE_BY_POLICY",
    "SEED",
    "SIGNAL_WEIGHTS",
    "TYPOLOGIES",
    "GeneratedFrameSpec",
    "default_spec",
    "generate_training_frame",
    "generated_spec_payload",
]
