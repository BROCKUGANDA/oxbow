"""``oxbow eval`` cannot print COMPLETE while a builder has reported that it could not measure.

The publisher has always known the answer to "did the file land" — that is what
``payload["missing_artifacts"]`` and the exit status were built on. What it did not know was the
answer to a different question a builder asks in its own return value: *"this section has no
measurement to publish, and here is the artifact that would give it one."* Both shapes exist in
this module today — a section returning ``{"status": "missing_artifact", ...}``, and a
:class:`oxbow.eval.Metric` whose value is :data:`oxbow.eval.NO_NUMBER` — and both were written
into ``eval.json`` and rendered into the documents, where the command then ignored them
completely.

The reachable case is not hypothetical. ``_drift_section`` and ``_capacity_sweep`` treat a
directory that exists but holds no files as a refusal, while ``ARTIFACTS`` treats the same
directory as present: ``out/warehouse/curve_point/`` created by a handoff that wrote nothing is
exactly the state in which the old ``main`` printed ``[eval] COMPLETE`` and exited 0.

These tests pin the gate in both directions: a refusal reaches the terminal even under
``--quiet``, a refusal alone is enough to lose the exit status, and a run whose builders all said
"present" still exits 0 — a gate that failed on everything is a gate that gets disabled.
"""

from __future__ import annotations

from typing import Any

import pytest

from oxbow import eval as oxbow_eval
from oxbow.dataset_card import CardAudit
from oxbow.eval import NO_NUMBER, collect_refusals

# ---------------------------------------------------------------------------
# collect_refusals: the two shapes a builder uses to say "not measured"
# ---------------------------------------------------------------------------


def test_a_section_that_declared_it_could_not_measure_is_a_refusal() -> None:
    payload = {
        "economics": {
            "capacity_sweep": {
                "status": "missing_artifact",
                "artifact": "out/warehouse/curve_point",
                "produced_by": "oxbow.quant.frontier via the warehouse handoff",
                "stage": "P5",
            }
        }
    }

    refusals = collect_refusals(payload)

    assert len(refusals) == 1
    line = refusals[0]
    assert "/economics/capacity_sweep" in line, "the refusal has to name where it was published"
    assert "out/warehouse/curve_point" in line
    assert "oxbow.quant.frontier" in line, "the refusal names who owes the artifact"
    assert "P5" in line


def test_a_figure_refused_by_its_own_producer_is_a_refusal() -> None:
    payload = {"integrity": {"chain_rows": {"value": NO_NUMBER, "source": "out/audit/audit.jsonl"}}}

    refusals = collect_refusals(payload)

    assert len(refusals) == 1
    assert "/integrity/chain_rows" in refusals[0]
    assert "out/audit/audit.jsonl" in refusals[0]


def test_a_published_section_and_a_scalar_are_not_mistaken_for_refusals() -> None:
    payload = {
        "model": {"status": "present", "headline": {"pr_auc": {"value": 0.059, "source": "x"}}},
        "artifacts": {"backtest_ablation": {"exists": True}},
        # The file-level list is reported as a gap by `main`; counting it again here would turn
        # one missing file into two findings and let the refusal tally drift away from the truth.
        "missing_artifacts": [
            {"name": "audit_chain", "path": "out/audit/audit.jsonl", "stage": "P7", "producer": "x"}
        ],
    }

    assert collect_refusals(payload) == []


def test_refusals_are_ordered_and_deduplicated_for_a_deterministic_card() -> None:
    same = {"status": "missing_artifact", "artifact": "out/warehouse/drift_period", "stage": "P4b"}
    payload = {"b": {"drift": same}, "a": {"drift": same}}

    refusals = collect_refusals(payload)

    assert len(refusals) == 2, "two published locations are two findings about one artifact"
    assert refusals == sorted(refusals)


# ---------------------------------------------------------------------------
# the command's own verdict
# ---------------------------------------------------------------------------


def _report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "eval_json": pytest.MonkeyPatch(),  # replaced below; main only prints its relative path
        "documents": [],
        "gaps": [],
        "refusals": [],
        "missing_artifacts": [],
        "limitation_count": 12,
        "harness_provenance": False,
        "card_audit": CardAudit(card_path="data/DATASET_CARD.md", outcomes=()),
        "card_ok": True,
        "payload": {},
    }
    report.update(overrides)
    return report


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    report: dict[str, Any],
    argv: list[str],
) -> int:
    monkeypatch.setattr(oxbow_eval, "run_eval", lambda root, **_: report)
    return oxbow_eval.main([*argv, "--root", "."])


def test_a_refusal_alone_loses_the_exit_status_and_reaches_the_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of the change: no gap, a clean card, and still not success."""
    report = _report(
        eval_json=AnyPath("data/processed/eval.json"),
        refusals=[
            "/economics/capacity_sweep: refused with status 'missing_artifact' — waiting on "
            "out/warehouse/curve_point (produced by oxbow.quant.frontier, stage P5)"
        ],
    )

    code = _run_main(monkeypatch, report, [])

    assert code == 4, "a builder's refusal must not be reportable as a completed run"
    captured = capsys.readouterr()
    assert "REFUSED BY BUILDER" in captured.err
    assert "out/warehouse/curve_point" in captured.err
    assert "[eval] COMPLETE" not in captured.out
    assert "INCOMPLETE (0 gap(s), 1 refusal(s))" in captured.out


def test_a_refusal_survives_quiet_because_quiet_is_not_an_excuse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report(
        eval_json=AnyPath("data/processed/eval.json"),
        refusals=["/model/drift: refused to publish a figure — 'not yet published' from x"],
    )

    code = _run_main(monkeypatch, report, ["--quiet"])

    assert code == 4
    err = capsys.readouterr().err
    assert "REFUSED BY BUILDER" in err
    assert "refusals reported by builders: 1" in err


def test_a_clean_run_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report(eval_json=AnyPath("data/processed/eval.json"))

    code = _run_main(monkeypatch, report, ["--quiet"])

    assert code == 0
    captured = capsys.readouterr()
    assert "REFUSED" not in captured.err
    assert "COMPLETE (0 gap(s), 0 refusal(s))" in captured.out


def test_card_drift_beside_a_refusal_is_still_the_loud_case(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from oxbow.dataset_card import FAIL, Outcome

    report = _report(
        eval_json=AnyPath("data/processed/eval.json"),
        refusals=["/model/drift: refused with status 'missing_artifact'"],
        card_audit=CardAudit(
            card_path="data/DATASET_CARD.md",
            outcomes=(
                Outcome(
                    field="ibm.rows",
                    owner="data/ibm_graph_measurement.json",
                    status=FAIL,
                    stated="93,183,650",
                    expected="93,183,651",
                ),
            ),
        ),
        card_ok=False,
    )

    code = _run_main(monkeypatch, report, [])

    assert code == 6
    assert "CARD DRIFT AND INCOMPLETE" in capsys.readouterr().err


def test_refusals_are_published_as_a_named_field_of_the_card() -> None:
    """The gate is not only the exit status: the published JSON carries the list itself."""
    source = __import__("inspect").getsource(oxbow_eval.build_eval_payload)
    assert 'payload["refusals"] = collect_refusals(payload)' in source
    assert '"refusals": payload["refusals"]' in __import__("inspect").getsource(oxbow_eval.run_eval)


class AnyPath:
    """A path-shaped stand-in: `main` prints it relative to the root and writes nothing."""

    def __init__(self, text: str) -> None:
        self._text = text

    def relative_to(self, _root: Any) -> Any:
        return self

    def __str__(self) -> str:
        return self._text
