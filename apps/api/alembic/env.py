"""Alembic environment for the OXBOW warehouse.

Two things are non-obvious and both matter for a schema whose whole purpose is a
trustworthy handoff:

* ``DATABASE_URL`` comes from the environment and is required. An offline-stub
  migration would produce a SQL file nobody ran against Postgres 16, and the first
  person to discover a bad constraint would be the deploy.
* ``target_metadata`` is the warehouse adapter's metadata, not a copy inside
  ``apps/api``. One declaration of the tables means autogenerate diffs against what
  the pipeline actually writes (02 §B seam 1).
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the repo root importable when alembic is invoked from a bare checkout, so
# `import oxbow` resolves even before `uv sync` has installed the project.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from oxbow.adapters.warehouse.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# The env var the API, the worker and the CLI all read. Documented once, here.
DATABASE_URL_ENV = "DATABASE_URL"


def _url() -> str:
    """The migration target URL, or a loud refusal.

    Never a committed default: the local port on this host is not the port in
    someone else's Compose, and a silent fallback turns a misconfigured deploy into
    a migration applied to the wrong database.
    """
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(
            f"{DATABASE_URL_ENV} is not set. Point it at the Compose Postgres, e.g. "
            "postgresql+psycopg://oxbow:<password>@127.0.0.1:5433/oxbow. Migrations are "
            "never run against a default URL, and never against a URL from the repo."
        )
    # Normalised the same way `api.settings.Settings.sqlalchemy_url` normalises it, so the
    # API and `make db-migrate` cannot end up on different drivers for one URL. A bare
    # `postgresql://` selects SQLAlchemy's psycopg2 dialect, which is not installed here
    # (02 F pins psycopg3), and the failure arrives as `ModuleNotFoundError: psycopg2`
    # from inside alembic — a message that names neither the URL nor the fix.
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _configure(url: str) -> MutableMapping[str, object]:
    settings = config.get_section(config.config_ini_section, {})
    settings["sqlalchemy.url"] = url
    return settings


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of applying it. Still binds the real metadata."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=False,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live Postgres 16.

    ``compare_type`` and ``compare_server_default`` are on because the two
    comparisons autogenerate misses are exactly the two this schema cares about: a
    ``bigint`` money column quietly becoming ``integer`` (DEV-005) and a dropped
    server default on an append-only table's sequence.
    """
    connectable = engine_from_config(
        _configure(_url()),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
