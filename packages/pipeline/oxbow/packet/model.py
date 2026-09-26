"""The packet's own view of a decided case: what it holds, and what it refuses.

Plan §15 defines the packet by its contents — evidence, SHAP contributions,
subgraph image, the decision and its free-text reason, model version, run id,
disclaimer — and by two properties: it renders from the **pinned run**, and it is
**byte-identical across two renders** of that run.

Byte-identity is not achieved by being careful about the template. It is achieved
by a type rule: nothing in this module reads a clock, a locale, a working
directory, a network, or ``os.environ``. Every instant below is a value that was
already recorded by the pipeline or by a reviewer, carried in from a landed
artifact, and the document's own timestamps are those recorded instants. The one
place a date is *derived* is the deployment timezone label, and it is derived from
``config/pipeline.yaml`` rather than from the host, so a packet rendered in
Kampala and a packet rendered in London off the same artifacts carry the same
wall text.

Ordering is total everywhere it matters. Lists arrive pre-sorted from the
loaders, and the sorts use the run's documented tie-break
(``determinism.sort_keys`` in ``config/pipeline.yaml``) so the packet's row order
matches the queue's, and two renders of the same rows cannot disagree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from oxbow.audit.chain import ChainRow
from oxbow.packet.errors import (
    AuditMismatchError,
    EmptyDecisionReasonError,
    MissingAssumptionLineError,
    PinnedRunMismatchError,
)
from oxbow.ports.case_sink import (
    DECISION_ACTIONS,
    OXBOW_DISCLAIMER,
    CaseBundle,
    DecisionRecord,
)
from oxbow.quant.economics import AssumptionBlock, Economics
from oxbow.quant.money import Money, ratio_to_micro, to_major_text

#: Bumped when the *layout contract* changes, not when a number changes. Printed on
#: the cover so a reader can tell which packet renderer produced a document.
PACKET_VERSION: Final = "oxbow-packet-v1"

#: The explanation sources the packet will print. Anything else is a bug in a
#: loader rather than a fourth kind of explanation, and it fails closed.
EXPLANATION_SOURCES: Final = ("shap-tree-explainer", "scorecard-explained")

BAND_LETTERS: Final = ("A", "B", "C", "D", "E")

#: The two money-shaped things the decision bundle carries. Listed so the renderer
#: can assert it printed every one of them rather than trusting a template.
BUNDLE_MONEY_FIELDS: Final = ("exposure_minor", "expected_value_minor")


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """One canonical event, as it existed in the pinned run.

    ``amount_minor`` stays an integer minor unit all the way to the template: the
    division by 100 happens in :meth:`MoneyText.render` at render time and nowhere
    earlier (DEV-005). The currency travels with the amount because summing UGX
    cents and EUR cents is a defect, not a rounding question.
    """

    txn_id: str
    occurred_at: datetime
    txn_type: str
    amount_minor: int
    currency: str
    account_from: str
    account_to: str
    subject_account: str
    column_pointer: str

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise AuditMismatchError(
                f"{self.txn_id}: amount_minor must be an integer count of minor units, "
                f"got {type(self.amount_minor).__name__}. Float money cannot be summed "
                "and cannot be audited (DEV-005)."
            )

    @property
    def counterparty(self) -> str:
        """The other side of the transfer, for the account under review."""
        return self.account_to if self.account_from == self.subject_account else self.account_from

    @property
    def direction(self) -> str:
        """``out`` when value leaves the subject, ``in`` when it arrives, ``self``."""
        if self.account_from == self.account_to:
            return "self"
        return "out" if self.account_from == self.subject_account else "in"

    def money(self) -> Money:
        """The amount as the quant layer carries it."""
        return Money(self.amount_minor, self.currency)


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """The evidence bundle for one account **as of one run**.

    ``run_id`` is load-bearing, not bookkeeping: :meth:`require_run` is what makes
    plan §15's pinned-run rule mechanical. A snapshot assembled from the wrong run
    would still render a plausible table — that is precisely why it is refused.
    """

    run_id: str
    account_key: str
    rows: tuple[EvidenceRow, ...]
    source: str
    window_start: datetime
    window_end: datetime

    def require_run(self, run_id: str, *, what: str) -> None:
        """Refuse to be used under a different pinned run than this snapshot came from."""
        if self.run_id != run_id:
            raise PinnedRunMismatchError(
                f"{what} was read from run {self.run_id} but this case is pinned to "
                f"{run_id}. Plan §15 requires the packet to show the evidence as it was "
                "at decision time; re-reading the live table after a newer run would "
                "print a transaction history the decision was not made on."
            )

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def totals_by_currency(self) -> tuple[tuple[str, int], ...]:
        """Per-currency totals, ascending by code. Never one number across two monies."""
        totals: dict[str, int] = {}
        for row in self.rows:
            totals[row.currency] = totals.get(row.currency, 0) + row.amount_minor
        return tuple(sorted(totals.items()))


# --------------------------------------------------------------------------
# explanation
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContributionRow:
    """One feature's share of the explanation, signed.

    ``contribution`` is a logit-space SHAP value (or a scorecard point total on the
    fallback path) and is *not* money, so it carries no assumption block. It does
    carry ``unit`` so the template cannot present a point total as a SHAP value.
    """

    feature: str
    contribution: float
    unit: str
    display_value: str | None = None


@dataclass(frozen=True, slots=True)
class ExplanationSnapshot:
    """SHAP contributions for one scored row, or the labelled scorecard fallback.

    ``source`` distinguishes them, and the packet prints the distinction (plan §10:
    a degenerate tree is labelled *scorecard-explained*, not left blank). The
    fallback is a different explanation, never a missing one.
    """

    run_id: str
    account_key: str
    source: str
    base_value: float
    base_unit: str
    rows: tuple[ContributionRow, ...]
    source_pointer: str
    note: str
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        if self.source not in EXPLANATION_SOURCES:
            raise AuditMismatchError(
                f"unknown explanation source {self.source!r}; the packet prints "
                f"{EXPLANATION_SOURCES} and nothing else"
            )

    @property
    def is_scorecard_explained(self) -> bool:
        return self.source == "scorecard-explained"

    def require_run(self, run_id: str, *, what: str) -> None:
        if self.run_id != run_id:
            raise PinnedRunMismatchError(
                f"{what} came from run {self.run_id} but this case is pinned to {run_id}. "
                "The SHAP values a decision was argued from belong to the model that "
                "produced the score, which belongs to the run."
            )


# --------------------------------------------------------------------------
# subgraph
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubgraphNode:
    """One drawn node. ``node_type`` keeps rails visually distinct without colour."""

    node_id: str
    hop: int
    node_type: str
    degree: int | None
    member_count: int | None = None


@dataclass(frozen=True, slots=True)
class SubgraphEdge:
    """One drawn edge, aggregated per currency.

    ``total_value_minor`` is per ``(from, to, currency)`` and never a sum across a
    currency boundary, because a rendered graph that added two monies together
    would be the one figure in the packet with no arithmetic behind it.
    """

    account_from: str
    account_to: str
    currency: str
    edge_count: int
    total_value_minor: int
    first_ts_us: int
    last_ts_us: int


@dataclass(frozen=True, slots=True)
class SubgraphView:
    """The picture the packet draws, and the disclosure that comes with it.

    ``truncated`` and ``truncation_reason`` print *on the image*. A cropped network
    diagram without its crop stated is the "density is not evidence" failure in
    plan §18, and the reader cannot tell a quiet account from a cut-off one.
    """

    run_id: str
    seed: str
    hops: int
    nodes: tuple[SubgraphNode, ...]
    edges: tuple[SubgraphEdge, ...]
    source: str
    truncated: bool = False
    truncation_reason: str | None = None
    reachable_before_cap: int = 0

    def require_run(self, run_id: str, *, what: str) -> None:
        if self.run_id != run_id:
            raise PinnedRunMismatchError(
                f"{what} was drawn from run {self.run_id} but this case is pinned to "
                f"{run_id}. Edges that did not exist at decision time cannot be evidence "
                "for a decision made before them."
            )

    @property
    def self_loop_count(self) -> int:
        return sum(1 for edge in self.edges if edge.account_from == edge.account_to)


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MoneyText:
    """A currency figure with its assumption line attached at construction.

    There is no field for "the number" without "the block": :func:`money_line` is
    the only constructor, and it refuses a missing block. The rendered form is
    ``1,234,567.00 UGX`` — the division by ``minor_units_per_major`` happens here,
    at render time, on the integer that was summed (03 §A: totals still match
    their rendered rows).
    """

    label: str
    minor: int
    currency: str
    basis: str
    block_text: str
    per_major: int

    def render(self) -> str:
        """Major units with the ISO code. The only producer of this string."""
        return to_major_text(self.minor, self.currency, self.per_major)


def money_line(
    label: str,
    amount: Money | int,
    *,
    block: AssumptionBlock | None,
    basis: str,
    currency: str | None = None,
    per_major: int | None = None,
) -> MoneyText:
    """Build a currency figure, refusing to produce one without its assumptions.

    ``basis`` is the sentence that says *what this figure is a function of* — the
    recovery rate it was priced at, the exposure definition behind it — as distinct
    from ``block``, which is the whole assumption set verbatim. Plan §11 asks for
    both: the line beside the number, and the block that lets a reader change it.
    """
    if block is None:
        raise MissingAssumptionLineError(
            f"refusing to render {label!r} without its assumption block. A currency "
            "figure with no stated basis is plan §18's rejection trigger; load "
            "config/economics.yaml and pass assumption_block()."
        )
    minor = amount.minor if isinstance(amount, Money) else amount
    if isinstance(amount, Money) and currency is not None and amount.currency != currency.upper():
        raise MissingAssumptionLineError(
            f"{label!r}: {amount.currency} amount under {currency} assumptions. There "
            "is no implicit FX in this product (02 money rules)."
        )
    code = amount.currency if isinstance(amount, Money) else str(currency or "").upper()
    if len(code) != 3:
        raise MissingAssumptionLineError(
            f"{label!r} has no ISO currency code ({code!r}); a money figure without a "
            "currency is not a money figure."
        )
    return MoneyText(
        label=label,
        minor=minor,
        currency=code,
        basis=basis,
        block_text=block.text,
        per_major=per_major if per_major is not None else block.per_major,
    )


# --------------------------------------------------------------------------
# integrity
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One audit row of this case's decision chain, as the chain recorded it.

    Append-only means a reversal is a new row referencing the original and **both**
    appear here (plan §15). The digest is printed so a reader holding two copies of
    the packet can tell whether they hold the same fact.
    """

    seq: int
    occurred_at: datetime
    actor_id: str
    action: str
    reason: str
    row_hash: str
    prev_hash: str
    reversal_of_seq: int | None
    decided_on_superseded_run: bool

    @property
    def action_verb(self) -> str:
        """``decision:escalate`` -> ``escalate``."""
        _, _, verb = self.action.partition(":")
        return verb or self.action


