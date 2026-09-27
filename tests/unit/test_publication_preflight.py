"""Publication pre-flight: what a repository must not contain the moment it is public.

OXBOW is heading to a public hackathon repository, which changes the threat model of an
otherwise-private tree: a developer path, a default secret, a licensed corpus committed as
bytes, or a dataset added to config without its obligations carried along are all invisible
defects while the work is local and become public ones the moment it is not.

Each test reads tracked bytes (`git ls-files`), not the working directory, so an untracked
scratch file does not fail the build and a committed one cannot hide behind a gitignore.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]

# Text only: reading 500 MB of parquet into a regex is not a check, it is an outage.
TEXT_SUFFIXES = frozenset(
    {
        ".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".yaml", ".yml", ".toml",
        ".md", ".txt", ".css", ".html", ".sh", ".ps1", ".cfg", ".ini", ".example",
    }
)
SKIP_NAMES = frozenset({"uv.lock", "pnpm-lock.yaml", "bun.lock"})
LARGE_FILE_CAP = 2_000_000


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return [REPO_ROOT / name for name in out.decode("utf-8").split("\0") if name]


def tracked_text() -> dict[Path, str]:
    files: dict[Path, str] = {}
    for path in tracked_files():
        if path.suffix not in TEXT_SUFFIXES and path.name not in {
            "LICENSE",
            "Dockerfile",
            "Makefile",
            ".gitignore",
            ".pre-commit-config.yaml",
        }:
            continue
        if path.name in SKIP_NAMES or not path.is_file():
            continue
        if path.stat().st_size > LARGE_FILE_CAP:
            continue
        try:
            files[path] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return files


def rel(paths: list[Path]) -> list[str]:
    return sorted(str(p.relative_to(REPO_ROOT)) for p in paths)


def test_the_repository_declares_a_license_and_says_which() -> None:
    """A public repo with no LICENSE is code under default copyright: nobody may use it.

    The declaration has to agree in all three places a reader looks, because a LICENSE file
    beside `license = "MIT"` in pyproject and nothing in package.json is three answers.
    """
    license_file = REPO_ROOT / "LICENSE"
    assert license_file.is_file(), (
        "no LICENSE at the repository root. A hackathon submission asks judges and future "
        "readers to copy this tree; without a licence grant they cannot."
    )
    head = license_file.read_text(encoding="utf-8")
    assert head.startswith("MIT License"), f"LICENSE opens with {head.splitlines()[0]!r}"
    assert "Copyright (c)" in head, "LICENSE carries no copyright line"

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"].get("license") == "MIT", (
        "pyproject does not declare MIT, so the packaged metadata contradicts the LICENSE file"
    )

    web = REPO_ROOT / "apps" / "web" / "package.json"
    assert '"license": "MIT"' in web.read_text(encoding="utf-8"), (
        "apps/web/package.json declares no license while the LICENSE file covers it"
    )


def test_no_developer_home_path_is_committed() -> None:
    """`C:\\Users\\name\\...` is a username, and it is a path that exists on one machine.

    Found here for real: apps/web/playwright.config.ts defaulted its browser binary to one
    author's cache directory, which leaked the username into a tree meant for judges and
    broke the suite for everyone else. The config now searches the platform's Playwright
    cache and honours OXBOW_CHROME_PATH.
    """
    pattern = re.compile(r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]\w+|/home/\w{3,}/|/Users/\w{3,}/)")
    offenders = [path for path, text in tracked_text().items() if pattern.search(text)]
    assert not offenders, (
        f"tracked files carry an absolute developer path: {rel(offenders)}. "
        "Derive it at runtime (os.path.expanduser, LOCALAPPDATA, XDG_CACHE_HOME) or read it "
        "from an environment variable whose example value is a placeholder."
    )


def test_no_private_key_or_pem_material_is_committed() -> None:
    offenders = [
        path
        for path, text in tracked_text().items()
        if "-----BEGIN " in text and "PRIVATE KEY-----" in text
    ]
    assert not offenders, f"tracked files contain private key material: {rel(offenders)}"


def test_no_secret_shaped_literal_is_committed() -> None:
    """A hard-coded credential, including the "default" one a receiver still accepts.

    The webhook receiver once shipped trusting a default signing key committed in the same
    repository, which makes the HMAC signature decoration rather than a control (DEV-017's
    family of self-deception). This is the same class, checked on every commit.

    Two different standards apply, because the two directories mean different things. In
    shipped code any secret-shaped literal is a finding — there is no legitimate reason for
    one. Under tests/ a literal is only allowed when it announces itself as deliberately
    wrong ("not-the-real-one", "some-other-tenant-secret"): those are the negative cases that
    prove a receiver rejects a bad signature, and refusing to commit them would push the
    tests towards real-looking keys, which is worse. The allowance is a named list of
    wrongness markers, not a blanket exemption for files in a directory.
    """
    pattern = re.compile(
        r"(?i)(secret|token|password|passwd|api[_-]?key|signing[_-]?key|hmac[_-]?key)"
        r"\s*[:=]\s*[\"'](?P<value>[^\"'\s<>]{12,})[\"']"
    )
    fake_markers = (
        "example", "placeholder", "changeme", "dummy", "sample", "test-", "invalid",
        "wrong", "not-", "not_", "other-", "other_", "bad-", "bad_", "no-secret",
    )
    shipped: list[str] = []
    test_files: list[str] = []
    for path, text in tracked_text().items():
        under_test = "tests" in path.relative_to(REPO_ROOT).parts
        for match in pattern.finditer(text):
            value = match.group("value")
            if value.startswith(("os.environ", "env.", "${")) or "{" in value:
                continue
            if path.name.endswith(".example"):
                continue  # the documented fake values are the point of that file
            if not under_test:
                shipped.append(f"{path.relative_to(REPO_ROOT)}: {match.group(1)}=…")
            elif not any(marker in value.lower() for marker in fake_markers):
                test_files.append(f"{path.relative_to(REPO_ROOT)}: {value[:10]}…")
    assert not shipped, (
        f"secret-shaped literals in shipped code: {shipped[:6]}. Read them from the "
        "environment; a default an operator has to override is a default they will not override."
    )
    assert not test_files, (
        f"a test hard-codes a key that does not announce itself as wrong: {test_files[:6]}. "
        "Negative cases should read as negative (not-the-real-one), so a reader can tell a "
        "planted bad secret from a leaked good one."
    )


def test_the_run_salt_value_is_not_in_the_tree() -> None:
    """The identity salt stays in .env. Its *value* must never reach a tracked byte.

    Asserted without printing it: the only thing quoted on failure is that it was found.
    """
    dotenv = REPO_ROOT / ".env"
    if not dotenv.is_file():
        return  # a clean clone has no .env; the check is for the machine that made the commit
    match = re.search(r"^RUN_SALT=(.+)$", dotenv.read_text(encoding="utf-8"), re.M)
    if match is None:
        return
    salt = match.group(1).strip().strip("\"'")
    if len(salt) < 8:
        return
    hits = [path for path, text in tracked_text().items() if salt in text]
    assert not hits, (
        f"the RUN_SALT value from .env appears in tracked files: {rel(hits)}. "
        "It is the identifier that makes account keys unlinkable; publishing it un-links them."
    )


def test_env_file_itself_is_never_tracked() -> None:
    tracked = tracked_files()
    assert not [p for p in tracked if p.name == ".env" or p.name.startswith(".env.") and p.name != ".env.example"], (
        "a dot-env file is tracked; .env holds RUN_SALT and belongs in .gitignore"
    )


def test_the_licensed_corpora_stay_out_of_the_repository() -> None:
    """The corpora are fetched by `make data` and verified against recorded hashes.

    Committing them would be three defects at once: a licensed redistribution the
    obligations in config/sources.yaml do not authorise, a repository nobody can clone, and
    a second copy of bytes the manifest already pins.
    """
    offenders = [
        path
        for path in tracked_files()
        if path.parts[-3:-2] == ("raw",) and path.name != ".gitkeep"
    ]
    assert not offenders, f"raw corpus bytes are committed: {rel(offenders)[:5]}"

    interim = [
        path
        for path in tracked_files()
        if "interim" in path.parts and path.suffix in {".parquet", ".csv", ".7z", ".zip"}
    ]
    assert not interim, f"canonical or ingested payloads are committed: {rel(interim)[:5]}"


def test_every_licensed_source_is_declared_in_the_license_notice() -> None:
    """Add a dataset to config/sources.yaml without carrying its licence into LICENSE and
    this fails — which is the point. The obligations are the part a code-only review misses,
    and CC BY-SA / CDLA-Sharing are share-alike, not "free to use".
    """
    sources = (REPO_ROOT / "config" / "sources.yaml").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s+license:\s*[\"']([^\"']+)[\"']", sources, re.M))
    assert declared, "config/sources.yaml declares no licenses at all — the pattern stopped matching"
    notice = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    missing = sorted(licence for licence in declared if licence.split()[0] not in notice)
    assert not missing, (
        f"LICENSE does not account for these dataset licences: {missing}. It covers the code "
        "and must name the terms the data keeps."
    )

    # A source that may not be ingested must say so, and its licence must be cited anyway.
    blocked = re.findall(r"^\s+ingest_allowed: false", sources, re.M)
    cited = re.findall(r"^\s+citation:", sources, re.M)
    assert len(blocked) <= len(cited), (
        f"{len(blocked)} source(s) are marked ingest_allowed: false but only {len(cited)} "
        "carry a citation — a dataset you may not use still has to be attributed if it is named"
    )
