# OXBOW — Devpost submission copy

Global Innovation Build Challenge V2 · **Track 02 — Applied (Medical Technology & Finance)**
Deadline 1 October 2026. Six required components. This file is the source for the ones that
are text; the ones that need a human are marked and left blank rather than filled in.

---

## 01 · Project description

**What it is.** OXBOW is a directed financial-crime analytics tool for mobile-money
networks: it scores accounts, explains every score twice, and then prices the review queue
against the analyst capacity that actually exists.

**The problem it solves.** AML tooling in production ranks alerts by a risk score and then
hands the list to a team with a fixed number of analyst-minutes per period. Two things go
wrong. The ranking is usually a single model nobody can argue with, so a false positive
costs an analyst an hour and a challenged decision has no defence. And the queue is treated
as if it were infinite: the system reports what it *found*, never what the team could
*afford to look at*, so "we chose not to review these 1,212 accounts, worth X" is nowhere on
record.

OXBOW's answer is three parts:

1. **Two models that disagree in public.** A WOE scorecard whose points a human can
   recalculate and contest, and a gradient-boosted model with per-account SHAP. Both are
   shown side by side, with the disagreement itself as a work queue. A scorecard is not a
   compromise on accuracy — it is the artifact a tribunal, a regulator or a defendant can
   actually interrogate.
2. **A graph layer that respects time.** Twelve typology rules run over a directed
   multigraph where a cycle only counts if it is *time-respecting* — each hop must move
   value forward and return to its origin inside the window. A two-hop round trip between
   one account and itself is excluded on purpose: that is reinvestment, not a network.
3. **Economics under a capacity constraint.** Every alert is priced by expected value
   (recovered loss × probability, less review cost and friction), the queue is allocated
   greedily and again by CP-SAT optimisation, and the difference between the two is shown in
   money. The cut line is drawn explicitly, with the value sitting below it named.

**How it works under the hood.** Polars and DuckDB over Parquet; a canonical event schema
(21 columns, 19 persisted) validated with Pandera, with anything that fails contract sent to
a quarantine file rather than coerced. Money is `int64` minor units end to end and is only
divided at render time — an AST gate (`scripts/no_float_money.py`, 158 files) fails the
build if a float ever holds a currency amount. Features are computed per fold from data at
that fold's cut, never once globally. Splits are five expanding walk-forward windows with a
30-day embargo that is *asserted equal to the longest feature lookback*, purged on the
outcome window, and shuffling is forbidden by the schema.

Everything is reproducible from a seed and a salt: seed 1337, a total order of
`(event_ts_utc, txn_id)`, content-hashed artifacts, and `make verify-determinism` which runs
the pipeline twice and diffs the digests.

The API is ports-and-adapters: the same read model serves Postgres and a read-only
"null-file" warehouse, so the tool can be demonstrated with no database at all. Each analyst
decision writes three rows in one transaction — the decision, a link in a SHA-256 hash
chain, and an outbox row carrying the case bundle to a webhook. `make verify-audit` walks
the chain and names the sequence number of the first broken link; a case packet re-walks it
on export and refuses to render if it does not verify. Reversals are new rows, never edits.

**The measurement that changed the architecture.** We pre-committed a pass condition for
the graph thesis before writing any graph code: median counterparty degree > 2. PaySim —
the standard, widely-cited fraud-detection simulator — has **median degree 1** and produced
**zero** surviving time-respecting cycles in a 20,000-row sample. The recorded verdict is
`STAR_SHAPED_TRIGGER_DAY4_FALLBACK`. Rather than quietly switching datasets, the pipeline
kept its shape and split its assignment: tabular risk trains on PaySim, network detection
runs on IBM-AML, which does have a graph (median degree 10 excluding self-loops, 173,735
accounts above two counterparties, 0.1019 % laundering-labelled over 5,078,345 rows). PaySim
is 6,362,620 canonical events over a 743-day window, ingested with 0 quarantined rows and 0
silently coerced.

**What we refuse to do.** Elliptic is CC BY-NC-ND, so it is cited as related work and never
acquired, never ingested, never behind a feature or a figure — enforced by
`ingest_allowed: false` in config, not by discipline. IEEE-CIS is unused. Every currency
figure carries the assumptions it derives from, printed beside it, naming the keys in
`config/economics.yaml`; a money figure without its assumption block is a rejection trigger
in code review and a hard refusal in the renderer. Twelve named weaknesses are in
LIMITATIONS.md with the measurement that shows each one.

**Honest status.** The pipeline runs end to end and has produced a real scored corpus
(79,998 account rows × 75 features, 108 positives). The headline model and economics cards
now carry `provenance: real_corpus`: 9 variants from the walk-forward over the landed PaySim
40k slice, with the fold plan agreeing with the corpus's own fold column on all 79,998 rows,
and the deliberately leaking control row scoring PR-AUC 1.0 against the best honest arm's
0.0591 — which is the harness proving it can see leakage, not a model proving it is good.
Two things are not what the table's labels imply, and the cards say so beside it: every honest
row runs the same fitted stack, so the ablation separates the allocation policy rather than
the model class, and every fold refuses to calibrate because the slice holds fewer validation
positives than `config/model.yaml`'s floor of 50 requires (DEV-024; a 500,000-event slice is
running to answer that). The harness itself was verified first against
hand-computed ground truth, and it still carries that labelled demonstration path. We
would rather ship a card that says so than one that does not.

