# OXBOW decision log

Every conflict between the five governing documents is resolved **once**, by the
authority order `00 Constitution > 01 Build Prompt > 03 Edge Cases > 02
Integration > Spec v2.1`, and logged here. Nothing is reconciled silently.

Each entry records what the documents said, which one won, and why. A decision
log that only records outcomes is a changelog; the reasoning is the point.

---

## DEV-001 — package layout: `contracts.py` singular, plus a `scoring/` package

- 01 P1 tree: `contracts.py` (singular) and a distinct `scoring/` package.
- Spec v2.1 tree: `contracts/` and no `scoring/`.
- **Decision: 01.** 01 is rank 2 on *what to build*; the spec is rank 5.
  `scoring/` stays separate because 01 P4 is a whole phase (scorecard, GBM,
  calibration, fusion) and merging it into `models/` buries the auditable
  scorecard under the ranking engine. They ship as a pair and disagree in
  public; that disagreement is the product.

## DEV-002 — API lives at `apps/api/`, one deployable

- 01 P7: `apps/api/main.py routers/ schemas/ deps.py problems.py events.py worker.py`
- 02: `packages/api/` + `packages/worker/`
- **Decision: 01.** One deployable, less ceremony. 02's `worker.py` collapses to
  `apps/api/worker.py`. The port/adapter separation 02 mandates is preserved in
  the *import* graph, not in the directory layout.

## DEV-003 — `run_id` is a ULID, stored as text

- 01 P7: `run_id uuid`
- 02 B identity model: `run_id uuid`, sortable by time
- **Decision: ULID.** Lexicographic time ordering is what 01 P7's SSE resume
  needs, and it is monotonic in the same way `created_at` is, without a second
  sort key. Postgres column is `char(26)`, never the native `uuid` type.

## DEV-004 — `txn_id` is corpus-qualified text, not a bigint

- 01: `paysim:12345` namespaced string
- Spec DDL: `txn_id bigint primary key`
- **Decision: namespaced text.** Two corpora must coexist in one table, and
  PaySim row 41 and IBM row 41 are different transactions. A bigint primary
  key cannot express that; a collision would silently merge two corpora.

## DEV-005 — money is `amount_minor: int64` everywhere, including Postgres

- 01: integer minor units, no float money anywhere
- Spec DDL: `numeric(18,2)`
- **Decision: int64 minor units.** The spec's `numeric(18,2)` is exact decimal
  and loses to the hard requirement, which exists because binary floats cannot
  represent 0.10 and money that cannot be represented cannot be summed.
  Enforced by `scripts/no_float_money.py`, an AST walk, not a code review note.

## DEV-006 — authentication ships as decision policy, not tenancy

- 01: roles and four-eyes approval only
- 02 F: full SSO product, tenancy, per-tenant isolation
- **Decision: 01.** Keycloak/OIDC and the four-eyes review remain, because
  "who approved this" is a real audit requirement. Tenancy does not ship: there
  is one investigating team in this prototype, and building tenancy to hold one
  tenant is a demo of a capability nobody asked for.

## DEV-007 — `verify` and `verify-determinism` are two commands

- 00: `verify` means the reproduction check
- 01: `verify` means the general gate
- **Decision: split.** Same name, two obligations, so they cannot both be
  satisfied by one green tick. `verify` runs the gate; `verify-determinism`
  re-runs a completed run and compares digests.

## DEV-008 — graph is built before features

- 01: features before graph
- Spec: graph features require the graph
- **Decision: graph first.** Execution order is
  `P0 → P1a → P1b → P3a → P2 → P3b → …`. You cannot compute a degree feature
  from a graph that does not exist yet.

## DEV-009 — MinIO image comes from quay.io, not Docker Hub

