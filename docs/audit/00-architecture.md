# OXBOW architecture conformance audit

Produced by the `architect` subagent against plan v2.1 (authority order: 00 Constitution > 01 Build Prompt > 03 Edge Cases > 02 Integration > Spec v2.1).
Tree state: read-only inspection while the repository was being committed to by other agents; HEAD moved during the audit (2e8b4b2 -> 1dc2499).

---

# OXBOW Architecture Conformance Audit
**Auditor scope:** static inspection only (no shell in this tool context — every runtime claim is marked `UNVERIFIED`). Tree clean at `2e8b4b2`. Read-only; no mutations. `bridge.py` / `test_p2_grain_bridge.py` read only, never edited.

**Verdict codes:** `IMPLEMENTED` · `PARTIAL` · `ABSENT` · `VIOLATED`

---

## 1. The seven ports and adapter families (plan §13, 02 §A/§E)

### 1.1 Port existence and `Null*` coverage — **IMPLEMENTED (8/8)**

| Port | Protocol | `Null*` | Null class site |
|---|---|---|---|
| `source.py SourceAdapter` | `ports/source.py:99` | ✅ | `adapters/null/source.py:30` `NullSourceAdapter` |
| `objectstore.py ObjectStoreAdapter` | `ports/objectstore.py:90` | ✅ | `adapters/null/objectstore.py:26` `NullObjectStore` |
| `case_sink.py CaseSink` | `ports/case_sink.py:344` | ✅ | `adapters/null/sinks.py:18` `NullCaseSink` |
| `notify.py NotifySink` | `ports/notify.py:110` | ✅ | `adapters/null/sinks.py:25` `NullNotifySink` |
| `report.py ReportSink` | `ports/report.py:118` | ✅ | `adapters/null/sinks.py:32` `NullReportSink` |
| `watchlist.py WatchlistAdapter` | `ports/watchlist.py:171` | ✅ | `adapters/null/watchlist.py:30` `NullWatchlistAdapter` |
| `audit.py AuditSink` | `ports/audit.py:38` | ✅ | `adapters/null/audit.py:17` `NullAuditSink` |
| `warehouse.py WarehouseSink` | `ports/warehouse.py:153` | ✅ | `adapters/null/warehouse.py:48` `NullWarehouse` (naming drift, cosmetic) |

All seven adapter **families** named in plan §13 exist: `null/` `file/` `s3/` `webhook/` `slack/` `goaml/` `ofac/`. `ofac/watchlist.py:1-8` and `ports/watchlist.py:184-187` correctly make `automatic_decision_authority == False` structural.

Minor deviation, justified in-tree: `ports/audit.py:30` imports `oxbow.audit.chain` (first-party). Plan says ports are "zero dependencies". import-linter contract 2 (`.importlinter:41-51`) does not forbid it, so the contract is written weaker than the prose. Not a defect, but a hole in the contract.

### 1.2 Contract tests — **ABSENT. This is the single highest-value finding.**

`tests/contracts_adapters/` contains exactly one file: `fixtures/rs256_token.json`. **Zero test files.**

- The plan's own P7 gate names that directory: `scripts/verify.py:235-238` → `("uv","run","pytest","-q","tests/contracts_adapters")`. With no test module, pytest exits `5` (no tests collected) — the gate **cannot pass as written**.
- The prose claims it exists: `apps/api/routers/cases.py:482` says *"Screening itself is exercised against a named query in `tests/contracts_adapters/test_watchlist_conformance.py`"* — **that file does not exist**. A source file cites a nonexistent test as its justification.
- `scripts/verify.py:236` gate description: *"port conformance across every adapter"*.
- The only conformance assertion anywhere is a structural `isinstance` against one adapter: `tests/unit/test_p1b_ibm_aml.py:1395-1404`. That proves the IBM adapter has the right *shape*; it does not run null-vs-real through shared behavioural assertions.
- Plan §13 requires *"pytest parametrised over every adapter implementing a port"*. There is **no `parametrize` over adapters anywhere** in the repo.
- Three adapter families have **zero** test references: `adapters/slack/`, `adapters/goaml/`, `adapters/ofac/` (grep of `tests/` for `slack|goaml|ofac` returns no adapter hits).
- Companion plan §13 requirements also absent: **Schemathesis** is a declared dev dep (`pyproject.toml:64`) with no test and no Makefile target; **Toxiproxy** has zero repo references; **toxiproxy/schemathesis in CI** — see §3.

Net: every port, every `Null*`, and every adapter family exists and is written carefully, but **nothing pins them to each other.** The "behaviourally interchangeable" claim in `ports/source.py:102-104`, `ports/objectstore.py:94-96`, `ports/audit.py:5-7` and `adapters/null/__init__.py:4-7` is asserted in prose only. This is 00 §B's exact failure mode (a claim with no test is decoration) applied to the part of the plan §13 calls "the part judges interrogate".

---

## 2. Internal seams (02 §B)

### Seam 3 — feature-spec hash written with the table; mismatch refuses to score — **IMPLEMENTED**
- Hash stamped on the frame: `scoring/frame.py:52` `COL_SPEC_HASH`, validated `scoring/frame.py:495-510` (one value per frame, then compared to `canonical_spec_hash(registry)`).
- Single definition (the STATE.md punch-list item 3 fix landed): `scoring/frame.py:99-120` delegates to `registry.spec_hash`.
- Refusal at scoring time: `scoring/model.py:496` `require_feature_hash_match(model.feature_spec_hash, frame.feature_spec_hash)`.
- Tests bite: `tests/test_leakage.py:489` `test_feature_hash_mismatch_refuses`; `tests/unit/test_p2_features.py:684`; `tests/unit/test_p2_spec_hash.py:305-320` (mismatch names both digests in the message).
- Verified as *not* the old bug: `scoring/generated.py:59,366` now imports `canonical_spec_hash` rather than re-deriving it.

### Seam 4 — MLflow model URI **+** version on every scored row — **PARTIAL**
- In the in-memory scored frame: `models/run.py:114-155` `SCORED_ROW_COLUMNS` includes `model_uri`, `model_version`, `mlflow_run_id`, `model_fingerprint`, `tracking_degraded`; populated from `lineage.to_row()` at `models/run.py:876-906`.
- **The URI never reaches the warehouse.** `adapters/warehouse/models.py:331-363` (`Score`) has `model_version` at `:363` and **no `model_uri` column**; the writer `adapters/warehouse/postgres.py:58,72` passes only `model_version`. Grep for `model_uri` across the whole repo returns 11 hits, **all** in `models/` — none in `adapters/warehouse/`, none in `apps/api/alembic/`.
- `models/run.py:881` defaults `model_uri: None` whenever lineage is absent, so even the parquet artifact can carry a null URI.
- Verdict: the *version* half of the seam survives the handoff; the *URI* half dies at the Postgres boundary. Plan §10: *"MLflow model URI + model version stored on every scored row"*.

### Seam 5 — the API never recomputes a score — **IMPLEMENTED, with two documented arithmetic exceptions**
- No model/feature/scoring library is importable from `apps/api`: grep for `lightgbm|optbinning|shap|score_frame|np\.` over `apps/api/**/*.py` returns **no imports** (only `ortools` availability *probing* at `deps.py:431`, and `backtest.metrics.monte_carlo_tail_risk` at `policy_engine.py:41`).
- The read path takes stored values verbatim: `routers/cases.py:149-150` reads `score["fused_score"]` and `score["scorecard_points"]`; `:153-156` reads the stored calibration band, observed rate and `n`; `:168` reads stored `shap_contribution` rows; `:174` reads the stored `policy_allocation.rank`.
- The one real bridge is explicit about its limits: `policy_engine.py:1-25` states no scoring, no EV arithmetic, no VaR maths, and the only recomputation is re-pricing at a different recovery rate, which the response states. Sanctioned by plan §14 screen 6 (*"the server re-allocates for real … nothing here is precomputed theatre"*).
- Two derived figures, both defensible but worth naming:
  - `routers/cases.py:499-559` `counterfactual()` re-derives `total = sum(points)` and a band from `band_definition`. It never overwrites the reported score. **However it does not cross-check its own `total` against the stored `scorecard_points` it already has at `:150`** — a silent divergence would be invisible. Suggested: assert equality and return `None` (or surface the delta) when they disagree.
  - `routers/dashboard.py:260-289` `_high_risk_networks()` counts stored communities ∩ band-D/E. It does not re-cluster and self-documents the basis (`:287-288`).
- **No seam-5 violation found.** Highest-severity candidate checked and cleared.

### Seam 8 — `design/tokens.ts` generated from `tokens.css`, CI fails on drift — **PARTIAL**
- Generator exists and is correct: `apps/web/src/design/icons/build.mjs:30-31,166,218` reads `../tokens.css`, throws if no `@theme` block, writes `../tokens.ts` with a `// GENERATED FILE` banner (`:45`).
- Committed output carries the banner: `apps/web/src/design/tokens.ts:4`.
- Test asserts the property: `tests/unit/test_p0_design_system.py:367-378` requires the banner and every band value.
- Drift is enforced **only by pre-commit**: `.pre-commit-config.yaml:92-100` → `cd apps/web && pnpm run icons && git diff --exit-code -- src/design/tokens.ts src/design/icons/sprite.svg`.
- **`CI fails on drift` is ABSENT: `.github/workflows/` is an empty directory** (see §3). Whether the tree is in sync right now is `UNVERIFIED` (needs `pnpm run icons`).
- Related: `apps/web/biome.json:5` ignores `src/design/tokens.ts` and `src/design/icons/sprite.svg` from Biome.

