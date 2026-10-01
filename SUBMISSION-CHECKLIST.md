# Submission checklist — the last day

Companion to [SUBMISSION.md](SUBMISSION.md). Every command here was run on this machine and is
quoted with what it returned, so the remaining work is execution, not discovery.
Deadline **2026-10-01**, six required components, judging on the Devpost page alone.

## What is already true, measured

| | |
|---|---|
| Repository | MIT `LICENSE` with the dataset terms; no CI workflows; `.env` untracked; `tests/unit/test_publication_preflight.py` **10 passed**; largest tracked file 508 KB (`apps/web/tests/states/.screens/full-gallery.png`). Safe to make public as it stands. |
| Remote | `github.com/BROCKUGANDA/oxbow` exists, **private**, default `main`. `gh auth` for `BROCKUGANDA` works. |
| Submission copy | `SUBMISSION.md` restructured into one section per judging criterion (Innovation & Impact, Technical Feasibility, Rigor & Validation, Presentation), each with a checkable claim and the command that produces it. |
| Decision record | `DECISIONS.md` to **DEV-033**. DEV-028 (uncalibrated scores land labelled, floor untouched), DEV-029 (uncalibrated EV priced and stamped), DEV-030 (11 of 12 typology rules fire on zero accounts), DEV-031 (exposure downstream term is a measured empty set), DEV-032 (the queue priced everyone at zero while the measured column sat in the next file), DEV-033 (the honest scorer aborts an ablation whose folds degraded, and that is correct). |
| Failure modes | `docs/FAILURE-MODES.md` plus `tests/unit/test_failure_mode_register.py`, which fails if a row cites a test that no longer exists or an open gap is missing from the findings list. Proven to bite: it failed on three unlisted rows before the list was completed. |
| Design system | 15 hand-drawn glyphs (the 12 typology marks plus 3 utility) at 1.75px on a 24px grid; OKLCH tokens with a generated `tokens.ts` mirror and a drift gate; banned-list clean (no Inter, no glassmorphism, no Lucide, no emoji); axe 0 critical on 10 routes; **CLS measured 0.000000** on `/alerts` and `/cases/[id]`. |
| Gates that pass now | `ruff check` clean over `apps/api`, `packages`, `scripts`; `lint-imports` 4 kept / 0 broken; `no-float-money` OK over 161 files; `tests/unit/test_p7_economics_landing.py`, `test_p7_warehouse_landing.py`, `test_p7_erasure_preserves_chain.py`, `test_p7_validation_served_fields.py`, `test_p9_eval_surfaces_builder_refusals.py`, `test_failure_mode_register.py` all green. |

## Blocking, and only you can do it

| | |
|---|---|
| **Team real full names** (component 05) | Left blank in `SUBMISSION.md` deliberately — inventing a name into a submission is the one thing that cannot be verified by a test. |
| **Confirm student eligibility** | Rules say *students only*, ages 13+, companies and professional organisations excluded. Late entries are rejected outright, so settle this before the deadline, not on it. |
| **YouTube unlisted link** (component 03) | Needs your account; the video file is produced locally. |
| **Push and the visibility flip** (component 02) | `git push origin main` then `gh repo edit BROCKUGANDA/oxbow --visibility public`. Nothing in this repository's own tooling pushes. Re-run the history scan first, not just the HEAD gate — a push makes every past commit readable. |

## The chain that produces the remaining two components

Screenshots (06) and video (03) both need **landed evidence in Postgres**. Nothing here has been
faked: `scripts/demo_seed.py --create` refuses until `score`, `backtest_fold` and `decision` are
all non-empty, and it was right to.

```bash
cd /c/Users/HP/Desktop/OXBOW
docker compose up -d postgres                      # the ONLY service needed; ~5 s once the engine is up
set -a && . ./.env && set +a
export DATABASE_URL="postgresql+psycopg://oxbow:${POSTGRES_PASSWORD}@127.0.0.1:5433/oxbow"
export OXBOW_WAREHOUSE=postgres
uv run alembic -c apps/api/alembic.ini upgrade head    # must read 0004_mc_interval_optional (head)
```

1. **Fresh score run** — `uv run oxbow score --max-events 40000`. A new run id is required because
   `01M3H8WG436R394NZT2GS1KG69` is frozen `state=complete` over an empty `score` table, and the
   0002 immutability trigger correctly refuses to amend or delete it. Plan §13 says rescoring
   *creates a new run*; that is the honest route, not an edit.
2. **Walk-forward on the new corpus** — `uv run oxbow backtest --corpus out/score/<run>/backtest_corpus.parquet`.
   This is what produces `fold_windows`, hence `backtest_fold` rows, hence a snapshottable demo.
   The previous attempt exited 0 having **written nothing**: folds 2 and 3 ran degraded
   (`scorecard_and_rules_only`), so `gbm_no_graph` had no number and DEV-027's guard raised
   instead of borrowing the full stack's. That refusal is kept; the fix makes an unavailable
   channel a reportable state rather than an abort (DEV-033).
