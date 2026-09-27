"""The analytical tables written into the real schema — not just shaped like it.

The mappers in `oxbow.adapters.warehouse.landing` are unit-tested against synthetic artifacts
(`tests/unit/test_p7_analytical_landing.py`): right rows, right refusals, money as integers. That
proves the *dicts*. It does not prove the dicts survive the table, which is a different claim —
`PostgresWarehouseSink.write` sends each key as a column, so a mapper naming a column the schema
does not have, or overflowing a `CHAR(12)`, fails at the database, mid-transaction, on the run's
first real landing. The `rule_hit` duplicate-key crash this session found is the same class of
gap one level earlier: the shape was right and the key was wrong.

So this file writes the four artifact-shaped tables into a scratch Postgres on the compose port
and reads them back. `backtest_fold` is deliberately absent: it refuses on every artifact this
build has, so there is nothing to write — and `test_a_fold_the_producer_never_describes_refuses_naming_every_missing_column`
is where that is pinned. Refusing is a result; writing a padded row to make a table non-empty is
not.

The fixtures are the unit file's, imported rather than restated: two tables holding two copies
of the same artifact shape is how a schema test and a mapper test drift apart while both stay
green.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# Imported before ``api.*`` (which reaches pyarrow) for the native-load reason in DEV-021.
import osqp  # noqa: E402, F401 -- for the native load it performs, before pyarrow (DEV-021)

from oxbow.adapters.warehouse.landing import (  # noqa: E402
    ablation_rows,
    fairness_rows,
    perturbation_rows,
    validation_metric_rows,
)
from oxbow.adapters.warehouse.models import Base  # noqa: E402
from oxbow.adapters.warehouse.postgres import (  # noqa: E402
    PostgresWarehouseSink,
    new_run_id,
)
from oxbow.ports.warehouse import RunState  # noqa: E402
from tests.unit.test_p7_analytical_landing import (  # noqa: E402
    _CARD,
    _ablation_document,
    _perturbation_card,
)

TABLES = ("ablation_row", "validation_metric", "fairness_row", "perturbation_row")


def _admin_urls() -> list[str]:
    from tests.integration.test_readmodel_page_shape import _candidate_admin_urls

    return _candidate_admin_urls()


@pytest.fixture(scope="module")
def engine() -> Iterator[Any]:
    """A scratch database on the compose Postgres (host port 5433), created and dropped here."""
    # This file's own database name, not the page-shape suite's: two scratch schemas dropped
    # and recreated under one name would each destroy the other's fixtures mid-session.
    import secrets

    import psycopg
    from sqlalchemy import create_engine

    from tests.integration.test_readmodel_page_shape import _redact

    scratch_db = f"oxbow_analytical_landing_{os.getpid()}_{secrets.token_hex(3)}"

    tried: list[str] = []
    last = "no candidate"
    admin = ""
    for candidate in _admin_urls():
        tried.append(_redact(candidate))
        try:
            with psycopg.connect(candidate, autocommit=True, connect_timeout=3) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {scratch_db} WITH (FORCE)")
                conn.execute(f"CREATE DATABASE {scratch_db}")
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            continue
        admin = candidate
        break
    else:
        pytest.skip(
            f"no Postgres on the compose host port (POSTGRES_PORT, default 5433): tried {tried}, "
            f"last failure {last}. Start it with `docker compose up -d postgres`; this file is the "
            "only place the analytical rows are checked against the real column set."
        )
    url = admin.split("//", 1)[0] + "//" + admin.split("//", 1)[1].split("/", 1)[0] + f"/{scratch_db}"
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1), future=True)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        # The scratch schema is this file's, so this file is the only thing allowed to remove it.
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {scratch_db} WITH (FORCE)")


@pytest.fixture()
def tables() -> dict[str, list[dict[str, Any]]]:
    """The four artifact tables, built from the unit file's fixtures with nothing dropped."""
    ablation = _ablation_document()
    rows: dict[str, list[dict[str, Any]]] = {}
    refusals: dict[str, list[str]] = {}
    rows["ablation_row"], refusals["ablation_row"] = ablation_rows(
        _CARD, ablation, declared_resamples=60
    )
    rows["validation_metric"], refusals["validation_metric"] = validation_metric_rows(ablation)
    rows["fairness_row"], refusals["fairness_row"] = fairness_rows(
        {
            "fairness": {
                "protected_attributes_note": "proxy axes, not protected attributes",
                "axes": [
                    {
                        "axis": "amount_band",
                        "available": True,
                        "buckets": [
                            {
                                "bucket": "high",
                                "false_positive_rate": 0.2,
                                "n_accounts": 5
                            },
                        ],
                    },
                ],
            }
        }
    )
    perturbations = _perturbation_card()
    # The unit fixture's third family exists to prove the refusal; this file writes rows, so the
    # unmappable one is removed rather than silently dropped by the mapper.
    perturbations["perturbations"].pop("unmapped_kind")
    rows["perturbation_row"], refusals["perturbation_row"] = perturbation_rows(perturbations)
    # An empty table here means the mapper refused, and the test would pass vacuously.
    for name in TABLES:
        assert rows[name], f"{name} produced no rows from a fixture that describes it: {refusals[name]}"
        assert not refusals[name], refusals[name]
    return rows


