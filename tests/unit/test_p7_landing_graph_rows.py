"""P7 — the three graph tables, landed from the graph artifact's own frames.

`oxbow.cli._graph_tables` reads `out/graph/<run_id>/nodes.parquet` and `pairs.parquet` plus that
run's `manifest.json`, then calls the only three writers those tables have: :func:`community_rows`
(`landing.py:2185`), :func:`account_membership_rows` (`landing.py:2275`) and
:func:`graph_edge_rows` (`landing.py:2322`). None of them had a test before this file, and the
tables they fill are the ones the network explorer and the community pane read.

Every expected number below is arithmetic done on paper against the fixture written beside it
(§19 rule 6) — the community sizes, the canonical indices, the internal minor-unit sums, the edge
counts — and no fixture is a copy of an artifact, so a mapper change that moves a number moves a
test. Nothing here reads a clock, a file, or Postgres: the builders are pure functions of two
polars frames and the manifest dict, which is what lets the refusal paths be tested at all.

What each builder exists to refuse is its own docstring's claim, restated as a shape that fails:

* a community whose algorithm and seed nobody recorded. `community.algorithm` and
  `community.seed` are NOT NULL (`models.py:568`), and the only place they exist is
  `settings_fingerprint.community`. A run without that block must raise, because
  `algorithm="leiden"` would be a guess about reproducibility dressed up as a measurement.
* a raw community label the remap does not know. `index_by_raw` turns the artifact's raw labels
  into contiguous warehouse ids; defaulting an unknown label to `0` repaints that membership onto
  the *biggest* community, and nothing in the schema would catch it.
* an edge endpoint the nodes frame never measured. `graph_edge.is_rail` is read off
  `nodes.is_rail`, so "not a rail" for a node that is absent from the frame is a claim about a
  node nobody measured, and it must refuse instead.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import polars as pl
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from oxbow.adapters.warehouse.landing import (  # noqa: E402
    CANONICAL_COMMUNITY_ORDER,
    GRAPH_EDGE_SOURCES,
    NODE_COMMUNITY_COLUMN,
    LandingError,
    account_membership_rows,
    community_index_by_raw_label,
    community_rows,
    graph_edge_rows,
)

#: The manifest's `settings_fingerprint.community` values for the run this fixture stands for.
ALGORITHM: Final = "leiden"
SEED: Final = 4242

# Twelve characters each, which is `account_key CHAR(12)` exactly (`models.py:51`).
A1: Final = "GRP0000000A1"
A2: Final = "GRP0000000A2"
A3: Final = "GRP0000000A3"
A4: Final = "GRP0000000A4"
A5: Final = "GRP0000000A5"
A6: Final = "GRP0000000A6"
A7: Final = "GRP0000000A7"
#: Not in the nodes frame at all — the artifact knows an edge to it but measured no node there.
A9: Final = "GRP0000000A9"

#: epoch microseconds of 2023-11-14T22:13:20Z: 1_700_000_000 s x 1e6.
BASE_US: Final = 1_700_000_000_000_000
BASE_MOMENT: Final = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
PLUS_60S_MOMENT: Final = datetime(2023, 11, 14, 22, 14, 20, tzinfo=UTC)

NODE_SCHEMA: Final = {
    "account": pl.Utf8,
    NODE_COMMUNITY_COLUMN: pl.Int64,
    "total_degree": pl.Int64,
    "is_rail": pl.Boolean,
    "pagerank": pl.Float64,
}
#: (account, community_id, total_degree, is_rail, pagerank).
NODE_ROWS: Final = [
    [A1, 7, 3, True, 0.30],
    [A2, 7, 1, False, 0.10],
    [A3, 3, 5, False, 0.25],
    [A4, 3, 2, False, 0.05],
    [A5, 3, 1, False, 0.02],
    [A6, None, 0, False, None],
]
PAIRS_SCHEMA: Final = {
    "account_from": pl.Utf8,
    "account_to": pl.Utf8,
    "currency": pl.Utf8,
    "edge_count": pl.Int64,
    "total_value_minor": pl.Int64,
    "first_ts_us": pl.Int64,
    "last_ts_us": pl.Int64,
    "is_self_pair": pl.Boolean,
}
#: (account_from, account_to, currency, edge_count, total_value_minor, first_ts_us, last_ts_us,
#: is_self_pair).
PAIRS_ROWS: Final = [
    [A1, A2, "UGX", 4, 500, BASE_US, BASE_US + 60_000_000, False],
    [A2, A1, "UGX", 2, 300, BASE_US + 1_000_000, BASE_US + 2_000_000, False],
    [A3, A4, "UGX", 3, 1000, BASE_US + 3_000_000, BASE_US + 4_000_000, False],
    [A3, A1, "UGX", 1, 700, BASE_US + 5_000_000, BASE_US + 5_000_000, False],
    [A4, A4, "UGX", 5, 90, BASE_US + 6_000_000, BASE_US + 9_000_000, True],
]


def _nodes(
    *,
    shuffled: bool = False,
    only: tuple[str, ...] = (),
    with_pagerank: bool = True,
    community_dtype: Any = None,
) -> pl.DataFrame:
    """The six-node artifact frame, optionally reversed, subset, or without the pagerank column."""
    rows = [row for row in NODE_ROWS if not only or row[0] in only]
    if shuffled:
        rows = rows[::-1]
    schema = (
        dict(NODE_SCHEMA)
        if community_dtype is None
        else {
            **NODE_SCHEMA,
            NODE_COMMUNITY_COLUMN: community_dtype,
        }
    )
    frame = pl.DataFrame(rows, schema=schema, orient="row")
    return frame.drop("pagerank") if not with_pagerank else frame


def _pairs(*, shuffled: bool = False, keep: tuple[str, ...] = ()) -> pl.DataFrame:
    """The five-pair artifact frame; `keep` drops aggregate columns the way a thin artifact does."""
    rows = list(PAIRS_ROWS)
    if shuffled:
        rows = rows[::-1]
    frame = pl.DataFrame(rows, schema=PAIRS_SCHEMA, orient="row")
    return frame if not keep else frame.select(list(keep))


def _edges(rows: list[list[Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=PAIRS_SCHEMA, orient="row")


def _stats(*, drop: tuple[str, ...] = (), **overrides: Any) -> dict[str, Any]:
    """The manifest's stats block, the only place algorithm and seed are ever written."""
    community_cfg: dict[str, Any] = {
        "algorithm": ALGORITHM,
        "seed": SEED,
        "n_iterations": 3,
        "resolution": 1.0,
        "canonical_order": CANONICAL_COMMUNITY_ORDER,
    }
    community_cfg.update(overrides)
    for key in drop:
        community_cfg.pop(key, None)
    return {
        "community_count": 2,
        "edge_count": 11,
        "settings_fingerprint": {
            "community": community_cfg,
            "page_rank": {"alpha": 0.85, "max_iter": 200},
        },
    }


