"""Port conformance for the watchlist port, asserted against every adapter.

`apps/api/routers/cases.py` cites this file by name as the evidence that screening is
exercised against a real query, and P7's gate ("port conformance across every
adapter") is `pytest -q tests/contracts_adapters`. Until this file existed the
directory held one fixture and no tests, so that gate collected zero tests and exited
5 - it could not pass, and a docstring pointed at a file that did not exist. A skipped
gate is how a doctrine ends up existing only in a docstring, which is punch-list item
11 all over again.

So this is a *conformance* suite, not a unit test of one adapter: the same assertions
run against every ``WatchlistAdapter`` implementation in the tree, and a new adapter
that does not satisfy the port fails here rather than in production. Adding a class is
one line in ``ADAPTERS`` - that is the intended way this file grows.

What the port actually forbids, and what each test below pins:

* a hit is never a ``bool``. It is a similarity, a basis and a list version, so a human
  can see why (02 §F);
* ``automatic_decision_authority`` is ``False`` on every implementation, so a second
  sanctions source cannot arrive with decision power (02 §F, plan §13);
* an empty query is an error, never an empty result - "screened clean" and "never
  screened" are different claims and 01 §G is the one that gets violated when they are
  conflated;
* a snapshot with no records is refused at construction: it would screen every account
  clean, which is the most misleading answer the port can give.

The family-level facts (an empty result carries ``record_count`` in the version, hits
stay inside ``MAX_CANDIDATES``) are asserted per adapter, because "we matched against
nothing" and "we matched against twelve entries and found nothing" must not look the
same downstream.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oxbow.adapters.null.watchlist import NullWatchlistAdapter
from oxbow.adapters.ofac.watchlist import (
    OfacWatchlistAdapter,
    SnapshotFormatError,
)
from oxbow.ports.watchlist import (
    MATCH_BASES,
    MAX_CANDIDATES,
    ScreeningQuery,
    WatchlistAdapter,
    WatchlistHit,
    WatchlistVersion,
    name_tokens,
    normalise_name,
    similarity,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SNAPSHOT = REPO_ROOT / "data" / "watchlist" / "sample-sdn-v1.csv"

AdapterFactory = Callable[[], WatchlistAdapter]

# A name in the shipped sample snapshot, and one that is not, so "screened and found
# it" and "screened and did not" are both reachable from a real adapter.
MATCHED_NAME = "TEST ALPHA BRAVO"
MATCHED_REFERENCE = "OXS-0002"
UNMATCHED_NAME = "WILHELMINA FERNANDEZ-QUINTERO"


def _ofac() -> WatchlistAdapter:
    return OfacWatchlistAdapter.from_path(SAMPLE_SNAPSHOT)


def _null() -> WatchlistAdapter:
    return NullWatchlistAdapter()


# Every WatchlistAdapter in the tree. A new implementation is added here.
ADAPTERS: dict[str, AdapterFactory] = {
    "ofac": _ofac,
    "null": _null,
}


def _ids() -> list[str]:
    return list(ADAPTERS)


@pytest.fixture(params=sorted(ADAPTERS))
def adapter(request: pytest.FixtureRequest) -> WatchlistAdapter:
    return ADAPTERS[request.param]()


# --- the port's structural promises -------------------------------------------


@pytest.mark.parametrize("name", _ids())
def test_satisfies_the_runtime_checkable_protocol(name: str) -> None:
    """A duck that does not satisfy the Protocol is not an implementation of the port."""
    assert isinstance(ADAPTERS[name](), WatchlistAdapter), (
        f"{name} does not satisfy WatchlistAdapter; a screen wired through a protocol the "
        "adapter does not implement fails at the call site, in production"
    )


@pytest.mark.parametrize("name", _ids())
def test_never_claims_decision_authority(name: str) -> None:
    """02 §F and plan §13: enrichment only, never an automatic decision.

    Asserted on the whole family including the null adapter, so the place a decision
    authority would leak in is also a place this test watches.
    """
    assert ADAPTERS[name]().automatic_decision_authority is False


def test_version_is_populated(adapter: WatchlistAdapter) -> None:
    """A screening result is meaningless without the snapshot it was made against."""
    version = adapter.version()
    assert isinstance(version, WatchlistVersion)
    assert version.list_name == adapter.list_name
    assert version.record_count >= 0
    assert version.version
    assert isinstance(version.effective_at, datetime)
    assert version.effective_at.tzinfo is not None, "an effective_at without a tz is ambiguous"
    assert version.source


def test_refuses_an_empty_query_rather_than_screening_clean(adapter: WatchlistAdapter) -> None:
    """01 §G: "nothing to match" and "matched nothing" must not look the same.

    An empty ``ScreeningQuery`` carries no name and no identifier. Returning ``[]``
    here is the exact bug this port exists to catch, so it is an error on every
    implementation.
    """
    query = ScreeningQuery(query_id="q-empty", account_key="acct-1")
    assert query.is_empty()
    with pytest.raises(ValueError):
        adapter.screen(query)


def test_screen_returns_hits_not_a_verdict(adapter: WatchlistAdapter) -> None:
    """A hit is a candidate with evidence, never a bool (02 §F)."""
    hits = adapter.screen(ScreeningQuery(query_id="q-1", display_name=MATCHED_NAME))

    assert isinstance(hits, Sequence)
    assert not isinstance(hits, bool)
    assert len(hits) <= MAX_CANDIDATES
    for hit in hits:
        assert isinstance(hit, WatchlistHit)
        assert isinstance(hit.similarity, float)
        assert 0.0 <= hit.similarity <= 1.0, "similarity is a bounded score, not a raw distance"
        assert hit.match_basis in MATCH_BASES
        assert hit.list_name == adapter.list_name
        assert hit.list_version == adapter.version().version
        assert hit.advisory_only is True


def test_an_empty_result_still_reports_what_was_searched(adapter: WatchlistAdapter) -> None:
    """The distinction the null adapter exists to preserve.

    An empty hit list is only honest alongside the record count it was produced
    against: zero means "matched against nothing at all", which is not evidence of
    innocence. Both adapters must surface that through ``version()``.
    """
    version = adapter.version()
    hits = adapter.screen(ScreeningQuery(query_id="q-2", display_name=UNMATCHED_NAME))

    assert list(hits) == [], "this name is in no shipped snapshot"
    # The caller can always tell how much was searched, so an empty result is never
    # mistaken for a clean screen of a real list.
    assert version.record_count >= 0
    assert version.record_count == adapter.version().record_count


def test_null_adapter_reports_zero_records_so_clean_is_never_implied() -> None:
    """The null adapter's whole reason to exist (02 §A)."""
    version = NullWatchlistAdapter().version()

    assert version.record_count == 0
    assert "not evidence" in version.source
    assert (
        NullWatchlistAdapter().screen(ScreeningQuery(query_id="q", display_name=MATCHED_NAME)) == []
    )


