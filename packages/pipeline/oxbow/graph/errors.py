"""Named failures for the graph layer.

Every one of these exists because the alternative was a quiet wrong answer.
03 A rule 2: never let an unknown become a zero — and in this layer the more
dangerous version of that is never let an unknown become a *graph*, because a
graph with manufactured structure produces an explanation that survives review.

The split from ``oxbow.config.ConfigError`` is deliberate: a config error says
the operator wrote the wrong YAML, a graph error says the events or the search
refused to become a network. The two have different fixes and different owners.
"""

from __future__ import annotations


class GraphError(RuntimeError):
    """Base for the layer. Catchable as one type at the CLI, still specific in the message."""


class GraphConfigError(GraphError):
    """The ``graph:`` block is missing, malformed, or internally contradictory.

    Read through :mod:`oxbow.graph.settings`, which refuses to substitute a
    default for any key: a silent seed here would cost a day of debugging,
    because the run would be perfectly reproducible and perfectly wrong.
    """


class EventContractError(GraphError):
    """The input frame is not a canonical event v1 the graph can be built from.

    Raised for a missing column, a wrong dtype, a null node key, a float amount,
    a naive timestamp, an empty batch, or a duplicated ``txn_id``. The last two
    are the ones worth naming: an empty frame built into an empty graph looks
    like a quiet day, and a duplicate id destroys the total order that every
    sort in this layer depends on.
    """


class MixedCurrencyError(GraphError):
    """An amount aggregation was asked to cross a currency boundary.

    Money is corpus-qualified (01 B, spec 5.2): a total that adds EUR to USD is
    not a total, it is an implicit FX rate nobody wrote down. Aggregates in this
    layer are keyed by currency, so reaching this error means a caller asked for
    one number where the honest answer is several.
    """


class GraphTooLargeError(GraphError):
    """The corpus is larger than the in-memory interactive graph can hold.

    The bound is ``sampling.interactive_txn_target``. Over it, the frame-level path
    (:func:`oxbow.graph.build.degree_measurement`) is the correct call and
    full-corpus metrics are offline work per ``sampling.full_corpus_metrics``.
    Raising rather than degrading is the point: an algorithm that quietly ran on a
    truncated graph would print a degree distribution that looks real.
    """


class UnknownAccountError(GraphError):
    """A neighbourhood was requested for an account with no events.

    Returning an empty subgraph here would be indistinguishable, in the explorer,
    from an account that genuinely has no network — which is the single most
    consequential negative statement this product can make about a person.
    """


class GraphArtifactError(GraphError):
    """A persisted graph on disk disagrees with its own manifest.

    The API and the explorer read these files instead of rebuilding, so a
    truncated or stale artifact has to fail loudly at read time: a silently
    partial graph renders as a real one.
    """
