# OXBOW - Makefile
# Every target below is a gate that gets RUN, never asserted. (00 C: a gate is a
# command plus an expected observable.)
# Spec 14 reproducibility contract + 01 D command table.
#
#   make bootstrap   uv sync, bun install, pre-commit install
#   make up          docker compose up: Postgres 16, Redis 7, MinIO, MLflow, Keycloak, echo svc
#   make up-full     the same, plus the application profile: api, worker, web and the caddy edge
#   make ingest|graph|score|backtest   the four pipeline stages, separately resumable
#   make pipeline    all four, streaming stage events over SSE
#   make api|web|dev dev servers
#   make demo        restore data/snapshots/demo.dump, boot the whole stack offline
#   make test|lint   pytest, vitest, playwright / ruff, mypy, biome, import-linter
#   make verify      EVERY gate from every completed phase (00 C step 3)
#   make verify-determinism  run the pipeline twice, diff artifact checksums (01 D)
#   make packet      render a sample case packet to out/packets/
#
# NOTE (DEV-010): 01 D defines `make verify` as the checksum double-run while 00 C
# step 3 uses it as "every gate to date". Rank 1 (00) wins on process, so the two
# meanings are split rather than overloaded onto one name.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PY        := uv run python
OXBOW     := uv run oxbow
COMPOSE   := docker compose
WEB       := apps/web
PIPELINE_STAGES := ingest graph score backtest

export OXBOW_SEED ?= 1337

# RUN_SALT is deliberately NOT defaulted here. It used to be
# `export RUN_SALT ?= $(shell uv run python -c "import secrets;...")`, which minted a
# fresh salt per invocation, re-keyed every account in the corpus on every `make`, and made
# two verify-determinism runs incomparable - the exact failure
# `oxbow.config.resolve_run_salt`'s own docstring names. The salt is an identity: it must
# come from the environment or .env. tests/unit/test_p0_toolchain.py asserts the text below.
# (Commit 8daa148 restored this line by accident while editing this file; the guard test
# caught it, which is the test doing its job.)

# ---------------------------------------------------------------- help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-26s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- bootstrap

.PHONY: bootstrap
bootstrap: ## uv sync, bun install, pre-commit install
	uv python pin 3.12
	uv sync --frozen --all-extras
	cd $(WEB) && bun install --frozen-lockfile
	uv run pre-commit install

.PHONY: sync
sync: ## uv sync without the lockfile check (day-to-day)
	uv sync --all-extras

# ---------------------------------------------------------------- services

.PHONY: up
up: ## docker compose up: Postgres 16, Redis 7, MinIO, MLflow, Keycloak, echo
	$(COMPOSE) up -d --wait
	@echo "--- health ---"
	@$(COMPOSE) ps --format 'table {{.Service}}\t{{.State}}\t{{.Health}}'

.PHONY: up-full
up-full: ## the application profile too: api, worker, web and the caddy edge on :8080
	@echo "--- building and booting the full profile (api image and next build run here) ---"
	COMPOSE_PROFILES=full $(COMPOSE) up -d --wait --build
	@echo "--- health ---"
	@COMPOSE_PROFILES=full $(COMPOSE) ps --format 'table {{.Service}}\t{{.State}}\t{{.Health}}'
	@echo "--- edge ---"
	@echo "http://localhost:$${OXBOW_EDGE_PORT:-8080}  (browser origin; /api/* goes to the API, the rest to the app)"

.PHONY: down
down: ## docker compose down, keep volumes
	$(COMPOSE) down

.PHONY: destroy
destroy: ## docker compose down, DESTROY volumes (fresh database)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail compose logs
	$(COMPOSE) logs -f --tail=100

# ---------------------------------------------------------------- data

.PHONY: data
data: ## Download or verify the corpora, check SHA-256 against the dataset card
	$(PY) scripts/download_data.py --verify

# ---------------------------------------------------------------- pipeline

.PHONY: ingest
ingest: ## Stage 1: read sources, contract-check, canonicalise
	$(OXBOW) ingest

.PHONY: graph
graph: ## Stage 2: build the time-respecting directed multigraph
	$(OXBOW) graph

.PHONY: score
score: ## Stage 3: rules, scorecard, GBM, calibration, fusion
	$(OXBOW) score

.PHONY: backtest
backtest: ## Stage 4: walk-forward with purging and embargo
	$(OXBOW) backtest

.PHONY: pipeline
pipeline: ## All four stages, streaming stage events over SSE
	$(OXBOW) pipeline --stream

.PHONY: eval
eval: ## Regenerate every metric, curve, frontier and ablation row; verifies data/DATASET_CARD.md
	$(OXBOW) eval

.PHONY: packet
packet: ## Render a sample case packet to out/packets/
	$(OXBOW) packet --all

# ---------------------------------------------------------------- app

.PHONY: api
api: ## FastAPI dev server on :8000
	uv run uvicorn main:app --reload --port 8000 --app-dir apps/api

.PHONY: web
web: ## Next.js dev server on :3000
	cd $(WEB) && bun run dev

.PHONY: worker
worker: ## RQ worker for pipeline and backtest jobs
	$(PY) apps/api/worker.py

.PHONY: dev
dev: up ## api :8000 and web :3000 locally, on top of the compose infra (mlflow :5000 comes from `up`)
	@$(MAKE) --no-print-directory -j2 api web

.PHONY: demo
demo: ## Restore data/snapshots/demo.dump and boot the whole stack offline (<90s)
	$(PY) scripts/demo_seed.py --restore --boot-budget 90

# ---------------------------------------------------------------- quality

.PHONY: lint
lint: lint-python lint-web supply-chain contracts ## ruff, mypy, biome, import-linter, image pins

.PHONY: supply-chain
supply-chain: ## 02 §F: every compose image pinned by digest, or excused with a reason
	$(PY) -m pytest -q tests/unit/test_supply_chain_pins.py

.PHONY: lint-python
lint-python:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages/pipeline apps/api scripts

.PHONY: lint-web
lint-web:
	cd $(WEB) && bun run lint

.PHONY: contracts
contracts: ## import-linter: no adapter imports outside adapters/
	uv run lint-imports

.PHONY: typecheck
typecheck: lint-python ## Alias

.PHONY: test
test: test-python test-web ## pytest + vitest

.PHONY: test-python
test-python:
	uv run pytest -q

.PHONY: test-web
test-web:
	cd $(WEB) && bun run test:unit --run

.PHONY: test-e2e
test-e2e: ## Playwright: state gallery, axe sweep, CLS, reduced motion
	cd $(WEB) && bun run test:e2e

.PHONY: audit
audit: ## Supply chain (02 F)
	uv run pip-audit
	cd $(WEB) && bun audit --audit-level=high

# ---------------------------------------------------------------- gates

.PHONY: verify
verify: ## EVERY gate from every completed phase (00 C step 3)
	@$(PY) scripts/verify.py

.PHONY: verify-determinism
verify-determinism: ## Run the pipeline twice and diff artifact checksums (01 D)
	@$(PY) scripts/verify_determinism.py

.PHONY: verify-audit
verify-audit: ## Walk the decision hash chain, print OK or the first broken link
	@$(PY) scripts/verify_audit.py

# ---------------------------------------------------------------- housekeeping

.PHONY: clean
clean: ## Remove build and cache artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache **/__pycache__ out/packets
	find . -name '*.pyc' -delete

.PHONY: db-migrate
db-migrate: ## alembic upgrade head
	uv run alembic -c apps/api/alembic.ini upgrade head