def test_the_analytical_rows_survive_the_real_schema(
    engine: Any, tables: dict[str, list[dict[str, Any]]]
) -> None:
    from sqlalchemy.orm import Session

    run_id = new_run_id()
    with Session(engine) as session:
        sink = PostgresWarehouseSink(session)
        sink.open_run(
            run_id,
            seed=1337,
            timezone="Africa/Kampala",
            provenance="pipeline",
            config_hash="0" * 64,
            model_version="p7-analytical-landing-test",
        )
        for name in TABLES:
            written = sink.write(name, run_id, tables[name])
            assert written == len(tables[name]), (
                f"{name}: the sink reported {written} of {len(tables[name])} rows, which is the "
                "shape of a partial write being presented as a complete one"
            )
        session.commit()

        for name in TABLES:
            read_back = sink.read(name, run_id)
            assert len(read_back) == len(tables[name]), (
                f"{name} wrote {len(tables[name])} and the table holds {len(read_back)}: the "
                "page the API serves is shorter than the run that produced it"
            )
        # Money stays an integer count of minor units across the round trip.
        ablated = {str(row["variant"]): row for row in sink.read("ablation_row", run_id)}
        assert ablated["arm_a"]["net_benefit_minor"] == 2500
        assert ablated["arm_b"]["net_benefit_minor"] == -100
        sink.complete_run(run_id, RunState.COMPLETE)
        session.commit()


def test_a_completed_run_takes_no_further_analytical_rows(
    engine: Any, tables: dict[str, list[dict[str, Any]]]
) -> None:
    """The immutability rule is the run's, not the table's — the analytical half included.

    Split from the write test on purpose: if this assertion lived inside it, a mapper that only
    ever wrote to a *fresh* run could pass the round trip and still be silent about the fact that
    a rerun overwriting a published run would be accepted.
    """
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session

    from oxbow.adapters.warehouse.landing import LandingError

    run_id = new_run_id()
    with Session(engine) as session:
        sink = PostgresWarehouseSink(session)
        sink.open_run(
            run_id,
            seed=1337,
            timezone="Africa/Kampala",
            provenance="pipeline",
            config_hash="0" * 64,
            model_version="p7-analytical-landing-test",
        )
        sink.write("validation_metric", run_id, tables["validation_metric"])
        sink.complete_run(run_id, RunState.COMPLETE)
        session.commit()

        session.rollback()
        with pytest.raises((LandingError, SQLAlchemyError, ValueError)) as caught:
            sink.write("validation_metric", run_id, tables["validation_metric"])
            session.commit()
        assert "run" in str(caught.value).lower(), (
            f"refused, but not for the reason that matters: {caught.value}"
        )