# --- community_rows: the canonical remap, and the money one community holds ----------------


def test_the_two_communities_the_artifact_measured_land_in_the_canonical_order() -> None:
    # Raw 3 has three members (A3, A4, A5) and raw 7 has two (A1, A2), so size descending puts
    # raw 3 at canonical_index 0 and raw 7 at 1.  Internal money counts same-community pairs only:
    #   raw 3: A3->A4 (1000) + A4->A4 (90) = 1090.  A3->A1 is 3 vs 7, so it is not internal.
    #   raw 7: A1->A2 (500) + A2->A1 (300) = 800.
    rows, refused = community_rows(_nodes(), _pairs(), stats=_stats())

    assert refused == [], f"every figure the fixture records is present: {refused}"
    assert rows == [
        {
            "canonical_index": 0,
            "raw_label": "3",
            "size": 3,
            "total_minor": 1090,
            "currency": "UGX",
            "algorithm": ALGORITHM,
            "seed": SEED,
        },
        {
            "canonical_index": 1,
            "raw_label": "7",
            "size": 2,
            "total_minor": 800,
            "currency": "UGX",
            "algorithm": ALGORITHM,
            "seed": SEED,
        },
    ], "dict equality is the point: no `density` key is invented, and no measured key is dropped"
    assert isinstance(rows[0]["total_minor"], int), "money is integer minor units (DEV-005)"


