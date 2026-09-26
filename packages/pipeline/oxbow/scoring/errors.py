"""Scoring-layer exceptions.

03 A rule 1: fail loud at the boundary, degrade gracefully in the middle, never
fail silent anywhere. Every class here is a *boundary* failure: a scorecard that
would otherwise emit a plausible number on impossible input raises instead, and
the message names the input that caused it. A scorecard that guesses is worse
than one that stops, because the guess travels into Module C and comes back as a
currency figure.
"""

from __future__ import annotations


class ScorecardError(RuntimeError):
    """Base class for the scorecard layer.

    Subclasses are used rather than a bare ``ValueError`` so a caller upstream
    (the CLI, P6's harness) can tell "the scorecard refused" apart from "the
    scorecard has a bug", which is the difference between a labelled degraded run
    and a crash report.
    """


class FrameContractError(ScorecardError):
    """The training frame does not satisfy the agreed input contract.

    Raised with the offending column named: 02 D's rule for a silently dropped
    row applies to a silently accepted column.
    """


class FeatureSpecHashMismatch(ScorecardError):  # noqa: N818 - re-exported from oxbow.scoring; renaming is an API change
    """Frame feature-spec hash disagrees with the hash recorded at training.

    Plan §8, seam 3 of 02 B: trained on one feature set, scored with another is
    the failure this hash exists to make impossible. Refusing to score is the
    only correct response; scoring anyway produces a confident number computed
    from the wrong inputs.
    """


class SeparationDetectedError(ScorecardError):
    """A feature separates the classes, so its coefficient is not estimable.

    Carries the feature names because the guard is only useful if it points at
    the culprit (03 H: ``test_separation_detected``).
    """

    def __init__(self, feature_names: tuple[str, ...], detail: str) -> None:
        super().__init__(
            f"separation detected in {len(feature_names)} feature(s): "
            f"{', '.join(feature_names)}. {detail}"
        )
        self.feature_names = feature_names


class NoAdmittedFeaturesError(ScorecardError):
    """Nothing survived IV admission, so there is no scorecard to fit.

    An empty scorecard reported as a zero score for every account would look like
    a quiet period rather than a build failure (03 A rule 2: never let an unknown
    become a zero).
    """


class WoeNotFiniteError(ScorecardError):
    """A WOE value survived the floor-merge and smoothing passes still infinite.

    This is an invariant violation, not a data condition: 03 H
    ``test_no_infinite_woe`` guarantees it cannot happen, so reaching it means the
    merge or smoothing rule is broken and the run must stop.
    """


class PointsInvariantError(ScorecardError):
    """Per-attribute points do not sum to the score on a row.

    The scorecard's entire claim is that a human can add the column up. If the
    identity fails on even one row the claim is false, so it is an exception
    rather than a warning.
    """


class BandFitError(ScorecardError):
    """The band cut points could not be fitted on observed validation rates."""


class ScorecardFitError(ScorecardError):
    """The fit population makes a scorecard impossible.

    No rows, no positives, no admitted features, no validation slice: each of these
    would otherwise produce an artefact that serialises beautifully and means
    nothing, so the build stops and says which precondition failed.
    """


class DegenerateBinningError(ScorecardError):
    """A feature's binning produced no usable boundaries and no fallback either."""
