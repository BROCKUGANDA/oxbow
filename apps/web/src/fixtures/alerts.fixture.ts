/* FIXTURE DATA — developer contract double (see common.fixture.ts).
   The alert queue. Sized deliberately: 40 rows for the ordinary page, and a
   10,000-row variant for `test_virtualised_large_queue`, which cannot be proven
   against a twenty-row list. */

import type { AlertPage, AlertRow, Band, Typology } from '../lib/api/contract';
import { BAND_LETTERS, RULE_IDS, figure, pick, seeded } from './common.fixture';

const REASON_TEXT: readonly { attribute: string; label: string; bin: string; text: string }[] = [
  {
    attribute: 'pass_through_ratio_1h',
    label: 'Pass-through ratio, trailing hour',
    bin: 'top decile',
    text: 'Pass-through ratio in top decile: minus 48 points',
  },
  {
    attribute: 'distinct_senders_24h',
    label: 'Distinct senders, 24 h',
    bin: '≥ 11',
    text: 'Distinct inbound counterparties above the ninth decile: minus 31 points',
  },
  {
    attribute: 'cycle_retention_score',
    label: 'Cycle value retention',
    bin: '0.60 – 0.85',
    text: 'Member of a value-retaining cycle: minus 27 points',
  },
  {
    attribute: 'cash_out_share_6h',
    label: 'Cash-out share of inflow, 6 h',
    bin: '≥ 0.70',
    text: 'Majority of inflow exited as cash-out inside six hours: minus 24 points',
  },
  {
    attribute: 'inactive_days_prior',
    label: 'Days inactive before reactivation',
    bin: '≥ 30',
    text: 'Dormant then reactivated: minus 18 points',
  },
  {
    attribute: 'night_volume_share',
    label: 'Quiet-hour volume share',
    bin: 'shift +0.44',
    text: 'Quiet-hour volume shifted by 44 points of share: minus 12 points',
  },
  {
    attribute: 'median_txn_amount',
    label: 'Median transaction amount',
    bin: 'missing',
    text: 'Median transaction amount not observed in window: minus 6 points',
  },
];

const OBSERVED: readonly string[] = [
  'receives, forwards 0.94 within 22 min',
  '11 distinct senders in 24 h, median below p25',
  '4-hop time-respecting cycle, retention 0.71',
  '0.81 of inflow exits as cash-out in 3 h',
  'reactivated after 41 days, 7 txns in 24 h',
  '5 transfers inside 80–100 % of the configured threshold',
];

function rows(count: number, seed: number): AlertRow[] {
  const next = seeded(seed);
  const out: AlertRow[] = [];
  for (let index = 0; index < count; index += 1) {
    // Score falls monotonically down the queue, as a rank-ordered run does: rank 1
    // is the highest EV density, and the band follows the scorecard cut points.
    const score = Math.max(0.02, 0.97 - index / (count * 1.04) - next() * 0.01);
    const band: Band = score > 0.86 ? 'E' : score > 0.7 ? 'D' : score > 0.45 ? 'C' : score > 0.2 ? 'B' : 'A';
    const exposureMinor = Math.round(20_000_000 + next() * 900_000_000);
    const reviewMinutes = { A: 5, B: 12, C: 25, D: 60, E: 120 }[band] ?? 25;
    const reasonStart = Math.floor(next() * REASON_TEXT.length);
    const key = `ACC-${(0x100000 + index * 0x9e37 + seed).toString(16).toUpperCase().slice(0, 6)}`;
    out.push({
      account_key: key,
      run_id: '01FIXTUREDQUEUE00000000000',
      case_href: `/cases/${key}`,
      case_status: index < 3 ? 'open' : null,
      score: Math.round(score * 1000) / 1000,
      band,
      // The calibrated arm, because the double exists to exercise the screen that a live
      // uncalibrated run cannot: `deriveAlertRow`'s uncalibrated branch is covered by
      // `explorer-and-fidelity.spec.ts` against the served payload, and this row shape is
      // how the calibrated branch gets rendered at all.
      confidence: {
        kind: 'calibrated',
        probability: Math.round(score * 1000) / 1000,
        observed_rate: Math.round(Math.max(0.01, score - 0.03 + next() * 0.06) * 1000) / 1000,
        n: 40 + Math.floor(next() * 900),
      },
      typology: pick<Typology>(RULE_IDS, next),
      reasons: [0, 1, 2].map((offset) => {
        const entry = REASON_TEXT[(reasonStart + offset) % REASON_TEXT.length];
        if (entry === undefined) throw new Error('fixture reason index out of range');
        return { text: entry.text, points: -Math.round(6 + next() * 42) };
      }),
      exposure: figure(exposureMinor),
      expected_value: figure(
        Math.round(exposureMinor * score * 0.35 - reviewMinutes * 15_000 - (1 - score) * 2_500_000),
      ),
      rank: index + 1,
      allocation_source: 'stored',
      selected: index < 200,
      beyond_capacity: index >= 200,
      cutoff_rank: 200,
      capacity_minutes: 12_000,
      txn_count: 12 + Math.floor(next() * 400),
      first_seen: new Date(Date.UTC(2026, 8, 2, 6, 0)).toISOString(),
      last_seen: new Date(Date.UTC(2026, 8, 24, Math.floor(next() * 23), Math.floor(next() * 59))).toISOString(),
    });
  }
  return out;
}

export const alertRows: AlertRow[] = rows(40, 1337);

/** The large-queue double: 10,000 rows, same generator, so row geometry is identical. */
export function largeAlertPage(limit: number, offset: number): AlertPage {
  const all = rows(10_000, 4242);
  return {
    rows: all.slice(offset, offset + limit),
    capacity: {
      cutoff_rank: 200,
      capacity_minutes: 12_000,
      allocation_source: 'stored',
      policy_id: 'POLICY-FIXTURE-1',
      unpriced_accounts: 0,
    },
    facets: {
      bands: BAND_LETTERS.map((band) => ({
        band,
        count: all.filter((row) => row.band === band).length,
      })),
      typologies: RULE_IDS.slice(0, 6).map((typology, index) => ({
        typology,
        rule_code: OBSERVED[index % OBSERVED.length] ?? 'rule hit',
        count: 40 + index * 37,
      })),
    },
    filter_recovery: null,
  };
}

/** The queue page the ordinary route serves. */
export const alertPage: AlertPage = {
  rows: alertRows,
  capacity: {
    cutoff_rank: 24,
    capacity_minutes: 12_000,
    allocation_source: 'stored',
    policy_id: 'POLICY-FIXTURE-1',
    unpriced_accounts: 2,
  },
  facets: {
    bands: BAND_LETTERS.map((band) => ({ band, count: alertRows.filter((row) => row.band === band).length })),
    typologies: [
      { typology: 'R4', rule_code: 'CYCLE_MEMBER', count: 41 },
      { typology: 'R2', rule_code: 'FAN_IN', count: 77 },
      { typology: 'R1', rule_code: 'RAPID_PASS_THROUGH', count: 96 },
      { typology: 'R10', rule_code: 'FAST_CASH_OUT', count: 52 },
      { typology: 'R12', rule_code: 'CHAIN_MEMBER', count: 28 },
      { typology: 'R5', rule_code: 'STRUCTURING', count: 19 },
    ],
  },
  filter_recovery: {
    narrowest: 'typology',
    label: 'Typology is circular transfer',
    rows_if_removed: 11,
    unfiltered_rows: 1_412,
  },
};
