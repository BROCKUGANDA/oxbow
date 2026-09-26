"""The leakage gate: prove that no published value reads a row after its own cutoff.

WHY THIS IS LIBRARY CODE AND NOT ONLY A TEST. The mechanism has to be callable from CI,
from the run itself, and from a fixture that introduces a deliberately leaking feature —
and all three must be the *same* code, or the fixture proves the test bites rather than
proving the guard does. 00 §B: a leakage test that does not fail when a leaking feature
is introduced is decoration, not a gate.

THE MECHANISM: TRUNCATION INVARIANCE. A feature value at cutoff ``T`` is backward-only if
and only if recomputing the whole table with every row after ``T`` deleted yields
byte-identical values for the rows that survive. Forward-looking code cannot fake its way
through this: deleting the rows it reads changes what it returns. The check runs at
several cutoffs, including ones placed inside a busy part of the timeline, because a leak
that only appears at one boundary is still a leak.

WHAT IT COVERS AND WHAT IT DOES NOT. Row-anchored kernels are covered exhaustively,
because their inputs are all in the frame being truncated. Fold-scoped graph and rule
attributes are *not* covered by truncation — they arrive from another layer as a sealed
table — and are guarded structurally instead: ``oxbow.features.fold_scope`` refuses a
table fed edges past the fold's cutoff, refuses a table sealed for another fold, and
refuses a bare DataFrame at the argument position. The two mechanisms together are what
plan §8 asks for, and the docstrings in ``test_leakage.py`` say which arm proves what.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import polars as pl

from oxbow.features.build import FeatureTable, FeatureTableBuilder
from oxbow.features.kinds import ENTITY, EVENT_TS, TXN_ID
from oxbow.features.registry import FeatureRegistry

KEY_COLUMNS: Final = (TXN_ID, ENTITY)
MAX_REPORTED_EXAMPLES: Final = 5


class FutureReadError(AssertionError):
    """A feature value changed when rows after its own cutoff were deleted.

    Subclasses AssertionError because this is a property of the data that failed, and a
    gate that raises a generic runtime error gets wrapped, logged and continued past.
    """


@dataclass(frozen=True, slots=True)
class Violation:
    """One column that read past its cutoff at one probe point."""

    feature_id: str
    cutoff_ts: datetime
    differing_rows: int
    checked_rows: int
    example_keys: tuple[tuple[str, str], ...]
    example_before: str
    example_after: str

    def describe(self) -> str:
        return (
            f"feature {self.feature_id!r} read data after its own cutoff "
            f"{self.cutoff_ts.isoformat()}: {self.differing_rows} of {self.checked_rows} "
            f"surviving rows changed when rows beyond the cutoff were deleted "
            f"(e.g. {self.example_keys[0] if self.example_keys else '?'}: "
            f"{self.example_before} -> {self.example_after})"
        )


def _compare(
    full: pl.DataFrame, truncated: pl.DataFrame, columns: Sequence[str]
) -> dict[str, tuple[int, int, list[tuple[str, str]], str, str]]:
    """Column-wise comparison of the rows the truncated build still contains."""
    shared = full.select(KEY_COLUMNS).join(
        truncated.select(KEY_COLUMNS), on=list(KEY_COLUMNS), how="inner"
    )
    left = full.join(shared, on=list(KEY_COLUMNS), how="semi").select([*KEY_COLUMNS, *columns])
    right = truncated.select([*KEY_COLUMNS, *columns])
    joined = left.join(right, on=list(KEY_COLUMNS), how="inner", suffix="__trunc")
    report: dict[str, tuple[int, int, list[tuple[str, str]], str, str]] = {}
    checked = joined.height
    for column in columns:
        before = joined[column]
        after = joined[f"{column}__trunc"]
        differs = (before != after) & ~(before.is_null() & after.is_null())
        count = int(differs.sum())
        if not count:
            continue
        sample_rows = (
            joined.filter(differs)
            .select([*KEY_COLUMNS, column, f"{column}__trunc"])
            .head(MAX_REPORTED_EXAMPLES)
        )
        keys = [(str(row[0]), str(row[1])) for row in sample_rows.select(list(KEY_COLUMNS)).rows()]
        first = sample_rows.row(0)
        report[column] = (
            count,
            checked,
            keys,
            str(first[2]),
            str(first[3]),
        )
    return report


def truncation_invariance_violations(
    builder: FeatureTableBuilder,
    events: pl.DataFrame,
    *,
    cutoffs: Sequence[datetime],
    columns: Sequence[str],
    full: pl.DataFrame | None = None,
) -> list[Violation]:
    """Every column that fails truncation invariance at any cutoff, in probe order.

    ``full`` may be the already-computed frame: the audit path has it, and recomputing a
    million-row matrix to compare it against itself is a second, slower answer to a
    question already answered.
    """
    reference = builder.compute_frame(events) if full is None else full
    available = [column for column in columns if column in reference.columns]
    missing = sorted(set(columns) - set(available))
    if missing:
        raise FutureReadError(
            f"the gate was asked to check columns that were never computed: {missing}"
        )
    violations: list[Violation] = []
    for cutoff in cutoffs:
        kept = events.filter(pl.col(EVENT_TS) <= cutoff)
        if kept.height == 0 or kept.height == events.height:
            continue
        truncated = builder.compute_frame(kept)
        report = _compare(reference, truncated, available)
        for column, (count, checked, keys, before, after) in sorted(report.items()):
            violations.append(
                Violation(
                    feature_id=column,
                    cutoff_ts=cutoff,
                    differing_rows=count,
                    checked_rows=checked,
                    example_keys=tuple(keys),
                    example_before=before,
                    example_after=after,
                )
            )
    return violations


def assert_backward_only(
    builder: FeatureTableBuilder,
    events: pl.DataFrame,
    *,
    cutoffs: Sequence[datetime],
    columns: Sequence[str],
) -> int:
    """Raise unless every listed column is truncation-invariant at every cutoff.

    Returns the number of (column, cutoff) pairs actually checked, so a caller can see
    that the gate did work rather than passing on an empty comparison.
    """
    violations = truncation_invariance_violations(builder, events, cutoffs=cutoffs, columns=columns)
    checked = len(cutoffs) * len(columns)
    if violations:
        detail = "\n  ".join(
            violation.describe() for violation in violations[:MAX_REPORTED_EXAMPLES]
        )
        raise FutureReadError(
            f"leakage gate: {len(violations)} column/cutoff combination(s) out of {checked} "
            f"read past their own as-of cutoff.\n  {detail}"
        )
    return checked


DEFAULT_PROBE_COUNT: Final = 4


def audit_probe_cutoffs(
    events: pl.DataFrame, *, count: int = DEFAULT_PROBE_COUNT, registry: object = None
) -> tuple[datetime, ...]:
    """Deterministic interior cutoffs, spread across the observed timeline.

    The probes are evenly spaced by *distinct timestamp*, not by row, because a corpus
    with 90 % of its rows in one day would otherwise put every probe inside that day and
    leave the quiet tail — where a long window behaves differently — untested. Every probe
    is strictly interior so each has rows on both sides; a cutoff with nothing after it
    tests nothing.

    ``registry`` is accepted for call sites that have one handy and is deliberately not
    read: the probe placement is a property of the timeline, and letting a registry steer
    it would let a registry declare itself untestable.
    """
    del registry
    if count <= 0:
        raise FutureReadError(f"count must be positive, got {count}")
    distinct = events.get_column(EVENT_TS).drop_nulls().unique(maintain_order=True).sort()
    total = distinct.len()
    if total < 3:
        return ()
    step = total / (count + 1)
    seen: dict[datetime, None] = {}
    for index in range(count):
        position = min(total - 2, max(1, round(step * (index + 1))))
        value = distinct.item(position)
        if value is not None:
            seen[value] = None
    return tuple(sorted(seen))


def audited_columns(registry: FeatureRegistry) -> tuple[str, ...]:
    """The columns this gate must prove backward-only, and why the rest are not here.

    Matrix and intermediate columns: the intermediates are included because a dropped
    column still feeds published ones, and a leak in an intermediate reaches the model
    through the entry that reads it. Outcome-role columns are excluded *by role*, not by
    name — plan §11 defines exposure E_i as a forward quantity, so it must change under
    truncation and proving otherwise would be proving the registry wrong. The companion
    assertion, made against the published artifact in :func:`audit_no_future_reads`, is
    that none of them reached the matrix, which is the failure mode that matters.
    """
    return tuple(
        entry.id for entry in registry.entries if entry.role in {"feature", "intermediate"}
    )


@dataclass(frozen=True, slots=True)
class LeakageAudit:
    """What the gate measured. Carried out of a run so a green result is a number, not a mood."""

    cutoffs: tuple[datetime, ...]
    columns: tuple[str, ...]
    pairs_checked: int
    events: int
    outcome_columns_withheld: tuple[str, ...]

    def statement(self) -> str:
        withheld = len(self.outcome_columns_withheld)
        return (
            f"leakage gate: {len(self.columns)} column(s) x {len(self.cutoffs)} cutoff(s) = "
            f"{self.pairs_checked} truncation-invariance checks over {self.events} events, "
            f"{withheld} forward-looking outcome column(s) withheld from the matrix by role"
        )


def audit_no_future_reads(
    feature_table: FeatureTable,
    events: pl.DataFrame,
    registry: FeatureRegistry,
    *,
    cutoffs: Sequence[datetime] | None = None,
    columns: Sequence[str] | None = None,
    probe_count: int = DEFAULT_PROBE_COUNT,
) -> LeakageAudit:
    """Prove that no published feature value moves when every row after its cutoff is deleted.

    Plan §8's central claim, as an executable statement: recompute the whole table with
    each probe's future removed and require byte-identical values on the surviving rows.
    Raises :class:`FutureReadError` naming the column, the cutoff, how many rows moved and
    a before/after pair, because "the gate failed" does not tell anyone which window to go
    and fix.

    The already-built ``feature_table`` is not decorative: its published matrix is checked
    against a fresh recomputation over the same events first, so the artifact a model would
    be trained on is what gets audited rather than an internal frame that happens to agree
    with it.
    """
    builder = FeatureTableBuilder(registry)
    chosen_columns = tuple(columns) if columns is not None else audited_columns(registry)
    if not chosen_columns:
        raise FutureReadError("the registry publishes no columns, so the gate has nothing to prove")
    leaked_outcomes = sorted(set(registry.outcome_ids) & set(feature_table.matrix.columns))
    if leaked_outcomes:
        raise FutureReadError(
            f"outcome column(s) {leaked_outcomes} are published in the feature matrix; a "
            "forward-looking quantity as a model input is plan §8's named failure (spec §7.2)"
        )
    reference = builder.compute_frame(events)
    _assert_matrix_matches(reference, feature_table, registry)

    chosen = (
        tuple(cutoffs) if cutoffs is not None else audit_probe_cutoffs(events, count=probe_count)
    )
    if not chosen:
        raise FutureReadError(
            f"the event timeline at {EVENT_TS} has fewer than three distinct instants, so no "
            "cutoff has a past and a future to test; the gate refuses to report a pass it "
            "did not run"
        )
    violations = truncation_invariance_violations(
        builder, events, cutoffs=chosen, columns=list(chosen_columns), full=reference
    )
    if violations:
        detail = "\n  ".join(
            violation.describe() for violation in violations[:MAX_REPORTED_EXAMPLES]
        )
        raise FutureReadError(
            f"leakage gate: {len(violations)} column/cutoff combination(s) out of "
            f"{len(chosen) * len(chosen_columns)} read past their own as-of cutoff.\n  {detail}"
        )
    return LeakageAudit(
        cutoffs=chosen,
        columns=chosen_columns,
        pairs_checked=len(chosen) * len(chosen_columns),
        events=events.height,
        outcome_columns_withheld=registry.outcome_ids,
    )


def _assert_matrix_matches(
    reference: pl.DataFrame, feature_table: FeatureTable, registry: FeatureRegistry
) -> None:
    """The published matrix must equal the recomputed frame on the shared keys.

    Without this, the gate would certify the builder and say nothing about the artifact,
    and the difference is where an assembly bug lives.
    """
    matrix = feature_table.matrix
    keys = [TXN_ID, ENTITY]
    left = matrix.join(
        reference.select([*keys, *registry.matrix_ids]), on=keys, how="left", suffix="__recomputed"
    )
    if left.height != matrix.height:
        raise FutureReadError(
            f"the published matrix has {matrix.height} rows but only {left.height} match the "
            "recomputed frame's keys; rows were added or dropped during assembly"
        )
    for column in registry.matrix_ids:
        published = left[column]
        recomputed = left[f"{column}__recomputed"]
        differs = (published != recomputed) & ~(published.is_null() & recomputed.is_null())
        count = int(differs.sum())
        if count:
            raise FutureReadError(
                f"published column {column!r} disagrees with a fresh recomputation on {count} "
                "row(s); the matrix and the builder are not the same computation"
            )


__all__ = [
    "DEFAULT_PROBE_COUNT",
    "KEY_COLUMNS",
    "FutureReadError",
    "LeakageAudit",
    "Violation",
    "assert_backward_only",
    "audit_no_future_reads",
    "audit_probe_cutoffs",
    "audited_columns",
    "truncation_invariance_violations",
]
