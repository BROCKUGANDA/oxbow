"""Feature admission by information value, with the refusal made visible.

Plan §10: **keep 0.02 <= IV <= 0.5.** The two bounds are not symmetric in meaning.

* Below 0.02 the feature says almost nothing; it adds a coefficient the auditors
  cannot explain, so it is excluded and recorded.
* Above 0.5 the feature says *too much*, and in financial-crime data the commonest
  explanation is that the label leaked into it. Exclusion is the default and the
  only way a high-IV feature enters is a written justification, which the artefact
  then carries verbatim next to the IV. Plan §10 asks for the rule to be visible on
  the page "because self-imposed constraints read as rigor"; making the refusal a
  field of the model artefact is the stronger form of the same idea -- a reader can
  see which features were refused and on what grounds, not just that a policy exists.

The label-correlation bound is features.yaml's guard (`guards.max_abs_correlation_with_label`,
plan §8) re-checked here at the last boundary before a coefficient exists. P2 owns
the feature table; that does not make P4 innocent of training on a leak.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import numpy as np

from oxbow.scoring.binning import FeatureBinning
from oxbow.scoring.config import AdmissionConfig

DECISION_ADMITTED: Final = "admitted"
DECISION_REFUSED_LOW_IV: Final = "refused_low_iv"
DECISION_REFUSED_SUSPECTED_LEAKAGE: Final = "refused_suspected_leakage"
DECISION_REFUSED_JUSTIFIED_LEAKAGE: Final = "admitted_with_written_justification"
DECISION_REFUSED_LABEL_CORRELATION: Final = "refused_label_correlation"

MIN_JUSTIFICATION_CHARACTERS: Final = 40


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """One feature's admission outcome, with the rule that produced it."""

    feature: str
    iv: float
    decision: str
    rule: str
    justification: str | None
    abs_correlation_with_label: float
    bins: int

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "iv": round(self.iv, 6),
            "decision": self.decision,
            "rule": self.rule,
            "justification": self.justification,
            "abs_correlation_with_label": round(self.abs_correlation_with_label, 6),
            "bins": self.bins,
        }


@dataclass(frozen=True, slots=True)
class SelectionOutcome:
    """What was admitted and, equally important, what was refused and why."""

    admitted: tuple[str, ...]
    decisions: tuple[AdmissionDecision, ...]

    def refused(self) -> tuple[AdmissionDecision, ...]:
        return tuple(d for d in self.decisions if not d.decision.startswith("admitted"))

    def suspected_leakage(self) -> tuple[AdmissionDecision, ...]:
        return tuple(
            d
            for d in self.decisions
            if d.decision in (DECISION_REFUSED_SUSPECTED_LEAKAGE, DECISION_REFUSED_JUSTIFIED_LEAKAGE)
        )

    @property
    def total_iv_admitted(self) -> float:
        table = {d.feature: d.iv for d in self.decisions}
        return float(sum(table[name] for name in self.admitted))

    def to_dict(self) -> dict[str, object]:
        return {
            "admitted": list(self.admitted),
            "admitted_count": len(self.admitted),
            "refused_count": len(self.refused()),
            "suspected_leakage_count": sum(
                1 for d in self.decisions if d.decision == DECISION_REFUSED_SUSPECTED_LEAKAGE
            ),
            "total_iv_admitted": round(self.total_iv_admitted, 4),
            "decisions": [d.to_dict() for d in self.decisions],
        }


def abs_correlation_with_label(woe: np.ndarray, labels: np.ndarray) -> float:
    """Absolute Pearson correlation of a feature's WOE column against the label.

    WOE is used rather than the raw value because WOE is what the scorecard is
    fitted on, and it is defined for both numeric and categorical features. A value
    near one means the column is very nearly the label.
    """
    if woe.size < 2:
        return 0.0
    left = woe - float(np.mean(woe))
    right = labels.astype(np.float64) - float(np.mean(labels))
    denominator = float(np.sqrt(np.sum(left * left) * np.sum(right * right)))
    if denominator == 0.0:
        return 0.0
    return abs(float(np.sum(left * right) / denominator))


def select_features(
    binnings: Mapping[str, FeatureBinning],
    woe_columns: Mapping[str, np.ndarray],
    labels: np.ndarray,
    cfg: AdmissionConfig,
    max_abs_correlation_with_label: float,
    justifications: Mapping[str, str] | None = None,
) -> SelectionOutcome:
    """Admit or refuse every binned feature, recording the rule for each.

    ``justifications`` is the written-justification channel: a feature above the IV
    ceiling is admitted only when the caller supplies text of at least
    ``MIN_JUSTIFICATION_CHARACTERS`` characters naming why the information is
    legitimate. The default is refusal, so silence excludes -- the direction the
    plan requires.
    """
    written = dict(justifications or {})
    decisions: list[AdmissionDecision] = []
    admitted: list[str] = []

    for feature in sorted(binnings):
        binning = binnings[feature]
        iv = binning.iv
        correlation = abs_correlation_with_label(woe_columns[feature], labels)
        justification = written.get(feature)

        if correlation >= max_abs_correlation_with_label:
            decision = DECISION_REFUSED_LABEL_CORRELATION
            rule = (
                f"features.yaml guards.max_abs_correlation_with_label="
                f"{max_abs_correlation_with_label}: |corr|={correlation:.4f} with the label"
            )
        elif iv > cfg.iv_max:
            if justification is not None and len(justification.strip()) >= MIN_JUSTIFICATION_CHARACTERS:
                decision = DECISION_REFUSED_JUSTIFIED_LEAKAGE
                rule = (
                    f"iv={iv:.4f} > iv_bounds.max={cfg.iv_max} and the ceiling action is "
                    f"{cfg.above_max_action!r}: a written justification was supplied, so the "
                    "feature enters with the text recorded in the artefact"
                )
            else:
                decision = DECISION_REFUSED_SUSPECTED_LEAKAGE
                rule = (
                    f"iv={iv:.4f} > iv_bounds.max={cfg.iv_max}: suspected leakage. "
                    f"Action {cfg.above_max_action!r}; no written justification of at least "
                    f"{MIN_JUSTIFICATION_CHARACTERS} characters was supplied."
                )
        elif iv < cfg.iv_min:
            decision = DECISION_REFUSED_LOW_IV
            rule = f"iv={iv:.4f} < iv_bounds.min={cfg.iv_min}: too little information to explain"
        else:
            decision = DECISION_ADMITTED
            rule = f"iv_bounds.min={cfg.iv_min} <= iv={iv:.4f} <= iv_bounds.max={cfg.iv_max}"

        if decision in (DECISION_ADMITTED, DECISION_REFUSED_JUSTIFIED_LEAKAGE):
            admitted.append(feature)

        decisions.append(
            AdmissionDecision(
                feature=feature,
                iv=iv,
                decision=decision,
                rule=rule,
                justification=justification.strip() if justification else None,
                abs_correlation_with_label=correlation,
                bins=len(binning.rows),
            )
        )

    return SelectionOutcome(admitted=tuple(admitted), decisions=tuple(decisions))


__all__ = [
    "DECISION_ADMITTED",
    "DECISION_REFUSED_JUSTIFIED_LEAKAGE",
    "DECISION_REFUSED_LABEL_CORRELATION",
    "DECISION_REFUSED_LOW_IV",
    "DECISION_REFUSED_SUSPECTED_LEAKAGE",
    "MIN_JUSTIFICATION_CHARACTERS",
    "AdmissionDecision",
    "SelectionOutcome",
    "abs_correlation_with_label",
    "select_features",
]
