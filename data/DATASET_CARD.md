# Dataset card

Provenance, licence obligations, base rates and label semantics for the two corpora
OXBOW ingests. This is the source that `/api/meta/dataset` serves, and the rule it
enforces is that **no number in the product may be vaguer than the number here**.

Every figure below was produced by a command on this machine, listed in §7. Nothing is
copied from the dataset description. Each figure carries the artifact or config entry
that owns it, tagged **measured** (read out of bytes), **declared in config/** (a value
this repository chose and pins), or **recorded in DECISIONS.md** (a measurement whose
only home is a decision entry — named as such wherever it appears).

**`make eval` verifies this file. It does not write it.** The generator that used to
render it is gone, because a document carrying judgement — what a label means, which
licence binds a derivative, what was refused and why, which corpus owns which module —
cannot be derived from artifacts, and every run overwrote the parts it could not
derive. `oxbow eval` now resolves each measured and config-declared figure against the
pointer beside it and fails loudly, naming the field and both values, when they drift.
A check whose artifact is not on this host is reported as SKIPPED with the reason and
the path named; it is never counted as a pass.

> OXBOW is a research prototype that analyzes historical, de-identified data only. It does not process live financial transactions, does not trade or advise on any financial instrument, does not make real financial decisions, and is not financial advice. Monetary figures are model estimates derived from stated assumptions, not measured outcomes. Results are not validated for operational use by any financial institution.

---

## 1. PaySim1 — primary for Module A (tabular risk, volume)

| Field | Value | Source · provenance |
| --- | --- | --- |
| Source | `https://www.kaggle.com/datasets/ealaxi/paysim1` | `config/sources.yaml#/sources/0/source_url` · declared in config/ |
| Retrieval | `scripts/download_data.py` — Kaggle REST over `httpx`, no `kaggle` dependency, per 01 §A rule 6 | `config/sources.yaml#/sources/0/retrieval` · declared in config/ |
| Retrieved | 2026-09-26T04:11:13Z | `data/download_manifest.json#/paysim/files/0/retrieved_at` · measured |
| Archive | `paysim.zip`, 186,385,561 bytes, SHA-256 `f7eef9ffad5cfa64a034143a5c9b30491d189420b273d5ad5723ca40b596613d` | `data/download_manifest.json#/paysim/archive` · measured |
| File read | `data/raw/paysim/PS_20174392719_1491204439457_log.csv` | `data/graph_measurement.json#/file` · measured |
| File bytes | 493,534,783 | `data/download_manifest.json#/paysim/files/0/bytes`, `os.stat` on that path · measured |
| SHA-256 | `16910f90577b0d981bf8ff289714510bb89bc71bff7d3f220f024e287e4eea6b` | `config/sources.yaml#/sources/0/files/0/sha256` · measured, verified on every `make data` |
| Rows | 6,362,620 | `data/graph_measurement.json#/n_rows`, re-counted from the file on this host · measured |
| Distinct `nameOrig` | 6,353,307 | `data/graph_measurement.json#/n_distinct_nameOrig` · measured |
| Distinct `nameDest` | 2,722,362 | `data/graph_measurement.json#/n_distinct_nameDest` · measured |
| Distinct accounts, either side | 9,073,900 | `data/graph_measurement.json#/counterparty_degree/accounts` · measured |
| **Sender reuse ratio** | **0.001464** — `1 − 6,353,307 / 6,362,620` | `data/graph_measurement.json#/reuse_ratio` · measured, and recomputed from the two counts |
| `isFraud` | 8,213 rows = **0.1291 %** | `data/raw/paysim/PS_20174392719_1491204439457_log.csv`, summed on this host · measured |
| `isFlaggedFraud` | **16 rows in 6,362,620** | the same file, summed on this host · measured |
| `step` range | 1–743 | the same file, min/max on this host · measured |
| Synthetic timeline | each `step` → 24 h from `2014-01-01T00:00:00Z` | `config/pipeline.yaml#/paysim/step_hours`, `#/paysim/epoch_utc` · declared in config/ |
| Currency | EUR, minor units (Int64) | `packages/pipeline/oxbow/ingest/canonical.py :: PAYSIM_CURRENCY` · declared in code |
| Median total degree | 1.0 | `data/graph_measurement.json#/degree_total/median` · measured |
| Median counterparty degree | 1.0, against the plan §4 condition **> 2** | `data/graph_measurement.json#/counterparty_degree/median`, `#/thresholds/median_counterparty_degree_gt` · measured |
| Degree p90 / p99 / max | 1 / 14 / 113 | `data/graph_measurement.json#/counterparty_degree` · measured |
| Highest-degree `nameOrig` | 3 edges | `data/graph_measurement.json#/top20_senders` · measured |
| Time-respecting 3–6 cycles | 0, against the condition **≥ 10**, on a 20,000-row sample | `data/graph_measurement.json#/cycles` · measured |
| Verdict | `STAR_SHAPED_TRIGGER_DAY4_FALLBACK` | `data/graph_measurement.json#/verdict` · measured |
| Licence | **CC BY-SA 4.0** | `config/sources.yaml#/sources/0/license` · declared in config/ |
| Citation | E. A. Lopez-Rojas, A. Elmir, S. Axelsson, "PaySim: A financial mobile money simulator for fraud detection," 28th European Modeling and Simulation Symposium (EMSS), Larnaca, Cyprus, 2016 | `config/sources.yaml#/sources/0/citation` · declared in config/ |

**Licence obligation.** Share-alike: any derivative dataset we publish — the canonical
event table, feature extracts, any released sample — inherits CC BY-SA 4.0 with
attribution (`config/sources.yaml#/sources/0/license_obligation`). The repository is not
the publication boundary; a published sample is.

**What `isFraud` means, stated before a judge finds it.** It covers one narrow
simulator behaviour: an agent takes over an account and drains it via TRANSFER then
CASH-OUT. `isFlaggedFraud` is a crude threshold, not ground truth, and at the count in
the table it is not a learnable target at any depth — it is carried, never trained on
(`config/sources.yaml#/sources/0/label_caveat`). Scripted in the UI: *"PaySim gives
realistic mobile-money topology and volume; IBM-AML gives multi-account laundering
typologies. We report metrics per corpus and never average them into one headline
number."*

**Synthetic fields and the timestamp rule.** `step` is a counter, not an instant, and
all five balance columns are simulator output known to be internally inconsistent with
`amount`. A `step` becomes `epoch + step_hours × step`, plus a **deterministic
intra-step microsecond offset derived from a hash of the transaction id** — without it
every transaction in a step shares a timestamp and intra-step ordering becomes
arbitrary, which a walk-forward backtest would silently inherit. The offset is
reproducible across runs (`test_step_expansion_stable`); the synthetic-ness is stated
here rather than left implicit.

**One place the corpus description and the bytes disagree, unresolved.** PaySim's own
documentation describes 30 days of traffic, and this repository's config expands each
`step` as a **day**. The bytes carry `step` up to the maximum in the table, so on the
configured mapping the synthetic timeline spans roughly two years, not one month. The
mapping is declared in config and the range is measured; neither has been reconciled
against the other, and no measurement in this repository depends on the difference
because every PaySim figure here is a count over rows, not over elapsed time. Recorded
as an open discrepancy rather than quietly re-labelled.

Balance inconsistency is **a feature, not an error to repair**: reconciling it would
be the silent coercion the strict contract exists to prevent, and the delta
(`balance_delta_*`) is itself signal.

**Why PaySim is not the network corpus (DEV-011).** The degrees in the table are the
finding: median counterparty degree against a condition of **> 2**, a highest-degree
originator with single-digit edges in millions of rows, and zero surviving
time-respecting cycles on the sample the enumerator searched. On the fraud subset alone
it is worse, not better: the sender reuse ratio is exactly **0.0** and the maximum
total degree is **2**, because every fraudulent transaction has a unique originator —
both re-measured from the file on this host, and both recorded in `DECISIONS.md`
DEV-011. Verdict: `STAR_SHAPED_TRIGGER_DAY4_FALLBACK`.

## 2. IBM Transactions for AML — primary for Module B (network typologies)

| Field | Value | Source · provenance |
| --- | --- | --- |
| Source | `https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml` | `config/sources.yaml#/sources/1/source_url` · declared in config/ |
| Size of what the slug serves | 8,176,169,418 bytes as reported by Kaggle (`datasets/list`) | `DECISIONS.md` DEV-013 · recorded in DECISIONS.md, no artifact holds it |
| Acquired | the **HI-Small** scenario bundle only, ~510 MB, of six bundles (HI/LI × Small/Medium/Large) | `config/sources.yaml#/sources/1/acquisition_scope` · declared in config/, sizes measured per DEV-013 and DEV-016 |
| `HI-Small_Trans.csv` | 475,664,283 bytes, SHA-256 `b19d39f515523373f991b689c07e11e7b0b95c17a2c27a87d91584ae16c5b040` | `config/sources.yaml#/sources/1/files/0`, `os.stat` on `data/raw/ibmaml/HI-Small_Trans.csv` · measured |
| `HI-Small_accounts.csv` | 34,053,187 bytes, SHA-256 `786808526e33cfc441212dd6fccda7edfc24172149bed59c6ef59b186836b014` | `config/sources.yaml#/sources/1/files/1`, `os.stat` on that path · measured |
| `HI-Small_Patterns.txt` | 323,844 bytes, SHA-256 `2c546b5ce6009e73851f0139af053cf845f08bf92f3bc82fe1eb937dec2ef39b` | `config/sources.yaml#/sources/1/files/2`, re-hashed from the bytes by `make eval` · measured |
| Rows | 5,078,345 | `data/ibm_graph_measurement.json#/n_transaction_rows` · measured |
| Distinct accounts | 515,080 | `data/ibm_graph_measurement.json#/n_distinct_accounts` · measured |
| Directed edges, self-loops set aside | 647,939 | `data/ibm_graph_measurement.json#/n_directed_edges_excl_self_loops` · measured |
| **Median account degree** | **10.0** (6.0 with self-loops counted — excluded deliberately, see below) | `data/ibm_graph_measurement.json#/degree_median` · measured; the self-loop-inclusive median is recorded in DECISIONS.md DEV-013 |
| Degree p90 / p99 / max | 53 / 116 / 169,756 | `data/ibm_graph_measurement.json#/degree_p90`, `#/degree_p99`, `#/degree_max` · measured |
| Median counterparties per account | 2.0 | `data/ibm_graph_measurement.json#/median_counterparties_per_account` · measured |
| Accounts with > 2 counterparties | 173,735 | `data/ibm_graph_measurement.json#/accounts_with_more_than_2_counterparties` · measured |
| Self-edge rows | 591,212 = **11.6 %** | `data/ibm_graph_measurement.json#/n_self_loop_rows` · measured |
| `Is Laundering` | 5,177 rows = **0.1019 %** | `data/ibm_graph_measurement.json#/laundering_rows`, `#/laundering_rate_pct` · measured |
| Temporal range | 2022-09-01 00:00 → 2022-09-18 16:18 | `data/ibm_graph_measurement.json#/temporal_range` · measured |
| Span | 17.68 days end to end | `data/ibm_graph_measurement.json#/temporal_range` · measured, derived |
| Currencies | 15, by name rather than ISO-4217 code: US Dollar, Euro, Swiss Franc, Yuan, Shekel, Rupee, UK Pound, Ruble, Yen, Bitcoin, Canadian Dollar, Australian Dollar, Mexican Peso, Saudi Riyal, Brazil Real | `data/ibm_graph_measurement.json#/n_currencies`, `#/currencies` · measured |
| Payment formats | Cheque 1,864,331 · Credit Card 1,323,324 · ACH 600,797 · Cash 490,891 · Reinvestment 481,056 · Wire 171,855 · Bitcoin 146,091 | `data/ibm_graph_measurement.json#/payment_formats` · measured |
| Measured | 2026-09-26 by `scripts/measure_ibm_graph.py` | `data/ibm_graph_measurement.json#/measured_at_utc` · measured |
| Licence | **CDLA-Sharing-1.0** (the repository code is Apache-2.0; the data is not) | `config/sources.yaml#/sources/1/license` · declared in config/ |
| Citation | ealtman2019, *IBM Transactions for Anti-Money Laundering*, Kaggle | `config/sources.yaml#/sources/1/citation` · declared in config/ |

**Licence obligation.** Share-alike on derived data: the canonical event table, the
feature tables and any published sample inherit CDLA-Sharing-1.0
(`config/sources.yaml#/sources/1/license_obligation`).

**Acquisition scope is part of every number above.** Six bundles exist and one was
taken, so each Module B figure describes HI-Small alone. Quoting them as corpus-wide
would overstate the evidence behind the network claim (DEV-016).

**Header hazard (found in the bytes, not the docs).** The header repeats the column
name `Account` for sender and receiver:
`Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,Amount Paid,Payment Currency,Payment Format,Is Laundering`.
A name-keyed reader silently loses one of them, so the contract reads positionally
(`data/ibm_graph_measurement.json#/notes`).

**Two timestamps, two formats.** The transaction file writes `/` separators, the
pattern file writes `-`. A text join across the two matches nothing; both sides are
normalised to `YYYY-MM-DD HH:MM` before any comparison. The corpus declares no timezone
and has **minute precision**, so intra-minute ordering is derived deterministically
rather than trusted from file order, and `local_hour` is computed once at ingest from
the deployment timezone (Africa/Kampala, `config/pipeline.yaml#/deployment_timezone`)
under an explicitly recorded assumption.

**Self-loops are kept and excluded from connectivity.** A tenth of the rows move value
between two accounts that are the same account, concentrated in `Reinvestment`. They are
ingested (rejecting them would fail the run on a sixth of the corpus), they are a
feature (`self_transfer_count`), and they are excluded from degree, edges, counterparty
counts and cycle detection — because a self-loop gives an account *degree* without
giving it a *counterparty*, and counting them inflates the very statistic the network
thesis rests on (plan §7). Hence the reported median degree rather than the
self-loop-inclusive one.

**Labels: the binary flag is not the typology.** `Is Laundering` is 0/1 and is the
corpus's only label column. Typologies come from the pattern file's annotations
(§2a), and the laundering-flagged rows are **near-degree-1 among themselves**, so a
model trained on the flag alone learns account-level oddity, not network shape — a
DEV-013 footnote, recorded in DECISIONS.md and not re-measured by any artifact here.
The `RANDOM` blocks are planted laundering-attempt traffic with **no** typology — a
free negative control for any rule that would otherwise fire on everything. Labelled
typologies are curated research annotations, not prosecuted cases.

### 2a. Typology ground truth

`data/processed/ibm_typologies.parquet`, built by `scripts/build_ibm_typologies.py`
from `HI-Small_Patterns.txt`.

| Field | Value | Source · provenance |
| --- | --- | --- |
| Annotated transactions | 3,209 | `data/processed/ibm_typologies.parquet#rows` · measured |
| Labelled attempt blocks | 370 | `data/processed/ibm_typologies.parquet#attempt_id(n_unique)` · measured |
| Typologies | 8 | `data/processed/ibm_typologies.parquet#typology(n_unique)` · measured |
| Per typology | GATHER-SCATTER 716 · SCATTER-GATHER 626 · STACK 466 · FAN-OUT 342 · FAN-IN 318 · CYCLE 287 · BIPARTITE 263 · RANDOM 191 | `data/processed/ibm_typologies.parquet#typology(value_counts)` · measured |
| CYCLE rows | 287 | `data/processed/ibm_typologies.parquet#typology=CYCLE` · measured |
| Fan rows, FAN-OUT + FAN-IN | 660 | `data/processed/ibm_typologies.parquet#typology=FAN-*` · measured, summed |
| Negative control, `RANDOM` | 191 | `data/processed/ibm_typologies.parquet#typology=RANDOM` · measured |

The builder walks `HI-Small_Patterns.txt`, joins each annotated row to its transaction
by the ten non-label columns FIFO per key, and fails closed when an annotation matches
nothing. The label column is deliberately excluded from the join key because the
pattern file repeats it.

**R4's definition vs. this corpus (DEV-015).** `data/ibm_cycle_measurement.json`,
`#/labelled_cycle_anatomy`, all measured over the labelled CYCLE blocks.

| Field | Value | Source · provenance |
| --- | --- | --- |
| Labelled CYCLE blocks | 54 | `data/ibm_cycle_measurement.json#/labelled_cycle_anatomy/labelled_cycle_blocks` · measured |
| Close as directed loops | 54 | `…#/labelled_cycle_anatomy/blocks_that_close` · measured |
| Cross-currency | 38 | `…#/labelled_cycle_anatomy/blocks_cross_currency` · measured |
| Below the 0.6 value-retention floor | 25 | `…#/labelled_cycle_anatomy/blocks_below_0_6_retention` · measured |
| Two-leg round-trips | 14 | `…#/labelled_cycle_anatomy/blocks_with_only_two_legs` · measured |
| Not time-monotonic | 5 | `…#/labelled_cycle_anatomy/blocks_with_non_monotonic_timestamps` · measured |
| Worst twelve retention ratios | 0.0015 – 0.0119 | `…#/labelled_cycle_anatomy/retention_ratios` · measured, first and twelfth |

A cycle rule restricted to one currency, requiring non-increasing legs and ≥ 0.6
retention **cannot fire on the majority of the cycles this corpus itself labels as
cycles**. This is stated rather than fixed by quietly loosening a default; see DEV-015.

## 3. Cited, never ingested

* **Elliptic Bitcoin Dataset** — CC BY-NC-**ND** 4.0 (`config/sources.yaml#/sources/2`).
  No derivatives, so even a derived sample would violate the licence. Cited in the
  README only; `ingest_allowed: false` in `config/sources.yaml`, which the reader
  enforces.
* **IEEE-CIS Fraud Detection** — competition-governed, redistribution terms unclear
  (`config/sources.yaml#/refused/0`). Never used, in any form.

Both are refusals, not preferences. A non-goal here is a hard "no", which is why it is
a config flag the ingest path checks rather than a comment.

## 4. Derived data, de-identification and the licensing consequence

* Both corpora carry share-alike on derived data (CC BY-SA 4.0 and CDLA-Sharing-1.0).
  That covers the canonical event table, the feature tables and any published sample,
  so the repository's Apache-2.0 code licence does not release the data terms. The
  stricter terms govern, and they are stated here rather than discovered later.
* Every account identifier downstream of ingest is `sha256(account_id + RUN_SALT)`,
  truncated to the configured prefix and rendered as `ACC-<HEX>`
  (`config/sources.yaml#/deidentification/account_key`). The salt lives in the
  environment, never in the repository; logs, traces and packets are all redacted.
* Erasure destroys the salt mapping and leaves the audit chain intact and verifiable:
  the subject becomes unrecoverable while the decision history stays provable.
* No live, real or re-identifiable financial data is processed, and none can be: ingest
  refuses any source not declared in `config/sources.yaml`.

## 5. Sampling and split boundaries for the interactive product

Disk and runtime are bounded, so the interactive artifacts are built on a **connected
subcorpus** while full-corpus metrics are computed offline.

| Field | Value | Source · provenance |
| --- | --- | --- |
| Interactive target | 500,000 transactions | `config/pipeline.yaml#/sampling/interactive_txn_target` · declared in config/ |
| Sampling strategy | `connected_subcorpus` | `config/pipeline.yaml#/sampling/strategy` · declared in config/ |
| Seed | 1,337, propagated to every stochastic stage | `config/pipeline.yaml#/seed` · declared in config/ |
| Folds | 5, expanding window, `shuffle: false` | `config/splits.yaml#/walk_forward/n_folds`, `#/scheme`, `#/shuffle` · declared in config/ |
| Embargo / purge | 30 days / 1 day | `config/splits.yaml#/walk_forward/embargo_days`, `#/purge_days` · declared in config/ |
| Split boundaries, `train_end` | 0.30 · 0.45 · 0.60 · 0.75 · 0.90 | `config/splits.yaml#/walk_forward/folds` · declared in config/ |
| Split boundaries, `test_end` | 0.45 · 0.60 · 0.75 · 0.90 · 1.00 | `config/splits.yaml#/walk_forward/folds` · declared in config/ |
| Validation slice | 0.15 of each training period, after the embargo | `config/splits.yaml#/validation/fraction_of_train` · declared in config/ |
| Entity-disjoint control | `account_holdout`, holdout fraction 0.20, reported as a robustness check | `config/splits.yaml#/entity_disjoint/method`, `#/holdout_fraction`, `#/report_as` · declared in config/ |

**Connected, and it must stay that way.** Selection is by whole component
(keep an account's edges only if both endpoints survive, or seed from a component and
take its closure). Per-row or per-account-hash sampling **severs the loops it is then
asked to count** — a four-node cycle survives independent 19 % node sampling about
0.13 % of the time, which is a measurement of the sampler, not of the corpus. That
error was made and caught here; DEV-015's zero-cycle first result was the artifact of
an account-hash sample, and the number only meant anything once selection was
component-intact.

The raw zips are not committed; artefacts are content-hashed and `data/interim/` is
pruned between runs.

## 6. Artifact digests behind this document

Recorded by the `make eval` run whose output is `data/processed/eval.json`; the live
digests are printed there on every run. **This table is deliberately not gated**,
because re-running a measurement script moves an artifact's digest — the recorded
timestamp is inside it — without moving any figure this card states. The gated figures
above resolve against those same artifacts, which is the check that actually catches a
stale claim. A stale document is still detectable: run `make eval` and diff these
digests against `data/processed/eval.json`.

| artifact | stage | state | bytes | sha256 |
| --- | --- | --- | --- | --- |
| `data/graph_measurement.json` | P1a | present | 2,192 | `8055beaff887d58e…` |
| `data/ibm_graph_measurement.json` | P1a | present | 2,575 | `829bde0c47214f18…` |
| `data/ibm_cycle_measurement.json` | P3a | present | 4,661 | `6ddb671dfca36634…` |
| `data/processed/ibm_typologies.parquet` | P1b | present | 9,357 | `faf682effbf58107…` |
| `data/download_manifest.json` | P0 | present | 841 | `a7368658cfcff07e…` |
| `out/backtest/model_card.json` | P6 | present | 15,728 | `2cd06143d76f7445…` |
| `out/backtest/ablation_results.json` | P6 | present | 273,053 | `69ca1437570e77ee…` |
| `out/p4/scored_rows.parquet` | P4b | **absent** | - files | `absent` |
| `out/warehouse/drift_period` | P4b | **absent** | - files | `absent` |
| `out/warehouse/curve_point` | P5 | **absent** | - files | `absent` |
| `out/audit/audit.jsonl` | P7 | **absent** | - files | `absent` |
| `out/warehouse/runs.jsonl` | P7 | **absent** | - files | `absent` |

## 7. Where each number came from

```
uv run python scripts/download_data.py --source paysim --verify   # hashes, row counts
uv run python scripts/download_data.py --verify-only              # the three IBM digests
uv run python scripts/measure_graph.py                            # data/graph_measurement.json
uv run python scripts/measure_ibm_graph.py                        # data/ibm_graph_measurement.json
uv run python scripts/build_ibm_typologies.py                     # data/processed/ibm_typologies.parquet
uv run python scripts/measure_ibm_cycles.py                       # data/ibm_cycle_measurement.json
uv run oxbow eval                                                 # verifies this card, writes eval.json
```

The `data/*.json` artifacts are committed; the card restates their contents in prose,
and `oxbow eval` checks that the restatement did not drift. If a figure here and a
figure in an artifact disagree, the artifact is right and this file is a bug.
