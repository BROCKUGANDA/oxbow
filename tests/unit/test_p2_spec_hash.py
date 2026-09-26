"""One feature-spec digest, and the proof that the stale-spec guard bites (02 §B seam 3).

WHAT THE BUG WAS. Two modules hashed ``config/features.yaml``. The feature registry
(:func:`oxbow.features.registry.parse_registry`) hashed every declared parameter of every
entry -- id, window, aggregation, predicate, dtype, sentence, leakage note -- in declared
order, and stamped that digest onto the matrix. ``oxbow.scoring.frame.canonical_spec_hash``
hashed something else entirely: the *sorted names*, the categorical subset and the spec
version, taken from P4's narrower read of the same file. Two digests for one registry is not
"checked twice" -- it is a guard that can never pass. A frame built by the real feature layer
carries P2's digest, ``build_training_frame`` recomputed P4's, the two disagreed on every
input, and the check that exists to stop "trained on one spec, scored on another" would have
refused every honest frame while accepting only frames stamped by that same function. The
guard was not weak; it was inverted.

WHAT THESE TESTS PROVE.

1. **One digest.** Both layers report the same 64-hex digest for the shipped registry, and
   it is the value that arrives on a real feature table -- P4 delegates to P2's loader
   instead of re-deriving, so there is one thing to keep true.
2. **It moves on a single declaration.** Change one window, one transform, or swap one
   column for another, and the digest changes while nothing else about the frame does. A
   digest covering only the name list would accept a 30-day aggregate standing in a 7-day
   column's place; these are the tests that show it does not.
3. **A stale frame is refused.** A frame carrying the parent's digest -- stamped from a real
   build against the parent registry -- is refused once the registry has moved on, and the
   same frame is accepted by the registry it was built from. The refusal is about the
   registry moving, not about the frame's shape.

Money and determinism: no amount is a float here, timestamps come from the fixture's fixed
epoch, and nothing reads a clock.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Final

import polars as pl
import pytest
import yaml

from oxbow.config import find_repo_root, load_yaml
from oxbow.features.compute import (
    build_feature_table,
    registry_from_repo,
    registry_with_code_version,
    registry_with_declared_window,
    registry_with_entry_order_reversed,
    registry_with_extra_entry,
)
from oxbow.features.fakes import canonical_event, canonical_frame
from oxbow.features.kinds import ENTITY, EVENT_TS, TXN_ID
from oxbow.features.registry import (
    FeatureRegistry,
    RegistryError,
    hash_registry,
    parse_registry,
)
from oxbow.scoring.config import load_feature_registry
from oxbow.scoring.errors import FrameContractError
from oxbow.scoring.frame import (
    COL_FOLD,
    COL_LABEL,
    COL_LABEL_TYPOLOGY,
    COL_SPEC_HASH,
    PROVENANCE_REAL,
    build_training_frame,
    canonical_spec_hash,
)

REPO_ROOT: Path = find_repo_root()
N_FOLDS: Final = 5
VALIDATION_FRACTION: Final = 0.15
ROWS_PER_FOLD: Final = 4
CATEGORICAL: Final[tuple[str, ...]] = ()


def _moved_hash(registry: FeatureRegistry) -> str:
    """Re-hash a hand-mutated registry with the loader's own function.

    Never by hand: a variant carrying a pasted-forward digest would prove nothing about the
    guard, and :func:`oxbow.features.registry.hash_registry` is the sanctioned way to say
    "this declaration, hashed" (which is how ``compute.py``'s variants get their hashes too).
    """
    return hash_registry(
        registry.entries,
        spec_version=registry.spec_version,
        code_version=registry.code_version,
        semantics=registry.semantics,
    )


def _contract_frame(
    matrix: pl.DataFrame, registry: FeatureRegistry, spec_hash: str
) -> pl.DataFrame:
    """A minimal account-grain frame carrying a *real* build's digest.

    One row per ``(entity, event_ts)`` taken from the matrix, with the contract columns
    attached. The fold column is a fixture's arithmetic, not a split claim -- the walk-forward
    discipline has its own gate in ``tests/unit/test_p2_splits.py`` -- and it is spread evenly
    so ``assign_roles`` has a training slice and a test fold to separate, which is the part of
    the frame this file's assertion depends on.
    """
    anchors = (
        matrix.sort([EVENT_TS, TXN_ID, ENTITY])
        .unique(subset=[ENTITY, EVENT_TS], keep="last", maintain_order=True)
        .select([ENTITY, EVENT_TS, *registry.matrix_ids])
        .head(N_FOLDS * ROWS_PER_FOLD)
    )
    if anchors.height < N_FOLDS * 2:
        raise AssertionError(
            f"the fixture yielded only {anchors.height} account-instant rows; the frame needs "
            "at least two per fold or the role assignment is vacuous"
        )
    folds = [index % N_FOLDS for index in range(anchors.height)]
    return anchors.rename({ENTITY: "account_key", EVENT_TS: "as_of_ts"}, strict=True).with_columns(
        pl.Series(COL_FOLD, folds, dtype=pl.Int32),
        pl.Series(
            COL_LABEL, [1 if index % 6 == 0 else 0 for index in range(anchors.height)], pl.Int8
        ),
        pl.Series(COL_LABEL_TYPOLOGY, [None] * anchors.height, dtype=pl.String),
        pl.lit(spec_hash, dtype=pl.String).alias(COL_SPEC_HASH),
    )


@pytest.fixture(scope="module")
def events() -> pl.DataFrame:
    """Thirty-two events over six accounts and three weeks, with a zero-amount probe.

    Small on purpose: what is under test is a digest, and a build that computes 75 columns
    over a frame this size is already enough to stamp a real matrix.
    """
    rows: list[dict[str, object]] = []
    pairs = [
        ("acct_a", "acct_b"),
        ("acct_b", "acct_a"),
        ("acct_a", "acct_c"),
        ("acct_c", "acct_d"),
        ("acct_d", "acct_a"),
        ("acct_e", "acct_a"),
        ("acct_a", "acct_f"),
        ("acct_f", "acct_b"),
    ]
    for index in range(32):
        sender, receiver = pairs[index % len(pairs)]
        rows.append(
            canonical_event(
                f"s{index:03d}",
                index * 211,
                sender,
                receiver,
                0 if index == 7 else 1_000 + index * 137,
                txn_type="REVERSAL" if index == 12 else "PAYMENT",
                label_is_fraud=1 if index in (5, 19) else 0,
                label_typology="pass_through" if index in (5, 19) else None,
                src_balance_before_minor=200_000,
                dst_balance_before_minor=25_000,
                source_dataset="p2_spec_hash",
            )
        )
    return canonical_frame(rows)


@pytest.fixture(scope="module")
def built(events: pl.DataFrame) -> tuple[pl.DataFrame, FeatureRegistry, str]:
    """The matrix, the registry that produced it, and the digest the build stamped."""
    registry = registry_from_repo(REPO_ROOT)
    table = build_feature_table(events, registry)
    return table.matrix, registry, table.spec_hash


# --- 1. one digest --------------------------------------------------------


def test_both_layers_report_the_same_digest_for_the_shipped_registry() -> None:
    """The bug, stated as an assertion: two hashes of one file used to disagree."""
    declared = registry_from_repo(REPO_ROOT)
    view = load_feature_registry(REPO_ROOT)
    assert canonical_spec_hash(view) == declared.spec_hash, (
        "oxbow.scoring.frame and oxbow.features.registry disagree about the spec digest, so "
        "the mismatch guard refuses every honest frame"
    )
    assert len(declared.spec_hash) == 64


def test_a_real_feature_table_carries_the_digest_the_scoring_layer_recomputes(
    built: tuple[pl.DataFrame, FeatureRegistry, str],
) -> None:
    """Frame stamp and recomputed expectation are one string.

    Before the delegation this asserted the opposite, which is exactly why 02 §B seam 3
    could not pass on a matrix built by the feature layer.
    """
    _, registry, stamped = built
    assert stamped == registry.spec_hash == canonical_spec_hash(load_feature_registry(REPO_ROOT))


def test_the_digest_is_a_pure_function_of_the_file() -> None:
    """Two loads, one value. A digest that moved between reads could not guard anything."""
    assert registry_from_repo(REPO_ROOT).spec_hash == registry_from_repo(REPO_ROOT).spec_hash


# --- 2. it moves on a single declaration ----------------------------------


def test_changing_one_window_moves_the_digest_and_keeps_every_name() -> None:
    """Plan §8's hash covers the declared windows -- demonstrated, not claimed.

    The published column list is identical before and after, so a guard that hashed only
    names would accept a 30-day aggregate standing where a 7-day one was trained.
    """
    registry = registry_from_repo(REPO_ROOT)
    target = next(entry for entry in registry.matrix_entries if entry.window == "30d")
    narrowed = registry_with_declared_window(registry, target.id, "7d")
    assert narrowed.spec_hash != registry.spec_hash
    assert narrowed.matrix_ids == registry.matrix_ids
    assert canonical_spec_hash(
        replace(load_feature_registry(REPO_ROOT), spec_hash=narrowed.spec_hash)
    ) != canonical_spec_hash(load_feature_registry(REPO_ROOT))


def test_changing_one_transform_moves_the_digest() -> None:
    """A different aggregation over the same source, predicate and window is another feature."""
    registry = registry_from_repo(REPO_ROOT)
    entry = next(
        item for item in registry.entries if item.kind == "window_agg" and item.agg == "sum"
    )
    changed = replace(entry, agg="max", null_policy="null_when_window_empty", winsorise=False)
    variant = replace(
        registry,
        entries=tuple(changed if item.id == entry.id else item for item in registry.entries),
    )
    moved = replace(variant, spec_hash=_moved_hash(variant))
    assert moved.spec_hash != registry.spec_hash
    assert moved.matrix_ids == registry.matrix_ids


def test_a_different_column_set_moves_the_digest() -> None:
    """A column that enters the matrix changes the digest, and the loader bounds the count.

    Two halves, because they are two different claims: the *digest* must move when one
    published column becomes another (``registry_with_extra_entry`` re-hashers through the
    loader's own function, so the value is derived rather than pasted), and the registry
    itself refuses a 76th published feature, which is what stops "add a column, keep the
    hash" being a way to smuggle a leaking declaration through.
    """
    registry = registry_from_repo(REPO_ROOT)
    template = next(entry for entry in registry.matrix_entries if entry.kind == "row_flag")
    twin = replace(
        template,
        id="is_probe_row",
        sentence=(
            "Marks the rows this run treats as balance probes rather than as reversals, so "
            "the matrix can be asked which of two same-shaped columns produced a score."
        ),
    )
    swapped = replace(
        registry,
        entries=tuple(twin if item.id == template.id else item for item in registry.entries),
    )
    moved = replace(swapped, spec_hash=_moved_hash(swapped))
    assert moved.spec_hash != registry.spec_hash
    assert twin.id in moved.matrix_ids and template.id not in moved.matrix_ids

    appended = registry_with_extra_entry(registry, twin)
    assert appended.spec_hash != registry.spec_hash
    assert len(appended.matrix_ids) == len(registry.matrix_ids) + 1

    raw = load_yaml(REPO_ROOT / "config" / "features.yaml")
    features = [dict(entry) for entry in raw["features"] if isinstance(entry, dict)]
    extra = next(
        {**entry, "id": "is_probe_row"} for entry in features if entry.get("id") == template.id
    )
    with pytest.raises(RegistryError, match="60-75"):
        parse_registry({**raw, "features": [*features, extra]})


def test_declared_order_and_kernel_version_both_move_the_digest() -> None:
    """The same entries in another order, and the same entries under new code, are new specs."""
    registry = registry_from_repo(REPO_ROOT)
    assert registry_with_entry_order_reversed(registry).spec_hash != registry.spec_hash
    assert registry_with_code_version(registry, "p2.probe").spec_hash != registry.spec_hash


# --- 3. a stale frame is refused ------------------------------------------


def test_a_frame_carrying_the_parent_hash_is_refused_once_the_registry_moved_on(
    built: tuple[pl.DataFrame, FeatureRegistry, str],
) -> None:
    """The clause the leakage argument rests on, in both directions.

    Accepted by the registry it was built from; refused, naming both digests, by the one
    that moved on. A refusal that fired in both directions would be a shape complaint, not a
    spec guard, so the accepting half is part of the test rather than a nicety.
    """
    matrix, registry, stamped = built
    parent_view = load_feature_registry(REPO_ROOT)
    frame = _contract_frame(matrix, registry, stamped)

    accepted = build_training_frame(
        frame, parent_view, CATEGORICAL, PROVENANCE_REAL, VALIDATION_FRACTION, N_FOLDS
    )
    assert accepted.feature_spec_hash == registry.spec_hash
    assert accepted.n_rows == frame.height

    target = next(entry for entry in registry.matrix_entries if entry.window == "30d")
    moved = registry_with_declared_window(registry, target.id, "7d")
    with pytest.raises(FrameContractError, match="does not match the declared") as caught:
        build_training_frame(
            frame,
            replace(parent_view, spec_hash=moved.spec_hash),
            CATEGORICAL,
            PROVENANCE_REAL,
            VALIDATION_FRACTION,
            N_FOLDS,
        )
    message = str(caught.value)
    assert registry.spec_hash[:16] in message and moved.spec_hash[:16] in message


def test_a_frame_with_two_digests_is_refused(
    built: tuple[pl.DataFrame, FeatureRegistry, str],
) -> None:
    """One frame is one spec: a row stamped from a different build fails the contract."""
    matrix, registry, stamped = built
    frame = _contract_frame(matrix, registry, stamped)
    view = load_feature_registry(REPO_ROOT)
    tampered = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit("0" * 64))
        .otherwise(pl.col(COL_SPEC_HASH))
        .alias(COL_SPEC_HASH)
    )
    with pytest.raises(FrameContractError, match="one frame is one feature spec"):
        build_training_frame(
            tampered, view, CATEGORICAL, PROVENANCE_REAL, VALIDATION_FRACTION, N_FOLDS
        )


def test_the_scoring_view_refuses_a_registry_the_feature_layer_would_reject(
    tmp_path: Path,
) -> None:
    """Delegation is two-way: P4 cannot bless a file P2's loader would refuse.

    Otherwise "valid registry" would have two definitions again, one per layer, which is the
    same drift this module set out to remove.
    """
    raw = load_yaml(REPO_ROOT / "config" / "features.yaml")
    features = [dict(entry) for entry in raw["features"] if isinstance(entry, dict)]
    offender = next(entry for entry in features if entry.get("kind") == "window_agg")
    offender["window"] = "30"  # a bare number: days or hours is a factor of 168 apart
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "features.yaml").write_text(
        yaml.safe_dump({**raw, "features": features}), encoding="utf-8"
    )
    with pytest.raises(RegistryError, match="not a duration"):
        load_feature_registry(tmp_path)
