"""Model-layer exceptions.

Same rule as the scoring layer (03 A rule 1): boundary failures are loud and named.
What is specific to P4b is that some refusals are *not* errors to catch and swallow --
an uncalibrated probability must reach the UI labelled uncalibrated, and a fold with
zero positives must appear on the validation page rather than vanish from the
average. Those two therefore return values with reasons attached, and the classes
here are reserved for the cases where proceeding would fabricate a number.
"""

from __future__ import annotations


class ModelLayerError(RuntimeError):
    """Base class for the models layer."""


class FrameContractViolationError(ModelLayerError):
    """A scored frame handed to P4b is missing a column P4b cannot compute."""


class CalibrationRefusedError(ModelLayerError):
    """Calibration was refused because the validation evidence is too thin.

    Raised only when a caller demands a calibrated probability it is not allowed to
    have. The normal path returns a :class:`~oxbow.models.calibration.CalibrationOutcome`
    with ``refused=True`` so the run can continue and label itself, which is what the
    plan asks for: the UI says the probabilities are uncalibrated, it does not crash.
    """


class FusionInputsError(ModelLayerError):
    """The fusion input vector does not match ``model.yaml fusion.inputs``."""


class DegenerateTreeError(ModelLayerError):
    """The boosted ensemble carries no split, so SHAP has nothing to attribute."""


class OptunaBudgetError(ModelLayerError):
    """The tuning budget as configured cannot be honoured."""


class TrackingUnavailableError(ModelLayerError):
    """Neither the tracking server nor the local file store could be written to.

    Losing the model registry is a degraded run, not a failed score: the artefacts
    carry the model hash, so the run is still reproducible and the banner says which
    part is missing.
    """
