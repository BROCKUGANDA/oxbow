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

## DEV-012 — run-stamped columns stay out of the persisted bytes, or determinism is unclaimable. **Written late; the code has carried it since P1b.**

**Status: in force, implemented, and previously missing from this file.** This entry was written
on 2026-09-27 when `PROMPT.md` was being assembled and the DEV sequence was checked for
completeness: DEV-011 and DEV-013 have entries, and DEV-012 — cited by name in **27 places across
17 files**, checked with `grep -rn "DEV-012" packages apps scripts config tests STATE.md` — had
none. A decision that eight modules point at as
authority was recorded nowhere a reader would look. The entry below is reconstructed from the code
that implements it, with the numbers measured rather than copied from the prose.

**The decision.** `ingested_at` and `run_id` are members of the canonical v1 contract and are
**never written into the Parquet bytes**. Both are sidecar-carried: the batch manifest and the
warehouse table. Measured against the running code, `CANONICAL_COLUMNS` is 21 wide,
`SIDECAR_COLUMNS` is `("ingested_at", "run_id")`, and `PERSISTED_CANONICAL_COLUMNS` is therefore 19
— and both `canonical_v1_schema()` (21) and `canonical_v1_persisted_schema()` (19) are **derived**
from that one tuple rather than written out twice, so the two shapes cannot silently drift apart.

**Why.** `make verify-determinism` runs the pipeline twice and diffs the artifact digests. A
wall-clock reading or a random ULID inside the data makes byte-identity arithmetically impossible,
so the gate would be unimplementable rather than merely failing. The corollary reaches one level
deeper than the columns: `new_run_identity` takes `batch_id` as a **parameter** rather than calling
`uuid4()`, because a random batch id embedded in canonical columns would break the same guarantee.
The caller passes a content-derived id (`batch_id_for_rows`), and the only random value in a run is
`run_id`, which lives in the manifest sidecar.

**What this deliberately is not.** A loophole in the determinism check. `verify_determinism.py`
names the excluded set in its own docstring — the identity columns and the batch id, which 01 §A
rule 8 *requires* to vary per run — and states that excluding them is the reason the
contract/persisted split exists. The claim being verified is "the same corpus through the same code
produces the same data bytes"; the run identity is the thing being held out of that comparison
because it is the thing that legitimately differs.

**The related bug this split once hid.** `adapters/file/source.py` and `ingest/canonical.py`
disagreed about which of the two column lists the persisted frame used (recorded in `STATE.md`);
they are reconciled here, and `contracts/canonical_v1.py` is the single source of truth. A test
that compared at the 21-column shape while the bytes held 19 would have passed by reading the
sidecar back in — which is why `tests/unit/p2_fixtures.py` re-attaches the two sidecar columns
explicitly and says so in the fixture, rather than letting the two shapes be confused for each other.

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

## DEV-020 — the scorecard's monotonic binning had never once run, and a fallback hid it

**Authority:** 00 §G ("a phase is done when a test would fail if the code regressed")
and §10's own gate clause ("no bin has zero bads without an explicit merge or smoothing
rule recorded"). This is a defect the *plan's named tests* would have caught, which is
why it is recorded next to the fix rather than in a punch list.

### What was found

`scoring/binning._numeric_edges` asks optbinning for `solver="mip"` with
`monotonic_trend="auto_asc_desc"` when `binning.enforce_monotonic_trend` is true. Two
independent faults meant that call never produced a boundary table:

1. `time_limit` was passed straight from config, where it is `20.0` **seconds**.
   optbinning forwards it to `pywraplp.Solver.SetTimeLimit`, whose signature is
   `int64_t` and whose unit is **milliseconds**, so the constructor raised
   `TypeError: in method 'Solver_SetTimeLimit', argument 2 of type 'int64_t'`.
2. `if binner.splits:` asked numpy for the truth value of a multi-element array, which
   raises `ValueError: The truth value of an array with more than one element is
   ambiguous` — on exactly the features that found two or more boundaries.

Both landed in the same `except Exception:` arm, which returns deterministic quantile
boundaries and does not carry the cause. So every numeric feature in every run this
repository has ever produced was binned by quantiles while the monotonic-trend promise
went unkept, and nothing anywhere said so. The quantile fallback is a legitimate
outcome; a fallback that is indistinguishable from success is not.

