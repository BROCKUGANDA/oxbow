# Dataset card

Provenance, licence obligations, base rates and label semantics for the two corpora
OXBOW ingests. This is the source that `/api/meta/dataset` serves, and the rule it
enforces is that **no number in the product may be vaguer than the number here**.

Every figure below was produced by a command on this machine, listed at the bottom.
Nothing is copied from the dataset description.

---

## 1. PaySim1 — primary for Module A (tabular risk, volume)

| Field | Value |
| --- | --- |
| Source | `https://www.kaggle.com/datasets/ealaxi/paysim1` |
| Retrieval | `uv run python scripts/download_data.py --source paysim` (Kaggle REST over `httpx`; no `kaggle` dependency, per 01 §A rule 6) |
| Retrieved | 2026-09-26T04:11:13Z |
| Archive | `paysim.zip`, 186,385,561 bytes, SHA-256 `f7eef9ffad5cfa64a034143a5c9b30491d189420b273d5ad5723ca40b596613d` |
| File read | `data/raw/paysim/PS_20174392719_1491204439457_log.csv` |
| File bytes | 493,534,783 |
| SHA-256 | `16910f90577b0d981bf8ff289714510bb89bc71bff7d3f220f024e287e4eea6b` (verified on every `make data`) |
| Rows | 6,362,620 |
| Temporal range | `step` 0–30 = 30 synthetic days from the configured epoch `2014-01-01T00:00:00Z` |
| Distinct `nameOrig` | 6,353,307 |
| Distinct `nameDest` | 2,722,362 |
| **Sender reuse ratio** | **0.001464** — `1 − 6,353,307 / 6,362,620` |
| `isFraud` | 8,213 rows = **0.1291 %** |
| `isFlaggedFraud` | **16 rows in 6,362,620** |
| Currency | EUR, minor units (Int64) |
| Licence | **CC BY-SA 4.0** |
| Citation | E. A. Lopez-Rojas, A. Elmir, S. Axelsson, "PaySim: A financial mobile money simulator for fraud detection," 28th European Modeling and Simulation Symposium (EMSS), Larnaca, Cyprus, 2016 |

**Licence obligation.** Share-alike: any derivative dataset we publish — the canonical
event table, feature extracts, any released sample — inherits CC BY-SA 4.0 with
attribution. The repository is not the publication boundary; a published sample is.

**What `isFraud` means, stated before a judge finds it.** It covers one narrow
simulator behaviour: an agent takes over an account and drains it via TRANSFER then
CASH-OUT. `isFlaggedFraud` is a crude threshold, not ground truth, and at 16 positives
in 6.36 M rows it is not a learnable target at any depth — it is carried, never
trained on. Scripted in the UI: *"PaySim gives realistic mobile-money topology and
volume; IBM-AML gives multi-account laundering typologies. We report metrics per
corpus and never average them into one headline number."*

**Synthetic fields and the timestamp rule.** `step` is a day counter, not an instant,
and all five balance columns are simulator output known to be internally inconsistent
with `amount`. A `step` becomes `2014-01-01T00:00:00Z + 24 h × step`, plus a
**deterministic intra-step microsecond offset derived from a hash of the transaction
id** — without it every transaction in a step shares a timestamp and intra-step
ordering becomes arbitrary, which a walk-forward backtest would silently inherit.
The offset is reproducible across runs (`test_step_expansion_stable`); the
synthetic-ness is stated here rather than left implicit.

Balance inconsistency is **a feature, not an error to repair**: reconciling it would
be the silent coercion the strict contract exists to prevent, and the delta
(`balance_delta_*`) is itself signal.

**Why PaySim is not the network corpus (DEV-011).** Median account degree 1.0; median
counterparty degree 1.0 against the plan §4 condition **> 2**; p90 1.0, p99 14, max
113; the highest-degree *sender* in 6.36 M rows has 3 edges; **zero** surviving
time-respecting 3–6 cycles on a 20k sample. On the fraud subset alone the sender reuse
ratio is exactly 0.0 and max degree is 2 — every fraudulent transaction has a unique
originator. Verdict: `STAR_SHAPED_TRIGGER_DAY4_FALLBACK`.

## 2. IBM Transactions for AML — primary for Module B (network typologies)

| Field | Value |
| --- | --- |
| Source | `https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml` |
| Dataset size | 8,176,169,418 bytes as reported by Kaggle (`datasets/list`) |
| Acquired | the **HI-Small** scenario bundle only — ~510 MB (see §4 of DEV-013) |
| `HI-Small_Trans.csv` | 475,664,283 bytes, SHA-256 `b19d39f515523373f991b689c07e11e7b0b95c17a2c27a87d91584ae16c5b040` |
| `HI-Small_accounts.csv` | 34,053,187 bytes, SHA-256 `786808526e33cfc441212dd6fccda7edfc24172149bed59c6ef59b186836b014` |
| `HI-Small_Patterns.txt` | 323,844 bytes, SHA-256 `2c546b5ce6009e73851f0139af053cf845f08bf92f3bc82fe1eb937dec2ef39b` |
| Rows | 5,078,345 |
| Distinct accounts | 515,080 |
| Directed edges (self-loops excluded) | 647,939 |
| **Median account degree** | **10.0** (6.0 with self-loops counted — excluded deliberately, see below) |
| Degree p90 / p99 / max | 51 / 119 / 169,756 |
| Median counterparties per account | 2.0 |
| Accounts with > 2 counterparties | 173,735 |
| Self-loop rows | 591,212 = **11.6 %** |
| `Is Laundering` | 5,177 rows = **0.1019 %** |
| Temporal range | 2022/09/01 00:00 → 2022/09/18 16:18 (**18 days**) |
| Currencies | 15 (US Dollar, Euro, Swiss Franc, Yuan, Shekel, Mexican Peso, Indian Rupee, Canadian Dollar, Congo Franc, Malaysian, Brazilian Real, British Pound, Romanian Leu, Turkish Lira, Vietnamese Dong, plus Australian Dollar, Ruble, Rupee, Saudi Riyal, Yen, Bitcoin spellings) |
| Payment formats | Cheque, Credit Card, ACH, Cash, Reinvestment, Wire, Bitcoin |
| Licence | **CDLA-Sharing-1.0** (the repository code is Apache-2.0; the data is not) |
| Citation | ealtman2019, *IBM Transactions for Anti-Money Laundering*, Kaggle |

