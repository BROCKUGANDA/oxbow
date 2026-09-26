# 01 — Repository Inventory (measured, not assumed)

Static inspection of `C:\Users\HP\Desktop\OXBOW`. Numbers are real values read from
disk at the times stated. No file was moved, edited, deleted or staged; the only
file this exercise created is this one.

## 0. Provenance and counting rules

| Item | Value |
| --- | --- |
| HEAD at **start** of work | `1dc2499` — `docs(state): record the measured position and the next five moves in order` |
| HEAD at **end** of work | `1dc2499` (unchanged) |
| HEAD named in the task brief | `2e8b4b2` — **already superseded** when work began; three commits landed on top of it (`0e8a5b8`, `1dc2499`) |
| Tracked files (`git ls-files`) | 386 |
| Working-tree changes in flight at end | 1 (`docs/audit/02-code-review.md`, written by another agent) |
| Prescribed tree | `C:\Users\HP\Downloads\2026-09-26_021500-oxbow-master-build-plan.md` §5, task **T1**, lines 125–143 |

**Counting set.** The brief's ignore list was applied verbatim: `.git`, `.venv`,
`node_modules`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, `htmlcov`,
`data/raw/`, `out/`. The resulting set is **6,044 files / 805,592,695 bytes
(768.0 MiB)**.

**Three deliberate deviations, all disclosed:**

1. `.mypy_cache/` (5,237 files / 141,616,177 B), `.import_linter_cache/`
   (5 / 90,820 B) and `.hypothesis/` (1 / 21,726 B) are **not** on the brief's
   ignore list, so they are counted. They are caches; §4 shows one of them is
   *committed*.
2. `apps/web/.next/` (340 files / 348,323,893 B) and
   `apps/web/tsconfig.tsbuildinfo` (167,675 B) are build output, also not on the
   ignore list, so they are counted. Both are correctly gitignored.
3. `data/interim/` and `data/processed/` are counted (only `data/raw/` was
   excluded).

**Paths read but never written:** `packages/pipeline/oxbow/features/bridge.py`,
`tests/unit/test_p2_grain_bridge.py`, and everything under `docs/audit/`.

**No test suite was run.** No `uv`/`python` command was executed. All findings
below come from `Get-ChildItem`, `git ls-files`, `git check-ignore`,
`Select-String`, `Get-FileHash` and direct file reads.

---

## 1. Inventory table

### 1.1 Top-level entries (counting set, n = 6,044)

| Entry | Kind | Files | Bytes | File-type breakdown |
| --- | --- | ---: | ---: | --- |
| `apps/` | dir | 469 | 349,800,019 | `.json` 5,352 · `.js` 208 · `.ts` 40 · `.tsx` 30 · `.svg` 19 · `.py` 39 · `.gz` 28 · `.woff2` 29 · `.pack` 9 · `.old` 4 · `.css` 2 · `.mako/.ini/.echo/.tsbuildinfo/.rscinfo` 5 |
| `data/` | dir | 79 | 309,643,605 | `.parquet` 68 · `.json` 8 · `.csv` 2 · `.md` 1 |
| `.mypy_cache/` | dir (cache) | 5,237 | 141,616,177 | `.json` 5,234 · `.gitignore` 1 · `CACHEDIR.TAG` 1 · data files |
| `packages/` | dir | 159 | 2,386,665 | `.py` 157 · `.j2` 1 · `.css` 1 |
| `tests/` | dir | 53 | 1,146,460 | `.py` 48 · `.csv` 1 · `.yaml` 1 · `.md` 1 · `.json` 2 |
| `uv.lock` | file | 1 | 318,431 | lockfile (T2 reproducibility artifact — **intended**) |
| `config/` | dir | 8 | 130,121 | `.yaml` 8 |
| `scripts/` | dir | 9 | 102,618 | `.py` 9 |
| `.import_linter_cache/` | dir (cache) | 5 | 90,820 | `.json` 3 · `.gitignore` 1 · `CACHEDIR.TAG` 1 |
| `docs/` | dir | 1 | 86,009 | `.md` 1 |
| `.hermes/` | dir (agent scratch) | 2 | 83,747 | `.py` 1 · `.md` 1 |
| `DECISIONS.md` | file | 1 | 31,648 | doc |
| `STATE.md` | file | 1 | 31,295 | doc |
| `.hypothesis/` | dir (cache, **tracked**) | 1 | 21,726 | `.gz` 1 |
| `LIMITATIONS.md` | file | 1 | 17,248 | doc |
| `MODEL_CARD.md` | file | 1 | 11,398 | doc |
| `README.md` | file | 1 | 10,639 | doc |
| `notebooks/` | dir | 1 | 8,721 | `.ipynb` 1 |
| `ECONOMICS_CARD.md` | file | 1 | 7,997 | doc |
| `docker-compose.yml` | file | 1 | 7,798 | compose |
| `DESIGN.md` | file | 1 | 6,795 | doc |
| `ARCHITECTURE.md` | file | 1 | 6,507 | doc |
| `Makefile` | file | 1 | 5,925 | make |
| `pyproject.toml` | file | 1 | 5,785 | toml |
| `.pre-commit-config.yaml` | file | 1 | 3,636 | yaml |
| `.importlinter` | file | 1 | 3,201 | contracts |
| `BACKLOG.md` | file | 1 | 2,921 | doc |
| `.env.example` | file | 1 | 2,410 | example (intended) |
| `.gitignore` | file | 1 | 1,589 | — |
| `.gitattributes` | file | 1 | 418 | — |
| `.env` | file (**ignored**) | 1 | 361 | secret file, untracked |
| `.python-version` | file | 1 | 5 | pin |
| **Total** | | **6,044** | **805,592,695** | |

**Whole counting set by extension:** `.json` 5,299 (141,882,236 B) · `.parquet`
68 (309,462,119 B) · `.py` 254 (3,988,453 B) · `.js` 208 (72,352,868 B) ·
`.pack` 9 (159,029,089 B) · `.gz` 28 (76,611,803 B) · `.old` 4 (37,700,064 B) ·
`.ts` 40 · `.tsx` 30 · `.woff2` 29 · `.svg` 19 · `.md` 13 · `.yaml` 11 ·
`.css` 4 · `.csv` 3 · `.tsbuildinfo` 2 · `.j2`/`.mjs`/`.jsonl`/`.TAG` 2 each ·
30 singletons.

**Read this first:** 96.6 % of the 768 MiB is `apps/web/.next/` (332 MiB),
`data/interim/` (295 MiB) and `.mypy_cache/` (135 MiB). All three are
gitignored. The *source* tree is small: `packages/` + `tests/` + `config/` +
`scripts/` + `apps/api/` + `apps/web/src/` = **344 files / 4.28 MiB**.

### 1.2 Second-level directories (counting set)

