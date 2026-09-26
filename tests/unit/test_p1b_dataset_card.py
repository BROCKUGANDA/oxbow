"""The dataset-card gate: it must pass on the committed card, and bite on a mutated one.

Why this file exists next to the others. ``data/DATASET_CARD.md`` is authored prose, and an
earlier ``make eval`` rendered it from artifacts, overwriting judgement no script can
derive -- including the PaySim reuse figure that decided which corpus feeds which module.
The fix has two halves, and a doc gate that has never failed on a deliberate error is
decoration, so this file exercises both:

1. ``oxbow eval`` no longer writes the card (``test_eval_verifies_the_card_it_does_not_write_it``).
2. The verifier refuses a card whose stated figure and owning value disagree, naming the
   field and both values (``test_mutating_a_measured_figure_is_named_and_refused``).

The SKIPPED path is asserted too: on a tree with no corpus and no artifacts the gate must
report that it could not look, never that the card agrees with what it could not find.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from oxbow import eval as oxbow_eval
from oxbow.dataset_card import CARD_RELPATH, CHECKS, Facts, Skip, verify_dataset_card

REPO_ROOT = Path(__file__).resolve().parents[2]
CARD = REPO_ROOT / CARD_RELPATH


def card_text() -> str:
    return CARD.read_text(encoding="utf-8")


# --- the committed card, as it stands --------------------------------------


def test_committed_card_passes_the_gate() -> None:
    """Every gated figure on disk agrees with the artifact or config that owns it.

    This is the test that fails when a measurement script is re-run and the card is left
    behind, and the reason the figures in the card are worth quoting.
    """
    audit = verify_dataset_card(REPO_ROOT)
    assert audit.outcomes, "the gate has no checks, which is not the same as passing"
    assert [
        f"{outcome.field}: {outcome.reason} (owns {outcome.owner})" for outcome in audit.failures
    ] == []


def test_only_the_oversized_digests_are_unverifiable_here() -> None:
    """The skip list is exactly the bytes a documentation run must not re-hash.

    A new skip means an artifact went missing or a pointer moved; either way the card got
    less checked than it was, and that has to be a named failure rather than a quieter pass.
    """
    audit = verify_dataset_card(REPO_ROOT)
    assert sorted(outcome.field for outcome in audit.skipped) == [
        "ibm.accounts_sha256_on_host",
        "ibm.trans_sha256_on_host",
    ]
    for outcome in audit.skipped:
        assert "above the" in outcome.reason and "byte limit" in outcome.reason


def test_every_card_section_is_covered_by_checks() -> None:
    """No section of the card quietly fell out of the gate when its headings moved."""
    windows = {check.window for check in CHECKS}
    assert windows == {"paysim", "ibm", "typologies", "cycles", "sampling"}
    for window in sorted(windows):
        assert sum(check.window == window for check in CHECKS) >= 3, window


# --- it bites --------------------------------------------------------------

# (what a mutation is meant to name, the exact text in the card, the lie that replaces it)
MUTATIONS: tuple[tuple[str, str, str], ...] = (
    ("paysim.rows", "| Rows | 6,362,620 |", "| Rows | 6,362,621 |"),
    ("paysim.reuse_ratio", "**0.001464**", "**0.001465**"),
    ("ibm.rows", "| Rows | 5,078,345 |", "| Rows | 5,078,344 |"),
    (
        "ibm.directed_edges_excl_self_loops",
        "set aside | 647,939 |",
        "set aside | 647,940 |",
    ),
    (
        "typologies.annotated_transactions",
        "| Annotated transactions | 3,209 |",
        "| Annotated transactions | 3,210 |",
    ),
    ("typologies.cycle_rows", "| CYCLE rows | 287 |", "| CYCLE rows | 288 |"),
    ("typologies.per_typology", "CYCLE 287 ·", "CYCLE 288 ·"),
    (
        "sampling.interactive_txn_target",
        "| Interactive target | 500,000 transactions |",
        "| Interactive target | 50,000 transactions |",
    ),
    (
        "ibm.patterns_sha256_recomputed",
        "dec2ef39b",
        "dec2ef39a",
    ),
)


@pytest.mark.parametrize(("field", "original", "mutated"), MUTATIONS, ids=lambda v: str(v)[:28])
def test_mutating_a_measured_figure_is_named_and_refused(
    field: str, original: str, mutated: str
) -> None:
    """The gate catches a one-digit lie, says which field it is, and shows both values.

    The card is never written here: the mutation is applied to the text in memory, which is
    also why :func:`verify_dataset_card` takes ``card_text`` at all.
    """
    text = card_text()
    assert text.count(original) >= 1, f"{original!r} is not in the card: this test is stale"
    tampered = text.replace(original, mutated, 1)

    audit = verify_dataset_card(REPO_ROOT, card_text=tampered)

    assert not audit.ok, f"the gate accepted a changed {field}"
    failures = {outcome.field: outcome for outcome in audit.failures}
    assert field in failures, f"{field} not named; it reported {sorted(failures)}"
    outcome = failures[field]
    # The verdict names the figure it read and the value the artifact owns, so the fix is
    # actionable without re-running the measuring script to find out which one moved.
    assert outcome.stated.strip() in mutated or mutated in outcome.stated.strip()
    assert outcome.expected, "a failure that does not print the owning value is not actionable"
    assert outcome.expected.strip() != outcome.stated.strip()
    assert audit.passed, "the other figures must still be checked, not skipped in a panic"


def test_deleting_a_figure_fails_rather_than_passing() -> None:
    """A card that stopped stating a measurement is drift, not an empty slot."""
    text = card_text()
    without_rows = "\n".join(
        line for line in text.splitlines() if not line.startswith("| Rows | 6,362,620 |")
    )
    assert without_rows != text

    audit = verify_dataset_card(REPO_ROOT, card_text=without_rows)

    missing = {outcome.field for outcome in audit.failures}
    assert {"paysim.rows", "paysim.rows_recounted"} <= missing, sorted(missing)
    assert any("no longer states" in line for line in audit.failure_lines())


def test_moving_a_section_reports_skips_naming_the_heading() -> None:
    """Restructuring the card cannot switch the gate off silently."""
    text = card_text().replace("### 2a. Typology ground truth", "### 2b. Typology ground truth")

    audit = verify_dataset_card(REPO_ROOT, card_text=text)

    typology_skips = [o for o in audit.skipped if o.field.startswith("typologies.")]
    assert len(typology_skips) == sum(check.field.startswith("typologies.") for check in CHECKS)
    assert "Typology ground truth" in typology_skips[0].reason


def test_a_tree_without_the_artifacts_yields_skips_never_passes(tmp_path: Path) -> None:
    """'Could not look' is reported as such; it is not folded into the pass total."""
    tmp_root = tmp_path / "repo"
    (tmp_root / "data").mkdir(parents=True)
    (tmp_root / CARD_RELPATH).write_text(card_text(), encoding="utf-8")

    audit = verify_dataset_card(tmp_root)

    # One figure is arithmetic rather than measurement, so it survives an empty tree; every
    # check with an artifact or config owner reports SKIPPED with the path named.
    assert {outcome.field for outcome in audit.passed} == {"sampling.four_node_cycle_survival"}
    assert audit.failures == ()
    assert len(audit.skipped) == len(CHECKS) - 1
    reasons = " ".join(outcome.reason for outcome in audit.skipped)
    assert "data/graph_measurement.json is not on this host" in reasons
    assert "config/sources.yaml" in reasons
    summary = audit.summary()
    assert "not claimed as passing" in summary


def test_facts_reports_a_moved_pointer_as_a_skip(tmp_path: Path) -> None:
    """A renamed or absent artifact field must not read as a value of zero or an empty string."""
    facts = Facts(tmp_path)
    with pytest.raises(Skip) as excinfo:
        facts.field("data/graph_measurement.json", "/n_rows")
    assert "not on this host" in str(excinfo.value)


# --- and it does not write the document it checks --------------------------


def test_eval_verifies_the_card_it_does_not_write_it() -> None:
    """``make eval`` must never render ``data/DATASET_CARD.md`` again.

    Pinned structurally because the regression was a whole document being replaced: the
    renderer is gone from the module, the five derived documents are still written, and the
    card is checked instead.
    """
    assert not hasattr(
        oxbow_eval, "render_dataset_card"
    ), "the dataset-card renderer is back; `make eval` would overwrite authored prose"
    rendered = inspect.getsource(oxbow_eval.render_documents)
    assert "data/DATASET_CARD.md" not in rendered.replace(
        "``data/DATASET_CARD.md``", ""
    ), "the card is in the render map again"
    assert "verify_dataset_card" in inspect.getsource(oxbow_eval.run_eval)


def test_verification_leaves_the_card_byte_identical() -> None:
    before = CARD.read_bytes()
    audit = verify_dataset_card(REPO_ROOT)
    assert audit.outcomes
    assert CARD.read_bytes() == before, "the verifier wrote the file it was asked to check"
