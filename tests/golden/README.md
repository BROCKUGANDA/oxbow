# tests/golden — the P3b golden fixture

## What this is

A ~500-row hand-built corpus (`transactions.csv`, actually 534 rows, 319
accounts, 59 scenarios + noise) planted with the twelve network typologies the
rule engine R1-R12 must detect, plus deliberate near-misses that must NOT
fire. Per plan §9 it is built **before** the rules code, and per plan §9/
00 §B its expected values are **hand-computed and checked in** — in
`expected.yaml` — not produced by the engine they will be used to test. A
fixture whose expectations come from the code under test is a mirror, not a
gate.

Why hand-built matters more here than anywhere else in this build:
DECISIONS DEV-011 measured PaySim and found the corpus **star-shaped** (max
sender degree 3, zero surviving time-respecting cycles in a 20k sample). The
real corpora cannot demonstrate that the typologies exist and are findable.
This fixture is the evidence layer for Module B: every cycle, fan-in,
ladder, dormant wake and pass-through chain in the prototype's detection
story is planted here first, with arithmetic a human can re-do in pen.

## Files

| File | Role |
| --- | --- |
| `transactions.csv` | the corpus, canonical columns, sorted by `(event_ts_utc, txn_id)`. CSV, not parquet: ground truth a judge must be able to eyeball and `git diff` |
| `loader.py` | the only sanctioned reader: `load_golden_transactions() -> pl.DataFrame` (strict Int64 money schema, no coercion, no inference) and `load_golden_expectations() -> dict` |
| `build_fixture.py` | deterministic generator, seed 1337. Scenario definitions are hand-authored constants; randomness only fills 25 inert background edges. Two runs produce byte-identical output (self-checked) |
| `build_manifest.json` | seed, row count, sha256, scenario → row-ordinal map, written by the builder |
| `expected.yaml` | scenario → plant description → rows → expected rule hits / non-hits → the hand arithmetic → severity orderings → overlap groups → deviations |
| `conftest.py` | `golden`, `expected`, `build_manifest` session fixtures |
| `test_golden_fixture_selfcheck.py` | the fixture validating ITSELF (27 tests, no rule code) |

## Re-validate everything, one command

```
uv run pytest -q tests/golden
```

That runs the self-check: exact canonical column set (ordered), money/balances
int-typed with no decimal cells and no float literals in money positions of
the generator (AST-scanned), `local_hour`/`event_date_local` consistent with
`event_ts_utc` under **Africa/Kampala (UTC+3, no DST)** on every row,
`golden:%06d` ids unique and equal to file order = the (event_ts_utc, txn_id)
total order, self-transfers / zero-amount rows / USD rows / REVERSAL rows /
balance-delta-inconsistent rows each exactly the declared planted sets,
every referenced account present, planted counts equal to what
`expected.yaml` claims, every row in exactly one scenario, no expected-hit
account typed `rail` under P3a's committed percentile formula, the tau and
structuring-band design brackets still holding, fraud labels only on planted
positives, the `rule_params` mirror not drifted from `config/rules.yaml`,
and a double-build byte-identical digest check.

Regenerate (must not change any byte):

```
uv run python tests/golden/build_fixture.py
```

## Invariants every added row must satisfy

1. Money columns are integers (minor units). Never a decimal literal.
2. `txn_id` = `golden:<zero-padded int>`; ids are assigned by the builder
   after sorting — never hand-number them.
3. `event_ts_utc` is ISO-8601 UTC **with microseconds**; `local_hour` and
   `event_date_local` are *derived* by the builder from it, not authored.
4. `source_dataset=golden`, one constant `run_id`/`batch_id`/`ingested_at`.
   Timestamps must be ≤ `ingested_at` (2026-09-26) — future rows would be
   quarantined by real ingest rules.
5. Balances default to delta-consistent (`src_after = src_before − amount`,
   `dst_after = dst_before + amount`, non-negative). Deliberate inconsistency
   is allowed **only** inside a scenario labelled `balance_inconsistent`
   (S58 is the planted one).
6. Amount scale per account stays within a <4× window range so no account
   trips R9 by accident; quiet-hour rows go only to dedicated receivers with
   <10 lifetime txns so no bystander inherits R8.