---

## 3. `import-linter` contract (plan §5 T7)

**Contract: IMPLEMENTED (4 contracts, wider than T7 asked for).** `.importlinter` exists with four `forbidden` contracts:

| # | Name | Line | Covers |
|---|---|---|---|
| 1 | core-does-not-import-adapters | `.importlinter:21` | `features graph rules models scoring quant backtest explain` → `adapters` (T7's four, plus four more) |
| 2 | ports-are-dependency-free | `:41` | `ports` → `adapters models scoring quant` |
| 3 | scoring-has-no-http-client | `:57` | `models scoring quant features rules backtest` → `httpx requests urllib3 aiohttp boto3` |
| 4 | cli-is-the-composition-root | `:78` | the eight core packages + `publish` + `ports` → `adapters` |

`include_external_packages = True` (`:15`) is required for contract 3 and is correctly set. `unmatched_ignore_imports_alerting = error` on all four.

**Pass/fail: UNVERIFIED.** I have no shell. The most recent in-repo evidence is `STATE.md:280` (*"import-linter: 4 kept, 0 broken over 197 files / 925 dependencies"*, recorded in the punch list at `STATE.md:150`) — a prior observation, not a verification by me.

**Wired into:** `Makefile:146-148` `contracts` target → also a leg of `make lint` (`:134`) → and pre-commit (`.pre-commit-config.yaml:38-46`).

**But the CI leg is absent.** Plan §5 T6 requires GitHub Actions with `lint, typecheck, unit tests, 50k-row pipeline smoke, one-fold backtest smoke, image build, pip-audit, pnpm audit, schemathesis, import-linter`. **`.github/workflows/` is an empty directory** (`read` returned 0 entries). So every T6 item is `ABSENT`, and every "CI fails on X" clause in the plan (seam 6, seam 8, schema drift) has no enforcement point.

---

## 4. Pipeline → Postgres → API → web handoff

**IMPLEMENTED. No route recomputes a score, a feature, or a model output.**

Evidence, in the order the data flows:

- **Write side is the only producer.** `adapters/warehouse/models.py` defines the full read model (`score:339`, `scorecard_point:369`, `rule_hit:390`, `shap_contribution:417`, `economics:443`, `evidence_event:474`, `graph_edge:528`, `community:557`, `ablation_row:917`, `validation_metric:938`, `fairness_row:1005`, `perturbation_row:1021`, `policy_allocation:776`, `backtest_fold:879`, …). Migrations `apps/api/alembic/versions/0001_*.py` and `0002_integrity_triggers.py` create and protect them (`0002:143` blocks a `model_version` UPDATE; `0002:66` includes `shap_contribution`).
- **Read side only selects.** `apps/api/readmodel.py:269,467` build `select(func.count())` paging; `routers/*.py` pass `where=`/`order=`/`limit=` and nothing else. Grep for `func.sum|func.avg|sum(|/ 100|round(` across `apps/api` returns **8 hits total**: two paging counts (`outbox.py:163,416`, `readmodel.py:269,467`), one counterfactual sum (`cases.py:529`), one chip delta (`dashboard.py:230`), one `sum(losses)` in the simulator (`policy_engine.py:510`), one in-memory set count (`dashboard.py:280`). No model is invoked.
- **Money never leaves as a float**, and that is tested: `tests/integration/test_p7_api.py:1501` `test_money_never_leaves_the_api_as_a_float`; the `Money` shape is `{minor, currency, decimals}` (`apps/web/src/lib/api/contract.ts:43-45`).
- **Pseudonymity enforced at the edge**: `tests/integration/test_p7_api.py:1534` `test_only_pseudonymous_account_keys_reach_a_response`; redaction filter at `apps/api/observability.py:105-162`.
- **The frontend cannot invent a shape**: `apps/web/src/lib/api/contract.ts:1-24` declares every field once with a runtime decoder; `client.ts:63` rejects a non-`{data, meta}` envelope; `tests/unit/test_anti_rubbish.py:103` `test_no_any_in_frontend_source`; `biome.json:35` `noExplicitAny: "error"`.

**One gap, not a seam-5 break:** the generated OpenAPI client the plan §13 requires (`openapi-typescript` → committed → `git diff --exit-code`) **does not exist**. `apps/web/src/lib/api/schema.d.ts` is un-ignored at `.gitignore:68` but is absent from the tree; `apps/web/package.json:9-19` has no generate script; `openapi-typescript` sits unused at `package.json:62`. `contract.ts:19-23` still lists `SEAM(P7)` reconciliations. The typed error union is therefore hand-written (`contract.ts:26` imports a local `Decoder` from `../codec`), not generated — which satisfies "not `any`" but not "generated in CI".

---

## 5. The web app contract (plan §19 rule 3, §14 gate)

**Verdict: `IMPLEMENTED` for numbers. `VIOLATED` for tunables (a different rule, §16/T10).**

### 5.1 Numeric literals in JSX — no displayable number is hardcoded

I grepped every `.tsx` under `apps/web/src` for JSX-embedded numbers, `toFixed(n)`, `NN.N%`, `oklch(`, hex and `rgb(`. Classification:

| Class | Count | Examples |
|---|---|---|
| Decimal places on an API value | ~30 | `alerts/page.tsx:477` `row.score.toFixed(3)`; `model/page.tsx:116,138,351,392`; `cases/[id]/page.tsx:316,488,638`; `scorecard/page.tsx:157,253-255,303,328,341`; `charts/charts.tsx:503,564,571` |
| Unit conversion of an API value | ~12 | `alerts/page.tsx:481-482` `observed_rate * 100`; `dashboard/page.tsx:171` `population_share * 100`; `cases/[id]/page.tsx:321,371-372,602`; `BandBadge.tsx:66`; `MarkArc.tsx:84-85` |
| Layout geometry (scale-adjacent) | ~12 | `charts/charts.tsx:33-36` `CHART_HEIGHT=220`, `MARGIN={12,12,28,52}`; `alerts/page.tsx:56` `ROW_HEIGHT=172`; `Skeleton.tsx:44-45`; `MarkArc.tsx:38` `ARC_RADIUS=40`; `network/page.tsx:57` `CANVAS_HEIGHT` |
| Documented plan constant | 2 | `Shimmer.tsx:34` `SHIMMER_PERIOD_S = 1.4` (= plan §14 "1.4 s loop"); `MARK_ARC` = the one licensed determinate spinner |
| **Test fixture** | all of `src/fixtures/**`, `src/app/dev/states/**` | explicitly permitted by §19 rule 3 |

**Zero displayable domain figures found.** Every visible number traces to `payload.*` / `answer.*` / `data.*` / `row.*`. Verified specifically for the highest-risk spots:
- The capacity cutoff line reads stored values, not a constant: `alerts/page.tsx:537-543,579-580` (`cutoff_rank`, `unreviewed_count`, `unreviewed_exposure`, `minutes_available`, `period_label`, `policy_label`).
- The subgraph cap is **server-owned**: `network/page.tsx:201-202,326` render `{count(data.cap)}` — the 1,500 never appears as a literal in a `.tsx`.
- The disclaimer, bands, and the twelve glyph names are enum/`const` tables, not numbers: `ports/case_sink.py:39-45` is mirrored through `provenance.tsx:158`, `StageLedger.tsx:54-62`, `Icon.tsx:41-60`.

Fixtures cannot leak into a demo: `lib/api/transport.ts:51-60` forces `mode: 'api'` in any production build unless `OXBOW_ALLOW_FIXTURE_BUILD=1`, and every fixture response carries `provenance: 'fixture'`, rendered as a permanent banner (`transport.ts:10-15`).

### 5.2 Tunables that *are* in code — `VIOLATED` against plan §16 / T10

These are not §19-rule-3 numbers, but §16 requires *"every tunable value lives in `config/` or `design/tokens.css`, not in code"*:

| file:line | Value | Note |
|---|---|---|
| `apps/web/src/app/global-error.tsx:24,25,32,42,57,58,59,67` | 8 hardcoded OKLCH triples | `global-error.tsx` renders outside the app CSS context by Next.js design, so some inline colour is unavoidable — but these **duplicate** `tokens.css` values (`canvas`, `ink`, `ink-muted`, `hairline-strong`) and will drift silently. Nothing checks them. |
| `apps/web/src/app/(dash)/scorecard/page.tsx:377` | `` `oklch(0.78 ${(0.04 + share*0.14).toFixed(3)} ${from===to ? 195 : 40})` `` | An **inline OKLCH ramp in a `.tsx`**, computed from a data value, not a token. It also encodes magnitude in chroma+hue with no token behind it, and the hue flip on `from === to` is a second encoding channel with no legend. This is the closest thing to a §14 "risk by colour alone" risk I found, mitigated but not eliminated by the adjacent band letters. |
| `apps/web/src/app/(dash)/scorecard/page.tsx:37` | `IV_BAR_MAX = 0.65` | A chart scale, undocumented in `config/`. Plan §10 sets the admission band at `0.02 ≤ IV ≤ 0.5`; 0.65 as a bar ceiling is unrelated and unexplained. |
| `apps/web/src/app/(dash)/network/canvas.tsx:312` | `numIter: built.length > 800 ? 1_500 : 3_000` | Layout-performance tuning in a component. 800 is nowhere. |
| `apps/web/src/app/(dash)/alerts/page.tsx:57` | `PAGE_SIZE = 100` | Client page size; arguably a server/read-model concern. |

**Also absent:** the P8 gate is unrunnable, so "no route renders a value not in the API response" is unverified in practice —
- No `playwright.config.ts` (glob: only `postcss.config.mjs` and `next.config.ts` exist under `apps/web`).
- No `vitest.config.*` and **no `.test.ts`/`.spec.ts` file anywhere under `apps/web`**.
- Therefore `Makefile:160-162` `make test-web` (`pnpm test:unit --run`) and `:164-166` `make test-e2e` have nothing to run; `verify.py:250-253` P8 gate is the same command.
- `STATE.md:21` says this honestly ("no build/browser evidence, no test config"). Confirmed.

---

## 6. The outbox pattern (plan §13, 02 §E)

**Verdict: `IMPLEMENTED`. This is the best-executed seam in the repo.**

| Requirement | Status | Evidence |
|---|---|---|
| Audit row + outbox row in **ONE** transaction | ✅ IMPLEMENTED | `decisions.py:209-213` (*"The caller owns the transaction; this function only flushes"*), `decisions.py:291-313` (decision → audit → outbox, all flush-only); commit is the router's single `session.commit()` at `routers/decisions.py:78`. Tested: `tests/integration/test_p7_api.py:1638` `test_a_decision_commit_writes_the_three_rows_together` and `:1694` `test_outbox_write_failure_rolls_back_the_audit_and_decision_rows` (the rollback direction, which is the one that matters). |
| Idempotency `sha256(run_id+case_id+decision_seq)` | ✅ with a documented fix | `ports/case_sink.py:56-65` adds a `\|` delimiter and *explains why* (`ab|c` vs `a|bc` collision). Same key computed at `decisions.py:225`; stored on `OutboxMessage.idempotency_key`. |
| Retry 1/4/16/64/256 s **full jitter**, 5 attempts, dead-letter | ✅ IMPLEMENTED | `adapters/retry.py:27-30` constants; `:51-58` `ceiling_seconds`; `:61-74` `plan_delay` = `uniform(0, ceiling)`; `:67-68` exhausted at 5. One definition, consumed by the outbox (`apps/api/outbox.py:40-45`) so the two cannot drift. Dead-letter at `outbox.py:20-23`; 4xx is **not** retried (`retry.py:77-85`, `outbox.py:20-23`) — which is also plan §18's "a retry loop around a 4xx" rejection trigger. |
| Ordering per `case_id` **only** | ✅ IMPLEMENTED | `outbox.py:160-183`: a `NOT EXISTS` guard on earlier `case_seq` for the same `case_id`, expressed in SQL so two workers cannot both claim adjacent rows, plus `FOR UPDATE SKIP LOCKED` (`:182`). `order_by(case_id.asc(), case_seq.asc())` (`:180`) is a *batching* order, not global ordering. `decisions.py:665-670` documents that global ordering would mean a single-threaded drain. Tested: `test_p7_api.py:2055` `test_outbox_ordering_is_per_case_and_never_global`. Bonus: `:187-192` reclaims rows stranded `in_flight` by a dead worker after 300 s. |
| `X-OXBOW-Signature: t=<unix>,v1=<hex>` over `t + "." + raw_body` | ✅ IMPLEMENTED | `adapters/signing.py:32,60-63,66-76`. Signed over **raw bytes**, never reparsed JSON (`:9-12` explains why). One implementation, both directions: `signing.py:15-22` + `test_p7_api.py:2321` `test_the_outbox_uses_the_same_implementation_the_receiver_verifies_against` (an `inspect.getsource` assertion, not a re-derivation). |
| **Constant-time** compare | ✅ IMPLEMENTED | `signing.py:123` `hmac.compare_digest`. Tested: `test_p7_api.py:2274` `test_the_replay_window_is_300_seconds_and_the_compare_is_constant_time`; also `apps/api/security.py:105`. |
| 300 s replay window | ✅ IMPLEMENTED | `signing.py:38` `REPLAY_WINDOW_SECONDS = 300`; enforced `signing.py:117-120`. Tested end-to-end over HTTP: `test_p7_api.py:2197` `test_a_stale_timestamp_is_refused`. |
| The database is the only source of truth | ✅ IMPLEMENTED | `outbox.py:1-7` (restart resumes from `status`/`next_attempt_at`, never from process memory), `:185-192`. |
| Payload travels with its assumptions | ✅ IMPLEMENTED | `ports/case_sink.py:311-340` `assert_self_describing`, called by all three sinks (`case_sink.py`, `notify.py:102-106`, `report.py:109-114`). The disclaimer and `advisory_only` are **added by the payload builder**, not accepted as input (`case_sink.py:230-236`). |
| Report totals reconcile against rows | ✅ (bonus, 02 §A) | `ports/report.py:72-89` `verified()` recomputes and raises rather than trusting a caller's arithmetic. |
| **Schemathesis** from OpenAPI | ❌ **ABSENT** | dep at `pyproject.toml:64`; zero references elsewhere. P7 gate "Schemathesis clean" unmeetable. |
| **Toxiproxy** in the integration suite | ❌ **ABSENT** | zero repo references. |
| OpenAPI → `openapi-typescript` → `git diff --exit-code` in CI | ❌ **ABSENT** | see §4. |

Also: four-eyes gating is real — `decisions.py:222` computes `four_eyes_required` from `economics.four_eyes.threshold_exposure_minor`, `:308-312` refuses to queue the outbox row until confirmation, and `test_p7_api.py:1835` proves a second *subject* is required (not merely a second role). `decisions.py:304-305` / `:284-289` implement reversal-as-new-row and one-success/one-409.

---

## 7. Identity model (02 §B)

| Id type | Status | Evidence |
|---|---|---|
| `txn_id` namespaced (`paysim:12345`) | ✅ IMPLEMENTED | `ports/warehouse.py:63` `TXN_NAMESPACE=":"`; `ports/warehouse.py:125-139` `assert_txn_id_namespaced` refuses an unqualified id on the `transaction` table; `ports/source.py:32-50` canonical field list; `contracts/canonical_v1.py` shape check. Tested `test_p1b_canonical_ingest.py:744` `test_no_cross_source_node_merge`. **DEV-004 satisfied.** Note `ingest/canonical.py:185-195` `node_key()` adds a corpus discriminator for the *graph* while leaving `account_key` alone — a correct and documented fix for a real cross-corpus merge hazard. |
| `run_id` is a **ULID**, not a uuid (DEV-003) | ✅ IMPLEMENTED | Real ULID, stdlib-only: `identity.py:37-45` (48-bit ms + 80-bit entropy, Crockford base32, no dashes, `_ULID_LEADING_CHAR_MAX = 7`), `:92-112` `new_ulid`, `:115-129` `is_ulid`, `:131-136` `require_ulid`, `:140-143` `ulid_to_datetime_utc`. Stored as `CHAR(26)` text: `adapters/warehouse/models.py:16,94`; enforced at the write boundary `ports/warehouse.py:87-104` `assert_run_id`; refused in routes `apps/api/readmodel.py:867-869`, `apps/api/decisions.py:137-138`; schema field `apps/api/schemas/events.py:43` `min_length=26, max_length=26`. Persistence trigger `0002_integrity_triggers.py:297` names DEV-003 in the DB. **DEV-003 satisfied.** Tested `test_p1b_ibm_aml.py:1268`. |
| `case_id` | ✅ IMPLEMENTED | `adapters/warehouse/models.py:1056` `CHAR(RUN_ID_LEN)`; generated `decisions.py:194-197` from `python-ulid`. Table renamed `review_case` with the reason at `:1040-1041` (`case` is reserved in Postgres). |
| `account_key` = 12-hex derivation, not a raw id | ⚠️ **PARTIAL** | See below. |

**`account_key` — the one identity defect.** Plan §6/02 §B: `account_key = sha256(account_id + RUN_SALT)[:12]`, **uppercased**, displayed `ACC-7F2A19`.

What ships:
- `ingest/canonical.py:152-171` — **HMAC-SHA256** keyed by `run_salt` over the string `f"{run_salt}|{raw_name}"`, truncated to 12 chars. Not plain `sha256(id + salt)`, and the operand order is reversed (salt first).
- `contracts/canonical_v1.py:165,294` — pattern `^[0-9a-f]{12}$`: **lowercase**, and the schema *enforces* lowercase.
- `display_account_key()` at `canonical.py:174-182` uppercases only at render, with the right reason (`:177-180`: two spellings in two places is how a join returns zero rows). So the plan's *"displayed `ACC-7F2A19`"* is met, while *"uppercased"* as a storage property is not.
- The HMAC choice is **better** than the plan and is argued at `canonical.py:160-167` (an unsalted SHA-256 of `C1231006815` is reversible by hashing candidates) and pinned by an independent oracle (`test_p1b_ibm_aml.py:847` `oracle_account_key`), which is 00 §B's rule honoured. I would not revert it — but it should be a logged decision, not an undocumented one.
- **Derivation drift risk (3 implementations):**
  1. `ingest/canonical.py:169` — HMAC over `salt|name`, 12 lowercase hex.
  2. `scripts/measure_ibm_cycles.py:175` — **plain** `hashlib.sha256(f"{run_salt}|{account}")`, full-length digest, no HMAC. A *different* function producing account keys from the same corpus.
  3. `ingest/run.py:559-571` `content_batch_id()` — reuses `ACCOUNT_KEY_LENGTH` as a truncation length for a batch id. Two unrelated ids sharing one length constant is a coincidence that will read as intent.
- `RUN_SALT` never in the repo: `config.py:148-161` `require_run_salt` fails loud when absent, `config.py:188-209` `resolve_run_salt` reads env then `.env`; `ingest/canonical.py:116-119` rejects a salt under 16 chars. Tests isolate it (`test_p0_toolchain.py:117-124`, `test_p7_api.py:98,124`).

---

## 8. Phase-gate readiness

`STATE.md`'s table vs the plan's gates. `scripts/verify.py:68-267` holds the machine-readable phase table; it is **honest** (`done=True` only for P0, P1a, P1b, P3a, P5).

| Phase | Plan's gate demands | Repo plausibly satisfies? | `STATE.md` claim | My verdict |
|---|---|---|---|---|
| **P0** | `make bootstrap && make up && make lint && make test` exits 0 · **CI green on first push** · `oxbow --help` lists all four verbs (01 P0); T1 tree, T2 pins, T3 16 Make targets, T4 pinned digests, T5 float-money lint, T6 **GitHub Actions**, T7 import-linter, T8 four verbs, T9 Alembic, T10 tokens + 12 glyphs, T11 bookkeeping | **NO — overclaimed.** `verify.py:69-82` defines P0 as only 3 narrow commands. The plan's own gate includes `make lint` and CI, and `STATE.md:280` records `make lint` as **red** (85 ruff errors, 133 files unformatted, 304 mypy errors). `.github/workflows/` is **empty** → T6 and "CI green" have no artifact. `PROMPT.md` (T1, *"missing files are not [fine]"*) is **absent** from the repo root. T4: images are tag-pinned (`postgres:16.4-alpine`, `redis:7.4-alpine`), not digest-pinned. T8: `STAGES == ("ingest","graph","score","backtest")` holds (`test_p0_toolchain.py:31-35`) but `--help` renders **9** verbs (`STATE.md:98-99`), so T8's *"lists exactly"* is deviated (T3 wants the extras, so the two clauses conflict — needs a `DECISIONS.md` line). T9 ✅ `apps/api/alembic.ini` + 2 migrations. T10 ✅ 12 glyphs in `design/icons/src/`, banner in `tokens.ts`. T7 ✅ §3. | **DONE**, proof `verify.py --phase P0` 2/2 | ❌ **OVERCLAIM (not flagged in `STATE.md`).** `P0 DONE` is incompatible with `make lint` red and no CI. |
| **P1a** | SHA-256 verified; §4 measurement recorded (00 §D day-3) | ✅ plausible. `verify.py:84-97`; measurement artefacts exist at `data/graph_measurement.json`, `data/ibm_graph_measurement.json`, `data/ibm_cycle_measurement.json`, `data/download_manifest.json`; DEV-011's verdict is written up honestly at `STATE.md:38-62` including the star-shaped-PaySim finding and the IBM fallback. | DONE | ✅ **Confirmed, well earned.** |
| **P1b** | Pandera zero silent coercions · printed counts match the card **exactly** · `pytest tests/contracts` green · EDA states counterparty reuse | ⚠️ PARTIAL. `verify.py:98-120` runs both corpora (good — the IBM gap was closed). `data/DATASET_CARD.md` is now **verify-not-write** (`dataset_card.py:1,47`; `eval.py:1776`) which is the right fix. But `STATE.md:242-252` records the card's own figures were wrong and that PaySim `step` runs **1–743, not 0–30**, which contradicts `config/pipeline.yaml` and `LIMITATIONS.md`'s "the corpus window is 18 days" — **still unresolved and load-bearing** (`:249-252`). So "printed counts match the card exactly" does not yet hold. | DONE, 3/3 | ⚠️ **Mildly overclaimed** — the card is self-consistent but the `step` semantics conflict with config, and the gate's "match exactly" clause is not demonstrable until that is decided. |
| **P3a** | cycle detection matches a hand-built fixture · degree distribution + top-20 printed | ✅ plausible. `verify.py:121-155` includes a **determinism double-run** over `out/graph` — an unusually good gate. `graph/cycles.py` reviewed at `STATE.md:322-326` (single-currency, rotation dedup, budget sets `cycle_search_truncated` rather than failing). | DONE, 2/2, 80 tests | ✅ **Confirmed.** |
| **P2** | feature table < 90 s on dev slice · `tests/test_leakage.py` proves no future read · **a deliberately leaking feature is caught** | ✅✅ **The strongest gate in the repo.** `verify.py:157-170`. `test_leakage.py:386` `test_the_gate_catches_a_deliberately_leaking_feature`; `:403` at every probe; `:417` the honest twin passes; `:440` a forward window may not declare itself a model input; `:460` a label-derived feature is refused at load; `:653` the gate reads the *shipped* registry, not a test copy. This is 00 §B's "proven to bite" clause, executed. | "in progress" | ✅ **Correctly not claimed.** (The `verify.py` 90 s clause is not enforced as a timing gate — worth noting.) |
| **P3b** | every rule fires on its planted case and on **none** of the near-misses · cycles time-respecting · per-rule hit counts sane · no rule on > ⅓ of accounts | ✅ plausible, with one live scientific dispute. `tests/golden/expected.yaml` covers R1–R12 with ≥1 positive and ≥1 near-miss each (`STATE.md:295`), built hand-computed (`tests/golden/build_fixture.py`, 1,700+ lines) and self-checked by `test_golden_fixture_selfcheck.py` — 00 §B's "a fixture whose expected value came from the code it tests proves nothing" honoured. **DEV-015 is live**: `STATE.md:165-175, 201-235` establishes R4's §9 definition is blind to ~70 % of the corpus's labelled cycles, and that the decision's own claim ("score zero") was **refuted** by measurement (union is 51 of 54, not 54). That is an honest, well-documented, plan-boundary conflict — the right handling, and it belongs in the report's DEVIATIONS block. | "in progress" | ✅ **Correctly not claimed.** |
| **P4** | reliability curve, Brier, PR-AUC + bootstrap CI, KS logged · **scorecard reproduces 600 at 50:1 in a unit test** · scorecard-vs-GBM matrix renders · no zero-bad bin unmerged | ❌ **WORSE than `STATE.md` says.** `STATE.md:17` says "code present, not verified by the orchestrator". I can strengthen that: `verify.py:192-196` runs `pytest -q tests/unit -k p4`, and **there is no `test_p4_*.py` file anywhere**. Zero tests are selectable, so the gate cannot pass even if run. The scoring code is substantial and `scoring/scale.py` + `config.py` are reviewed as sound at `STATE.md:311-321` (including the PDO/offset arithmetic and the honest "minus 48 points" sign argument) — but one wart is named there: `:319-321` `int()` **truncates** when `round_points_to_integer` is false. | "code present, not verified" | ✅ **Honest, but understated.** No P4 test file exists at all. |
| **P5** | CP-SAT and greedy agree within **2 % of total EV** on the golden fixture · capacity sweep monotone non-decreasing · zero-capacity and all-negative-EV both return a documented result · no currency figure without its assumption line | ✅ plausible. `verify.py:198-218` runs 5 named files. `quant/ev.py` is reviewed as sound at `STATE.md:326-333` (integer minor units, probabilities as micro-ratios so `Money * micro/1e6` never touches a float; **currency disagreement raises rather than converts**; sub-floor `m_i` raises; ordering `(-density_ratio, account_key)`). Named tests `test_zero_capacity_returns_forgone_value` etc. are described at plan §11 — the P5 tests exist (`test_p5_economics/allocate/exposure/frontier/monte_carlo.py`). | DONE, 1/1, 89 tests | ✅ **Confirmed.** |
| **P6** | five folds from one command · ablation table with CIs · a **deliberately lookahead-leaking control visibly outperforms** (proving the harness detects leakage) · results serialise to JSON the validation page reads | ⚠️ PARTIAL. `verify.py:219-229` runs `-k p6`; `tests/unit/test_p6_metrics.py` exists, so the gate is runnable. `backtest/fakes.py:188,288,318` provides a fake scorer incl. a `fake-control-lookahead` variant (`:288`), which is the right shape for the control. But `STATE.md:19` is explicit: `data/processed/eval.json` exists and *"its numbers have not been reproduced from a command here"*, and `STATE.md:306-309` lists "P6's ablation" as unverified. | "in progress" | ✅ **Correctly not claimed.** |
| **P7** | `openapi-typescript` client with a **typed error union, not `any`** · forced failure per router returns `problem+json` **with a run id** · SSE mid-run kill + reconnect resumes without duplicate rows · two concurrent decision writes → one 200 and one 409 | ⚠️ PARTIAL. **Genuinely strong:** `tests/integration/test_p7_api.py` really serves the app and has 34 tests, including `:1084` OpenAPI + problem union, `:1199-1287` every error tier, `:1565` SSE resume without duplicates, `:1932` `test_concurrent_append_gives_one_success_and_one_409`, `:2113` the null path refuses loudly, plus 9 session-hygiene tests. Four-eyes, outbox atomicity and signing are all covered (§6). **Gaps:** the generated client does not exist (§4) and the port-conformance gate cannot run (§1.2). `apps/api/worker.py` (plan C2 commit matrix) is **ABSENT**, so `Makefile:122` `make worker` and `docker-compose.yml:195` (`command: ["python","apps/api/worker.py"]`) both point at a missing file, and the `worker` service in the `full` profile cannot start. `apps/api/Dockerfile` (referenced `docker-compose.yml:176`) and `apps/web/Dockerfile` (`:208`) are also **absent** — only `Dockerfile.echo` exists. | "partially executed" | ✅ **Honest.** One thing `STATE.md` does not flag: `worker.py` and the two Dockerfiles are missing, so the `full` Compose profile is non-runnable, not merely untested. |
| **P8** | Playwright screenshots over `/dev/states` green · **axe zero critical** · **measured CLS zero** on queue and case · 60 fps at 1,500 nodes · `prefers-reduced-motion` a **tested path** · no route renders a value not in the API response | ❌ **The gate is unrunnable, not merely unmet.** No `playwright.config.ts`; no `vitest.config.*`; **no test file of any kind under `apps/web`**. `@axe-core/playwright` and `@playwright/test` are pinned (`package.json:51,53`) and unused. `STATE.md:21` already says "never rendered … no test config". What *is* real: 7 route files exist, `dev/states/{page.tsx,gallery-rows.tsx,gallery-meta.ts}` exist, and the seam-5 design work is genuinely good — `contract.ts:1-24` (one declared shape, runtime decoder), `transport.ts:51-60` (fixtures cannot reach a production build), `charts.tsx:11-18` (`minimumSeriesNote` = `test_single_point_series` implemented as a component), `ErrorPane.tsx`/`EmptyState.tsx` primitives, `MarkArc.tsx` (the one licensed determinate spinner), `Shimmer.tsx` (transform sweep, 1.4 s). **But `test_cls_zero`, `test_stale_response_discarded`, `test_virtualised_large_queue`, `test_four_empty_states_distinct`, `test_reduced_motion_gallery`, `test_pane_error_isolated`, `test_greyscale_bands_distinguishable`, `test_timestamps_show_zone` exist only as prose** — the named tests for the whole P8 state-craft section are ABSENT. | "code present, never rendered" | ✅ **Confirmed, and the correct label.** |
| **P9** | packet **byte-identical across two renders of a pinned run** · hash chain verifies and **a tampered row fails in a test** · `make demo` boots offline **< 90 s** · every doc specific, no boilerplate · demo runs twice with no console error | ❌ ABSENT on the demo leg. `Makefile:127-129` `make demo` runs `scripts/demo_seed.py --restore --boot-budget 90`; **`scripts/demo_seed.py` does not exist** (`scripts/` holds 9 files, none of them that) and `data/snapshots/` is empty. `STATE.md:187-193` already calls this "a phantom gate" and I confirm it. `WeasyPrint` cannot import on this host at all (`STATE.md:177-185`), so the PDF leg is environmentally blocked. What *is* real: `test_p9_packet.py:789` `test_disclaimer_present_everywhere` exists; the audit chain is fixed and tamper-evident (`audit/chain.py:13-18,88-92,156-166` — now a JSON **array** with `HASH_VERSION` first, closing the `\|`-ambiguity defect in `STATE.md:153-161`); docs are genuinely generated from `make eval` output (`eval.py:1784-1788`, with `DATASET_CARD.md` correctly excluded at `:1776`); `LIMITATIONS.md` is generated. | "in progress" | ✅ **Correctly not claimed.** |

### 8.1 Rows that overclaim (the answer to your specific question)

| Row | Verdict |
|---|---|
| **P4 "not verified by the orchestrator"** | ✅ **Confirmed and understated** — there is no `test_p4_*.py` at all, so `verify.py:194`'s `-k p4` selects zero tests. The row understates the gap. |
| **P7 "never executed"** (your prompt) / **"partially executed"** (the file) | ✅ **Confirmed** — the file's wording is the accurate one, and I add that `worker.py`, `apps/api/Dockerfile` and `apps/web/Dockerfile` are missing, so the `full` profile cannot start. |
| **P8 "never rendered"** | ✅ **Confirmed** — and stronger: no test config and no test files at all, so the gate cannot even be attempted. |
| **P0 "DONE"** | ❌ **NEW OVERCLAIM, not in `STATE.md`'s punch list.** `P0 DONE` coexists with `make lint` red (`STATE.md:280`) and an empty `.github/workflows/`, both of which are inside the P0 gate (01 P0: `make lint`; T6: GitHub Actions). The right fix is either to make P0's `verify.py` entry include `make lint` + a CI run, or to move P0 to "PARTIAL" until CI exists. |
| **P1b "DONE"** | ⚠️ **Mildly overclaimed** — the `step` 1–743 vs 30-day config conflict is unresolved (`STATE.md:249-252`) and the gate says counts must match the card *exactly*. |
| **P1a, P3a, P5 "DONE"** | ✅ **Earned.** |
| **P2, P3b, P6 "in progress"** | ✅ **Correctly conservative.** |

### 8.2 Cross-cutting: named tests that exist only in prose

This deserves its own line because it is a systemic pattern, not one gap. Grepping the plan's named tests against real `def test_*`:

**Real:** `test_manifest_mismatch_rejects_batch` (`test_p1b_batch_boundary.py:116`), `test_unknown_column_fails_closed` (`:193`), `test_dup_txn_identical_dropped` (`:143`), `test_ingest_deterministic` (`:319`), `test_account_key_unique_per_run` (`:700`), `test_no_cross_source_node_merge` (`:744`), `test_counterparty_reuse_reported` (`:793`), `test_step_expansion_stable` (`:602`), `test_feature_hash_mismatch_refuses` (`test_leakage.py:489`), `test_disclaimer_present_everywhere` (`test_p9_packet.py:789`), plus the P7 outbox/signing set.

**Prose-only** (named in a docstring, no function anywhere):
- `test_shap_fallback_to_points` — `models/explain.py:5`
- `test_calibration_refused_below_floor` — `models/calibration.py:15`
- `test_erasure_preserves_chain` — `audit/erasure.py:57`
- `test_label_not_sharpe` — `backtest/metrics.py:452`, `backtest/config_io.py:67`, `backtest/model_card.py:12`, `apps/api/schemas/policy.py:124`
- `test_single_point_series` — `apps/api/schemas/validation.py:141`, `apps/api/routers/validation.py:303`
- `test_no_dead_rules`, `test_periodic_cycle_downweighted` — not found in any file
- The entire P8 named-test set (§14) — not found
- `tests/contracts_adapters/test_watchlist_conformance.py` — cited by `apps/api/routers/cases.py:482`, does not exist

The behaviours are mostly *implemented* (`single_point_series` is the `minimumSeriesNote` guard; the not-Sharpe labelling is in three places). What is missing is the **test that would fail if the behaviour regressed** — which is 00 §B's stated definition of a gate, and plan §16's *"a test was added that fails if this change is reverted"*.

---

# DELIVERABLE 2 — Target folder layout

## 2.1 The target tree

The prescribed tree is plan §5 T1 verbatim. Additions are marked `+`; each is justified.

```
OXBOW/
├── README.md  ARCHITECTURE.md  LIMITATIONS.md  MODEL_CARD.md  ECONOMICS_CARD.md  DESIGN.md
│   + PROMPT.md                        # T1 requires it; MISSING from the tree today
├── STATE.md  DECISIONS.md  BACKLOG.md
├── Makefile  docker-compose.yml  .env.example  .pre-commit-config.yaml  .python-version
│   + pyproject.toml  uv.lock          # T2 requires them; not in T1's ASCII box but mandatory
│   + .importlinter  .gitignore  .gitattributes
│   + .github/workflows/               # T6; currently EMPTY — must be populated
├── config/                            # the ONLY home for tunables (00 §G)
│   pipeline.yaml rules.yaml features.yaml model.yaml scorecard.yaml
│   economics.yaml splits.yaml sources.yaml
├── data/                              # inputs, intermediates, committed fixtures
│   raw/ interim/ processed/ snapshots/ golden/  DATASET_CARD.md
│   + watchlist/                       # OFAC SDN snapshot; input data, not code
│   + *_measurement.json               # §4 + DEV-011/013/015 numbers (see rule A3)
├── notebooks/                         # EDA; 01_eda.ipynb present, 02/03 MISSING
├── packages/pipeline/oxbow/           # the installable library (pyproject:83)
│   cli.py config.py identity.py stage_events.py eval.py dataset_card.py
│   contracts/ ingest/ features/ graph/ rules/ models/ scoring/ quant/
│   backtest/ explain/ packet/ ports/ adapters/ publish/
│   + audit/                           # chain.py + erasure.py; hash-chain arithmetic,
│                                      #   imported BY ports/audit.py:30. Its own
│                                      #   layer, not part of adapters/.
├── apps/api/                          # FastAPI; one deployable (C2)
│   main.py routers/ schemas/ deps.py problems.py events.py jobs.py
│   decisions.py outbox.py readmodel.py policy_engine.py security.py
│   observability.py settings.py echo.py
│   + worker.py                        # C2 commit matrix; MISSING — Makefile:122 needs it
│   alembic.ini  alembic/  keycloak/  Dockerfile  Dockerfile.echo
├── apps/web/
│   package.json pnpm-lock.yaml next.config.ts postcss.config.mjs biome.json
│   tsconfig.json Dockerfile            # Dockerfile MISSING — compose:208 needs it
│   src/
│     app/(dash)/ dashboard alerts cases/[id] network scorecard policy model
│     app/dev/states/                   # the state gallery (§14)
│     design/ tokens.css tokens.ts motion.ts icons/ primitives/
│     components/ lib/ types/ fixtures/
├── tests/
│   conftest.py  test_leakage.py
│   contracts/                         # data contracts (P1b) — currently
│   + contracts_adapters/              # PORT CONFORMANCE (02 §H). Currently a dir
│   + unit/ golden/ integration/ e2e/
├── scripts/                           # operator tools; see rule A2
└── out/                               # null-adapter landing (gitignored, .gitignore:62)
    sources/ objectstore/ case_sink/ notify/ report/ audit/ warehouse/
    features/ rules/ graph/ backtest/ pipeline/ packets/
```

Directory purposes in one line each: `config/` = every tunable; `data/` = bytes on disk, never code; `notebooks/` = human-facing EDA only; `packages/pipeline/oxbow/` = the importable library, ports inward-only; `apps/` = the two deployables plus their migrations; `tests/` = proof, split by what it proves; `scripts/` = one-shot operator actions; `out/` = null adapters' landing zone, gitignored.

## 2.2 Rules for every currently-ambiguous location

**A. Ad-hoc analysis / measurement scripts → `scripts/`, with a naming convention.**
Today: `scripts/measure_graph.py`, `measure_ibm_graph.py`, `measure_ibm_cycles.py`, `build_ibm_typologies.py`. These are one-shot measurements that produced `data/*_measurement.json` and a `DECISIONS.md` entry. **Rule:** anything that *measures the corpus* goes in `scripts/` as `measure_<subject>_<what>.py`; anything that *derives a committed dataset* goes in `scripts/` as `build_<subject>_<what>.py`; both must write their output under `data/` and print the command that reproduces it. They must not import from `features/`, `models/` or `quant/` beyond the library (import-linter contracts 1 and 3 already forbid that, and `measure_ibm_cycles.py:46` hand-rolls `sys.path.insert(REPO_ROOT/"packages"/"pipeline")` because it cannot import `oxbow.*` normally — fix that by installing the package, not by reordering the tree).
**Not** `notebooks/` (these are re-runnable gates cited in `DECISIONS.md` DEV-011/013/015) and **not** `packages/` (they are not library code).

**B. Operator/gate tooling → `scripts/`.** `verify.py`, `verify_determinism.py`, `verify_audit.py`, `download_data.py`, `no_float_money.py`. They are the commands `make verify*` and the pre-commit hooks run, and `tests/unit/test_anti_rubbish.py:144` loads `scripts/verify.py` **by path** — so `scripts/` is load-bearing and must not be renamed or relocated. Add a `scripts/README.md` naming which script produces which artifact; today nothing does.

**C. Generated docs → the repository root, never a `docs/`.** `eval.py:1784-1788` writes `README.md`, `ARCHITECTURE.md`, `MODEL_CARD.md`, `ECONOMICS_CARD.md`, `LIMITATIONS.md` at the root; `dataset_card.py:47` pins `data/DATASET_CARD.md`. **Rule:** generated documents are code output and belong beside the code they describe. `DESIGN.md` and `PROMPT.md` are **authored**, and must say so in their first line, or the next `make eval` will be asked to regenerate a contract.

**D. Notebooks → `notebooks/`, hand-built EDA only, numbered by phase.** `01_eda.ipynb` is the §4 measurement notebook required by plan §4 step 6. `02_features.ipynb` and `03_validation.ipynb` are prescribed by T1 and **absent** — either create them or record in `BACKLOG.md` why not. Rule: a notebook is a document for a human; if its number is cited in a gate, its producing command must also be in `scripts/`.

**E. Measurement outputs → `data/`, not `out/`.** `out/` is gitignored (`.gitignore:62`) and is the null adapters' landing zone. A number that `DECISIONS.md` or `MODEL_CARD.md` cites must be committed, so it belongs in `data/`. But `data/` currently mixes a *card* with four *machine artefacts*; **rule: `data/` root holds exactly one markdown file (`DATASET_CARD.md`) plus a `data/measurements/` subdirectory** for `graph_measurement.json`, `ibm_graph_measurement.json`, `ibm_cycle_measurement.json`, `download_manifest.json`. Cost: 4 script constants + 3 docstring references.

**F. Scratch → `out/tmp/`, never a named top-level dir.** `out/` currently holds `p7a.log`, `p9tmp/`, `tmp_graph/` alongside the eight port directories. **Rule:** `out/` contains exactly the port names from `ports/` plus `packets/`; everything transient goes to `out/tmp/` and is gitignored already. `out/p7a.log` is a stray log at the wrong level.

**G. Ports vs adapters stay split; `audit/` is neither.** `audit/` holds pure hash arithmetic imported *by* a port (`ports/audit.py:30`), so it must stay importable without touching `adapters/`. Keep it a top-level layer of the pipeline package. It is the one addition to the prescribed tree that I would defend as load-bearing rather than tidy.

**H. `tests/contracts/` vs `tests/contracts_adapters/` is a real distinction — keep both, and fill the second.** `tests/contracts/` = *data* contracts (does a row satisfy Pandera). `tests/contracts_adapters/` = *port* contracts (does an adapter behave like every other adapter of its port). Today the second is an empty directory holding one fixture, and `verify.py:237` already names it. Renaming it to `tests/port_conformance/` would be defensible; **leaving it empty is not.**

**I. `apps/web/src/fixtures/` is a first-class location, not a test fixture folder.** It is imported by production code (`lib/api/devproblem.transport.ts:18`) and gated out of production builds (`lib/api/transport.ts:51-60`). **Rule: keep it under `src/`, and never let a fixture path be imported by a route component** — that separation is the only thing preventing a demo number from being read as a measurement.

## 2.3 Reorganisation risk list — exhaustive

A move is only safe if every row below is handled. Ordered by blast radius.

### Tier 1 — moves that break a build or a gate immediately

| # | What breaks | file:line |
|---|---|---|
| 1 | Hatch wheel package list | `pyproject.toml:83` `packages = ["packages/pipeline/oxbow"]` |
| 2 | import-linter `root_package` + all 30+ `oxbow.*` module names in 4 contracts | `.importlinter:12,25-32,45,60-66,82-91` |
| 3 | `sys.path` bootstrap for the whole `api.*` import style — `main.py:37` `parents[1]` = `apps/`; every module imports `api.deps`, `api.problems`, `api.readmodel` | `apps/api/main.py:37-66`; `apps/api/routers/*.py`; `apps/api/schemas/*.py` |
| 4 | Alembic `script_location` **and** `prepend_sys_path` are both relative to the ini | `apps/api/alembic.ini:10` `%(here)s/alembic`, `:11` `%(here)s/../..` |
| 5 | Alembic env's own repo-root discovery | `apps/api/alembic/env.py:28` `parents[3]` |
| 6 | `uvicorn --app-dir apps/api` | `Makefile:114` |
| 7 | `make worker` → a file that does not exist | `Makefile:122` `$(PY) apps/api/worker.py` |
| 8 | Compose build context + dockerfile for `api` and `worker` | `docker-compose.yml:174-176` `context: .` / `dockerfile: apps/api/Dockerfile`; `:190-192` |
| 9 | Compose command for `worker` | `docker-compose.yml:195` `["python","apps/api/worker.py"]` |
| 10 | Compose build context for `web` | `docker-compose.yml:206-208` `context: ./apps/web` / `dockerfile: Dockerfile` |
| 11 | Compose bind-mount of the Keycloak realm | `docker-compose.yml:140` `./apps/api/keycloak/realm-oxbow.json` |
| 12 | Echo image build | `docker-compose.yml:153-155` `dockerfile: apps/api/Dockerfile.echo` + the comment explaining *why the context is the repo root* |
| 13 | `make db-migrate` | `Makefile:196` `alembic -c apps/api/alembic.ini upgrade head` |
| 14 | Every `Makefile` web leg via `WEB :=` | `Makefile:28`; used at `:47,118,144,162,166` |
| 15 | `make demo` (already broken) | `Makefile:129` `scripts/demo_seed.py` |
| 16 | pytest collection roots | `pyproject.toml:151` `testpaths = ["tests","packages/pipeline/tests"]` (the second **does not exist**) |
| 17 | mypy module root | `pyproject.toml:142` `mypy_path = "packages/pipeline"`; `Makefile:140` `mypy packages/pipeline apps/api scripts` |
| 18 | ruff import roots + excludes | `pyproject.toml:89` `src`, `:90` `extend-exclude`, `:101-124` `per-file-ignores` (9 patterns, all path-keyed) |
| 19 | coverage source root | `pyproject.toml:164` `source = ["packages/pipeline/oxbow"]` |
| 20 | pre-commit mypy / import-linter / float-money path filters | `.pre-commit-config.yaml:29,46,60` |
| 21 | pre-commit biome — `cd apps/web` **and** a path regex | `.pre-commit-config.yaml:86,90` |
| 22 | **seam 8 drift hook** — `cd apps/web && pnpm run icons && git diff --exit-code -- src/design/tokens.ts src/design/icons/sprite.svg` | `.pre-commit-config.yaml:97,100` |
| 23 | Biome ignore list of generated files | `apps/web/biome.json:5` |
| 24 | `verify.py` P7 gate path | `scripts/verify.py:237` `tests/contracts_adapters` |
| 25 | `verify.py` P8 gate — `pnpm --dir apps/web` | `scripts/verify.py:252` |
| 26 | `verify.py` P3a determinism gate artifact dir | `scripts/verify.py:151` `out/graph` |

### Tier 2 — moves that break a test silently (a green suite that no longer tests anything)

| # | What breaks | file:line |
|---|---|---|
| 27 | Test sys.path bootstraps for `api.*` | `tests/integration/test_p7_api.py:48,52`; `test_p7_session_hygiene.py:23,26`; `test_envelope_doctrine.py:41,46` — all three insert `REPO_ROOT/"apps"` **and** `REPO_ROOT/"apps"/"api"` |
| 28 | Alembic driven from a test by absolute path | `tests/integration/test_p7_api.py:239` `script_location = str(REPO_ROOT/"apps"/"api"/"alembic")` |
| 29 | A test reading the null warehouse from `out/` | `tests/integration/test_p7_api.py:1171` `FileWarehouseSource(REPO_ROOT/"out")` |
| 30 | Anti-rubbish test **names the CLI file** | `tests/unit/test_anti_rubbish.py:34` `CLI_PATH = REPO_ROOT/"packages"/"pipeline"/"oxbow"/"cli.py"` |
| 31 | Anti-rubbish test **loads `verify.py` by path** | `tests/unit/test_anti_rubbish.py:143-144` |
| 32 | Design-system test resolves `apps/web` by depth | `tests/unit/test_p0_design_system.py:28` `parents[2] / "apps" / "web"` |
| 33 | Seam 8 test reads both token files by depth | `tests/unit/test_p0_design_system.py:30-31`, assertions at `:367-378` |
| 34 | Toolchain test asserts 8 files by `REPO_ROOT / name` | `tests/unit/test_p0_toolchain.py:148` (`config/*.yaml`), `:386` `uv.lock`, `:391` `.python-version`, `:392,433` `pyproject.toml`, `:398,413` `apps/web/pnpm-lock.yaml`, `:409` `apps/web/package.json`, `:445` |
| 35 | Golden-fixture test reads config by depth | `tests/golden/test_golden_fixture_selfcheck.py:44,310,358` |
| 36 | Contract-test conftest resolves `config/` by depth | `tests/contracts/conftest.py:32-33` |
| 37 | Dataset-card contract test resolves the notebook | `tests/contracts/test_p1b_canonical_ingest.py:803,828` `notebooks/01_eda.ipynb` |
| 38 | Batch-boundary test asserts the architecture contract file exists | `tests/contracts/test_p1b_batch_boundary.py:392` `REPO_ROOT/".importlinter"` |
| 39 | Import-linter gate itself is a test | `tests/contracts/test_p1b_batch_boundary.py:364` `test_graph_imports_the_canonical_column_list` |

### Tier 3 — moves that break a script, a doc, or a contract at runtime

| # | What breaks | file:line |
|---|---|---|
| 40 | `RUN_SALT` resolution reads `REPO_ROOT/.env` | `packages/pipeline/oxbow/config.py:188-209`; `config.py:79` `find_repo_root` |
| 41 | API settings resolve the repo root from the env | `apps/api/settings.py:71` `OXBOW_REPO_ROOT` |
| 42 | Re-**used** repo-root discovery (the "three places" in `STATE.md:122`) | `packages/pipeline/oxbow/config.py:79`; `apps/api/routers/dashboard.py:314`; `apps/api/observability.py:75` — a 4th is `cli.py:154` |
| 43 | Redaction rules read `config/sources.yaml` | `apps/api/observability.py:75` |
| 44 | Dashboard dataset badge reads `config/sources.yaml` | `apps/api/routers/dashboard.py:295,314` |
| 45 | Dataset-card path pinned as a literal | `packages/pipeline/oxbow/dataset_card.py:47` `CARD_RELPATH = "data/DATASET_CARD.md"` |
| 46 | Generated docs' filenames + cross-links | `packages/pipeline/oxbow/eval.py:1784-1788, 2106-2121` |
| 47 | Packet render **mirrors** the token font stack and band ramp in Python — a `tokens.css` move breaks packet parity and the pre-commit hook cannot catch it | `packages/pipeline/oxbow/packet/render.py:18,61,70`; `packet/__init__.py:22` |
| 48 | Every `scripts/*.py` computes `REPO_ROOT = parents[1]` and hardcodes `data/` subpaths | `download_data.py:39-43`; `measure_graph.py:33-35`; `measure_ibm_graph.py:34-36`; `measure_ibm_cycles.py:45-55,403`; `build_ibm_typologies.py:40-43`; `verify.py:29`; `verify_audit.py:33,38` (`out/audit`); `verify_determinism.py:32-33,201` (`data/interim`) |
| 49 | Scripts hand-roll `sys.path` for the pipeline package | `measure_ibm_cycles.py:46`; `verify_determinism.py:189` — both insert `REPO_ROOT/"packages"/"pipeline"`, which a `src/` layout invalidates |
| 50 | tokens/sprite generator walks three levels up and writes `public/` | `apps/web/src/design/icons/build.mjs:30-41` (`WEB_ROOT = join(HERE,"..","..","..")`, `PUBLIC_DIR`, `SPRITE_PUBLIC_OUT`, `MARK_PUBLIC_OUT`) |
| 51 | Icon runtime href | `apps/web/src/design/icons/Icon.tsx:79` `SPRITE_HREF = '/sprite.svg'` (served from `apps/web/public/`, which does not exist yet) |
| 52 | `next.config.ts` rewrite (no path aliases, so a `src/` reorg is safe — but a `web/` move is not, because `make web` does `cd apps/web`) | `apps/web/next.config.ts:16-18`; `Makefile:118` |
| 53 | `.gitignore` path rules, including a **negation** | `.gitignore:38-41` (`apps/web/.next/`, `apps/web/out/`), `:50-60` (`data/raw/*` … `!data/golden/*`), `:68` (`!apps/web/src/lib/api/schema.d.ts`) |
| 54 | Docs that name the paths | `DESIGN.md:3,5,18,123`; `ARCHITECTURE.md:64`; `README.md` doc links; `eval.py:2210-2211, 2106-2121` |
| 55 | A source docstring citing a **nonexistent** test path | `apps/api/routers/cases.py:482` |
| 56 | The frontend's own scanner root | `tests/unit/test_anti_rubbish.py:73` `root = REPO_ROOT / root_name`; `:99` |
| 57 | Fixture transport imported by production code | `apps/web/src/lib/api/devproblem.transport.ts:18` → `src/fixtures/transport.fixture.ts`; `src/fixtures/*.fixture.ts` (8 files) |
| 58 | Anything under `apps/web/src/` imported by **relative** path — a `design/` move out of `src/` breaks all of them | e.g. `charts.tsx:30` `'../../design/tokens'`; `colour.ts:4-7`; `components/ui/sx.ts:10,42`; `packet/render.py:61` |
| 59 | `P7` fixture RSA key | `tests/contracts_adapters/fixtures/rs256_token.json` (referenced by `test_p7_api.py:124`) |
| 60 | The one env→path indirection that already exists and would be the model for a reorg | `apps/api/routers/validation.py:187` / `readmodel.py` use table names, not paths — good; but `OXBOW_REPO_ROOT` (`settings.py:71`) is the only knob, and a reorg must not need a second one |

**Recommendation on ordering.** Do **not** move anything until the four currently-missing artifacts exist, because each is a *destination* a move would have to account for: `apps/api/worker.py`, `apps/api/Dockerfile`, `apps/web/Dockerfile`, `.github/workflows/*.yml`. Then move in this order, re-running the named gate after each step: (1) `out/` hygiene (rule F) — zero code impact; (2) `data/measurements/` (rule E) — 7 constants; (3) `tests/contracts_adapters/` (rule H) — 0 constants, it is already empty; (4) notebooks (rule D); (5) `PROMPT.md` (rule C); (6) scripts README (rule B). **Never** move `packages/`, `apps/api/`, `apps/web/`, `config/`, `.importlinter` or `design/tokens.*` — the first four are load-bearing for 38 of the 60 rows above and the fifth is pinned by a pre-commit hook and a test.

---

# Top 10 structural risks, prioritised

**1. No port-conformance contract test exists for any of the eight ports.**
`tests/contracts_adapters/` holds one fixture and zero tests. The P7 gate at `scripts/verify.py:237` therefore cannot pass. The "behaviourally interchangeable" claim is prose in `ports/source.py:102-104`, `ports/objectstore.py:94-96`, `ports/audit.py:5-7`, `adapters/null/__init__.py:4-7`, and `apps/api/routers/cases.py:482` cites a test file that does not exist. Three adapter families (`slack/`, `goaml/`, `ofac/`) have no test reference at all.
→ plan §13's "the part judges interrogate" is the one part with no proof. `scripts/verify.py:237` · `apps/api/routers/cases.py:482`

**2. There is no CI. `.github/workflows/` is an empty directory.**
Every "CI fails on drift" clause has no enforcement point: seam 6 (generated client), seam 8 (`tokens.ts`), `pip-audit`/`pnpm audit`, schemathesis, import-linter, the 50k smoke, the backtest smoke. All of it currently lives in pre-commit (`.pre-commit-config.yaml:38-46, 92-100`), which a judge cloning the repo never runs. Plan §5 T6 and §13.
→ `.github/workflows/` (empty) · `pyproject.toml:68` claims *"The CI check regenerates it and fails on any difference"* — a comment describing a check that does not exist

**3. `STATE.md` marks P0 DONE while `make lint` is red.**
The P0 gate (01 P0) includes `make lint`; `STATE.md:280` records 85 ruff errors, 133 files unformatted, 304 mypy errors. `verify.py:69-82` quietly defines P0 as three narrow commands that exclude lint, `make up` and CI. This is the only phase row that overclaims beyond what the tree shows, and it is the *first* phase, so it sets the precedent.
→ `scripts/verify.py:69-82` · `STATE.md:11` vs `STATE.md:280`

**4. P4 has no tests at all, not merely unverified ones.**
`verify.py:194` runs `pytest -q tests/unit -k p4`; there is no `test_p4_*.py` in the repo. `STATE.md:17` says "not verified by the orchestrator", which understates it: the gate selects zero tests. The scoring code is substantial and its scaling arithmetic is sound (`STATE.md:311-321`), including the named `int()`-truncation wart.
→ `scripts/verify.py:192-196` · `tests/unit/` (no `test_p4_*`)

**5. The generated OpenAPI client does not exist; the typed contract is hand-written.**
`apps/web/src/lib/api/schema.d.ts` is un-ignored at `.gitignore:68` but absent; `apps/web/package.json:9-19` has no generate script; `openapi-typescript` is pinned and unused (`package.json:62`). `contract.ts:19-23` still lists `SEAM(P7)` items. The *behaviour* (no `any`, decoded shapes) is right; the *mechanism* the plan mandates is not.
→ `apps/web/src/lib/api/contract.ts:19-23` · `apps/web/package.json:16-18,62` · `.gitignore:68`

**6. `make up`'s `full` profile cannot start: three referenced files do not exist.**
`apps/api/worker.py` (C2 commit matrix; `Makefile:122` and `docker-compose.yml:195`), `apps/api/Dockerfile` (`docker-compose.yml:176`), `apps/web/Dockerfile` (`docker-compose.yml:208`). Only `Dockerfile.echo` exists. `STATE.md` flags the missing decision path but not the missing build inputs.
→ `docker-compose.yml:176,195,208` · `Makefile:122` · `DECISIONS.md:24-27`

**7. Seam 4 is half-implemented: the MLflow model URI never reaches Postgres.**
`models/run.py:114-155` puts `model_uri` on the in-memory scored row, but `adapters/warehouse/models.py:331-363` (`Score`) has only `model_version` at `:363`; `postgres.py:58,72` writes only that; `0002_integrity_triggers.py` mentions neither. Eleven repo-wide `model_uri` hits, all in `models/`. `models/run.py:881` defaults it to `None` anyway. Plan §10 says "URI **+** version".
→ `adapters/warehouse/models.py:363` · `models/run.py:881`

**8. P8's entire named-test set is prose, and the gate cannot be attempted.**
No `playwright.config.ts`, no `vitest.config.*`, no test file of any kind under `apps/web` — so `Makefile:160-166` and `verify.py:250-253` have nothing to run. `@playwright/test` and `@axe-core/playwright` are pinned and unused. `test_cls_zero`, `test_reduced_motion_gallery`, `test_greyscale_bands_distinguishable`, `test_pane_error_isolated`, `test_four_empty_states_distinct`, `test_stale_response_discarded`, `test_timestamps_show_zone`, `test_virtualised_large_queue` exist only in §14's prose — even though the *behaviours* are largely implemented (`contract.ts`, `transport.ts:51-60`, `charts.tsx:11-18`).
→ `apps/web/` (no test config) · `Makefile:160-166` · `scripts/verify.py:250-253`

**9. `account_key` has three derivations and an undocumented deviation from the plan.**
The plan says `sha256(account_id + RUN_SALT)[:12]`, uppercased. What ships is HMAC-SHA256 keyed by the salt over `"{salt}|{name}"`, **lowercase**, enforced lowercase by `contracts/canonical_v1.py:294` — and `scripts/measure_ibm_cycles.py:175` uses **plain** `sha256` over the same shape. The HMAC choice is better than the plan and argued at `ingest/canonical.py:160-167`, but the cross-script divergence is a real join hazard and there is no `DECISIONS.md` line authorising either.
→ `ingest/canonical.py:169` vs `scripts/measure_ibm_cycles.py:175` · `contracts/canonical_v1.py:294`

**10. §19's own marker grep is not clean, and a self-described wrapper import is un-audited.**
`apps/web/src/app/dev/states/page.tsx:153` renders the literal string `label="Sweeping placeholder"` — a §19 rule 1 hit in JSX that `test_anti_rubbish.py:99` either does not scan or does not classify. Separately, `state_pending`, `state_running` and `state_failed` render as `--color-state-pending` at `alerts/page.tsx:518` — a **job** state token used to encode a **decision** state, which the plan's "no dead rules / no decoration" and "status lives in the HTTP code" instincts both warn against; and `scorecard/page.tsx:377` computes an inline OKLCH ramp from a data value, outside `tokens.css`, with no drift check.
→ `apps/web/src/app/dev/states/page.tsx:153` · `apps/web/src/app/(dash)/alerts/page.tsx:518` · `apps/web/src/app/(dash)/scorecard/page.tsx:377`

**Also worth naming, just outside the top 10:** the idempotency key's `|` delimiter (`ports/case_sink.py:56-65`) is a *correct* deviation that should be in `DECISIONS.md`; `ports/audit.py:30` importing `oxbow.audit.chain` weakens the "zero-dependency ports" contract that `.importlinter:41-51` does not test; two independent `is_ulid` implementations exist with different strictness (`identity.py:115-129` checks the leading char, `ports/warehouse.py:87-89` does not); `config/` is a *file* list with no `config/README.md` naming which key is owned by which phase, which T1 explicitly asked for; and `scripts/verify.py:151`'s determinism gate, `out/graph`'s artifact comparison and `verify_determinism.py:278`'s 67-artifact result are the strongest gates in the repo and deserve to be cited in the report's EVIDENCE block rather than buried in a punch list.

---
STATUS: PASS
RESULT: Architecture audit complete, read-only, with file:line evidence for all eight numbered items. The ports, the outbox, the identity model, seam 3, seam 5 and the web number contract are all genuinely well built. Four structural absences dominate: no port-conformance contract test for any of the eight ports, no CI at all, no P4 tests, and no web test harness. `STATE.md` is honest about P2/P3b/P4/P6/P7/P8/P9; it is wrong about P0 and mildly wrong about P1b. Target folder layout and a 60-row reorganisation risk list follow.
DESIGN: Report-only deliverable — no code was written. The design decisions embedded in Deliverable 2 are the eight placement rules (A–H) and the six-phase, re-gate-after-each-step move order, with an explicit never-move list (`packages/`, `apps/api/`, `apps/web/`, `config/`, `.importlinter`, `design/tokens.*`).
COMPONENTS: Seven ports (all 8 Protocols + all 8 `Null*` present); seven adapter families (present, three untested); four import-linter contracts (present, pass UNVERIFIED); outbox module (complete and tested); audit chain (delimiter-ambiguity defect already fixed); pipeline frame/scoring seam 3 (complete); MLflow lineage seam 4 (half-built — URI dies at the warehouse); web contract layer (hand-written decoders, fixtures gated out of production builds).
INTEGRATION: Entry points audited are `Makefile` (28 targets), `scripts/verify.py` (12 phases, 22 gates), `docker-compose.yml` (8 services), `.pre-commit-config.yaml` (8 hooks), `apps/api/main.py` (12 routers), `apps/web/src/lib/api/transport.ts` (3 modes). No route recomputes a score, feature or aggregate; the only derived figures (`cases.py:529` counterfactual, `dashboard.py:260-289` community count, `policy_engine.py` allocation) are documented and sanctioned by plan §14.
FILES_TO_CREATE: `.github/workflows/*.yml` (lint, typecheck, pytest, 50k smoke, backtest smoke, image build, pip-audit, pnpm audit, schemathesis, import-linter) · `tests/contracts_adapters/test_*_conformance.py` (8 parametrised port suites) · `apps/api/worker.py` · `apps/api/Dockerfile` · `apps/web/Dockerfile` · `apps/web/playwright.config.ts` + `apps/web/vitest.config.ts` + `apps/web/e2e/*.spec.ts` · `tests/unit/test_p4_*.py` · root `PROMPT.md` · `notebooks/02_features.ipynb`, `03_validation.ipynb` · `config/README.md` · `scripts/README.md` · `apps/web/public/` (sprite target).
FILES_TO_MODIFY: `apps/api/observability.py` (no test asserts the redaction filter fires — plan §13) · `adapters/warehouse/models.py:331-363` + a migration (add `model_uri`) · `adapters/warehouse/postgres.py:58,72` (write it) · `scripts/measure_ibm_cycles.py:175` (match the HMAC derivation) · `apps/web/src/app/global-error.tsx:24-67` and `scorecard/page.tsx:377` (move colour into `tokens.css`) · `apps/web/src/app/(dash)/alerts/page.tsx:518` (stop reusing job-state tokens for decision states) · `pyproject.toml:151` (drop the dead `packages/pipeline/tests` testpath) · `.gitignore:68` + `apps/web/package.json:9-19` (add the client-generate script) · `STATE.md:11` (P0 → PARTIAL until lint and CI are green).
RISKS: 60-row reorganisation risk list, of which 26 would break a build or a gate on the first commit and 13 more would break a test *silently* (a green suite that no longer tests anything). Four destinations do not yet exist (`worker.py`, two Dockerfiles, CI workflows), so any move before they land has no correct answer. `ports/audit.py:30` and `ports/case_sink.py:56-65` are two deliberate deviations from the plan that are argued in code but absent from `DECISIONS.md`. `ingest/canonical.py:169` and `scripts/measure_ibm_cycles.py:175` derive `account_key` differently from the same corpus — a cross-corpus join hazard, not yet a demonstrated failure.
</task_result>
</task>
