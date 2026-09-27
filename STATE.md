# STATE

Living index of what is true in this repository right now. Written from observed
output, never from intent. `DECISIONS.md` holds the reasoning; this file holds
the position.

## Phase position

| Phase | Scope | State | Proof |
| --- | --- | --- | --- |
| **P0** | toolchain, tree, config, CLI verbs, design tokens, 12 glyphs, CI | **DONE** | `verify.py --phase P0` 2/2 · `9582638` |
| **P1a** | acquire PaySim, measure the graph thesis, log DEV-011 | **DONE** | `verify.py --phase P1a` 2/2 · PaySim + all three IBM members match their recorded SHA-256 against bytes on disk |
| **P1b** | canonical v1, PaySim + IBM-AML adapters, quarantine, Parquet/DuckDB writers, dataset card | **DONE** | `verify.py --phase P1b` **3/3 PASS** — 28 contract tests; 200,000 canonical events with 0 quarantined and 0 silently coerced from PaySim *and* from IBM through its own adapter. The card deliverable closed by observation: `oxbow eval` ran twice and `data/DATASET_CARD.md`'s sha256 was identical before and after (`41e653ad922cfab1…`), with `[card] 106 passed, 0 failed, 2 skipped, 108 checks` |
| **P3a** | time-stamped directed multigraph, rails, Leiden, cycles, subgraph cap | **DONE** | `verify.py --phase P3a` 2/2 — 80 tests (61 prior + `test_p3a_self_edges.py`'s 19), and two `oxbow graph` runs landing **5 byte-identical artifacts** with self-transfers admissible in the input |
| **P2** | 60-75 features, feature-spec hash, leakage gate proven to bite, purged splits | **DONE** | `verify.py --phase P2` 2/2, re-measured directly after: `tests/test_leakage.py` **19 passed** (exit 0) and `tests/unit -k p2` **78 passed** (exit 0) — 78 where it was 76 before, so the P4 worker's new tests landed without breaking this layer |
| **P3b** | golden fixture, rules R1-R12 | **DONE** | `verify.py --phase P3b` **2/2 PASS** — 86 tests across rules, golden matrix and the DEV-015 labelled-cycle file (the four that were failing earlier are green now) |
| **P4** | WOE scorecard, LightGBM, Isolation Forest, calibration, fusion, SHAP | in progress — **the first real scored run has landed** | The segfault was a native load order (DEV-021) and the monotonic binning had never run behind a swallowing fallback (DEV-020); both fixed and mutation-proven. On 2026-09-27 `uv run oxbow score --max-events 40000` finished end to end for the first time and wrote `out/score/01M3FZ2GC3J71AYT1QEKPWEDKJ/`: **fold 0 scored 28,588 rows with SHAP** over a 79,998-row × 75-feature account-grain corpus (108 positives, base rate 0.001350), fold 0 `calibrated=false` for the reason the config names (14 validation positives < `min_positives_for_calibration=50`, so probabilities ship labelled uncalibrated). **Folds 1-4 did not fit** — each skipped with `LightGBMError: bad allocation` on a corpus far too small to need that much RAM. **Superseded the same day:** the cause was that `config/scorecard.yaml` declares three categorical features that are *already* dense integer codes, and `feature_matrix` ran them through P4a's FNV-1a string encoder anyway, so LightGBM was handed 2.4e9-magnitude values as category **indices** and sized its per-category arrays by the largest — 7,339 MB, then the refusal. Fix: integer categoricals pass through untouched, string categoricals are coded numerically and not offered as `categorical_feature`, and `MAX_CATEGORY_INDEX = 100_000` refuses by column name. Measured on the committed harness: 900 s-and-still-fitting → **3.9 s at 484 MB peak**. A second run then scored **all five folds** (`01M3H8WG436R394NZT2GS1KG69`, 2026-09-27 15:09: folds 0/1/4 `full_model_stack` at 23,032/16,830/872 rows, folds 2/3 `scorecard_and_rules_only` at 1,574/1,412 because the GBM's own average_precision guard refused, every fold `calibrated=False` for the countable reason in DEV-024) — and then died *writing* them, because degraded and full-stack folds have the same width with different column names and `cli.py` concatenated them positionally. See DEV-025. `cli.py`'s RESULT line counted summary entries and read "5 fold(s) scored"; it now reads `N of M` and prints each skipped fold's reason, because a progress line that scores a refusal as a pass is how a phase gets claimed on a tree that never finished fitting. |
| **P5** | EV allocation, greedy vs CP-SAT, Monte Carlo exposure, economics | **DONE** | `verify.py --phase P5` 1/1 (89 tests) |
| **P6** | walk-forward backtest, ablation table, fairness, perturbation | in progress — **it now runs, and its own postcondition is what found the defect** | The embargo refusal was resolved by reading the window the features were cut on rather than the corpus's own span: `oxbow backtest --corpus` now prints that it is resolving the fold fractions on the recorded window from `features_manifest.json`, and it gets past fold planning into the fitting. What then killed the run was a grain defect (**DEV-026**): the corpus is one row per (account, as-of) — **79,998 rows over 77,691 (account, fold) pairs** — while the harness books money per account, so an account selected on two of its rows paid its analyst minutes twice. The capacity postcondition caught it after 70 minutes of fitting (`policy booked 12025 minutes over a 12000-minute capacity`), which is the guard doing its job and the only reason this is known. Fixed by collapsing the fold to one decision per account (latest stamp carries the state, any positive stamp makes the account positive: 108 positives as rows, 106 latest, 105 first), with the re-booking path refusing a repeated key, a repeated review and a review the fold never scored. The fifth run then **completed**: `out/backtest/real40k/ablation_results.json` carries 9 variants at `provenance=real_corpus`, the fold plan agrees with the corpus's own fold column on all 79,998 rows (`agreement_share: 1.0`), and the leakage control still detects the lookahead arm — control PR-AUC 1.0 against best honest 0.0591, which is the harness proving it can see cheating rather than a model proving it is good. `oxbow eval` publishes those numbers and `grep -c fake_harness` across README, ARCHITECTURE, MODEL_CARD, ECONOMICS_CARD and LIMITATIONS is 0, 0, 0, 0, 0. What P6 still lacks: a calibration curve (every fold refuses on DEV-024's arithmetic, and the 500k slice is running now to answer it) and the **re-run** of the model ablation — the per-row scorer landed on 2026-09-28 (**DEV-027**: each row now reads the channel its label names, with one extra booster fit per fold for the graph-free model, and a missing channel raises instead of borrowing the full stack's number), but the artifact on disk still predates it and still carries the old `ablation_caveat`, which the generated card faithfully prints. |
| **P7** | FastAPI, SSE, RQ, ports/adapters, outbox, audit chain, OIDC | **DONE**, with three named gaps now visible instead of asserted away | `verify.py --phase P7` **3/3 PASS** as recorded when the phase was claimed (4 import-linter contracts kept, 77 integration tests against a real Postgres, the port-conformance directory green) — re-measure it before repeating that line, because the tree has moved since. Re-measured today, that same gate had been proving **one port of eight**: the directory held only `test_watchlist_conformance.py` while eight ports and eight `Null*` adapters exist. It now holds one conformance file per port — `pytest tests/contracts_adapters` is **301 passed, 1 skipped** (the skip is the S3-only ETag trap and says so), with each implementation held to the same promises and the divergences recorded in BACKLOG rather than frozen as expected behaviour. A second §13 deliverable turned out to be absent rather than failing: **`openapi-typescript` is a declared devDependency that nothing invokes** — there is no generated types file anywhere under `apps/web/src/lib/api/`, so the "typed error union rather than `any`" clause had no executor, and `contract.ts` is hand-written decoders. Schemathesis is now driven from the served document, and its first run earned two fixes rather than a green tick: the document declared FastAPI's `HTTPValidationError` for every 422 while the app's own handler emits a `ProblemDetail` (so a generated client typed the whole validation branch against a schema the server never sends), and `/healthz` documented no error at all. Its first failure list — 15 operations — then turned out to be **the harness, not the API**: the generated header values carried characters latin-1 cannot encode, so the transport rejected the request before the app saw it. The suite now skips those by name instead of reporting a server fault it did not observe, and re-measures the rest. Toxiproxy remains absent (BACKLOG). The analytical warehouse handoff moved the same day: 15 new table builders, 5 reading the backtest artifacts and 9 the frames, of which the four artifact tables that produce rows are proven into a real scratch Postgres (round trip, money as integers, and a completed run refusing further writes) while the nine frame-backed ones are wired and untested and recorded as such in BACKLOG. `backtest_fold` lands no rows on any artifact this build has — nine of its NOT NULL columns have no producer, and the mapper names them instead of padding. Two further §13 deliverables are absent, not failing: **Schemathesis** is a declared dependency that no test imports, and **Toxiproxy** is not in the compose file or the integration suite, so the "timeout/reset yields degraded UI, not a stack trace" clause has no executor. Also fixed today: the `score` handoff emitted one row per scored row against a table declared `UNIQUE (run_id, account_key)` — 674 collisions waiting on the landed 40k run, which the calibration refusal was hiding (DEV-026). Open: `/api/graph/subgraph` serves nodes as `id/label/is_seed/is_rail/flags` while `apps/web/src/lib/api/contract.ts:773` decodes `key/node_type/flagged/is_cycle_member/hops/true_size`, so the explorer's pane refuses rather than rendering — which is the decoder behaving correctly and the server being out of contract (plan §12's PII boundary names `account_key` downstream of ingest, so `key` is the right field) |
| **P8** | seven screens, state craft, `/dev/states`, zero-CLS | **measured, not yet claimed** | Playwright against the cached chromium (nothing downloaded): **24 passed, 1 recorded skip, 1 red**. The skip is the 1,500-node fps probe — the null-file warehouse serves 1 node and the spec says so instead of passing vacuously. **Measured CLS is 0.000000 on `/alerts` and 0.000000 on `/cases/[id]`** against the plan's zero (was 0.0153 on the queue; its skeleton is now built from the same slot table the resolved rows use), and 0.000438 on `/dev/states` against a 0.001 budget. axe 10/10 routes clean, vitest 51/51, tsc 0 errors, biome 0 findings over 86 files (from 76). P8's phase entry now gates vitest, `tsc --noEmit` and `make lint-web`'s biome check through the installed tree with each entry point as its `prerequisite`, and all three were executed through `verify.py`'s own `_run_gate` — not by hand. The red spec is the dataset-fidelity clause on the live API: **13 of `/model`'s panes never leave their loading state even though their route answered 200**, so DESIGN.md §5's pane-error tier is not reached on that path. Read from the code rather than from a live run, the mechanism is `components/Pane.tsx`: `MaybeSuspense` renders children when `meta !== null`, so `meta` — the provenance slot — is doubling as the liveness signal, and `/api/validation` builds its envelope through `schemas/common.py::envelope(data, **meta_fields)`, a plain function with no run context. A route that emits `meta: {}` would NOT hang there — an empty object is not null — so the hang needs `meta` to arrive null or absent, which points at `ListMetaDecoder` yielding nothing for a body missing a required key, or at a route that does not go through `envelope()`. The mechanism is therefore not established, and this row will not print a guess as a diagnosis: re-measure against a live server with the analytical tables filled (BACKLOG: that handoff is the current work) before touching a component. If it still hangs, the fix is to stop letting `meta` carry liveness — `Pane` already has a `pending` prop for that concept and would need a `resolved` one beside it. The honest re-measurement is against a filled warehouse (BACKLOG: the analytical tables are being landed), because if real rows bring real meta the symptom never appears on the demo path; if it still hangs, the fix is to stop making `meta` carry liveness — `Pane` already has a `pending` prop for that concept and needs a `resolved` one beside it. |
| **P9** | packet, generated docs, demo snapshot, limitations | in progress | `README`/`ARCHITECTURE`/`MODEL_CARD`/`ECONOMICS_CARD`/`LIMITATIONS` written by `make eval`, which also overwrote the authored dataset card (see below) |

