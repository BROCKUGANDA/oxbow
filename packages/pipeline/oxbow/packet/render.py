"""The renderer: verify, compose, typeset.

``render_case_packet(case, out_dir=...)`` is the whole P9 packet surface. Three
properties decide its shape, and each is enforced in code rather than in review:

**Verify on export.** :func:`verify_chain_for_export` walks the *entire* audit chain
the case came with before a byte of the document is written, using the same pure
arithmetic as ``make verify-audit`` (:func:`oxbow.audit.chain.verify_chain`). A
broken link raises :class:`~oxbow.packet.errors.ChainIntegrityError` **naming the
sequence number**, and no packet exists for that case. A document that printed a
tampered chain and looked fine would be the failure mode the chain exists to
prevent, and the packet is the artifact that leaves the building.

**Byte-identical.** Two renders of the same case produce the same bytes. That is why
this module reads no clock: the PDF's ``/CreationDate`` is the **recorded decision
instant**, injected as a ``dcterms.created`` meta tag so WeasyPrint writes the
document's date from evidence rather than from the wall. Fonts are fixed by name
(the IBM Plex stack from ``apps/web/src/design/tokens.css``), the CSS is inlined so
it is part of the hashed artifact, the subgraph is generated rather than fetched,
and every table arrives pre-sorted. Everything below is a pure function of the
:class:`~oxbow.packet.model.PacketCase`, which is itself a pure function of files
on disk.

**Refuse, never substitute.** An empty decision reason, a snapshot from a
non-pinned run, a missing graph artifact or a chain that does not verify all raise
before writing. A packet is signed evidence; a packet with a hole in it that reads
like a hole-free packet is the artifact a reviewer cannot rely on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import jinja2
from markupsafe import Markup

from oxbow.audit.chain import ChainVerification, verify_chain
from oxbow.packet.errors import (
    ChainIntegrityError,
    PacketError,
    TemplateRenderError,
)
from oxbow.packet.model import PACKET_VERSION, MoneyText, PacketCase
from oxbow.packet.subgraph_image import (
    SubgraphImage,
    render_subgraph_svg,
    value_basis_lines,
)
from oxbow.ports.case_sink import OXBOW_DISCLAIMER
from oxbow.quant.money import to_major_text

TEMPLATE_DIR: Final = Path(__file__).parent / "templates"
TEMPLATE_NAME: Final = "case_packet.html.j2"
STYLESHEET_NAME: Final = "packet.css"

#: Fixed font stack, mirroring ``apps/web/src/design/tokens.css``'s ``--font-sans``
#: and ``--font-mono``. Declared here as the packet's own constant because a print
#: document that inherited the host's default face would look different on the demo
#: laptop and the CI box, which is exactly what §15's byte gate forbids.
PACKET_FONTS: Final = {
    "sans": '"IBM Plex Sans", ui-sans-serif, system-ui, "Segoe UI", sans-serif',
    "mono": '"IBM Plex Mono", ui-monospace, "Cascadia Mono", Menlo, monospace',
}

#: The band ramp, verbatim from ``tokens.css`` (``--color-band-a`` … ``-e``). Printed
#: values only: DESIGN §6 requires the letter and the meter glyph to carry the band,
#: so if a renderer cannot resolve ``oklch()`` the document still reads.
BAND_OKLCH: Final = {
    "A": "oklch(0.78 0.075 235)",
    "B": "oklch(0.78 0.095 195)",
    "C": "oklch(0.78 0.115 155)",
    "D": "oklch(0.78 0.135 95)",
    "E": "oklch(0.78 0.155 40)",
}

SEGMENTS_IN_METER: Final = 5


@dataclass(frozen=True, slots=True)
class ComposedPacket:
    """The deterministic artifact pair: HTML and SVG.

    The PDF is these bytes typeset. Exposing the composition separately is what lets
    the determinism gate be asserted even on a host where WeasyPrint's native text
    shaper (Pango) is not installed — a claim about byte-identity should never have
    to depend on a shared library being present to be testable.
    """

    stem: str
    html: str
    svg: SubgraphImage

    def html_bytes(self) -> bytes:
        return self.html.encode("utf-8")

    def svg_bytes(self) -> bytes:
        return self.svg.bytes_()


# --------------------------------------------------------------------------
# chain verification
# --------------------------------------------------------------------------


def verify_chain_for_export(case: PacketCase) -> ChainVerification:
    """Walk the case's chain and refuse the export if any link is broken.

    The sequence number is in the message because the whole value of a hash chain is
    localising the edit: "the chain is broken" sends an investigator to read 5,000
    rows, and "seq=41, digest mismatch" sends them to one.
    """
    verification = verify_chain(list(case.chain))
    if verification.ok:
        return verification
    broken = verification.first_broken
    if broken is None:  # verify_chain never returns a failure without one
        raise PacketError(
            "verify_chain reported a failure with no broken link attached; that is a "
            "bug in the chain module, not a packet decision."
        )
    raise ChainIntegrityError(
        broken.seq,
        broken.reason,
        expected=broken.expected,
        actual=broken.actual,
    )


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Zone:
    """The deployment timezone and its abbreviation, resolved once per render.

    DESIGN §5: "a UTC timestamp shown to a UTC+3 analyst is an hour-hunting bug".
    The zone comes from ``config/pipeline.yaml``, never from the host, so two
    machines print the same wall text; the abbreviation comes from the tz database,
    so it is the real one (EAT, not a hand-written guess).
    """

    name: str
    abbrev: str
    offset_hours: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.abbrev}, UTC{self.offset_hours})"


def resolve_zone(name: str, *, at: datetime) -> Zone:
    """Resolve ``name`` against the tz database, stamped at a recorded instant."""
    tzinfo_: tzinfo = ZoneInfo(name)
    local = at.astimezone(tzinfo_)
    offset = local.utcoffset()
    if offset is None:
        raise PacketError(f"timezone {name!r} reports no UTC offset at {at}")
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return Zone(
        name=name,
        abbrev=local.tzname(),
        offset_hours=f"{sign}{hours:02d}:{minutes:02d}",
    )


def format_instant(value: datetime, zone: Zone) -> str:
    """A recorded instant in the deployment zone, with the zone abbreviation.

    The seconds are printed because an audit timeline ordered to the second must be
    readable to the second; ``T`` separates date from time so the string is
    unambiguous at a glance and identical wherever it is copied.
    """
    local = value.astimezone(ZoneInfo(zone.name))
    return f"{local.strftime('%Y-%m-%d')}T{local.strftime('%H:%M:%S')} {zone.abbrev}"


def money_lines_text(lines: Sequence[MoneyText]) -> tuple[str, ...]:
    """The flat ``label: figure`` rendering used in the appendix."""
    return tuple(f"{line.label}: {line.render()}" for line in lines)


def assumption_keys(case: PacketCase) -> str:
    """The ``config/economics.yaml`` keys behind every money figure, named.

    Plan §11 and DESIGN §7 ask for the *keys*, not a gesture at a file: a reader who
    is told "recovery.sensitivity_band" can go and change it, and a reader told
    "depends on assumptions" cannot. This is the line printed beside each figure;
    section 8 prints the whole block verbatim.
    """
    economics = case.economics
    band = case.bundle.score.band
    keys: list[str] = [
        "currency",
        "minor_units_per_major",
        "recovery.rate",
        "recovery.sensitivity_band",
        "exposure.window_hours",
        "exposure.downstream_hops",
        "analyst.cost_per_minute_minor",
        "analyst.min_review_minutes",
        f"review_minutes_by_alert_class.{band}",
        "friction_cost_minor",
        "capacity.review_minutes_per_period",
    ]
    if case.bundle.economics.monte_carlo is not None:
        keys += [
            "monte_carlo.runs",
            "monte_carlo.max_depth",
            "monte_carlo.seed",
            "monte_carlo.interval",
        ]
    return f"{case.assumption_block.source_path} :: " + ", ".join(keys)


def _major_text(amount: Any, per_major: int) -> str:
    """Filter: a ``Money`` as major units with its ISO code. Render time only."""
    return to_major_text(amount.minor, amount.currency, per_major)


def _minor_to_major(minor: int, currency: str, per_major: int) -> str:
    """Filter: a summed minor-unit integer plus the currency it was summed in."""
    return to_major_text(int(minor), currency, per_major)


def band_meter_glyph(band: str) -> dict[str, Any]:
    """The five-segment meter as inline SVG, lit to the band's rank.

    DESIGN §6: the meter glyph *and* the letter always render next to any band
    colour, so the encoding survives greyscale, colour blindness and a laser
    printer. Filled segments carry the rank; the letters beside the glyph are the
    non-redundant channel and are printed in the template, not drawn here.
    """
    lit = list("ABCDE").index(band) + 1
    segments: list[dict[str, object]] = []
    for index in range(SEGMENTS_IN_METER):
        segments.append(
            {
                "x": 2.0 + index * 10.0,
                "y": 2.0,
                "width": 8.0,
                "height": 12.0,
                "lit": index < lit,
            }
        )
    return {"segments": segments, "lit": lit, "segments_total": SEGMENTS_IN_METER}


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------


def packet_stem(case: PacketCase) -> str:
    """The filename stem: case, decision sequence and pinned run.

    Deterministic by construction and collision-free for the case it names — a
    reversal is a different ``decision_seq``, so both packets exist and the timeline
    in each points at the other (plan §15's append-only rule, observable from a
    directory listing).
    """
    return f"{case.case_id}-d{case.decision.decision_seq:03d}-{case.bundle.run_id}"


def compose_packet(case: PacketCase) -> ComposedPacket:
    """Render the document body to HTML. No clock, no network, no filesystem reads."""
    zone = resolve_zone(case.deployment_timezone, at=case.decision.decided_at)
    image = render_subgraph_svg(
        case.subgraph,
        per_major=case.economics.minor_units_per_major,
        band_letter=case.bundle.score.band,
        title=(
            f"Subgraph for {case.account_key} in case {case.case_id}, " f"run {case.bundle.run_id}"
        ),
    )
    context: dict[str, object] = {
        "case": case,
        "bundle": case.bundle,
        "decision": case.decision,
        "score": case.bundle.score,
        "economics_block": case.bundle.economics,
        "evidence": case.evidence,
        "explanation": case.explanation,
        "subgraph": case.subgraph,
        "image": image,
        "value_basis": value_basis_lines(
            case.subgraph, per_major=case.economics.minor_units_per_major
        ),
        "money_lines": case.money_lines(),
        "timeline": case.timeline,
        "scorecard_points": case.scorecard_points,
        "runs": case.runs,
        "economics": case.economics,
        "assumptions": case.assumption_block,
        "assumption_keys": assumption_keys(case),
        "zone": zone,
        "band_meter": band_meter_glyph(case.bundle.score.band),
        "band_oklch": BAND_OKLCH[case.bundle.score.band],
        "fonts": PACKET_FONTS,
        "disclaimer": OXBOW_DISCLAIMER,
        "packet_version": PACKET_VERSION,
        "decision_row_hash": case.decision_row.row_hash,
        "decision_row_seq": case.decision_row.seq,
        "decision_row_prev_hash": case.decision_row.prev_hash,
        "chain_rows_checked": len(case.chain),
        "chain_head_hash": case.chain[-1].row_hash if case.chain else "",
        "evidence_totals": case.evidence.totals_by_currency(),
        "format_instant": lambda value: format_instant(value, zone),
        "iso_instant": lambda value: value.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "per_major": case.economics.minor_units_per_major,
    }
    try:
        template = _environment().get_template(TEMPLATE_NAME)
        html = template.render(**context)
    except jinja2.UndefinedError as exc:  # StrictUndefined makes this the only failure mode
        raise TemplateRenderError(
            f"the packet template asked for a value that is not there: {exc}"
        ) from exc
    return ComposedPacket(stem=packet_stem(case), html=html, svg=image)


def _stylesheet_text() -> str:
    return (TEMPLATE_DIR / STYLESHEET_NAME).read_text(encoding="utf-8")


@jinja2.pass_context
def _inline_stylesheet(ctx: jinja2.runtime.Context) -> Markup:
    """The print stylesheet, inlined.

    Inlined rather than linked so the HTML *is* the artifact: its bytes cover the
    layout as well as the content, and the document renders identically when moved
    without its directory.
    """
    del ctx
    return Markup(f"<style>{_stylesheet_text()}</style>")


def _environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=jinja2.select_autoescape(["j2", "html"]),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.globals["stylesheet"] = _inline_stylesheet
    env.filters["major_text"] = _major_text
    env.filters["minor_to_major"] = _minor_to_major
    return env


# --------------------------------------------------------------------------
# typesetting
# --------------------------------------------------------------------------


def _pdf_dates(case: PacketCase) -> tuple[str, str]:
    """The PDF's ``/CreationDate`` and ``/ModDate``, taken from evidence.

    WeasyPrint writes those two fields from ``dcterms.created`` / ``dcterms.modified``
    meta tags and omits them otherwise. They are set here to the *recorded decision
    instant* so the document is dated by the event it describes — the only dates in a
    packet are recorded ones (plan §15), and the byte-identity gate can therefore pass
    with a real creation date in the file rather than with the fields stripped.
    """
    created = case.decision.decided_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    latest = max(row.occurred_at for row in case.chain) if case.chain else case.decision.decided_at
    modified = latest.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return created, modified


def render_case_packet(case: PacketCase, *, out_dir: Path) -> Path:
    """Write the packet and return the PDF path.

    Three files land together, and all three are part of the exhibit:

    * ``<stem>.pdf`` — the document a human signs and a court reads.
    * ``<stem>.html`` — the composed body, the deterministic artifact whose digest two
      renders are compared on.
    * ``<stem>.svg`` — the subgraph figure, standalone, so the image can be verified
      against the graph artifact without opening the PDF.

    ``out_dir`` is keyword-only per the phase contract. It is created if absent and
    overwritten if present: a packet is a function of its artifacts, so a second
    render of the same case is the same document, not a ``(1)`` copy.
    """
    directory = Path(out_dir)
    verify_chain_for_export(case)
    composed = compose_packet(case)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{composed.stem}.html").write_text(composed.html, encoding="utf-8", newline="\n")
    (directory / f"{composed.stem}.svg").write_bytes(composed.svg_bytes())
    target = directory / f"{composed.stem}.pdf"
    _write_pdf(composed.html, target, case=case)
    return target


def _write_pdf(html: str, target: Path, *, case: PacketCase) -> None:
    """Typeset with WeasyPrint.

    Imported inside the function so the composition layer (HTML, SVG, and therefore
    the byte-identity gate) stays usable on a host where the native text shaper is
    not installed — and so an absent library fails with its *name and the missing
    shared object*, not with an ImportError at module load that hides which of the
    two packet halves is broken.
    """
    created, modified = _pdf_dates(case)
    try:
        from weasyprint import HTML
    except OSError as exc:  # pragma: no cover - environment-dependent
        raise PacketError(
            f"WeasyPrint is installed but its native libraries are not available on this "
            f"host ({exc}). The PDF step of the packet needs Pango and GObject: install "
            "them (or render in the Linux image the deploy uses) and re-run "
            "`oxbow packet`. The composed HTML and SVG were written; the packet itself "
            "was not, because a half-rendered exhibit is not an exhibit."
        ) from exc
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise PacketError(
            "WeasyPrint is not installed, so the packet cannot be typeset. "
            "`uv sync --all-extras` installs weasyprint==63.0."
        ) from exc
    document = HTML(
        string=_with_pdf_dates(html, created=created, modified=modified),
        base_url=str(target.parent),
    )
    document.write_pdf(target)


def _with_pdf_dates(html: str, *, created: str, modified: str) -> str:
    """Inject the ``dcterms`` meta tags WeasyPrint reads for ``/CreationDate``.

    A marker comment in the template's ``<head>`` is the insertion point, so the
    composition layer and the typeset document differ by exactly these two lines and
    nothing else.
    """
    marker = "<!-- oxbow:pdf-dates -->"
    if marker not in html:
        raise TemplateRenderError(
            f"the packet template no longer carries {marker!r} in its <head>; the PDF "
            "would be dated by the wall clock and the byte-identity gate would fail."
        )
    injection = (
        f'<meta name="dcterms.created" content="{created}">'
        f'<meta name="dcterms.modified" content="{modified}">'
    )
    return html.replace(marker, injection, 1)


__all__ = [
    "BAND_OKLCH",
    "PACKET_FONTS",
    "SEGMENTS_IN_METER",
    "STYLESHEET_NAME",
    "TEMPLATE_DIR",
    "TEMPLATE_NAME",
    "ComposedPacket",
    "Zone",
    "band_meter_glyph",
    "compose_packet",
    "format_instant",
    "money_lines_text",
    "packet_stem",
    "render_case_packet",
    "resolve_zone",
    "verify_chain_for_export",
]