| Directory | Files | Bytes | Types |
| --- | ---: | ---: | --- |
| `apps/api/` | 43 | 552,433 | `.py` 39 · `.ini` 1 · `.echo` 1 · `.mako` 1 · `.json` 1 |
| `apps/web/` | 426 | 349,247,586 | `.next` 340 files/348,323,893 B · `src` 76/601,485 · `public` 2/6,913 · 8 config files/315,295 |
| `apps/web/.next/` | 340 | 348,323,893 | build output (cache 273.5 MB, static 53.6 MB, server 19.1 MB, trace 2.05 MB) |
| `apps/web/src/` | 76 | 601,485 | `.tsx` 30 · `.ts` 28 · `.css` 1 · `.svg` 15 · `.mjs` 1 · `.d.ts` 1 |
| `config/` | 8 | 130,121 | `.yaml` 8 |
| `data/interim/` | 70 | 309,457,481 | `.parquet` 67 · `.json` 2 · `.csv` 1 |
| `data/processed/` | 2 | 147,158 | `.json` 1 · `.parquet` 1 |
| `data/watchlist/` | 2 | 2,153 | `.csv` 1 · `.json` 1 |
| `docs/audit/` | 1 | 86,009 | `.md` 1 |
| `notebooks/` | 1 | 8,721 | `.ipynb` 1 |
| `packages/pipeline/` | 159 | 2,386,665 | `.py` 157 · `.j2` 1 · `.css` 1 |
| `scripts/` | 9 | 102,618 | `.py` 9 |
| `tests/contracts/` | 4 | 66,542 | `.py` 4 |
| `tests/contracts_adapters/` | 1 | 1,296 | `.json` 1 (fixture only) |
| `tests/e2e/` | 1 | 0 | `.py` 1 (empty `__init__.py`) |
| `tests/golden/` | 9 | 252,605 | `.csv` 1 · `.py` 5 · `.yaml` 1 · `.json` 1 · `.md` 1 |
| `tests/integration/` | 4 | 112,935 | `.py` 4 |
| `tests/unit/` | 31 | 682,704 | `.py` 31 |
| `tests/` (root files) | 2 | 30,378 | `__init__.py` 0 B · `conftest.py` 2,787 B · `test_leakage.py` 27,591 B |
| `.mypy_cache/3.12/` | 5,235 | 141,615,948 | cache |
| `.hermes/plans/` | 1 | 76,076 | `.md` 1 (copy of the build plan) |
| `.hypothesis/unicode_data/` | 1 | 21,726 | `.gz` 1 (**tracked**) |
| `.import_linter_cache/` | 5 | 90,820 | `.json` 3 · `.gitignore` 1 · `CACHEDIR.TAG` 1 |

### 1.3 On disk, outside the counting set (context; all gitignored except where noted)

| Path | Files | Bytes | Note |
| --- | ---: | ---: | --- |
| `.venv/` | 29,315 | 1,441,420,062 | ignored |
| `data/raw/` | 6 | 1,189,962,661 | PaySim 493.5 MB, IBM-AML 475.7 MB, archive zip 186.4 MB |
| `apps/web/node_modules/` | 22,963 | 551,301,808 | ignored; install present (`.modules.yaml`, `.pnpm/`) |
| `out/` | 88 | 46,000,673 | ignored; 12 run trees + `warehouse/oxbow.duckdb` (274,432 B) + `p7a.log` |
| `.git/` | — | — | 386 tracked objects |

---

## 2. Prescribed-vs-actual diff

71 prescribed paths extracted from plan §5 T1 (lines 127–142).

| Status | Count | Detail |
| --- | ---: | --- |
| **PRESENT** | **67** | including 2 present-but-empty (below) |
| **MISSING** | **4** | `PROMPT.md`, `notebooks/02_features.ipynb`, `notebooks/03_validation.ipynb`, `apps/api/worker.py` |
| **MISPLACED** | **0** (strict) | no prescribed path exists anywhere else; nothing was renamed away from its prescribed name |
| PRESENT but **empty** | 2 | `data/snapshots/`, `data/golden/` |
| Stray / misfiled (**not** in the 71) | 22 files | see §3 |

### 2.1 Root documents (10 prescribed)

| Path | Status | Bytes |
| --- | --- | ---: |
| `README.md` | PRESENT | 10,639 |
| `ARCHITECTURE.md` | PRESENT | 6,507 |
| `LIMITATIONS.md` | PRESENT | 17,248 |
| `MODEL_CARD.md` | PRESENT | 11,398 |
| `ECONOMICS_CARD.md` | PRESENT | 7,997 |
| `DESIGN.md` | PRESENT | 6,795 |
| **`PROMPT.md`** | **MISSING** | — |
| `STATE.md` | PRESENT | 31,295 |
| `DECISIONS.md` | PRESENT | 31,648 |
| `BACKLOG.md` | PRESENT | 2,921 |

`PROMPT.md` is the only root document absent. T1 line 143 is explicit: it is
"the verbatim 01 build prompt, committed, per its own aside". A near-copy of the
build plan does exist at `.hermes/plans/2026-09-26_021500-oxbow-master-build-plan.md`
(76,076 B) but that is the *master build plan*, not the 01 build prompt, and
`.hermes/` is gitignored.

Root infrastructure (5 prescribed) — all PRESENT: `Makefile` 5,925 ·
`docker-compose.yml` 7,798 · `.env.example` 2,410 · `.pre-commit-config.yaml`
3,636 · `.python-version` 5.

### 2.2 `config/` — 8 of 8 PRESENT, and no extras

`pipeline.yaml` 10,533 · `rules.yaml` 12,406 · `features.yaml` 58,899 ·
`model.yaml` 9,069 · `scorecard.yaml` 10,843 · `economics.yaml` 9,575 ·
`splits.yaml` 5,059 · `sources.yaml` 13,737. Every file carries a real schema
and a named owner in its header comment (T1 line 143 satisfied).
`tests/unit/test_p0_toolchain.py:133-148` parametres over exactly these eight
and asserts each exists and parses.

### 2.3 `data/`

| Path | Status | Note |
| --- | --- | --- |
| `data/raw/` | PRESENT | 6 files, 1.19 GB (excluded from counts) |
| `data/interim/` | PRESENT | 70 files — 67 `.parquet`, 2 `run_manifest.json`, 1 `_probe.csv` |
| `data/processed/` | PRESENT | 2 files — `eval.json`, `ibm_typologies.parquet` |
| `data/snapshots/` | **PRESENT but EMPTY** | T3 requires `data/snapshots/demo.dump`; absent |
| `data/golden/` | **PRESENT but EMPTY** | §9 P3b requires a golden fixture here |
| `data/DATASET_CARD.md` | PRESENT | 26,544 |

Unprescribed additions at `data/` root: `download_manifest.json` (841),
`graph_measurement.json` (2,192), `ibm_cycle_measurement.json` (4,661),
`ibm_graph_measurement.json` (2,575); unprescribed dir `data/watchlist/`.

### 2.4 `notebooks/` — 1 of 3