Execution order is `P0 → P1a → P1b → P3a → P2 → P3b → …` per DEV-008: the graph
is built before features, because a degree cannot be computed from a graph that
does not exist yet.

## Environment (measured, this session)

```
uv python 3.12.13 · docker UP (postgres healthy on host 5433, redis 6379, mlflow 5000,
keycloak 8081, echo 8099, minio 9000) · disk 81 GB free of 953 GB · RAM 15.7 GB total,
2.1–3.4 GB free while the pipeline and one agent shared the box · PaySim on disk
493,534,783 B · IBM HI-Small_Trans.csv 475,664,283 B over 17.68 days
```

Memory is a load-bearing constraint, not a footnote: an `import mlflow` MemoryError
reported by a worker as a blocker was host load under concurrent runs (it imports fine at
2.19.0 — see the ledger), and running a full-corpus score stage next to an agent that is
itself running pytest is how that happens.

The two P0 blockers are cleared: the Docker daemon is running (B1) and disk is sufficient
(B3), though free disk fell from 104 GB to 81 GB as the corpora and artifacts landed.

`make` still does not exist on this host, so every §16 gate phrased as `make X` has been
run as the underlying command (see "Two things about this host" below). The JS toolchain is
**Bun**: `packageManager: "bun@1.4.2"` with a committed `bun.lock`, and no `pnpm-lock.yaml`.
That is DEV-022, an amendment the owner ruled on twice rather than a migration an agent
decided alone, and `test_the_declared_js_toolchain_is_the_one_every_recipe_uses` now holds
manifest, Dockerfile, Makefile, pre-commit recipes and the phase table to that one name -- so
a future swap has to be another amendment, whichever direction it goes in. Plan §T2 still
prints `pnpm@9.15.9`; DEV-022 supersedes it, and quoting §T2 alone gives a stale answer.

Nothing about the swap changed the dependency bytes, and that was measured rather than
assumed: diffing `bun.lock` against the `pnpm-lock.yaml` it replaced gives **438 resolved
packages on each side, identical sets, 438/438 matching sha512 integrity hashes, zero version
drift**, and all 40 declared dependencies resolve to exactly their pin -- which is the check
that matters for the `pnpm.overrides` -> `overrides` move (Bun ignores the `pnpm` key), since
`@tailwindcss/oxide`, its `win32-x64-msvc` binary and `@tailwindcss/node` are all locked at
4.0.0. The earlier open item -- `node_modules` being a tree that matched neither manifest,
because two sessions whipsawed the declaration while the install stayed Bun's -- is closed:
`bun install --frozen-lockfile` reports 340 installs across 439 packages, no changes.
Bun is the runner, not the runtime; `bun run build` and the vitest process it starts execute
on Node, the interpreter `engines.node` names. Nothing installed a system dependency to get
any of this. Playwright is usable without a download because a chromium already sits in
`~/AppData/Local/ms-playwright` -- the config points at it by path rather than installing one.

## The load-bearing fact: DEV-011

PaySim is **star-shaped**. Median account degree 1.0; sender reuse ratio 0.0015;
zero surviving time-respecting 3–6 cycles on a 20k sample; the highest-degree
*sender* in 6.36 M rows has 3 edges. On the fraud subset alone the reuse ratio is
exactly 0.0 and max degree is 2. So the network thesis has no substrate on the
corpus the spec assumed it on.

Consequence, pre-committed in plan §4 before the number was known: IBM-AML is
primary for **Module B** (typology rules, graph features, network detection);
PaySim stays primary for **Module A** (tabular risk, volume). The code is
corpus-agnostic, so this changed which corpus feeds which module and the framing
in the dataset card — not the pipeline.

**Correction discovered this session:** IBM-AML was recorded as 41.6 GB and parked
as blocking. It is neither. Kaggle reports the `-aml` slug at 8.18 GB, and the
dataset actually ships **scenario bundles** (`HI-Small_Trans.csv` and siblings),
so the acquisition that Module B needs cost ~510 MB. Measured on the real bytes:
**median account degree 10.0** (self-loops excluded; 6.0 with them) against the > 2
pass condition, 515,080 accounts over 647,939 directed edges, 0.1019 % laundering
rate. DEV-011's fallback is now executable rather than aspirational — see
**DEV-013**, which also records three schema facts no document predicted (a repeated
`Account` header column, `/` vs `-` timestamps across the two files, and 591,212
self-loop rows, 11.6 % of the corpus).
`uv run python scripts/measure_ibm_graph.py` reproduces
`data/ibm_graph_measurement.json`.

**Typology ground truth (DEV-014):** `HI-Small_Patterns.txt` labels
**3,209 transactions across 370 laundering attempts in 8 typologies — 287 CYCLE,
342 FAN-OUT, 318 FAN-IN, 466 STACK, 191 RANDOM.** Built by
`scripts/build_ibm_typologies.py` into `data/processed/ibm_typologies.parquet`. This
is what makes plan §6's per-typology recall measurable against real annotations, and
the `RANDOM` block is a free negative control.

