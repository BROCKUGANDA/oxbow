"""P9 packet gate tests (plan §15).

The phase gate is four claims, and each is a test that fails if the code regresses:

* the packet is **byte-identical across two renders** of a pinned run;
* the hash chain **verifies on export**, and a tampered row **fails naming its
  sequence number**;
* the packet renders the evidence **as it was at decision time**, and a decision made
  on a superseded run carries ``decided_on_superseded_run`` on the cover because the
  audit row says so;
* an **empty decision reason cannot be rendered**, and **no currency figure appears
  without its assumption line**.

Everything else here is the 03 §12 edge-case walk for this surface: band legibility
without colour, timestamps with the zone abbreviation, the figure generated rather than
screenshotted, refusal naming the missing artifact, and reversals appearing as new rows.

Expected values are hand-computed from ``config/economics.yaml`` and
``tests/unit/p9_fixtures.py``'s arithmetic — not taken from the renderer's own output,
which would make the test a screenshot of the code.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oxbow.audit.chain import ChainRow
from oxbow.packet import (
    AuditMismatchError,
    ChainIntegrityError,
    EmptyDecisionReasonError,
    MissingArtifactError,
    MissingAssumptionLineError,
    PacketCase,
    PinnedRunMismatchError,
    build_packet_case,
    compose_packet,
    decision_chain_seq,
    load_chain,
    money_line,
    render_case_packet,
    resolve_zone,
    subgraph_from_graph_artifact,
    verify_chain_for_export,
)
from oxbow.ports.case_sink import OXBOW_DISCLAIMER
from oxbow.quant.economics import AssumptionBlock, Economics
from tests.unit.p9_fixtures import (
    BASE,
    CASE_ID,
    DELTA,
    REASON,
    RUN_A,
    RUN_B,
    SUBJECT,
    assumption_fixture,
    make_bundle,
    packet_fixture,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _weasyprint_available() -> str | None:
    """``None`` when WeasyPrint can typeset, else the exact import failure."""
    try:
        import weasyprint  # noqa: F401  (probe only)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


@pytest.fixture(scope="module")
def economics() -> tuple[Economics, AssumptionBlock]:
    return assumption_fixture(REPO_ROOT)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory, economics: tuple[Economics, AssumptionBlock]) -> dict[str, Any]:
    """A landed packet workspace: graph artifact, chain, bundles, run ledger, scored rows."""
    econ, block = economics
    root = tmp_path_factory.mktemp("p9-workspace")
    return packet_fixture(root, economics=econ, block=block)


def _case(workspace: dict[str, Any], index: int = 0) -> PacketCase:
    econ, block = assumption_fixture(REPO_ROOT)
    bundles: list[Any] = workspace["bundles"]
    bundle = bundles[index]
    path = workspace["case_sink"] / f"{bundle.idempotency_key}.json"
    return build_packet_case(
        path,
        graph_artifact_dir=workspace["graph_dir"],
        audit_chain_path=workspace["audit_path"],
        warehouse_dir=workspace["warehouse"],
        economics=econ,
        assumption_block=block,
        deployment_timezone="Africa/Kampala",
        scored_rows=workspace["scored_rows"],
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------- determinism


def test_packet_is_byte_identical_across_two_renders(workspace: dict[str, Any]) -> None:
    """Plan §15's first clause, on the composition layer.

    HTML and SVG cover every byte the packet body is made of — content, order, the
    inlined print stylesheet, and the generated figure. The PDF is the same bytes
    typeset, and its determinism is asserted separately below.
    """
    case = _case(workspace)
    first = compose_packet(case)
    second = compose_packet(case)
    assert _sha256(first.html_bytes()) == _sha256(second.html_bytes())
    assert _sha256(first.svg_bytes()) == _sha256(second.svg_bytes())
    # Re-reading the artifacts from disk must not change the answer either: the two
    # renders below rebuild the case object rather than reusing it.
    third = compose_packet(_case(workspace))
    assert _sha256(third.html_bytes()) == _sha256(first.html_bytes())
    assert _sha256(third.svg_bytes()) == _sha256(first.svg_bytes())


def test_render_case_packet_is_byte_identical_including_the_pdf(
    workspace: dict[str, Any], tmp_path: Path
) -> None:
    """The gate as §15 words it: two renders, same bytes, PDF included."""
    blocker = _weasyprint_available()
    if blocker is not None:
        pytest.skip(
            f"WeasyPrint cannot typeset on this host: {blocker}. The composed HTML and "
            "SVG byte-identity is asserted by the test above; the PDF step needs Pango "
            "and GObject installed natively. Re-run `make packet` on a host that has "
            "them (the deploy image does)."
        )
    case = _case(workspace)
    one = render_case_packet(case, out_dir=tmp_path / "one")
    two = render_case_packet(case, out_dir=tmp_path / "two")
    assert one.name == two.name == f"{CASE_ID}-d001-{RUN_A}.pdf"
    first_bytes = one.read_bytes()
    second_bytes = two.read_bytes()
    assert _sha256(first_bytes) == _sha256(second_bytes), (
        "two renders of the same pinned run produced different PDFs: something in the "
        "renderer read a clock, a locale or a host default"
    )
    # The sidecar artifacts travel with it and are identical too.
    assert _sha256(one.with_suffix(".html").read_bytes()) == _sha256(
        two.with_suffix(".html").read_bytes()
    )
    assert _sha256(one.with_suffix(".svg").read_bytes()) == _sha256(
        two.with_suffix(".svg").read_bytes()
    )


def test_render_writes_pdf_html_and_svg(workspace: dict[str, Any], tmp_path: Path) -> None:
    """Three files per case: the document, its body, and the figure it was drawn from."""
    case = _case(workspace)
    blocker = _weasyprint_available()
    target = tmp_path / "packets"
    if blocker is None:
        path = render_case_packet(case, out_dir=target)
        assert path.is_file()
        assert (target / f"{case.case_id}-d001-{RUN_A}.html").is_file()
        assert (target / f"{case.case_id}-d001-{RUN_A}.svg").is_file()
        return
    composed = compose_packet(case)
    target.mkdir(parents=True)
    (target / f"{composed.stem}.html").write_text(composed.html, encoding="utf-8")
    (target / f"{composed.stem}.svg").write_bytes(composed.svg_bytes())
    assert (target / f"{composed.stem}.html").is_file()
    assert (target / f"{composed.stem}.svg").is_file()


def test_no_wall_clock_appears_in_the_body(workspace: dict[str, Any]) -> None:
    """Every date in a packet is a recorded one (plan §15).

    The fixture's instants are all in 2024; if the renderer stamped today anywhere,
    this fails on the year, and it would fail on the day-of-year too.
    """
    html = compose_packet(_case(workspace)).html
    assert str(datetime.now(UTC).year) not in html
    assert "2026" not in html
    assert BASE.strftime("%Y-%m-%d") in html


# ------------------------------------------------------------------- integrity


def test_chain_verifies_on_export_and_tampering_fails_naming_the_sequence(
    tmp_path: Path, economics: tuple[Economics, AssumptionBlock]
) -> None:
    """§15's second clause, both halves."""
    econ, block = economics
    clean = packet_fixture(tmp_path / "clean", economics=econ, block=block)
    case = _case(clean)
    verification = verify_chain_for_export(case)
    assert verification.ok
    assert verification.rows_checked == 2

    broken = packet_fixture(
        tmp_path / "broken",
        economics=econ,
        block=block,
        tamper=(2, "reason", "Withdrawn: the beneficiary was a registered agent."),
    )
    tampered_case = _case(broken)
    with pytest.raises(ChainIntegrityError) as caught:
        verify_chain_for_export(tampered_case)
    message = str(caught.value)
    assert "seq=2" in message, message
    assert "digest mismatch" in message, message
    assert caught.value.seq == 2
    # The packet is not written when the chain fails: a half-valid exhibit is the
    # artifact a reviewer cannot rely on.
    with pytest.raises(ChainIntegrityError):
        render_case_packet(tampered_case, out_dir=tmp_path / "should-not-exist")
    assert not (tmp_path / "should-not-exist").exists()