7. A new counterparty cluster never introduces ≥6 first-time counterparties
   within any 48h window unless R11 firing is the declared expectation
   (spread histories ≤2 new pairings per 48h).
8. Degree budget: all rule hubs ≤ ~13 incident edges; supernode gap nodes
   (RAIL-AGENT 48, burst pair 44, RAIL-MARKET 20) exist so the
   rail-percentile threshold falls inside the gap at any account count
   in [101, 390]. The self-check recomputes the P3a percentile formula
   against the current N and fails if a hub crosses into rail territory.
9. Cross-currency rows must never be sum-compatible with EUR rows; new USD
   rows keep each USD account's ledger inside one currency.
10. `txn_type` ∈ {CASH_IN, CASH_OUT, DEBIT, PAYMENT, TRANSFER, REVERSAL}
    (the golden alphabet; `REVERSAL` extends PaySim's five — see
    expected.yaml DEV-G5).

## Adding a scenario

1. Write `sNN_*()` in `build_fixture.py` returning hand-chosen rows
   (`event(...)`) and register it in `BUILDERS`.
2. `uv run python tests/golden/build_fixture.py`, take the row ordinals
   from `build_manifest.json`.
3. Add the scenario block to `expected.yaml`: plant description, rows, the
   **paper arithmetic** against `rule_params` (ratio/window/median spelled
   out), `expect_fire` / `expect_silent`, and update `expected_hits`,
   `counts`, and the severity orderings if the new rows change a ranking.
4. `uv run pytest -q tests/golden` — the planted-count, total-order,
   scale-bracket and rail-gap checks will reject the most common mistakes.

Never adjust an `expected.yaml` number to match engine output. If the engine
disagrees with the fixture, one of them is wrong, and the tie-break is the
paper arithmetic in §9/config — say so in the commit.

## Known interpretation pins (fixture declares; engine may differ — loudly)

`expected.yaml` `conventions:` pins: R5 origin-side counting with T=2_500_000
(synthetic, absent from config — DEV-G1); R6 baseline over *active* days with
MAD=0 ⇒ not significant; R11 count = distinct counterparties, fresh share
denominator = distinct counterparties active in W; R12 gate = non-increasing
(decay → severity; S28 fires under either reading). Near-miss margins were
chosen so the verdict survives both variants of every coin-flip except where
the convention note says otherwise.

## Coverage: R1-R12, positives and must-not-fire near-misses

| Rule | Positive scenario(s) | Near-miss scenario(s) |
| --- | --- | --- |
| R1 | S01 (0.92), S02 (0.88), S03 (0.85) | S04 ratio 0.79, S05 min-A, S06 window 61min |
| R2 | S11, S12 | S13 k=7, S14 window, S15 tau (240000 ≥ p25) |
| R3 | S16, S17 | S18 k=7 |
| R4 | S19 (4-cycle), S20 (3-cycle), S24 (payroll, down-weighted) | S21 retention 0.59, S22 length 7, S23 time-reversed, S25 self-loop, S26/S27 reversals |
| R5 | S33 (n=4), S34 (n=3) | S35 79%-of-T + count 2, S36 window spread |
| R6 | S37 (z 6.74, both sides) | S38 (z 1.35/2.0), S39 baseline 3d < 5 |
| R7 | S40 (35d gap), S41 (91d, higher severity) | S42 29d gap, S43 burst of 4 < k=5 |
| R8 | S44 (jump 0.80, local hours) | S45 share 0.30, S46 total 9 < min 10, S47 UTC-trap |
| R9 | S48 (ratio 4.83) | S49 (ratio 3.68) |
| R10 | S07 (0.775) + S02 (0.88 overlap) | S08 share 0.69, S09 holding 7h10m |
| R11 | S50 (8/8, count 8) + hub collateral | S51 share 0.667, S52 count 5 |
| R12 | S28 (4 hops, decay-exact) | S29 increasing, S30 slow 30h, S31 short 2-hop, S32 decoy |
| structural | S10 cross-currency, S53 burst×40, S54 rail, S55 second rail, S56 singleton, S57 zero, S58 balance-delta, S25 self | S59 noise (must fire nothing) |
