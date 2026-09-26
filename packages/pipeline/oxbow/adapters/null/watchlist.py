"""Null watchlist: no snapshot loaded, and it says so on every result (02 §A).

The demo default for the watchlist port, and the only honest shape for "screening is
not configured": the version it reports has ``record_count = 0`` in it, so a case
payload that carries screening results from this adapter visibly carries "we matched
against nothing" rather than an implied clean screen. An empty result from a
twelve-entry sample and an empty result from no list at all must not look the same
downstream, and here they do not.

Like every other watchlist adapter, ``automatic_decision_authority`` is ``False``,
and the contract test asserts that on the whole family — including this one, so a
null adapter can never be the place a decision authority leaks in.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from oxbow.ports.watchlist import (
    MAX_CANDIDATES,
    ScreeningQuery,
    WatchlistHit,
    WatchlistVersion,
)

NULL_LIST_NAME = "none-configured"


class NullWatchlistAdapter:
    """A watchlist that holds nothing, for a deployment that has not chosen one."""

    def __init__(self, *, list_name: str = NULL_LIST_NAME) -> None:
        self._list_name = list_name

    @property
    def list_name(self) -> str:
        return self._list_name

    @property
    def automatic_decision_authority(self) -> bool:
        return False

    def version(self) -> WatchlistVersion:
        return WatchlistVersion(
            list_name=self._list_name,
            version="null",
            effective_at=datetime.now(UTC),
            record_count=0,
            source="no snapshot configured: a clean result from this adapter is not evidence",
        )

    def screen(self, query: ScreeningQuery) -> Sequence[WatchlistHit]:
        """Always empty, and still refuses a query with nothing in it.

        The refusal is the point: the caller has to distinguish "we asked and found
        nothing" from "we never asked", and this adapter is where that distinction is
        enforced rather than assumed.
        """
        if query.is_empty():
            raise ValueError(
                f"screening query {query.query_id!r} carries no name and no identifier"
            )
        return [][:MAX_CANDIDATES]


__all__ = ["NULL_LIST_NAME", "NullWatchlistAdapter"]
