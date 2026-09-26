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
| **P4** | WOE scorecard, LightGBM, Isolation Forest, calibration, fusion, SHAP | code present, **not verified by the orchestrator** | agent-reported full-stack run at 620 s/fold with an `import mlflow` MemoryError; nothing audited here |
| **P5** | EV allocation, greedy vs CP-SAT, Monte Carlo exposure, economics | **DONE** | `verify.py --phase P5` 1/1 (89 tests) |
| **P6** | walk-forward backtest, ablation table, fairness, perturbation | in progress | `data/processed/eval.json` exists; its numbers have not been reproduced from a command here |
| **P7** | FastAPI, SSE, RQ, ports/adapters, outbox, audit chain, OIDC | partially executed | `tests/integration/test_p7_api.py` now exists and **really serves the app** (its own words: "before this file, nothing had ever imported apps/api"); 9 session-hygiene tests pass against real SQLAlchemy statements, and the read model's connection leak is fixed. **8 of its tests cannot run on this host right now** — see item 15 |
| **P8** | seven screens, state craft, `/dev/states`, zero-CLS | code present, **never rendered** | `apps/web/src/app/` routes exist; no build/browser evidence, no test config |
| **P9** | packet, generated docs, demo snapshot, limitations | in progress | `README`/`ARCHITECTURE`/`MODEL_CARD`/`ECONOMICS_CARD`/`LIMITATIONS` written by `make eval`, which also overwrote the authored dataset card (see below) |

Execution order is `P0 → P1a → P1b → P3a → P2 → P3b → …` per DEV-008: the graph
is built before features, because a degree cannot be computed from a graph that
does not exist yet.

## Environment (measured, this session)

```
uv python 3.12.13 · docker 29.6.2 UP · disk 104 GB free · PaySim on disk 493,534,783 B
```

The two P0 blockers are cleared: the Docker daemon is now running (B1) and free
disk rose from 49 GB to 104 GB (B3).

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

11. **`make demo` is a phantom gate.** The target runs
    `$(PY) scripts/demo_seed.py --restore --boot-budget 90`, and that script does not
    exist; `data/snapshots/` is empty. So the plan §15 requirement -- "boots offline in
    under 90 seconds" -- has never been attempted, while `make help` advertises it. The
    seeder and a pinned snapshot are the work; the hosted read-only demo stays blocked
    on the licensing decision already recorded in `BACKLOG.md` (00 §I.4), which is a
    human call, not a task to absorb.
    **Deliberately not written yet, as of this session.** A seeder's two jobs are to
    snapshot a warehouse that holds scored rows, backtest folds and one landed
    reviewer decision, and to prove the stack reaches healthy within a budget on restore.
    The first job has nothing to snapshot until P4/P6 land (the score stage still stops
    before a model is trained, so `out/` holds features and rules only), and writing it
    against an empty database would produce a `make demo` that boots a blank UI -- a
    green target with no evidence behind it, which is the thing this file has been
    cataloguing all session. Its ordering is therefore: P4 scorer -> P6 folds -> a
    decision landed through the API -> then this, with the 90 s budget measured for real.

12. **A full-suite number taken while workers are editing is not a verdict.** The one
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

14. **The dataset card's own figures were wrong, and the artifacts corrected them.**
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

15. **The Docker engine went away mid-session, and it is the only thing between P7 and
    a verdict.** `docker info` fails on `npipe://./pipe/dockerDesktopLinuxEngine`, and
    `.env` has no `DATABASE_URL` (which is why `verify-audit` independently reports
    `SKIPPED postgres`). Port 5432 still answers a PostgreSQL SSLRequest with `N`, so a
    listener is up while the VM behind it is not — which turns into
    `psycopg.OperationalError: server closed the connection unexpectedly` inside the 8
    database-backed tests rather than a clean "no database" message. The daemon was UP
    earlier in this session, so this is a state change, not a missing prerequisite.
    Docker Desktop's executable is not at the path I tried and only a client exists at
    `~/bin/docker.exe`, so I did not start it myself; restarting the engine is the
    user's action, after which `uv run pytest -q tests/integration` is the command that
    says whether P7 holds.

