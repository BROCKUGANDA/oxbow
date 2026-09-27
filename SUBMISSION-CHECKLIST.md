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

Narration is generated locally, no download and no cloud voice. Verified: the first beat
renders **14.9 s** at `Rate = -1`, which fits the 20 s slot in the script.

```bash
# one wav per beat (7 beats, ~3:30 total); keep them under a scratch dir
powershell -NoProfile -File speak.ps1 \
  -Text "OXBOW is a financial crime analytics tool…" \
  -Out "$PWD/narr/beat1.wav"
```

`speak.ps1` is the working synthesiser (note `SetOutputToWaveFile`, not `SetWaveFile`,
which does not exist on this API):

```powershell
param([string]$Text, [string]$Out)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft Zira Desktop')   # or 'Microsoft David Desktop'
$s.Rate = -1                              # slightly slower than default; measured above
$s.Volume = 100
$s.SetOutputToWaveFile($Out)
$s.Speak($Text)
$s.Dispose()
```

Screen capture: Playwright records per-page video natively, which avoids a full-desktop
capture and the RAM it costs on this box.

```bash
cd apps/web
node node_modules/@playwright/test/cli.js test demo-tour.spec.ts   # writes .test-output/video/*.webm
# assemble: concat the beats, pad each to its slot, mux over the video
ffmpeg -f concat -safe 0 -i beats.txt -i tour.webm -c:v libx264 -pix_fmt yuv420p \
       -shortest -movflags +faststart oxbow-demo.mp4
```

`demo-tour.spec.ts` (the scripted walkthrough the video is captured from) is **not written
yet** — it is the remaining piece of component 03, and it wants the demo snapshot below so
the tour shows real numbers.

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

1. **Real five folds** — the score stage lands fold 0 today; folds 1–4 died on host
   allocation. Then `oxbow backtest --corpus …` (its embargo refusal was the guard working,
   not a bug).
2. **Regenerate the cards** — `uv run oxbow eval` clears `provenance: fake_harness`.
3. **Land the demo** — decision through the API, then `demo_seed --create`, then
   `demo_seed --restore --boot-budget 90`.
4. **Re-capture screenshots** and write/record `demo-tour.spec.ts`.
5. **`gh auth login`**, publish, paste the six components into Devpost.