`01_eda.ipynb` PRESENT (8,721) · **`02_features.ipynb` MISSING** ·
**`03_validation.ipynb` MISSING**.

### 2.5 `packages/pipeline/oxbow/`

`cli.py` PRESENT (74,433). Subpackages, all PRESENT as directories:

| Subpackage | Files | Bytes | State |
| --- | ---: | ---: | --- |
| `contracts/` | 4 | 64,970 | populated |
| `ingest/` | 5 | 161,457 | populated |
| `features/` | 10 | 277,146 | populated (incl. `bridge.py`, 30,042) |
| `graph/` | 12 | 156,804 | populated |
| `rules/` | 15 | 207,640 | populated |
| `models/` | 15 | 259,038 | populated |
| `quant/` | 8 | 150,204 | populated |
| `backtest/` | 15 | 210,112 | populated |
| `explain/` | 1 | 75 | **stub only** — a 75-byte `__init__.py`, zero modules |
| `packet/` | 6 | 110,676 | populated (+ `templates/`) |
| `ports/` | 9 | 56,288 | populated |
| `adapters/` | 4 + 33 | 18,691 + 173,873 | populated, 10 sub-adapters |

Unprescribed additions: `oxbow/scoring/` (12 files, 197,794),
`oxbow/audit/` (3, 19,487), `oxbow/publish/` (1, 75 — stub), and seven
top-level modules `config.py` (8,374), `dataset_card.py` (55,072),
`eval.py` (127,735), `identity.py` (7,803), `stage_events.py` (13,101) plus
`__init__.py` (1,468). `packages/pipeline/tests/` (1 file, 56 B).

### 2.6 `apps/api/`

| Path | Status | Bytes |
| --- | --- | ---: |
| `main.py` | PRESENT | 16,277 |
| `routers/` | PRESENT | 12 files (11 routers + `__init__.py`) |
| `schemas/` | PRESENT | 10 files (9 schemas + `__init__.py`) |
| `deps.py` | PRESENT | 31,545 |
| `problems.py` | PRESENT | 14,946 |
| `events.py` | PRESENT | 13,992 |
| **`worker.py`** | **MISSING** | — |

`worker.py` is referenced by three live declarations and does not exist:
`Makefile:122` (`$(PY) apps/api/worker.py`), `docker-compose.yml:195`
(`command: ["python", "apps/api/worker.py"]`), and `apps/api/jobs.py:103`
(enqueues the RQ target string `"api.worker.run_stages"`). `make worker`, the
`worker` compose service and every submitted job are therefore broken.

Unprescribed additions in `apps/api/`: `decisions.py` (43,631), `echo.py`
(6,629), `jobs.py` (11,932), `observability.py` (9,204), `outbox.py` (17,296),
`policy_engine.py` (27,456), `readmodel.py` (46,026), `security.py` (15,532),
`settings.py` (7,641), `__init__.py` (312), `alembic.ini` (1,296),
`alembic/` (4 files), `keycloak/realm-oxbow.json` (33), `Dockerfile.echo` (2,165).

### 2.7 `apps/web/src/`

All 20 prescribed paths PRESENT. `app/(dash)/` holds all seven prescribed
routes (`dashboard/`, `alerts/`, `cases/[id]/`, `network/`, `scorecard/`,
`policy/`, `model/`) plus `layout.tsx`; `app/dev/states/` holds 3 files;
`design/` holds `tokens.css` (9,684), `tokens.ts` (6,217), `motion.ts` (6,888),
`icons/` (4 + 15 files) and `primitives/` (5 files).

Unprescribed additions: `app/{error,global-error,layout,not-found,page}.tsx` and
`globals.css` at the route root (all standard Next.js), and
`src/{components,fixtures,lib,types}/`.

### 2.8 `tests/`

| Path | Status | Files | Bytes |
| --- | --- | ---: | ---: |
| `tests/contracts/` | PRESENT, populated | 4 | 66,542 |
| `tests/unit/` | PRESENT, populated | 31 | 682,704 |
| `tests/golden/` | PRESENT, populated | 9 | 252,605 |
| `tests/integration/` | PRESENT, populated | 4 | 112,935 |
| `tests/e2e/` | PRESENT, **empty** (only 0-byte `__init__.py`) | 1 | 0 |

`tests/contracts_adapters/` is **not prescribed**; `scripts/verify.py:237` uses
it as the P7 gate path. `STATE.md:118` still records it as "does not exist yet" —
that line is now stale: the directory exists but holds one fixture and no test.

---

## 3. Stray and misplaced file table (primary deliverable)

### 3.1 Loose scripts at repo root

**None.** All 9 loose scripts are in `scripts/`, all 9 tracked, and 5 of 9 are
wired into `Makefile` (`download_data.py`, `verify.py`, `verify_audit.py`,
`verify_determinism.py`) or `.pre-commit-config.yaml:56`
(`no_float_money.py`). The remaining 4 (`build_ibm_typologies.py`,
`measure_graph.py`, `measure_ibm_cycles.py`, `measure_ibm_graph.py`) are the
§4 measurement scripts that produced the committed `data/*_measurement.json`
files. No action.

### 3.2 Modules in the wrong subpackage

| # | Current path | What it is | Target path | Conf. | Reason |
| ---: | --- | --- | --- | --- | --- |
| 1 | `apps/api/jobs.py` (11,932 B) | `APIRouter(tags=["jobs"])` at line 40; job submission + status endpoints | `apps/api/routers/jobs.py` | **HIGH** | It is the only APIRouter outside `routers/`; all 11 sibling routers live there, and the sole exception `events.py` is *explicitly prescribed* at top level |
| 2 | `packages/pipeline/oxbow/scoring/` (12 files, 197,794 B) | Scorecard: `bands.py binning.py config.py drift.py errors.py frame.py generated.py model.py reasons.py scale.py selection.py` | (a) keep as-is + amend the plan tree, or (b) `packages/pipeline/oxbow/models/` | **AMBIGUOUS** | 55 import references across 23 files; named in 3 of 4 `.importlinter` contracts; `config/scorecard.yaml` names `oxbow/scoring/config.py` as its reader. Not a move, a decision |
| 3 | `packages/pipeline/oxbow/audit/` (3 files, 19,487 B) | `chain.py` (hash chain), `erasure.py` (GDPR erasure) | (a) keep, or (b) `chain.py` → `oxbow/explain/`, `erasure.py` → `oxbow/adapters/file/` | **AMBIGUOUS** | 14 references across 12 files; not prescribed; `explain/` is an empty stub that would absorb it, but `ports/audit.py` + `adapters/{audit,file,null}/` already own the adapter side |
| 4 | `packages/pipeline/oxbow/publish/__init__.py` (75 B) | Empty stub subpackage, docstring only | delete, or keep | **AMBIGUOUS** | **Zero references repo-wide.** Named only in `.importlinter:90` (contract 4 `source_modules`), so deleting it requires that edit too |
| 5 | `packages/pipeline/oxbow/eval.py` (127,735 B) | Largest single module in the repo | (a) keep beside `cli.py`, or (b) `oxbow/eval/` | **AMBIGUOUS** | Only `cli.py` is prescribed at that level; the other six top-level modules are additive and consistently placed. Splitting is a refactor, not a reorganisation |
| 6 | `packages/pipeline/oxbow/packet/templates/` | Jinja2 template + CSS | **no move** | n/a | Correctly has no `__init__.py`; a package-data directory, not a package |

