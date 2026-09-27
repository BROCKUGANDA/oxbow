# Submission checklist — the four days

Companion to [SUBMISSION.md](SUBMISSION.md). Every command here was run on this machine and
is quoted with what it produced, so the remaining work is execution, not discovery.
Rules: deadline **1 October**, six required components, judging is on the Devpost page alone.

## Blocking, and only you can do it

| | |
|---|---|
| **`gh auth login -h github.com`** | The keyring token for `BROCKUGANDA` is **invalid** — verified: `gh auth status` returns `Failed to log in … The token in keyring is invalid`. Nothing can be published until this is redone interactively. |
| **Team real full names** (component 05) | Left blank in SUBMISSION.md on purpose. |
| **Confirm student eligibility** | Rules say *students only*, ages 13+, *companies and professional organisations excluded*. Late entries are rejected outright, so settle this before the deadline, not on it. |
| **YouTube unlisted link** (component 03) | Needs your account; the video file is produced locally. |

## 02 — publish the repository

Already done and gated: MIT `LICENSE` with the dataset-licence notice, no CI workflows,
`.env` untracked, no corpora committed, and
`tests/unit/test_publication_preflight.py` (10 tests) asserting all of it — no developer
home paths, no private key material, no secret-shaped literals in shipped code, the
`RUN_SALT` value in no tracked file, and one code licence named everywhere.

```bash
# after gh auth login
# from the repository root
git ls-files | wc -l                       # sanity: what leaves this machine
uv run python -m pytest tests/unit/test_publication_preflight.py -q   # must be green
gh repo create oxbow --public --source . --push
```

Nothing is pushed by this repository's own tooling; the push stays a human act.

## 06 — screenshots

```bash
cd apps/web
OXBOW_DATA_MODE=fixture node node_modules/next/dist/bin/next start --port 3100 &
node scripts/capture-screens.mjs     # -> docs/screens/*.png, 8 routes, 2x scale
```

Verified working. Current set shows the read-only warehouse (real provenance in the header,
skeletons in the panes) — **re-run after the demo snapshot lands** for submission-quality
images.

## 03 — demo video

Narration is generated locally: no download, no cloud voice, nothing to authenticate.
`scripts/make_narration.py` reads the beat table out of `SUBMISSION.md` §03 — that table is
the only copy of the words, so the voice cannot drift from the script the judge reads — and
synthesizes one WAV per beat through `scripts/speak.ps1`, then pads them onto one timeline at
their own slot offsets.

```bash
uv run python scripts/make_narration.py            # 7 beats + timeline.wav + beats.txt
uv run python scripts/make_narration.py --check    # re-measure existing waves only
```

Measured on this host, Microsoft Zira Desktop at `Rate = -1`, 22,050 Hz:

| beat | slot | audio | fits |
|---|---|---|---|
| 1 Command strip | 0:00–0:20 | 14.8 s | yes |
| 2 Scorecard | 0:20–0:50 | 21.8 s | yes |
| 3 Network | 0:50–1:25 | 17.9 s | yes |
| 4 Queue + capacity | 1:25–2:00 | 23.5 s | yes |
| 5 Case workspace | 2:00–2:35 | 27.5 s | yes |
| 6 Validation / ablation | 2:35–3:00 | 20.0 s | yes |
| 7 Limitations | 3:00–3:30 | 26.6 s | yes |

Total spoken audio **2:32**; `out/narr/timeline.wav` is **206.6 s** with each beat starting at
its own slot, so the voiceover is cut against the script's clock rather than re-timed by hand.
Both are inside the 2–5 minute Devpost ceiling. Every beat was checked for actual signal, not
just file size: RMS 2,795–3,757 and peak 25,412 of 32,767, and the timeline carries audio at
all seven slot offsets.

`speak.ps1` uses `SetOutputToWaveFile` — `SetWaveFile` does not exist on this API and fails at
runtime, not at parse time.

Screen capture: **not** Playwright's `video: 'on'`. That option needs Playwright's private ffmpeg
build under the browser cache (`ms-playwright/ffmpeg-1010/ffmpeg-win64.exe`), and this project
installs no browsers and downloads nothing — enabling it fails with
`Executable doesn't exist`, which is the kind of failure that arrives on the day of filming. So
`tests/states/demo-tour.spec.ts` screenshots the tour on a 500 ms timer into
`out/video/frames/`, and `scripts/make_demo_video.py` assembles the sequence with the system
ffmpeg that is actually installed.

```bash
cd apps/web
# needs OXBOW_LIVE_WEB_BASE_URL and OXBOW_DEMO_CASE_ID set, or it skips with the reason
node node_modules/@playwright/test/cli.js test demo-tour.spec.ts   # ~420 JPEG frames
cd ../..
uv run python scripts/make_demo_video.py --check-only              # verify inputs, no encode
uv run python scripts/make_demo_video.py                           # out/oxbow-demo.mp4
```

`make_demo_video.py` derives the frame rate from `frames / narration_seconds` rather than
declaring one, so the picture ends when the last word is spoken; it then reads the container's
duration back with ffprobe and fails if it disagrees with the narration by more than 2 seconds.
Verified: the guard fires with 0 frames ("only 0 frames in out\video\frames — run the tour
first", exit 1), `probe_seconds` returns 3.0 s on a real file and `None` on a missing one, and a
three-still sequence assembles into h264 with this ffmpeg. The full 206-second encode has not been
run, because a 3:30 encode cannot finish while the score run holds the machine's remaining
memory — do it after the numbers land, with nothing else running.

`demo-tour.spec.ts` exists and is wired to the beat slots, but it **skips until the demo
snapshot below exists** — it needs a live origin serving landed numbers and a case id carrying a
landed decision, because a tour of the bundled fixture would demonstrate the fixture.

## What the video needs first: real evidence

Both component 03 and component 06 are gated on the same thing — landed evidence in
Postgres:

```bash
uv run python scripts/demo_seed.py --create     # refuses until score + fold + decision exist
```

Measured refusal today: `the warehouse holds no evidence to snapshot: scored rows: 0 in
score; backtest folds: 0 in backtest_fold; a landed reviewer decision: 0 in decision`. The
seeder refusing is correct — a blank demo is worse than no demo.

## Order that gets all six done in four days

1. **Real five folds** — **done.** All five folds fitted and scored on the landed corpus, the
   walk-forward completed (`out/backtest/real40k/ablation_results.json`, 9 variants at
   `provenance: real_corpus`), and the fold plan agrees with the corpus's own fold column on
   all 79,998 rows. What cost four attempts was DEV-026: a fold booking one account's analyst
   minutes once per scored row, caught by its own capacity postcondition.
2. **Regenerate the cards** — **done.** `uv run oxbow eval` publishes the real run; `grep -c
   fake_harness` over README, ARCHITECTURE, MODEL_CARD, ECONOMICS_CARD and LIMITATIONS is 0.
   The cards now disclose the two things the table does not measure: the honest rows share one
   fitted stack, and no fold cleared the calibration floor (DEV-024).
3. **Land the demo** — open. Decision through the API, then `demo_seed --create`, then
   `demo_seed --restore --boot-budget 90`. Blocked on rows in Postgres, which is the analytical
   handoff now being landed; the score rows themselves wait on a run that calibrates.
4. **Re-capture screenshots** and write/record `demo-tour.spec.ts` — open. The spec exists, is
   wired to the beat slots and skips with a named reason until step 3 lands, because a tour of
   the bundled fixture would demonstrate the fixture.
5. **`gh auth login`**, publish, paste the six components into Devpost — open, and owner-only:
   the tree stays private until the push is said yes to.
