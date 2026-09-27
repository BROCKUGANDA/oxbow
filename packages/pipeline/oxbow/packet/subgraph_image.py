"""The subgraph picture, drawn from the graph artifact — not screenshotted.

Plan §14 forbids screenshots of the explorer in the packet and plan §15 requires
the image to be *generated*: an exhibit's figure has to be reproducible from the
data it claims to depict, and a screenshot is a picture of a browser at one
moment, with no path back to the rows. This module has no browser, no DOM, no
layout engine and no random seed. It takes the
:class:`~oxbow.packet.model.SubgraphView` read from the pinned run's persisted
graph and returns SVG bytes.

**Why the output is byte-identical.** Every coordinate is arithmetic on
``sorted(...)`` of node keys: hop rings place a node by its index within its ring,
ring radius and node size come from fixed tables, stroke width from a
log-bucketed total. There is no force-directed layout — a spring solve would
depend on iteration order and floating-point history, and would make the packet's
determinism gate unsatisfiable by construction. The cost is a less organic
picture; the benefit is that two renders of the same artifact are the same bytes,
which is what §15 asks a packet for.

**Why the encoding survives greyscale and colour blindness** (DESIGN §3 rule 7,
§6): shape carries node type (account circle, rail square, external diamond,
community meta-node rounded square), stroke weight carries value, dash pattern
carries self-transfer, and every node is *labelled with its key*. Colour is
decoration on top of three redundant encodings, so a laser-printed packet loses
nothing.

**What is stated on the image itself**: the hop radius, the truncation verdict and
reason, and the per-currency value basis. A cropped network diagram that does not
say it is cropped is the "density is not evidence" failure in plan §18.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from oxbow.packet.model import SubgraphView
from oxbow.quant.money import to_major_text

#: Fixed canvas. Millimetres are not used because the SVG is also written as a
#: standalone file, and a standalone image has no page box to inherit.
CANVAS_WIDTH: Final = 720.0
CANVAS_HEIGHT: Final = 460.0
CENTRE_X: Final = CANVAS_WIDTH / 2.0
CENTRE_Y: Final = 232.0
RING_RADII: Final = (0.0, 96.0, 168.0, 214.0)

#: Node radius by degree bucket. Buckets, not a continuous scale: a size that encodes
#: degree linearly makes the rail 1000x bigger than everything else and the picture
#: unreadable, which is why the graph layer types rails in the first place.
NODE_RADIUS_BY_DEGREE: Final = ((1, 5.0), (5, 7.0), (25, 9.0), (100, 11.0))
NODE_RADIUS_MAX: Final = 13.0

#: Edge stroke width by log10 of the per-currency value total, in minor units.
EDGE_WIDTH_BY_LOG10: Final = ((0.0, 0.6), (4.0, 1.1), (7.0, 1.9), (10.0, 2.8))
EDGE_WIDTH_MAX: Final = 3.6

SELF_TRANSFER_DASH: Final = "4 3"

NODE_SHAPE_ACCOUNT: Final = "circle"
NODE_SHAPE_RAIL: Final = "square"
NODE_SHAPE_EXTERNAL: Final = "diamond"
NODE_SHAPE_COMMUNITY: Final = "rounded-square"

_LABEL_CHARS: Final = 14


@dataclass(frozen=True, slots=True)
class SubgraphImage:
    """The rendered picture plus the caption the page must print beside it."""

    svg: str
    width: float
    height: float
    alt: str
    caption: str

    def bytes_(self) -> bytes:
        """Standalone file bytes, UTF-8, no XML declaration comment or timestamp."""
        return self.svg.encode("utf-8")


def _escape(text: str) -> str:
    """XML text escaping. Autoescape is off inside this module by construction."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _number(value: float) -> str:
    """Fixed-precision coordinate formatting.

    ``repr`` of a float would print ``96.00000000000001`` on one machine's
    intermediate and ``96.0`` on another's; three decimals is both below the pixel
    and stable.
    """
    return f"{value:.3f}".rstrip("0").rstrip(".") if value else "0"


def _node_radius(degree: int | None) -> float:
    if degree is None:
        return NODE_RADIUS_MAX
    for ceiling, radius in NODE_RADIUS_BY_DEGREE:
        if degree <= ceiling:
            return radius
    return NODE_RADIUS_MAX


def _edge_width(total_minor: int) -> float:
    if total_minor <= 0:
        return EDGE_WIDTH_BY_LOG10[0][1]
    magnitude = math.log10(float(total_minor))
    for ceiling, width in EDGE_WIDTH_BY_LOG10:
        if magnitude <= ceiling:
            return width
    return EDGE_WIDTH_MAX


def _label(node_id: str, member_count: int | None) -> str:
    if member_count is not None:
        return f"{node_id[:_LABEL_CHARS]} ({member_count} accounts)"
    return node_id[:_LABEL_CHARS]