def test_deleted_row_is_reported_as_a_sequence_gap(
    tmp_path: Path, economics: tuple[Economics, AssumptionBlock]
) -> None:
    """A missing row is a different failure from an edited one, and says so."""
    econ, block = economics
    fixture = packet_fixture(tmp_path / "gap", economics=econ, block=block)
    lines = fixture["audit_path"].read_text(encoding="utf-8").splitlines()
    fixture["audit_path"].write_text(f"{lines[1]}\n", encoding="utf-8")
    # The surviving row is the reversal (decision 2), so the packet is built for that
    # decision and the gap is the deleted row 1.
    with pytest.raises(ChainIntegrityError) as caught:
        verify_chain_for_export(_case(fixture, index=1))
    assert "seq=2" in str(caught.value)
    assert "deleted" in str(caught.value)


def test_decision_row_and_bundle_must_agree(
    tmp_path: Path, economics: tuple[Economics, AssumptionBlock]
) -> None:
    """The cover stamp comes from the audit row; a bundle that contradicts it is refused."""
    econ, block = economics
    fixture = packet_fixture(tmp_path / "disagree", economics=econ, block=block)
    bundle = fixture["bundles"][0]
    rows: tuple[ChainRow, ...] = load_chain(fixture["audit_path"])
    seq = decision_chain_seq(rows, bundle)
    tampered = make_bundle(
        economics=econ,
        block=block,
        decided_on_superseded_run=True,
    )
    path = Path(fixture["case_sink"]) / f"{tampered.idempotency_key}.json"
    path.write_text(
        json.dumps(tampered.to_payload(), sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    case = build_packet_case(
        path,
        graph_artifact_dir=fixture["graph_dir"],
        audit_chain_path=fixture["audit_path"],
        warehouse_dir=fixture["warehouse"],
        economics=econ,
        assumption_block=block,
        deployment_timezone="Africa/Kampala",
        scored_rows=fixture["scored_rows"],
    )
    assert case.decided_on_superseded_run is True
    assert seq == 1
    with pytest.raises(AuditMismatchError, match="decided_on_superseded_run"):
        _ = case.decision_row


# ------------------------------------------------------------ pinned-run truth


def test_evidence_renders_as_it_was_at_decision_time(
    tmp_path: Path, economics: tuple[Economics, AssumptionBlock]
) -> None:
    """Opened under run A, decided after run B: the packet shows run A.

    The mechanism under test is the ``run_id`` on every snapshot. Here the case is
    pinned to ``RUN_A`` while the run ledger already lists a newer complete
    ``RUN_B``, and the evidence table still prints run A's transactions with run A's
    artifact as its source.
    """
    econ, block = economics
    fixture = packet_fixture(tmp_path / "pinned", economics=econ, block=block)
    case = _case(fixture)
    assert case.bundle.run_id == RUN_A
    assert case.runs.head_run_id == RUN_B
    assert not case.runs.is_head
    html = compose_packet(case).html
    assert RUN_A in html
    assert f"{CASE_ID}-d001-{RUN_A}" in compose_packet(case).stem
    assert "paysim:t-1004" in html
    assert "edges.parquet" in html
    # A decision made while run A was current is NOT stamped as superseded, even
    # though run B exists now. Stamping it would accuse our own log of a staleness
    # it never had.
    assert case.decided_on_superseded_run is False
    assert "decided_on_superseded_run" in html
    assert ">true<" not in html.split("decided_on_superseded_run")[1][:220]


def test_superseded_run_stamp_reaches_the_cover(
    tmp_path: Path, economics: tuple[Economics, AssumptionBlock]
) -> None:
    """The audit row's flag is printed on page one, not buried in a table."""
    econ, block = economics
    bundle = make_bundle(
        economics=econ,
        block=block,
        run_id=RUN_A,
        decided_on_superseded_run=True,
    )
    fixture = packet_fixture(
        tmp_path / "superseded", economics=econ, block=block, bundles=[bundle]
    )
    html = compose_packet(_case(fixture)).html
    cover = html.split('<table class="facts">')[1].split("</table>")[0]
    assert "decided_on_superseded_run" in cover
    assert "true" in cover
    assert "already" in cover


def test_snapshot_from_another_run_is_refused(
    tmp_path: Path, economics: tuple[Economics, AssumptionBlock]
) -> None:
    """Mixing runs would describe a transaction history that never happened."""
    econ, block = economics
    fixture = packet_fixture(tmp_path / "mix", economics=econ, block=block)
    case = _case(fixture)
    wrong_run = subgraph_from_graph_artifact(
        fixture["graph_dir"],
        run_id=RUN_B,
        account_key=SUBJECT,
        hops=2,
    )
    with pytest.raises(PinnedRunMismatchError, match="pinned to"):
        PacketCase(
            **{
                **{
                    field: getattr(case, field)
                    for field in (
                        "bundle",
                        "evidence",
                        "scorecard_points",
                        "chain",
                        "decision_chain_seq",
                        "runs",
                        "economics",
                        "assumption_block",
                        "deployment_timezone",
                    )
                },
                "explanation": case.explanation,
                "subgraph": wrong_run,
            }
        )


def test_empty_decision_reason_cannot_be_rendered(
    tmp_path: Path, economics: tuple[Economics, AssumptionBlock]
) -> None:
    """P7 refuses it server-side; the packet refuses it defensively, and says which."""
    econ, block = economics
    for blank in ("", "   ", "\t\n"):
        bundle = make_bundle(economics=econ, block=block, reason=blank)
        fixture = packet_fixture(
            tmp_path / f"blank-{len(blank)}", economics=econ, block=block, bundles=[bundle]
        )
        path = Path(fixture["case_sink"]) / f"{bundle.idempotency_key}.json"
        with pytest.raises(EmptyDecisionReasonError) as caught:
            build_packet_case(
                path,
                graph_artifact_dir=fixture["graph_dir"],
                audit_chain_path=fixture["audit_path"],
                warehouse_dir=fixture["warehouse"],
                economics=econ,
                assumption_block=block,
                deployment_timezone="Africa/Kampala",
                scored_rows=fixture["scored_rows"],
            )
        message = str(caught.value)
        assert "ck_decision_reason_not_blank" in message
        assert "defensively" in message


# --------------------------------------------------------------------- money


def test_every_money_figure_carries_its_assumption_line(workspace: dict[str, Any]) -> None:
    """§11/§18: a currency number without its assumptions is a rejection trigger.

    Asserted structurally over the rendered page: each money cell contains the
    currency code, the config keys it depends on, and a basis sentence — and the
    assumption block itself appears verbatim further down.
    """
    case = _case(workspace)
    lines = case.money_lines()
    assert lines, "a packet with no money figures has no economics section"
    for line in lines:
        assert line.currency == "UGX"
        assert line.basis.strip(), f"{line.label} has a figure and no basis"
        assert line.block_text.startswith("Assumptions behind this figure")
    html = compose_packet(case).html
    economics_section = html.split('<section id="economics">')[1].split("</section>")[0]
    cells = economics_section.count('class="money"')
    assert cells == len(lines)
    assert economics_section.count("Assumption keys:") == len(lines)
    assert "config/economics.yaml ::" in economics_section
    assert "recovery.sensitivity_band" in economics_section
    assert "review_minutes_by_alert_class.E" in economics_section
    assert "friction_cost_minor" in economics_section
    assumptions_section = html.split('<section id="assumptions">')[1].split("</section>")[0]
    assert case.assumption_block.text in assumptions_section


def test_money_values_are_the_hand_computed_arithmetic(workspace: dict[str, Any]) -> None:
    """Rendered figures against arithmetic done by hand in the fixture docstring."""
    html = compose_packet(_case(workspace)).html
    assert "4,000,000.00 UGX" in html  # E_i
    assert "1,400,000.00 UGX" in html  # E_i x 0.35
    assert "1,239,500.00 UGX" in html  # EV_i as recorded
    assert "18,000.00 UGX" in html  # c_i: 120 min x 150 UGX
    assert "25,000.00 UGX" in html  # f
    assert "1,900,000.00 UGX" in html  # Monte Carlo p05
    assert "4,400,000.00 UGX" in html  # Monte Carlo p95


def test_minor_units_divide_only_at_render_time(workspace: dict[str, Any]) -> None:
    """Money stays an integer until the page is drawn (DEV-005)."""
    case = _case(workspace)
    assert case.bundle.economics.exposure_minor == 400_000_000
    line = case.money_lines()[0]
    assert line.minor == 400_000_000
    assert line.render() == "4,000,000.00 UGX"


def test_money_line_refuses_to_exist_without_assumptions() -> None:
    """The refusal is a type-level rule in the packet too, not a template convention."""
    with pytest.raises(MissingAssumptionLineError, match="assumption block"):
        money_line("Exposure", 12345, block=None, basis="none")


def test_evidence_totals_never_add_across_currencies(workspace: dict[str, Any]) -> None:
    """Two currencies in the drawn network ⇒ two lines, and the page says why."""
    case = _case(workspace)
    html = compose_packet(case).html
    evidence_section = html.split('<section id="evidence">')[1].split("</section>")[0]
    assert "Total, UGX only" in evidence_section
    # 1,250,000 + 1,100,000 + 900,000 + 4,800,000 + 300,000 + 50,000 + 250,000, all
    # UGX, hand-summed from tests/unit/p9_fixtures.py::EVENTS.
    assert "8,650,000.00 UGX" in evidence_section
    assert "never converted" in evidence_section
    assert "not a priced estimate" in evidence_section
    network_section = html.split('<section id="network">')[1].split("</section>")[0]
    assert "EUR:" in network_section
    assert "UGX:" in network_section


# ------------------------------------------------------------- legibility/copy


def test_band_letter_and_meter_glyph_render_together(workspace: dict[str, Any]) -> None:
    """DESIGN §6: no risk by colour alone, because a packet gets photocopied."""
    case = _case(workspace)
    html = compose_packet(case).html
    assert 'class="meter-letter">E</span>' in html
    meter = html.split('<svg class="meter"')[1].split("</svg>")[0]
    assert meter.count("<rect") == 5
    assert meter.count("meter-segment lit") == 5  # band E lights all five
    assert "band letter and meter glyph" in html


def test_timestamps_show_the_zone_abbreviation(workspace: dict[str, Any]) -> None:
    """§12.8: a UTC stamp shown to a UTC+3 analyst is an hour-hunting bug."""
    case = _case(workspace)
    zone = resolve_zone(case.deployment_timezone, at=case.decision.decided_at)
    assert zone.name == "Africa/Kampala"
    assert zone.abbrev == "EAT"
    assert zone.offset_hours == "+03:00"
    html = compose_packet(case).html
    # Decided at 10:30 UTC, printed in the deployment zone: Kampala is UTC+3 year-round.
    assert "2024-03-01T13:30:00 EAT" in html
    assert "Africa/Kampala (EAT, UTC+03:00)" in html


def test_subgraph_image_is_generated_not_screenshotted(workspace: dict[str, Any]) -> None:
    """The figure is vector output of the graph artifact, with its own disclosures."""
    case = _case(workspace)
    composed = compose_packet(case)
    svg = composed.svg.svg
    assert "<svg" in svg
    assert "<image" not in svg and "base64" not in svg
    assert SUBJECT[:14] in svg
    assert DELTA[:14] in svg
    assert "hop 2" in svg
    assert "not truncated" in svg or "TRUNCATED" in svg
    assert composed.svg_bytes() == svg.encode("utf-8")
    assert composed.svg.alt.startswith("Subgraph of account")
    # The rail is drawn as an endpoint of the subject's own edges, and typed as such.
    assert "rail" in svg


def test_disclaimer_is_on_page_one_and_in_the_footer(workspace: dict[str, Any]) -> None:
    """§15: page one of every packet, verbatim."""
    html = compose_packet(_case(workspace)).html
    page_one = html.split('<section id="decision">')[0]
    assert OXBOW_DISCLAIMER in page_one
    assert html.count(OXBOW_DISCLAIMER) == 2
    assert "illustrative scenario dressing" in page_one


def test_reversal_is_a_new_row_and_both_appear_in_the_timeline(workspace: dict[str, Any]) -> None:
    """Append-only is observable in the document, not asserted about the database."""
    case = _case(workspace)
    timeline = case.timeline
    assert [entry.seq for entry in timeline] == [1, 2]
    assert timeline[0].action_verb == "escalate"
    assert timeline[1].action_verb == "reverse"
    assert timeline[1].reversal_of_seq == 1
    assert timeline[0].row_hash != timeline[1].row_hash
    html = compose_packet(case).html
    assert "of seq 1" in html
    assert REASON in html
    assert "Withdrawn: the beneficiary was a registered agent" in html


def test_reason_codes_print_verbatim_from_the_bundle(workspace: dict[str, Any]) -> None:
    """The packet re-composes nothing: the scorecard's own sentence is the exhibit."""
    html = compose_packet(_case(workspace)).html
    assert "Pass-through ratio in the top decile: minus 48 points" in html
    assert "minus 21 points" in html
    assert "R4_CYCLE_MEMBER" in html


# ---------------------------------------------------------------- refusals


def test_missing_artifacts_fail_naming_their_path(tmp_path: Path) -> None:
    """No plausible defaults: the failure carries the filename to go and produce."""
    econ, block = assumption_fixture(REPO_ROOT)
    bundle = make_bundle(economics=econ, block=block)
    fixture = packet_fixture(tmp_path / "workspace", economics=econ, block=block, bundles=[bundle])
    path = Path(fixture["case_sink"]) / f"{bundle.idempotency_key}.json"

    with pytest.raises(MissingArtifactError, match="no-such-graph"):
        build_packet_case(
            path,
            graph_artifact_dir=tmp_path / "no-such-graph",
            audit_chain_path=fixture["audit_path"],
            warehouse_dir=fixture["warehouse"],
            economics=econ,
            assumption_block=block,
            deployment_timezone="Africa/Kampala",
            scored_rows=fixture["scored_rows"],
        )
    with pytest.raises(MissingArtifactError, match="audit.jsonl"):
        build_packet_case(
            path,
            graph_artifact_dir=fixture["graph_dir"],
            audit_chain_path=tmp_path / "audit.jsonl",
            warehouse_dir=fixture["warehouse"],
            economics=econ,
            assumption_block=block,
            deployment_timezone="Africa/Kampala",
            scored_rows=fixture["scored_rows"],
        )
    # No scored rows and no explanation embedded in the bundle: both paths named.
    with pytest.raises(MissingArtifactError, match="scored_rows.parquet"):
        build_packet_case(
            path,
            graph_artifact_dir=fixture["graph_dir"],
            audit_chain_path=fixture["audit_path"],
            warehouse_dir=fixture["warehouse"],
            economics=econ,
            assumption_block=block,
            deployment_timezone="Africa/Kampala",
            scored_rows=tmp_path / "nowhere" / "scored_rows.parquet",
        )
    with pytest.raises(MissingArtifactError, match="runs.jsonl"):
        build_packet_case(
            path,
            graph_artifact_dir=fixture["graph_dir"],
            audit_chain_path=fixture["audit_path"],
            warehouse_dir=tmp_path / "empty-warehouse",
            economics=econ,
            assumption_block=block,
            deployment_timezone="Africa/Kampala",
            scored_rows=fixture["scored_rows"],
        )


def test_explanation_embedded_in_the_bundle_wins(tmp_path: Path) -> None:
    """The copy the reviewer saw is the copy the packet prints, without the pipeline."""
    econ, block = assumption_fixture(REPO_ROOT)
    bundle = make_bundle(
        economics=econ,
        block=block,
        explanation={
            "source": "shap-tree-explainer",
            "unit": "logit-space SHAP value",
            "base_value": -2.5,
            "base_unit": "expected model output (logit)",
            "note": "carried in the decision bundle at decision time",
            "rows": [
                {"feature": "pass_through_ratio", "value": 0.9},
                {"feature": "outflow_degree_24h", "value": -0.25},
            ],
        },
    )
    fixture = packet_fixture(tmp_path / "embedded", economics=econ, block=block, bundles=[bundle])
    case = build_packet_case(
        Path(fixture["case_sink"]) / f"{bundle.idempotency_key}.json",
        graph_artifact_dir=fixture["graph_dir"],
        audit_chain_path=fixture["audit_path"],
        warehouse_dir=fixture["warehouse"],
        economics=econ,
        assumption_block=block,
        deployment_timezone="Africa/Kampala",
        scored_rows=None,
    )
    assert case.explanation.source == "shap-tree-explainer"
    assert case.explanation.rows[0].feature == "pass_through_ratio"
    html = compose_packet(case).html
    assert "+0.900000" in html
    assert "decision bundle provenance" in html


def test_scorecard_explained_is_labelled_as_a_different_explanation(
    tmp_path: Path,
) -> None:
    """A degenerate tree is labelled, not left blank (plan §10's fallback rule)."""
    econ, block = assumption_fixture(REPO_ROOT)
    bundle = make_bundle(
        economics=econ,
        block=block,
        explanation={
            "source": "scorecard-explained",
            "unit": "scorecard points",
            "base_value": 0.0,
            "base_unit": "no base value on the fallback path",
            "fallback_reason": "the ensemble split on 0 feature(s)",
            "note": "scorecard points carried instead",
            "rows": [{"feature": "pass_through_ratio", "value": -48.0}],
        },
    )
    fixture = packet_fixture(
        tmp_path / "fallback", economics=econ, block=block, bundles=[bundle]
    )
    case = build_packet_case(
        Path(fixture["case_sink"]) / f"{bundle.idempotency_key}.json",
        graph_artifact_dir=fixture["graph_dir"],
        audit_chain_path=fixture["audit_path"],
        warehouse_dir=fixture["warehouse"],
        economics=econ,
        assumption_block=block,
        deployment_timezone="Africa/Kampala",
    )
    assert case.explanation.is_scorecard_explained
    html = compose_packet(case).html
    assert "scorecard-explained" in html
    assert "not a missing one" in html
    assert "the ensemble split on 0 feature(s)" in html


def test_unknown_pinned_run_is_refused(tmp_path: Path) -> None:
    """A case id nobody ran cannot get a provenance line."""
    econ, block = assumption_fixture(REPO_ROOT)
    ghost = new_run_id_far_from_fixtures()
    bundle = make_bundle(economics=econ, block=block, run_id=ghost)
    fixture = packet_fixture(tmp_path / "ghost", economics=econ, block=block, bundles=[bundle])
    path = Path(fixture["case_sink"]) / f"{bundle.idempotency_key}.json"
    with pytest.raises(AuditMismatchError, match="not in the run ledger|not in the"):
        build_packet_case(
            path,
            graph_artifact_dir=fixture["graph_dir"],
            audit_chain_path=fixture["audit_path"],
            warehouse_dir=fixture["warehouse"],
            economics=econ,
            assumption_block=block,
            deployment_timezone="Africa/Kampala",
            scored_rows=fixture["scored_rows"],
        )


def new_run_id_far_from_fixtures() -> str:
    from oxbow.identity import new_ulid

    return new_ulid(int((BASE + timedelta(days=400)).timestamp() * 1000), entropy=b"\x7f" * 10)


def test_recovery_rate_outside_the_configured_band_is_refused(tmp_path: Path) -> None:
    """The assumptions printed beside a figure must be the ones that produced it."""
    econ, block = assumption_fixture(REPO_ROOT)
    bundle = make_bundle(economics=econ, block=block)
    from oxbow.ports.case_sink import EconomicsBlock

    repriced = EconomicsBlock(
        currency=bundle.economics.currency,
        exposure_minor=bundle.economics.exposure_minor,
        expected_value_minor=bundle.economics.expected_value_minor,
        recovery_rate=0.42,
        analyst_cost_minor=bundle.economics.analyst_cost_minor,
        friction_cost_minor=bundle.economics.friction_cost_minor,
        assumptions=dict(bundle.economics.assumptions),
        monte_carlo=bundle.economics.monte_carlo,
    )
    from dataclasses import replace

    moved = replace(bundle, economics=repriced)
    fixture = packet_fixture(tmp_path / "band", economics=econ, block=block, bundles=[moved])
    path = Path(fixture["case_sink"]) / f"{moved.idempotency_key}.json"
    with pytest.raises(MissingAssumptionLineError, match="sensitivity-band|not one of"):
        build_packet_case(
            path,
            graph_artifact_dir=fixture["graph_dir"],
            audit_chain_path=fixture["audit_path"],
            warehouse_dir=fixture["warehouse"],
            economics=econ,
            assumption_block=block,
            deployment_timezone="Africa/Kampala",
            scored_rows=fixture["scored_rows"],
        )


def test_written_packet_files_are_utf8_and_self_contained(workspace: dict[str, Any], tmp_path: Path) -> None:
    """The HTML carries its stylesheet, so the artifact renders without a directory."""
    case = _case(workspace)
    composed = compose_packet(case)
    assert "@page {" in composed.html
    assert "IBM Plex Sans" in composed.html
    assert "<link" not in composed.html
    target = tmp_path / "sidecars"
    target.mkdir()
    (target / f"{composed.stem}.html").write_text(composed.html, encoding="utf-8", newline="\n")
    written = (target / f"{composed.stem}.html").read_bytes()
    assert _sha256(written) == _sha256(composed.html_bytes())
    # The evidence table prints the transaction ids and the pointer they came from.
    assert "paysim:t-1001" in composed.html
    assert "edges.parquet#txn_id=paysim:t-1001" in composed.html


# --- plan §15: the disclaimer is verbatim, in README, the app footer, and page one
# of every exported packet. The plan names the asserting test, and it did not exist:
# the constant was right, the template was right, and nothing anywhere checked the
# README or the footer, so a reword in either would have survived review.
PLAN_DISCLAIMER: str = (
    "OXBOW is a research prototype that analyzes historical, de-identified data only. "
    "It does not process live financial transactions, does not trade or advise on any "
    "financial instrument, does not make real financial decisions, and is not financial "
    "advice. Monetary figures are model estimates derived from stated assumptions, not "
    "measured outcomes. Results are not validated for operational use by any financial "
    "institution."
)


def _normalise(text: str) -> str:
    """One whitespace between sentences, so line wrapping is not a second spelling.

    The plan mandates the disclaimer *verbatim*; four documents wrap it differently
    (Markdown hard-wraps, TypeScript concatenates string literals, Jinja emits one
    paragraph), and a gate that compared raw bytes would fail on formatting while
    passing on an actual reword. Punctuation and wording stay exact.
    """
    return " ".join(text.split())


def _ts_constant(source: str, name: str) -> str:
    """Reassemble a `export const NAME = 'a' + 'b';` concatenation into one string."""
    start = source.index(f"export const {name} =")
    end = source.index(";", start)
    chunks = source[start:end].split("'")[1::2]
    assert all("\\" not in chunk for chunk in chunks), (
        f"{name} uses a TS escape, which this reader does not decode"
    )
    return "".join(chunks)


def test_disclaimer_present_everywhere(workspace: dict[str, Any]) -> None:
    """The same sentence, in all three places, or the build fails.

    Asserted against the packet's **rendered** page one rather than the template
    source: what a reviewer receives is the composed document, and a template that
    kept the section but lost the value would pass a source grep.
    """
    case = _case(workspace)
    composed = compose_packet(case)
    cover = composed.html[: composed.html.index("</header>")]
    assert _normalise(PLAN_DISCLAIMER) == _normalise(OXBOW_DISCLAIMER), (
        "the constant is no longer plan §15's text verbatim"
    )
    assert _normalise(OXBOW_DISCLAIMER) in _normalise(cover), (
        "the disclaimer is not on page one (the cover section) of the rendered packet"
    )

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert _normalise(OXBOW_DISCLAIMER) in _normalise(readme), (
        "README.md does not carry the disclaimer"
    )
    for needle, why in (
        ("Lopez-Rojas", "the PaySim citation (plan §15 requires author + EMSS 2016)"),
        ("CDLA-Sharing", "IBM-AML's share-alike licence obligation"),
        ("config/economics.yaml", "the statement that money figures depend on the "
         "recovery-rate and cost assumptions, and that those are illustrative"),
        ("illustrative scenario dressing", "the note that any East-African "
         "mobile-money framing is scenario dressing over permitted public data"),
    ):
        assert needle in readme, f"README.md is missing {why}: no {needle!r}"

    copy_ts = (REPO_ROOT / "apps" / "web" / "src" / "lib" / "copy.ts").read_text(
        encoding="utf-8"
    )
    assert _normalise(_ts_constant(copy_ts, "DISCLAIMER")) == _normalise(OXBOW_DISCLAIMER), (
        "apps/web/src/lib/copy.ts DISCLAIMER has drifted from the canonical text"
    )
    shell = (REPO_ROOT / "apps" / "web" / "src" / "components" / "Shell.tsx").read_text(
        encoding="utf-8"
    )
    footer = shell[shell.index("<footer") :]
    assert "DISCLAIMER" in footer[: footer.index("</footer>")], (
        "the app footer does not render DISCLAIMER -- copy.ts having the string is not "
        "the same claim as the footer showing it"
    )
