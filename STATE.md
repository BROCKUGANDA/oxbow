# STATE

Living index of what is true in this repository right now. Written from observed
output, never from intent. `DECISIONS.md` holds the reasoning; this file holds
the position.

## Phase position

| Phase | Scope | State | Proof |
| --- | --- | --- | --- |
| **P0** | toolchain, tree, config, CLI verbs, design tokens, 12 glyphs, CI | **DONE** | `9582638`, `tests/unit/test_p0_*.py` |
| **P1a** | acquire PaySim, measure the graph thesis, log DEV-011 | **DONE** | `7a04bc0`, `afdb7a8`, `data/graph_measurement.json` |
| **P1b** | canonical v1, PaySim + IBM-AML adapters, quarantine, Parquet/DuckDB writers, 14 named tests, dataset card | in progress | — |
| **P3a** | time-stamped directed multigraph, rails, Leiden, cycles, subgraph cap | in progress | — |
| **P2** | 60–75 features, feature-spec hash, leakage gate proven to bite, purged splits | in progress | — |
| **P3b** | golden fixture, rules R1–R12 | in progress (fixture) | — |
| **P4** | WOE scorecard, LightGBM, Isolation Forest, calibration, fusion, SHAP | not started | — |
| **P5** | EV allocation, greedy vs CP-SAT, Monte Carlo exposure, economics | not started | — |
| **P6** | walk-forward backtest, ablation table, fairness, perturbation | not started | — |
| **P7** | FastAPI, SSE, RQ, ports/adapters, outbox, audit chain, OIDC | in progress | — |
| **P8** | seven screens, state craft, `/dev/states`, zero-CLS | not started | — |
| **P9** | packet, generated docs, demo snapshot, limitations | not started | — |

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

1. **The IBM contract is fabricated.** `contracts/raw_ibm_aml.py` declares 14 columns
   (`stepFrom, stepTo, Type, Category, Amount, nameOrig, balanceOrig, nameDest,
   balanceDest, isLaundering, isFlood, isDateSpam, isForcedCashout, unlabeled`) that
   belong to a different IBM artefact. The file on disk has 11 columns, two of them
   named `Account`, and the only label column is `Is Laundering` (full header in
   DEV-013). Its 56 tests pass against the invention. Typology must come from
   `data/processed/ibm_typologies.parquet`, not from label columns.
2. **CLI not rewired.** All four verbs still print `NOT IMPLEMENTED in P0` and exit 2,
   so `make ingest|graph|score|backtest` cannot pass their gates even where the
   packages are built.
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
12. **Repo-wide `make lint` is red: 208 ruff errors** concentrated in the packages
    live agents are still writing, plus `make verify`'s P1a IBM gate. Architecture
    contracts are green (import-linter: 4 kept, 0 broken over 197 files / 925
    dependencies). Whole-suite status as measured: **405 passed, 6 failed**, all six
    inside `tests/contracts/test_p1b_canonical_ingest.py` — the file the P1b agent is
    editing right now, so that is in-flight work rather than decay.
10. **Audit digest is delimiter-ambiguous.** `audit/chain.py::compute_row_hash`
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

## Verification ledger (run by the orchestrator, not reported by agents)

§16 requires a gate to be run in-session with observed output, so this is the list of
what has been independently executed here, with the number that came back. Anything
not on this list is *not* verified, regardless of what a package's own tests claim.

| What | Command | Result |
| --- | --- | --- |
| Completed-phase gates as claimed | `uv run python scripts/verify.py` | 4 PASS / 1 FAIL — PaySim SHA-256 verified; the failure was IBM's declared file absent (since resolved) |
| Graph layer | `uv run pytest -q tests/unit/test_p3a_graph.py tests/unit/test_p3a_cycles.py` | **61 passed** |
| Quant layer (P5) | `uv run pytest -q tests/unit -k p5` | **89 passed** (economics, allocate, exposure, frontier, monte carlo) |
| Cycle enumerator is not broken | hand-built `A→B→C→D→A` through `build_graph` | exactly **1 cycle** found, not truncated |
| Typology join alignment | `Is Laundering` share of joined CYCLE rows | **287/287 = 1.0** — ordinals line up |
| IBM network thesis | `uv run python scripts/measure_ibm_graph.py` | median degree **10.0** (> 2 passes), 647,939 edges |
| IBM cycle reality vs R4 | `uv run python scripts/measure_ibm_cycles.py` | **0 found** under §9's definition → DEV-015 |
| Tree syntax after interrupted agents | `py_compile` over every `.py` | clean |
| `any` / unbuilt-stage gate | `uv run pytest -q tests/unit/test_anti_rubbish.py` | 3 passed |
| Golden fixture self-check, no rule code | `uv run pytest -q tests/golden` | **27 passed**; two builds byte-identical (`49f6c3bd…`) |
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