def _positions(view: SubgraphView) -> dict[str, tuple[float, float, int]]:
    """Deterministic concentric-ring placement keyed by account.

    Within a ring, nodes sit at angle ``2*pi*i/n`` starting at the top, ordered by
    account key. Two renders of the same artifact land every node on the same pixel;
    and a reader who has seen one OXBOW packet sees the same layout logic in the
    next, which is the whole point of a document that is meant to be compared.
    """
    by_hop: dict[int, list[str]] = {}
    for node in view.nodes:
        by_hop.setdefault(node.hop, []).append(node.node_id)
    positions: dict[str, tuple[float, float, int]] = {}
    for hop in sorted(by_hop):
        members = sorted(by_hop[hop])
        radius = RING_RADII[min(hop, len(RING_RADII) - 1)]
        if radius == 0.0 or (len(members) == 1 and hop == 0):
            for account in members:
                positions[account] = (CENTRE_X, CENTRE_Y, hop)
            continue
        for index, account in enumerate(members):
            angle = 2.0 * math.pi * index / len(members) - math.pi / 2.0
            positions[account] = (
                CENTRE_X + radius * math.cos(angle),
                CENTRE_Y + radius * math.sin(angle),
                hop,
            )
    return positions


def _shape(node_type: str, member_count: int | None) -> str:
    if member_count is not None:
        return NODE_SHAPE_COMMUNITY
    if node_type == "rail":
        return NODE_SHAPE_RAIL
    if node_type == "external":
        return NODE_SHAPE_EXTERNAL
    return NODE_SHAPE_ACCOUNT


def _draw_shape(shape: str, x: float, y: float, radius: float, *, seed: bool) -> str:
    fill = "#ffffff" if not seed else "#1f2933"
    stroke = "#111820"
    width = 2.6 if seed else 1.4
    if shape == NODE_SHAPE_RAIL:
        return (
            f'<rect x="{_number(x - radius)}" y="{_number(y - radius)}" '
            f'width="{_number(2 * radius)}" height="{_number(2 * radius)}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{width}"/>'
        )
    if shape == NODE_SHAPE_COMMUNITY:
        return (
            f'<rect x="{_number(x - radius)}" y="{_number(y - radius)}" '
            f'width="{_number(2 * radius)}" height="{_number(2 * radius)}" rx="3" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>'
        )
    if shape == NODE_SHAPE_EXTERNAL:
        points = " ".join(
            f"{_number(x + dx)},{_number(y + dy)}"
            for dx, dy in ((0.0, -radius), (radius, 0.0), (0.0, radius), (-radius, 0.0))
        )
        return (
            f'<polygon points="{points}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{width}"/>'
        )
    return (
        f'<circle cx="{_number(x)}" cy="{_number(y)}" r="{_number(radius)}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>'
    )


def value_basis_lines(view: SubgraphView, *, per_major: int) -> tuple[str, ...]:
    """The per-currency value lines printed with the picture.

    Observed totals, stated as observed: no recovery rate, no cost assumption, and
    never one figure across two currencies (02 money rules). This is the assumption
    line for the amounts drawn on the image, and it is rendered beside the image so
    a reader who zooms into the diagram's edge labels still finds the basis.
    """
    totals: dict[str, list[int]] = {}
    for edge in view.edges:
        bucket = totals.setdefault(edge.currency, [0, 0])
        bucket[0] += edge.total_value_minor
        bucket[1] += edge.edge_count
    if not totals:
        return ("no edges in the drawn neighbourhood; nothing to sum",)
    return tuple(
        f"{currency}: {_sum_text(totals[currency][0], currency, per_major)} across "
        f"{totals[currency][1]:,} event(s) — observed values, unpriced, not converted"
        for currency in sorted(totals)
    )


def _sum_text(minor: int, currency: str, per_major: int) -> str:
    return to_major_text(minor, currency, per_major)