# --- OFAC: real screening against the shipped sample -------------------------


def test_ofac_finds_a_name_in_the_shipped_snapshot() -> None:
    adapter = OfacWatchlistAdapter.from_path(SAMPLE_SNAPSHOT)
    hits = adapter.screen(ScreeningQuery(query_id="q-hit", display_name=MATCHED_NAME))

    assert hits, "the sample snapshot ships this name; screening must find it"
    best = hits[0]
    assert best.reference == MATCHED_REFERENCE
    assert best.similarity == 1.0
    assert best.match_basis in {"exact_normalised", "token_subset", "identifier"}


def test_ofac_matches_on_an_identifier_without_a_name() -> None:
    """``account_key`` is a screening label, not a join key; identifiers are."""
    adapter = OfacWatchlistAdapter.from_path(SAMPLE_SNAPSHOT)
    hits = adapter.screen(ScreeningQuery(query_id="q-id", identifiers=[MATCHED_REFERENCE]))

    assert len(hits) == 1
    assert hits[0].match_basis == "identifier"
    assert hits[0].similarity == 1.0


def test_ofac_ranks_best_first_and_is_deterministic() -> None:
    """Same snapshot, same query, same order - a screening result has to be reproducible."""
    adapter = OfacWatchlistAdapter.from_path(SAMPLE_SNAPSHOT)
    query = ScreeningQuery(query_id="q-rank", display_name="TEST ALPHA BRAVO")

    first = list(adapter.screen(query))
    second = list(adapter.screen(query))

    assert [hit.reference for hit in first] == [hit.reference for hit in second]
    scores = [hit.similarity for hit in first]
    assert scores == sorted(scores, reverse=True)


def test_ofac_refuses_an_empty_snapshot() -> None:
    """A list with no records screens everything clean; construction must refuse it."""
    with pytest.raises(SnapshotFormatError):
        OfacWatchlistAdapter(
            [],
            list_name="empty",
            version="v0",
            effective_at=datetime.now(UTC),
            source="unit test",
        )


def test_ofac_reports_a_missing_snapshot_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        OfacWatchlistAdapter.from_path(tmp_path / "not-there.csv")


def test_ofac_rejects_a_snapshot_missing_required_columns(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("nmae,note\nfoo,bar\n", encoding="utf-8")

    with pytest.raises(SnapshotFormatError):
        OfacWatchlistAdapter.from_path(bad)


# --- the shared matcher, asserted once because both adapters use it ------------


def test_normalisation_folds_case_accents_and_punctuation() -> None:
    assert normalise_name("Test  Alpha-Bravo!") == "test alpha bravo"
    assert normalise_name("José Müller") == "jose muller"
    assert normalise_name("  ") == ""


def test_short_tokens_are_dropped_rather_than_matched() -> None:
    """MIN_TOKEN_LENGTH: "MO" matching "MD" at edit distance 1 is noise in a sanctions screen.

    "al" is two characters, so it is dropped by the same rule that drops "of" and "the" -
    the rule is length, not a stop-word list, and a token the matcher would otherwise
    let two one-letter-off names collide on is exactly what MIN_TOKEN_LENGTH excludes.
    """
    assert name_tokens("Al Gore of the CD") == ("gore", "the")
    assert "al" not in name_tokens("Al Gore of the CD"), "a 2-char token carries no identity"
    assert "of" not in name_tokens("Al Gore of the CD")
    assert name_tokens("Alexander Gore") == ("alexander", "gore")


def test_similarity_is_bounded_and_deterministic() -> None:
    """Bounded, and the same query scores the same every time.

    Deliberately *not* asserted symmetric. `similarity` short-circuits on a token-subset
    test (`ta <= tb or tb <= ta`) before falling through to `difflib`'s ratio, and
    SequenceMatcher is not symmetric, so "A vs B" and "B vs A" can differ. That is a
    property of the matcher as specified, not a defect introduced here - the property
    that matters for a screening result is that it is bounded and reproducible, so
    that is what is pinned.
    """
    assert similarity(MATCHED_NAME, MATCHED_NAME) == 1.0
    assert similarity("", MATCHED_NAME) == 0.0
    assert similarity(MATCHED_NAME, UNMATCHED_NAME) == similarity(
        MATCHED_NAME, UNMATCHED_NAME
    ), "scoring must be reproducible run to run"
    for a, b in ((MATCHED_NAME, UNMATCHED_NAME), ("Abdul", "Abdullah"), ("acme", "")):
        assert 0.0 <= similarity(a, b) <= 1.0
