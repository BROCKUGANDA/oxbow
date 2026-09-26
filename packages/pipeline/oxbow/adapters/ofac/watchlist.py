"""Offline SDN-snapshot screening: enrichment only, never an automatic decision.

02 §F and plan §13 both put the same constraint on this adapter, so it is built
into the class rather than left to the caller:

* there is no method that changes anything. Screening returns ranked candidates with
  a similarity, the basis for it, and the list version consulted;
* ``automatic_decision_authority`` is ``False`` as a *constant*, and the contract
  test asserts it on every watchlist adapter — so a future second list cannot
  quietly arrive with decision power;
* a hit is always ``advisory_only``, and an empty result is reported as "the snapshot
  we hold has N entries, version V" rather than as a clean bill of health. Absence of
  a match against a stale list is not evidence of innocence, and the response says
  so.

The snapshot that ships is a labelled sample of fictional entries
(``data/watchlist/sample-sdn-v1.csv``); :meth:`OfacWatchlistAdapter.from_path` takes
a real SDN.CSV download with the same columns. The path to a replacement comes from
the environment, and nothing in this module downloads it: a prototype that fetched a
sanctions list at scoring time would break the no-network-at-scoring-time rule and
make every historical result depend on what the list said today.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from oxbow.ports.watchlist import (
    DEFAULT_MIN_SIMILARITY,
    MAX_CANDIDATES,
    ScreeningQuery,
    WatchlistHit,
    WatchlistVersion,
    similarity,
)

DEFAULT_SNAPSHOT_NAME: Final = "sample-sdn-v1.csv"
WATCHLIST_PATH_ENV: Final = "OXBOW_WATCHLIST_PATH"
_MAX_ALT_NAMES: Final = 8


class SnapshotFormatError(ValueError):
    """The snapshot on disk is not the format this adapter reads."""


class OfacWatchlistAdapter:
    """Screens against one offline snapshot held in memory."""

    def __init__(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        list_name: str,
        version: str,
        effective_at: datetime,
        source: str,
    ) -> None:
        if not records:
            raise SnapshotFormatError(
                f"watchlist snapshot {list_name!r} has no records. An empty list screens "
                "every account clean, which is the most misleading answer it can give."
            )
        self._records: list[dict[str, Any]] = [dict(record) for record in records]
        self._list_name = list_name
        self._version = version
        self._effective_at = effective_at
        self._source = source
        self._min_similarity = DEFAULT_MIN_SIMILARITY

    # --- construction -------------------------------------------------------

    @classmethod
    def from_path(cls, path: Path, *, list_name: str | None = None) -> OfacWatchlistAdapter:
        """Load a snapshot CSV and derive its version from the bytes themselves.

        The version is a count plus an mtime rather than a field someone remembered
        to update, because a screening hit has to name the snapshot it actually used
        and a hand-maintained version string goes stale silently.
        """
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"watchlist snapshot is missing: {path}")
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"reference", "name"}
            if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
                raise SnapshotFormatError(
                    f"{path} must have at least the columns {sorted(required)}, got "
                    f"{list(reader.fieldnames or [])}"
                )
            records = [dict(row) for row in reader]
        stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        return cls(
            records,
            list_name=list_name or path.stem,
            version=f"{path.stem}-rows{len(records)}-{stamp:%Y%m%dT%H%M%SZ}",
            effective_at=stamp,
            source=str(path),
        )

    @classmethod
    def from_repo_default(cls, repo_root: Path) -> OfacWatchlistAdapter:
        """The snapshot the demo uses, unless the environment names another."""
        override = os.environ.get(WATCHLIST_PATH_ENV, "").strip()
        path = (
            Path(override) if override else repo_root / "data" / "watchlist" / DEFAULT_SNAPSHOT_NAME
        )
        return cls.from_path(path)

    # --- the port -----------------------------------------------------------

    @property
    def list_name(self) -> str:
        return self._list_name

    @property
    def automatic_decision_authority(self) -> bool:
        """Always False. See the module docstring: this is a read, not a decision."""
        return False

    def version(self) -> WatchlistVersion:
        return WatchlistVersion(
            list_name=self._list_name,
            version=self._version,
            effective_at=self._effective_at,
            record_count=len(self._records),
            source=self._source,
        )

    def screen(self, query: ScreeningQuery) -> Sequence[WatchlistHit]:
        """Ranked candidate matches for one query, best first.

        Raises on a query with nothing to screen rather than returning an empty list
        for a caller bug — an empty list here means "screened clean", and conflating
        the two is how a screening integration quietly stops screening (01 §G).
        """
        if query.is_empty():
            raise ValueError(
                f"screening query {query.query_id!r} carries no name and no identifier; "
                "there is nothing to match. An empty result here would read as a clean screen."
            )
        hits: list[WatchlistHit] = []
        for record in self._records:
            best = self._best_match(record, query)
            if best is not None:
                hits.append(best)
        hits.sort(key=lambda hit: (-hit.similarity, hit.reference))
        return hits[:MAX_CANDIDATES]

    def _best_match(self, record: dict[str, Any], query: ScreeningQuery) -> WatchlistHit | None:
        reference = str(record.get("reference") or "")
        for identifier in query.identifiers:
            if identifier.strip() and identifier.strip() == reference:
                return self._hit(record, 1.0, "identifier")
        if not query.display_name:
            return None
        candidates: list[tuple[float, str, str]] = [
            (similarity(query.display_name, str(record.get("name") or "")), "name", "")
        ]
        alt_field = str(record.get("alt_names") or "")
        for alt in alt_field.split(";")[:_MAX_ALT_NAMES]:
            if alt.strip():
                candidates.append(
                    (similarity(query.display_name, alt.strip()), "alt_name", str(alt.strip()))
                )
        best_score, best_basis, best_matched = max(candidates, key=lambda item: item[0])
        if best_score < self._min_similarity:
            return None
        # Both the primary name and an alt name have to be reported in the port's
        # vocabulary. The candidate list uses "name"/"alt_name" as *provenance* - which
        # column won - but that is not a basis a reviewer can act on, and a consumer
        # switching on `match_basis` would meet a value MATCH_BASES does not contain.
        # The basis says *how* it matched; the column is carried by `matched_name`.
        if best_score >= 1.0:
            best_basis = "exact_normalised"
        elif best_basis == "alt_name":
            best_basis = "token_subset"
        else:
            best_basis = "edit_distance"
        return self._hit(record, best_score, best_basis, matched_name=best_matched or None)

    def _hit(
        self,
        record: dict[str, Any],
        score: float,
        basis: str,
        *,
        matched_name: str | None = None,
    ) -> WatchlistHit:
        return WatchlistHit(
            list_name=self._list_name,
            list_version=self._version,
            reference=str(record.get("reference") or ""),
            matched_name=matched_name or str(record.get("name") or ""),
            similarity=score,
            match_basis=basis,
            country=str(record.get("country") or "") or None,
            remarks=str(record.get("remarks") or "") or None,
            advisory_only=True,
        )


__all__ = ["WATCHLIST_PATH_ENV", "OfacWatchlistAdapter", "SnapshotFormatError"]
