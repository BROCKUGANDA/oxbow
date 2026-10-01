# OXBOW — Devpost submission copy

Global Innovation Build Challenge V2 · **Track 02 — Applied (Medical Technology & Finance)**
Deadline 1 October 2026. Six required components. This file is the source for the ones that
are text; the ones that need a human are marked and left blank rather than filled in.

Judging is on this page, against four criteria. Each one is answered as its own section, in
§01, with a claim a judge can check and the command that produces it. Every figure below is
read out of an artifact on disk, not written from intent; the `provenance:` field in each
card names which.

---

## 01 · Project description

> **OXBOW is a risk-scoring and financial-crime system that is evaluated like a trading
> strategy. Every alert has a price, every policy has a backtest, and every number has an
> audit path.**

A transaction-monitoring desk flags payments one at a time, ranks them by a score nobody can
argue with, and hands the list to a team with a fixed number of analyst-minutes. Two things go
wrong at that handover. The ranking is a single model, so a false positive costs an analyst an
hour and a challenged decision has no defence. And the queue is treated as infinite: the
system reports what it *found*, never what the desk could *afford to look at*, so "we chose not
to review these accounts, worth X" is nowhere on record.

OXBOW is built from public, historical, de-identified mobile-money data and answers the only
question a risk manager actually asks: **if my team can review 200 accounts this week, which
200, what does that save, and what am I leaving on the table?**

### 1A · Innovation & Impact

**The novelty is not the classifier. It is that the review queue is treated as a portfolio.**

Three things ship here that we did not find combined in a published prototype:

1. **Two models that disagree in public.** A weight-of-evidence scorecard whose whole-number
   points sum exactly to the score — so a tribunal, a regulator or a defendant can add the
   column up by hand — runs beside a gradient-boosted model with per-account SHAP. Both are
   shown, both are reported, and the accounts where they diverge most are their own work
   queue, because that is where model risk actually lives.
2. **A graph layer that respects time.** Twelve typology rules run over a directed,
   timestamped multigraph where a cycle only counts if it is *time-respecting* — every hop
   moves value forward and the loop closes inside the window — and *value-retaining*. A
   two-hop round trip from an account back to itself is excluded on purpose: that is
   reinvestment, not a network.
3. **An expected-value allocation under a hard capacity budget.** Every alert is priced,
   `EV_i = p·E·r − c − (1−p)·f`, and the queue is allocated to analyst-minutes by greedy
   density order *and* by exact CP-SAT, with the optimality gap reported in money.

**Measurable impact, on one real scored corpus, under one stated assumption set.**
Five walk-forward folds over 79,998 scored account rows (base rate 0.135 %), each with a
12,000-analyst-minute budget, priced from `config/economics.yaml` — which is illustrative and
says so:

| policy at the same capacity | accounts reviewed | analyst-minutes used | true / false positives | net benefit |
| --- | --- | --- | --- | --- |
| score threshold — what most desks actually do | 2,625 | 59,993 of 60,000 | 31 / 2,594 | **−UGX 61,991,618** |
| expected value, greedy = exact CP-SAT | 1 | 120 | 1 / 0 | **+UGX 2,586,702** |

**Read this table with its caveat.** It is computed from the run's measured
per-account exposure (, , 40,001 accounts non-zero).
The warehouse table that feeds the live queue currently reconstructs exposure from activity
features instead and gets zero for every account, so the shipped queue contradicts the figure
above until DEV-032 is applied. The measurement is right; the plumbing is not.

At one capacity and one assumption file, pricing the queue rather than thresholding it is
worth **UGX 64.58 million** across five folds — and the mechanism is the surprising part: the
optimal policy spends **1 of the 200 available analyst-hours**, because at a 0.135 % base rate
almost no alert clears its own review and friction cost. "Review less, deliberately, and say
what you gave up" is the result, and the capacity cut line in the UI is that result made
visible.

**The same table contains the finding against us, printed rather than removed.** Expected
shortfall at 97.5 % on the exposure *not* reviewed is UGX 16.03 M under the EV policy against
UGX 8.52 M under the threshold policy: the policy that wins on the mean fattens the tail by
UGX 7.50 M, because leaving the queue unread leaves value unexamined. A risk-adjusted benefit
ratio and a tail comparison exist in this build precisely so that we cannot hide that, and a
policy that beats another on average while losing on the tail is a policy a desk has to
choose knowingly.

