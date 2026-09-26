"""OXBOW packet layer (P9): the case exhibit.

Plan §15's deliverable is a document a reviewer can put in front of a compliance
officer that never contains a number the system did not produce. The layer is split
along the line that determines whether that claim is checkable:

* :mod:`oxbow.packet.model` — the types, and the rules that make an inconsistent case
  unconstructable (wrong band, empty reason, evidence from a run other than the pinned
  one, a recovery rate outside the configured band);
* :mod:`oxbow.packet.loaders` — reads the landed artifacts (decision bundle, audit
  chain, graph artifact, scored rows, run ledger) and fails with a path when one is
  missing;
* :mod:`oxbow.packet.subgraph_image` — draws the network *from the graph data*, which
  is what makes it reproducible rather than a screenshot;
* :mod:`oxbow.packet.render` — verifies the hash chain, composes the document, and
  typesets it with WeasyPrint;
* :mod:`oxbow.packet.errors` — the refusals, each naming what is missing.

Config remains the only source of tunables (00 §G): economic assumptions come from
``config/economics.yaml`` through :func:`oxbow.quant.economics.load_economics`, the
deployment timezone from ``config/pipeline.yaml``, and the visual values from
``apps/web/src/design/tokens.css``, mirrored in ``templates/packet.css``.
"""

from __future__ import annotations

from oxbow.packet.errors import (
    AuditMismatchError,
    ChainIntegrityError,
    EmptyDecisionReasonError,
    MissingArtifactError,
    MissingAssumptionLineError,
    PacketError,
    PinnedRunMismatchError,
    SubgraphArtifactError,
    TemplateRenderError,
)
from oxbow.packet.loaders import (
    AUDIT_CHAIN_FILENAME,
    CASE_SINK_DIRNAME,
    RUNS_FILENAME,
    build_packet_case,
    case_bundle_from_payload,
    decision_chain_seq,
    evidence_from_graph_artifact,
    explanation_for_bundle,
    landed_bundles,
    load_case_bundle,
    load_chain,
    load_run_registry,
    scorecard_points_for_bundle,
    subgraph_from_graph_artifact,
)
from oxbow.packet.model import (
    BAND_LETTERS,
    PACKET_VERSION,
    EvidenceRow,
    EvidenceSnapshot,
    ExplanationSnapshot,
    MoneyText,
    PacketCase,
    RunRecord,
    RunRegistry,
    ScorecardPointRow,
    SubgraphEdge,
    SubgraphNode,
    SubgraphView,
    TimelineEntry,
    money_line,
)
from oxbow.packet.render import (
    ComposedPacket,
    Zone,
    assumption_keys,
    band_meter_glyph,
    compose_packet,
    format_instant,
    packet_stem,
    render_case_packet,
    resolve_zone,
    verify_chain_for_export,
)
from oxbow.packet.subgraph_image import SubgraphImage, render_subgraph_svg

__all__ = [
    "AUDIT_CHAIN_FILENAME",
    "BAND_LETTERS",
    "CASE_SINK_DIRNAME",
    "PACKET_VERSION",
    "RUNS_FILENAME",
    "AuditMismatchError",
    "ChainIntegrityError",
    "ComposedPacket",
    "EmptyDecisionReasonError",
    "EvidenceRow",
    "EvidenceSnapshot",
    "ExplanationSnapshot",
    "MissingArtifactError",
    "MissingAssumptionLineError",
    "MoneyText",
    "PacketCase",
    "PacketError",
    "PinnedRunMismatchError",
    "RunRecord",
    "RunRegistry",
    "ScorecardPointRow",
    "SubgraphArtifactError",
    "SubgraphEdge",
    "SubgraphImage",
    "SubgraphNode",
    "SubgraphView",
    "TemplateRenderError",
    "TimelineEntry",
    "Zone",
    "assumption_keys",
    "band_meter_glyph",
    "build_packet_case",
    "case_bundle_from_payload",
    "decision_chain_seq",
    "evidence_from_graph_artifact",
    "explanation_for_bundle",
    "landed_bundles",
    "load_case_bundle",
    "load_chain",
    "load_run_registry",
    "money_line",
    "packet_stem",
    "render_case_packet",
    "render_subgraph_svg",
    "resolve_zone",
    "scorecard_points_for_bundle",
    "subgraph_from_graph_artifact",
    "verify_chain_for_export",
]
