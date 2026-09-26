"""Adverse-action reason codes: deterministic, no model needed to write them.

Plan §10 takes the **three largest negative point contributions** and renders each
as attribute plus bin, e.g. *"Pass-through ratio in top decile: minus 48 points"*.
Nothing here is learned: a reason code is a sort, a lookup and a format string.
That is the point. A score a human must be able to argue with cannot have reasons
written by a second model, because then the argument becomes about the writer
rather than the evidence.

Two properties are asserted in tests rather than aimed for:

* the ordering is total -- most negative first, then feature name -- so two runs
  produce the same three sentences on the same row;
* only *negative* contributions qualify. A large positive contribution is a reason
  the account looks safe, which is not an adverse action, and calling it one would
  be the sort of thing a regulator reads as a fiction.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from oxbow.scoring.binning import (
    KIND_CATEGORY_GROUP,
    KIND_MISSING,
    KIND_UNSEEN,
    KIND_ZERO,
)
from oxbow.scoring.config import ReasonConfig

MINUS_WORD: str = "minus"


@dataclass(frozen=True, slots=True)
class ReasonCode:
    """One adverse contribution, rendered and with its numbers attached."""

    feature: str
    attribute: str
    attribute_sentence: str
    bin_label: str
    bin_display: str
    points: int
    text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "attribute": self.attribute,
            "attribute_sentence": self.attribute_sentence,
            "bin_label": self.bin_label,
            "bin_display": self.bin_display,
            "points": self.points,
            "text": self.text,
        }


def short_attribute_label(sentence: str, feature: str) -> str:
    """A display name for the reason sentence, cut from the registry's own sentence.

    features.yaml declares one plain-English sentence per feature and P2 states it is
    "both the SHAP panel's dictionary and the scorecard's attribute labels", so the
    short form is derived from it rather than from a second naming scheme written in
    this layer. The leading clause is what reads as a name; the full sentence stays on
    the artefact as ``attribute_sentence`` so nothing is lost.
    """
    lead = re.split(r",| which | that | how ", sentence, maxsplit=1)[0].strip()
    lead = lead.rstrip(".:")
    if not lead or len(lead) > 60:
        lead = feature.replace("_", " ")
    return lead[:1].upper() + lead[1:]


def humanise_bin_label(label: str, kind: str) -> str:
    """Turn a bin key into the phrase a letter to a customer would use.

    The artefact keeps the machine key (``bin_label``) alongside this so a reviewer
    can trace the sentence back to the row of the table it came from; a readable
    label that cannot be traced is decoration.
    """
    if kind == KIND_MISSING:
        return "no data in the window"
    if kind == KIND_ZERO:
        return "a structural zero"
    if kind == KIND_UNSEEN:
        return "a category not seen when the model was built"
    if kind == KIND_CATEGORY_GROUP:
        inner = label.strip("[]")
        parts = [part.strip() for part in inner.split(",")]
        if len(parts) == 1:
            return f"category {parts[0]}"
        return "one of " + ", ".join(parts)
    lower, _, upper = label[1:-1].partition(", ")
    if lower == "-inf" and upper == "inf":
        return "its only observed range"
    if lower == "-inf":
        return f"the lowest band, under {upper}"
    if upper == "inf":
        return f"the top band, {lower} and above"
    return f"between {lower} and {upper}"


def format_points(points: int, cfg: ReasonConfig) -> str:
    """Render a point contribution, spelling the negative sign when config asks.

    ``reason_codes.points_signed_format`` is ``"{points:+d}"``; plan §10's example
    reads "minus 48 points". Both are satisfied: the signed number is always
    available to the UI, and the spoken form is used in the sentence when
    ``negative_word_form`` is on.
    """
    if points < 0 and cfg.negative_word_form:
        return f"{MINUS_WORD} {abs(points)}"
    return cfg.points_signed_format.format(points=points)


def top_reason_codes(
    points_by_feature: Mapping[str, int],
    bin_of_feature: Mapping[str, str],
    bin_kinds: Mapping[str, str],
    attribute_labels: Mapping[str, str],
    cfg: ReasonConfig,
) -> list[ReasonCode]:
    """The largest negative contributions, rendered in the adverse-action style.

    ``points_by_feature`` covers only the admitted scorecard attributes, so a row's
    reason codes can never cite a feature the scorecard did not use -- which is what
    makes the sentence checkable against the points table.
    """
    negatives = [
        (points, feature)
        for feature, points in points_by_feature.items()
        if int(points) < 0
    ]
    # Total order: most negative first, then the feature name. A reason list that
    # shuffled between runs would look like the model had changed.
    negatives.sort(key=lambda item: (item[0], item[1]))
    out: list[ReasonCode] = []
    for points, feature in negatives[: cfg.top_n]:
        label = bin_of_feature[feature]
        kind = bin_kinds[feature]
        display = humanise_bin_label(label, kind)
        sentence = attribute_labels.get(feature, feature)
        attribute = short_attribute_label(sentence, feature)
        text = cfg.template.format(
            attribute=attribute,
            bin_label=display,
            points_signed=format_points(points, cfg),
        )
        out.append(
            ReasonCode(
                feature=feature,
                attribute=attribute,
                attribute_sentence=sentence,
                bin_label=label,
                bin_display=display,
                points=points,
                text=text,
            )
        )
    return out


__all__ = [
    "MINUS_WORD",
    "ReasonCode",
    "format_points",
    "humanise_bin_label",
    "top_reason_codes",
]