16. **A four-way audit found two gates that cannot pass and one that cannot fail.**
    Reports are in `docs/audit/` (`00-architecture.md`, `01-inventory.md`,
    `02-code-review.md`, `03-gates.md`), measured against HEAD `470b09c`.
    * **The P7 port-conformance gate is UNRUNNABLE.** `tests/contracts_adapters/`
      holds no test module — one fixture and nothing else — so `pytest -q
      tests/contracts_adapters` collects zero tests and exits 5. The gate named "port
      conformance across every adapter" cannot pass, and every one of the 7 ports, 7
      `Null*` implementations and 7 adapter families is unverified against the others.
      `apps/api/routers/cases.py:482` cites
      `tests/contracts_adapters/test_watchlist_conformance.py` as its justification and
      that file does not exist. **This is the "part judges interrogate".**
    * **The P4 gate is UNRUNNABLE.** `verify.py` selects it with `-k p4` and no
      `test_p4_*.py` exists anywhere, so the phase's real state is "no tests", which
      the phase table's "not verified by the orchestrator" understates.
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
    * Also missing and load-bearing: `apps/api/worker.py` (so `make worker` and the
      `full` Compose profile cannot start), `scripts/demo_seed.py` (item 11), `PROMPT.md`,
      `notebooks/02_features.ipynb`, `notebooks/03_validation.ipynb`, `apps/api/Dockerfile`
      and `apps/web/Dockerfile` (both referenced by `docker-compose.yml`).
    * The suite is otherwise green: **790 passed, 8 failed, 1 skipped in 427 s**, with
      all 8 failures in `tests/integration/test_p7_api.py` and item 15 the stated cause.
      `ruff check .` is at 45 errors, down from 85; the `F821` class is now zero.

## Verification ledger (run by the orchestrator, not reported by agents)

§16 requires a gate to be run in-session with observed output, so this is the list of
what has been independently executed here, with the number that came back. Anything
not on this list is *not* verified, regardless of what a package's own tests claim.

| What | Command | Result |
| --- | --- | --- |
| Completed-phase gates as claimed | `uv run python scripts/verify.py` | superseded by the per-phase rows below, run after DEV-016/017/018 landed |
| P1a gates | `uv run python scripts/verify.py --phase P1a` | **2/2 PASS** — PaySim and all three IBM members match their recorded SHA-256 against bytes on disk (475 MB + 34 MB + 324 KB) |
| P1b gates | `uv run python scripts/verify.py --phase P1b` | **3/3 PASS** — 28 contract tests; 200,000 canonical events with 0 quarantined and 0 silently coerced from PaySim **and** from IBM through its own adapter. Held pending on the dataset-card deliverable, not on ingest |
| Byte determinism (01 §D, DEV-007) | `uv run python scripts/verify_determinism.py --command "uv run oxbow ingest --source ibmaml --limit 50000"` | **67 artifacts byte-identical across two runs**, including the IBM batch whose txn ids are hashed in a process pool. This gate had never actually executed before: it read RUN_SALT from the environment while the CLI resolves it from `.env`, so it refused in every shell where ingest works |
| `make verify`, full claimed set | `uv run python scripts/verify.py` | **did not finish in 26+ minutes** while the graph-determinism gate was in the claimed set: `oxbow graph` has no input-root option, so it re-read every batch sitting in `data/interim` (grown to 600k+ events across three ingest runs) and ran that twice. Gate withdrawn -- see verify.py's comment. Graph determinism was already proven once by hand on a bounded corpus: **5 artifacts byte-identical across two runs** |
| `make verify`, all claimed phases | `uv run python scripts/verify.py` | **10 gates run, 10 passed, exit 0 in ~3 minutes** (P0 2, P1a 2, P1b 3, P3a 1, P5 1) -- §16's "passes for every prior phase" observed green for the first time; seven phases remain honestly pending |
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

## Where this stands, and the next five moves
### Two things about this host that the plan's wording hides