3. **Land** — `uv run python scripts/land_warehouse.py --run-id <run>`. It is not a CLI verb
   because 01 §D pins the CLI to four verbs, so this script is the documented driver for the
   fifth stage `jobs.PIPELINE_STAGES` already names. Report per-table counts *and* the refusal
   text for every table that stays empty.
4. **Case, decision, audit** — start the API and open a real case on the top-ranked alert:
   ```bash
   uv run uvicorn main:app --app-dir apps/api --port 8123      # port 8000 is Windows PID 4
   TOKEN=$(curl -s -X POST http://127.0.0.1:8123/api/auth/demo-token \
            -H 'Content-Type: application/json' \
            -d '{"subject":"analyst@oxbow.dev","roles":["analyst"]}' | ... )
   ```
   Then `POST /api/cases`, `POST /api/cases/{id}/decisions` with a genuine free-text reason about
   that account's actual evidence, and `uv run python scripts/verify_audit.py` — which must walk
   a chain, not report `NOTHING VERIFIED`. Its `AS subject AS subject` defect was fixed in
   `63cedca`; that fix has never been exercised against populated tables, so this is its first
   real test.
5. **Snapshot** — `uv run python scripts/demo_seed.py --create` then
   `--restore --boot-budget 90`.
6. **Screenshots** — rebuild the web app with the API origin baked in (`next.config.ts` reads it
   at config time), start it, then:
   ```bash
   cd apps/web && bun run build          # with OXBOW_API_ORIGIN=http://127.0.0.1:8123
   node node_modules/next/dist/bin/next start --port 3100 &
   OXBOW_DEMO_ACCOUNT_KEY=<key> OXBOW_DEMO_CASE_ID=<id> node scripts/capture-screens.mjs
   ```
   The script now **exits 1** if it cannot name a real case and account, and asserts each route
   rendered evidence before writing it (`a04e7c3`). Do not defeat that guard: the previous set of
   eleven committed images showed the state gallery rather than the product precisely because
   taking a picture cannot fail.
7. **Video** — `out/oxbow-demo.mp4`, 204.1 s (3:24), 1440×900, verified by reading the
   container back with ffprobe. Three steps, in this order, because each one measures the last:
   ```bash
   # 1. Narration. The beat table in SUBMISSION.md §03 is the only script; this reads it,
   #    speaks each beat with a neural voice (en-KE-AsiliaNeural through `uvx --from edge-tts`,
   #    at its own pace — not a slowed read, which overran the slots), and FAILS on any beat
   #    whose measured audio does not fit its own slot.
   uv run python scripts/make_narration.py

   # 2. Frames. Run from apps/web, and note that `FRAMES_DIR` is relative to the cwd: either
   #    set OXBOW_DEMO_FRAMES_DIR or move out/video/frames up to the repo root before encoding.
   cd apps/web
   OXBOW_LIVE_WEB_BASE_URL=http://127.0.0.1:3100 OXBOW_DEMO_CASE_ID=<id> \
   OXBOW_DEMO_ACCOUNT_KEY=<key> node node_modules/@playwright/test/cli.js test demo-tour.spec.ts

   # 3. Encode. fps is derived from the frame count over the narration's own duration.
   cd .. && uv run python scripts/make_demo_video.py
   ```
   Not Playwright's `video: 'on'` — that needs a private ffmpeg build this project never
   downloads. Frames + system ffmpeg instead. The tour **skips by design** until a live origin
   serving landed numbers exists, because a tour of the bundled fixture would demonstrate the
   fixture. The seven beats are the queue, the case, the empty graph, and three refusals — the
   refusals are in the film on purpose, and the narration says what each one is refusing for.
   Two screens are unfilmable on this deployment today (`/policy` answers 404 for want of an
   active policy row, `/network` draws no canvas because the run stored no edges), and the
   degraded banners on every pane are the deployment naming redis, mlflow, the solver, the
   summariser and OIDC as not running. Starting those containers was not attempted: another
   project on this machine holds port 6379, and bouncing shared infrastructure to make a demo
   look better is not a trade worth making the day of.

## Do not do these, even for time

- Do not lower `config/model.yaml`'s `min_positives_for_calibration` to obtain a reliability
  curve. Every fold refuses it on arithmetic, and the product now ships labelled uncalibrated
  instead — that is the specified behaviour (03 §H), and a card that says so is worth more than
  one that does not.
- Do not catch `ProfileUnavailableError` and fill an ablation cell with another channel's number,
  or with `None` rendered as zero. That is the exact substitution DEV-027 removed.
- Do not let `economics_rows` coalesce a null exposure to zero. The §3.2 cap
  `min(outflow, inflow)` is degenerate on PaySim — **0 of 43,720 accounts both receive and send
  inside the same window** — which is why DEV-032 points at the measured `exposure_minor` in
  `backtest_corpus.parquet` (40,001 accounts non-zero) instead of the reconstruction.
- Do not capture in fixture mode and caption it as the product.

## Honest state if time runs out

Ship the six components with the engineering sections as they are and say plainly in the video's
last beat which screens are serving landed evidence and which are waiting on the walk-forward.
A submission that names its own gap reads as rigor; a screenshot of a skeleton reads as a bug, and
a judge can open the repo and diff the two.
