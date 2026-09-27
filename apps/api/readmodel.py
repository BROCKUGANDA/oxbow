"""The API's read side: one seam over the warehouse, two interchangeable backends.

This module exists to make one plan §13 sentence structurally true rather than
repeatedly remembered: **the API never recomputes a score**. Every number a router
serves comes from a row the pipeline wrote, through a method here. There is no
scoring import anywhere in this file, and the one place a number *is* produced on
demand — the policy simulator — goes through the P5 allocator over stored priced
rows, in :mod:`api.policy_engine`, and says so in its response.

Two backends implement the same source interface:

* :class:`PostgresSource` — filter, sort and page in SQL. This is the real path,
  and the alert queue's server-side paging is what lets a virtualised list of a
  few thousand accounts exist at all.
* :class:`FileWarehouseSource` — the null adapter's JSONL/Parquet tables, filtered
  in Python. It exists because plan §13's gate says the null adapters must run with
  nothing else up; it is not a mock, it reads the same shapes the pipeline's null
  warehouse writes.

A table the null warehouse cannot have (decisions, audit, outbox, policy) raises
:class:`WarehouseUnavailable` naming the table and the reason. It never returns an
empty list: an empty list rendered by a UI is an assertion that there was nothing to
find, which is the failure plan §18 lists as fatal.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement

from api.problems import CaseNotFound, DependencyUnavailable, NotFound, RunNotFound
from api.schemas.catalog import Band, RunStateName
from oxbow.adapters.io import read_jsonl, resolve_out_root
from oxbow.adapters.warehouse.models import Base
from oxbow.ports.case_sink import OXBOW_DISCLAIMER
from oxbow.ports.warehouse import RunState, is_ulid

# The alert queue's sortable columns, whitelisted rather than interpolated. An
# unknown ``sort=`` is a 400 naming the allowed set; a silently ignored one would
# page through a differently-ordered list than the client thinks it has.
SORTABLE_ALERT_COLUMNS: Final = frozenset(
    {
        "fused_score",
        "band",
        "exposure_minor",
        "expected_value_minor",
        "rank",
        "last_seen_at",
        "first_seen_at",
        "txn_count",
        "calibrated_probability",
        "account_key",
    }
)
SORTABLE_RUN_COLUMNS: Final = frozenset({"created_at", "run_id", "state", "seed", "provenance"})
BANDS: Final = ("A", "B", "C", "D", "E")

# Tables the read side may query. Broader than ``WAREHOUSE_TABLES`` (which is the
# write vocabulary) because the API also reads the case/decision/outbox ledger.
READABLE_TABLES: Final = frozenset(Base.metadata.tables) - {"alembic_version"}

# What the null warehouse can never provide: rows only the API's own write path
# creates. Named here so the 503 says which table, every time, identically.
POSTGRES_ONLY_TABLES: Final = frozenset(
    {
        "review_case",
        "decision",
        "audit_event",
        "outbox",
        "job_run",
        "policy",
        "policy_summary",
        "policy_simulation",
        "pseudonym_map",
        "erasure_request",
    }
)

_ALERT_SELECT_COLUMNS: Final = (
    "account_key",
    "run_id",
    "band",
    "fused_score",
    "calibrated_probability",
    "observed_rate",
    "calibration_n",
    "calibration_band",
    "predicted_typology",
    "reason_codes",
    "rule_ids",
    "scorecard_points",
    "model_version",
    "exposure_minor",
    "expected_value_minor",
    "currency",
    "rank",
    "selected",
    "beyond_capacity",
    "case_id",
    "case_status",
    "first_seen_at",
    "last_seen_at",
    "txn_count",
)


def _alert_order_by(
    order_column: ColumnElement[Any], tie_column: ColumnElement[Any], order: str
) -> list[ColumnElement[Any]]:
    """One ORDER BY for the queue, whichever way the ranks were produced.

    ``NULLS LAST`` in both directions with ``account_key`` as the tie-break: the same three
    terms :func:`_sort_rows` applies in memory, so a page does not reorder because the run
    happened to write (or not write) ``policy_allocation`` rows. Postgres puts NULLs first
    when descending, which renders "not priced" as "most urgent".
    """
    term = order_column.asc().nulls_last() if order != "desc" else order_column.desc().nulls_last()
    return [term, tie_column.asc()]


def _alert_queue_clauses(
    score: Any,
    economics: Any,
    *,
    run_id: str,
    bands: Sequence[str] | None,
    typology: str | None,
    rule_id: str | None,
    account_key: str | None,
    min_exposure_minor: int | None,
    max_exposure_minor: int | None,
) -> tuple[list[ColumnElement[bool]], bool]:
    """The queue's shared WHERE terms, and whether any of them narrows past the run.

    One builder for both rank sources on purpose: the page boundary, the filter and the
    total have to come from the same predicate list, or ``side_of_cutoff`` and ``band=``
    can disagree with the ``total`` the pager is drawing its last page from.
    """
    clauses: list[ColumnElement[bool]] = [score.c.run_id == run_id]
    narrowed = False
    if bands:
        clauses.append(score.c.band.in_(list(bands)))
        narrowed = True
    if typology:
        clauses.append(score.c.predicted_typology == typology)
        narrowed = True
    if account_key:
        clauses.append(score.c.account_key == account_key)
        narrowed = True
    if rule_id:
        clauses.append(score.c.rule_ids.contains([rule_id]))
        narrowed = True
    if min_exposure_minor is not None:
        clauses.append(economics.c.exposure_minor >= min_exposure_minor)
        narrowed = True
    if max_exposure_minor is not None:
        clauses.append(economics.c.exposure_minor <= max_exposure_minor)
        narrowed = True
    return clauses, narrowed


def _contiguous_rank_order(
    allocations: Mapping[str, Mapping[str, Any]],
) -> list[str] | None:
    """The allocated accounts in rank order, if the map states one at all.

    A live allocation is ``rank = index + 1`` over the allocator's candidate order, so its
    ranks are a contiguous 1..N and the *position in that sequence* is the page boundary —
    which is what lets the statement fetch one page instead of the run. Anything else (a
    repeated rank, a gap, a non-integer) returns ``None`` and the caller ranks the keys it
    read instead of assuming an order the map does not carry.
    """
    positions: list[str | None] = [None] * len(allocations)
    for key, entry in allocations.items():
        rank = entry.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int):
            return None
        if rank < 1 or rank > len(positions) or positions[rank - 1] is not None:
            return None
        positions[rank - 1] = str(key)
    if any(position is None for position in positions):
        return None
    return [str(position) for position in positions]


def _rank_window(
    keys: Sequence[str],
    allocations: Mapping[str, Mapping[str, Any]],
    side_of_cutoff: str | None,
    order: str,
) -> list[str]:
    """Queue keys in the order ``sort=rank`` serves them, unranked accounts last either way.

    The same shape :func:`_sort_rows` produces — present values ordered by
    ``(rank, account_key)``, absent ones appended after them in key order — because the
    page boundary and the order must be decided by one rule or a deep page repeats a row.
    """
    present: list[tuple[Any, str]] = []
    missing: list[str] = []
    for key in keys:
        entry = allocations.get(key)
        if entry is None:
            if side_of_cutoff == "above":
                continue
            missing.append(key)
            continue
        selected = bool(entry.get("selected", False))
        if side_of_cutoff == "above" and not selected:
            continue
        if side_of_cutoff == "below" and selected:
            continue
        rank = entry.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int):
            # A map entry without a usable position is an unranked account: it goes last
            # rather than being compared against integers, which is what _sort_rows does.
            if side_of_cutoff != "above":
                missing.append(key)
            continue
        present.append((rank, key))
    present.sort(key=lambda item: (item[0], item[1]), reverse=order == "desc")
    missing.sort()
    return [key for _, key in present] + missing


class WarehouseUnavailable(DependencyUnavailable):
    """The configured backend cannot answer this query, and says which table is missing."""


def money(minor: int | None, currency: str | None, *, decimals: int = 2) -> dict[str, Any]:
    """Minor units plus currency, always together, never a float.

    ``minor`` is passed straight through: a null money figure is a bug upstream, and
    turning it into ``0`` here would be the fabrication this function exists to
    prevent, so it raises instead.
    """
    if minor is None or currency is None:
        raise DependencyUnavailable(
            f"a money field arrived as minor={minor!r} currency={currency!r}; refusing to "
            "render an absent amount as zero (03 §A: an absent value is not a measured one)"
        )
    return {"minor": int(minor), "currency": str(currency), "decimals": decimals}


def _jsonable(value: Any) -> Any:
    """Row values as JSON, with datetimes and Decimals handled explicitly."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _jsonable(value) for key, value in dict(row).items()}