A third symptom of the same fault: the `ValueError`/`DeprecationWarning` was raised
*inside* the guarded block and attributed to `oxbow.scoring.binning`, which
`pyproject.toml`'s `error::DeprecationWarning:oxbow.*` filter promotes to an error —
so the same data produced eight bins under pytest and one bin outside it. Bin tables
depended on the warning filters of the calling process.

### The resolution

`_solver_time_limit_ms` converts at the boundary where the unit differs, and returns
`None` for a non-positive limit. `_splits_to_list` replaces the truth test with an
explicit `None`/`size` check. `_numeric_edges` now returns its fallback **cause** as a
third element, and `_fit_numeric` writes it into the feature's notes with the sentence
"the declared monotonic trend could NOT be enforced on this feature" — the artifact
says when the promise was not kept. `binning.prebinning_method`, which was loaded and
validated in `scoring/config.py:409` and then never passed anywhere, is now wired into
the constructor.

Measured after the change, on a seeded 4,000-row monotone signal:
`boundary_source='optbinning-mip'`, eight value bins, bad rates
`0.011 → 0.026 → 0.075 → 0.127 → 0.276 → 0.411 → 0.597 → 0.711`,
`monotonic_direction='ascending'`, and the same seven-bin table inside and outside the
pytest warning filter.

### The second fault, same cause

`models/explain._scorecard_outcome` counted fallback rows with
`Series.filter(pl.col(...) == ...)` — polars `Series.filter` takes a predicate or a
boolean series, not an expression, so it raised `TypeError: unsupported type 'Expr'` on
**every** scorecard-explained path, including `bundle is None`, which is the mode the
PSI drift action selects. The points fallback existed only on the branch where it was
not needed. Fixed by counting the mask directly; `test_the_scorecard_fallback_completes_on_every_row`
now asserts the annotated frame, the reason string and the per-row points.

### What this changes for reading earlier results

Anything that quotes a numeric bin table produced before this commit describes quantile
bins. The §10 gate clause itself still held — no populated zero-bad bin survives without
a merge or smoothing record — because the merge pass ran either way. What was not real
was the monotonicity.


## DEV-021 — the scorecard segfault is a native load order, and the guard that named it was itself too strong

**Authority:** 01 §D's determinism requirement (a stage that dies with exit 139 and no
traceback cannot be a reproducible gate), and 03 §A rule 1 (fail loudly at the boundary
and name the cause).

### The fault

`oxbow score` ended in a segmentation fault inside the scorecard fit: no Python
traceback, exit 139, deepest frames `osqp/interface.py:33 algebra_available` →
`:48 default_algebra` → `:59 default_algebra_module`, i.e. the import of
`osqp.ext_builtin`. Two wheels ship overlapping OpenMP/BLAS runtimes, and the load
order decides whether the process survives: on this machine
`import pyarrow; import cvxpy` dies and the reverse exits 0. Nothing in the repository's
own code was wrong. `tests/conftest.py` had already been carrying this fact for the test
session — its docstring says the pipeline's composition root does the same thing
explicitly — but it did not, and that is why the CLI died where pytest did not.

### What was rejected, and why

- **`OSQP_ALGEBRA_BACKEND=<bogus>`.** It survives the crash only by making cvxpy's
  probe raise `KeyError` and skip OSQP. That is disabling a solver to dodge a defect.
- **Selecting SCS or CLARABEL in the scorecard.** optbinning 0.19's `solver="mip"` path
  is ortools (`binning/mip.py` → `pywraplp`), not cvxpy, and cvxpy's OSQP discovery
  happens during `import cvxpy` — before any binning problem exists. A solver choice
  cannot reach the fault.
- Both were tried or reasoned to exhaustion before the ordering was accepted as the
  cause, and DEV-019's predecessor entry recorded the fault while it was still
  unexplained; this entry closes it.

### The fix, and the guard that overreached