def test_the_remap_is_a_function_of_the_partition_not_of_the_row_order() -> None:
    index_by_raw, label_rows, label_refusals = community_index_by_raw_label(_nodes(shuffled=True))
    shuffled, refused = community_rows(_nodes(shuffled=True), _pairs(shuffled=True), stats=_stats())
    ordered, _ = community_rows(_nodes(), _pairs(), stats=_stats())

    assert label_refusals == [] and index_by_raw == {3: 0, 7: 1}, index_by_raw
    assert [row["canonical_index"] for row in label_rows] == [0, 1]
    assert [row["size"] for row in label_rows] == [3, 2], "3 members then 2, size descending"
    assert (
        refused == [] and shuffled == ordered
    ), "reversing both input frames cannot change one byte of the community table"


def test_equal_sized_communities_are_ordered_by_their_smallest_account_key() -> None:
    # Both communities have two members, so the tie-break is the smallest account key ascending:
    # raw 7 holds A1 (first) and raw 9 holds A4/A5, so raw 7 is canonical_index 0.
    nodes = _nodes().with_columns(
        pl.when(pl.col("account").is_in([A1, A2]))
        .then(7)
        .when(pl.col("account").is_in([A3, A4]))
        .then(9)
        .otherwise(None)
        .cast(pl.Int64)
        .alias(NODE_COMMUNITY_COLUMN)
    )

    index_by_raw, label_rows, label_refusals = community_index_by_raw_label(nodes)
    rows, refused = community_rows(nodes, _pairs(), stats=_stats())

    assert label_refusals == [] and refused == []
    assert index_by_raw == {7: 0, 9: 1}, f"size ties break on the min account key: {index_by_raw}"
    assert [(row["canonical_index"], row["raw_label"], row["size"]) for row in rows] == [
        (0, "7", 2),
        (1, "9", 2),
    ]


def test_a_community_whose_internal_edges_carry_two_currencies_reports_no_total_and_names_them() -> (
    None
):
    money = _edges(
        [
            [A1, A2, "UGX", 4, 500, BASE_US, BASE_US, False],
            [A2, A1, "UGX", 2, 300, BASE_US, BASE_US, False],
            [A1, A2, "USD", 1, 250, BASE_US, BASE_US, False],
            [A3, A4, "UGX", 3, 1000, BASE_US, BASE_US, False],
        ]
    )

    rows, refused = community_rows(_nodes(), money, stats=_stats())

    by_raw = {row["raw_label"]: row for row in rows}
    assert (
        by_raw["7"]["total_minor"] is None and by_raw["7"]["currency"] is None
    ), "500+300 UGX and 250 USD would have to be summed across an exchange rate nobody declared"
    assert (
        by_raw["3"]["total_minor"] == 1000 and by_raw["3"]["currency"] == "UGX"
    ), "the community the mixed money does not touch lands unchanged"
    assert refused == [
        "community 7: its internal edges carry 2 currencies (UGX, USD); one money column holds "
        "one currency, so the total is left unset rather than summed across an exchange rate "
        "nobody declared"
    ], refused


def test_a_community_with_no_internal_pair_at_all_is_zero_minor_units_and_names_no_currency() -> (
    None
):
    """Nothing was measured inside it, so the sum over an empty set is 0 and the currency is None."""
    rows, refused = community_rows(
        _nodes(), _pairs(keep=("account_from", "account_to")), stats=_stats()
    )

    assert refused == [], refused
    assert [(row["total_minor"], row["currency"]) for row in rows] == [(0, None), (0, None)]
    assert all(row["algorithm"] == ALGORITHM and row["seed"] == SEED for row in rows)


# --- community_rows: the reproducibility gate ------------------------------------------------


def test_a_manifest_without_community_algorithm_and_seed_raises_rather_than_naming_an_algorithm() -> (
    None
):
    """`algorithm`/`seed` are NOT NULL and the artifact holds them nowhere else: no default exists."""
    nodes, pairs = _nodes(), _pairs()

    for label, stats in (
        ("stats=None", None),
        ("no stats keys at all", {}),
        ("fingerprint without a community block", {"settings_fingerprint": {"page_rank": {}}}),
        ("community block with no settings", {"settings_fingerprint": {"community": []}}),
        ("algorithm but no seed", _stats(drop=("seed",))),
        ("seed but no algorithm", _stats(drop=("algorithm",))),
        ("seed serialised as JSON text", _stats(seed=str(SEED))),
        ("seed serialised as a boolean", _stats(seed=True)),
        ("algorithm longer than the VARCHAR(32)", _stats(algorithm="leiden" + "x" * 27)),
    ):
        with pytest.raises(LandingError, match="no community algorithm or seed") as caught:
            community_rows(nodes, pairs, stats=stats)
        message = str(caught.value)
        assert "manifest.json" in message, f"{label}: the fix has to be named: {message}"
        assert ALGORITHM not in message, f"{label}: the mapper invented {ALGORITHM!r}: {message}"