### 3.3 Tests outside `tests/`

| # | Current path | What it is | Target path | Conf. | Reason |
| ---: | --- | --- | --- | --- | --- |
| 7 | `packages/pipeline/tests/__init__.py` (56 B) | A test package outside the prescribed test root; docstring "OXBOW pipeline tests, colocated with the package" | (a) `tests/pipeline/__init__.py`, or (b) delete the dir and drop `testpaths[1]` | **BLOCKED** | It is `pyproject.toml:151` `testpaths[1]`. Any move changes what `testpaths` resolves to — a decision, not a move (§7 rule) |
| 8 | `tests/test_leakage.py` (27,591 B) | The P2 leakage-gate test, sitting at `tests/` root | `tests/unit/test_p2_leakage.py` | **MEDIUM** | It is the only test module outside a `tests/` subdirectory, and every other P2 test is `tests/unit/test_p2_*.py`. But `scripts/verify.py:164` names it by exact path |
| 9 | `tests/contracts_adapters/fixtures/rs256_token.json` (1,296 B) | A JWT fixture in a directory with no tests | (a) `tests/contracts/fixtures/rs256_token.json`, or (b) keep — it is the future P7 gate's own fixture dir | **AMBIGUOUS** | `scripts/verify.py:237` runs `pytest -q tests/contracts_adapters` as the P7 gate; moving the fixture out leaves that path with nothing at all |
| 10 | `tests/contracts_adapters/` and `tests/contracts_adapters/fixtures/` | Directories, both missing `__init__.py` | add `__init__.py` | **HIGH** | They are the **only** `tests/` subdirectories without one — `tests/`, `contracts/`, `unit/`, `golden/`, `integration/`, `e2e/` all have a 0-byte `__init__.py` |
| 11 | `tests/e2e/__init__.py` (0 B) | Prescribed dir, no content | — | n/a | A gap, not clutter: §5 |

### 3.4 Generated artifacts committed by mistake

| # | Current path | Bytes | Tracked? | Target | Conf. | Reason |
| ---: | --- | ---: | --- | --- | --- | --- |
| 12 | `.hypothesis/unicode_data/15.0.0/charmap.json.gz` | **21,726** | **YES** | untrack (cache) | **HIGH** | Hypothesis's property-test cache. `.gitignore` has **no `.hypothesis/` rule at all** — a genuine `.gitignore` gap, not an ordering accident. `test_anti_rubbish.py:55` already lists `.hypothesis` in `SKIP_DIRS`, so the project treats it as a cache |
| 13 | `data/processed/ibm_typologies.parquet` | **9,357** | **YES** | untrack (dataset) | **HIGH** | `.gitignore:52` says `data/processed/*`, but ignore rules do not apply to already-tracked paths, so the file survives. A committed Parquet dataset directly contradicts `.gitignore:46-49` ("corpora and artifacts are content-hashed, not committed") |
| 14 | `data/graph_measurement.json` | 2,192 | YES | (a) keep, or (b) `out/measurements/` | **AMBIGUOUS** | 60 code references. It is the §4 measurement that decides the architecture, so it is evidence — but `data/` has a prescribed shape with no room for it |
| 15 | `data/ibm_graph_measurement.json` | 2,575 | YES | same | **AMBIGUOUS** | 35 references |
| 16 | `data/ibm_cycle_measurement.json` | 4,661 | YES | same | **AMBIGUOUS** | 13 references |
| 17 | `data/download_manifest.json` | 841 | YES | same | **AMBIGUOUS** | 5 references; a download receipt, arguably belongs beside `DATASET_CARD.md` |
| 18 | `tests/golden/build_manifest.json` | 6,307 | YES | **no move** | n/a | Deliberate: it is the record that `expected.yaml` was hand-computed. `.gitignore:58-60` states the golden fixture IS committed |
| 19 | `data/watchlist/sample-sdn-v1.csv` + `.meta.json` | 1,154 + 999 | YES | (a) `tests/contracts_adapters/fixtures/watchlist/`, or (b) keep | **AMBIGUOUS** | Synthetic fixture from commit `91d7a0e`. `oxbow/ports/watchlist.py` + `oxbow/adapters/{null,ofac}/watchlist.py` read snapshots from disk, so the path is a runtime input, not just a test input |
| 20 | `tests/golden/transactions.csv` | 121,186 | YES | **no move** | n/a | Deliberate, per `.gitignore:58-60` |
| 21 | `apps/web/public/{mark.svg,sprite.svg}` | 1,061 + 5,852 | YES | **no move** | n/a | Byte-identical build copies of `src/design/icons/*` (SHA-256 `7FCE7177…` and `C80222A8…` match exactly) and Next.js must serve them from `public/`. **Drift risk:** the `design-tokens-in-sync` pre-commit hook (`.pre-commit-config.yaml:97`) diffs only `src/design/tokens.ts` and `src/design/icons/sprite.svg`, so the `public/` copies are unchecked |

### 3.5 Generated artifacts on disk, correctly ignored (listed so a reorg does not re-add them)

| Path | Files | Bytes |
| --- | ---: | ---: |
| `apps/web/.next/` | 340 | 348,323,893 |
| `apps/web/tsconfig.tsbuildinfo` | 1 | 167,675 |
| `apps/web/node_modules/` | 22,963 | 551,301,808 |
| `data/interim/` (67 parquet) | 70 | 309,457,481 |
| `data/processed/eval.json` | 1 | 137,801 |
| `data/raw/` | 6 | 1,189,962,661 |
| `out/` (incl. `warehouse/oxbow.duckdb` 274,432 B, `p7a.log` 7,869 B) | 88 | 46,000,673 |
| `.venv/` | 29,315 | 1,441,420,062 |
| `.mypy_cache/` | 5,237 | 141,616,177 |
| `.import_linter_cache/` | 5 | 90,820 |

No `.dump` snapshot, no model binary (`.pkl`/`.joblib`/`.onnx`/`.ubj`), no
coverage output and no `.log` file exists anywhere outside `out/` and
`data/raw/`. `htmlcov/` does not exist.

### 3.6 Package directories missing `__init__.py`

