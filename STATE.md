# STATE

Living index of what is true in this repository right now. Written from observed
output, never from intent. `DECISIONS.md` holds the reasoning; this file holds
the position.

## Phase position

| Phase | Scope | State | Proof |
| --- | --- | --- | --- |
| **P0** | toolchain, tree, config, CLI verbs, design tokens, 12 glyphs, CI | **DONE** | `verify.py --phase P0` 2/2 · `9582638` |
| **P1a** | acquire PaySim, measure the graph thesis, log DEV-011 | **DONE** | `verify.py --phase P1a` 2/2 · PaySim + all three IBM members match their recorded SHA-256 against bytes on disk |
| **P1b** | canonical v1, PaySim + IBM-AML adapters, quarantine, Parquet/DuckDB writers, dataset card | in progress — 1 of 2 gates failing | `verify.py --phase P1b`: the ingest gate passes (200,000 canonical events, 0 quarantined, 0 silently coerced; the same holds for an explicit `--source ibmaml` run), and 26 of 27 contract tests pass — the failure is `test_counterparty_reuse_reported`, i.e. punch-list item 2a, not the ingest layer |
| **P3a** | time-stamped directed multigraph, rails, Leiden, cycles, subgraph cap | **DONE, one amendment open** | `verify.py --phase P3a` 1/1 (61 tests) · DEV-018 needs the graph to exclude self-edges it can now receive |
| **P2** | 60–75 features, feature-spec hash, leakage gate proven to bite, purged splits | in progress | 5 of 6 known failures are in `test_p2_splits.py`: expanding window not enforced, purge decorative, entity-disjoint share off target |
| **P3b** | golden fixture, rules R1–R12 | in progress | `test_p3b_rules.py` + `test_p3b_golden_matrix.py`: **82 passed**; 4 remain in `test_p3b_r4_labelled_cycles.py` — see punch-list item 13, one of which is a mis-specified expectation rather than a measurement |
| **P4** | WOE scorecard, LightGBM, Isolation Forest, calibration, fusion, SHAP | code present, **not verified by the orchestrator** | agent-reported full-stack run at 620 s/fold with an `import mlflow` MemoryError; nothing audited here |
| **P5** | EV allocation, greedy vs CP-SAT, Monte Carlo exposure, economics | **DONE** | `verify.py --phase P5` 1/1 (89 tests) |
| **P6** | walk-forward backtest, ablation table, fairness, perturbation | in progress | `data/processed/eval.json` exists; its numbers have not been reproduced from a command here |
| **P7** | FastAPI, SSE, RQ, ports/adapters, outbox, audit chain, OIDC | code present, **never executed** | ~20 modules in `apps/api/`, one integration test file, no route has been served |
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
13. **Repo-wide `make lint` is red: 208 ruff errors** concentrated in the packages
    live agents are still writing, plus `make verify`'s P1a IBM gate. Architecture
    contracts are green (import-linter: 4 kept, 0 broken over 197 files / 925
    dependencies). Whole-suite status as measured: **405 passed, 6 failed**, all six
    inside `tests/contracts/test_p1b_canonical_ingest.py` — the file the P1b agent is
    editing right now, so that is in-flight work rather than decay.
14. **Audit digest is delimiter-ambiguous.** `audit/chain.py::compute_row_hash`
    concatenates `HASH_VERSION | seq | occurred_at | actor_id | subject | action |
    canonical_json(payload) | prev_hash` with `"|"`. The payload's canonical JSON can
    contain `"|"` inside string values, and so can `subject`/`action`, so two different
    field splits can produce byte-identical hash material. It is not a practical
    forgery (seq and prev-hash anchor the chain), but it weakens the exact property
    plan §15 claims — tamper evidence with a named sequence number. Fix: digest a
    canonical JSON **array** of the fields, where escaping removes the ambiguity, and
    keep `HASH_VERSION` as the first element so old chains stay verifiable.
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

12. **A full-suite number taken while workers are editing is not a verdict.** The one
    snapshot captured concurrently reported 114 failed / 538 passed / 34 errors,
    including every test in `test_p9_packet.py`; the same file run alone reports 28
    passed, 1 skipped. `make test` has to be read on a quiet tree, and the green claim
    in §16 belongs to that run only.

13. **`test_p3b_r4_labelled_cycles.py` has four failures, and one of them is a
    mis-specified expectation, not a measurement.** The file expects a refusal-reason
    aggregate key `amount_increases_along_loop = 45`, but the live anatomy dict reports
    five keys (cross-currency 38, length-above 20, length-below 14, timestamps 5,
    retention 25) which match DEV-015's recorded table exactly. `amount_increases_along_loop`
    is not an aggregate reason code at all -- in `rules/network.py:127` it is a
    per-hit evidence field read off one cycle anchor (`anchor.non_increasing_breaches`),
    and 45 appears in no artifact in the repo. So either the rules layer needs a real
    non-increasing *reason* aggregate that nobody has measured, or the test invented a
    number. Unresolved deliberately: the self-edge amendment (DEV-018) changes which
    anchors the enumerator sees, so any figure taken now would move again. Re-read this
    item once `graph/build.py` settles.

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
