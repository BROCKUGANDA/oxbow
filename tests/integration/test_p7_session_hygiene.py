"""Every warehouse read has to hand its session back.

`PostgresSource` is built on a provider that mints a **new** `Session` on each call
(`deps.py` passes `lambda: _new_session(sessions)`), so `self.session` is not a
connection the object owns -- it is a fresh checkout. Six read paths took one and
never closed it, which leaks a connection per read and leaves `ACCESS SHARE` on
whatever was selected, blocking writes and `TRUNCATE` on the same tables.

The instrument is the session itself rather than the engine, and the statements are
real SQLAlchemy Core built from the pipeline's own `Base.metadata`: nothing here
needs a database, and nothing here is mocked at the boundary being tested. A fake
engine would have let the leak hide in the part we actually care about.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Same bootstrap the other API tests use: served as `uvicorn main:app --app-dir
# apps/api`, so the modules are `api.*` with `apps` on the path.
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api.readmodel import Base, PostgresSource  # noqa: E402

_TABLE = "account"


class _Result:
    """The two shapes the read paths use: `.mappings()` rows, and a scalar total."""

    def __init__(self, rows: list[dict[str, Any]], scalar: int) -> None:
        self._rows = rows
        self._scalar = scalar

    def mappings(self) -> list[dict[str, Any]]:
        return self._rows

    def scalar_one(self) -> int:
        return self._scalar

    def scalar(self) -> int:
        return self._scalar


class _Session:
    def __init__(self, ledger: list["_Session"], *, fail: bool = False) -> None:
        self.closed = False
        self.ledger = ledger
        self.fail = fail
        ledger.append(self)

    def execute(self, _statement: Any) -> _Result:
        if self.fail:
            raise RuntimeError("simulated connection loss")
        return _Result([], 0)

    def close(self) -> None:
        self.closed = True


def _source(*, fail: bool = False) -> tuple[PostgresSource, list[_Session]]:
    ledger: list[_Session] = []
    return PostgresSource(lambda: _Session(ledger, fail=fail)), ledger


def _require_table() -> str:
    if _TABLE not in Base.metadata.tables:  # pragma: no cover - guards the premise
        pytest.skip(f"{_TABLE} is not in the warehouse metadata; this test's premise is stale")
    return _TABLE


@pytest.mark.parametrize(
    "read",
    ["select", "rows", "scalar", "session_scope", "alert_rows", "edges_for"],
)
def test_each_read_closes_the_session_it_opened(read: str) -> None:
    """The happy path: one checkout in, one close, no session left hanging."""
    table = _require_table()
    source, ledger = _source()
    statement = Base.metadata.tables[table].select()

    if read == "select":
        source.select(table, where={"run_id": "01X"}, allow_missing=True)
    elif read == "rows":
        source.rows(table, where={"run_id": "01X"})
    elif read == "scalar":
        source.scalar(statement)
    elif read == "session_scope":
        with source.session_scope() as session:
            session.execute(statement)
    elif read == "alert_rows":
        source.alert_rows(run_id="01X" + "0" * 22, policy_id="pol-default", limit=5)
    else:
        source.edges_for("01X" + "0" * 22, ["a11ce0000000"])

    assert ledger, f"{read} performed no read at all, so the test proved nothing"
    assert all(session.closed for session in ledger), (
        f"{read} left {sum(not s.closed for s in ledger)} of {len(ledger)} "
        "session(s) open: each one holds a pooled connection and an ACCESS SHARE lock"
    )


def test_a_failed_read_still_closes() -> None:
    """The error path is where a leak is worst, because it is the path in production.

    `DependencyUnavailable` maps to a 503 and the request ends, but a worker that
    survived the exception with its session open would keep the lock after the
    caller gave up on the retry.
    """
    table = _require_table()
    source, ledger = _source(fail=True)
    with pytest.raises(Exception, match="failed|DependencyUnavailable|warehouse") as excinfo:
        source.select(table, where={"run_id": "01X"}, allow_missing=True)
    assert type(excinfo.value).__name__ == "DependencyUnavailable"
    assert ledger and all(session.closed for session in ledger)


def test_a_request_doing_six_reads_opens_six_and_closes_six() -> None:
    """The compounding case: balance, not merely "eventually closed".

    The alert queue path issues several reads per request; leaking one per read is
    what exhausted the pool. Paired counts are the assertion, because a method that
    opened a second session inside the same call would pass a per-method test and
    still drain the pool.
    """
    table = _require_table()
    source, ledger = _source()
    for _ in range(3):
        source.select(table, where={"run_id": "01X"}, allow_missing=True)
        source.rows(table, where={"run_id": "01X"})
    assert len(ledger) == 6, f"expected one session per read, saw {len(ledger)}"
    assert all(session.closed for session in ledger)
    assert sum(not session.closed for session in ledger) == 0


def test_the_provider_is_not_re_read_per_attribute() -> None:
    """`self.session` mints a session per access, so it must not be used twice in one read.

    A method that wrote `self.session.execute(...)` twice would silently hold two
    connections even with every close in place -- the property this test pins by
    counting sessions opened for a single logical read.
    """
    table = _require_table()
    source, ledger = _source()
    source.select(table, where={"run_id": "01X"}, allow_missing=True)
    # one session for the rows, one for the count: the pair is the budget, and it is
    # deliberately not "one per attribute access", which would be unbounded.
    assert len(ledger) <= 2, f"a single read opened {len(ledger)} sessions"
    assert all(session.closed for session in ledger)