**Impact is bounded by an assumption, and we say which one.** Every one of these figures also inherits a second
honesty we found this week and have not papered over: the exposure column the run actually
measures is a **30-day** outflow, while `config/economics.yaml` declares a 24-hour recovery
window. Capping the measured column at 24 hours leaves one non-zero row of 43,046, because
PaySim accounts are one-directional inside any short window, so we price the measurement and
print `exposure_source` and the basis sentence on every row rather than the cap the definition
assumed. `DECISIONS.md` DEV-034 records the discrepancy and the fact that the economics card
still describes only one of the two clocks.

** Every currency figure above is a
function of a 0.35 recovery rate swept over 0.20 / 0.35 / 0.50, a 24-hour recovery window,
UGX 150 per analyst-minute and UGX 25,000 of friction cost per wrongly-touched legitimate
customer. No money figure appears anywhere in this product — UI, packet, report or slide —
without that band printed beside it.

### 1B · Technical Feasibility

It runs, from one command, and what it produces is queryable.

- **Ingest.** 6,362,620 PaySim canonical events across 64 batches, **0 quarantined and 0
  silently coerced**, over a 743-day window (`2014-01-02 → 2016-01-14`), verified against a
  recorded SHA-256. IBM-AML ingests through its own adapter: 5,078,345 transaction rows,
  515,080 accounts, 647,939 directed edges excluding self-loops.
- **Modelling.** 75 published features on a 79,998-row account-grain corpus with a recorded
  feature-spec hash; five folds each fitted and scored end to end; SHAP persisted per scored
  row so the UI never computes on request.
- **Quant layer.** EV allocation in integer minor units, greedy against exact CP-SAT with a
  measured optimality gap, Monte-Carlo downstream exposure at 50,000 seeded draws per fold,
  VaR95 / ES97.5 on unreviewed exposure, a capacity sweep and a policy frontier.
- **Serving.** FastAPI over Postgres behind eight **ports with working null adapters** — the
  same read model serves a real warehouse and a disk-only one, so the tool can be
  demonstrated with no external system at all. **The calibration fix works — `score_rows()` shapes
  43,046 of that run's 43,720 out-of-sample rows, every one labelled `uncalibrated` with its fold's
  refusal reason — and we are publishing the harder fact beside it: the warehouse stage that writes
  those rows crashed before its first `INSERT` for every run, on a rebound local that turned a
  slice frame into a column list, so the queue was empty for two independent reasons and only one
  of them was about calibration.** That was found by running the stage, not by reading the loader,
  and it is fixed in `63cedca`. An empty queue is the failure mode this product exists to avoid, so
  an empty queue caused by our own crash is not a footnote. Every outbound payload carries its own
  assumptions, model version and disclaimer, so a consumer cannot receive an OXBOW number
  without receiving what it depends on. Errors are RFC 9457 `problem+json` with a `run_id`.
- **Interface.** Seven product screens plus a state gallery, on a design system with 12
  hand-drawn typology glyphs on a 24 px grid at 1.75 px stroke — cycle, fan-in, fan-out,
  pass-through, structuring, dormant-wake, hash-link, embargo and four more. These encode
  typologies no icon library has. Measured, not asserted: axe 0 critical on all 10 sampled
  routes, and layout-shift **0.000000** on both the alert queue and the case workspace.
- **Integrity.** Money is `int64` minor units end to end and is divided only at render time; a
  custom AST gate walks 161 files and fails the build if a float ever holds a currency amount
  (measured: `no-float-money: OK (161 files scanned)`).
  Every analyst decision writes three rows in one transaction: the decision, a link in a
  SHA-256 hash chain, and an outbox row. `make verify-audit` walks the chain and names the
  sequence number of the first broken link; a case packet re-walks it on export and refuses to
  render if it does not verify. Reversals are new rows, never edits.
- **Reproducibility.** Seed 1337 propagated to NumPy, LightGBM, Optuna, the Monte-Carlo engine
  and the typology injector; a total order of `(event_ts_utc, txn_id)`; content-hashed
  artifacts; `make verify-determinism` runs a stage twice and diffs the digests — measured at
  **67 artifacts byte-identical** across two IBM ingests.

What is *not* claimed: no live transaction is processed, no payment rail is connected, nothing
trades, nothing screens against a sanctions list to make a decision, and no LLM sits anywhere
in a scoring, ranking or decision path. Non-goals are refusals here, not preferences.

