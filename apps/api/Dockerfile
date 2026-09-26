# OXBOW API and RQ worker (P7). One image, two commands.
#
# The `api` and `worker` services in docker-compose.yml both build this file with
# `context: .` (the repository root), which is the house convention already recorded in
# Dockerfile.echo: this process imports the pipeline through `oxbow.*`, so the pipeline
# package has to be in the build context. The worker is `python apps/api/worker.py`; the
# API is the CMD below.
#
# Supply chain (02 §F, plan T4): every base image is pinned by tag *and* digest, resolved
# from the registry with `docker buildx imagetools inspect` on 2026-09-26. A bare tag is
# mutable upstream, which makes the reproducibility claim false; a bare digest is unreadable
# in review. tests/unit/test_supply_chain_pins.py enforces the rule on the Compose file, and
# tests/integration/test_p7_worker.py enforces it on these FROM lines.
#
# No secrets and no RUN_SALT are baked in: `docker-compose.yml` supplies RUN_SALT with
# `${RUN_SALT:?}`, which refuses to start rather than defaulting an identity that re-keys
# every account in the corpus (01 §A rule 8).

# uv 0.12.19, used only to turn uv.lock into an installed environment. Copied out as two
# static binaries so nothing from this image reaches the final layer.
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

# 3.12, because pyproject fixes `requires-python = ">=3.12,<3.13"`; the tag currently carries
# CPython 3.12.14, and the digest freezes whatever it carries at pull time.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /srv --shell /usr/sbin/nologin app

WORKDIR /srv

# Install the locked environment into the interpreter's own prefix so `python` and `uvicorn`
# resolve without activating anything, and so a forked RQ horse inherits the same view of the
# world the parent has. Copy, not hardlink: the lock's links would point outside the layer.
ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/srv/apps

COPY --from=uv /uv /usr/local/bin/uv

# The manifests first, and the pipeline package with them: `uv sync --no-install-project`
# resolves and installs the pinned dependency set from uv.lock into a layer that only breaks
# when a requirement changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Only the paths this image imports. There is deliberately no `COPY . .`: the repository has
# no .dockerignore, and `data/` (1.2 GB of corpora, PII-bounded by the ingest rule in 02 §F),
# `out/` (120 MB of artifacts) and `.venv/` would otherwise be baked into an image that ships
# to someone else's cluster. The corpus is runtime input: a service that ingests needs it
# mounted, which the stock Compose file does not do -- see the note at the bottom of this file.
COPY apps/api/ ./apps/api/
COPY packages/pipeline/ ./packages/pipeline/
COPY config/ ./config/

# Installs `oxbow` itself (hatchling maps packages/pipeline/oxbow) on top of the cached env.
RUN uv sync --frozen

# The worker writes its stage artifacts under /srv/out and the pipeline's DuckDB file under
# /srv/data; neither exists in the image, so both have to be creatable by the runtime user.
# Root owns the tree until this point.
RUN mkdir -p /srv/out && chown -R app:app /srv

USER app

EXPOSE 8000

# No HEALTHCHECK here on purpose: this image serves two different commands, and a probe
# against :8000 would report every worker container unhealthy. Dockerfile.echo, which is one
# process with one socket, is where the in-image probe lives; for these two services the
# checks belong to the Compose service that owns the port.
#
# Import style: `api.main:app` with /srv/apps on PYTHONPATH, not `main:app --app-dir
# apps/api`. main.py:32-39 makes the same argument -- one import style across app, worker,
# tests and alembic -- and the worker's dotted name is `api.worker.run_stages`, which only
# resolves if `api` is a package on the path.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