- P0 wrote `minio/minio:RELEASE.2024-12-18T13-15-44Z`
- **Decision:** that tag was invented, and MinIO has since moved off Docker Hub
  entirely; the Docker Hub path answers `pull access denied`. The pinned tag is
  `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z`, smoke-tested on this host
  (HTTP 200) before being committed. Recorded because the failure mode is
  instructive: a plausible-looking tag that was never queried.

## DEV-010 — visx 4.0.0, not 3.12.0

- P0 pinned `@visx/*@3.12.0`, whose peer range is `react ^16 || ^17 || ^18`.
- The tree installs React 19, so six visx packages reported unmet peers and the
  graph layer would have failed at runtime.
- **Decision: 4.0.0**, which declares `react ^18 || ^19`. A peer warning is a
  latent runtime break in the one component that cannot degrade quietly.

---

## DEV-011 — PaySim has no network; the day-4 fallback fires

**This is the load-bearing decision in the whole build, and it was pre-committed
before the number was known.** Plan §4 states the rule; this entry records the
result.

### The measurement

Run `uv run python scripts/measure_graph.py` on the full corpus
(`PS_20174392719_1491204439457_log.csv`, 6,362,620 rows, SHA-256
`16910f90…4eea6b`). Full output in `data/graph_measurement.json`.

| Measurement | Value | Condition |
| --- | --- | --- |
| `n_distinct_nameOrig` | 6,353,307 | — |
| Sender reuse ratio | **0.001464** | — |
| `n_distinct_nameDest` | 2,722,362 | — |
| Distinct accounts (either side) | 9,073,900 | — |
| Median total degree | **1.0** | — |
| Median counterparty degree | **1.0** | **must be > 2** |
| p90 / p99 / max total degree | 1.0 / 14 / 113 | — |
| Highest-degree **sender** in 6.36M rows | **3 edges** | — |
| Surviving time-respecting 3–6 cycles (20k sample) | **0** | **must be ≥ 10** |

Both pre-committed conditions fail, by a wide margin, not marginally.

### The fraud subset is worse, not better

The obvious objection is that a whole-corpus average could be diluted by the
99.87% of legitimate one-off senders while the fraud subgraph still holds
structure. Measured directly on `isFraud == 1`:

| Measurement | Value |
| --- | --- |
| Fraud rows | 8,213 |
| Distinct `nameOrig` | 8,213 |
| Sender reuse ratio | **0.0 exactly** |
| Max total degree | **2** |
| Accounts with ≥ 2 counterparties | **44 of 16,382** |

Every single fraud transaction has a unique originator. There is no subgraph to
find, because there are no edges to connect.

### Verdict

`STAR_SHAPED_TRIGGER_DAY4_FALLBACK`. The graph is a forest of disconnected
edges, not a network.

This falsifies spec §3.1's framing directly: it cannot be rescued by better
feature engineering, because the substrate is absent. There is nothing to detect.

### Consequence

IBM-AML becomes **primary for Module B** — the 12 typology rules, the graph
features, and the whole network-detection pillar. It is the corpus that carries
real multi-account structure *and* explicit typology labels. PaySim remains
primary for **Module A** (tabular risk scoring and volume), where 6.36M rows of
realistic mobile-money volume is exactly what is wanted.

**The code does not change.** The pipeline, the contracts, the feature
computation and the graph builder are all corpus-agnostic. What changes is which
corpus is primary for which module, and spec §3.1's prose framing.

### Label-quality finding that also stands

`isFlaggedFraud == 1` occurs **16 times in 6,362,620 rows**. It is not a usable
learning target at any depth. `isFraud == 1` (8,213 rows, 0.129%) is the only
viable label on this corpus. The dataset card must say so, because the spec
describes `isFlaggedFraud` as "a crude threshold", which understates how thin it
is.

### Open and blocking

IBM-AML is **41.6 GB**. The host has **57 GB free**. The archive plus its
extracted contents cannot both fit. Options are put to the user rather than
decided unilaterally, because the plan lists representative sampling as an
unresolved choice and 00 §I.3 makes a spec-contradicting finding a halt-and-ask.
