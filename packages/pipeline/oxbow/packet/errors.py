"""Every way a case packet can refuse to be produced.

Plan §15 makes the packet an *exhibit*, not a print job: an exhibit that cannot
account for its own numbers is worse than no exhibit, because it carries the
court's confidence anyway. So each refusal below is a type a caller can catch
precisely, and each message names the thing that is missing rather than
describing a problem in the abstract.

One rule shapes all of them: **the packet never substitutes**. Where the web UI
may degrade to a labelled fallback (03 §12.8), a packet either carries the real
artifact or it does not exist. A silent fallback in a document that asserts its
own provenance is the rejection trigger in plan §18 wearing a suit.

Every error derives from :class:`PacketError`, which is a ``RuntimeError``
subclass, so ``except PacketError`` catches the family without ever catching a
broad ``Exception`` and swallowing a real fault (01 §G).
"""

from __future__ import annotations

from typing import Final


class PacketError(RuntimeError):
    """Base class for packet refusals. Catch this, never ``Exception``."""


class MissingArtifactError(PacketError):
    """An artifact the packet needs is not on disk.

    Raised with the *path* and the *producing stage* named, because the honest
    failure at 02:00 on demo day is "``out/p4/scored_rows.parquet`` does not
    exist — run ``make score``", not a blank section and not a plausible default.
    """


class MissingAssumptionLineError(PacketError):
    """A currency figure reached the renderer without its assumption block.

    The plan §18 rejection trigger, in exception form. ``quant/economics.py``
    already refuses this at the type level; this is the packet-side refusal for
    figures assembled from several sources, where the block can go missing in the
    seam between them.
    """


class EmptyDecisionReasonError(PacketError):
    """The decision reason is blank, so there is nothing to sign.

    Refusing an empty reason is **P7's server-side rule** — the ``decision``
    table carries ``ck_decision_reason_not_blank``, and the UI blocks submission.
    A packet can be built from a bundle that arrived over a seam that did not
    enforce it (a hand-written fixture, a restored dump, an API that was
    downgraded), so the packet refuses defensively and says which rule it is
    echoing. An exhibit for a decision nobody justified is not an exhibit.
    """


class ChainIntegrityError(PacketError):
    """The hash chain did not verify on export.

    Carries the sequence number, because "the chain is broken" is not actionable
    and "the chain is broken at seq=41 (digest mismatch: this row was edited after
    it was written)" is (plan §13: *prints OK or the first broken link naming the
    sequence number*).
    """

    def __init__(self, seq: int, reason: str, *, expected: str, actual: str) -> None:
        super().__init__(
            f"audit chain failed verification on export at seq={seq}: {reason}\n"
            f"     expected {expected}\n"
            f"     actual   {actual}"
        )
        self.seq = seq
        self.reason = reason
        self.expected = expected
        self.actual = actual


class AuditMismatchError(PacketError):
    """The audit row and the case bundle disagree about the same decision.

    The packet's integrity claim is that what it prints *is* what was signed. If
    the digest-covered payload says one thing and the delivered bundle another,
    one of the two was rewritten, and rendering either would launder the doubt.
    """


class PinnedRunMismatchError(PacketError):
    """Evidence, explanation or subgraph came from a run other than the pinned one.

    Plan §15: a case opened under run A and decided after run B must render the
    evidence **as it was at decision time**. The mechanism is that every snapshot
    carries the ``run_id`` it was read from, and the renderer refuses a snapshot
    whose run is not the case's pinned run. Without that check the packet would
    quietly mix a decided-on run-A score with run-B transactions and look
    internally consistent while describing a transaction that never happened.
    """


class SubgraphArtifactError(PacketError):
    """The subgraph picture cannot be drawn honestly from the artifact.

    Two shapes matter: the artifact is missing entirely, and the artifact exists
    but the seed account is not in it. The second one is the dangerous one,
    because drawing an empty diagram of an account the graph never saw would
    render "no network" as a finding rather than as a window limitation
    (03 §A rule 2: never let an unknown become a figure).
    """


class TemplateRenderError(PacketError):
    """A template variable is missing.

    Jinja is configured with ``StrictUndefined`` so this is raised, not
    silently rendered as an empty cell — a blank money column in a signed
    document reads as "there was none".
    """


DISCLAIMER_MISSING: Final = (
    "the packet cover must carry the OXBOW disclaimer verbatim (plan §15)"
)

__all__ = [
    "DISCLAIMER_MISSING",
    "AuditMismatchError",
    "ChainIntegrityError",
    "EmptyDecisionReasonError",
    "MissingArtifactError",
    "MissingAssumptionLineError",
    "PacketError",
    "PinnedRunMismatchError",
    "SubgraphArtifactError",
    "TemplateRenderError",
]