def timeline_from_chain(chain: Sequence[ChainRow], case_id: str) -> tuple[TimelineEntry, ...]:
    """Every row of the chain that concerns ``case_id``, in sequence order.

    Selection is by the payload's ``case_id`` (which the digest covers) and not by
    the subject string, so an editor who relabelled a subject could not remove a
    row from a case's timeline — they would have to change the payload, which
    changes the digest, which :func:`oxbow.audit.chain.verify_chain` reports by
    sequence number.
    """
    entries: list[TimelineEntry] = []
    for row in chain:
        payload: Mapping[str, Any] = row.payload
        if str(payload.get("case_id", "")) != case_id:
            continue
        action = str(row.action)
        verb = action.partition(":")[2]
        if verb and verb not in DECISION_ACTIONS:
            raise AuditMismatchError(
                f"seq={row.seq} carries action {action!r}, which is not one of "
                f"{DECISION_ACTIONS}. A decision row the port does not recognise "
                "cannot be printed as a decision."
            )
        reversal = payload.get("reversal_of_seq")
        entries.append(
            TimelineEntry(
                seq=row.seq,
                occurred_at=row.occurred_at,
                actor_id=row.actor_id,
                action=action,
                reason=str(payload.get("reason", "")),
                row_hash=row.row_hash,
                prev_hash=row.prev_hash,
                reversal_of_seq=None if reversal is None else int(str(reversal)),
                decided_on_superseded_run=bool(payload.get("decided_on_superseded_run", False)),
            )
        )
    return tuple(entries)


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One run in the registry, as recorded by the pipeline."""

    run_id: str
    state: str
    seed: int | None
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunRegistry:
    """The runs that existed, in ULID order.

    The packet prints the pinned run and the current head side by side. Whether the
    decision was made on a superseded run is **not** computed here from "pinned !=
    head": a run that was current at 09:14 and superseded at 11:02 produced a
    decision that was not made on a stale model, and stamping it would be a false
    accusation against our own log. The stamp therefore comes from the audit
    payload, which recorded the comparison at write time (``apps/api/decisions.py``
    evaluates ``_run_is_superseded`` inside the decision transaction). The registry
    supplies the context line and refuses a case pinned to a run it has never seen.
    """

    pinned_run_id: str
    head_run_id: str
    runs: tuple[RunRecord, ...]

    def record(self, run_id: str) -> RunRecord | None:
        for item in self.runs:
            if item.run_id == run_id:
                return item
        return None

    def require_known(self) -> None:
        """Refuse to render a cover page about a run the registry has never heard of."""
        if self.record(self.pinned_run_id) is None:
            raise AuditMismatchError(
                f"this case is pinned to run {self.pinned_run_id}, which is not in the "
                "run registry the packet was given. Either the registry is the wrong "
                "one or the case id was invented; neither renders."
            )

    @property
    def is_head(self) -> bool:
        return self.pinned_run_id == self.head_run_id


# --------------------------------------------------------------------------
# scorecard
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScorecardPointRow:
    """One adverse point contribution, as the scorecard stated it.

    ``text`` is the reason code sentence produced by ``scoring/reasons.py`` —
    "Pass-through ratio in top decile: minus 48 points" — carried verbatim rather
    than re-composed here, because two renderers of one reason produce two
    sentences that can disagree.
    """

    feature: str
    bin_display: str
    points: int
    text: str


# --------------------------------------------------------------------------
# the packet case
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PacketCase:
    """Everything the renderer is allowed to print, and the claims it enforces.

    Construction validates rather than defers: an empty decision reason, an
    unrecognised band, an unknown pinned run, a priced recovery rate outside the
    configured band, or a snapshot from a different run all fail *here*, so a
    half-built case never reaches a document that asserts its own provenance on
    page one.
    """

    bundle: CaseBundle
    evidence: EvidenceSnapshot
    explanation: ExplanationSnapshot
    subgraph: SubgraphView
    scorecard_points: tuple[ScorecardPointRow, ...]
    chain: tuple[ChainRow, ...]
    decision_chain_seq: int
    runs: RunRegistry
    economics: Economics
    assumption_block: AssumptionBlock
    deployment_timezone: str
    disclaimer: str = OXBOW_DISCLAIMER
    packet_version: str = PACKET_VERSION

    def __post_init__(self) -> None:
        decision = self.bundle.decision
        if not decision.reason or not decision.reason.strip():
            raise EmptyDecisionReasonError(
                f"case {self.bundle.case_id} decision {decision.decision_seq} has an "
                "empty reason, so there is nothing for a reviewer to have signed. "
                "The server-side refusal is the decision API's (the ``decision`` table "
                "carries ck_decision_reason_not_blank); this is the packet refusing the "
                "same rule defensively, because a packet is the artifact that leaves "
                "the building and an exhibit for an unjustified decision is not an "
                "exhibit."
            )
        band = self.bundle.score.band
        if band not in BAND_LETTERS:
            raise AuditMismatchError(
                f"band {band!r} is not one of {BAND_LETTERS}; the packet renders the "
                "band letter and the meter glyph from this value, so an unrecognised "
                "letter would print a glyph for a band that does not exist."
            )
        if self.disclaimer != OXBOW_DISCLAIMER:
            raise AuditMismatchError(
                "the packet's disclaimer is not the plan §15 text verbatim; the "
                "disclaimer is a constant, not a field a caller may reword."
            )
        for snapshot, what in (
            (self.evidence, "The evidence table"),
            (self.explanation, "The explanation"),
            (self.subgraph, "The subgraph"),
        ):
            snapshot.require_run(self.bundle.run_id, what=what)
        self.runs.require_known()
        self._require_priced_rate_on_band()

    # --- derived views ----------------------------------------------------

    @property
    def case_id(self) -> str:
        return self.bundle.case_id

    @property
    def account_key(self) -> str:
        return self.bundle.account_key

    @property
    def decision(self) -> DecisionRecord:
        return self.bundle.decision

    @property
    def decided_on_superseded_run(self) -> bool:
        """The audit row's own claim, which is the only authoritative one (see RunRegistry)."""
        return self.bundle.decision.decided_on_superseded_run

    @property
    def decision_row(self) -> ChainRow:
        """The audit row whose digest covers this decision.

        Looked up by sequence, then checked to cover the same case, decision,
        reason and superseded-run stamp as the bundle. A packet that printed a
        decision the chain does not carry would be the tamper case, not the tamper
        detector.
        """
        for row in self.chain:
            if row.seq != self.decision_chain_seq:
                continue
            payload: Mapping[str, Any] = row.payload
            if str(payload.get("case_id", "")) != self.case_id:
                raise AuditMismatchError(
                    f"audit row seq={self.decision_chain_seq} covers case "
                    f"{payload.get('case_id')!r}, not {self.case_id!r}."
                )
            if int(str(payload.get("decision_seq", -1))) != self.decision.decision_seq:
                raise AuditMismatchError(
                    f"audit row seq={self.decision_chain_seq} is decision "
                    f"{payload.get('decision_seq')} for {self.case_id}, which is not "
                    f"decision {self.decision.decision_seq} being rendered."
                )
            if str(payload.get("reason", "")) != self.decision.reason:
                raise AuditMismatchError(
                    f"audit row seq={self.decision_chain_seq} covers a different "
                    "decision reason than the bundle being rendered; one of the two "
                    "was rewritten after it was signed."
                )
            if bool(payload.get("decided_on_superseded_run", False)) != (
                self.decided_on_superseded_run
            ):
                raise AuditMismatchError(
                    f"audit row seq={self.decision_chain_seq} disagrees with the "
                    "bundle on decided_on_superseded_run. The cover stamp comes "
                    "from the chain, so a disagreement is not a formatting "
                    "difference — it is the integrity claim failing."
                )
            return row
        raise AuditMismatchError(
            f"no audit row with seq={self.decision_chain_seq} exists in the chain given "
            f"for case {self.case_id}. A decision with no covering digest is not "
            "exportable."
        )

    @property
    def timeline(self) -> tuple[TimelineEntry, ...]:
        return timeline_from_chain(self.chain, self.case_id)

    def _require_priced_rate_on_band(self) -> None:
        """The recovery rate the money was priced at must be one the config declares.

        A bundle priced at r = 0.42 under an assumption block that says
        0.20/0.35/0.50 would print a figure and a footnote describing two
        different economies.
        """
        rate = self.bundle.economics.recovery_rate
        if not any(rate == value for value in self.assumption_block.rates):
            raise MissingAssumptionLineError(
                f"this decision was priced at r={rate}, which is not one of the "
                f"configured sensitivity-band coordinates {self.assumption_block.rates} "
                "(config/economics.yaml :: recovery.sensitivity_band). The assumptions "
                "printed beside the money would not be the assumptions the money was "
                "computed under."
            )

    def money_lines(self) -> tuple[MoneyText, ...]:
        """Every currency figure on the packet, each with its assumption block.

        Built from the decision-time bundle plus ``config/economics.yaml``, in a
        fixed order, and never from a re-scored value. The only figure restated
        over the recovery band is ``E_i x r``, and that is a multiplication of a
        recorded exposure, not a re-run of the model — the document says so on the
        line where a reader would otherwise assume otherwise, because EV_i needs
        ``p_i``, which the decision bundle does not carry.
        """
        block = self.assumption_block
        econ = self.bundle.economics
        economics = self.economics
        rate = econ.recovery_rate
        lines: list[MoneyText] = [
            money_line(
                f"Exposure at risk E_i (observed, {economics.exposure.window_hours} h "
                f"window, {economics.exposure.downstream_hops}-hop downstream, capped at "
                "inflow in the same window)",
                Money(econ.exposure_minor, econ.currency),
                block=block,
                basis=(
                    "value still interceptable at decision time; recorded by the "
                    "pipeline and printed unscaled"
                ),
            ),
            money_line(
                "Interceptable value at the priced recovery rate (E_i x r)",
                Money(econ.exposure_minor, econ.currency).scaled_by_micro(ratio_to_micro(rate)),
                block=block,
                basis=(
                    f"arithmetic restatement of the E_i above at r={rate:.2f}, the rate "
                    f"this decision was priced at (configured default "
                    f"{economics.recovery.rate:.2f}); the model was not re-run"
                ),
            ),
            money_line(
                "Expected value of reviewing this account (EV_i, as recorded)",
                Money(econ.expected_value_minor, econ.currency),
                block=block,
                basis=(
                    f"EV_i = p_i * E_i * r - c_i - (1 - p_i) * f, recorded by the "
                    f"pipeline at r={rate:.2f} and printed as recorded; p_i is not "
                    "carried in the decision bundle, so this figure is deliberately "
                    "not restated at the other band coordinates"
                ),
            ),
            money_line(
                f"Review cost c_i for a band-{self.bundle.score.band} alert",
                Money(econ.analyst_cost_minor, econ.currency),
                block=block,
                basis=(
                    f"{economics.minutes_for(self.bundle.score.band)} analyst-minutes for "
                    f"band {self.bundle.score.band} at "
                    f"{economics.analyst.cost_per_minute_minor} minor units per minute"
                ),
            ),
            money_line(
                "Friction cost f of wrongly touching a legitimate customer",
                economics.friction_cost,
                block=block,
                basis="the (1 - p_i) * f term of EV_i; an assumption, not a measurement",
            ),
        ]
        interval = econ.monte_carlo
        if interval is not None:
            lower, upper = interval.interval[0], interval.interval[1]
            span_pct = round((upper - lower) * 100)
            for name, minor in (
                ("lower", interval.p05_minor),
                ("median", interval.p50_minor),
                ("upper", interval.p95_minor),
            ):
                lines.append(
                    money_line(
                        f"Modelled exposure if unactioned — {name} of the {span_pct} % " "interval",
                        Money(minor, econ.currency),
                        block=block,
                        basis=(
                            f"quantile of {interval.runs:,} seeded propagation draws "
                            f"(seed {interval.seed}, depth "
                            f"{economics.monte_carlo.max_depth}); the propagation model "
                            "is an assumption about onward flow, not an observation of it"
                        ),
                    )
                )
        return tuple(lines)


__all__ = [
    "BAND_LETTERS",
    "BUNDLE_MONEY_FIELDS",
    "EXPLANATION_SOURCES",
    "PACKET_VERSION",
    "ContributionRow",
    "EvidenceRow",
    "EvidenceSnapshot",
    "ExplanationSnapshot",
    "MoneyText",
    "PacketCase",
    "RunRecord",
    "RunRegistry",
    "ScorecardPointRow",
    "SubgraphEdge",
    "SubgraphNode",
    "SubgraphView",
    "TimelineEntry",
    "money_line",
    "timeline_from_chain",
]
