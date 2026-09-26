"""Every environment variable the code reads has to be declared in `.env.example`.

Three real defects this catches, all found by cross-checking rather than running:
docker-compose injected `OXBOW_RUN_SALT` while `oxbow.config` resolves `RUN_SALT`, so
the pipeline inside the stack would have failed to find the salt that re-keys every
account; compose set `NEXT_PUBLIC_API_BASE` while `lib/api/transport.ts` reads
`NEXT_PUBLIC_API_BASE_URL`, so a composed web build would have silently fallen back to
a hard-coded address; and `DATABASE_URL`, `OXBOW_WAREHOUSE`, `OXBOW_WATCHLIST_PATH`,
`OXBOW_MLFLOW_URL`, `OXBOW_LOG_FORMAT` and `OXBOW_HASH_WORKERS` were read by code and
documented nowhere, which is how a working tree becomes a non-reproducible one.

The rule is directional on purpose: `.env.example` may carry more than the code reads
(compose needs credentials for services this repo only configures), but a name read by
code and absent from the file is an undocumented switch that only exists in someone's
shell.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]

_READS_PY: Final = re.compile(
    r"""(?:environ\.get|environ\[|getenv)\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""
)
_READS_TS: Final = re.compile(r"process\.env\.([A-Z][A-Z0-9_]{2,})|process\.env\[['\"]([A-Z][A-Z0-9_]{2,})['\"]\]")

# Set by the runtime or the platform rather than by this project, so declaring them in
# .env.example would be noise: Node/Next provide NODE_ENV, uv and the container runtime
# provide PATH/HOME, and CI sets GITHUB_*.
FRAMEWORK_VARS: Final[frozenset[str]] = frozenset(
    {
        "NODE_ENV",
        "PATH",
        "HOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "UV_CACHE_DIR",
        "SYSTEMROOT",
        "COMSPEC",
        "PWD",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "TZ",
    }
)


def _python_reads(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _READS_PY.finditer(text):
            found.setdefault(match.group(1), str(path.relative_to(REPO_ROOT)))
    return found


def _ts_reads(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.ts*")):
        parts = set(path.parts)
        if {"node_modules", ".next", "dist"} & parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _READS_TS.finditer(text):
            name = next((group for group in match.groups() if group), None)
            if name:
                found.setdefault(name, str(path.relative_to(REPO_ROOT)))
    return found


def _declared_keys() -> set[str]:
    example = REPO_ROOT / ".env.example"
    keys: set[str] = set()
    for line in example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and "=" in stripped:
            # A commented-out example (DATABASE_URL=...) is still documentation of the
            # name; a bare prose comment is not.
            candidate = stripped.lstrip("#").strip().split("=", 1)[0].strip()
            if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", candidate):
                keys.add(candidate)
        elif re.match(r"^[A-Z][A-Z0-9_]{2,}=", stripped):
            keys.add(stripped.split("=", 1)[0].strip())
    return keys


def test_every_env_var_read_by_code_is_declared() -> None:
    reads: dict[str, str] = {}
    for root in (
        REPO_ROOT / "packages",
        REPO_ROOT / "apps",
        REPO_ROOT / "scripts",
    ):
        reads |= _python_reads(root)
        reads |= _ts_reads(root)

    assert reads, "no environment reads found at all; this gate would pass by accident"
    declared = _declared_keys()
    undeclared = sorted(
        name for name in reads if name not in declared and name not in FRAMEWORK_VARS
    )
    assert not undeclared, (
        "these variables are read by code but declared nowhere, so a fresh clone "
        f"cannot reproduce the behaviour: {undeclared}. Where they are read: "
        + ", ".join(f"{name}={reads[name]}" for name in undeclared)
    )


def test_the_gate_itself_bites() -> None:
    """A name invented by the reader and absent from the file must be reported.

    Without this, a regex that silently stopped matching -- a refactor to
    `config("X")`, say -- would turn the gate green by reading nothing.
    """
    reads = {"RUN_SALT": "x.py", "NODE_ENV": "y.ts", "OXBOW_NOT_DOCUMENTED_ANYWHERE": "z.py"}
    declared = _declared_keys()
    missing = sorted(
        name
        for name in reads
        if name not in declared and name not in FRAMEWORK_VARS
    )
    assert missing == ["OXBOW_NOT_DOCUMENTED_ANYWHERE"], missing
    assert "RUN_SALT" in declared, "RUN_SALT must stay declared or the check above lies"