def test_a_manifest_declaring_a_different_canonical_order_raises_before_it_can_relabel() -> None:
    with pytest.raises(LandingError, match="declares canonical_order") as caught:
        community_rows(_nodes(), _pairs(), stats=_stats(canonical_order="membership_then_id"))
    message = str(caught.value)
    assert "membership_then_id" in message and CANONICAL_COMMUNITY_ORDER in message, message
    assert "disagree" in message, f"the refusal says what would break: {message}"


def test_a_run_that_partitioned_nothing_returns_the_named_reason_before_the_algorithm_gate() -> (
    None
):
    """No community rows means nothing to describe, so the missing algorithm is not the story."""
    unplaced = _nodes().with_columns(pl.lit(None).cast(pl.Int64).alias(NODE_COMMUNITY_COLUMN))

    rows, refused = community_rows(unplaced, _pairs(), stats=None)

    assert (
        rows == [] and len(refused) == 1
    ), "the absence of a partition is a measurement, and it is reported instead of raising"
    assert "every node's community_id is null" in refused[0]
    assert "community_skipped_reason" in refused[0], refused[0]


def test_a_community_label_the_remap_cannot_read_never_lands_as_canonical_index_zero() -> None:
    """Raw labels are ints; a frame that carries them as text remaps to nothing and says so.

    `community_index_by_raw_label` cannot number a label :func:`_integer` cannot read, so no row
    is written — and what must never happen is a row invented at index 0 for a community nobody
    numbered. An empty community table with an EMPTY refusal list is the other half of the same
    defect: it reads as a graph that found no communities. Each unreadable group is therefore
    named, with its size and the artifact that would carry it.
    """
    textual = _nodes(community_dtype=pl.Utf8)

    index_by_raw, label_rows, label_refusals = community_index_by_raw_label(textual)
    rows, refused = community_rows(textual, _pairs(), stats=_stats())

    assert index_by_raw == {} and label_rows == []
    assert rows == [], "a canonical_index here would be a numbering over unreadable labels"
    assert label_refusals == refused
    assert len(refused) == 2, f"one reason per unreadable group, not one for the table: {refused}"
    for message in refused:
        assert "has to be an integer label" in message
        assert "out/graph/<run>/nodes.parquet" in message
    assert any("3 node(s)" in message for message in refused), refused
    assert any("2 node(s)" in message for message in refused), refused


# --- account_membership_rows: the same numbering, per node ------------------------------------


def test_each_placed_node_lands_under_the_index_the_community_table_uses() -> None:
    index_by_raw, _, _ = community_index_by_raw_label(_nodes())
    rows, refused = account_membership_rows(_nodes(), index_by_raw)

    assert refused == [], f"every node the graph placed is fully measured: {refused}"
    # A1 and A2 carry the artifact's raw 7, which the remap numbers 1; A3/A4/A5 carry raw 3 -> 0.
    # A6 has no community, so it has no border colour and lands no row (and refuses none).
    assert rows == [
        {"account_key": A1, "community_id": 1, "degree": 3, "pagerank": 0.30},
        {"account_key": A2, "community_id": 1, "degree": 1, "pagerank": 0.10},
        {"account_key": A3, "community_id": 0, "degree": 5, "pagerank": 0.25},
        {"account_key": A4, "community_id": 0, "degree": 2, "pagerank": 0.05},
        {"account_key": A5, "community_id": 0, "degree": 1, "pagerank": 0.02},
    ]


