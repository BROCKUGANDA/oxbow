"""The scorecard's artefact readers round-trip — or they fail naming the field.

WHY THIS FILE EXISTS AT ALL. ``band_table_from_dict`` / ``binning_from_dict`` /
``selection_from_dict`` / ``scaling_from_dict`` are the "score without retraining" path: a landed
artefact is read back so a case page can explain a score years after the fit. None of the four had
a test, and the first one was simply broken — ``BandRow`` declares ``merged_from`` and
``merge_reason``, ``BandRow.to_dict`` writes both, and the reader passed neither, so calling it
raised ``TypeError: missing 2 required positional arguments``. An exported function that has never
been called is not a feature; it is a bug with a name on it. mypy found it (``[call-arg]``) where
four years of green tests had not, because it was the one path nobody exercised.

Every assertion here is on the *composed object* after a round trip through JSON text, not on the
dict the writer produced: a field that survives ``to_dict()`` and dies in ``from_dict()`` is
exactly the failure being guarded, and comparing two dicts would also pass when both are wrong.

The coercion half matters as much as the round trip. These readers used to wrap every access in
``int(...)`` / ``float(...)`` / ``str(...)`` over an ``object``, which means a corrupt artefact
raised a bare ``TypeError: int() argument must be...`` from inside a rebuild — naming no field,
naming no artefact, and indistinguishable from a bug in this code. They now name what they wanted
and what they got.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from oxbow.scoring.bands import BandTable, fit_bands  # noqa: E402
from oxbow.scoring.config import load_scorecard_config  # noqa: E402
from oxbow.scoring.model import (  # noqa: E402
    ScorecardFitError,
    band_table_from_dict,
    scaling_from_dict,
)

# 24 validation rows, one point per row, scores 0..23 with the four worst being the positives:
# a band cut on OBSERVED bad rate puts every positive in the top band and nothing below it.
SCORES = np.arange(24, dtype=np.float64)
LABELS = np.zeros(24, dtype=np.int32)
LABELS[20:] = 1


def _fitted_table() -> BandTable:
    cfg = load_scorecard_config(REPO_ROOT)
    table, _assignments = fit_bands(SCORES, LABELS, cfg.bands)
    return table


def _through_artefact_json(payload: dict[str, object]) -> dict[str, Any]:
    """Round trip as text, because that is what the artefact actually is."""
    return json.loads(json.dumps(payload))


# ---------------------------------------------------------------------------


def _assert_same_band_table(rebuilt: BandTable, table: BandTable) -> None:
    """Equal where it must be exact, within the writer's rounding where it is a float.

    ``BandRow.to_dict`` rounds rates to 8 decimal places on purpose (a float in an artefact is a
    rendering decision, and the plan's money rule is the same idea one level up), so ``==`` on the
    dataclass is the wrong assertion: it would fail for the right reason and pass for a
    coincidence. Strings, ids, counts, band membership and the merge provenance are exact.
    """
    assert [row.band_id for row in rebuilt.rows] == [row.band_id for row in table.rows]
    assert rebuilt.n_rows == table.n_rows
    assert rebuilt.method == table.method
    for before, after in zip(table.rows, rebuilt.rows, strict=True):
        assert before.glyph == after.glyph, "the twelve-glyph design ships through this payload"
        for field in before.__dataclass_fields__:
            value, restored = getattr(before, field), getattr(after, field)
            message = f"{before.band_id}.{field}: {value!r} -> {restored!r}"
            if isinstance(value, float):
                assert restored == pytest.approx(value, abs=1e-7), message
            elif isinstance(value, int | str | tuple | None):  # bool is an int; None is exact
                assert restored == value, message
            else:
                raise AssertionError(
                    f"{before.band_id}.{field} is {type(value).__name__}, which this guard does "
                    "not compare — a new BandRow field would otherwise pass unasserted"
                )
    assert rebuilt.boundaries() == table.boundaries()


def test_a_band_table_survives_the_round_trip_including_its_merge_provenance() -> None:
    """The bug this file was written for.

    Before the fix this raised ``TypeError`` from inside ``BandRow(...)``: the reader never passed
    ``merged_from`` or ``merge_reason``, both required on the dataclass and both written by
    ``BandRow.to_dict``. A rebuilt table that lost them could not say why a band covers the point
    range it does, which is the question the studio's band panel exists to answer.
    """
    table = _fitted_table()
    rebuilt = band_table_from_dict(_through_artefact_json(table.to_dict()))

    _assert_same_band_table(rebuilt, table)
    assert all(row.merge_reason is not None or row.merged_from == () for row in rebuilt.rows)


def test_a_non_finite_rate_survives_as_a_non_finite_rate_not_a_refusal() -> None:
    """JSON has no NaN, so the writer maps it to null; the reader has to map it back.

    The old code did neither: it rebuilt with ``float(None)``, which raises a bare TypeError, so an
    artefact for a band with no observed rate could not be read back at all. This fixture has four
    such entries, which is why the fitted table is the one that proves it.
    """
    table = _fitted_table()
    assert any(
        value != value for value in table.smoothed_rate_at_cut
    ), "the fixture must actually contain a NaN, or this asserts nothing"

    rebuilt = band_table_from_dict(_through_artefact_json(table.to_dict()))
    assert len(rebuilt.smoothed_rate_at_cut) == len(table.smoothed_rate_at_cut)
    for before, after in zip(table.smoothed_rate_at_cut, rebuilt.smoothed_rate_at_cut, strict=True):
        if before != before:
            assert after != after, "null came back as something other than NaN"
        else:
            assert after == pytest.approx(before, abs=1e-7)


def test_a_band_table_that_rebuilds_from_text_assigns_the_same_bands() -> None:
    """The point of the path: a rebuilt table must cut scores identically to the fitted one."""
    from oxbow.scoring.bands import assign_bands

    table = _fitted_table()
    rebuilt = band_table_from_dict(_through_artefact_json(table.to_dict()))
    scores = SCORES.astype(int)
    ids = tuple(row.band_id for row in table.rows)

    assert list(assign_bands(scores, rebuilt.boundaries(), ids)) == list(
        assign_bands(scores, table.boundaries(), ids)
    ), "a rebuilt boundary set that moves one account is a different scorecard"


def test_a_missing_key_names_the_field_instead_of_raising_a_bare_keyerror() -> None:
    payload = _through_artefact_json(_fitted_table().to_dict())
    del payload["base_rate"]

    with pytest.raises(ScorecardFitError, match="base_rate"):
        band_table_from_dict(payload)


def test_a_corrupt_number_names_the_field_and_what_it_found() -> None:
    """``int("12")`` used to succeed silently; a float where a count belongs used to round.

    Both are refusals now, because an artefact whose population is a string or a fraction is a
    corrupt artefact, not a formatting preference — and a reader that quietly coerces is how a
    band table starts disagreeing with the rows it claims to describe.
    """
    payload = _through_artefact_json(_fitted_table().to_dict())
    payload["n_rows"] = "1_000"
    with pytest.raises(ScorecardFitError, match="n_rows.*expected an integer"):
        band_table_from_dict(payload)

    payload["n_rows"] = 1000.5
    with pytest.raises(ScorecardFitError, match="n_rows"):
        band_table_from_dict(payload)

    payload = _through_artefact_json(_fitted_table().to_dict())
    payload["bands"][0]["population_share"] = "0.2"
    with pytest.raises(ScorecardFitError, match="population_share.*expected a number"):
        band_table_from_dict(payload)


def test_a_boolean_is_not_an_integer_and_a_string_is_not_a_boolean() -> None:
    """DEV-005's rule, applied to artefact reading: ``True`` is Python's ``int`` and is not a count."""
    payload = _through_artefact_json(_fitted_table().to_dict())
    payload["n_rows"] = True
    with pytest.raises(ScorecardFitError, match="expected an integer, got True"):
        band_table_from_dict(payload)


def test_a_field_that_must_be_a_list_of_labels_says_so() -> None:
    """``tuple(str(item) for item in 12)`` would have been a TypeError about iteration."""
    payload = _through_artefact_json(_fitted_table().to_dict())
    payload["bands"][0]["merged_from"] = "A,B"  # a string IS a Sequence; it is not a list of labels
    with pytest.raises(ScorecardFitError, match="merged_from"):
        band_table_from_dict(payload)

    payload["bands"][0]["merged_from"] = 12
    with pytest.raises(ScorecardFitError, match="merged_from"):
        band_table_from_dict(payload)


def test_a_null_merge_reason_is_the_measured_absence_and_round_trips() -> None:
    """None is a value here, not a missing key: most bands are not merges."""
    table = _fitted_table()
    assert all(row.merge_reason is None or isinstance(row.merge_reason, str) for row in table.rows)
    rebuilt = band_table_from_dict(_through_artefact_json(table.to_dict()))
    assert [row.merge_reason for row in rebuilt.rows] == [row.merge_reason for row in table.rows]


def test_the_scaling_constants_round_trip_too() -> None:
    """The second reader in the same family, with the same coercion failure modes."""
    from oxbow.scoring.scale import build_scaling

    cfg = load_scorecard_config(REPO_ROOT)
    constants = build_scaling(cfg.scaling, intercept=0.5)
    payload = _through_artefact_json(constants.to_dict())
    rebuilt = scaling_from_dict(payload)

    assert rebuilt.pdo == constants.pdo
    assert rebuilt.base_score == constants.base_score
    assert rebuilt.base_odds == constants.base_odds
    assert rebuilt.factor == pytest.approx(constants.factor, abs=1e-5)
    assert rebuilt.base_points == constants.base_points

    broken = _through_artefact_json(constants.to_dict())
    broken["pdo"] = "20"
    with pytest.raises(ScorecardFitError, match="pdo"):
        scaling_from_dict(broken)

    del_key = _through_artefact_json(constants.to_dict())
    del del_key["base_points"]
    with pytest.raises(ScorecardFitError, match="base_points"):
        scaling_from_dict(del_key)