## Also true

- `isFlaggedFraud` fires **16 times in 6,362,620 rows**: not a usable target.
  `isFraud` (8,213 rows, 0.129 %) is the only viable label on PaySim.
- Canonical v1 is the single source of truth for the event shape. `ports/source.py`
  and `ingest/canonical.py` disagreed; they are being reconciled under DEV-012
  (persisted Parquet carries deterministic columns only; `run_id`/`ingested_at`
  live in the manifest and the warehouse so `make verify-determinism` can compare
  bytes across runs).
- Root docs (`README`, `ARCHITECTURE`, `MODEL_CARD`, `ECONOMICS_CARD`,
  `LIMITATIONS`) are generated from `make eval` output in P9, not written from
  intent (plan §15). `DESIGN.md` is authored, because it is a contract rather than a
  result.

## Integration punch list (found by inventory, not yet fixed)

Ordered by damage if any of it survives into a claimed-complete phase.

1. ~~**The IBM contract is fabricated.**~~ **CLOSED.** `contracts/raw_ibm_aml.py` declared
   14 columns (`stepFrom, stepTo, Type, Category, Amount, nameOrig, balanceOrig,
   nameDest, balanceDest, isLaundering, isFlood, isDateSpam, isForcedCashout,
   unlabeled`) belonging to a different IBM artefact, and its 56 tests passed against
   the invention. It now carries the real 11-column header with the two `Account`
   columns distinguished positionally, `Is Laundering` as the only label column, and
   typology from `data/processed/ibm_typologies.parquet` (DEV-014). 137 tests pass, and
   200,000 real IBM rows ingest through it.
2. ~~**CLI not rewired.**~~ **CLOSED.** All four verbs now dispatch to stage runners;
   `uv run oxbow --help` lists `ingest|graph|score|backtest|pipeline|eval|packet|
   verify-audit|verify-determinism`, and `tests/unit/test_anti_rubbish.py` fails the
   build if a claimed-complete phase's verb stops reaching its runner. The
   `NOT IMPLEMENTED in P0` banner is gone.
2a. **`make eval` overwrites authored documents.** The run that produced
   `README/ARCHITECTURE/MODEL_CARD/ECONOMICS_CARD/LIMITATIONS` also replaced
   `data/DATASET_CARD.md` with a shorter generated file, dropping measured figures the
   authored card carried — which is what broke
   `tests/contracts/test_p1b_canonical_ingest.py::test_counterparty_reuse_reported`.
   Judgement content (label definition and its limits, known biases, the sampling rule)
   cannot be regenerated, so eval must verify the card against the artifacts that own
   its numbers rather than write it.
3. **Canonical column list duplicated** — `contracts/canonical_v1.py` (21 columns) vs
   `graph/events.py::CANONICAL_EVENT_V1_COLUMNS` (20, no `batch_id`). One must import
   the other.
4. **Fold logic has no owning module** — roles and slicing live in `scoring/frame.py`
   and `features/registry.py`, while `config/splits.yaml` names a "P2 splits module"
   that does not exist. Plan §8 requires exactly one.
5. `rules/`, `models/`, `backtest/` are still docstring-only (those agents are
   mid-flight); `tests/contracts/`, `tests/integration/`, `tests/e2e/` are empty.