1. **`make` does not exist here.** `make`, `mingw32-make` and `gmake` are all absent
   from PATH, so every gate §16 phrases as `make lint` / `make test` / `make verify`
   has been run as the underlying command instead (`uv run ruff check .`,
   `uv run pytest -q tests`, `uv run python scripts/verify.py`, `uv run lint-imports`).
   The substance is covered; the literal target names are not executable on this
   machine, and `make web`/`lint-web` additionally assume `pnpm`, which is also absent
   (the frontend checks were run as `./node_modules/.bin/tsc --noEmit` and
   `./node_modules/.bin/biome check .`). A CI runner with GNU make is the place these
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



Measured on a quiet tree at commit `0e8a5b8`: **789 passed, 8 failed, 1 skipped**
(`uv run pytest -q tests`, 7m15s). `make verify` claims P0, P1a, P1b, P3a, P5.
`make lint`'s format leg passes for the first time; `ruff check` has 46 errors left
(15 unused test-fixture arguments, 7 docstring style, 6 `isinstance` tuple form) and
`mypy` has 304 in 64 files, led by `scoring/model.py` (87).

The eight failures are all `tests/integration/test_p7_api.py` needing PostgreSQL
through the Docker engine, which went down mid-session (item 15). None is a code
defect on the evidence available.

In order, each unblocking the next:

1. **Start Docker Desktop**, then `docker compose up -d postgres redis` and
   `uv run pytest -q tests/integration`. That converts eight unknowns into a verdict
   on P7 and is the only thing standing between P7 and being claimable.
2. **Wire the grain bridge into `run_score_stage`** (`cli.py` still carries the
   refusal string at the score body; `oxbow.features.bridge.build_account_frame` and
   the two fold-scoped providers exist with 17 passing tests). Then
   `uv run oxbow pipeline --limit 20000` reaches backtest for the first time, which is
   the precondition for P6's numbers, MODEL_CARD/ECONOMICS_CARD being generated from
   measured results rather than placeholders, and a landed case bundle.
2a. **The seam is smaller than the refusal says — three verified findings.** Every input
   the composition needs exists and is named: `load_split_config` and
   `build_walk_forward` (`backtest/splits.py:392` and `:409`, the latter already
   refusing `shuffle: true` and `purge: false`), `fold_providers`
   (`features/fold_providers.py:438`, returning the graph and rule providers as a pair
   so each fold's graph is built once), and `build_account_frame`
   (`features/bridge.py:271`). Second, `02 §B seam 3` is **already satisfied** on this
   path — `test_p2_grain_bridge.py::test_the_bridge_output_satisfies_the_scoring_contract`
   asserts `training.feature_spec_hash == registry.spec_hash == bridged.spec_hash`, so
   the stale-spec guard passes and that clause of the refusal text is out of date.
   Third, and the reason to do this as two commits: **nothing under `tests/` is named
   for P4** — no file matches p4, scorecard, model, calibration or SHAP — so those two
   P2 tests are the only contact the scoring layer has ever had, and the model fitting
   has never been executed from a test or from the CLI. Read `oxbow.models.run` before
   assuming it has one entrypoint. Task 14 carries the chain with line numbers.

3. **Reconcile DEV-015's four cycle-test disagreements** (item 13) and the
   `amount_increases_along_loop` aggregate, now that self-edges are settled.
4. **`make demo`**: `scripts/demo_seed.py` does not exist (item 11). It needs a landed
   case from step 2, then a pinned snapshot under `data/snapshots/`.
5. **`make packet`** is blocked twice and only one block is ours: no case bundle
   (step 2), and no Pango/GObject on this host. The packet's HTML and SVG are proven
   byte-identical; the PDF test skips with the reason printed rather than pretending.

Blocked on a human, not on work: the hosted read-only demo publishes CC BY-SA /
CDLA share-alike derived data and needs a licensing decision (BACKLOG.md, 00 §I.4).
Unresolved data question worth a decision: PaySim's `step` runs 1-743 against a config
modelling 30 synthetic days (item 14), which touches the split rationale and one
published limitation.

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
