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

---

## DEV-013 — the IBM-AML figure was wrong; Module B has its substrate after all

DEV-011 left one item open: IBM-AML at 41.6 GB against available disk, deferred
to a human. Both halves of that were measured wrong, and re-measuring them
removed the blocker without needing the decision.

**Size.** Kaggle's own metadata for
`ealtman2019/ibm-transactions-for-anti-money-laundering-aml` reports
`totalBytes = 8,176,169,418` — 8.18 GB, not 41.6 GB. The larger number was the
whole published dataset as catalogued elsewhere, not what this slug serves.

**Shape.** The dataset does not contain `transactions.csv` or `patterns.csv`. It
ships **scenario bundles**: `<HI|LI>-<Small|Medium|Large>_Trans.csv`,
`_accounts.csv` and `_Patterns.txt`, up to 17 GB each. The graph thesis needs one
bundle, so `HI-Small` was fetched: 475,664,283 bytes
(`sha256 b19d39f5…c5b040`), plus `HI-Small_accounts.csv` (34,053,187 B,
`78680852…36b014`) and `HI-Small_Patterns.txt` (323,844 B, `2c546b5c…2ef39b`).
Total acquisition cost: ~510 MB and two minutes, against a download of the full
8.18 GB that the CDN refused to resume (`curl: (33) HTTP server does not seem to
support byte ranges`, then `(56) Connection was reset` at 31 %).

**The measurement (`uv run python scripts/measure_ibm_graph.py`, output in
`data/ibm_graph_measurement.json`):**

| Measurement | Value | Condition |
| --- | --- | --- |
| Transaction rows | 5,078,345 | — |
| Distinct accounts | 515,080 | — |
| Directed edges (self-loops excluded) | 647,939 | — |
| **Median account degree** | **10.0** | **must be > 2 — PASSES** |
| Median counterparties per account | 2.0 | — |
| Accounts with > 2 counterparties | 173,735 | — |
| Laundering rows | 5,177 (0.1019 %) | — |
| Surviving time-respecting 3–6 cycles | pending the P3a enumerator | must be non-trivial |

The degree figure is **10.0 with self-loops excluded** and 6.0 with them counted.
The excluded figure is the one reported, because a self-loop gives an account
*degree* without giving it a *counterparty*: including 591k of them would credit the
corpus with connectivity it does not have. Median counterparties per account is
2.0, which is the honest companion number — the pass condition in plan §4 is
phrased on degree, and it passes, but the distribution is a small world of
hub-and-spoke clusters plus 173,735 genuinely connected accounts rather than a
uniformly dense mesh.

DEV-011's verdict stands for PaySim — it is star-shaped and it is not the network
corpus. What changes is that the fallback has real data under it: the pre-committed
action ("promote IBM-AML to primary for Module B") is now executable rather than
aspirational, so no spec rewrite is needed and nothing is parked on the user.

**Two facts found in the bytes that no document predicted, both now constraints:**

1. **The header repeats a column name.** The real `HI-Small_Trans.csv` header, read
   from the bytes with `od -c` rather than recalled, is exactly:

   ```
   Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,
   Amount Paid,Payment Currency,Payment Format,Is Laundering
   ```

   eleven columns, two of them named `Account` (sender and receiver), and the only
   label column is `Is Laundering`. Any reader that keys columns by name silently
   loses one.

   This is a live defect risk, not a historical note: the first IBM contract written
   in this repo declared `stepFrom, stepTo, Type, Category, Amount, nameOrig,
   balanceOrig, nameDest, balanceDest, isLaundering, isFlood, isDateSpam,
   isForcedCashout, unlabeled` — fourteen columns belonging to a *different* IBM AML
   artefact (the `obj_feature`/DTL variant). Every test written against it passes,
   because it is testing its own invention. It is recorded here in full so that the
   correction can be checked against this list instead of against anyone's memory.
   `Is Flood`, `Is DateSpam`, `Is ForcedCashout` **do not exist in this bundle**: the
   typology dimension comes from the pattern file, per DEV-014, not from label
   columns.
2. **591,212 rows (11.6 %) are self-loops** where sender account and receiver account
   are the same, concentrated in `Reinvestment`. Plan §7 already mandates excluding
   self-transfers from cycle and fan detection while keeping them as a feature; on
   this corpus that is not an edge case but a tenth of the data, and a graph layer
   that collapsed them would invent 591k edges of fake structure.