### 1C · Rigor & Validation

This is the section most hackathon ML projects skip, so it is where the design work went.

- **The split is temporal, purged and embargoed.** Five expanding walk-forward windows with a
  30-day embargo that is *asserted equal to the longest feature lookback in
  `config/features.yaml`* — the two numbers disagreeing is a build failure, not a config
  drift. Random shuffling is forbidden by the schema. The fold plan agrees with the corpus's
  own fold column on **79,998 of 79,998 rows** (`agreement_share: 1.0`).
- **The leakage gate is proven to bite, in both directions.** A deliberately leaking feature
  added to a test fixture is caught. And the published backtest carries a control arm that
  reads the label ahead of time: it scores **PR-AUC 1.0 against the best honest arm's 0.0591**,
  which is the harness proving it can see cheating — not a model proving it is good.
- **A headline metric chosen for the actual problem.** At a 0.135 % positive rate AUROC
  inflates, so PR-AUC leads and AUROC is reported only for comparability, explicitly
  de-emphasised. Best honest arm 0.0591 is 44× the base rate; alerts per 10k accounts,
  precision at budget and Brier score accompany it.
- **Seed stability is reported as a distribution, not as a lucky run.** PR-AUC over five seeds:
  **mean 0.0347, sd 0.0213** — a spread wide enough to matter, published because the alternative
  is quoting 0.0591 as if it were stable.
- **Calibration was refused, and we honoured the refusal instead of the metric.** Isotonic
  calibration has a floor of 50 validation positives; this slice's folds carried 14 and 6.
  Rather than lower the floor to get a reliability curve, the probabilities ship labelled
  **uncalibrated**, the queue prints the fold's own refusal reason, and every money figure
  derived from them inherits the label. A miscalibrated probability multiplied by money is a
  wrong currency figure, so this is not a caveat — it is the load-bearing limitation.
- **Selection and testing are separated and timestamped.** Nine configurations evaluated;
  selection on validation; `test_fold_touched_once: true` with a recorded instant; the
  multiple-testing bias stated in the model card.
- **Fairness and robustness checked even though no protected attribute exists** in either
  corpus: false-positive rate by amount decile (6.43 % in the smallest-value bucket against
  4.79 % mid-range — over-flagging small-value accounts is the realistic harm and it is named
  as such), plus two perturbation controls: ±10 % amount shift leaves score rank correlation at
  Spearman 0.99998, and a 10 % edge drop is reported with its own recall stability.
- **Two corpora, reported separately, never averaged** into one headline number.
- **A per-rule hit-rate report runs every time, and we publish the answer it gave us.** Each of
  R1–R12 is checked against a floor and a one-third ceiling, because a rule that fires on
  nothing is decoration and a rule that fires on everyone is a constant. On the scored PaySim
  corpus, **eleven of the twelve typology rules fire on zero of 76,851 accounts** and are
  excluded from scoring with that status recorded: `RAPID_PASS_THROUGH`, `FAN_IN`, `FAN_OUT`,
  `CYCLE_MEMBER`, `STRUCTURING`, `VELOCITY_SPIKE`, `DORMANT_REACTIVATION`, `ODD_HOUR_SHIFT`,
  `FAST_CASH_OUT`, `NEW_COUNTERPARTY_SURGE` and `CHAIN_MEMBER` are all `below_floor`, and only
  `AMOUNT_REGIME_SHIFT` fires (170 accounts). That is not a surprise, it is the consequence of
  the measurement above — PaySim is star-shaped, so a network rule has no network to fire on —
  and the gate designed to shout about it did shout. The graph corpus where those typologies
  are labelled is IBM-AML, and the named gap in this build is that the *scored* run is the one
  corpus that cannot exercise the network layer. We are reporting that rather than shipping a
  "12 typology rules" bullet with the hit counts left out.
- **The ablation's own honesty is disclosed in the artifact.** The eight honest rows currently
  share one fitted stack, so they separate the *allocation policy* — which is where the result
  above lives — rather than the model class. That caveat is printed under the published table,
  and the per-row channel scorer that fixes it landed on 2026-09-28 with the re-run still owed.
  We would rather ship the disclosure than a table whose labels claim more than they measure.
- **Twelve named limitations**, each with the measurement that shows it, written in the first
  person, in `LIMITATIONS.md`.