**Licence obligation.** Share-alike on derived data: the canonical event table, the
feature tables and any published sample inherit CDLA-Sharing-1.0.

**Header hazard (found in the bytes, not the docs).** The header repeats the column
name `Account` for sender and receiver:
`Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,Amount Paid,Payment Currency,Payment Format,Is Laundering`.
A name-keyed reader silently loses one of them, so the contract reads positionally.

**Two timestamps, two formats.** `HI-Small_Trans.csv` writes `2022/09/01 00:20`;
`HI-Small_Patterns.txt` writes `2022-09-01 00:06`. A text join across the two matches
nothing; both sides are normalised to `YYYY-MM-DD HH:MM` before any comparison. The
corpus declares no timezone and has **minute precision**, so intra-minute ordering is
derived deterministically rather than trusted from file order, and `local_hour` is
computed once at ingest from the deployment timezone (Africa/Kampala) under an
explicitly recorded assumption.

**Self-loops are kept and excluded from connectivity.** 11.6 % of rows move value
between two accounts that are the same account, concentrated in `Reinvestment`. They
are ingested (rejecting them would fail the run on a sixth of the corpus), they are a
feature (`self_transfer_count`), and they are excluded from degree, edges, counterparty
counts and cycle detection — because a self-loop gives an account *degree* without
giving it a *counterparty*, and counting them inflates the very statistic the network
thesis rests on. Hence 10.0 rather than 6.0 above.

**Labels: the binary flag is not the typology.** `Is Laundering` is 0/1. Typologies
come from the pattern file's annotations — 370 labelled laundering attempts over
3,209 transactions in 8 kinds: GATHER-SCATTER 716, SCATTER-GATHER 626, STACK 466,
FAN-OUT 342, FAN-IN 318, **CYCLE 287**, BIPARTITE 263, RANDOM 191
(`scripts/build_ibm_typologies.py` → `data/processed/ibm_typologies.parquet`).
Measured, and load-bearing for the model: the laundering-flagged rows are
**near-degree-1 among themselves** (median 1.0), so a model trained on the flag alone
learns account-level oddity, not network shape. The `RANDOM` blocks are planted
laundering-attempt traffic with **no** typology — a free negative control for any rule
that would otherwise fire on everything. Labelled typologies are curated research
annotations, not prosecuted cases.

**R4's definition vs. this corpus (DEV-015).** Of the 54 labelled CYCLE blocks: all
54 close as directed loops, **38 are cross-currency**, **25 fall below the 0.6 value
retention floor** (the worst twelve retain 0.0015–0.0119), 14 are two-leg
round-trips, and 5 are not time-monotonic. A cycle rule restricted to one currency,
requiring non-increasing legs and ≥ 0.6 retention **cannot fire on the majority of the
cycles this corpus itself labels as cycles**. This is stated rather than fixed by
quietly loosening a default; see DEV-015.

## 3. Cited, never ingested

* **Elliptic Bitcoin Dataset** — CC BY-NC-**ND** 4.0. No derivatives, so even a
  derived sample would violate the licence. Cited in the README only;
  `ingest_allowed: false` in `config/sources.yaml`, which the reader enforces.
* **IEEE-CIS Fraud Detection** — competition-governed, redistribution terms unclear.
  Never used, in any form.

Both are refusals, not preferences. A non-goal here is a hard "no", which is why it is
a config flag the ingest path checks rather than a comment.

## 4. Sampling rule for the interactive product

Disk and runtime are bounded (`config/pipeline.yaml → sampling`), so the interactive
artefacts are built on a **connected subcorpus** targeting
`interactive_txn_target: 500_000` transactions, while full-corpus metrics are
computed offline.

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

## 5. Where each number came from

```
uv run python scripts/download_data.py --source paysim --verify   # hashes, row counts
uv run python scripts/measure_graph.py                             # data/graph_measurement.json
uv run python scripts/measure_ibm_graph.py                         # data/ibm_graph_measurement.json
uv run python scripts/build_ibm_typologies.py                      # data/processed/ibm_typologies.parquet
uv run python scripts/measure_ibm_cycles.py                        # data/ibm_cycle_measurement.json
```

The four `data/*.json` files are committed; the card restates their contents in prose.
If a figure here and a figure in an artefact disagree, the artefact is right and this
file is a bug.