---

## 02 · Source code

Public repository: **`<PASTE URL — see SUBMISSION-CHECKLIST.md, this needs `gh auth login`>`**

No CI workflows are included, by request. The repository is MIT-licensed for code; the
datasets keep their own terms (PaySim CC BY-SA 4.0, IBM-AML CDLA-Sharing-1.0, both
share-alike on derived data; Elliptic CC BY-NC-ND, cite-only) and are not redistributed —
`make data` fetches them and verifies each against a recorded SHA-256.

Measured rather than asserted: `git ls-files` returns nothing under `.github/` (an empty local
`.github/workflows/` directory exists and is in no commit — git does not track empty directories,
so a clone has no CI either), and `tests/unit/test_publication_preflight.py` holds the rest of it:
license consistency across `pyproject.toml` and `package.json`, no developer home path in shipped
code, no secret-shaped literal, the `RUN_SALT` value absent from every tracked file, `.env`
untracked, no corpus bytes committed, and every `license:` in `config/sources.yaml` named in
`LICENSE`.

---

## 03 · Demo video — script and narration

Target length **3:30** (limit 2–5 min). Capture at 1440×900, device scale 2, from the
fixture/live app; narration is generated with the local TTS voice at a slightly slowed rate
(see `SUBMISSION-CHECKLIST.md` for the exact commands).

| # | Beat | On screen | Narration (read as written) |
|---|---|---|---|
| 1 | 0:00–0:20 | Command strip | "OXBOW is a financial-crime analytics tool for mobile money networks. It scores accounts, it explains every score twice, and it prices the review queue against the analyst capacity that actually exists." |
| 2 | 0:20–0:50 | Scorecard route | "Two models run side by side. On the left, a weight-of-evidence scorecard: whole-number points per feature, and the points sum exactly to the score, so a human can recalculate it. On the right, a gradient boosted model with per-account SHAP. Where the two disagree is its own work queue." |
| 3 | 0:50–1:25 | Network explorer | "The graph layer only counts a cycle if it respects time. Each hop has to move value forward, and the loop has to close inside the window. A two-hop round trip from an account back to itself is excluded on purpose — that is reinvestment, not a network." |
| 4 | 1:25–2:00 | Queue + capacity line | "This dashed line is the capacity cutoff, and it is the part most tooling leaves out. Above it, what the team can review this period at the configured analyst minutes. Below it, the alerts that will not be looked at, and the money that consciously goes unexamined. Choosing not to look is on the record rather than implied by a scrollbar." |
| 5 | 2:00–2:35 | Case workspace | "Opening a case: the score, the evidence, the counterparty subgraph, and the decision. Each decision writes three rows in one transaction — the decision itself, a link in a SHA-256 hash chain, and an outbox row that carries the bundle onward. Reversals are new rows, never edits, and the export packet re-walks the chain and refuses to render if a link is broken." |
| 6 | 2:35–3:00 | Validation / ablation | "Every number is produced by five expanding walk-forward windows with a thirty-day embargo, asserted equal to the longest feature lookback, and shuffling is forbidden by the schema. The ablation table includes a deliberately leaking control row, so the harness is proven to be capable of catching leakage." |
| 7 | 3:00–3:30 | Limitations + disclaimer | "PaySim, the standard simulator in this space, has no network at all — median degree one, and zero time-respecting cycles. We pre-committed that test before writing graph code, it failed, and it changed the architecture. Twelve named weaknesses are in the limitations file, and every currency figure on this site is a function of one assumptions file, printed beside it." |

**Required on-screen at all times:** the research-prototype disclaimer is in the footer of
every route and on page one of every exported packet — asserted by
`test_disclaimer_present_everywhere`.

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

| File | Route | What it shows |
|---|---|---|
| `01-dashboard.png` | Command | headline loss avoided with its assumption line, band distribution, latest patterns |
| `02-alerts-queue.png` | Queue | virtualised alert table, the capacity cutoff line and the value below it |
| `03-network-explorer.png` | Network | Cytoscape subgraph with cycle/flagged/velocity overlays and a keyboard route into the canvas |
| `04-scorecard.png` | Scorecard | WOE points per feature, the disagreement queue, PSI drift |
| `05-case-workspace.png` | Case | score header, SHAP, evidence, four-eyes decision write with the audit hash |
| `06-policy-frontier.png` | Policy | threshold-versus-EV frontier in money, greedy against CP-SAT |
| `07-model-validation.png` | Validation | walk-forward folds, ablation table, fairness and perturbation |
| `08-state-gallery.png` | `/dev/states` | every state the app can be in: 5 empty states, 4 error tiers, matched-geometry skeletons, degraded banners |

**Status of the current set:** they are honest but not yet persuasive — they were captured
against the read-only warehouse, so the header carries real provenance while the panes show
skeletons and one pane correctly says *"policy_summary does not exist in the null-file
warehouse … needs Postgres"*. Re-capture after the demo snapshot lands (component 03 depends
on the same thing). The `08-state-gallery.png` set is already submission-quality.