### 1D · Presentation

- A **four-minute demo path** that a judge can walk unaided and that every screen deep-links:
  currency KPI → alert queue with the capacity cut line → case workspace → network explorer →
  a written, hash-chained decision → the exported packet → the policy simulator, ending on the
  limitations and the named assumption. It is scripted beat by beat in §03 below and the
  narration is generated from that same table, so the voice cannot drift from the words a
  judge reads.
- **Eight screenshots** of product screens, produced by a script rather than a camera, at
  2× / 1440×900.
- A **`/dev/states` gallery** holding every loading, empty and error state in the product on
  one page, screenshot-tested and swept for accessibility. When a judge asks to see it fail,
  the answer is a route.
- **Attribution and disclaimers are structural, not decorative**: the research-prototype
  disclaimer is asserted by a test to appear in the README, in the footer of every route and on
  page one of every packet; the dataset card carries licence, share-alike obligation, citation,
  retrieval date and per-file SHA-256; and every currency figure prints the config keys it is a
  function of.
- **Risk is never encoded by colour alone** — every band renders as its letter plus a
  five-segment meter glyph, so the encoding survives a greyscale print of the packet and a
  washed-out projector.

### What the data actually is, and the measurement that changed the architecture

We pre-committed a pass condition for the network thesis before writing any graph code:
median counterparty degree > 2. PaySim — the standard, widely-cited fraud-detection simulator —
has **median degree 1**, a sender-reuse ratio of 0.0015, and produced **zero** surviving
time-respecting cycles in a 20,000-row sample. The recorded verdict is
`STAR_SHAPED_TRIGGER_DAY4_FALLBACK`.

Rather than quietly switching datasets, the pipeline kept its shape and split its assignment:
tabular risk trains on PaySim, network detection runs on IBM-AML, which does have a graph
(median degree 10 excluding self-loops, 173,735 accounts above two counterparties, 0.1019 %
laundering-labelled), and which also ships **3,209 labelled transactions across 370 laundering
attempts in 8 typologies** — so per-typology recall is measured against real annotations rather
than asserted.

PaySim's `isFraud` covers one narrow behaviour (an agent takes over an account and drains it)
and `isFlaggedFraud` fires 16 times in 6.36 M rows, which is not a usable target. Metrics are
reported per corpus and never averaged.

**What we refuse to do.** Elliptic is CC BY-NC-ND, so it is cited as related work and never
acquired, never ingested, never behind a feature or a figure — enforced by `ingest_allowed:
false` in config, not by discipline. IEEE-CIS is unused. A source that is not declared in
`config/sources.yaml` is refused by ingest.

### Honest status

The pipeline runs end to end and has produced a real scored corpus and a real five-fold
backtest, both at `provenance: real_corpus`, and the generated cards publish those numbers.
Two things the table's labels do not yet measure are disclosed beside it: the honest ablation
rows share one fitted stack, so they separate policy rather than model class; and no fold
cleared the calibration floor, so the probabilities are labelled uncalibrated rather than being
presented as measured. The harness was verified first against hand-computed ground truth and
still carries that labelled demonstration path. We would rather ship cards that say so than
cards that do not.

---

## 02 · Source code

Public repository: **`https://github.com/BROCKUGANDA/oxbow`**

The repository is MIT-licensed for code; the datasets keep their own terms (PaySim CC BY-SA 4.0
and IBM-AML CDLA-Sharing-1.0, both share-alike on derived data; Elliptic CC BY-NC-ND, cite-only)
and are not redistributed — `make data` fetches them and verifies each against a recorded
SHA-256.

No CI workflows are included, deliberately. Measured rather than asserted: `git ls-files`
returns nothing under `.github/` (an empty local `.github/workflows/` directory exists and is in
no commit — git does not track empty directories, so a clone has no CI either), and
`tests/unit/test_publication_preflight.py` holds the rest of the publication hygiene: licence
consistency across `pyproject.toml` and `package.json`, no developer home path in shipped code,
no secret-shaped literal, the `RUN_SALT` value absent from every tracked file, `.env` untracked,
no corpus bytes committed, and every `license:` in `config/sources.yaml` named in `LICENSE`.

---

## 03 · Demo video — script and narration