6. `scripts/verify.py` names `tests/contracts_adapters/`, which does not exist yet
   (P7's conformance suite).
7. Name collisions to unify: `QuarantineRecord` defined in both `ports/source.py` and
   `ingest/paysim.py`; repo-root discovery in three places; deterministic JSON
   serialisation in three places; webhook signature verification in both
   `adapters/signing.py` and `apps/api/echo.py`.
11. **`apps/api` described a guard that did not exist.** Both
    `schemas/common.py:13` and `routers/common.py:6` state that "a doctrine test walks
    the OpenAPI document and fails if any 2xx schema grows a `success` or `error`
    property". No such test existed anywhere in the repo. It now exists at
    `tests/integration/test_envelope_doctrine.py`: three checks pass, and the fourth
    (walking the real OpenAPI document's 2xx responses) **fails on purpose** until
    `apps/api/main.py` exposes the app — a skipped check is how a doctrine ends up
    existing only in a docstring.
    Note for whoever reads it next: `error` is banned at the *envelope top level* but
    allowed as a domain field (`RunDetail.error`, `JobStatus.error` are facts about a
    run, not a second status channel); a scanner that forbids the word outright would
    force sensible fields to be renamed.
12. **`adapters/goaml/xml.py:77` defaults money to no-currency.**
    `currency = str(txn.get("currency", "XXX"))` — `XXX` is ISO-4217's "no currency"
    code, so a canonical row that somehow lacks a currency emits a case packet to a
    AML system asserting the transaction had no currency, rather than failing. The
    canonical contract declares `currency` non-nullable, so the branch is
    unreachable-by-contract and wrong-if-reached: 03 A rule 1 wants a loud boundary
    failure, and a money figure whose currency was invented is the one class of error
    the whole no-implicit-FX rule exists to stop. Fix: raise, naming the txn_id.
    (Separately confirmed *not* a defect: `features/registry.py:216`'s "lorem ipsum"
    is an entry in a banned-substring list for feature descriptions — a guard, not a
    placeholder.) §19's marker grep over all `.py`/`.ts`/`.tsx` returns no other hits.
13. **CLOSED — `make lint` debt, measured rather than remembered.** The earlier
    figure in this slot ("208 ruff errors", with "405 passed, 6 failed" for the suite)
    is superseded and was kept here only as a record of how stale a lint claim goes: as
    of this session `ruff format --check` is green repo-wide (254 files), `ruff check`
    is down to 46 errors (15 unused test-fixture arguments, 7 docstring style, 6
    `isinstance` tuple form), and `mypy` carries 304 errors in 64 files led by
    `scoring/model.py` (87). Whole-suite measurement is now 789 passed / 8 failed / 1
    skipped, with the eight all PostgreSQL-dependent. The engine has since come back
    and `postgres`/`redis` report healthy, but that is the prerequisite, not the result:
    whether those eight pass is only known when `uv run pytest -q tests/integration`
    says so, and it has not been re-run at the time of writing. `make lint` remains red
    -- on `mypy` and those 46, no longer on formatting.
14. **CLOSED — the audit digest is no longer delimiter-ambiguous.** It used to join
    `HASH_VERSION | seq | occurred_at | actor_id | subject | action | canonical_json
    (payload) | prev_hash` with `"|"`, and since `subject`, `action` and payload string
    values can each contain a pipe, two different field splits could produce identical
    hash material -- weakening exactly the tamper-evidence property plan 15 claims.
    `compute_row_hash` now digests a canonical JSON **array** with quoted element
    boundaries (`_canonical_array`, `audit/chain.py:84`) and `HASH_VERSION` still first,
    so no field value can imitate a boundary. Proven by
    `test_audit_chain.py::test_adversarial_delimiters_in_any_field_still_separate_the_digest`,
    which constructs the split-shifting payloads and compares against the retired
    pipe-join to show the old scheme collided where the new one does not; 11 tests pass
    in that file.

8. Money parsing exists in four places (`ingest/canonical.py` expression + text forms,
   `ingest/paysim.py`, `ingest/ibm_aml.py`, `quant/money.py`). Not wrong yet, but it is
   where a cent-level divergence will appear first.
9. **R4's cycle definition is at odds with the corpus — see DEV-015.** Measured, not
   theorised: the enumerator is correct (a hand-built A→B→C→D→A returns exactly 1
   cycle through `build_graph`), the sampler was ruled out (component-intact selection
   over the 271 planted-cycle accounts plus their one-hop closure), and relaxing rails,
   non-increasing and the retention floor together **still returned 0**. The reason is
   in the annotations: of the corpus's 54 labelled CYCLE blocks, **all 54 close as
   directed loops, 38 are cross-currency, 25 breach the 0.6 retention floor, and 5 are
   not time-monotonic**. R4 as specified in plan §9 is therefore definitionally blind
   to 70 % of the only labelled cycles available. `data/ibm_cycle_measurement.json`
   carries the anatomy. The fix is a configurable, documented currency/retention
   policy plus a distinct two-leg round-trip pattern — **not** a quiet default change.

10. **`make packet` is blocked twice over, and neither block is the PDF library.**
    Running `uv run oxbow packet` refuses correctly -- no case bundle exists under
    `out/case_sink`, it says so, exits 1, and does not invent an exhibit. The reason
    there is no bundle is that P7's decision path has never executed, so the packet
    depends on the API being run, not on the renderer. Separately, WeasyPrint cannot
    import on this host at all (`import weasyprint` fails for want of libgobject/Pango;
    no GTK runtime is installed anywhere on the box, and none will be installed
    silently). So even with a landed case the PDF step would fail here. Both must be
    named in `LIMITATIONS.md`; only the first is this build's to fix.

11. **`make demo` was a phantom gate. The seeder now exists; the snapshot does not.**
    The target runs `$(PY) scripts/demo_seed.py --restore --boot-budget 90`. That script
    did not exist, and `data/snapshots/` was empty, so the plan §15 requirement -- "boots
    offline in under 90 seconds" -- had never been attempted while `make help` advertised
    it. `scripts/demo_seed.py` is now written (this session), and it is deliberately
    built to refuse rather than to fake it: it audits `score`, `backtest_fold` and
    `decision` first and names every gap, so on the present warehouse it exits non-zero
    with `scored rows: 0`, `backtest folds: 0`, `a landed reviewer decision: 0` rather
    than dumping an empty database that would boot a blank UI. It pins a manifest beside
    the dump (alembic revision, row counts, digest) and verifies that digest before a
    restore, and it is wired into P9 as a prerequisite-gated gate so a reviewer reads
    `SKIPPED-PREREQUISITE` with the missing path named instead of a passing name.
    `tests/unit/test_p9_demo_seed.py` covers it, including that a missing script in any
    Makefile recipe or phase gate fails the suite -- the general form of the phantom gate
    this item is about.
    **What still blocks it is item 12's finding, not the script.** A seeder's two jobs are
    to snapshot a warehouse holding scored rows, backtest folds and one landed reviewer
    decision, and to prove the stack reaches healthy within a budget on restore. The first
    job has nothing to snapshot until the score and backtest stages hand their results to
    the warehouse sink at all. Its ordering is therefore: wire the sink -> land a decision
    through the API -> then take the snapshot with the 90 s budget measured for real.

12. **`WarehouseSink.write` has no caller outside a test, so `score` and `backtest_fold`
    can never hold a row.** Measured this session, and not recorded anywhere until now.
    The port declares `write(self, table, run_id, rows)` (`ports/warehouse.py:188`), the
    Postgres adapter implements it, and `apps/api/worker.py:235` wraps it -- but nothing
    in `packages/` or `apps/` ever *calls* it. The one call site in the whole tree is
    `tests/integration/test_p7_api.py:348`, which writes rows to set up a test. What the
    score stage produces is Parquet under `out/features/<run>/` and `out/rules/<run>/`,
    and the backtest writes `out/backtest/<run>/ablation_results.json`; none of that
    reaches Postgres. Confirmed live against the compose database:
    `select count(*) filter (where n_live_tup>0), count(*) from pg_stat_user_tables`
    returns **3 of 44** -- `run`, `stage_event`, `job_run`. The 41 tables the API reads
    are structurally unreachable from the pipeline.
    This is what blocks items 10 and 11, and it is a design question rather than a patch:
    the score and backtest stages need to hand per-fold and per-row results to the sink,
    and `out/` and the warehouse need one stated relationship instead of two parallel
    stores nobody has reconciled. The P9 scorer and P6 allocator modules are landed and
    wired into the run path, so the arithmetic exists; the last hop to Postgres does not.

13. **The MIP binning's model build was unbounded, and it hung the unit suite.**
    `binning.time_limit_seconds: 20.0` reads like a per-feature time limit and is not one:
    it reaches ortools `SetTimeLimit`, which bounds `Solve()` and nothing else. The MIP
    solver spends most of its time *building* the model -- `add_constraint_monotonic_descending`
    is a double loop issuing one IPC call per candidate pair. Measured here, `solver: mip`,
    1200 rows, build against candidate count: 20 -> 0.60 s, 32 -> 2.28 s, 48 -> 7.35 s,
    64 -> 13.85 s. Quadratic, at 1.5-3.4 ms per pair, and dominant: at 300 rows build took
    13.7 s against 9.9 s solving. A yaml comment here claimed "100 candidates solve in
    ~0.1 s", measured on the default 5-fold corpus and wrong by two orders of magnitude.
    With the build unbounded, `test_p4_scorer.py::test_scorer_output_satisfies_the_scoreresult_contract`
    ran until pytest's own 300 s timeout with the stack parked in `mip.py`, and took the
    whole `tests/unit` run down with it at 68% (0 of 4 tests in that file completing).
    **Fixed this session:** `binning.build_budget_seconds: 6.0` plus
    `measured_ms_per_candidate_pair: 3.4` let `_effective_max_n_prebins` solve for the
    candidate cap the budget affords -- 42 on this host, down from the declared 100 -- and
    that cap is what reaches the solver. Deliberately *not* a wall-clock abort: the bin
    table is written into the hashed artefact, so aborting on load would make one input
    produce different bins on a busy machine. A feature needing more candidates than the
    budget allows takes the quantile table and records that in `boundary_source`. The file
    went 0 completed -> `4 passed in 188.69s`; guard added as
    `test_the_build_budget_bounds_the_mip_build_and_not_only_the_solve`.

14. **A full-suite number taken while workers are editing is not a verdict.** The one
    snapshot captured concurrently reported 114 failed / 538 passed / 34 errors,
    including every test in `test_p9_packet.py`; the same file run alone reports 28
    passed, 1 skipped. `make test` has to be read on a quiet tree, and the green claim
    in §16 belongs to that run only.

13. **RESOLVED — `test_p3b_r4_labelled_cycles.py` is green, and DEV-015's headline is
    wrong by three cycles.** The four failures are closed, and the "either the rules
    layer needs a real non-increasing reason aggregate, or the test invented a number"
    question above has a third answer: the aggregate exists, and 45 is what it measures.
    `loop_reasons` emits `REASON_AMOUNT_INCREASES` whenever `require_non_increasing` is
    set (`rules/cycles.py:270`), so the reason code is a first-class refusal reason and
    not only the per-hit evidence field at `rules/network.py:127`. The test counted it
    with the *shipped* settings, where `config/rules.yaml` sets
    `cycle_non_increasing: false`, so the key was absent from the dict rather than zero --
    `counts.get(...)` returned `None`, which is the failure that read as "no such
    aggregate". Measured with the knob on, exactly as plan §9 states the rule, the six
    counts reproduce DEV-015's recorded table including 45.

    Two of the four were test defects and one was a *claim* defect, which is the part
    worth keeping:

    * `non_increasing_breaches` counts consecutive legs only, not the wrap from the last
      leg back to the first, so DEV-015's quoted 9-hop ring has 3 breaches rather than
      the 4 a reader counting round the circle gets. Asserted with the arithmetic.
    * The end-to-end test fed the corpus's first labelled ring, which is 10 hops. R4's
      search horizon is `max_length + NEAR_MISS_DEPTH_SLACK` = 8, so the ring never became
      a loop and the near-miss ledger came back **empty** -- which reads as "nothing
      refused it" and is really "nothing looked at it". The test now picks a ring inside
      the horizon, and the corpus-free fallback folds DEV-015's quoted ring to four hops
      a day apart, because on a four-account fixture an R12 chain walk over the quoted
      three-hour spacing flags half the accounts and trips the corpus-level hit-rate
      ceiling before R4's ledger is read.
    * DEV-015's sentence "cannot fire on them … score zero on the labelled positive set"
      is **refuted**: the six counts are per-knob tallies that overlap, and their union
      is 51 of 54, not 54. Three single-currency Saudi Riyal rings of 3-4 hops, retention
      0.82-0.94, strictly increasing timestamps are refused by nothing, so shipped R4
      fires on three of the 54 (two with the non-increasing knob on). The decision entry
      is not amended: its *decision* re-specifies the knobs and assumed a zero to begin
      with, and the tests now pin both the per-knob table and the three survivors so the
      next reader cannot re-derive "score zero" by adding the columns together.

    The `test_p2_distinct_counts.py` setup errors (`config/config/features.yaml`) were the
    same class of mistake one layer over: the fixture passed the config *directory* to
    `registry_from_repo`, whose parameter is a repository root -- the exact two-meanings
    bug its own docstring warns about. 11 passed after the one-line fix.

14. **CLOSED — PaySim's `step` semantics, measured rather than argued.** The card's
   `step` range is 1–743 against `config/pipeline.yaml`'s `step_hours: 24`, i.e. the clock
   this build declares is **743 days**, not the 30 synthetic days an earlier draft assumed.
   The consequence is not cosmetic: `LIMITATIONS.md` §7 said five expanding folds with a
   30-day embargo "do not fit inside the observation period" and proposed shortening the
   fold design. Measured on the bytes: a 1,500,000-row PaySim ingest spans
   **2014-01-02 → 2014-05-24 (143 days)**, and `build_walk_forward` accepted it —
   `30d embargo over 2014-01-02T00:48:09 → 2014-06-12T11:48:49`, five folds, no knob
   touched. §7's sentence is therefore true of the **IBM** corpus (17.68 days) and false of
   PaySim, and it has to name which corpus it means. The `step_hours: 24` reading stays a
   declared assumption (the published simulator's own `step` is an hour; the plan's 30-day
   feature windows are only meaningful under the day reading) — the declaration is in
   `config/pipeline.yaml` and the card prints it as declared, not measured.

14a. **The dataset card's own figures were wrong, and the artifacts corrected them.**
    Running the new card verifier (`oxbow dataset_card`, wired into `make eval` as a
    verify-not-write step) found three: degree p90/p99 is **53 / 116**, not the 51 / 119
    the authored card carried; the IBM currency-name list I wrote included six names
    that do not occur in the file (Indian Rupee, Congo Franc, Malaysian, Romanian Leu,
    Turkish Lira, Vietnamese Dong) and now lists the 15 measured; and **PaySim's `step`
    column runs 1–743, not 0–30**, against a config that models the corpus as 30
    synthetic days. The first two are fixed. The third is unresolved and load-bearing:
    step semantics feed the temporal-split rationale and
    `LIMITATIONS.md`'s "the corpus window is 18 days", so it needs a decision about
    which claim is authoritative before either number moves.
    → resolved by item 14 above: the day reading is the declared one, the landed slice
    spans 143 days, and the fold plan accepts it without any knob being touched.

15. **CLOSED — the Docker engine came back and P7 was judged on it.** The blocker recorded
    below was real at the time (`docker info` failing on the Desktop Linux engine while port
    5432 still answered a PostgreSQL `SSLRequest` with `N`, turning eight database-backed
    tests into `server closed the connection unexpectedly`), and the diagnosis was right:
    a state change, not a missing prerequisite, and not something to fix by restarting
    someone else's VM without asking. The engine is up now and the verdict is in: **77
    integration tests pass** (see the ledger). `docker compose ps` shows postgres healthy
    on host port **5433** — host 5432 belongs to a different project's listener, which is
    why a bare `localhost:5432` probe is not evidence about this stack.

16. **A four-way audit found two gates that cannot pass and one that cannot fail.**
    Reports are in `docs/audit/` (`00-architecture.md`, `01-inventory.md`,
    `02-code-review.md`, `03-gates.md`), measured against HEAD `470b09c`.
    * **CLOSED — the P7 port-conformance gate was UNRUNNABLE.** `tests/contracts_adapters/`
      held one fixture and no test module, so the gate collected zero tests and exited 5,
      while `apps/api/routers/cases.py:482` cited
      `tests/contracts_adapters/test_watchlist_conformance.py` as its justification. That
      file now exists (22 tests, and it is the reason the OFAC adapter's `match_basis`
      vocabulary was corrected to stay inside `MATCH_BASES`), and the phase is claimed:
      `verify.py --phase P7` 3/3.
    * **CLOSED — the P4 gate was UNRUNNABLE.** `-k p4` matched nothing because no
      `test_p4_*.py` existed. Sixteen do now, and they found two real defects (DEV-020)
      rather than only describing code that works. The gate itself still has to be written,
      because a P4 gate worth having runs a scored corpus and there is not one yet.
    * **`make verify` being green does not mean the gates pass.** Bare `verify.py` calls
      `verify(PHASES)`, which runs only the gates of phases *marked done*; P7 is not, so
      its unrunnable gate is skipped rather than passed. Run a phase explicitly to see
      it. `470b09c`'s "record make verify green" is true as written and misleading as a
      summary.
    * **Fixed here: `Makefile` minted a `RUN_SALT` per invocation.**
      `export RUN_SALT ?= $(shell ... secrets.token_hex(32))` applied whenever the
      environment and `.env` were both empty, re-keying every account in the corpus on
      every `make` and making the two `verify-determinism` runs incomparable — the exact
      failure `oxbow.config.resolve_run_salt`'s own docstring names. Removed, with
      `test_the_makefile_does_not_invent_a_run_salt` added and verified to fail when the
      line is restored.
    * **Open, and deliberately not fixed blind: 20 raw PaySim account ids are committed**
      in `data/graph_measurement.json` (`C1065307291` and 19 more). That is the PII
      boundary the plan draws at ingest. The remedy is a history rewrite, which is a
      human's call, so it is recorded rather than performed.
    * **Missing and load-bearing, part-closed.** `apps/api/worker.py` now exists (with
      23 tests and the `api.worker.run_stages` name `jobs.py` had been enqueueing into
      nothing), and so do `apps/api/Dockerfile` and `apps/web/Dockerfile`, digest-pinned as
      the compose services already assume; compose also gained the `data/` and `out/`
      mounts without which the worker container cannot declare a source at all. Still
      missing: `scripts/demo_seed.py` (item 11), `PROMPT.md`, `notebooks/02_features.ipynb`
      and `notebooks/03_validation.ipynb` — all four named by the plan, none of them
      invented here as a checkbox.
    * The suite is otherwise green: **790 passed, 8 failed, 1 skipped in 427 s**, with
      all 8 failures in `tests/integration/test_p7_api.py` and item 15 the stated cause.
      `ruff check .` is at 45 errors, down from 85; the `F821` class is now zero.

16. **P4's current blocker is a native fault inside OSQP's algebra probe, not the
    bridge.** `uv run oxbow score --max-events 20000` now gets *past* features and dies
    with `Segmentation fault` (exit 139, reproduced on an idle machine) while the
    scorecard fit imports the solver stack; the deepest Python frames are
    `osqp/interface.py:33 algebra_available` -> `:48 default_algebra` ->
    `:59 default_algebra_module`, i.e. the import of `osqp.ext_builtin`. Ruled out by
    measurement, not assumption: `import optbinning` alone, `lightgbm+numpy+optbinning`,
    `osqp` alone, `osqp.ext_builtin` alone, the heavy stack then osqp, and osqp first
    then the heavy stack -- all exit 0. So it is not import order and not a missing
    binary; it needs the live stage context (plausibly numba/LightGBM's OpenMP runtime
    already resident when the extension loads). A temporary bypass exists -- pointing
    `OSQP_ALGEBRA_BACKEND` at a nonexistent key makes cvxpy skip OSQP with a
    `KeyError` and the process survives to the next, legitimate refusal -- but that is
    disabling a solver to dodge a crash, not a fix, and it is recorded as such rather
    than shipped.
    The stage's next blocker, seen once OSQP is skipped, is real and correct behaviour on
    a small slice: `REFUSED (fold plan): the embargo puts the training cutoff ...`,
    because a 30-day embargo against a 5k-row slice leaves no training window. P4 and
    P6 therefore need a genuine full-corpus run, which is minutes of compute rather than
    more diagnosis.

17. **`data/interim/<source>/run_manifest.json` is one mutable pointer, and any ingest
    re-points every downstream stage.** Found the hard way: a worker container run inside
    Docker executed `oxbow ingest` with a small limit, rewrote the PaySim manifest to 2,000
    rows over a 1-day window, and the next `oxbow score` refused at the fold plan —
    `the embargo puts the training cutoff (2013-12-03 …) before the training start` —
    because the corpus it "had" was now seven days short of one. Nothing was corrupted and
    nothing was wrong with the code: the manifest is the contract, and it had honestly
    changed. The same hazard is built into the P1b gate, which runs
    already. **`make verify` therefore changes what the score stage reads**, and a score run
    must re-assert its own corpus first. Two follow-ons:
    (a) per-run manifests under `data/interim/<source>/runs/<run_id>.json` with the flat
    file as a symlink/pointer would make the pointer explicit; (b) the score stage should
    print the landed window next to the embargo arithmetic when it refuses, since "the
    corpus you are reading is 1 day long" is the sentence the operator needed. Related, and
    already fixed: a manifest recorded **absolute** batch paths, so a container that wrote
    `/srv/data/interim/...` produced a manifest the host could not read and discarded a
    6.4M-event corpus as "absent on disk" — now relative on both sides, pinned by
    `tests/unit/test_p1b_manifest_path_portability.py`.

18. **The RQ worker has run real jobs, and four of them are in the database as `failed`.**
    Queried out of the compose Postgres (`job_run` joined to `run`), not inferred from a
    test: four `pipeline` jobs, all `state='failed'`, each with a named cause stored --
    `OSError: [Errno 30] Read-only file system: '/srv/data/interim/paysim/...parquet.partial'`
    (the compose file mounted `data/` read-only; ingest writes there, so the mount was
    widened to `./data:/srv/data` plus `./data/raw:/srv/data/raw:ro`),
    `StageChainFailedError: run 01M3FGE8... went red: ingest exited 1`, and two
    `OSError: [Errno 12] Cannot allocate memory` from `adapters/io.py:104`.
    Three things follow. The failure bookkeeping works: the run row and the job row both
    say `failed`, with the reason, and nothing hangs in `running` -- which is the property
    `jobs.py`'s enqueue guard exists to protect, now demonstrated on production-shaped data
    rather than only in `test_p7_worker.py`. The worker's `--demo`-free path executes:
    `docker logs oxbow-worker-1` shows it cleaning registries for queue `oxbow` and taking
    jobs. And **memory is the binding constraint on this host**, not CPU and not disk:
    a container running the full pipeline concurrently with a host-side score run and one
    agent is enough to hit `Errno 12` inside a 15.7 GB machine with ~2 GB free. That is the
    real explanation for the "import mlflow MemoryError" a worker once reported as a
    blocker, and it is why the score stage is run serially here rather than fanned out.

19. **`verify-determinism` at the score stage cannot be claimed on this host yet, and the
    sampler was the reason.** The slice is a pure function of the landed corpus, but the
    corpus is not a pure function of the bytes: `induced_subcorpus` took the *head by time*
    of the induced mutual set, so a re-ingest that minted different batch ids reordered the
    head and changed the slice's span (18 days vs 162 for the same command and seed). Fixed
    in `fix(p2)` by striding across the ordered set instead of cutting its head; the
    endpoints and the coverage are now asserted in `tests/unit/test_p2_features.py`. What is
    still owed is the double-run comparison at the score stage itself
    (`scripts/verify_determinism.py --command "uv run oxbow score --max-events 40000"
    --artifacts out/score`), which needs one score run to finish before there is an
    artifact pair to compare.

## Verification ledger (run by the orchestrator, not reported by agents)

§16 requires a gate to be run in-session with observed output, so this is the list of
what has been independently executed here, with the number that came back. Anything
not on this list is *not* verified, regardless of what a package's own tests claim.

| What | Command | Result |
| --- | --- | --- |
| Full PaySim corpus | `uv run oxbow ingest -s paysim` (no limit) | **6,362,620 canonical events across 64 batches, 0 quarantined, 0 silently coerced, 252.7s**, window `2014-01-02 → 2016-01-14` = **743 days**, which is the `step 1–743 × step_hours 24` reading asserted in item 14 |
| The worker, in production | `docker exec oxbow-postgres-1 psql ... -c "select state, kind, error from job_run"` | **4 `pipeline` jobs, all `failed`, each with a named cause stored** (`Errno 30` read-only mount, `Errno 12` cannot allocate memory ×2, `StageChainFailedError: run ... went red: ingest exited 1`); the matching `run` rows are `failed` too, so nothing is left `running`. Item 18 |
| Sampler coverage after `fix(p2)` | `uv run pytest -q tests/unit/test_p2_features.py -k slice` | **2 passed** — slice spans ≥ 90% of the corpus timeline, stride keeps both endpoints of the frame it is handed, is ordered, and repeats identically |
| P2 gates after the sampler change | `uv run pytest -q tests/test_leakage.py` / `tests/unit -k p2` | **19 passed** and **79 passed** (was 78; the new test is the +1) |
| Score at the configured slice | `uv run oxbow score` (500,000-event target) | **did not finish in 1h47m** (72 CPU-minutes) while two agents shared the host; killed. The lesson is the same one that withdrew the P3a graph gate: a phase gate that cannot finish is not a gate |
| P7 gate set, this session | `uv run python scripts/verify.py --phase P7` | **3 gates run, 3 passed** — 22 conformance, `Contracts: 4 kept, 0 broken`, and **77 passed in 160.62s** across the API, worker, session hygiene and envelope doctrine |
| Append race, isolated | `uv run pytest -q tests/integration/test_p7_api.py` | **41 passed**. The two order-dependent assertions in `test_concurrent_append_gives_one_success_and_one_409` are now scoped to the race itself (`chain_seq == winner + 1`, counts by `trace_id`). Mutation-proven: `+ 2` produced `E assert 2 == (1 + 2)` |
| The scorecard solver actually runs | seeded 4,000-row monotone signal through `fit_feature_binning` | `boundary_source='optbinning-mip'`, eight value bins, bad rates `0.011 → 0.711` monotone, `monotonic_direction='ascending'`, identical table inside and outside the pytest warning filter. Before DEV-020 every numeric feature came from `quantile-fallback` |
| **First score run to reach the end** (2026-09-27) | `uv run oxbow score --max-events 40000` on the full 6,362,620-event corpus | **finished, exit 0.** rules 170 hit rows after R10/R11/R12 were excluded at `hit_rate_floor`; features 75 published, spec hash `b622c5f6…`; grain bridge **79,998 rows × 75 features, 108 positives, base rate 0.001350**; **fold 0 landed 28,588 scored rows with SHAP**, `calibrated=false` (14 validation positives < 50); **folds 1-4 skipped**, each `LightGBMError: bad allocation`. The RESULT line previously printed "5 fold(s) scored" from the entry count — it now prints `1 of 5` and each skip's reason |
| The backtest on real bytes for the first time | `uv run oxbow backtest --corpus out/score/01M3FZ2GC3J71AYT1QEKPWEDKJ/backtest_corpus.parquet` | **refused, correctly**: `FoldError: fold 0: embargo gap is 0.00d but the embargo is 30d`. Every P6 number before this came from `--demo-fakes`. The guard is not being loosened to get past it |
| P8's three gates, through the gate runner | `uv run python -c "…verify._run_gate(g) for g in PHASES['P8'].gates"` | `[('unit tests…','PASS'), ('typecheck…','PASS'), ('biome lint and format…','PASS')]` — the same argv the phase table holds, executed rather than hand-run |
| Full web suite, this session | `node node_modules/@playwright/test/cli.js test` (cached chromium, fixture app on :3100, live app on :3101, uvicorn on :8123) | **24 passed, 1 recorded skip, 1 red** in 16.1m. Separately: axe **10/10 routes clean**, cls **3/3** with `/alerts 0` and `/cases 0`, and the three reworked files **11 passed / 1 skipped / 1 red** in 1.9m |
| The reduced-motion fix, both paths | `page.emulateMedia({reducedMotion:'reduce'})` and `/dev/states?motion=reduced` | OS path: 49 sweep elements at the first frame → **0** once hydrated, `running=0`. Query path: 49 present, **0 visible, running 0** (`animation-duration: 1e-06s`), `data-motion="reduced"`, document height **4549 unchanged** on both paths. Before this session `data-motion` had **no writer at all**, so the forced path's entire CSS block matched nothing |
| The JS-toolchain gate can fail | mutation: `packageManager` → a name that is not the declared one, then restore | **restated after DEV-022.** This row used to record `packageManager` → `bun@1.4.2` turning the gate red, because the guard asserted `startswith("pnpm@")`. The guard's subject moved to Bun, so that mutation now passes and the old entry would have been a stale claim left standing. Re-measured on the current guard: renaming `packageManager` away from `bun@1.4.2`, or renaming the top-level `overrides` key back to `pnpm`, each turns `test_the_declared_js_toolchain_is_the_one_every_recipe_uses` red; so does pointing `make lint-web` at `pnpm lint`. Restored state green |
| The swap changed no dependency bytes | diff `bun.lock` against the `pnpm-lock.yaml` at `HEAD~1` in git, matched on `name@version` by registry hash | **438 resolved packages each side, identical sets, 438/438 sha512 matches, 0 version drift, 40/40 declared deps at their exact pin**; `@tailwindcss/oxide`, `oxide-win32-x64-msvc`, `@tailwindcss/node` locked at 4.0.0. Only 3 packages declare a lifecycle script (esbuild, sharp, biome) and all three were exercised working; the fresh `bun run build` CSS carries `--tw-` properties, which only the oxide napi binding writes |
| The edge is the browser's only origin | `COMPOSE_PROFILES=full API_PORT=8011 docker compose up -d --no-build web caddy`, then curl through `:8080` | caddy **healthy** off its 2019 admin probe; `/` → 307 → `/dashboard` 200, `/cases/<id>` and `/dev/states` 200; `/api/runs` 401 `application/problem+json` unauthenticated and 200 with a token minted at the edge; `/api/nope` 400 from FastAPI, i.e. the `/api` prefix arrived unstripped. From inside the web container `api:8000/healthz` → **200** where the previously-baked `127.0.0.1:8000` → **ECONNREFUSED**, which is the defect DEV-023 found and fixed. Not measured: per-frame SSE flushing (see BACKLOG) |
| Web suite on the Bun toolchain | `bun run test:unit --run`; `bun run typecheck`; fixture build + `next start` on :3100 + `bun run test:e2e` | **51/51 unit over 11 files**, typecheck clean, **e2e 23 passed / 3 skipped / 0 failed** in 2.4m. The vitest process printed Node's DEP0205 and Vite's CJS-API warning, which is the evidence Bun ran the task and Node ran the tests. The 3 skips need the live API origin; Docker was down at that moment |
| The API served, and the proxy proved | `uv run uvicorn main:app --port 8123 --app-dir apps/api` with `OXBOW_WAREHOUSE=null`; web rebuilt with `OXBOW_API_ORIGIN=:8123` | `/healthz 200`, `/api/meta/dataset 200` and `/api/graph/subgraph 200` **with a minted demo token**, `401 application/problem+json` without one. Host :8000 is Windows `Microsoft-HTTPAPI/2.0` (PID 4), which is why every browser check before this returned a 401 that was not the API's |
| P4 guardrail tests | `uv run pytest -q tests/unit -k p4` | **16 passed** (9 guards + 6 calibration/fusion/explain + the scorer seam) |
| 1.5M-row ingest | `uv run oxbow ingest -s paysim --limit 1500000` | **1,500,000 canonical events, 0 quarantined, 0 silently coerced, 69.4s**, window `2014-01-02 → 2014-05-24` = 143 days, which is what makes the 30-day embargo arithmetically satisfiable |
| Score on a bounded slice | `uv run oxbow score --max-events 120000` | killed by a 90-minute budget: the rules-layer graph alone took ~65 min for **173,031 nodes, 0 cycles, 23,646 communities** |
| Score on the run now in flight | `uv run oxbow score --max-events 40000` | rules **736 hits over 72,135 accounts, 11 rules below the hit-rate floor**, graph `0 cycles / 6,150 communities`; features 75 published; **fold plan accepted** (`30d embargo over 2014-01-02 → 2014-06-12`); per-fold model stack running at the time of writing |
| First browser run of the web app | `./node_modules/.bin/playwright test` (chromium from the ms-playwright cache) | **21 passed, 5 failed in 5.8 min.** Measured CLS `/alerts 0.0153` (gate: zero), `/cases 0.00059`, `/dev/states 0.00072`; axe 0 critical/serious on every route sampled. After two fixes the gallery file is 3 passed / 2 failed |
| Web static + unit | `tsc --noEmit` / `biome check .` / `vitest run` | **0 type errors**, 109 files clean, **51 tests in 11 files all passing** |
| P0 toolchain after the guard fix | `uv run pytest -q tests/unit/test_p0_toolchain.py` | **44 passed** (was 3 failed when the import-order guard asserted `osqp` before `pyarrow`; see DEV-021) |

| Completed-phase gates as claimed | `uv run python scripts/verify.py` | superseded by the per-phase rows below, run after DEV-016/017/018 landed |
| P1a gates | `uv run python scripts/verify.py --phase P1a` | **2/2 PASS** — PaySim and all three IBM members match their recorded SHA-256 against bytes on disk (475 MB + 34 MB + 324 KB) |
| P1b gates | `uv run python scripts/verify.py --phase P1b` | **3/3 PASS** — 28 contract tests; 200,000 canonical events with 0 quarantined and 0 silently coerced from PaySim **and** from IBM through its own adapter. Held pending on the dataset-card deliverable, not on ingest |
| Byte determinism (01 §D, DEV-007) | `uv run python scripts/verify_determinism.py --command "uv run oxbow ingest --source ibmaml --limit 50000"` | **67 artifacts byte-identical across two runs**, including the IBM batch whose txn ids are hashed in a process pool. This gate had never actually executed before: it read RUN_SALT from the environment while the CLI resolves it from `.env`, so it refused in every shell where ingest works |
| `make verify`, full claimed set | `uv run python scripts/verify.py` | **did not finish in 26+ minutes** while the graph-determinism gate was in the claimed set: `oxbow graph` has no input-root option, so it re-read every batch sitting in `data/interim` (grown to 600k+ events across three ingest runs) and ran that twice. Gate withdrawn -- see verify.py's comment. Graph determinism was already proven once by hand on a bounded corpus: **5 artifacts byte-identical across two runs** |
| `make verify`, all claimed phases | `uv run python scripts/verify.py` | **14 gates run, 14 passed, exit 0** after P2 and P3b were claimed (was 10/10): P0 3, P1a 2, P1b 3, P2 2, P3a 1, P3b 2, P5 1 -- seven phases asserted together |
| Full suite, quiet tree | `uv run pytest -q tests` | **788 passed, 9 failed, 1 skipped in 5m48s** after every worker had reported. The 9: 8 in `tests/integration/test_p7_api.py` needing PostgreSQL through a Docker engine that went down mid-session (item 15), and 1 grain-bridge test now fixed. The earlier "114 failed" figure was a mid-flight snapshot and is superseded |
| `make lint`, truthfully | `ruff check .` / `ruff format --check .` / `mypy packages/pipeline apps/api scripts` | **not green, and wider than previously recorded**: 85 ruff errors (39 auto-fixable), **133 files would be reformatted**, and **304 mypy errors across 64 files** — clustered in `scoring/model.py` (87), `scripts/measure_ibm_cycles.py` (34), `models/calibration.py` (22), `features/compute.py` (21). `make lint` runs all three legs, so it has been failing repo-wide |
| Grain bridge and fold providers | `uv run pytest -q tests/unit/test_p2_grain_bridge.py` | **17 passed** — the txn→account seam, the per-fold graph provider and the rule-hit provider exist and are tested |
| End-to-end pipeline | `uv run oxbow pipeline --limit 20000` | ingest → graph → score now execute (two composition-root defects kept the run from passing stage 1: a stage finalised the run and the root's own finish hit the "completed run does not change state" guard; `registry_from_repo` read its argument as a config dir while its name, signature and every CLI caller meant a repo root). Score lands **75 features × 40,000 rows, 406 rule hits**, then refuses at three named unwired seams: no txn→account grain bridge into `build_training_frame`, `registry.py` and `scoring/frame.py::canonical_spec_hash` disagreeing on the spec digest (so the stale-feature-spec guard cannot pass), and no `GraphFeatureProvider`/`RuleHitProvider`, which is why 16 fold-scoped columns are 100% null. This is the first time P4's real gap has been observable rather than asserted |
| Packet refusal | `uv run oxbow packet` | **exits 1, names the missing case bundle, invents nothing** — see punch-list item 10 |
| IBM/contract suites | `uv run pytest -q tests/contracts/test_p1b_canonical_ingest.py tests/unit/test_p1b_ibm_aml.py` | **137 passed** (then 28 of them inside the P1b gate) |
| P4's reported mlflow blocker | `uv run python -c "import mlflow"` | **not a blocker** — imports fine at 2.19.0. An agent's MemoryError was host load under concurrent runs, and is recorded here so nobody re-escalates it |
| Graph layer | `uv run pytest -q tests/unit/test_p3a_graph.py tests/unit/test_p3a_cycles.py` | **61 passed** |
| Quant layer (P5) | `uv run pytest -q tests/unit -k p5` | **89 passed** (economics, allocate, exposure, frontier, monte carlo) |
| Cycle enumerator is not broken | hand-built `A→B→C→D→A` through `build_graph` | exactly **1 cycle** found, not truncated |
| Typology join alignment | `Is Laundering` share of joined CYCLE rows | **287/287 = 1.0** — ordinals line up |
| IBM network thesis | `uv run python scripts/measure_ibm_graph.py` | median degree **10.0** (> 2 passes), 647,939 edges |
| IBM cycle reality vs R4 | `uv run python scripts/measure_ibm_cycles.py` | **0 found** under §9's definition → DEV-015 |
| Tree syntax after interrupted agents | `py_compile` over every `.py` | clean |
| `any` / unbuilt-stage gate | `uv run pytest -q tests/unit/test_anti_rubbish.py` | 3 passed |
| Golden fixture self-check, no rule code | `uv run pytest -q tests/golden` | **27 passed**; two builds byte-identical (`49f6c3bd…`) |
| Golden fixture covers all 12 rules | `expected.yaml` scenarios parsed by rule × kind | **R1–R12 each have ≥1 positive and ≥1 near-miss**; near-misses are discriminating, not repetitive (`tau, not k`, `window, not count`, `time-reversed loop returns nothing`, `the UTC-trap case` = 03 §C's local_hour trap) |
| Envelope doctrine vs the live app | `uv run pytest -q tests/integration/test_envelope_doctrine.py` | 4 passed |
| P0 + P1a gates after the registry landed | `uv run pytest -q tests/unit/test_p0_toolchain.py tests/unit/test_p0_design_system.py tests/unit/test_p1a_sources.py tests/unit/test_anti_rubbish.py` | **121 passed** |
| Feature registry declares what the plan asks | inspect `config/features.yaml` via the registry | 75 model features + 21 intermediates (target 60–75 met); longest window 30 d == `max_lookback_days` == embargo |

### Commits on `main` so far
`9d1a313` P3a graph · `433bc24` P5 quant · `3489755` P3b golden fixture ·
`42e5968` P1/P2 evidence + gate tooling. Each was staged only after its own tests
were run in the same session; `config/` files still under edit by live agents were
deliberately left out of those commits.

Still unverified and pending: P1b's 14 named contract tests (`tests/contracts/` is
being written), `make ingest` end to end, P2's leakage gate, P3b's rule-vs-fixture
gate, P4b, P6's ablation, P7's API gates, P8's browser gates, P9's packet/demo. The
IBM adapter's schema is known-wrong (item 1) and its correction is unscheduled work.

## Where this stands

Eight phases are claimed on gates run in this session: **P0, P1a, P1b, P2, P3a, P3b,
P5, P7**. `make verify` therefore asserts seven of them plus P7's three gates; the eighth
(P8) has a gate that runs (vitest, 51 tests) and a `pending_reason` that states what is
still red rather than a claim.

Three phases remain open and they are open for one reason, in one order:

1. **P4 needs one scored run.** The blockers were found by running, not by reading: the
   OSQP native fault (DEV-021), the monotonic binning that never ran behind a swallowed
   `TypeError` (DEV-020), the ledger that inferred its own schema, the all-structural-zero
   feature that killed five folds, and the sum-of-squares that overflowed `i64`. Each is
   fixed and mutation-proven. What is left is arithmetic against wall clock: the 40k
   connected slice over the full 6,362,620-row corpus spends most of its time in
   `build_graph` and the per-fold graph rebuilds, and `--max-events 40000` has not yet
   completed here.
2. **P6 runs `oxbow backtest --corpus <the frame P4 lands>`.** The harness, the eight-row
   ablation including the deliberately leaking control arm, the economics and the fairness
   checks are all built and unit-tested (19 metric tests); no fold has produced a real
   number, which is why MODEL_CARD and ECONOMICS_CARD still carry placeholders.
3. **P9 is downstream of both**: the packet needs a case bundle, and a case needs scored
   alerts; the demo seeder needs a warehouse holding folds, scores and one landed decision;
   the generated docs read eval output. The hosted demo is separately blocked on the
   share-alike licensing decision (00 §I.4) and will not be published unilaterally.

Two things about this host that the plan's wording hides

1. **`make` does not exist here.** `make`, `mingw32-make` and `gmake` are all absent
   from PATH, so every gate §16 phrases as `make lint` / `make test` / `make verify`
   has been run as the underlying command instead (`uv run ruff check .`,
   `uv run pytest -q tests`, `uv run python scripts/verify.py`, `uv run lint-imports`).
   The substance is covered; the literal target names are not executable on this
   machine. The web recipes do run once invoked directly, though: `make web`, `lint-web`
   and `test-web` are `bun run` forms now (DEV-022), and bun is installed, so the only
   missing layer is `make` itself -- the frontend checks were run as `bun run lint`,
   `bun run typecheck` and `bun run test:unit --run` rather than as raw
   `./node_modules/.bin/...`. A CI runner with GNU make is the place these
   become the plan's own commands.
2. **A worker kept committing to `main` after reporting it had finished, and the
   orchestrator mis-read that as corruption.** Sequence: the webhook/env commit landed,
   then two commits appeared that the orchestrator did not author (`bfc05eb` "four-way
   audit", `1aa42bc` pytest-cache `.gitignore`), after which `tests/unit/
   test_env_declarations.py` seemed deleted and `.env.example` truncated. The real
   explanation is that the agent's commits moved HEAD underneath a working tree holding
   newer uncommitted edits, so the apparent "deletions" were the diff between two
   writers' states, not a revert of committed work. Recovery was correct anyway -- the
   three files newer than HEAD were preserved to a temp directory, `git checkout -- .`
   restored HEAD, the files came back, and 42 gate tests pass -- but the *first*
   diagnosis written into this file and into commit `8daa862`'s message was wrong: it
   blamed the orchestrator's own `git add -A` for sweeping in `docs/audit/*`, which that
   agent had in fact committed itself in `bfc05eb`. Corrected here rather than quietly
   edited, because a wrong root cause is the thing that gets repeated.
   The audit scratch files are removed from the tree; copies are parked at
   `~/qoder-parked-oxbow-audit/`. Working rule for this repo, earned twice over:
   confirm no worker still holds the tree before `git add -A` or `git checkout`.



The last whole-suite number on a quiet tree was **789 passed / 8 failed / 1 skipped**
at `0e8a5b8`. That number is superseded at the edges: P7's eight failures were the
Docker engine being down and are now green (77 integration tests, `verify.py --phase
P7` 3/3), and the tree has since gained the P4 guard tests, the P8 suites and the
worker. A fresh full-suite number is owed and belongs to the next quiet tree, not to
this one -- several agents were editing concurrently for most of the session, and
p.12 of the punch list records exactly why a mid-flight suite number is not a verdict.

The three things left, in order, each unblocking the next, are written at the top of
this file under "Where this stands": one scored run (P4), one backtest over it (P6),
then the packet, the generated docs, the demo snapshot and the notebook figures that
all read from those numbers (P9).

Blocked on a human, not on work: the hosted read-only demo publishes CC BY-SA /
CDLA share-alike derived data and needs a licensing decision (BACKLOG.md, 00 §I.4).
The PaySim `step` question that used to sit here is resolved -- see item 14: the
declared clock is `step_hours: 24`, the landed corpus spans 743 days, and the fold
plan accepts a 40k slice of it.

## Reviewed and sound (read, not merely reported)

- `scoring/scale.py` + `scoring/config.py`: `factor = PDO/ln2`,
  `offset = base − factor·ln(base_odds)`, so `score_at_odds(50) = 600` exactly;
  `base_points = offset − factor·intercept` and per-bin points
  `−factor·βⱼ·WOEⱼ` — signs work out so a risky bin *subtracts* points, which is what
  makes the plan's "minus 48 points" reason code true. Points are integers and the
  score is defined as their sum (auditable by construction), with the deviation from
  the continuous score measured rather than asserted. One wart: when
  `round_points_to_integer` is false the code **truncates** via `int()` instead of
  keeping a float — a misleading branch to fix or delete.
- `graph/cycles.py`: single-currency loops only (a converted loop is FX, not a
  round-robin), non-increasing amounts, retention floor, all-zero loops rejected
  instead of treated as perfect retention, rotation-to-minimum dedup so the same
  cycle entered at a different point is not counted twice, and budget/timeout that
  set `cycle_search_truncated` and report the count as a lower bound.
- `quant/ev.py`: `EV_i = p_i·E_i·r − c_i − (1−p_i)·f` is implemented in integer minor
  units with probabilities carried as micro-ratios, so `Money * micro/1e6` never
  touches a float; currency disagreement **raises** rather than converting; a
  sub-floor `m_i` raises because it is the density denominator; and a scored account
  with no exposure (or the reverse) fails the run instead of pricing at a silent
  zero. Ordering is `(-density_ratio, account_key)`, i.e. the deterministic tie-break
  plan §10 asks for.