| Path | Verdict |
| --- | --- |
| `tests/contracts_adapters/` | **DEFECT** — add `__init__.py` |
| `tests/contracts_adapters/fixtures/` | **DEFECT** — add `__init__.py` |
| `packages/pipeline/` | Not a defect — not a package; the wheel `packages` entry points at `packages/pipeline/oxbow` |
| `packages/pipeline/oxbow/packet/templates/` | Not a defect — package data (`.j2`, `.css`) |
| `apps/api/alembic/`, `apps/api/alembic/versions/` | Not a defect — standard alembic layout |
| `apps/api/keycloak/` | Not a defect — realm-import data |
| `scripts/` | Not a defect — invoked by path from `Makefile` |

### 3.7 Unprescribed directories on disk

| Path | Files | Verdict |
| --- | ---: | --- |
| `packages/pipeline/oxbow/scoring/` | 12 | AMBIGUOUS — §3.2 #2 |
| `packages/pipeline/oxbow/audit/` | 3 | AMBIGUOUS — §3.2 #3 |
| `packages/pipeline/oxbow/publish/` | 1 | Stray stub, 0 refs — §3.2 #4 |
| `packages/pipeline/tests/` | 1 | BLOCKED — §3.3 #7 |
| `tests/contracts_adapters/` | 1 | Named by `scripts/verify.py:237`; keep, add `__init__.py` |
| `data/watchlist/` | 2 | AMBIGUOUS — §3.4 #19 |
| `docs/`, `docs/audit/` | 1 | Created by this task / another agent; mandated by the brief, not by T1 |
| `scripts/` | 9 | Not in the T1 tree but load-bearing in `Makefile` and `.pre-commit-config.yaml`. Legitimate — the tree in T1 lists directories, not an exhaustive file manifest |
| `.github/`, `.github/workflows/` | 0 | **EMPTY** — T6 requires the CI workflow; it does not exist |
| `.hermes/` | 2 | Agent scratch, gitignored (`.gitignore:79`); holds a copy of the build plan and `p2_registry_patch.py` |
| `apps/api/alembic/`, `apps/api/keycloak/` | 4, 1 | T9 mandates alembic; keycloak is mandated by T4 (C7) |
| `apps/web/src/{components,fixtures,lib,types}/` | 24 | Additive and conventionally placed |
| `apps/web/public/` | 2 | Next.js static serving — see §3.4 #21 |

### 3.8 Correctly placed — explicitly NOT moves

`apps/api/{decisions,outbox,policy_engine,readmodel,security,settings,observability,echo}.py`
are domain modules sitting beside the prescribed `deps.py`/`problems.py`; none
defines an `APIRouter`, and all are imported as `api.<name>` throughout
`tests/integration/test_p7_api.py`. `apps/api/events.py` **is** a router but is
prescribed at that exact path. `apps/api/routers/common.py` has no `APIRouter`
(it is a shared `build_meta` helper) and is correctly inside `routers/`. The
`config/*.yaml` eight are exactly right.

---

## 4. Committed-artifact audit

**386 tracked files, 5,628,731 bytes (5.37 MiB).** Every tracked path exists on
disk; zero tracked-but-missing. For scale: the disk holds 768 MiB in the
counting set, so the committed tree is 0.7 % of it.

### 4.1 The 15 largest tracked files

| # | Bytes | Path |
| ---: | ---: | --- |
| 1 | 318,431 | `uv.lock` |
| 2 | 142,163 | `apps/web/pnpm-lock.yaml` |
| 3 | 127,735 | `packages/pipeline/oxbow/eval.py` |
| 4 | 121,186 | `tests/golden/transactions.csv` |
| 5 | 96,419 | `tests/integration/test_p7_api.py` |
| 6 | 86,419 | `tests/unit/test_p1b_ibm_aml.py` |
| 7 | 74,433 | `packages/pipeline/oxbow/cli.py` |
| 8 | 71,231 | `packages/pipeline/oxbow/models/run.py` |
| 9 | 67,764 | `packages/pipeline/oxbow/ingest/ibm_aml.py` |
| 10 | 65,693 | `packages/pipeline/oxbow/adapters/warehouse/models.py` |
| 11 | 60,136 | `tests/unit/test_p3b_rules.py` |
| 12 | 58,899 | `config/features.yaml` |
| 13 | 56,708 | `tests/golden/build_fixture.py` |
| 14 | 55,669 | `apps/api/alembic/versions/0001_7907d04c69bc_warehouse_read_model.py` |
| 15 | 55,072 | `packages/pipeline/oxbow/dataset_card.py` |

Tracked by top level: `packages` 159 · `apps` 128 · `tests` 53 · root 19 ·
`scripts` 9 · `data` 8 · `config` 8 · `.hypothesis` 1 · `notebooks` 1.

### 4.2 Tracked files that are datasets, caches or build output

| Path | Bytes | Category | Verdict |
| --- | ---: | --- | --- |
| `.hypothesis/unicode_data/15.0.0/charmap.json.gz` | 21,726 | test-runner cache | **Should not be tracked** |
| `data/processed/ibm_typologies.parquet` | 9,357 | Parquet dataset | **Should not be tracked** |
| `data/watchlist/sample-sdn-v1.csv` | 1,154 | synthetic dataset | Debatable (a fixture) |
| `data/download_manifest.json` | 841 | generated receipt | Debatable |
| `data/graph_measurement.json` | 2,192 | generated measurement | Debatable (evidence) |
| `data/ibm_graph_measurement.json` | 2,575 | generated measurement | Debatable (evidence) |
| `data/ibm_cycle_measurement.json` | 4,661 | generated measurement | Debatable (evidence) |
| `tests/golden/transactions.csv` | 121,186 | committed fixture | **Intended** (`.gitignore:58-60`) |
| `tests/golden/build_manifest.json` | 6,307 | generated manifest | **Intended** (evidence of hand-computation) |
| `tests/contracts_adapters/fixtures/rs256_token.json` | 1,296 | test fixture | Intended |
| `notebooks/01_eda.ipynb` | 8,721 | notebook with outputs | Intended |
| `apps/web/pnpm-lock.yaml` | 142,163 | lockfile | **Intended** (T2) |
| `uv.lock` | 318,431 | lockfile | **Intended** (T2) |
| `apps/api/keycloak/realm-oxbow.json` | 33 | config | Intended (T4/C7) |

**Not tracked, verified:** `apps/web/.next/`, `apps/web/tsconfig.tsbuildinfo`,
`apps/web/node_modules/`, `data/raw/`, `data/interim/`, `data/processed/eval.json`,
`out/`, `.venv/`, `.mypy_cache/`, `.import_linter_cache/`, `.env`, every
`__pycache__/`, `htmlcov/`.

### 4.3 `.gitignore` vs disk reality

`.gitignore` is 79 lines and is **substantively correct**. Verified by
`git check-ignore -v` on 21 representative paths.