Target length **3:25** (limit 2–5 min). Capture at 1440×900, device scale 2, from the live app
serving landed evidence; narration is a neural text-to-speech read at its own pace (see
`SUBMISSION-CHECKLIST.md` for the exact commands), and the slots below are the clock the video
is cut to — `scripts/make_narration.py` measures every beat's real audio length against them.

The cut is the deployment's own honesty, in order: the two screens that serve a landed run, the
screen that reports an absent graph, and then the three that refuse. The refusals are beats, not
cropped out.

| # | Beat | On screen | Narration (read as written) |
|---|---|---|---|
| 1 | 0:00–0:55 | Queue + capacity line | "OXBOW scores mobile money accounts for financial crime, and then prices the review queue against the analyst time that actually exists. This is a live deployment over a landed run. Forty three thousand accounts scored, half a million rule hits, and the dashed line across the screen is the capacity cutoff. Above it, three accounts are funded at twelve thousand analyst minutes. Below it, the rest of the queue, which will not be looked at. Every card states its band, its score, its exposure and its expected value, and beside every money figure sits the list of assumptions that figure is a function of. This one says the probability is uncalibrated, because the validation fold held eighteen positives and the floor is fifty. It prints that, instead of showing you a rate it does not have." |
| 2 | 0:55–1:45 | Case workspace | "Opening the top account. The score comes apart into the rules that fired, the evidence events behind them, and the transactions themselves. A decision here is not an edit. Escalating writes the decision row, a link in a SHA two fifty six hash chain, and an outbox row, in one transaction, and this case is held for a second reviewer because the exposure crosses the four eyes threshold. The banner across the top is the deployment naming which optional components are not running, and it stays on screen while they are not. Nothing on this page is composed out of a count when the measurement is missing." |
| 3 | 1:45–2:15 | Network explorer | "The network explorer walks two hops from an account. This one has no stored edge at two hops, and the screen says so, rather than drawing one node on an empty canvas to look like a graph. That is a deliberate finding. The standard simulator in this space has a median degree of one and no time respecting cycles at all. We pre committed that test before writing any graph code, it failed, and it changed the architecture." |
| 4 | 2:15–2:30 | Command view, refusing | "The command view answers fifty three. This run stored no policy summary, so there are no money tiles to show. Every tile on that screen is a stored measurement, and none of them is composed here from counts." |
| 5 | 2:30–2:45 | Scorecard studio, refusing | "The scorecard studio refuses the same way. It will not print points without the scaling constants those points were built from, because that would leave the reader to guess the units." |
| 6 | 2:45–3:10 | Validation, refusing, and what was measured | "And validation reports that no backtest folds landed, instead of drawing empty axes that would read as the model having found nothing. What was measured is still on the record. Five expanding walk forward windows with a thirty day embargo, asserted equal to the longest feature lookback, and an expected shortfall on the unreviewed remainder that got worse by seven and a half million." |
| 7 | 3:10–3:25 | Closing on the queue | "Twelve named weaknesses in the limitations file, and every currency figure on this site is a function of one assumptions file, printed beside it. OXBOW is a research prototype over historical, de identified data." |


**Required on-screen at all times:** the research-prototype disclaimer is in the footer of
every route and on page one of every exported packet — asserted by
`test_disclaimer_present_everywhere`.

**If beat 6 draws a question, the answer is in the table:** the threshold policy lost
sixty-two million in modelled benefit across five folds at full capacity, the expected-value
policy made two and a half million by spending one of two hundred analyst hours — and the
expected shortfall on what was left unreviewed got worse by seven and a half million, which is
printed on the same page rather than cropped out of the screenshot. All three figures are in
the same currency and the same scale, and the scale is the one `config/economics.yaml` names.

---

## 04 · Built with

**Language and environment** — Python 3.12 (uv, locked), TypeScript 5.6 (Bun as the package
manager and task runner, `bun.lock` committed; Node serves the build), Node 20+.

**Data and pipeline** — Polars 1.32 · DuckDB 1.1 · Pandera 0.20 (contracts) · PyArrow 18 ·
NumPy 1.26 + Numba · NetworkX 3.3, python-igraph and leidenalg (communities) · WeasyPrint
(packet rendering).

**Modelling** — optbinning 0.19 (WOE binning, MIP solver via OR-Tools) · scikit-learn 1.5 ·
LightGBM 4.5 · Optuna 4.3 (hyperparameters) · SHAP 0.46 (explanations) · MLflow 2.19
(experiment tracking) · OR-Tools CP-SAT 9.11 (capacity allocation).

