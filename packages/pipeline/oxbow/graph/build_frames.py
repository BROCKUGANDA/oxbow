"""Frame- and structure-level building blocks, shared by the bounded and the
offline build paths.

Split out of :mod:`oxbow.graph.build` because ``degree_measurement`` — the path
that runs over a full corpus without a NetworkX graph — must compute *exactly* the
same degrees as the interactive build. Two implementations of "how many edges
touch this account" is how a gate number and a screen number stop agreeing, so
there is one skeleton here and both callers read it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import networkx as nx
import polars as pl

from oxbow.graph.events import DERIVED_SELF_COLUMN, DERIVED_TS_COLUMN
from oxbow.graph.model import EdgeLeg

DIRECTION_SENT_TO: Final = "sent_to"
DIRECTION_RECEIVED_FROM: Final = "received_from"

NODE_COLUMNS: Final[tuple[str, ...]] = (
    "account",
    "total_degree",
    "in_degree",
    "out_degree",
    "unique_counterparties",
    "fan_in",
    "fan_out",
    "self_transfer_count",
    "is_originator",
    "first_ts_us",
    "last_ts_us",
)


@dataclass(frozen=True, slots=True)
class NodeSkeleton:
    """Per-account counts before typing, plus the distinct-counterparty links.

    ``counterparty_links`` is one row per ``(account, counterparty, direction)`` and
    is the only place "who dealt with whom" is stated; effective fan-in is derived
    from it after rails are known, which is why it is carried rather than folded
    into the table.
    """

    table: pl.DataFrame
    counterparty_links: pl.DataFrame


def build_node_skeleton(frame: pl.DataFrame) -> NodeSkeleton:
    """Degree, in-degree, out-degree, counterparties and self-transfers per account.

    Self-transfers are excluded from every degree and counterparty figure and
    counted separately (P3a): ``A → A`` forty times is not forty relationships, and
    counting it as forty would make a bookkeeping entry look like a network.
    """
    edges = frame.filter(~pl.col(DERIVED_SELF_COLUMN))

    out_degrees = (
        edges.group_by("account_from")
        .agg(pl.len().cast(pl.Int64).alias("out_degree"))
        .rename({"account_from": "account"})
    )
    in_degrees = (
        edges.group_by("account_to")
        .agg(pl.len().cast(pl.Int64).alias("in_degree"))
        .rename({"account_to": "account"})
    )
    self_counts = (
        frame.filter(pl.col(DERIVED_SELF_COLUMN))
        .group_by("account_from")
        .agg(pl.len().cast(pl.Int64).alias("self_transfer_count"))
        .rename({"account_from": "account"})
    )
    accounts = pl.concat(
        [
            frame.select(pl.col("account_from").alias("account")),
            frame.select(pl.col("account_to").alias("account")),
        ],
        how="vertical",
    ).unique(maintain_order=False)
    presence = (
        pl.concat(
            [
                frame.select(
                    pl.col("account_from").alias("account"),
                    pl.col(DERIVED_TS_COLUMN).alias("ts_us"),
                ),
                frame.select(
                    pl.col("account_to").alias("account"), pl.col(DERIVED_TS_COLUMN).alias("ts_us")
                ),
            ],
            how="vertical",
        )
        .group_by("account")
        .agg(pl.col("ts_us").min().alias("first_ts_us"), pl.col("ts_us").max().alias("last_ts_us"))
    )
    originators = frame.select(pl.col("account_from").alias("account")).unique()

    links = pl.concat(
        [
            edges.select(
                pl.col("account_from").alias("account"),
                pl.col("account_to").alias("counterparty"),
                pl.lit(DIRECTION_SENT_TO).alias("direction"),
            ),
            edges.select(
                pl.col("account_to").alias("account"),
                pl.col("account_from").alias("counterparty"),
                pl.lit(DIRECTION_RECEIVED_FROM).alias("direction"),
            ),
        ],
        how="vertical",
    ).unique(maintain_order=False)

    per_account = links.group_by("account").agg(
        pl.col("counterparty").n_unique().alias("unique_counterparties"),
        pl.col("counterparty")
        .filter(pl.col("direction") == DIRECTION_SENT_TO)
        .n_unique()
        .alias("fan_out"),
        pl.col("counterparty")
        .filter(pl.col("direction") == DIRECTION_RECEIVED_FROM)
        .n_unique()
        .alias("fan_in"),
    )

    table = (
        accounts.join(out_degrees, on="account", how="left")
        .join(in_degrees, on="account", how="left")
        .join(per_account, on="account", how="left")
        .join(self_counts, on="account", how="left")
        .join(presence, on="account", how="left")
        .join(
            originators.with_columns(pl.lit(True).alias("is_originator")), on="account", how="left"
        )
        .with_columns(
            pl.col("out_degree").fill_null(0).cast(pl.Int64),
            pl.col("in_degree").fill_null(0).cast(pl.Int64),
            pl.col("unique_counterparties").fill_null(0).cast(pl.Int64),
            pl.col("fan_in").fill_null(0).cast(pl.Int64),
            pl.col("fan_out").fill_null(0).cast(pl.Int64),
            pl.col("self_transfer_count").fill_null(0).cast(pl.Int64),
            pl.col("is_originator").fill_null(False).cast(pl.Boolean),
        )
        .with_columns((pl.col("in_degree") + pl.col("out_degree")).alias("total_degree"))
        .select(list(NODE_COLUMNS))
        .sort("account")
    )
    return NodeSkeleton(table=table, counterparty_links=links.sort(["account", "counterparty"]))


def pair_aggregates(frame: pl.DataFrame) -> pl.DataFrame:
    """One row per ``(from, to, currency)``: count, total, time span, extremes.

    Currency is a group key, never a value column, which is the structural reason
    no total in this layer can silently add two currencies together. Self-pairs are
    kept (flagged) rather than dropped: the money they carry is real money, and an
    aggregate that quietly omits it would under-report the corpus.

    ``first_txn_id`` / ``last_txn_id`` rely on ``frame`` already being in the
    canonical total order and on polars preserving within-group order — which it
    does — so "first" means first in time, not first in a hash bucket.
    """
    return (
        frame.group_by("account_from", "account_to", "currency")
        .agg(
            pl.len().cast(pl.Int64).alias("edge_count"),
            pl.col("amount_minor").sum().alias("total_value_minor"),
            pl.col("amount_minor").min().alias("min_amount_minor"),
            pl.col("amount_minor").max().alias("max_amount_minor"),
            pl.col(DERIVED_TS_COLUMN).min().alias("first_ts_us"),
            pl.col(DERIVED_TS_COLUMN).max().alias("last_ts_us"),
            (pl.col(DERIVED_TS_COLUMN).max() - pl.col(DERIVED_TS_COLUMN).min()).alias("span_us"),
            pl.col("txn_id").first().alias("first_txn_id"),
            pl.col("txn_id").last().alias("last_txn_id"),
            # A sorted list, not a joined string: it survives Parquet round-tripping
            # with its ordering guarantee intact and without a delimiter collision.
            pl.col("txn_type").unique().sort().alias("txn_types"),
            pl.col(DERIVED_SELF_COLUMN).first().alias("is_self_pair"),
        )
        .sort(["account_from", "account_to", "currency"])
    )


def split_currency_pairs(pairs: pl.DataFrame) -> list[tuple[str, str, int]]:
    """Unordered ``(left, right, edge_count)`` with both directions merged.

    Community detection asks "are these two accounts connected, and how strongly",
    and that answer must not depend on who happened to send first, nor on which
    currency moved: the count is summed across currencies, which is a count of
    events and therefore safe to add, while value never is.
    """
    undirected = (
        pairs.filter(~pl.col("is_self_pair"))
        .with_columns(
            pl.min_horizontal("account_from", "account_to").alias("left"),
            pl.max_horizontal("account_from", "account_to").alias("right"),
        )
        .group_by("left", "right")
        .agg(pl.col("edge_count").sum().alias("edge_count"))
        .sort(["left", "right"])
    )
    return [(str(left), str(right), int(count)) for left, right, count in undirected.iter_rows()]


def build_multigraph(
    nodes: pl.DataFrame, out_edges: Mapping[str, tuple[EdgeLeg, ...]]
) -> nx.MultiDiGraph:
    """The NetworkX time-stamped multigraph: parallel edges retained, never merged.

    Each event is its own edge with its own instant and amount, so a 40-transfer
    burst stays 40 edges and the explorer can show the burst. Node attributes carry
    the typing so a traversal can honour rails without a second lookup.
    """
    graph: nx.MultiDiGraph = nx.MultiDiGraph()
    graph.add_nodes_from(
        (str(row["account"]), {str(key): value for key, value in row.items() if key != "account"})
        for row in nodes.iter_rows(named=True)
    )
    for account in sorted(out_edges):
        for leg in out_edges[account]:
            graph.add_edge(
                account,
                leg.dst,
                txn_id=leg.txn_id,
                ts_us=leg.ts_us,
                amount_minor=leg.amount_minor,
                currency=leg.currency,
                txn_type=leg.txn_type,
                edge_id=leg.edge_id,
            )
    return graph


__all__ = [
    "DIRECTION_RECEIVED_FROM",
    "DIRECTION_SENT_TO",
    "NODE_COLUMNS",
    "NodeSkeleton",
    "build_multigraph",
    "build_node_skeleton",
    "pair_aggregates",
    "split_currency_pairs",
]