def test_a_membership_pointing_at_a_community_the_run_never_wrote_refuses_not_at_zero() -> None:
    """A dangling colour, not a fact: raw 7 is in the nodes frame but not in this run's table."""
    # The index is derived honestly: number communities from the three placed nodes only, so the
    # run wrote raw 3 (index 0) and never wrote raw 7.  A1/A2 still carry raw 7.
    index_by_raw, _, _ = community_index_by_raw_label(_nodes(only=(A3, A4, A5, A6)))
    assert index_by_raw == {3: 0}, index_by_raw

    rows, refused = account_membership_rows(_nodes(), index_by_raw)

    assert refused == [
        f"membership {A1}: community 7 is not in the run's community table",
        f"membership {A2}: community 7 is not in the run's community table",
    ], "the refusal names the node and the raw label it could not place"
    assert [row["account_key"] for row in rows] == [A3, A4, A5]
    assert all(
        row["community_id"] == 0 for row in rows
    ), "raw 7 must not appear at all; defaulting it to 0 would paint A1 and A2 onto raw 3's row"


def test_no_communities_means_no_memberships_and_says_which_before_any_column_gate() -> None:
    rows, refused = account_membership_rows(_nodes().drop("total_degree"), {})

    assert rows == []
    assert refused == [
        "account_membership: the run detected no communities, so no node is a member"
    ]


def test_membership_rows_are_sorted_by_account_key_whatever_order_the_nodes_arrived_in() -> None:
    index_by_raw, _, _ = community_index_by_raw_label(_nodes(shuffled=True))
    rows, refused = account_membership_rows(_nodes(shuffled=True), index_by_raw)

    assert refused == []
    assert [row["account_key"] for row in rows] == [
        A1,
        A2,
        A3,
        A4,
        A5,
    ], "the table's key is (run_id, account_key); a shuffled artifact cannot shuffle the sequence"


def test_a_key_too_long_for_char_12_and_an_unmeasured_degree_refuse_their_own_rows() -> None:
    # CHAR(12): "BIGKEY1234567890" is 16 characters, so the row would be cut mid-key; the refusal
    # quotes it cut to 12.  A7 has a community but no degree, and `degree` is NOT NULL.
    nodes = pl.DataFrame(
        [
            ["BIGKEY1234567890", 7, 2, False, 0.1],
            [A7, 7, None, False, 0.2],
            [A1, 7, 3, True, 0.30],
        ],
        schema=NODE_SCHEMA,
        orient="row",
    )

    rows, refused = account_membership_rows(nodes, {7: 0})

    assert rows == [{"account_key": A1, "community_id": 0, "degree": 3, "pagerank": 0.30}]
    assert refused == [
        "membership BIGKEY123456: no 12-character key or no total degree measured",
        f"membership {A7}: no 12-character key or no total degree measured",
    ], refused


def test_a_degree_serialised_as_a_float_refuses_because_no_rounding_rule_was_measured() -> None:
    nodes = pl.DataFrame(
        [[A1, 7, 3.0, True, 0.30], [A2, 7, 1.5, False, 0.10]],
        schema={**NODE_SCHEMA, "total_degree": pl.Float64},
        orient="row",
    )

    rows, refused = account_membership_rows(nodes, {7: 0})

    assert rows == [], "an integer degree column holding 3.0 claims a degree nobody counted"
    assert len(refused) == 2 and all(
        "no total degree measured" in line for line in refused
    ), refused


def test_a_node_the_graph_never_pageranked_lands_the_row_without_the_key() -> None:
    index_by_raw, _, _ = community_index_by_raw_label(_nodes(only=(A1, A2)))

    no_column, refused = account_membership_rows(
        _nodes(only=(A1, A2), with_pagerank=False), index_by_raw
    )
    nan_column, _ = account_membership_rows(
        pl.DataFrame(
            [[A1, 7, 3, True, float("nan")], [A2, 7, 1, False, 0.10]],
            schema=NODE_SCHEMA,
            orient="row",
        ),
        index_by_raw,
    )

    assert refused == []
    assert all("pagerank" not in row for row in no_column), no_column
    assert "pagerank" not in nan_column[0], "NaN is not a measurement, so the key stays absent"
    assert nan_column[1]["pagerank"] == 0.10, "a neighbour's NaN does not remove this node's number"
    assert (
        [row["account_key"] for row in no_column]
        == [A1, A2]
        == [row["account_key"] for row in nan_column]
    )