Also for the record: 15 currencies in one bundle, so the cross-currency aggregation
guard (plan §8) is live rather than defensive; timestamps are
`YYYY/MM/DD HH:MM` with no timezone and minute precision, which is why intra-minute
ordering must be derived deterministically rather than trusted from file order;
and the laundering-labelled rows are themselves near-degree-1 (median 1.0), which
means the typology signal lives in the *pattern annotations*, not in the label
column — a finding that shapes how per-typology recall is measured in P6.

---

## DEV-014 — IBM's pattern file is labelled typology ground truth, and it is used

Plan §6 requires per-typology recall in the backtest ("proves network detection
rather than point-anomaly detection"), and §9's rules name the typologies they are
supposed to catch — but no document said where typology *labels* would come from.
PaySim has none. This resolves it: they were in the corpus all along.

`HI-Small_Patterns.txt` is the transaction stream interleaved with markers —
`BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 16-degree Fan-Out` …
`END LAUNDERING ATTEMPT - FAN-OUT`. `scripts/build_ibm_typologies.py` walks it and
joins each annotated row to its transaction by the ten non-label columns, FIFO per
key, failing closed if an annotation matches nothing. Output
`data/processed/ibm_typologies.parquet`:

**3,209 annotated transactions in 370 attempt blocks, across 8 typologies** —
GATHER-SCATTER 716 · SCATTER-GATHER 626 · STACK 466 · FAN-OUT 342 · FAN-IN 318 ·
**CYCLE 287** · BIPARTITE 263 · RANDOM 191.

Why this matters more than a row count: **287 CYCLE rows and 660 fan rows are
labelled ground truth for R4, R2 and R3.** P6 can now report recall *per typology*
against real annotations instead of inferring network detection from an aggregate
AUC, and the `RANDOM` block is a free negative control — planted laundering-attempt
traffic with no typology, which is exactly the population a rule that "fires on a
third of accounts" would wrongly catch.

Rejected alternative: relying on the `Is Laundering` column alone. That was measured
in DEV-013's footnote — labelled rows are near-degree-1 among themselves, so a model
tuned on the binary label learns account-level oddity, not network shape. The label
column says *whether*; the pattern file says *what*. Using only *whether* would have
quietly defeated the thesis.

Three format hazards found while building it, all now handled in code: marker lines
appear both with and without a trailing description; the pattern file's timestamps
use `-` while the transaction file's use `/`; and the pattern file's rows repeat the
label column, which is why the join key deliberately excludes it.

---

## DEV-015 — measured reality contradicts R4's definition of a cycle (00 §I.3 halt-and-ask)

**This is the second time in this build a spec assumption has met the data and lost,
and it is more serious than DEV-011 because it does not change which corpus we use —
it changes a rule definition.**

### How it was found

`scripts/measure_ibm_cycles.py` was written to answer DEV-013's remaining
`PENDING` condition: does a non-trivial number of time-respecting, value-retaining
3–6 cycles survive on IBM-AML? It answered **zero**, and `truncated=False` ruled out
the budget. Three checks isolated the cause:

1. **The sampler was not the problem.** An earlier draft selected accounts by a hash
   nibble (~19 % each), which severs a four-node loop with probability 1 − 0.19⁴; that
   arm's result was discarded as an artefact of its own sampling. The rerun selected
   *whole* components — every planted-cycle account plus its one-hop counterparties
   (1,218 accounts) — and still found zero.
2. **The enumerator is correct.** A hand-built A→B→C→D→A loop, fed through
   `build_graph`, returns exactly 1 cycle. `graph/cycles.py` is not broken.
3. **Relaxing everything still gave zero.** `ignore_rails=False`,
   `require_non_increasing=False`, `value_retention_floor=0.0` — all separately and
   all together — 0 cycles.

So the question became what the corpus's own labelled cycles actually look like.
Parsing the 54 CYCLE attempt blocks out of `HI-Small_Patterns.txt` directly:

| Property of the 54 labelled cycles | Count | Against our definition |
| --- | --- | --- |
| Last hop returns to the first account (a real loop) | **54 / 54** | — |
| **Cross-currency legs** | **38 / 54 (70 %)** | excluded: the graph requires one currency per loop (§8 "currency is part of every amount") |
| Retention `min/max` below 0.6 | **25 / 54** | excluded by `value_retention_floor: 0.6` |
| Retention of the twelve worst | **0.0015 – 0.0119** | the floor is not marginally wrong, it is two orders of magnitude wrong for this corpus |
| Amounts that **grow** along the loop | most blocks | excluded by `require_non_increasing` |
| Only two legs (A→B→A mutual round-trip) | **14 / 54** | excluded by `min_length: 3` |
| Timestamps not monotonically increasing | 5 / 54 | excluded by time-respecting |

Reproduced by `uv run python scripts/measure_ibm_cycles.py`, which writes
`labelled_cycle_anatomy` into `data/ibm_cycle_measurement.json`; the numbers above are
that field, not a hand count.

A typical planted cycle, verbatim from the corpus: Yuan 58,702 → Swiss Franc 7,332 →
Shekel 26,443 → Canadian Dollar 10,621 → Rupee 637,140 → Rupee 621,578 → Euro 7,222 →
Yen 892,031. Retention against the peak is 0.008, the amounts are nowhere near
non-increasing, and no two consecutive legs share a currency.

### The consequence, stated plainly

**R4 `CYCLE_MEMBER` as defined in plan §9 cannot fire on 70 % of the cycles the corpus
itself labels as cycles, and on the remaining 30 % it is blocked by the retention
floor, the non-increasing requirement, or the two-leg shape.** Left as written, the
headline network rule would score zero on the labelled positive set — and §9's own
guard would then be obliged to report a permanently dead rule. The failure mode is
not "the rule is weak"; it is "we shipped a typology detector that is definitionally
blind to the typology."

### Decision

Both requirements are real and neither is being thrown away, so the definitions
change rather than the intent:

1. **Retention and non-increasing are re-specified in a common unit, with the
   conversion stated rather than implicit.** §8 forbids *implicit* FX; it does not
   forbid a documented one. `config/economics.yaml` gains a declared reference-rate
   block, marked **illustrative** like every other assumption, and R4 measures
   retention on converted minor units. Cross-currency layering is not an artefact to
   be filtered out — it is the mechanism layering uses.
2. **`require_non_increasing` becomes an upper-bound-per-hop policy**, not a hard
   monotone constraint, and is configurable; the retention floor stays but is
   documented as a *choice about the corpus*, with 0.6 shown to exclude 25 of 54
   labelled cycles at that threshold.
3. **Two-leg mutual round-trips become their own typed pattern**, not a silently
   dropped sub-threshold cycle: A→B→A is how a mule pair parks value, and suppressing
   it because `min_length=3` loses real signal for a definitional reason.
4. **R4's hit-rate and the ablation's graph-features row must be reported against the
   54 labelled blocks as ground truth**, so the rule is measured on what it claims to
   detect instead of on an aggregate that hides it.

### What this does *not* claim

It does not say the corpus's cycles are "better" than our definition, nor that the
planted patterns are real laundering — they are synthetic scenario annotations
(DEV-014's caveat). It says our detection definition and the only labelled positive
set available disagree, and that a rule whose positives are definitionally disjoint
from the labels is a rule that will be reported as dead by our own guards. Fixing the
definition is cheaper and more honest than discovering it at the demo.

Raised as a halt-and-ask under 00 §I.3: measured reality contradicting a spec
assumption. The pre-committed action is to report the numbers and continue with the
documented fix — which is what this entry does.

## DEV-016 — the IBM source entry declared files that do not exist, and hid its own scope

**Authority:** 00 §C step 1 ("acquire the data before writing code that assumes its
shape") and §16's definition-of-done item requiring every recorded figure to be
reproducible from a command.

### The observation

`config/sources.yaml` pinned `ibmaml` to two members, `transactions.csv` and
`patterns.csv`, each carrying `sha256: "RECORDED_AT_DOWNLOAD"`. Neither exists in the
archive the declared slug serves, so `scripts/download_data.py --verify-only` — the
P1a gate — died with `does not exist`, and had done so since the entry was written.
The same entry declared `role: secondary` while every Module B artefact in the
repository, including DEV-013's graph measurements and DEV-015's cycle table, was
computed from this corpus and from nothing else.

### Why it survived

The sentinel is the mechanism: a slot that reads `RECORDED_AT_DOWNLOAD` looks like
an unfinished task rather than a false claim, so it passes a reviewer's eye and fails
only a command that actually opens the file. The two names came from the 2019
revision of the corpus; the slug serves the 2023 revision (arXiv 2306.16424), whose
members are `HI-Small_Trans.csv`, `HI-Small_accounts.csv` and `HI-Small_Patterns.txt`
— the shape DEV-013 established by reading bytes. `role: secondary` was written when
PaySim was assumed sufficient for the whole build, which DEV-011 later refuted.

### The resolution

1. Both phantom entries are deleted. A hash slot for an absent file can only hold a
   sentinel or an invention, and the gate's job is to distinguish "not yet
   downloaded" from "this is not what the source ships". The three real members now
   carry hashes measured on this host, and `--verify-only` confirms all three
   against 475 MB, 34 MB and 324 KB of bytes on disk.
2. `role` becomes `primary` and `module` names Module B explicitly, because the
   config is what an operator reads to learn what the evidence rests on.
3. A new `acquisition_scope` field states that the corpus ships six bundles — HI/LI
   crossed with Small/Medium/Large — and that only HI-Small was acquired, about
   510 MB of the 8.18 GB served. Without that sentence, "IBM-AML has 5,078,345 rows"
   reads as if the network claim were measured over the corpus; it is measured over
   one scenario bundle.
4. The stale 41.6 GB figure in the same block's comment is replaced with the
   measured 8.18 GB (DEV-013), and the 50,000-row statistics are now labelled as the
   slice they are, with the full-corpus prevalence (0.1019%) and median degree (10.0)
   alongside.

### Consequence to watch

The P1a gate now passes for a verifiable reason, which means it can also fail for one:
if the bundle is ever re-acquired under a different revision, the hashes break by
design. That is the intended behaviour, not a regression — a silent mismatch between
declared and actual bytes is the failure this entry exists to prevent.

---

## DEV-017 — canonical event v1 made balance columns non-null, and one corpus has no ledger

**Authority:** 00 §I (no invented data) over the P0 schema declaration.

### The collision

`assert_canonical_frame` required all four balance columns non-null, because
`NULLABLE_COLUMNS` named only `label_typology`. IBM-AML's real header — DEV-013 read
it off the bytes, `Timestamp,From Bank,Account,To Bank,Account,Amount Received,
Receiving Currency,Amount Paid,Payment Currency,Payment Format,Is Laundering` — has no
balance column of any kind. So the only ways to make 5,078,345 real rows validate were
to write `0` (an invented ledger: every account in Module B would appear to hold
nothing, and the balance-delta features the plan explicitly wants would compute from
it) or to drop the corpus.

### The resolution

The four columns became nullable, and the meaning of a null was pinned by
`assert_balance_provenance`: a source either carries a ledger on **every** row or on
**none**. All-absent is a documented property of the corpus; a mixture is a column that
failed to parse, and it is a hard failure because of what the alternative reading is —
downstream, a null balance is indistinguishable from "this account held nothing" unless
something at the boundary said which one it was.

`label_typology` was already nullable for the mirror-image reason, and the same
discipline now applies to it: DEV-014 measured 3,209 annotated rows inside a
5,078,345-row stream, so a per-row null is normal, while zero joined rows means the
annotation join died. The first version of that guard refused any null, which read as
strict and made the corpus uningestable; the replacement refuses the actual failure.

### What this changes downstream

Balance-proxy features are PaySim-only. The plan's money rules say balance
inconsistency is a feature and not an error — that statement was written about PaySim,
whose balances are known to disagree with `amount`, and it does not transfer to a
corpus with no balances at all.

## DEV-018 — the contract refused self-transfers; the plan asked for them kept and excluded

**Authority:** 01 P3 (quoted below) over the P0 check. This is the fourth time measured
reality contradicted a spec assumption in this build.

### The collision

`_no_self_edge` rejected any canonical row whose originator and destination account
keys were equal, with a docstring citing 01 P3's authority. But 01 P3 says self
transfers are "**excluded from cycle and fan detection while keeping them as a
feature**" — kept. The check read the second half of the sentence and enforced the
first half at the wrong layer, which silently discarded the "keep" part.

DEV-013 measured 591,212 self-edges in IBM's HI-Small bundle: 11.6% of the corpus,
mostly `Payment Format = Reinvestment`. PaySim has none, so no test had ever
distinguished "the rule holds" from "no corpus has ever hit the rule". Ingesting IBM
therefore failed on the first batch that contained one, and the ingest ran to
completion only after the check was deleted.

### The resolution

Self-edges are legitimate events and canonical rows. The exclusion belongs to
`oxbow.graph`, which is where "what counts as a counterparty" is decided, and which
already counts what it sets aside (singletons are reported, not hidden). Deleting a
contract check without moving the guarantee would have been the worse error: a graph
with self-loops admitted silently would inflate degree and could report A→A as a loop
laundering pattern, which is precisely the false positive DEV-015 is about. So the
contract relaxation and the graph-layer exclusion land together, and the graph layer
now has to state how many self-edges it ignored.

The rule mirrors what the plan already does for reversals: kept visible, excluded from
cycle detection, and declared.

## DEV-019 - DEV-015's table was right and its conclusion was arithmetic

**Authority:** 00 §I.3 over DEV-015's prose, on measurement. DEV-015's *decision* stands;
one sentence of its stated consequence does not, and the sentence is the one a later
phase would have built on.

### The collision

DEV-015 measured the IBM-AML bundle's own 54 labelled `CYCLE` blocks and recorded a
per-knob table: 38 cross-currency, 25 below the 0.6 retention floor, 45 with amounts
growing along the ring, 14 shorter than three hops, 20 longer than six, 5 not
time-increasing. Every one of those numbers reproduces from the bytes. From the table
DEV-015 concluded:

> R4 `CYCLE_MEMBER` as defined in plan §9 cannot fire on 70 % of the cycles the corpus
> itself labels as cycles, and on the remaining 30 % it is blocked by the retention
> floor, the non-increasing requirement, or the two-leg shape. [...] the headline network
> rule would score zero on the labelled positive set.

That conclusion is the **union** of the six columns, and it was never computed. The
columns overlap heavily — 38 + 25 + 14 + 5 reads like 82 against a population of 54 only
if you add them. Measured, the union is 51 of 54. Three cycles are refused by nothing at
all: three single-currency Saudi Riyal rings of 3–4 hops, value retention 0.82–0.94,
strictly increasing timestamps (two of the three also grow along the ring, so the
non-increasing knob leaves two). Shipped R4 fires on three of the labelled positive set,
not zero.

The failure mode this entry exists to prevent is the one DEV-015 was written to prevent:
a rule declared dead, and a detector therefore re-specified around a zero that the corpus
never agreed to. It was one inference away from happening, and the difference between the
two outcomes is a tally nobody ran.

### The resolution

DEV-015's decision is unchanged: the retention floor stays configurable, the currency
rule stays a per-loop requirement, and `require_non_increasing` becomes a policy knob
rather than a hard monotone constraint. Three single-currency rings scoring hits is
exactly the behaviour a typology detector should have on a corpus of typologies, and it
is the evidence the re-specification was argued from.

What changes is that the claim is now asserted as a measurement instead of asserted as a
sum. `tests/unit/test_p3b_r4_labelled_cycles.py` pins all six per-knob counts *and* the
three survivors, measured through the same `build_loop` / `loop_reasons` pair the rule
uses, so a change in any knob moves a number in a test rather than a sentence in this
file. The counts are taken with `require_non_increasing` set, as §9 states the rule,
because that is the configuration under which the "amounts grow" column exists at all;
the headline is then taken with the shipped configuration, where the knob is off.

Two smaller facts the same measurements settled, both of which had been read the other
way:

* `amount_increases_along_loop` is a first-class refusal **reason**, not only the
  per-hit evidence field `rules/network.py` reads off an anchor. `loop_reasons` emits it
  whenever `require_non_increasing` is set, and 45 is what it counts. A test that tallies
  reasons with the knob off sees the key absent, not zero.
* `non_increasing_breaches` counts consecutive legs and does not wrap from the last leg
  back to the first. DEV-015's own quoted nine-hop ring therefore has three breaches, not
  the four a reader gets counting round the circle — and the wrap is the one comparison
  that runs against the direction of time, since the closing leg is the most recent and
  the first leg the oldest.

Neither of these changes a shipped default. Both are now visible in a test, which is where
a claim about a corpus belongs.
