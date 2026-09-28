"""`docs/FAILURE-MODES.md` is a claim, so it has to be a checkable one.

The register asserts, row by row, that a failure path is handled at a given line and
proved by a given test. Both halves of that sentence rot on their own: a test gets
renamed, a guard moves, a row gets deleted because its status became embarrassing. A
register nobody re-verifies is decoration, and by this repository's own rule -- *a gate
that cannot fail is decoration* -- the answer is a gate, not a promise.

What is enforced, in the order of the failure it prevents:

1. **Structure.** The register is a pipe table with exactly six populated cells per
   row. An empty "test" cell is precisely how a `handled+tested` claim would stop
   meaning anything, so a blank cell is an error rather than a rendering glitch.
2. **Every cited test exists by name.** `tests/` is walked with `ast` and every
   `def test_*` collected; a row citing `test_foo` goes red the moment `test_foo` is
   renamed or deleted. That is the whole point of the file.
3. **Every `handled at` pointer resolves.** The named file must exist and must have at
   least that many lines, so a citation cannot outlive the code it names.
4. **The honest gaps stay at the top.** Every row whose status is `handled, untested` or
   `unguarded` must appear verbatim in the ordered open-findings section. A gap may be
   closed or left open; it may not be buried below a 200-row table.
5. **The register cannot quietly shrink.** Floors on row count and distinct areas, set
   from the measured tree, so deleting rows cannot make this document look more passing.
6. **This file proves its own scanner bites**, against registers broken in four
   different ways, because a parser that silently matches nothing would report a clean
   register forever.

Deliberately *not* enforced here: that a cited test passes. That belongs to the test run
itself; collapsing "a test exists" into "the behaviour works" would make this gate slow,
flaky and less precise about which of the two claims it is refuting.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
REGISTER: Final = REPO_ROOT / "docs" / "FAILURE-MODES.md"
TESTS_DIR: Final = REPO_ROOT / "tests"

CELLS: Final = 6
AREA, FAILURE, HANDLED, SEES, TEST, STATUS = range(CELLS)
HEADER: Final = ("area", "failure", "handled at", "what the user sees", "test", "status")

STATUSES: Final = frozenset(
    {
        "handled+tested",
        "handled, untested",
        "unguarded",
        "degraded-not-broken OK",
    }
)
OPEN_STATUSES: Final = frozenset({"handled, untested", "unguarded"})

# A row may cite a Vitest file instead, written `web:<path>`. The gate checks the file
# exists; it does not claim those cases pass, and such a row may never be marked
# `handled+tested` -- that status is reserved for the Python index this gate reads.
WEB_PREFIX: Final = "web:"

# Floors set from the measured tree, not from optimism. The register was 219 rows over 14
# areas when it was written; 150/12 leaves room for honest re-categorisation while
# making the loss of a whole area -- or of a third of the table -- a build failure.
MIN_ROWS: Final = 150
MIN_AREAS: Final = 12

FINDINGS_HEADING: Final = "## Open findings"
REGISTER_HEADING: Final = "## Register"


class Row:
    """One register row, kept as data so each check reads as a sentence."""

    __slots__ = ("cells", "line")

    def __init__(self, line: int, cells: list[str]) -> None:
        self.line = line
        self.cells = cells

    @property
    def area(self) -> str:
        return self.cells[AREA]

    @property
    def failure(self) -> str:
        return self.cells[FAILURE]

    @property
    def handled(self) -> str:
        return self.cells[HANDLED]

    @property
    def test(self) -> str:
        return self.cells[TEST]

    @property
    def status(self) -> str:
        return self.cells[STATUS]

    def at(self) -> str:
        return f"line {self.line} [{self.area}] {self.failure[:58]!r}"


def _split_row(line: str) -> list[str] | None:
    """The cells of a pipe-table row, or ``None`` when the line is not one."""
    if not line.startswith("|"):
        return None
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    dashes = [cell for cell in cells if cell]
    return bool(dashes) and all(re.fullmatch(r":?-{2,}:?", cell) for cell in dashes)


def parse_register(text: str) -> list[Row]:
    """The rows of the `## Register` table, header and separator excluded.

    Parsing stops at the next `## ` heading, so the measured-counts table at the foot of
    the document cannot be mistaken for register rows.
    """
    rows: list[Row] = []
    inside = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            inside = line.strip() == REGISTER_HEADING
            continue
        if not inside:
            continue
        cells = _split_row(line.rstrip())
        if cells is None or _is_separator(cells) or tuple(cells) == HEADER:
            continue
        rows.append(Row(number, cells))
    return rows


def findings_section(text: str) -> str:
    """The open-findings block, so coverage is checked against it and not against prose."""
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == FINDINGS_HEADING),
        None,
    )
    assert start is not None, f"{REGISTER} has no {FINDINGS_HEADING!r} section"
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def python_test_names() -> frozenset[str]:
    """Every ``def test_*`` under ``tests/``, collected from the syntax tree.

    Parsed rather than grepped: the claim being checked is specifically "a function with
    this name exists", and a grep over 90 files matches prose and docstrings too.
    """
    found: set[str] = set()
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test_"
            ):
                found.add(node.name)
    return frozenset(found)


def handled_targets(handled: str) -> list[tuple[str, int]]:
    """The ``(repo-relative path, line)`` pairs a `handled at` cell cites."""
    found: list[tuple[str, int]] = []
    for match in re.finditer(
        r"(?P<path>(?:packages|apps|tests|scripts|config|docs)/[A-Za-z0-9_./-]*?):(?P<line>\d+)",
        handled,
    ):
        found.append((match.group("path"), int(match.group("line"))))
    return found


# --- the checks ----------------------------------------------------------------


def test_the_register_exists_and_is_parseable() -> None:
    assert REGISTER.is_file(), f"{REGISTER} is missing; the register is a deliverable"
    rows = parse_register(REGISTER.read_text(encoding="utf-8"))
    assert len(rows) >= MIN_ROWS, (
        f"only {len(rows)} register rows parsed; the table moved shape or the "
        f"{REGISTER_HEADING!r} heading was renamed, and a register that parses empty "
        "passes every check below for the wrong reason"
    )


def test_no_row_is_blank_or_malformed() -> None:
    rows = parse_register(REGISTER.read_text(encoding="utf-8"))
    wrong_arity = [
        f"{row.at()} -> {len(row.cells)} cells" for row in rows if len(row.cells) != CELLS
    ]
    assert not wrong_arity, (
        "every register row needs exactly six cells (area, failure, handled at, what "
        "the user sees, test, status);\nan unbalanced pipe breaks the table silently:\n"
        + "\n".join(wrong_arity)
    )
    blank = [
        f"{row.at()} -> empty cell {index} ({name})"
        for row in rows
        for index, name in enumerate(HEADER)
        if index < len(row.cells) and not row.cells[index]
    ]
    assert not blank, "no register cell may be empty:\n" + "\n".join(blank)


def test_every_status_is_one_of_the_four_allowed() -> None:
    rows = parse_register(REGISTER.read_text(encoding="utf-8"))
    bad = [f"{row.at()} -> {row.status!r}" for row in rows if row.status not in STATUSES]
    assert not bad, f"status must be one of {sorted(STATUSES)}:\n" + "\n".join(bad)


def test_every_row_names_a_test_that_exists() -> None:
    """A row cannot keep claiming a proof that has been renamed, deleted or never written."""
    names = python_test_names()
    assert len(names) > 1_000, (
        f"only {len(names)} test names were collected from {TESTS_DIR}; that index is "
        "the basis of every check below, and a thin or broken index would let any "
        "citation pass"
    )
    rows = parse_register(REGISTER.read_text(encoding="utf-8"))
    problems: list[str] = []
    for row in rows:
        cited = row.test
        if cited == "none":
            if row.status not in OPEN_STATUSES:
                problems.append(f"{row.at()} cites none but claims {row.status!r}")
            continue
        if cited.startswith(WEB_PREFIX):
            target = REPO_ROOT / cited[len(WEB_PREFIX) :]
            if not target.is_file():
                problems.append(f"{row.at()} cites a missing web test {cited}")
            elif row.status not in OPEN_STATUSES | {"degraded-not-broken OK"}:
                problems.append(
                    f"{row.at()} cites a web test yet claims {row.status!r}; only the "
                    "Python index can support that status here"
                )
            continue
        if cited not in names:
            problems.append(f"{row.at()} cites {cited!r}, which is no longer a test name in tests/")
    assert not problems, "unusable test citations:\n" + "\n".join(problems)


def test_every_handled_at_pointer_resolves_to_a_real_line() -> None:
    """A handler citation that outlives its code is a lie with a line number on it."""
    rows = parse_register(REGISTER.read_text(encoding="utf-8"))
    lengths: dict[str, int] = {}
    stale: list[str] = []
    uncited: list[str] = []
    for row in rows:
        targets = handled_targets(row.handled)
        if not targets:
            uncited.append(f"{row.at()} -> {row.handled!r} names no path:line")
            continue
        for path, line in targets:
            if path not in lengths:
                resolved = REPO_ROOT / path
                lengths[path] = (
                    len(resolved.read_text(encoding="utf-8").splitlines())
                    if resolved.is_file()
                    else -1
                )
            total = lengths[path]
            if total < 0:
                stale.append(f"{row.at()} -> {path}:{line}, and {path} does not exist")
            elif line > total:
                stale.append(f"{row.at()} -> {path}:{line}, but the file has {total} lines")
    assert not uncited, "every handled-at cell must cite a resolvable path:line:\n" + "\n".join(
        uncited
    )
    assert not stale, "unresolvable handler citations:\n" + "\n".join(stale)


def test_row_keys_are_unique() -> None:
    rows = parse_register(REGISTER.read_text(encoding="utf-8"))
    counts = Counter((row.area, row.failure) for row in rows)
    duplicated = sorted(key for key, count in counts.items() if count > 1)
    assert not duplicated, f"duplicate (area, failure) rows: {duplicated}"


def test_every_open_gap_is_listed_in_the_findings_section() -> None:
    """The gaps that matter cannot be buried under a long table."""
    text = REGISTER.read_text(encoding="utf-8")
    findings = findings_section(text)
    rows = parse_register(text)
    open_rows = [row for row in rows if row.status in OPEN_STATUSES]
    assert open_rows, (
        "no row carries an open status: a register with zero honest gaps has stopped "
        "being an audit and started being an advertisement"
    )
    missing = [row.at() for row in open_rows if row.failure not in findings]
    assert not missing, (
        f"{len(missing)} open row(s) are absent from {FINDINGS_HEADING!r}, so the gap "
        "exists only in a table nobody scrolls to:\n" + "\n".join(missing)
    )
    numbers = [int(match.group(1)) for match in re.finditer(r"^(\d+)\. \*\*", findings, re.M)]
    assert numbers, f"{FINDINGS_HEADING} holds no ordered entries"
    assert numbers == list(
        range(1, len(numbers) + 1)
    ), f"open findings must be one ordered list without gaps or repeats; got {numbers}"
    # The findings block must also sit *above* the register it summarises, or "visible at
    # the top" is a heading and nothing more.
    lines = text.splitlines()
    findings_at = next(i for i, line in enumerate(lines) if line.strip() == FINDINGS_HEADING)
    register_at = next(i for i, line in enumerate(lines) if line.strip() == REGISTER_HEADING)
    assert (
        findings_at < register_at
    ), f"{FINDINGS_HEADING} must precede {REGISTER_HEADING} in the document"


def test_the_register_covers_the_measured_area_floor() -> None:
    """Losing an area loses the claim, so the floor is a constraint and not a comment."""
    rows = parse_register(REGISTER.read_text(encoding="utf-8"))
    areas = {row.area for row in rows}
    assert len(areas) >= MIN_AREAS, (
        f"only {len(areas)} distinct areas ({sorted(areas)}); the floor of {MIN_AREAS} "
        "is the twelve areas the brief names plus docs-gate"
    )
    counts = Counter(row.status for row in rows)
    for status in sorted(STATUSES):
        assert counts[status] > 0, (
            f"no row carries status {status!r}; a register in which one of the four "
            "states is empty has stopped discriminating"
        )


# --- proof that this gate bites --------------------------------------------------


def _synthetic(handled: str, test: str, status: str) -> str:
    return (
        f"{FINDINGS_HEADING}\n\n1. **a gap**\n\n{REGISTER_HEADING}\n\n"
        "| area | failure | handled at | what the user sees | test | status |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        f"| ingest | a gap | {handled} | a refusal | {test} | {status} |\n"
    )


def test_the_gate_itself_bites() -> None:
    """Each check is run against a register broken in the way it exists to catch.

    Without this, a silent parser failure -- a renamed heading, a changed cell count, an
    index that stopped matching anything -- would keep the real file green for the wrong
    reason, which is the same defect the register is here to catch elsewhere.
    """
    real_line = "packages/pipeline/oxbow/identity.py:134"
    names = python_test_names()
    assert "test_run_id_must_be_a_ulid" in names, (
        "the fixture name this test mutates away is itself missing; the mutation "
        "proof below would then prove nothing"
    )

    parsed = parse_register(_synthetic(real_line, "test_run_id_must_be_a_ulid", "handled+tested"))
    assert len(parsed) == 1 and parsed[0].cells == [
        "ingest",
        "a gap",
        real_line,
        "a refusal",
        "test_run_id_must_be_a_ulid",
        "handled+tested",
    ], "the parser does not round-trip its own template"

    # (a) an invented status survives parsing but fails the status set
    broken_status = parse_register(
        _synthetic(real_line, "test_run_id_must_be_a_ulid", "probably fine")
    )
    assert broken_status[0].status not in STATUSES

    # (b) a renamed test is invisible to the index, which is what makes the citation fail
    renamed = parse_register(
        _synthetic(real_line, "test_run_id_must_be_a_ulid_but_renamed", "handled+tested")
    )
    assert renamed[0].test not in names

    # (c) a handler pointer past the end of a real file is caught by length, not by luck
    identity = REPO_ROOT / "packages/pipeline/oxbow/identity.py"
    total = len(identity.read_text(encoding="utf-8").splitlines())
    stale = parse_register(
        _synthetic(
            f"packages/pipeline/oxbow/identity.py:{total + 500}",
            "test_run_id_must_be_a_ulid",
            "handled+tested",
        )
    )
    _path, line = handled_targets(stale[0].handled)[0]
    assert line > total
    assert handled_targets("nothing here") == [], (
        "the extractor matches free text, so a row citing prose instead of a path:line "
        "would pass the pointer check"
    )

    # (d) an open row with no findings entry is caught
    orphaned = parse_register(_synthetic(real_line, "none", "unguarded"))
    assert orphaned[0].status in OPEN_STATUSES
    findings = findings_section(_synthetic(real_line, "none", "unguarded"))
    assert orphaned[0].failure in findings, (
        "the synthetic findings section lost the row, so the coverage check would have "
        "nothing to compare against"
    )
    buried = _synthetic(real_line, "none", "unguarded").replace("1. **a gap**", "1. **unrelated**")
    buried_row = parse_register(buried)[0]
    assert buried_row.failure not in findings_section(
        buried
    ), "an unlisted gap would have passed the findings check"