def test_membership_refuses_a_nodes_frame_missing_account_community_or_degree() -> None:
    for column, expected in (
        ("account", "'account'"),
        (NODE_COMMUNITY_COLUMN, "'community_id'"),
        ("total_degree", "'total_degree'"),
    ):
        with pytest.raises(LandingError, match="membership cannot be described") as caught:
            account_membership_rows(_nodes().drop(column), {7: 0, 3: 1})
        assert expected in str(caught.value), f"{column} should be named: {caught.value}"

    with pytest.raises(LandingError, match="nodes.parquet") as caught:
        community_index_by_raw_label(_nodes().drop(NODE_COMMUNITY_COLUMN))
    assert "communities cannot be described" in str(caught.value), caught.value


# --- graph_edge_rows: one row per published pair, read off both endpoints ---------------------


def test_every_pair_the_graph_published_lands_with_its_measured_figures() -> None:
    rows, refused = graph_edge_rows(_pairs(), _nodes())

    assert refused == [], f"the fixture measures every column graph_edge asks for: {refused}"
    # Sorted by (account_from, account_to): A1->A2, A2->A1, A3->A1, A3->A4, A4->A4.
    assert [
        (
            row["src_account_key"],
            row["dst_account_key"],
            row["txn_count"],
            row["total_minor"],
            row["currency"],
            row["is_rail"],
            row["flags"],
        )
        for row in rows
    ] == [
        (A1, A2, 4, 500, "UGX", True, []),
        (A2, A1, 2, 300, "UGX", True, []),
        (A3, A1, 1, 700, "UGX", True, []),
        (A3, A4, 3, 1000, "UGX", False, []),
        (A4, A4, 5, 90, "UGX", False, ["self_pair"]),
    ]
    assert rows[0]["first_ts"] == BASE_MOMENT and rows[0]["last_ts"] == PLUS_60S_MOMENT
    assert rows[0]["first_ts"].tzinfo is UTC
    assert set(rows[0]) == set(GRAPH_EDGE_SOURCES) | {
        "is_rail",
        "flags",
    }, "the row's keys are the mapped columns plus the two derived ones, exactly"
    assert all(isinstance(row["is_rail"], bool) for row in rows)


def test_an_edge_is_a_rail_edge_when_either_endpoint_is() -> None:
    edges = _edges(
        [
            [A1, A2, "UGX", 4, 500, BASE_US, BASE_US, False],
            [A2, A1, "UGX", 2, 300, BASE_US, BASE_US, False],
            [A2, A3, "UGX", 1, 100, BASE_US, BASE_US, False],
        ]
    )
    rows, refused = graph_edge_rows(edges, _nodes())

    assert refused == [], refused
    assert rows[0]["is_rail"] is True, "the source is the rail here and the destination is not"
    assert rows[1]["is_rail"] is True, "the same pair read the other way round: still a rail edge"
    assert rows[2]["is_rail"] is False, "A2 and A3 are both non-rails, so the edge is not one"


def test_an_endpoint_the_nodes_frame_does_not_know_refuses_the_edge() -> None:
    edges = _edges(
        [
            [A1, A9, "UGX", 2, 200, BASE_US, BASE_US, False],
            [A9, A1, "UGX", 3, 300, BASE_US, BASE_US, False],
        ]
    )

    rows, refused = graph_edge_rows(edges, _nodes())

    assert rows == [], "defaulting the unknown endpoint to not-a-rail would be a false measurement"
    assert refused == [
        f"edge {A1}->{A9}: an endpoint is absent from the nodes frame, so its rail flag "
        "was never measured",
        f"edge {A9}->{A1}: an endpoint is absent from the nodes frame, so its rail flag "
        "was never measured",
    ]


def test_a_pair_nothing_dated_refuses_naming_the_unmeasured_column() -> None:
    edges = _edges(
        [
            [A1, A2, "UGX", 4, 500, None, BASE_US, False],
            [A2, A1, "UGX", 2, 300, BASE_US, None, False],
        ]
    )

    rows, refused = graph_edge_rows(edges, _nodes())

    assert rows == [], "first_ts/last_ts are NOT NULL; a None would fail the write, not the read"
    assert len(refused) == 2
    assert "no measurement for ['first_ts']" in refused[0], refused[0]
    assert "no measurement for ['last_ts']" in refused[1], refused[1]


