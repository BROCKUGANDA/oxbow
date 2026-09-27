"""The decision record has to contain the decisions the code cites.

On 2026-09-27, assembling `PROMPT.md` meant walking the `DEV-NNN` sequence to check it was
complete. It was not: DEV-011 and DEV-013 each have an entry, and DEV-012 — the rule that keeps
`ingested_at` and `run_id` out of the persisted Parquet bytes, which is what makes
`make verify-determinism` implementable at all — was cited by name in 27 places across 17 files
and had no entry anywhere. Every module that needed it quoted a decision record that did not
contain it.

That is not a typo risk, it is how a decision gets lost: the citation is copied forward, the
prose is never read, and the authority for a load-bearing rule becomes a number. The plan's §16
definition of done says `DECISIONS.md` is where measured reality beating the spec gets recorded,
and a record with a hole where the determinism rule belongs fails that on its face.

So two checks, both cheap, both about the failure that actually happened:

* the sequence 001..N is contiguous and duplicate-free, and
* every `DEV-NNN` referenced anywhere else in the repository is declared here.

The second is the one that would have caught DEV-012. The first is included because a gap is what
made the omission visible at all — nothing else in the tree indexes the numbering, so a missing
number is otherwise silent.

Deliberately NOT asserted: a required section per entry (e.g. "Status"). Three of twenty-five
entries carry one, so it is a style some entries happen to use rather than the project's
convention, and a gate that invents a convention gets worked around instead of followed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DECISIONS: Final = REPO_ROOT / "DECISIONS.md"

#: Directories whose contents are generated, vendored, or enormous. `apps/web/node_modules` alone
#: is large enough that walking it would make this the slowest test in the suite for no benefit:
#: a vendored dependency cannot cite this repository's decision record.
SKIP_DIRS: Final = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        ".next",
        "__pycache__",
        ".test-output",
        "dist",
        "build",
    }
)
SCAN_SUFFIXES: Final = frozenset({".py", ".pyi", ".md", ".yaml", ".yml", ".toml", ".ts", ".tsx"})
SCAN_ROOTS: Final = (
    "packages",
    "apps",
    "scripts",
    "config",
    "tests",
    "data",
    "docs",
)
CITED = re.compile(r"\bDEV-(\d{3})\b")
DECLARED = re.compile(r"^## (DEV-(\d{3}))\b", re.MULTILINE)


def _declared() -> set[str]:
    text = DECISIONS.read_text(encoding="utf-8")
    return {match.group(1) for match in DECLARED.finditer(text)}


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for relative in SCAN_ROOTS:
        root = REPO_ROOT / relative
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix not in SCAN_SUFFIXES or not path.is_file():
                continue
            if SKIP_DIRS.intersection(set(path.parts)):
                continue
            files.append(path)
    for extra in sorted(REPO_ROOT.glob("*.md")):
        if extra.name != "DECISIONS.md":
            files.append(extra)
    return files


def test_the_decision_sequence_has_no_gaps() -> None:
    """A hole in the numbering is how an unwritten decision stays invisible."""
    numbers = sorted(int(tag[4:]) for tag in _declared())
    assert numbers, "no DEV entries parsed — did the heading format change?"
    gaps = sorted({n for n in range(1, max(numbers) + 1)} - set(numbers))
    assert gaps == [], (
        f"DECISIONS.md skips {['DEV-%03d' % g for g in gaps]}; the sequence runs "
        f"{min(numbers)}..{max(numbers)} with {len(numbers)} entries"
    )
    assert len(numbers) == len(set(numbers)), "a DEV number is declared twice"


def test_every_cited_decision_is_written() -> None:
    """The DEV-012 check: cited 27 times, declared zero times."""
    declared = _declared()
    missing: dict[str, list[str]] = {}
    for path in _scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for tag in sorted({f"DEV-{n}" for n in CITED.findall(text)}):
            if tag not in declared:
                missing.setdefault(tag, []).append(str(path.relative_to(REPO_ROOT)))

    assert not missing, "these decisions are cited but never written: " + "; ".join(
        f"{tag} ({len(where)} place(s), e.g. {where[0]})" for tag, where in sorted(missing.items())
    )


def test_the_scan_actually_reads_something() -> None:
    """A gate that scans nothing passes vacuously, which is the failure it exists to catch."""
    files = _scanned_files()
    assert len(files) > 200, f"only {len(files)} files scanned — the file walk is broken"
    # The point of the walk is the citations, so prove it can see at least one.
    cited_any = any(
        CITED.search(path.read_text(encoding="utf-8", errors="replace"))
        for path in files[:400]
    )
    assert cited_any, "no DEV citation found in the scanned files; the regex or the walk is wrong"


@pytest.mark.parametrize("tag", ["DEV-012"])
def test_the_decision_that_started_this(tag: str) -> None:
    """The specific hole, pinned. Delete this test only by deleting the rule it protects."""
    assert tag in _declared(), f"{tag} is not declared in DECISIONS.md"
    text = DECISIONS.read_text(encoding="utf-8")
    body = text.split(f"## {tag}", 1)[1].split("\n## DEV-", 1)[0]
    assert "ingested_at" in body and "run_id" in body, (
        f"{tag} lost the two columns the rule is about"
    )
    assert "verify-determinism" in body, f"{tag} no longer names the gate it exists to make work"
