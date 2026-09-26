# BACKLOG

Work that is deliberately not done yet, each with the reason it is deferred and
what would force it. A deferral without a named reason is a lie by omission — the
same failure mode as a silently dropped row (02 §D), applied to scope instead of
data.

## Blocking nothing, but real

| Item | Phase | Why it is here | Force it with |
| --- | --- | --- | --- |
| Full-corpus metrics on IBM-AML typologies | P6 | The interactive product runs on a connected ~500 k subcorpus per the sampling rule in `config/pipeline.yaml`; full-corpus numbers are offline work. Plan §B3 sanctions the split. | `make eval --corpus ibmaml --full` |
| Elliptic dataset | — | CC BY-NC-**ND** 4.0 forbids derivatives, so a derived sample would violate the licence. Cite-only in the README, never ingested. Enforced by `ingest_allowed: false` in `config/sources.yaml`, which the reader honours. | Nothing — it is a refusal, not a preference |
| IEEE-CIS | — | Competition-governed; redistribution terms on derived splits are unclear. Never used. | Nothing |
| Fold plan is computed **last** in the score stage | P4 | `oxbow score` builds the graph, fires R1–R12 and computes all 75 features before `build_walk_forward` decides whether the landed corpus can hold a 30-day embargo across five folds. Measured cost of finding out late: 20–45 minutes per attempt, three attempts. The refusal itself is correct and stays — inventing fold boundaries is the leakage §8 forbids — but the *order* is an operational defect: the cheapest check in the stage should run first. | Reordering the stage so the timeline is measured before the expensive work, and a printed corpus-window line in the refusal |
| `data/interim/<source>/run_manifest.json` is one mutable pointer | P1b | Any ingest re-points every downstream consumer, including `make verify`'s own P1b gate. It fired twice this session: a queued job landed a 5,000-row manifest and the score stage refused on a 4-day corpus. Per-run manifests (`runs/<run_id>.json`) plus an explicit pointer would make the choice visible. | A `--keep-manifest` / per-run manifest change in the ingest writer |
| `PROMPT.md` | P0 | Plan §5 line 143 asks for the **verbatim** 01 build prompt committed. This session has the master plan, which *encodes and quotes* the five upstream documents but does not reproduce 01 whole. Committing a reconstruction and calling it the original prompt is the exact kind of artifact this build refuses to produce, so the file is absent rather than invented. | The 01 document itself, dropped into the repo |
| `notebooks/02_features.ipynb`, `notebooks/03_validation.ipynb` | P2, P6 | `01_eda.ipynb` exists and carries the measured reuse figure, row count and DEV-011 framing. The other two are gate-bearing (§4 requires the number and the command that produced it pasted in), and there is no feature-matrix run or fold result to paste yet: the score stage has not produced scored rows on this host. A notebook of prose where the plan expects output is a placeholder, which §19 forbids. | One completed `oxbow score` and one completed `oxbow backtest --corpus` |
| Worker liveness is not observable | P7 | `api` now has a healthcheck; `worker` has a 5m stop grace and RQ heartbeats in Redis, but nothing probes *this* worker being alive, so a silently-dead queue looks healthy and jobs queue forever. `worker_ttl` expires the registration key; nothing reads it. | A queue-depth/heartbeat probe in `/healthz`'s component list |
| Stream / CDC ingestion | P7 | `StreamSourceAdapter` is declared in `ports/source.py` and deliberately not built: the detection thesis is windowed, and incremental graph maintenance is a quarter of work, not a week (02 §C, §G). | A live-rails requirement, which the track rule forbids anyway |
| Push-API ingestion | P7 | Route behind a flag, disabled in demo. | Real integrator demand |
| Toxiproxy integration suite | P7 | Needs a pullable image and a network path; the degraded-response behaviour is unit-tested in the meantime. | CI with registry access |
| Hosted read-only demo (Fly.io / Render) | P9 | Plan §15 lists it, but it publishes derived CC BY-SA / CDLA data and needs a human decision on share-alike publication first (00 §I.4). **Not deployed unilaterally.** | An explicit licensing confirmation |

## Cut ladder position (00 §F)

Nothing on the never-cut list is cut: the ablation table, the calibration curve,
the walk-forward embargo, the cumulative benefit curve, the capacity cutoff line,
the twelve typology glyphs, matched-geometry skeletons, the hash-chained decision
log, the limitations section. If the schedule slips, cuts come in §17's order and
each cut gets a line here naming what was dropped.

## Known-unknowns carried into later phases

- `local_hour` and `event_ts_utc` must never be mixed (03 §C). The rule engine and
  the UI's hour-hunting both depend on this staying true; the graph and features
  layers assert it at their boundaries.
- PaySim balance columns are internally inconsistent with `amount`. That
  inconsistency is a **feature**, not an error to repair — the repair would be the
  silent coercion the contracts exist to prevent.
- IBM-AML's column names must be re-verified against the real bytes on arrival if
  the adapter was written before the download landed; `make data --verify-only`
  is the check.