class Gt:
    """A ``column > value`` predicate for ``select(where=...)``.

    An equality mapping cannot express "strictly after this id", and the SSE cursor needs
    it in the *statement*: filtering ``id > last`` in Python after an unbounded read means
    every poll of a long run re-fetches the whole stage ledger to serve the two rows that
    arrived since the last one.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Gt({self.value!r})"


def _where_clause(table: Any, where: Mapping[str, Any] | None) -> list[ColumnElement[bool]]:
    """Translate a mapping of column -> value (or tuple of values, or ``Gt``) into SQL."""
    if not where:
        return []
    clauses: list[ColumnElement[bool]] = []
    for column, value in where.items():
        target = table.c[column]
        if isinstance(value, Gt):
            clauses.append(target > value.value)
        elif value is None:
            clauses.append(target.is_(None))
        elif isinstance(value, tuple | list) and value:
            clauses.append(target.in_(list(value)))
        else:
            clauses.append(target == value)
    return clauses


class WarehouseSource(ABC):
    """The query primitives both backends must offer."""

    backend: Final = "abstract"

    @property
    @abstractmethod
    def name(self) -> str:
        """``postgres`` or ``null-file``, echoed into every response's meta."""

    @abstractmethod
    def select(
        self,
        table: str,
        *,
        where: Mapping[str, Any] | None = None,
        columns: Sequence[str] | None = None,
        order: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
        allow_missing: bool = False,
        with_count: bool = True,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Rows, plus the total matching ``where`` before paging — only if asked for.

        ``with_count`` defaults to ``True`` because the routers that page already rely on
        the second element for ``meta.total``. A caller that discards it must pass
        ``False``: the aggregate is a second round trip and a second set of locks, and
        :meth:`PostgresSource.select` used to run one on every read, including the forty
        per alert-queue request that threw the answer away. When it is off, ``total`` is
        ``None`` rather than a length: the number of rows *fetched* is not the number of
        rows that *match*, and the second is the only thing this field has ever meant.
        """

    @abstractmethod
    def count(self, table: str, where: Mapping[str, Any] | None = None) -> int:
        """How many rows match ``where``. One statement; no rows are fetched to find out."""

    @abstractmethod
    def alert_rows(
        self,
        *,
        run_id: str,
        bands: Sequence[str] | None = None,
        typology: str | None = None,
        rule_id: str | None = None,
        account_key: str | None = None,
        min_exposure_minor: int | None = None,
        max_exposure_minor: int | None = None,
        side_of_cutoff: str | None = None,
        sort: str = "rank",
        order: str = "asc",
        limit: int = 50,
        offset: int = 0,
        policy_id: str | None = None,
        allocations: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """The queue, filtered, sorted and paged. ``rank`` may come from a live
        re-allocation when the run wrote no ``policy_allocation`` rows."""

    @abstractmethod
    def edges_for(self, run_id: str, account_keys: Sequence[str]) -> list[dict[str, Any]]:
        """Every graph edge touching any of ``account_keys`` (one hop outward)."""

    def close(self) -> None:
        """Release anything the backend holds. The no-op is deliberate on the file path.

        Explicitly not abstract: no backend here holds a resource that outlives a
        request, so the base implementation is the real one for every subclass.
        """
        return


class PostgresSource(WarehouseSource):
    """SQLAlchemy Core over the pipeline's own metadata. No second schema declaration."""

    def __init__(self, session_provider: Any) -> None:
        self._session_provider = session_provider

    @property
    def name(self) -> str:
        return "postgres"

    @property
    def session(self) -> Session:
        return self._session_provider()

    @contextmanager
    def _borrowed_session(self) -> Iterator[Session]:
        """Open one session, hand it out, close it -- including on the error path.

        ``session_provider`` mints a fresh ``Session`` on every call, so a read that
        takes one without returning it holds a connection for the life of the
        process. A request path doing six reads was leaking six.
        """
        session = self._session_provider()
        try:
            yield session
        finally:
            session.close()

    def select(
        self,
        table: str,
        *,
        where: Mapping[str, Any] | None = None,
        columns: Sequence[str] | None = None,
        order: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
        allow_missing: bool = False,
        with_count: bool = True,
    ) -> tuple[list[dict[str, Any]], int | None]:
        if table not in READABLE_TABLES:
            raise DependencyUnavailable(f"table {table!r} is not part of the warehouse metadata")
        target = Base.metadata.tables[table]
        clauses = _where_clause(target, where)
        selected = [target.c[column] for column in columns] if columns else None
        statement = select(*selected) if selected else select(target)
        count_statement = select(func.count()).select_from(target)
        if clauses:
            statement = statement.where(*clauses)
            count_statement = count_statement.where(*clauses)
        if order is not None:
            column = target.c[order]
            statement = statement.order_by(column.desc() if descending else column.asc())
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        elif offset:
            statement = statement.offset(offset)
        session = self._session_provider()
        try:
            rows = [
                _row_to_dict(dict(mapping)) for mapping in session.execute(statement).mappings()
            ]
            # The aggregate is the caller's decision, not this method's reflex.
            total: int | None = (
                int(session.execute(count_statement).scalar_one()) if with_count else None
            )
        except Exception as exc:  # a connection or a missing relation is a dependency failure
            raise DependencyUnavailable(f"warehouse read of {table} failed: {exc}") from exc
        finally:
            session.close()
        # With no COUNT there is no `total == 0` to test, and "fetched nothing" is not the
        # same fact as "the table is empty". They do coincide for an unwindowed read, which
        # is the only shape this refusal is allowed to infer an emptiness from.
        emptiness_known = total == 0 if with_count else (limit is None and not offset)
        if not rows and emptiness_known and not allow_missing and not where:
            # A filter matching nothing is a result. A table with no rows in it at all is a
            # schema nobody has written to, and the two must not sound the same: one is an
            # empty list in the UI, the other is a deployment that was never migrated.
            raise DependencyUnavailable(
                f"table {table} holds no rows at all. Check `make db-migrate` and whether the "
                "pipeline has written a run; this is not reported as an empty result set."
            )
        return rows, total

    def count(self, table: str, where: Mapping[str, Any] | None = None) -> int:
        if table not in READABLE_TABLES:
            raise DependencyUnavailable(f"table {table!r} is not part of the warehouse metadata")
        target = Base.metadata.tables[table]
        statement = select(func.count()).select_from(target)
        clauses = _where_clause(target, where)
        if clauses:
            statement = statement.where(*clauses)
        session = self._session_provider()
        try:
            return int(session.execute(statement).scalar_one())
        except Exception as exc:
            raise DependencyUnavailable(f"warehouse count of {table} failed: {exc}") from exc
        finally:
            session.close()

    def rows(self, table: str, *, where: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        rows, _ = self.select(table, where=where, allow_missing=True, with_count=False)
        return rows

    def scalar(self, statement: Any) -> Any:
        with self._borrowed_session() as session:
            return session.execute(statement).scalar()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        """A session on loan. Callers get ``with``, and it closes either way.

        This used to hand out a bare ``Session`` from a factory that mints a new one
        per attribute access, so every caller leaked a connection and the pool kept
        ``ACCESS SHARE`` on the relations it read.
        """
        with self._borrowed_session() as session:
            yield session

    def alert_rows(
        self,
        *,
        run_id: str,
        bands: Sequence[str] | None = None,
        typology: str | None = None,
        rule_id: str | None = None,
        account_key: str | None = None,
        min_exposure_minor: int | None = None,
        max_exposure_minor: int | None = None,
        side_of_cutoff: str | None = None,
        sort: str = "rank",
        order: str = "asc",
        limit: int = 50,
        offset: int = 0,
        policy_id: str | None = None,
        allocations: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Score joined to account, economics, rank and case — one statement.

        The join is here rather than in four round trips because the queue's sort
        column and its page boundaries must be decided by the same expression;
        filtering ``rank`` client-side after a ``fused_score`` sort is exactly the
        disagreement between order and page that this endpoint exists to avoid.
        """
        if sort not in SORTABLE_ALERT_COLUMNS:
            raise DependencyUnavailable(
                f"sort={sort!r} is not sortable; choose one of " f"{sorted(SORTABLE_ALERT_COLUMNS)}"
            )
        if allocations is not None:
            return self._alert_rows_from_allocations(
                run_id=run_id,
                allocations=allocations,
                bands=bands,
                typology=typology,
                rule_id=rule_id,
                account_key=account_key,
                min_exposure_minor=min_exposure_minor,
                max_exposure_minor=max_exposure_minor,
                side_of_cutoff=side_of_cutoff,
                sort=sort,
                order=order,
                limit=limit,
                offset=offset,
            )
        score, account, economics = (
            Base.metadata.tables["score"],
            Base.metadata.tables["account"],
            Base.metadata.tables["economics"],
        )
        allocation, case = (
            Base.metadata.tables["policy_allocation"],
            Base.metadata.tables["review_case"],
        )
        clauses, _narrowed = _alert_queue_clauses(
            score,
            economics,
            run_id=run_id,
            bands=bands,
            typology=typology,
            rule_id=rule_id,
            account_key=account_key,
            min_exposure_minor=min_exposure_minor,
            max_exposure_minor=max_exposure_minor,
        )
        if side_of_cutoff == "above":
            clauses.append(allocation.c.beyond_capacity.is_(False))
        elif side_of_cutoff == "below":
            clauses.append(allocation.c.beyond_capacity.is_(True))
        if policy_id is None:
            raise DependencyUnavailable(
                "reading stored ranks needs a policy_id: policy_allocation rows are per policy, "
                "and joining across every policy would duplicate each account once per policy."
            )
        clauses.append(allocation.c.policy_id == policy_id)

        join = (
            score.join(
                account,
                (account.c.run_id == score.c.run_id)
                & (account.c.account_key == score.c.account_key),
            )
            .join(
                economics,
                (economics.c.run_id == score.c.run_id)
                & (economics.c.account_key == score.c.account_key),
                isouter=True,
            )
            .join(
                allocation,
                (allocation.c.run_id == score.c.run_id)
                & (allocation.c.account_key == score.c.account_key)
                & (allocation.c.policy_id == policy_id),
                isouter=True,
            )
            .join(
                case,
                (case.c.run_id == score.c.run_id) & (case.c.account_key == score.c.account_key),
                isouter=True,
            )
        )
        columns = {
            "fused_score": score.c.fused_score,
            "band": score.c.band,
            "calibrated_probability": score.c.calibrated_probability,
            "exposure_minor": economics.c.exposure_minor,
            "expected_value_minor": economics.c.expected_value_minor,
            "rank": allocation.c.rank,
            "account_key": score.c.account_key,
            "txn_count": account.c.txn_count,
            "first_seen_at": account.c.first_seen_at,
            "last_seen_at": account.c.last_seen_at,
        }
        try:
            order_column = columns[sort]
        except KeyError as exc:
            raise DependencyUnavailable(
                f"sort={sort!r} is whitelisted but has no stored column to order by"
            ) from exc
        projection = [
            score.c.account_key,
            score.c.run_id,
            score.c.band,
            score.c.fused_score,
            score.c.calibrated_probability,
            score.c.observed_rate,
            score.c.calibration_n,
            score.c.calibration_band,
            score.c.predicted_typology,
            score.c.reason_codes,
            score.c.rule_ids,
            score.c.scorecard_points,
            score.c.model_version,
            economics.c.exposure_minor,
            economics.c.expected_value_minor,
            economics.c.currency,
            allocation.c.rank,
            allocation.c.selected,
            allocation.c.beyond_capacity,
            case.c.case_id,
            case.c.status.label("case_status"),
            account.c.first_seen_at,
            account.c.last_seen_at,
            account.c.txn_count,
        ]
        statement = select(*projection).select_from(join).where(*clauses)
        count_statement = select(func.count()).select_from(join).where(*clauses)
        statement = (
            statement.order_by(*_alert_order_by(order_column, score.c.account_key, order))
            .limit(limit)
            .offset(offset)
        )
        session = self._session_provider()
        try:
            rows = [_row_to_dict(dict(m)) for m in session.execute(statement).mappings()]
            total = int(session.execute(count_statement).scalar_one())
        except Exception as exc:
            raise DependencyUnavailable(f"alert queue read failed: {exc}") from exc
        finally:
            session.close()
        return rows, total

    def _alert_rows_from_allocations(
        self,
        *,
        run_id: str,
        allocations: Mapping[str, Mapping[str, Any]],
        bands: Sequence[str] | None,
        typology: str | None,
        rule_id: str | None,
        account_key: str | None,
        min_exposure_minor: int | None,
        max_exposure_minor: int | None,
        side_of_cutoff: str | None,
        sort: str,
        order: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Page in the statement, then stitch the live rank onto the served rows only.

        Used when the run wrote no ``policy_allocation`` rows: the queue still has to show
        *a* rank and *the* cutoff, and the only honest source is the allocator that just
        ran over the stored priced rows. That map is a request's worth of Python, but it is
        a *lookup* here, not a fetch: the joined rows come back with ``LIMIT``/``OFFSET``
        and ``total`` comes from a ``COUNT``, so a page turn costs a page instead of the
        run. It used to read every joined row, sort it in Python and take
        ``total = len(rows)``, which made page 1 and page forty identical work.

        ``rank`` is the one column SQL cannot page by, because it exists only in the live
        map and the allocator's order is a money computation this read side must never
        re-implement. So for ``sort=rank`` the window comes from the map and the statement
        reads exactly those accounts (``account_key IN``), and when the map does not cover
        every joined row — a scored account with no economics row has no rank — or when a
        filter or the cutoff line narrows the queue, the matching keys are read narrow (one
        column) and the map orders them. Either way the wide projection is fetched once per
        page and never once per run.
        """
        score, account, economics = (
            Base.metadata.tables["score"],
            Base.metadata.tables["account"],
            Base.metadata.tables["economics"],
        )
        case = Base.metadata.tables["review_case"]
        clauses, narrowed = _alert_queue_clauses(
            score,
            economics,
            run_id=run_id,
            bands=bands,
            typology=typology,
            rule_id=rule_id,
            account_key=account_key,
            min_exposure_minor=min_exposure_minor,
            max_exposure_minor=max_exposure_minor,
        )
        projection = [
            score.c.account_key,
            score.c.run_id,
            score.c.band,
            score.c.fused_score,
            score.c.calibrated_probability,
            score.c.observed_rate,
            score.c.calibration_n,
            score.c.calibration_band,
            score.c.predicted_typology,
            score.c.reason_codes,
            score.c.rule_ids,
            score.c.scorecard_points,
            score.c.model_version,
            economics.c.exposure_minor,
            economics.c.expected_value_minor,
            economics.c.currency,
            account.c.first_seen_at,
            account.c.last_seen_at,
            account.c.txn_count,
            case.c.case_id,
            case.c.status.label("case_status"),
        ]
        join = (
            score.join(
                account,
                (account.c.run_id == score.c.run_id)
                & (account.c.account_key == score.c.account_key),
            )
            .join(
                economics,
                (economics.c.run_id == score.c.run_id)
                & (economics.c.account_key == score.c.account_key),
                isouter=True,
            )
            .join(
                case,
                (case.c.run_id == score.c.run_id) & (case.c.account_key == score.c.account_key),
                isouter=True,
            )
        )

        if sort == "rank":
            return self._live_rank_page(
                join=join,
                clauses=clauses,
                projection=projection,
                score=score,
                allocations=allocations,
                side_of_cutoff=side_of_cutoff,
                narrowed=narrowed,
                order=order,
                limit=limit,
                offset=offset,
            )

        page_clauses = list(clauses)
        if side_of_cutoff is not None:
            # The cutoff line is the allocator's decision, so the predicate that expresses
            # it is the selected key set — bounded by the capacity budget, not by the run.
            selected = sorted(key for key, entry in allocations.items() if entry["selected"])
            if side_of_cutoff == "above":
                if not selected:
                    return [], 0
                page_clauses.append(score.c.account_key.in_(selected))
            elif selected:
                page_clauses.append(score.c.account_key.notin_(selected))
        try:
            order_column = {
                "fused_score": score.c.fused_score,
                "band": score.c.band,
                "calibrated_probability": score.c.calibrated_probability,
                "exposure_minor": economics.c.exposure_minor,
                "expected_value_minor": economics.c.expected_value_minor,
                "last_seen_at": account.c.last_seen_at,
                "first_seen_at": account.c.first_seen_at,
                "txn_count": account.c.txn_count,
                "account_key": score.c.account_key,
            }[sort]
        except KeyError as exc:
            raise DependencyUnavailable(
                f"sort={sort!r} is whitelisted but has no stored column to order by"
            ) from exc
        statement = (
            select(*projection)
            .select_from(join)
            .where(*page_clauses)
            .order_by(*_alert_order_by(order_column, score.c.account_key, order))
            .limit(limit)
            .offset(offset)
        )
        rows = self._execute(statement, error="alert queue read (live rank) failed")
        total = self._execute_count(
            select(func.count()).select_from(join).where(*page_clauses),
            error="alert queue count (live rank) failed",
        )
        return [self._attach_allocation(row, allocations) for row in rows], total

    def _live_rank_page(
        self,
        *,
        join: Any,
        clauses: list[ColumnElement[bool]],
        projection: list[Any],
        score: Any,
        allocations: Mapping[str, Mapping[str, Any]],
        side_of_cutoff: str | None,
        narrowed: bool,
        order: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """The ``sort=rank`` half of the live queue: the window comes from the map.

        Two statements for the common page (a COUNT for ``total``, one bounded read for the
        rows), three when a filter or the cutoff line means the key set has to be read
        before it can be ranked. The narrow read is a single column; the wide projection is
        never fetched for more rows than the page holds.
        """
        ranked = (
            None if narrowed or side_of_cutoff is not None else _contiguous_rank_order(allocations)
        )
        if ranked is not None:
            total = self._execute_count(
                select(func.count()).select_from(join).where(*clauses),
                error="alert queue count (live rank) failed",
            )
            if total == len(ranked):
                window = ranked[offset : offset + limit]
                if not window:
                    return [], total
                rows = self._queue_window(join, clauses, projection, score, window, allocations)
                if len(rows) == len(window):
                    return rows, total
                # A ranked account that the join does not return: fall through and let the
                # rows the join really has decide the window, rather than serving a short
                # page and a total that cannot be reached.
        keys = self._queue_keys(join, clauses, score)
        ordered = _rank_window(keys, allocations, side_of_cutoff, order)
        total = len(ordered)
        window = ordered[offset : offset + limit]
        if not window:
            return [], total
        return self._queue_window(join, clauses, projection, score, window, allocations), total

    def _queue_keys(self, join: Any, clauses: list[ColumnElement[bool]], score: Any) -> list[str]:
        """Every account the queue's filter matches, as one column, in key order."""
        rows = self._execute(
            select(score.c.account_key)
            .select_from(join)
            .where(*clauses)
            .order_by(score.c.account_key.asc()),
            error="alert queue key read (live rank) failed",
        )
        return [str(row["account_key"]) for row in rows]

    def _queue_window(
        self,
        join: Any,
        clauses: list[ColumnElement[bool]],
        projection: list[Any],
        score: Any,
        window: Sequence[str],
        allocations: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """The page's wide rows, fetched for the window's keys and returned in that order.

        The live rank is stitched on here, onto at most ``limit`` rows: that is the whole
        point of deciding the window before the fetch rather than after it.
        """
        rows = self._execute(
            select(*projection)
            .select_from(join)
            .where(*clauses, score.c.account_key.in_(list(window))),
            error="alert queue read (live rank) failed",
        )
        by_key = {str(row["account_key"]): row for row in rows}
        return [
            self._attach_allocation(by_key[key], allocations) for key in window if key in by_key
        ]

    @staticmethod
    def _attach_allocation(
        row: dict[str, Any], allocations: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Rank, selected and beyond_capacity from the live map, onto one served row.

        An account the map does not hold was scored but not priced: it has no position, so
        it is reported as such rather than as rank 0 or as the most urgent row in the queue.
        """
        position = allocations.get(str(row["account_key"]))
        row["rank"] = None if position is None else position["rank"]
        row["selected"] = False if position is None else position["selected"]
        row["beyond_capacity"] = True if position is None else position["beyond_capacity"]
        return row

    def _execute(self, statement: Any, *, error: str) -> list[dict[str, Any]]:
        session = self._session_provider()
        try:
            return [
                _row_to_dict(dict(mapping)) for mapping in session.execute(statement).mappings()
            ]
        except Exception as exc:
            raise DependencyUnavailable(f"{error}: {exc}") from exc
        finally:
            session.close()

    def _execute_count(self, statement: Any, *, error: str) -> int:
        session = self._session_provider()
        try:
            return int(session.execute(statement).scalar_one())
        except Exception as exc:
            raise DependencyUnavailable(f"{error}: {exc}") from exc
        finally:
            session.close()

    def edges_for(self, run_id: str, account_keys: Sequence[str]) -> list[dict[str, Any]]:
        if not account_keys:
            return []
        edges = Base.metadata.tables["graph_edge"]
        statement = (
            select(edges)
            .where(
                edges.c.run_id == run_id,
                edges.c.src_account_key.in_(list(account_keys))
                | edges.c.dst_account_key.in_(list(account_keys)),
            )
            .order_by(edges.c.total_minor.desc())
        )
        session = self._session_provider()
        try:
            return [_row_to_dict(dict(m)) for m in session.execute(statement).mappings()]
        except Exception as exc:
            raise DependencyUnavailable(f"graph edge read failed: {exc}") from exc
        finally:
            session.close()


def _sort_key(row: Mapping[str, Any], sort: str) -> Any:
    """A comparable key for one column, with the types this schema actually holds.

    ``None`` is extracted rather than coerced: an unranked account has no position,
    and sorting ``None`` as ``0`` would present "not priced" as "most urgent".
    """
    value = row.get(sort)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, list):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _sort_rows(rows: Sequence[Mapping[str, Any]], sort: str, order: str) -> list[dict[str, Any]]:
    """Sort by ``sort``, then by ``account_key``, keeping absent values last either way.

    Both halves of this matter for a paged list: without the tie-break two rows with
    the same score swap between pages, and with a plain ``reverse=True`` the absent
    values would jump to the front, which is a wrong answer rendered as an urgent one.
    """
    present = [row for row in rows if _sort_key(row, sort) is not None]
    missing = [row for row in rows if _sort_key(row, sort) is None]
    ordered = sorted(
        present,
        key=lambda row: (_sort_key(row, sort), str(row.get("account_key") or "")),
        reverse=order == "desc",
    )
    ordered.extend(sorted(missing, key=lambda row: str(row.get("account_key") or "")))
    return [dict(row) for row in ordered]


class FileWarehouseSource(WarehouseSource):
    """The null warehouse as a read source: same tables, filtered in Python.

    Every method here refuses ``POSTGRES_ONLY_TABLES`` loudly instead of returning
    nothing. The distinction the UI depends on — "this deployment has no decision
    store" versus "this case has no decisions" — only survives if the first is an
    error rather than an empty list.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = resolve_out_root(root) / "warehouse"
        # (table, run scope) -> (file signature, rows). The signature is what re-measuring
        # costs microseconds of `stat` against a parquet parse, and what ignoring costs the
        # demo: a cache without one is what froze the stream surface, because the SSE poll
        # reads `stage_event` once a second, the first read populated this dict, and every
        # read after it was answered from memory while the pipeline went on writing the very
        # file being cached. A run that was measurably advancing looked dead.
        self._cache: dict[
            tuple[str, str], tuple[tuple[tuple[str, int, int], ...], list[dict[str, Any]]]
        ] = {}

    def _signature(self, table: str, run_id: str | None) -> tuple[tuple[str, int, int], ...]:
        """What the rows below were read from, re-measured without reading them.

        Every file whose name starts `run=` counts, not just the one parsed: the aggregate
        scope's job is to notice that a run nobody had asked for yet has appeared, and that is
        a change in the directory, not in a file already listed. Absent files contribute
        nothing, so a file arriving is a signature change all by itself.
        """
        if table == "run":
            candidates = [self._root / "runs.jsonl"]
        elif run_id is None:
            directory = self._root / table
            candidates = sorted(directory.glob("run=*")) if directory.is_dir() else []
        else:
            directory = self._root / table
            candidates = [directory / f"run={run_id}.jsonl", directory / f"run={run_id}.parquet"]
        signed: list[tuple[str, int, int]] = []
        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            signed.append((path.name, stat.st_size, stat.st_mtime_ns))
        return tuple(signed)

    @property
    def name(self) -> str:
        return "null-file"

    def _table_rows(self, table: str, run_id: str | None) -> list[dict[str, Any]]:
        if table in POSTGRES_ONLY_TABLES:
            raise WarehouseUnavailable(
                f"{table} does not exist in the null-file warehouse: it is written by the API's "
                "own decision transaction, which needs Postgres. This deployment is read-only."
            )
        key = (table, run_id or "*")
        signature = self._signature(table, run_id)
        cached = self._cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        if table == "run":
            directory = self._root
            records = read_jsonl(directory / "runs.jsonl")
        elif run_id is None:
            records = []
            for manifest in sorted((self._root / table).glob("run=*.manifest.json")):
                part = manifest.name.split(".")[0]
                records.extend(self._table_rows(table, part.removeprefix("run=")))
        else:
            directory = self._root / table
            jsonl = directory / f"run={run_id}.jsonl"
            if jsonl.is_file():
                records = read_jsonl(jsonl)
            else:
                parquet = directory / f"run={run_id}.parquet"
                if not parquet.is_file():
                    records = []
                else:
                    import pyarrow.parquet as pq

                    records = [
                        {k: _jsonable(v) for k, v in dict(row).items()}
                        for row in pq.read_table(parquet).to_pylist()
                    ]
        rows = [
            {key: _jsonable(value) for key, value in dict(record).items()} for record in records
        ]
        self._cache[key] = (signature, rows)
        return rows

    def select(
        self,
        table: str,
        *,
        where: Mapping[str, Any] | None = None,
        columns: Sequence[str] | None = None,
        order: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
        allow_missing: bool = False,
        with_count: bool = True,
    ) -> tuple[list[dict[str, Any]], int | None]:
        rows = self._table_rows(table, None if not where else _run_id_of(where))
        matched = [row for row in rows if _matches(row, where)]
        if order is not None:
            matched = _sort_rows(matched, order, "desc" if descending else "asc")
        # The same contract as the Postgres source: the total is the caller's choice, and
        # the file tables are already fully read, so counting them is free either way.
        total: int | None = len(matched) if with_count else None
        window = matched[offset:] if limit is None else matched[offset : offset + limit]
        if columns:
            window = [{key: row[key] for key in columns if key in row} for row in window]
        return window, total

    def count(self, table: str, where: Mapping[str, Any] | None = None) -> int:
        rows = self._table_rows(table, None if not where else _run_id_of(where))
        return len([row for row in rows if _matches(row, where)])

    def alert_rows(
        self,
        *,
        run_id: str,
        bands: Sequence[str] | None = None,
        typology: str | None = None,
        rule_id: str | None = None,
        account_key: str | None = None,
        min_exposure_minor: int | None = None,
        max_exposure_minor: int | None = None,
        side_of_cutoff: str | None = None,
        sort: str = "rank",
        order: str = "asc",
        limit: int = 50,
        offset: int = 0,
        policy_id: str | None = None,
        allocations: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        scores, _ = self.select(
            "score", where={"run_id": run_id}, allow_missing=True, with_count=False
        )
        accounts, _ = self.select(
            "account", where={"run_id": run_id}, allow_missing=True, with_count=False
        )
        economics, _ = self.select(
            "economics", where={"run_id": run_id}, allow_missing=True, with_count=False
        )
        stored_allocations: Mapping[str, Mapping[str, Any]] = {}
        if allocations is None:
            rows, total = self.select(
                "policy_allocation",
                where={"run_id": run_id}
                if policy_id is None
                else {"run_id": run_id, "policy_id": policy_id},
                allow_missing=True,
            )
            if total:
                stored_allocations = {
                    str(row["account_key"]): {
                        "rank": row["rank"],
                        "selected": bool(row["selected"]),
                        "beyond_capacity": bool(row["beyond_capacity"]),
                    }
                    for row in rows
                }
        else:
            stored_allocations = allocations
        by_account = {str(row["account_key"]): row for row in accounts}
        by_economics = {str(row["account_key"]): row for row in economics}
        joined: list[dict[str, Any]] = []
        for score in scores:
            key = str(score["account_key"])
            if bands and score.get("band") not in bands:
                continue
            if typology and score.get("predicted_typology") != typology:
                continue
            if account_key and key != account_key:
                continue
            if rule_id and rule_id not in (score.get("rule_ids") or []):
                continue
            economic = by_economics.get(key) or {}
            if min_exposure_minor is not None and int(economic.get("exposure_minor", 0)) < (
                min_exposure_minor
            ):
                continue
            if max_exposure_minor is not None and int(economic.get("exposure_minor", 0)) > (
                max_exposure_minor
            ):
                continue
            position = stored_allocations.get(key) or {}
            row = {
                "account_key": key,
                "run_id": score["run_id"],
                "band": score.get("band"),
                "fused_score": score.get("fused_score"),
                "calibrated_probability": score.get("calibrated_probability"),
                "observed_rate": score.get("observed_rate"),
                "calibration_n": score.get("calibration_n"),
                "calibration_band": score.get("calibration_band"),
                "predicted_typology": score.get("predicted_typology"),
                "reason_codes": score.get("reason_codes") or [],
                "rule_ids": score.get("rule_ids") or [],
                "scorecard_points": score.get("scorecard_points"),
                "model_version": score.get("model_version"),
                "exposure_minor": economic.get("exposure_minor"),
                "expected_value_minor": economic.get("expected_value_minor"),
                "currency": economic.get("currency"),
                "rank": position.get("rank"),
                "selected": bool(position.get("selected", False)),
                "beyond_capacity": bool(position.get("beyond_capacity", True)),
                "case_id": None,
                "case_status": None,
                "first_seen_at": (by_account.get(key) or {}).get("first_seen_at"),
                "last_seen_at": (by_account.get(key) or {}).get("last_seen_at"),
                "txn_count": (by_account.get(key) or {}).get("txn_count"),
            }
            if side_of_cutoff == "above" and not row["selected"]:
                continue
            if side_of_cutoff == "below" and row["selected"]:
                continue
            joined.append(row)
        joined = _sort_rows(joined, sort, order)
        return joined[offset : offset + limit], len(joined)

    def edges_for(self, run_id: str, account_keys: Sequence[str]) -> list[dict[str, Any]]:
        if not account_keys:
            return []
        wanted = set(account_keys)
        rows, _ = self.select(
            "graph_edge", where={"run_id": run_id}, allow_missing=True, with_count=False
        )
        touched = [
            row
            for row in rows
            if row.get("src_account_key") in wanted or row.get("dst_account_key") in wanted
        ]
        touched.sort(key=lambda row: -int(row.get("total_minor") or 0))
        return touched


def _run_id_of(where: Mapping[str, Any] | None) -> str | None:
    value = (where or {}).get("run_id")
    return None if value is None else str(value)


def _matches(row: Mapping[str, Any], where: Mapping[str, Any] | None) -> bool:
    for column, value in (where or {}).items():
        cell = row.get(column)
        if isinstance(value, list | tuple):
            if cell not in list(value):
                return False
        elif isinstance(value, Gt):
            # The file source has to answer the predicate the SQL source answers at :292, or a
            # `Gt` falls through to the equality below, compares a `Gt` object to an integer,
            # matches nothing, and the SSE stream on the null-file deployment — the one
            # `make demo` and the offline boot run on — serves an empty ledger for a run that
            # is writing rows. NULL > 0 is NULL in SQL, so a missing cell never matches here.
            if cell is None or _jsonable(cell) <= _jsonable(value.value):
                return False
        elif _jsonable(cell) != _jsonable(value):
            return False
    return True


class ReadModel:
    """Domain queries every router calls. One place that knows the column names.

    The methods return response-ready dictionaries, not ORM objects: a router that
    builds its own query cannot be checked against the read model, and the read model
    is the boundary that keeps "the API never recomputes a score" auditable.
    """

    def __init__(self, source: WarehouseSource, *, money_decimals: int = 2) -> None:
        self.source = source
        self.money_decimals = money_decimals

    # --- runs ---------------------------------------------------------------

    def run_row(self, run_id: str) -> dict[str, Any]:
        if not is_ulid(run_id):
            raise RunNotFound(
                f"{run_id!r} is not a 26-character ULID, so no run can have been opened under "
                "it (DEV-003)."
            )
        rows, _ = self.source.select("run", where={"run_id": run_id}, limit=1, with_count=False)
        if not rows:
            raise RunNotFound(
                f"no run {run_id} in the warehouse. A run id is printed on every response and "
                "every error, so a mistyped one is the usual cause."
            )
        return rows[0]

    def resolve_run(self, run_id: str | None, *, state: str | None = "complete") -> dict[str, Any]:
        """The named run, or the newest one in ``state`` when none was given.

        Defaulting to the newest *complete* run matters: a run in ``running`` has
        partially-written tables, and serving its score list would let a dashboard
        show a queue that is still being built without saying so.

        No COUNT is asked for: one row is fetched either way, and this runs on the front
        of every list request that does not name a run.
        """
        if run_id is not None:
            return self.run_row(run_id)
        rows, _ = self.source.select(
            "run",
            where=None if state is None else {"state": state},
            order="run_id",
            descending=True,
            limit=1,
            with_count=False,
        )
        if not rows:
            raise RunNotFound(
                f"there is no run in state {state!r} to default to. Name a run explicitly, "
                "or run the pipeline: the queue cannot invent one."
            )
        return rows[0]

    def list_runs(
        self, *, state: str | None, limit: int, offset: int, sort: str, order: str
    ) -> tuple[list[dict[str, Any]], int]:
        if sort not in SORTABLE_RUN_COLUMNS:
            raise DependencyUnavailable(
                f"sort={sort!r} is not sortable on runs; one of {sorted(SORTABLE_RUN_COLUMNS)}"
            )
        rows, total = self.source.select(
            "run",
            where=None if state is None else {"state": state},
            order=sort,
            descending=order == "desc",
            limit=limit,
            offset=offset,
        )
        return [self.run_summary(row) for row in rows], total

    def run_summary(self, row: Mapping[str, Any]) -> dict[str, Any]:
        run_id = str(row["run_id"])
        return {
            "run_id": run_id,
            "created_at": row["created_at"],
            "finished_at": row.get("finished_at"),
            "state": row["state"],
            "seed": row["seed"],
            "timezone": row["timezone"],
            "provenance": row["provenance"],
            "model_version": row["model_version"],
            "config_hash": row["config_hash"],
            "dataset_ref": row.get("dataset_ref"),
            "error": row.get("error"),
            "superseded_by": row.get("superseded_by"),
            "notes": row.get("notes"),
            "artifact_hashes": row.get("artifact_hashes") or {},
            "account_count": self.count("account", {"run_id": run_id}),
            "scored_count": self.count("score", {"run_id": run_id}),
            "alert_count": self.count("score", {"run_id": run_id, "band": ("D", "E")}),
            "quarantine_count": self.count("quarantine_row", {"run_id": run_id}),
        }

    def count(self, table: str, where: Mapping[str, Any]) -> int:
        # One aggregate, no row fetch. This used to run `select(..., limit=0)` and read the
        # total off its second element, which cost a round trip per number as well as the
        # count itself — four of them per run row on /api/runs.
        return self.source.count(table, where)

    def stage_events(
        self, run_id: str, *, after_id: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """The stored ledger after ``after_id``, in id order.

        ``limit`` bounds the read; ``None`` means the whole remainder, which is what the
        non-streaming ledger route serves.
        """
        self.run_row(run_id)
        return self.stage_event_rows(run_id, after_id=after_id, limit=limit)

    def stage_event_rows(
        self, run_id: str, *, after_id: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """The same read without the existence check, for a caller that just did one.

        The cursor goes into the statement (``id > after_id``) rather than a Python filter
        after an unbounded read: a poll of the SSE stream used to re-fetch a run's entire
        ledger every second to serve the two rows that arrived since the last one, and it
        asked whether the run existed twice while doing it.
        """
        rows, _ = self.source.select(
            "stage_event",
            where={"run_id": run_id, "id": Gt(after_id)},
            order="id",
            limit=limit,
            with_count=False,
        )
        return rows

    def latest_stage_id(self, run_id: str) -> int | None:
        rows, _ = self.source.select(
            "stage_event",
            where={"run_id": run_id},
            order="id",
            descending=True,
            limit=1,
            with_count=False,
        )
        return int(rows[0]["id"]) if rows else None

    # --- cases --------------------------------------------------------------

    def score_row(self, run_id: str, account_key: str) -> dict[str, Any]:
        rows, _ = self.source.select(
            "score",
            where={"run_id": run_id, "account_key": account_key},
            limit=1,
            with_count=False,
        )
        if not rows:
            raise NotFound(
                f"account {account_key} has no stored score in run {run_id}. The API reads "
                "scores and never derives one, so an unscored account has no answer to give.",
                run_id=run_id,
            )
        return rows[0]

    def optional_row(self, table: str, where: Mapping[str, Any]) -> dict[str, Any] | None:
        rows, _ = self.source.select(
            table, where=where, limit=1, allow_missing=True, with_count=False
        )
        return rows[0] if rows else None

    def rows(self, table: str, where: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows, _ = self.source.select(table, where=where, allow_missing=True, with_count=False)
        return rows

    def paged(
        self,
        table: str,
        where: Mapping[str, Any],
        *,
        order: str,
        descending: bool = False,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        return self.source.select(
            table,
            where=where,
            order=order,
            descending=descending,
            limit=limit,
            offset=offset,
            allow_missing=True,
        )

    def money(self, minor: int | None, currency: str | None) -> dict[str, Any]:
        return money(minor, currency, decimals=self.money_decimals)

    # --- alerts -------------------------------------------------------------

    def alert_page(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        return self.source.alert_rows(**kwargs)

    # --- graph --------------------------------------------------------------

    def subgraph(
        self, run_id: str, account_key: str, *, hops: int, min_amount_minor: int | None
    ) -> list[dict[str, Any]]:
        """Breadth-first over stored ``graph_edge`` rows, ``hops`` deep.

        The traversal walks stored edges only: no edge is inferred from a shared
        counterparty or a timestamp, because an edge the pipeline did not write is a
        claim about the corpus that nothing measured.
        """
        frontier = {account_key}
        seen = {account_key}
        edges: list[dict[str, Any]] = []
        for _hop_index in range(max(hops, 0)):
            if not frontier:
                break
            found = self.source.edges_for(run_id, sorted(frontier))
            nxt: set[str] = set()
            for edge in found:
                if min_amount_minor is not None and int(edge["total_minor"]) < min_amount_minor:
                    continue
                if edge not in edges:
                    edges.append(edge)
                for side in ("src_account_key", "dst_account_key"):
                    key = str(edge[side])
                    if key not in seen:
                        seen.add(key)
                        nxt.add(key)
            frontier = nxt
        return edges

    # --- meta helpers -------------------------------------------------------

    def assumptions(self, economics: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The assumption lines every money figure on this response depends on.

        Flattened from ``config/economics.yaml`` verbatim, each naming the file it
        came from, because plan §16 requires a visible assumption line that traces to
        a config file rather than a number in a component.
        """
        lines: list[dict[str, Any]] = []

        def walk(node: Mapping[str, Any], prefix: str) -> None:
            for key, value in node.items():
                path = f"{prefix}{key}"
                if isinstance(value, Mapping):
                    walk(value, f"{path}.")
                elif isinstance(value, list):
                    lines.append(
                        {
                            "key": path,
                            "value": ", ".join(str(item) for item in value),
                            "source": "config/economics.yaml",
                        }
                    )
                else:
                    lines.append({"key": path, "value": value, "source": "config/economics.yaml"})

        walk(dict(economics), "")
        return lines

    @staticmethod
    def disclaimer() -> str:
        return OXBOW_DISCLAIMER

    def run_states(self) -> list[str]:
        return [state.value for state in RunState]


def band_value(value: Any) -> Band:
    """Narrow a stored band letter to the response enum, or refuse.

    A CHECK constraint already restricts the column, so arriving here with anything
    else means a row was written outside the ORM. Saying so beats casting it.
    """
    text_value = str(value)
    if text_value not in BANDS:
        raise DependencyUnavailable(
            f"stored band {text_value!r} is not one of {BANDS}; the schema's CHECK constraint "
            "was bypassed and the queue cannot be trusted to render it"
        )
    return text_value  # type: ignore[return-value]


def state_value(value: Any) -> RunStateName:
    text_value = str(value)
    allowed: tuple[str, ...] = ("running", "complete", "failed", "superseded")
    if text_value not in allowed:
        raise DependencyUnavailable(f"stored run state {text_value!r} is not one of {allowed}")
    return text_value  # type: ignore[return-value]


def utcnow() -> datetime:
    return datetime.now(UTC)


def is_run_id(value: str) -> bool:
    return is_ulid(value)


def engine_dialect(engine: Engine) -> str:
    """Used by the health probe to name the driver actually connected."""
    return engine.dialect.name


__all__ = [
    "BANDS",
    "POSTGRES_ONLY_TABLES",
    "READABLE_TABLES",
    "SORTABLE_ALERT_COLUMNS",
    "CaseNotFound",
    "FileWarehouseSource",
    "PostgresSource",
    "ReadModel",
    "WarehouseSource",
    "WarehouseUnavailable",
    "band_value",
    "engine_dialect",
    "money",
    "state_value",
    "utcnow",
]