**Backend** — FastAPI 0.115 · Pydantic v2 · SQLAlchemy 2 + Alembic · PostgreSQL 16 ·
Redis 7 with RQ (job queue) · HMAC-SHA256 signed webhooks with an outbox and dead-letter
path · RFC 9457 problem+json · OpenAPI.

**Frontend** — Next.js 15 (App Router, React 19) · TanStack Query v5, Table v8, Virtual ·
Tailwind CSS v4 · Motion · Cytoscape.js with the fcose layout · visx · lightweight-charts ·
custom SVG sprite (no icon library).

**Quality and reproducibility** — pytest (unit + integration) · Playwright (browser suite,
measured CLS, axe accessibility, real `prefers-reduced-motion` emulation) · Vitest ·
Biome · ruff · mypy · import-linter (architecture contracts) · a custom AST gate that fails
the build on float money · pre-commit hooks · Docker Compose with every image pinned by
digest · a phase-gate runner (`make verify`) that executes each claimed phase as a command.

**Datasets** — PaySim (synthetic mobile-money simulator, CC BY-SA 4.0) · IBM Transactions
for Anti-Money Laundering (CDLA-Sharing-1.0). Elliptic (CC BY-NC-ND) is cited only and never
ingested; IEEE-CIS is unused.

---

## 05 · Team information

> Judges require **real full names**. Not filled in here deliberately: inventing or copying
> a name into a submission is the one thing in this file that cannot be verified by a test.

- Name: `<FULL NAME>`
- Role: sole builder (solo entry — teams of 1–6 are permitted)
- Country / timezone: `<COUNTRY>` · Africa/Kampala (UTC+3) is the deployment timezone used
  throughout the product.

**Eligibility to confirm before submitting** (from the official rules): ages 13+,
**students only**, and "companies / professional organizations excluded". If the entry is
not a student entry, Track 02 eligibility needs checking with the organisers first — late
entries are rejected outright, so this is worth settling before the deadline.

---

## 06 · Screenshots

Eight interface captures, produced by `node apps/web/scripts/capture-screens.mjs` into
`docs/screens/` (2× device scale, 1440×900):

| File | Route | Source | What it shows |
|---|---|---|---|
| `01-dashboard.png` | Command | **live run** | the refusal, and its reason: this run stored no policy summary, so there are no money tiles to compose from counts |
| `02-alerts-queue.png` | Queue | **live run** | 43,046 scored accounts, three funded at 12,000 analyst-minutes, the capacity cutoff drawn at rank 3, and every money figure with its assumption lines beside it |
| `03-network-explorer.png` | Network | **live run** | an account with no stored edge at two hops, stated as an empty state with the window it was measured over and controls to widen it |
| `04-scorecard.png` | Scorecard | fixture build | WOE points per feature, the disagreement queue, PSI drift |
| `05-case-workspace.png` | Case | **live run** | score 0.554 held for four-eyes at 7.4m UGX, its reason codes, its evidence, and one hash-chained decision already written |
| `06-policy-frontier.png` | Policy | fixture build | threshold-versus-EV frontier in money, greedy against CP-SAT |
| `07-model-validation.png` | Validation | **live run** | the refusal, and its reason: no backtest folds landed, which is not the same claim as empty axes |
| `08-state-gallery.png` | `/dev/states` | fixture build | every state the app can be in: 5 empty states, 4 error tiers, matched-geometry skeletons, degraded banners |

**Status of the set.** Five images are the live deployment serving the landed run
(`01M3H8WG436R394NZT2GS1KG69`), and two of those five show a pane refusing — deliberately. The
other three are the bundled fixture, labelled as such in the table, because the scorecard,
policy and state-gallery screens have no landed evidence to render yet: `scorecard_spec` has
no producer that writes it, `policy`/`policy_summary` are not declared in
`oxbow.ports.warehouse.WAREHOUSE_TABLES`, and the walk-forward folds for this run were never
completed. Each of those routes answers 503 naming the missing artifact on the live
deployment, which is what `01` and `07` photograph.

The degraded banners visible across the live captures are the deployment listing the optional
components that are not running here (redis, mlflow, the CP-SAT solver, the narrative
summariser, OIDC). They are printed on every pane because the response says so; the product
does not hide a partial deployment behind a clean-looking screen.

