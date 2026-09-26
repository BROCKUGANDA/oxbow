"""The rule-hit ledger declares its schema; it is not whatever the first rule looked like.

WHY THIS FILE EXISTS. The score stage died on its first real grouped corpus with
`ComputeError: could not append value "aggregation" of type str to the builder` at
`cli.py`'s ledger frame. Nothing was wrong with the rules: R11's hits carry
`overlap_group="aggregation"` and most other rules carry `None`, polars inferred the
column from the first sampled rows, found only ungrouped hits there, typed it `Null`, and
refused the first grouped row that arrived. So the artifact's column types depended on
which rule happened to fire first -- and a ledger that only builds when the corpus is
shaped a certain way is not a ledger.

The fix is a declared schema (`_RULE_HIT_SCHEMA`). This file pins both halves: the types
are the ones the readers expect, and the ordering that used to kill the run is now just
another row order.
"""

from __future__ import annotations

import polars as pl
import pytest

from oxbow.cli import _RULE_HIT_SCHEMA, _rule_hits_frame
from oxbow.rules import RuleHit, Window


def _hit(
    *,
    rule_id: str,
    account: str,
    overlap_group: str | None,
    threshold_param: str,
    threshold_value: float,
) -> RuleHit:
    return RuleHit(
        rule_id=rule_id,
        rule_name=rule_id,
        account_key=account,
        severity=0.5,
        evidence={
            "observation": 0.25,
            "threshold_param": threshold_param,
            "threshold_value": threshold_value,
            "txn_ids": [f"{account}-t1"],
        },
        window=Window(start_us=1_700_000_000_000_000, end_us=1_700_000_003_600_000, label="w"),
        hit_signature=f"{rule_id}-{account}",
        overlap_group=overlap_group,
    )


def test_a_grouped_hit_after_only_ungrouped_ones_still_lands() -> None:
    """The exact shape that crashed: ungrouped rows first, then R11's group."""
    hits = [
        _hit(
            rule_id="R9",
            account="ACC-00000000000A",
            overlap_group=None,
            threshold_param="ratio",
            threshold_value=2.0,
        ),
        _hit(
            rule_id="R11",
            account="ACC-00000000000B",
            overlap_group="aggregation",
            threshold_param="m",
            threshold_value=3.0,
        ),
    ]

    frame = _rule_hits_frame(hits)

    assert frame.schema["overlap_group"] == pl.Utf8, frame.schema
    assert frame["overlap_group"].to_list() == [None, "aggregation"]
    assert frame.height == 2
    # The reverse order is the same frame: the schema cannot depend on which came first,
    # so the two orders must agree on dtype as well as on content.
    reversed_frame = _rule_hits_frame(list(reversed(hits)))
    assert reversed_frame.schema == frame.schema
    assert reversed_frame.sort("rule_id").rows() == frame.sort("rule_id").rows()


def test_the_ledger_columns_are_the_types_the_readers_ask_for() -> None:
    """A declared schema is only a guarantee if it is the right one, checked by name."""
    assert {
        "rule_id": pl.Utf8,
        "rule_name": pl.Utf8,
        "account_key": pl.Utf8,
        "severity": pl.Float64,
        "hit_signature": pl.Utf8,
        "overlap_group": pl.Utf8,
        "window_start_us": pl.Int64,
        "window_end_us": pl.Int64,
        "observation": pl.Float64,
        "threshold_param": pl.Utf8,
        "threshold_value": pl.Float64,
        "txn_ids": pl.Utf8,
    } == _RULE_HIT_SCHEMA
    frame = _rule_hits_frame(
        [
            _hit(
                rule_id="R2",
                account="ACC-00000000000C",
                overlap_group=None,
                threshold_param="tau_minor",
                threshold_value=527245,
            )
        ]
    )
    assert frame.schema == _RULE_HIT_SCHEMA
    assert frame["window_start_us"].dtype == pl.Int64
    assert frame["txn_ids"].to_list() == ["ACC-00000000000C-t1"]


def test_an_empty_ledger_still_has_a_schema() -> None:
    """A run with no hits writes the same table, not a zero-column frame the API cannot read."""
    frame = _rule_hits_frame([])
    assert frame.height == 0
    assert frame.schema == _RULE_HIT_SCHEMA


def test_the_frame_refuses_a_row_that_is_not_a_hit_rather_than_guessing() -> None:
    """The schema is a contract: a row without the fields fails loudly, not as nulls."""
    with pytest.raises(AttributeError, match="rule_id"):
        _rule_hits_frame([object()])  # type: ignore[list-item]