def render_subgraph_svg(
    view: SubgraphView,
    *,
    band_letter: str,
    title: str,
) -> SubgraphImage:
    """Draw ``view`` and return the SVG plus the caption the page must carry.

    No money figure is drawn: edge widths are a relative weight over ``total_value_minor``,
    and the amounts themselves belong to :func:`value_basis_lines`, which prints them with
    the currency they were recorded in.
    """
    positions = _positions(view)
    node_by_id = {node.node_id: node for node in view.nodes}
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_number(CANVAS_WIDTH)}" '
        f'height="{_number(CANVAS_HEIGHT)}" '
        f'viewBox="0 0 {_number(CANVAS_WIDTH)} {_number(CANVAS_HEIGHT)}" role="img" '
        f'aria-label="{_escape(title)}">',
        f"<title>{_escape(title)}</title>",
        "<desc>Generated from the pinned run's persisted graph artifact by "
        "oxbow.packet.subgraph_image; not a screenshot of the explorer.</desc>",
        "<style>text{font-family:'IBM Plex Sans',ui-sans-serif,system-ui,sans-serif;"
        "fill:#111820}.mono{font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace}</style>",
        f'<rect x="0" y="0" width="{_number(CANVAS_WIDTH)}" height="{_number(CANVAS_HEIGHT)}" '
        'fill="#ffffff"/>',
    ]

    # Hop rings, labelled at the top of each ring.
    for hop in (1, 2, 3):
        radius = RING_RADII[min(hop, len(RING_RADII) - 1)]
        parts.append(
            f'<circle cx="{_number(CENTRE_X)}" cy="{_number(CENTRE_Y)}" '
            f'r="{_number(radius)}" fill="none" stroke="#c9ced6" '
            f'stroke-dasharray="2 4" stroke-width="0.8"/>'
        )
        parts.append(
            f'<text x="{_number(CENTRE_X + 4)}" y="{_number(CENTRE_Y - radius - 3)}" '
            f'font-size="8" class="mono">hop {hop}</text>'
        )

    # Edges first, so nodes sit on top of their endpoints.
    for edge in view.edges:
        start = positions.get(edge.account_from)
        end = positions.get(edge.account_to)
        if start is None or end is None:
            continue
        if edge.account_from == edge.account_to:
            parts.append(
                f'<circle cx="{_number(start[0] + 11)}" cy="{_number(start[1] - 11)}" r="7" '
                f'fill="none" stroke="#111820" stroke-width="{_number(_edge_width(edge.total_value_minor))}" '
                f'stroke-dasharray="{SELF_TRANSFER_DASH}"/>'
            )
            continue
        width = _number(_edge_width(edge.total_value_minor))
        parts.append(
            f'<line x1="{_number(start[0])}" y1="{_number(start[1])}" '
            f'x2="{_number(end[0])}" y2="{_number(end[1])}" stroke="#111820" '
            f'stroke-width="{width}"/>'
        )
        midpoint_x = (start[0] + end[0]) / 2.0
        midpoint_y = (start[1] + end[1]) / 2.0
        parts.append(
            f'<text x="{_number(midpoint_x)}" y="{_number(midpoint_y - 2)}" font-size="7" '
            f'text-anchor="middle">{_number(edge.edge_count)}x '
            f"{_escape(edge.currency)}</text>"
        )

    # Nodes and their labels.
    for account in sorted(positions):
        x, y, _ = positions[account]
        node = node_by_id[account]
        radius = _node_radius(node.degree)
        parts.append(
            _draw_shape(
                _shape(node.node_type, node.member_count),
                x,
                y,
                radius,
                seed=account == view.seed,
            )
        )
        parts.append(
            f'<text x="{_number(x)}" y="{_number(y + radius + 9)}" font-size="7.5" '
            f'text-anchor="middle" class="mono">{_escape(_label(account, node.member_count))}</text>'
        )
        parts.append(
            f'<text x="{_number(x + radius + 2)}" y="{_number(y - radius - 1)}" '
            f'font-size="6.5" class="mono">{_escape(node.node_type[:4])}</text>'
        )

    # Legend: shapes, weights and the disclosures. A reader who cannot see the
    # colours must still be able to read the diagram, so the key is text.
    legend_y = CANVAS_HEIGHT - 66
    parts.append(
        f'<text x="12" y="{_number(legend_y)}" font-size="8.5" '
        f'font-weight="600">Reading this diagram (band {band_letter})</text>'
    )
    legend_lines = [
        "circle = account   square = rail (merchant/agent: endpoint, never a bridge)   "
        "diamond = external counterparty   rounded square = collapsed community",
        "line weight = log-bucketed total value per currency; 'Nx CUR' = N events in "
        "that currency; dashed = self-transfer (excluded from cycle and fan evidence)",
    ]
    for offset, line in enumerate(legend_lines):
        parts.append(
            f'<text x="12" y="{_number(legend_y + 12 + 10 * offset)}" font-size="7.5">'
            f"{_escape(line)}</text>"
        )
    disclosures = [
        f"{view.reachable_before_cap} accounts reachable in {view.hops} hops; "
        f"{len(view.nodes)} drawn",
        (
            f"TRUNCATED: {view.truncation_reason}"
            if view.truncated
            else "not truncated: every reachable account is drawn"
        ),
    ]
    for offset, line in enumerate(disclosures):
        parts.append(
            f'<text x="12" y="{_number(legend_y + 38 + 10 * offset)}" font-size="7.5" '
            f'font-weight="600">{_escape(line)}</text>'
        )
    parts.append("</svg>")

    caption = (
        f"Two-hop network around {_label(view.seed, None)} as recorded in run "
        f"{view.run_id}, drawn from {view.source}. " + " ".join(disclosures)
    )
    alt = (
        f"Subgraph of account {view.seed}: {len(view.nodes)} nodes and "
        f"{len(view.edges)} currency-aggregated edges within {view.hops} hops"
    )
    return SubgraphImage(
        svg="".join(parts),
        width=CANVAS_WIDTH,
        height=CANVAS_HEIGHT,
        alt=alt,
        caption=caption,
    )


__all__ = [
    "CANVAS_HEIGHT",
    "CANVAS_WIDTH",
    "NODE_SHAPE_ACCOUNT",
    "NODE_SHAPE_COMMUNITY",
    "NODE_SHAPE_EXTERNAL",
    "NODE_SHAPE_RAIL",
    "SubgraphImage",
    "render_subgraph_svg",
    "value_basis_lines",
]
