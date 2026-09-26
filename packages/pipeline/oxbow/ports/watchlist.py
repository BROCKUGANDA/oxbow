"""The WatchlistAdapter port: screening is enrichment, never a decision (02 §A, §F).

The failure this port's shape exists to prevent is a plausible and common one: a
watchlist match comes back as a boolean, someone wires the boolean into the
decision path, and the system starts making automated sanctions decisions with no
appeal, no reason code and no human. 02 §F is explicit that OXBOW does not make
real financial decisions, and plan §13 says the OFAC adapter is "enrichment only,
never an automatic decision".

So the port makes that structural rather than aspirational:

* a hit is never a bool. It is a ``similarity`` in ``[0, 1]``, the ``match_basis``
  that produced it, and the list version it was matched against, so a human can
  see why and a reviewer can reproduce it;
* every implementation reports ``automatic_decision_authority == False``, and the
  contract test asserts it on each adapter, so adding a new watchlist source
  cannot silently add a new decision-maker;
* fuzzy matching lives here, in :func:`normalise_name`, so two adapters cannot
  disagree about whether the same name matches — the disagreement would show up
  as an unexplained gap between two screens of the same account.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

# Tokens shorter than this cannot carry identity: "MO" matching "MD" at edit
# distance 1 is noise, and noise in a sanctions screen is an audit finding.
MIN_TOKEN_LENGTH: Final = 3

# Below this, a hit is reported as a near-miss with its basis and nothing more.
DEFAULT_MIN_SIMILARITY: Final = 0.82

MAX_CANDIDATES: Final = 200

MATCH_BASES: Final = ("exact_normalised", "token_subset", "edit_distance", "identifier")


@dataclass(frozen=True, slots=True)
class WatchlistVersion:
    """Which snapshot was actually consulted.

    A screening result is meaningless without this: lists are revised weekly, and
    "no match" against a stale snapshot is a different claim from "no match".
    """

    list_name: str
    version: str
    effective_at: datetime
    record_count: int
    source: str


@dataclass(frozen=True, slots=True)
class ScreeningQuery:
    """What to look for. Only pseudonymous subjects and names appear here.

    ``account_key`` is a screening *label*, not a join key into the watchlist: no
    real institution's customer data is in these snapshots, and pretending
    otherwise would be a fabricated match.
    """

    query_id: str
    display_name: str | None = None
    account_key: str | None = None
    identifiers: Sequence[str] = ()
    dob: str | None = None
    country: str | None = None

    def is_empty(self) -> bool:
        """Nothing to screen on. Callers must handle this as an error, not a miss."""
        return not (self.display_name or self.identifiers)


@dataclass(frozen=True, slots=True)
class WatchlistHit:
    """One candidate match, with the reason and the list version behind it.

    ``advisory_only`` is a field on the hit rather than a comment because the hit
    is what gets serialised into a case payload; the constraint has to survive the
    trip to the consumer.
    """

    list_name: str
    list_version: str
    reference: str
    matched_name: str
    similarity: float
    match_basis: str
    country: str | None = None
    remarks: str | None = None
    advisory_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Wire form for the evidence timeline and the case payload."""
        return {
            "list_name": self.list_name,
            "list_version": self.list_version,
            "reference": self.reference,
            "matched_name": self.matched_name,
            "similarity": self.similarity,
            "match_basis": self.match_basis,
            "country": self.country,
            "remarks": self.remarks,
            "advisory_only": self.advisory_only,
        }


def strip_accents(value: str) -> str:
    """Fold accented characters to ASCII so a transliterated name can match.

    Sanctions lists are transliteration nightmares; the same person appears as
    ``Abdul``, ``Abdel``, and ``\\u0410bdul`` -- that last spelling begins with U+0410,
    CYRILLIC CAPITAL LETTER A, which renders identically to the Latin A. It is written
    as an escape rather than the character itself on purpose: a raw homoglyph in the
    source of a screening port is one copy-paste away from a matcher or a list file,
    where it would be invisible to review.

    That case is also the limit of what this function does. NFKD removes combining marks
    only, so a character from a different script survives it untouched and still fails
    to match -- measured, not theorised: ``similarity("Abdul", "\\u0410bdul")`` is 0.80,
    under the 0.82 default, so a single-token confusable is a silent miss. Confusables
    folding is a separate change (reported); this is the cheap half.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalise_name(value: str) -> str:
    """Canonical form used for every comparison in this port."""
    folded = strip_accents(value).casefold()
    cleaned = "".join(char if char.isalnum() else " " for char in folded)
    return " ".join(cleaned.split())


def name_tokens(value: str) -> tuple[str, ...]:
    """Meaningful tokens only — short particles are dropped, see MIN_TOKEN_LENGTH."""
    return tuple(token for token in normalise_name(value).split() if len(token) >= MIN_TOKEN_LENGTH)


def similarity(a: str, b: str) -> float:
    """Bounded, deterministic name similarity in ``[0, 1]``.

    Three signals, cheapest first, and the strongest one wins: identical after
    normalisation, one name's tokens a subset of the other's (a sanctions listing
    usually carries extra titles), then a sequence-similarity ratio for typos and
    transliteration drift.

    ``difflib`` rather than a hand-rolled Levenshtein: it is in the standard
    library, it is documented, and 01 §A rule 6 forbids growing dependencies to
    save twenty lines.
    """
    import difflib  # local: keeps the port import-cheap for callers that only need types

    na, nb = normalise_name(a), normalise_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(name_tokens(a)), set(name_tokens(b))
    if ta and tb and (ta <= tb or tb <= ta):
        return 0.95
    return round(difflib.SequenceMatcher(None, na, nb).ratio(), 4)


@runtime_checkable
class WatchlistAdapter(Protocol):
    """Screens a query against one offline watchlist snapshot.

    Implementations must never raise on a near-miss and must never return a bool.
    The port has no method that mutates a case, a decision or a score: enrichment
    is a read.
    """

    @property
    def list_name(self) -> str:
        """Which list this adapter holds."""
        ...

    @property
    def automatic_decision_authority(self) -> bool:
        """Always ``False``. Asserted on every adapter by the contract test (02 §F)."""
        ...

    def version(self) -> WatchlistVersion:
        """The snapshot actually loaded, including its record count."""
        ...

    def screen(self, query: ScreeningQuery) -> Sequence[WatchlistHit]:
        """Ranked candidate matches, best first, capped at MAX_CANDIDATES."""
        ...


__all__ = [
    "DEFAULT_MIN_SIMILARITY",
    "MATCH_BASES",
    "MAX_CANDIDATES",
    "MIN_TOKEN_LENGTH",
    "ScreeningQuery",
    "WatchlistAdapter",
    "WatchlistHit",
    "WatchlistVersion",
    "name_tokens",
    "normalise_name",
    "similarity",
    "strip_accents",
]
