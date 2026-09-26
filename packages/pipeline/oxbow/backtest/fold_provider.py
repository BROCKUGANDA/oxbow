"""Adapter from the ONE splits module to the harness's fold protocol.

WHY THIS FILE EXISTS: plan §8 keeps fold computation in exactly one module
(``oxbow.backtest.splits``, owned by the feature layer), and the task's rule is that I
*import* it and never rewrite it. But that module's ``Fold`` exposes polars *expressions*
and timestamps, while the harness consumes boolean masks so a hand-built fake can drive
it with no polars machinery (see ``HarnessFold``). This thin adapter is the bridge: it
holds a ``SplitPlan`` and evaluates the splits module's own mask expressions against the
corpus, emitting ``HarnessFold`` values. It adds no fold arithmetic of its own — if it
did, there would be two places computing boundaries, which is the defect plan §8 names
"the single most common way a backtest becomes fiction".

The splits import is done by the caller (``run.py`` passes a built ``SplitPlan``), so
this module has no import-time dependency on the feature layer and stays unit-testable
against a stubbed plan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl

from oxbow.backtest.interfaces import HarnessFold

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps runtime decoupled from P2
    from oxbow.backtest.splits import SplitPlan


class SplitsFoldProvider:
    """A ``FoldProvider`` backed by the real, purged, embargoed ``SplitPlan``.

    ``as_of_column`` names the corpus timestamp the splits expressions are evaluated
    against (the feature table's as-of), so the same plan drives both the transaction
    timeline and the per-account backtest corpus without recomputing boundaries.
    """

    def __init__(self, plan: SplitPlan, *, as_of_column: str = "as_of_ts") -> None:
        self._plan = plan
        self._as_of_column = as_of_column

    def folds(self, corpus: pl.DataFrame) -> list[HarnessFold]:
        """Materialise each split fold's masks against ``corpus`` row order."""
        harness_folds: list[HarnessFold] = []
        for fold in self._plan.folds:
            harness_folds.append(
                HarnessFold(
                    index=fold.index,
                    train_mask=self._evaluate(corpus, fold.train_rows_mask(self._as_of_column)),
                    validation_mask=self._evaluate(
                        corpus, fold.validation_mask(self._as_of_column)
                    ),
                    test_mask=self._evaluate(corpus, fold.test_mask(self._as_of_column)),
                    embargo_days=self._plan.embargo_days,
                )
            )
        return harness_folds

    def embargo_days(self) -> int:
        """The plan's embargo, equal to the longest feature lookback (asserted in splits)."""
        return int(self._plan.embargo_days)

    def _evaluate(self, corpus: pl.DataFrame, expression: pl.Expr) -> tuple[bool, ...]:
        """Evaluate a splits mask expression into a boolean tuple aligned to the frame."""
        column = corpus.select(expression.cast(pl.Boolean).alias("_mask")).get_column("_mask")
        values = column.to_list()
        if any(value is None for value in values):
            # A null mask means the corpus lacks the timestamp the expression reads; a
            # silent False would fold rows into the wrong side, so it fails loud.
            raise ValueError(
                f"splits mask is null for some rows; the corpus is missing a non-null "
                f"{self._as_of_column!r}"
            )
        return tuple(bool(value) for value in values)


__all__ = ["SplitsFoldProvider"]