def test_a_currency_code_that_does_not_fit_char_3_refuses_rather_than_truncating() -> None:
    edges = _edges([[A1, A2, "UGXX", 4, 500, BASE_US, BASE_US, False]])

    rows, refused = graph_edge_rows(edges, _nodes())

    assert rows == [], "a currency cut to 'UGX' would be a different code than the one recorded"
    assert len(refused) == 1 and "no measurement for ['currency']" in refused[0], refused


def test_an_endpoint_that_is_not_a_twelve_character_key_refuses_by_its_own_words() -> None:
    # `LONGENDPOINTKEY13` is 17 characters, so `CHAR(12)` would cut it; the refusal quotes it whole.
    long_key = "LONGENDPOINTKEY13"
    edges = _edges([[long_key, A2, "UGX", 4, 500, BASE_US, BASE_US, False]])

    rows, refused = graph_edge_rows(edges, _nodes())

    assert rows == []
    assert refused == [f"edge {long_key}->{A2}: an endpoint is not a 12-character key"], refused


def test_a_repeated_pair_refuses_the_second_copy_because_the_key_has_no_currency() -> None:
    # pairs aggregates by (from, to, currency), so a two-currency corpus writes two rows for the
    # one (run_id, src, dst) key graph_edge is indexed on. One lands, the other refuses.
    edges = _edges(
        [
            [A1, A2, "UGX", 4, 500, BASE_US, BASE_US, False],
            [A1, A2, "USD", 1, 250, BASE_US, BASE_US, False],
        ]
    )

    rows, refused = graph_edge_rows(edges, _nodes())

    assert len(rows) == 1 and len(refused) == 1
    assert rows[0]["txn_count"] in (4, 1) and rows[0]["currency"] in ("UGX", "USD")
    assert refused == [
        f"edge {A1}->{A2}: the pairs frame repeats it, and (run_id, src, dst) is the key"
    ], refused


def test_edges_come_out_ordered_by_the_two_endpoints_whatever_order_the_pairs_arrived_in() -> None:
    rows, refused = graph_edge_rows(_pairs(shuffled=True), _nodes(shuffled=True))

    assert refused == []
    assert [(row["src_account_key"], row["dst_account_key"]) for row in rows] == [
        (A1, A2),
        (A2, A1),
        (A3, A1),
        (A3, A4),
        (A4, A4),
    ]


def test_a_pairs_frame_missing_a_graph_edge_column_raises() -> None:
    with pytest.raises(LandingError, match="no edge can be described") as caught:
        graph_edge_rows(_pairs(keep=("account_from", "account_to", "currency")), _nodes())
    message = str(caught.value)
    assert "'edge_count'" in message and "'total_value_minor'" in message, message
    assert "pairs.parquet" in message, f"the file that would carry it is the fix: {message}"


def test_a_nodes_frame_without_account_or_is_rail_raises() -> None:
    for column in ("account", "is_rail"):
        with pytest.raises(LandingError, match="account/is_rail") as caught:
            graph_edge_rows(_pairs(), _nodes().drop(column))
        assert "every row would be a guess" in str(caught.value), caught.value


# --- the three tables read one partition, so they must number it once ------------------------


def test_the_three_tables_number_the_same_communities_the_same_way() -> None:
    """`_graph_tables` passes one remap to two builders; a disagreement is an invisible wrong map."""
    nodes, pairs = _nodes(), _pairs()
    index_by_raw, _, label_refusals = community_index_by_raw_label(nodes)
    communities, community_refusals = community_rows(nodes, pairs, stats=_stats())
    memberships, membership_refusals = account_membership_rows(nodes, index_by_raw)
    edges, edge_refusals = graph_edge_rows(pairs, nodes)

    assert (label_refusals, community_refusals, membership_refusals, edge_refusals) == (
        [],
        [],
        [],
        [],
    )
    written = {row["canonical_index"] for row in communities}
    assert written == {0, 1}, "the community ids are contiguous, so nothing dangles by shape alone"
    assert {row["community_id"] for row in memberships} <= written
    raw_by_index = {index: raw for raw, index in index_by_raw.items()}
    assert all(
        str(raw_by_index[row["community_id"]])
        == next(c["raw_label"] for c in communities if c["canonical_index"] == row["community_id"])
        for row in memberships
    ), "a membership's community has to be the row the community table wrote under that index"
    assert len(edges) == 5 and len(memberships) == 5 and len(communities) == 2