`oxbow/cli.py` now imports `osqp` above every `from oxbow...` line, so the extension
resolves at the one moment when it is safe and every later probe is a cache hit. OSQP
stays installed, stays discoverable by cvxpy, and still solves what it is asked to.

The first version also asserted, at import time, that `osqp` had been loaded *before*
`pyarrow`. That is not the property the fault depends on, and it broke
`tests/unit/test_p0_toolchain.py` three ways: `import oxbow.cli` raised in any process
that had touched pyarrow first, which includes the pytest session — where
`conftest.py` has already loaded optbinning (and so cvxpy) early and osqp legitimately
arrives later. That session loads osqp second and does not crash, which is the direct
refutation of the stricter rule. The check is now only "the pin ran" (`osqp` present in
`sys.modules`), with the reasoning recorded at the call site: **a guard that fires on a
healthy process is worse than no guard**, because the next reader reaches for the skip.

Status of the claim itself: the ordering fix is in place and the score stage gets
further than it ever has, but "the scorecard fit no longer segfaults" is only a claim
when a run lands scored rows. Until it does, this entry records the diagnosis, not a
green P4.

---

## DEV-022 — Bun replaces pnpm as the JS toolchain. **Accepted by the owner's ruling.**

**Status: accepted, and enforced by the gate.** plan §T2 pinned `packageManager:
"pnpm@9.15.9"` with a committed `pnpm-lock.yaml`, and §13 put `pnpm audit` in CI. That pin has
now been amended by the owner, who instructed the change directly ("use bun as package
manager") after being shown the conflict between the §T2 pin and the measured fact that pnpm
has never existed on this host. The tree reflects the ruling: `packageManager: "bun@1.4.2"`,
`bun.lock` committed, `pnpm-lock.yaml` removed, and the Dockerfile, Makefile, pre-commit
recipes, README generator and P8 gate all agree. `bun` installs the tree and runs the scripts;
**Node still serves the build** — the runtime stage is `node:22-slim` and its CMD is
`node node_modules/next/dist/bin/next start`, so every web measurement taken on Node remains
about the artifact that runs.

**The correction this heading carries.** A second session, working from its own transcript,
could find no record of being asked and therefore concluded the approval had been invented. It
reverted the migration twice, wrote "no such approval was given" into a commit message, and
restated this entry as *rejected*. The approval had in fact been given — in a parallel session
on the same repository, whose transcript sat in the same project directory the whole time. The
ruling was real; the revert was not. Recorded here because the failure is general and worth
remembering in both directions: **an agent's claim that the owner approved something is not
authorisation**, and neither is another agent's claim that no approval was ever given. Check
the source, and when the source is a transcript, search all of them.

What remains genuinely open, and was true before the ruling and after it: the plan's §T2 text
still names pnpm, so this entry is the amendment and a reader of §T2 alone will get the wrong
answer. Update the plan or the pin, not just the code.

---

### The proposal, as written

**Authority:** 01 §D's reproducibility rule (a pinned toolchain a judge can re-run) collides with
plan §T2's specific pin, `packageManager: "pnpm@9.15.9"` with a committed `pnpm-lock.yaml`. The
pin is a claim about bytes on a machine, and 00 §I.3 says measure it before building on it.

### The fault

pnpm is not installed on this host and, per `scripts/verify.py`'s own comment, never was. Neither
is corepack. So every web gate the repository named was unrunnable as written: `make web`,
`make lint-web`, `make test-web`, `make test-e2e`, `make audit`, and `make bootstrap`'s
`pnpm install --frozen-lockfile`. The P8 gate had already been rewritten to
`node node_modules/vitest/vitest.mjs run` to dodge this -- a green phase definition that cannot
execute is the same phantom `make demo` was, so the dodge was correct for the host and wrong for
the project: it left the *declared* toolchain fictitious.

Measured on 2026-09-27: `bun 1.4.2` is installed. It is the only JS package manager on the box.

### What was rejected, and why

- **Keep pnpm declared, document Bun as an alternative.** Leaves the fiction in place: a clone
  following the README hits `command not found` at the first web gate. This is the exact failure
  class the plan's halt-and-ask rule exists for.
- **Bun as the production runtime too** (both Docker stages on `oven/bun`, `bun run start`).
  Rejected: the process answering a request would then be Bun's Node-compatibility layer, and
  every web measurement already in STATE.md -- CLS per route, first-load JS, the 4-minute demo's
  timings -- was taken on Node, the interpreter `engines.node` names. Bun installs the tree and
  runs the scripts; Node serves the build. The runtime stage keeps `node:22-slim` on its existing
  digest and runs `node node_modules/next/dist/bin/next start`.
- **Committing both `pnpm-lock.yaml` and `bun.lock`.** Two JS lockfiles that can disagree, with
  nothing in the repo to catch the drift. Deleted, and
  `test_bun_lockfile_is_committed_and_owns_the_tree` asserts the absence.
- **`bun install` over the existing pnpm tree.** Tried first, and it lied: it reported
  "292 packages installed" while leaving `node_modules/next` a symlink into `node_modules/.pnpm`,
  so the host would have measured a pnpm-isolated tree that no container ever builds. `rm -rf
  node_modules` then `bun install --frozen-lockfile` produced the hoisted layout the image gets.

### The landmine that only a toolchain swap finds

Bun reads a top-level `overrides` and ignores `pnpm.overrides` entirely. The three pins there hold
`@tailwindcss/oxide` and its `win32-x64-msvc` native binary at 4.0.0; left under `pnpm` they would
have silently detached, and the failure would have surfaced as a missing native binding at build
time rather than as a warning. Moved, and now asserted by test -- mutation-proved by renaming the
key back, which turns the suite red.

### Measured, on the Bun toolchain

- `bun install --frozen-lockfile` with no `pnpm-lock.yaml` present: clean, 340 installs.
- `bun run build`: 11 routes, `○ /` at 142 B / 106 kB first-load JS, 2 m 27 s.
- `docker compose build web` (oven/bun stage → Node runtime stage): built; the container answered
  `GET / -> 307 /dashboard -> 200` and reported `Ready in 2.4s`, with no bun binary in the image.
  The first attempt failed inside `bun run build` with webpack errors while another session was
  editing `src/**`; the retry on settled sources succeeded, which is the honest sequence.
- `bun run test:unit --run`: 51 passed over 11 files, and the process printed Node's own DEP0205
  and Vite's CJS Node API deprecation -- evidence the suite ran on Node, not on Bun's runtime.
- `bun run lint`: 35 errors, 1 warning over 91 files. `biome check .` invoked directly, bypassing
  Bun, reports the identical 35 + 1. So the lint gate's red is pre-existing source lint in the
  working tree's 70 uncommitted files, not a consequence of this swap.

### The switch is byte-neutral, and that is checkable rather than asserted

The question a package-manager swap has to answer is whether the tree changed. Diffing `bun.lock`
against the `pnpm-lock.yaml` it replaced (taken from git, not from memory), matched on
`name@version` with the registry's own integrity hash:

- **438 resolved packages on each side; the two sets are identical** -- nothing only in one.
- **438 of 438 sha512 integrity hashes match**, so every tarball is the same bytes, not merely the
  same version string.
- Zero per-name version drift, and every one of the 40 declared dependencies and devDependencies
  resolves to exactly its pin -- which is what makes the `overrides` move (Bun ignores
  `pnpm.overrides`) verifiable rather than assumed: `@tailwindcss/oxide`,
  `@tailwindcss/oxide-win32-x64-msvc` and `@tailwindcss/node` are all locked at 4.0.0, and
  `bun.lock` records the `overrides` block itself.
- Of the packages declaring a lifecycle script -- only three, `esbuild`, `sharp`,
  `@biomejs/biome` -- none needed one: esbuild transforms, sharp encodes a PNG, biome reports
  1.9.4, and the fresh `bun run build` emitted 41,762 bytes of CSS containing `--tw-` custom
  properties, which only the oxide native binding can produce.
- `bun run typecheck` clean; `bun run test:unit --run` 51/51; the Playwright state suite
  **23 passed, 3 skipped, 0 failed** against a fixture build served by `next start` on Node (the
  3 skips need the live API origin, and the Docker daemon was down at that moment).
- `bun audit --json` on the same tree: 53 unique advisories by GHSA (6 critical, 20 high, 23
  moderate, 4 low), 34 of them on `next@15.1.3`. Identical pin, identical hash in the pnpm lock, so
  none of it is a migration artifact -- it is the finding the unrunnable gate was hiding, and it is
  written up in `BACKLOG.md`.


Digest pins resolved with `docker buildx imagetools inspect` on 2026-09-27:
`oven/bun:1.4.2-debian@sha256:4f6e31d1a54d6a3dd312daef655fc998101b5043d52e12592ac293ef04b9bc73`.

### Status of this entry: applied, as an approved plan amendment

A second session working in this repository reverted the Bun changes twice while this entry was
being written, and wrote its objection into `scripts/verify.py` at the P8 gate: plan §T2 pins
`packageManager: "pnpm@9.15.9"` with a committed `pnpm-lock.yaml`, §13 puts `pnpm audit` in CI, and
`test_pnpm_lockfile_is_committed` enforces the artifact -- so swapping the toolchain is a plan
amendment the owner accepts, not a convenience a task discovers mid-flight.

**That objection was right as process, and it is the reason this entry exists.** The owner ruled on
2026-09-27 that §T2's pin is superseded, so the amendment is applied: the P8 gate, `.env.example`,
`README.md`, the README generator in `oxbow/eval.py`, `tests/unit/test_p0_toolchain.py` and
`test_p9_demo_seed.py`'s narrative moved with `package.json`, the Dockerfile, the Makefile and the
pre-commit hooks.

The ruling is recorded here verbatim because the session that reverted this disputed that it
happened -- `70b745d` says "No such approval was given or asked for", and from inside that session
that was probably true: it never saw the exchange. The ask was made in the session writing this
entry, as a direct choice between "the plan pin wins" and "Bun wins", and the answer was to approve
the amendment and re-apply. When the same question came up again after `70b745d` had undone it, the
owner's reply was **"use bun as package manager"**. Two rulings, both in the owner's words, both
recorded. A later reader with only one session's view should weigh that rather than trust either
commit message -- including this one.

What the disagreement left behind is worth keeping, so that session's contribution is recorded
rather than quietly overwritten. It wrote `test_the_declared_js_toolchain_is_the_one_every_recipe_uses`,
which asserts that `packageManager`, `apps/web/Dockerfile`, the `Makefile` web recipes and
`.pre-commit-config.yaml` all name the *same* package manager, because a manifest the Dockerfile
contradicts is drift wearing a clean badge. That shape is toolchain-agnostic and correct, so it
stayed and only its subject moved to Bun. Mutation-proved in both directions: flipping `make
lint-web` back to `pnpm lint` turns it red. Its first offender scan matched the word "pnpm" inside a
prose comment and failed on a file that was doing nothing wrong, which is why the scan now skips
comment lines -- a guard that fires on a healthy file is the same trap as a guard that cannot fire.

`apps/web/node_modules` was reinstalled from `bun.lock` so the tree on disk and the committed
lockfile agree. Mid-whipsaw it was possible to be measuring a Bun hoisted tree under a pnpm
manifest, which is the state no gate should ever be in.

## DEV-023 — the same-origin seam moves from the Next rewrite to a Caddy edge

**Authority:** plan §13's OIDC cookie session (which only works if the browser sees one origin)
and 02 §F's supply-chain pinning, against 02's own integration diagram, which never named an edge
proxy at all -- `next.config.ts`'s `rewrites()` has been doing this job since P8.

### Why the rewrite was the wrong place for the promise

`transport.ts` sends relative `/api` paths from the page because a cross-origin POST cannot ride a
simple request: Chrome blocks it on the preflight, no demo token is minted, every later GET answers
401, and the envelope decoder reports that as a contract failure rather than as wiring. The rewrite
keeps that promise in three bad places: it is applied after Node has loaded the app, it has no
per-route way to leave the run-progress SSE unbuffered, and it does not exist for anything that
does not enter through `next start`. It stays in `next.config.ts` for a bare `make web` with no
edge in front -- removing it would break a path that works today.

### What was rejected, and why

- **`tls internal` on 443.** Ports 80 and 443 are measurably free on this host, and real TLS at
  the edge is where OIDC redirect URIs eventually go. Rejected for one reason: it requires
  installing and trusting Caddy's local root CA on every machine that runs the demo, before a
  browser will load the page at all. Spec §12.9 makes `docker compose up` the reproducibility
  claim, so the edge binds `{$OXBOW_EDGE_PORT:8080}` and nothing privileged.
- **Routing `/healthz`, `/docs` and `/openapi.json` through the edge too.** The browser needs
  `/api/*` and nothing else; three more matchers are three more facts to keep in agreement.
- **Naming a host in the site block** (`localhost:8080 { ... }`). Caddy then matches on Host and
  answers any other Host with its no-site page, which looks exactly like an app failure. Bound to
  the port instead.

### The defect this found in the file it was already touching

The `web` service set `OXBOW_API_ORIGIN` and `NEXT_PUBLIC_API_BASE_URL` to
`http://127.0.0.1:8000` and `http://localhost:8000`. Inside that container, loopback is that
container. Measured directly:

```
docker exec oxbow-web-1 node -e "fetch('http://api:8000/healthz')..."     -> 200 degraded
docker exec oxbow-web-1 node -e "fetch('http://127.0.0.1:8000/healthz')"  -> FAIL ECONNREFUSED
```

Both origins now name the `api` service, in compose and in the image's baked default, so a
server-rendered call has something to reach. This was true before the edge existed and is recorded
here rather than as a separate entry because it is the same seam.

### No documented command could start these services

`api`, `worker` and `web` are behind `profiles: ["full"]`, and nothing in the repository --
Makefile, README, docs, tests -- referenced `--profile full` or `COMPOSE_PROFILES`. The services
were unbootable by any named command, which matters more now that the edge depends on two of them.
Added `make up-full`.

### Measured through the edge, 2026-09-27

`COMPOSE_PROFILES=full API_PORT=8011 docker compose up -d --no-build web caddy` (8011 because
Windows PID 4 holds 0.0.0.0:8000, and the other session's `oxbow-api-verify` is published there).

- `caddy` service health: **healthy**, via the admin endpoint on 2019 -- which is the point of that
  probe: it answers once a config has loaded, without borrowing an upstream's health.
- `GET :8080/` -> `307 -> /dashboard` -> `200`. `GET :8080/cases/<id>` and `/dev/states` -> 200.
- `GET :8080/api/runs` unauthenticated -> `401 application/problem+json`; with a token minted
  through the edge (`POST /api/auth/demo-token`, 295-byte JWT) -> `200 application/json`.
- `GET :8080/api/nope` -> `400 problem+json` from the API, which is what an intact `/api` prefix
  looks like. A `handle_path` would have delivered `/nope`.
- The healthcheck asymmetry was demonstrated by accident: while `api` was still starting,
  `GET :8080/api/runs` returned `502` from Caddy while `caddy` itself stayed healthy.

### Not proven, and what would force it

`flush_interval -1` is set on the `/api` route and asserted by `test_the_api_route_flushes_every_frame`,
but **per-frame flushing through the edge is not measured**. Streaming
`GET /api/runs/01M3FGE812VJZJNKXX64F9CJQ5/events/stream` returned 4 frames in a single 830-byte
read with one arrival timestamp -- and the *same* shape when fetched directly from the API on 8011.
A finished run's stored events leave the source in one burst, so no observation at the edge can
distinguish proxy buffering from source burstiness. What would force it: a run in flight, streaming
live stage events, sampled with per-frame arrival times through `:8080` and to the API directly.
Caddy does auto-detect `text/event-stream` independent of this directive, which is why the explicit
setting and its test exist -- but that is config, not measurement.

## DEV-024 — the 40k slice cannot calibrate, and that is arithmetic rather than a defect. **Open; needs the owner.**

**Status as of this commit: unverified finding, deliberately not decided.** The score stage now
trains all five folds, and every one of them reports `calibrated=False`. The reason is countable:
the run measured **108 positives across 79,998 account-instant rows** (base rate 0.001350) in the
40,000-event slice, and the floors in `config/model.yaml` are
`min_positives_for_calibration: 50` (:141) and `isotonic_min_positives: 500` (:133), applied to
the *validation* split of each expanding window. Below the floor the calibration branch refuses
and the row says *probabilities are uncalibrated* and carries the positive count and the floor —
which is the documented behaviour (`oxbow/models/calibration.py:9-15`), not a bug.

The consequence is a submission fact, not a modelling nicety: `MODEL_CARD.md` cannot show a fitted
reliability curve, a Brier score against calibrated probabilities, or a calibration-in-the-large
number from this slice, and `ECONOMICS_CARD.md` prices the queue on `p_scorecard`-ranked alerts
whose probabilities are explicitly labelled uncalibrated. Both cards will say so. The plan §16
definition of done asks for measured numbers with their provenance, and an uncalibrated fold
carries provenance `measured` — so shipping it is not the fabrication the plan's rejection
triggers name. Shipping it *described as calibrated* would be.

At the measured base rate the arithmetic is: Platt needs roughly **110k events**, isotonic roughly
**1.1M**. The 40k slice took about two hours on this host with nothing else running; a 110k run is
plausibly three to four hours of wall time and more memory than the machine has been holding, and
the 1.1M run is not a same-day option.

The two honest positions, for the owner to choose between:

1. **Ship the 40k slice with calibration declared refused.** Every number is real, the cards name
   the refusal and the count behind it, and `LIMITATIONS.md` gains the thin-positive-count row it
   half-already has. Costs nothing but a sentence on the model card.
2. **Spend a ~110k run before 2026-10-01** to clear the Platt floor, so the card shows a fitted
   curve. Costs most of one working day, with the risk that the run does not finish and the
   numbers that *would* have shipped are the ones in flight when the deadline arrives.

Not offered as an option: lowering `min_positives_for_calibration` to make a fold report
calibrated. That is the same sin as widening a leakage guard to get past it — the guard exists
because isotonic on a thin positive count steps on noise and reports a confident 0.0 or 1.0, and
the money layer multiplies whatever it is given.

---

## DEV-025 — folds that degrade do not carry the same columns, and the artifact refused to hold them. **Fixed; the alignment is on the live path.**

A run that scored all five folds died on its last step:

    ComputeError: schema names differ: got p_gbm, expected p_fused_raw

`cli.py` stacked the per-fold frames with `pl.concat(..., how="vertical_relaxed")`, which aligns
**by position** and rejects a name mismatch. A fold that degraded to `scorecard_and_rules_only`
carries no `p_gbm` and no `anomaly_norm`; a fold that ran the full stack carries no `drift_banner`
or `drift_score_psi`. Each mode drops two columns and adds two others, so the frames are the same
*width* with different *names* — the one shape positional alignment cannot absorb. Every model had
been fitted and not one number reached disk, which is the worst place in the pipeline for a guard
to fire.

**The decision: align on names at the persist boundary, and keep the contract check on the union.**
`stack_scored_frames` concatenates `how="diagonal_relaxed"` and then refuses if any
`SCORED_ROW_COLUMNS` entry is missing from the result. A channel one fold lacked is a null on that
fold's rows, which is what `scoring_mode` already says; a column NO fold produced is a build
defect, and the all-degraded run is the case that proves the distinction matters — its `p_gbm`
would otherwise ship as a column of nothing but nulls that the read model would present as a
channel that exists and is empty.

Alternatives considered and rejected:

- **Make every fold carry every column by writing explicit nulls in each producer.** It keeps
  `vertical_relaxed` strict, but it spreads the schema across four call sites and the next
  optional channel has to be remembered in all of them. The contract is already declared in one
  place; the check belongs next to it.
- **Suppress the degraded folds so all frames agree.** That throws away the two folds that carry
  the refusal evidence, which is the opposite of what `record_refusals_in_artifact` exists for.
- **`diagonal` without the contract check.** A typo in a column name would then become a second
  all-null column instead of a failure — the silent widening this file keeps refusing.

The first fix went into `write_run_artifacts`, which is exported, documented, tested and **has no
production caller** — the score stage lands its own frames. It was green in its own test file and
would still have crashed the run. The gate is now a source assertion on `_score_models_and_land`
itself, mutation-proved by putting the positional concat back.

