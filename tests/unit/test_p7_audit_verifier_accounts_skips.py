"""`verify_audit.py` reported the chains it skipped as chains it walked.

The summary line was `chains walked: {len(results)}`, and `results` is the list of *reports*,
one per chain attempted — SKIPPED included. On this host, where no reviewer decision has ever
been written and `DATABASE_URL` is unset, that printed:

    chains walked: 2  failed: 0

while the two lines above it both said SKIPPED. A verifier whose headline reads green because it
found nothing to look at is precisely the failure this script exists to catch, and it is the
shape this repository keeps redocumenting: the number has to be the number of things actually
read, and a run that read nothing has to say so in words a reader cannot mistake.

The chain fixtures here are built with `append_row` and `FileAuditSink` — the producing code, not
a hand-written JSONL — because a test of the verifier that invented the row format would be
checking the verifier against the test.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from oxbow.adapters.file.audit import FileAuditSink  # noqa: E402
from oxbow.audit.chain import append_row  # noqa: E402


def _load_verifier() -> object:
    spec = importlib.util.spec_from_file_location(
        "verify_audit", REPO_ROOT / "scripts" / "verify_audit.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_audit = _load_verifier()


def _write_chain(directory: Path, *, rows: int = 2) -> Path:
    """A real chain, produced by the code that produces chains."""
    sink = FileAuditSink(directory)
    prev = None
    for index in range(rows):
        prev = sink.append(
            append_row(
                prev=prev,
                occurred_at=datetime(2026, 9, 27, 12, 0, index, tzinfo=UTC),
                actor_id="analyst-1",
                subject=f"case-{index}",
                action="review",
                payload={"reason": f"reviewed at {index}"},
            )
        )
    return directory / "audit.jsonl"


def _run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = verify_audit.main(argv)  # type: ignore[attr-defined]
    return int(code), buffer.getvalue()


def test_a_run_that_walked_nothing_says_so(tmp_path: Path) -> None:
    """The defect: two SKIPPED lines and a headline that read "chains walked: 2"."""
    code, out = _run(["--audit-dir", str(tmp_path), "--no-database"])
    summary = next(line for line in out.splitlines() if line.startswith("chains walked:"))

    assert "chains walked: 0" in summary, out
    assert "skipped: 1" in summary, out
    assert "NOTHING VERIFIED" in out, "a verifier that read nothing must not look like a pass"
    assert code == 0, "nothing broken is still not a failure; it is simply nothing"


def test_a_real_chain_is_counted_as_walked(tmp_path: Path) -> None:
    path = _write_chain(tmp_path)
    assert path.is_file(), "the fixture did not write the chain it claims to"

    code, out = _run(["--audit-dir", str(tmp_path), "--no-database"])
    summary = next(line for line in out.splitlines() if line.startswith("chains walked:"))

    assert "chains walked: 1" in summary, out
    assert "failed: 0" in summary, out
    assert "NOTHING VERIFIED" not in out
    assert code == 0


def test_a_broken_link_is_counted_as_walked_and_fails(tmp_path: Path) -> None:
    """The gate has to be capable of failing, and a broken row must land in `walked`, not `skipped`."""
    import json

    path = _write_chain(tmp_path, rows=2)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    # Zeroing the second row's own digest breaks the chain at sequence 2 while leaving the file
    # parseable, so the verifier walks it and reports the break rather than skipping past it.
    records[1]["row_hash"] = "0" * len(records[1]["row_hash"])
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8"
    )

    code, out = _run(["--audit-dir", str(tmp_path), "--no-database"])
    summary = next(line for line in out.splitlines() if line.startswith("chains walked:"))

    assert "chains walked: 1" in summary, out
    assert "failed: 1" in summary, out
    assert code == 1, "a broken hash chain is the one thing this script exists to catch"
    assert "NOTHING VERIFIED" not in out