| Rule | Covers | Disk reality | Verdict |
| --- | --- | --- | --- |
| `.env` / `.env.*` / `!.env.example` | secrets | `.env` (361 B) present and ignored; `.env.example` tracked | **correct** |
| `__pycache__/`, `*.py[cod]` | python | 30+ `__pycache__` dirs on disk, all ignored | correct |
| `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `.coverage`, `htmlcov/` | caches | present, all ignored | correct |
| `.venv/` | venv | 29,315 files, ignored | correct |
| `node_modules/`, `apps/web/.next/`, `*.tsbuildinfo`, `apps/web/out/`, `.turbo/` | node | all present, all ignored | correct |
| `data/raw/*`, `data/interim/*`, `data/processed/*`, `data/snapshots/*` + four `!.gitkeep` negations | corpora | all ignored — **but `data/processed/ibm_typologies.parquet` is tracked anyway** | **MISMATCH** |
| `!data/golden/*` | golden fixture | `data/golden/` is empty | rule fine, dir empty |
| `artifacts/`, `out/`, `mlruns/` | build output | `out/` present (46 MB), ignored | correct |
| `!apps/web/src/lib/api/schema.d.ts` | generated client, "committed on purpose" | **the file does not exist** | **MISMATCH** |
| `.hermes/` | agent scratch | 2 files, ignored | correct |
| **— none —** | **`.hypothesis/`** | **1 file, 21,726 B, TRACKED** | **MISSING RULE** |
| `htmlcov/` | coverage | dir absent | fine (dir-only rule) |

**Two further gaps, both about `data/` not `.gitignore` syntax:**

1. **No `.gitkeep` exists in any of the five `data/` subdirectories.** The four
   negations at `.gitignore:54-57` (`!data/raw/.gitkeep`,
   `!data/interim/.gitkeep`, `!data/processed/.gitkeep`,
   `!data/snapshots/.gitkeep`) all name files that are not on disk. Because
   every file inside those directories is ignored, **all five directories
   vanish on a fresh clone** — including the two *prescribed* ones,
   `data/snapshots/` and `data/golden/`. `data/golden/` has no ignore rule at
   all, so it is simply untracked-and-empty.
2. `data/interim/_probe.csv` (220 B) is a scratch probe sitting in an ignored
   directory. Harmless, but it indicates ad-hoc runs write into `data/`.

---

## 5. Empty / near-empty prescribed directories

These are **gaps, not clutter**. Nothing here should be moved or deleted.

| Prescribed path | State | Files / bytes | Assessment |
| --- | --- | ---: | --- |
| `tests/contracts_adapters/` | **near-empty** (not prescribed) | 1 / 1,296 B | Only `fixtures/rs256_token.json`; **zero tests**. `scripts/verify.py:237` runs `pytest -q tests/contracts_adapters` as the P7 gate, so that gate currently collects nothing. `STATE.md:118` still calls the directory non-existent — stale |
| `tests/contracts/` | populated | 4 / 66,542 B | `__init__.py`, `conftest.py`, `test_p1b_batch_boundary.py`, `test_p1b_canonical_ingest.py` |
| `tests/golden/` | populated | 9 / 252,605 B | Fixture, manifest, `expected.yaml`, self-check test, `README.md`, `transactions.csv` |
| `tests/e2e/` | **EMPTY** | 1 / 0 B | A 0-byte `__init__.py` and nothing else. No e2e test exists; the `e2e` pytest marker (`pyproject.toml:159`) is declared and unused |
| `data/snapshots/` | **EMPTY** | 0 / 0 B | No `.gitkeep`. T3 (`Makefile:129`) needs `data/snapshots/demo.dump`; `STATE.md:336-337` confirms it is item 4 on the punch list. `make demo` also needs the missing `scripts/demo_seed.py` |
| `data/golden/` | **EMPTY** | 0 / 0 B | No `.gitkeep`, no ignore rule. §9 P3b wants a golden fixture here; the working golden fixture currently lives in `tests/golden/` |
| `data/interim/` | populated | 70 / 309,457,481 B | 66 `paysim` parquet + 1 `ibmaml` parquet + 2 `run_manifest.json` + `_probe.csv`. All ignored |
| `notebooks/` | **1 of 3** | 1 / 8,721 B | `01_eda.ipynb` only. `02_features.ipynb` and `03_validation.ipynb` are missing outright |
| `apps/web/src/design/primitives/` | populated | 5 / 51,044 B | `EmptyState.tsx` 21,895 · `StageLedger.tsx` 12,022 · `ErrorPane.tsx` 9,078 · `Skeleton.tsx` 4,602 · `Shimmer.tsx` 3,759 |
| `apps/web/src/app/dev/states/` | populated | 3 / 25,091 B | `page.tsx` 19,245 · `gallery-rows.tsx` 4,840 · `gallery-meta.ts` 1,006 |
| `apps/api/routers/` | populated | 12 / 152,478 B | 11 routers + `__init__.py`; **but `jobs.py` is missing from here** (§3.2 #1) |
| `apps/api/schemas/` | populated | 10 / 74,332 B | 9 schemas + `__init__.py` |
| `packages/pipeline/oxbow/explain/` | **EMPTY** | 1 / 75 B | Prescribed subpackage with only a docstring `__init__.py`. **Zero modules.** Note `oxbow/models/explain.py` (13,011 B) exists and is the de-facto explain implementation |
| `packages/pipeline/oxbow/publish/` | **EMPTY** | 1 / 75 B | Not prescribed, 0 references (§3.2 #4) |
| `.github/workflows/` | **EMPTY** | 0 / 0 B | T6 requires lint, typecheck, unit tests, 50k-row smoke, one-fold backtest smoke, image build, `pip-audit`, `pnpm audit`, `schemathesis`, `import-linter`. None exists |

---

## 6. Package wiring

### 6.1 `pyproject.toml` (root, 165 lines, 5,785 B)

| Setting | Value | Resolves on disk? |
| --- | --- | --- |
| `build-backend` | `hatchling.build` (`requires = ["hatchling"]`) | n/a |
| `[tool.hatch.build.targets.wheel] packages` | `["packages/pipeline/oxbow"]` | **EXISTS** (159 files) |
| `[project.scripts] oxbow` | `oxbow.cli:app` | `cli.py` exists (74,433 B) |
| `[tool.pytest.ini_options] testpaths` | `["tests", "packages/pipeline/tests"]` | **BOTH EXIST** — 53 files and 1 file |
| `python_files` | `["test_*.py"]` | n/a |
| `markers` | `golden`, `slow`, `contract`, `integration`, `e2e` | `e2e` and `slow` are declared but **no test uses them** (zero matches in `tests/`) |
| `[tool.ruff] src` | `["packages/pipeline", "apps/api", "tests", "scripts"]` | all four EXIST |
| `[tool.ruff] extend-exclude` | `[".hermes", "data", "out"]` | correct |
| `[tool.ruff.lint.per-file-ignores]` | `tests/**`, `apps/api/alembic/**`, `apps/api/main.py`, `apps/api/**`, `apps/api/problems.py` | all patterns match existing paths |
| `[tool.mypy] mypy_path` | `packages/pipeline` | EXISTS |
| `[tool.mypy] exclude` | `['(^|/)\.hermes/', 'data/', 'out/']` | correct |
| `[tool.coverage.run] source` | `["packages/pipeline/oxbow"]` | EXISTS |
| `requires-python` | `>=3.12,<3.13` | `.python-version` = `3.12` ✓ |

**Any move that changes what `packages = [...]` or `testpaths = [...]` resolves
to is a decision, not a move.** That constrains items 7, 8, 9, 12, 13, 14 in §3
and moves M5–M9 in §7.

**Note on `testpaths[1]`:** `packages/pipeline/tests/` contains exactly one
file, a 56-byte `__init__.py`. It contributes **zero tests**. The entry is pure
debt: it is the only reason a second test root exists.

### 6.2 `apps/web/package.json` (76 lines) + `pnpm-lock.yaml` (142,163 B, tracked)

| Check | Result |
| --- | --- |
| `packageManager` | `pnpm@9.15.9` — **pinned exactly**, matches T2 |
| `lockfileVersion` | `'9.0'` — current, and `apps/web/pnpm-lock.yaml` is **tracked** |
| `pnpm.overrides` | 3 entries, all pinned to `4.0.0`; mirrored in the lockfile's `overrides:` block |
| `dependencies` | 28 entries, **every one an exact version** — no `^`, no `~`, no `*` |
| `devDependencies` | 18 entries, **every one an exact version** |
| Lockfile sync | Cannot be proven without running `pnpm install --frozen-lockfile`, which was out of scope. An install *is* present (`node_modules/.modules.yaml`, `node_modules/.pnpm/`) |
| `engines.node` | `>=20` |

**Verdict: the frontend is properly pinned.** No version-range drift exists.

### 6.3 Every declared path that does not exist

| Declared by | Missing path | Consequence |
| --- | --- | --- |
| plan T1 | `PROMPT.md` | T1 line 143 explicit |
| plan T1 | `notebooks/02_features.ipynb` | — |
| plan T1 | `notebooks/03_validation.ipynb` | — |
| plan T1 | `apps/api/worker.py` | see below |
| `Makefile:122` | `apps/api/worker.py` | **`make worker` fails** |
| `docker-compose.yml:195` | `apps/api/worker.py` | **`worker` service fails to start** |
| `apps/api/jobs.py:103` | `api.worker.run_stages` | **every enqueued job fails at dispatch** |
| `Makefile:129` | `scripts/demo_seed.py` | **`make demo` fails** (corroborated by `STATE.md:336`) |
| `Makefile:128` (target doc) | `data/snapshots/demo.dump` | **`make demo` fails** |
| `docker-compose.yml:176`, `:192` | `apps/api/Dockerfile` | **`api` and `worker` services cannot build** |
| `docker-compose.yml:208` | `apps/web/Dockerfile` | **`web` service cannot build** |
| `.gitignore:68` | `apps/web/src/lib/api/schema.d.ts` | the "committed on purpose" generated client does not exist |
| `apps/web/package.json:16` | vitest config + `*.test.ts(x)` | **`make test-web` / P8 gate collect nothing** — no `vitest.config.*`, no test files under `apps/web/` |
| `apps/web/package.json:17` | `playwright.config.ts` | **`make test-e2e` cannot run** |
| `.github/workflows/` | any workflow | **T6 CI does not exist** |
| `pyproject.toml:159` | any test using `slow` / `e2e` markers | markers declared, unused |
| `.gitignore:54-57` | `data/{raw,interim,processed,snapshots}/.gitkeep` | all four negations are dead; the dirs vanish on fresh clone |

Referenced-and-present (no action): `apps/api/alembic.ini` ✓,
`apps/api/keycloak/realm-oxbow.json` ✓, `apps/api/Dockerfile.echo` ✓,
`apps/web/{biome.json,next.config.ts,postcss.config.mjs,tsconfig.json,next-env.d.ts}` ✓,
`scripts/{download_data,verify,verify_audit,verify_determinism,no_float_money}.py` ✓.

---

## 7. Ready-to-execute move list

Ordered so that no move depends on a later one. Companion changes are named per
move. **Nothing in this list was executed.**

Legend — `SAFE-TO-EXECUTE-NOW`: the move is self-contained, nothing references
the old path. `NEEDS-A-COMPANION-CHANGE`: a named file must be edited in the
same commit or the tree breaks. `BLOCKED`: requires a human decision first.

### Step 0 — untrack (not moves; two commands, highest value per byte)

| # | Command | Bytes | Flag | Companion |
| ---: | --- | ---: | --- | --- |
| 0.1 | `git rm --cached .hypothesis/unicode_data/15.0.0/charmap.json.gz` | 21,726 | **SAFE-TO-EXECUTE-NOW** | add `.hypothesis/` to `.gitignore` (currently absent) |
| 0.2 | `git rm --cached data/processed/ibm_typologies.parquet` | 9,357 | **SAFE-TO-EXECUTE-NOW** | none — `.gitignore:52` already covers it; only the tracked state is wrong |

### Step 1 — executable now, with a named companion edit

| # | Command | Bytes | Flag | Companion change required |
| ---: | --- | ---: | --- | --- |
| 1 | `git mv apps/api/jobs.py apps/api/routers/jobs.py` | 11,932 | **NEEDS-A-COMPANION-CHANGE** | **Two** import sites: `apps/api/main.py:46` `from api import jobs` → `from api.routers import jobs`; `apps/api/routers/runs.py:31` `from api.jobs import submit_job` → `from api.routers.jobs import submit_job`. `main.py:155` (`jobs.router`) and the `routers/__init__.py` are unaffected. `pyproject.toml:117` `"apps/api/**" = ["ARG"]` still matches. No doc, Makefile, compose or fixture reference |
| 2 | `git mv tests/test_leakage.py tests/unit/test_p2_leakage.py` | 27,591 | **NEEDS-A-COMPANION-CHANGE** | `scripts/verify.py:164` `("uv","run","pytest","-q","tests/test_leakage.py")` → `tests/unit/test_p2_leakage.py`. `pyproject.toml:146` `module = "tests.*"` override still applies. Also aligns it with `scripts/verify.py:167` (`tests/unit -k p2`), which today does **not** collect it |

### Step 2 — executable only after a decision is recorded

| # | Command | Bytes | Flag | Companion change required |
| ---: | --- | ---: | --- | --- |
| 3 | `git mv packages/pipeline/tests/__init__.py tests/pipeline/__init__.py` | 56 | **BLOCKED** | Changes what `pyproject.toml:151` `testpaths[1]` resolves to. **Decision required:** either amend `testpaths` to `["tests"]` (preferred — the file contributes zero tests) or accept a second test root. Do not move before that edit |
| 4 | `git mv data/watchlist/sample-sdn-v1.csv tests/contracts_adapters/fixtures/watchlist/sample-sdn-v1.csv` **and** `git mv data/watchlist/sample-sdn-v1.meta.json tests/contracts_adapters/fixtures/watchlist/sample-sdn-v1.meta.json` | 1,154 + 999 | **NEEDS-A-COMPANION-CHANGE** | **BLOCKED on decision.** The path is a *runtime* input read by `oxbow/adapters/{null,ofac}/watchlist.py`, not only a test input. Every `watchlist` path string in `config/sources.yaml`, `oxbow/ports/watchlist.py` and the two adapters must be updated together. Verify with a repo-wide grep for `data/watchlist` before executing |
| 5 | `git mv tests/contracts_adapters/fixtures/rs256_token.json tests/contracts/fixtures/rs256_token.json` | 1,296 | **NEEDS-A-COMPANION-CHANGE** | **AMBIGUOUS — recommend NOT executing.** `scripts/verify.py:237` runs `pytest -q tests/contracts_adapters` as the P7 gate; this file is the only thing in that path. Moving it leaves the gate pointing at an empty directory. Cheaper fix: keep it and `git add tests/contracts_adapters/__init__.py` |
| 6 | `git mv data/graph_measurement.json out/measurements/graph_measurement.json` (+ `ibm_graph_measurement.json`, `ibm_cycle_measurement.json`, `download_manifest.json`) | 2,192 + 2,575 + 4,661 + 841 | **NEEDS-A-COMPANION-CHANGE** | **AMBIGUOUS.** 113 code references across `oxbow/`, `scripts/` and `tests/`. Moving into `out/` also makes them gitignored, which may be wrong: these are the §4 measurement that decides the architecture and are the evidence a judge reads. If they stay, the alternative is to record them in `data/DATASET_CARD.md` instead |

### Step 3 — BLOCKED: decisions, not moves

| # | Proposed | Files / bytes | Flag | Why it is blocked |
| ---: | --- | ---: | --- | --- |
| 7 | `git mv packages/pipeline/oxbow/scoring/* packages/pipeline/oxbow/models/` | 12 / 197,794 | **BLOCKED** | Touches the largest wired surface outside `apps/`: 55 import references across 23 files, 3 of 4 `.importlinter` contracts (`oxbow.scoring` at lines 29, 49, 62, 86), `.pre-commit-config.yaml` `files: ^(packages/pipeline/oxbow/.*\.py)$` (pattern still matches), and `config/scorecard.yaml` which names `oxbow/scoring/config.py` as the reader of every key. **Recommendation: do not move. Amend the T1 tree to list `scoring/`** — it is a deliberate layer, and the contract in `.importlinter` is the proof |
| 8 | `git mv packages/pipeline/oxbow/audit/chain.py packages/pipeline/oxbow/explain/chain.py` | 1 / 11,897 | **BLOCKED** | 14 references across 12 files; `oxbow/explain/` is an empty stub, so this is a design decision about whether the audit chain *is* the explain layer. `oxbow/ports/audit.py` and `oxbow/adapters/audit/postgres.py` already sit on the adapter side |
| 9 | `git rm packages/pipeline/oxbow/publish/__init__.py` | 1 / 75 | **BLOCKED** | Zero references repo-wide, but it is listed at `.importlinter:90` in contract 4 `source_modules`. Deleting it requires that edit in the same commit, and `.importlinter` has `unmatched_ignore_imports_alerting = error` — verify `lint-imports` still passes |

### Step 4 — not moves; creations the tree needs

Ordered by dependency. None is a reorganisation, so none is a `git mv`.

1. `apps/api/worker.py` — required by `Makefile:122`, `docker-compose.yml:195`
   and `jobs.py:103` (`api.worker.run_stages`). Must define `run_stages`.
   **Do not create by moving `jobs.py`** — `jobs.py` is the HTTP submission side
   and is registered as a router in `main.py`.
2. `apps/api/Dockerfile` and `apps/web/Dockerfile` — required by
   `docker-compose.yml:176`, `:192`, `:208`.
3. `scripts/demo_seed.py` + `data/snapshots/demo.dump` — required by
   `Makefile:129`.
4. `data/{raw,interim,processed,snapshots,golden}/.gitkeep` — makes the five
   prescribed/needed `data/` subdirectories survive a fresh clone.
5. `tests/contracts_adapters/__init__.py` — the only `tests/` subdirectory
   missing one.
6. `PROMPT.md`, `notebooks/02_features.ipynb`, `notebooks/03_validation.ipynb` —
   the three missing prescribed paths.
7. `packages/pipeline/oxbow/explain/` modules, or a decision that
   `oxbow/models/explain.py` (13,011 B) is the explain layer and T1's
   `explain/` is redundant.
8. `.github/workflows/*.yml` — T6 CI.
9. `apps/web/vitest.config.*`, `apps/web/playwright.config.ts` and web tests —
   otherwise `make test-web`, `make test-e2e` and the P8 gate are vacuous.
10. `apps/web/src/lib/api/schema.d.ts` — the generated client `.gitignore:68`
    promises is committed.

### Explicitly BLOCKED by the concurrent-edit constraint

| Path | Why |
| --- | --- |
| `packages/pipeline/oxbow/features/bridge.py` (30,042 B) | Under active edit by another agent. **No move proposed, and none may be executed.** It is already at its prescribed path (`oxbow/features/`), so no move is warranted in any case |
| `tests/unit/test_p2_grain_bridge.py` (25,244 B) | Under active edit by another agent. **No move proposed.** Already at its prescribed path (`tests/unit/`) |
| `docs/audit/` (86,009 B) | Being written by another agent. `docs/` is not in the T1 tree, so no move is proposed; if the tree is later amended, decide the fate of `docs/` as a whole, not file-by-file |

### Sanity checks before executing Step 1

- `git grep -n "api\.jobs\|from api import jobs"` → expect exactly 2 hits
  (`main.py:46`, `routers/runs.py:31`).
- `git grep -n "tests/test_leakage.py"` → expect exactly 1 hit
  (`scripts/verify.py:164`).
- `uv run lint-imports` after any `.importlinter`-adjacent move (steps 3 and 9).
- `uv run ruff check .` after step 1 — `pyproject.toml:117` and `:101` key off
  path globs that survive the move, but confirm.

---

## Appendix — stale claims found while indexing

Recorded because a reorganisation executed against them would be working from a
wrong picture. None of these was edited.

- `STATE.md:116-119` — "tests/contracts/, tests/integration/, tests/e2e/ are
  empty" and "scripts/verify.py names tests/contracts_adapters/, which does not
  exist yet". Measured now: `tests/contracts/` has 4 files, `tests/integration/`
  has 4 files, and `tests/contracts_adapters/` exists (fixture only, no tests).
  Only `tests/e2e/` is still empty.
- `STATE.md:313` pins its measurement to commit `0e8a5b8`; HEAD is `1dc2499`.
- `docs/` contains no prose documentation at all — the 10 prescribed documents
  are all at the repository root, which matches T1, so `docs/` is a directory
  with a single audit subdirectory and nothing else.
