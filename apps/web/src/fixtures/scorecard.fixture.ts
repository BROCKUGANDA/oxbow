/* FIXTURE DATA — developer contract double (see common.fixture.ts).
   Scorecard Studio payloads: the WOE table, the band cut points, drift, and the
   scorecard/GBM disagreement list.

   The numbers are internally consistent on purpose: each attribute's points are
   derived from its WOE against the scaling constants in config/scorecard.yaml
   (PDO 20, base 600 at 50:1), and the bin population shares sum to one. A fixture
   whose columns do not add up teaches the page to render nonsense and nobody
   notices, which is the opposite of what a double is for. */

import type { DisagreementPage, Drift, Scorecard, ScorecardAttribute } from '../lib/api/contract';
import { BAND_LETTERS, figure, seeded } from './common.fixture';

/** factor = PDO / ln(2), offset = base - factor * ln(base odds). config/scorecard.yaml. */
const PDO = 20;
const BASE_SCORE = 600;
const BASE_ODDS = 50;
const FACTOR = Math.round((PDO / Math.LN2) * 100) / 100;
const OFFSET = Math.round((BASE_SCORE - FACTOR * Math.log(BASE_ODDS)) * 100) / 100;

type BinSeed = { bin: string; bad_rate: number; share: number; special: 'missing' | 'structural_zero' | 'unseen' | null };

/** WOE from the base rate the corpus carries, so the sign follows the bad rate. */
function woeOf(badRate: number, baseRate: number): number {
  const good = 1 - badRate;
  const goodBase = 1 - baseRate;
  return Math.round(Math.log((good / goodBase) * (baseRate / Math.max(badRate, 0.0001))) * 1000) / 1000;
}

function binsFor(seeds: readonly BinSeed[], baseRate: number, total: number): ScorecardAttribute['bins'] {
  return seeds.map((seed) => {
    const woe = woeOf(seed.bad_rate, baseRate);
    return {
      bin: seed.bin,
      woe,
      points: -Math.round(woe * FACTOR + (OFFSET - BASE_SCORE) / total),
      population_share: seed.share,
      bad_rate: seed.bad_rate,
      bad_count: Math.round(seed.share * total * seed.bad_rate),
      is_special: seed.special,
    };
  });
}

const BASE_RATE = 0.1019;
const TOTAL = 515_080;

const ATTRIBUTE_SEEDS: { attribute: string; label: string; sentence: string; iv: number; seeds: BinSeed[] }[] = [
  {
    attribute: 'pass_through_ratio_1h',
    label: 'Pass-through ratio, trailing hour',
    sentence: 'Share of what the account received that it forwarded on inside the same hour.',
    iv: 0.41,
    seeds: [
      { bin: '0.00 – 0.20', bad_rate: 0.012, share: 0.58, special: null },
      { bin: '0.20 – 0.55', bad_rate: 0.051, share: 0.24, special: null },
      { bin: '0.55 – 0.86', bad_rate: 0.208, share: 0.12, special: null },
      { bin: '0.86 – 1.00', bad_rate: 0.63, share: 0.012, special: null },
      { bin: 'missing', bad_rate: 0.04, share: 0.048, special: 'missing' },
    ],
  },
  {
    attribute: 'distinct_senders_24h',
    label: 'Distinct senders, 24 h',
    sentence: 'Count of different accounts that sent to this one inside the window.',
    iv: 0.28,
    seeds: [
      { bin: '1 – 2', bad_rate: 0.021, share: 0.47, special: null },
      { bin: '3 – 6', bad_rate: 0.078, share: 0.31, special: null },
      { bin: '7 – 10', bad_rate: 0.24, share: 0.09, special: null },
      { bin: '≥ 11', bad_rate: 0.44, share: 0.03, special: null },
      { bin: '0 (structural zero)', bad_rate: 0.004, share: 0.1, special: 'structural_zero' },
    ],
  },
  {
    attribute: 'cycle_retention_score',
    label: 'Cycle value retention',
    sentence: 'Share of value that survives a round trip between accounts that keep transacting.',
    iv: 0.33,
    seeds: [
      { bin: 'not in a cycle', bad_rate: 0.041, share: 0.66, special: null },
      { bin: '< 0.60', bad_rate: 0.12, share: 0.19, special: null },
      { bin: '0.60 – 0.85', bad_rate: 0.39, share: 0.08, special: null },
      { bin: '> 0.85', bad_rate: 0.58, share: 0.02, special: null },
      { bin: 'unseen category', bad_rate: 0.11, share: 0.05, special: 'unseen' },
    ],
  },
  {
    attribute: 'cash_out_share_6h',
    label: 'Cash-out share of inflow, 6 h',
    sentence: 'How much of what arrived left again as cash inside six hours.',
    iv: 0.19,
    seeds: [
      { bin: '0.00 – 0.25', bad_rate: 0.036, share: 0.62, special: null },
      { bin: '0.25 – 0.55', bad_rate: 0.101, share: 0.21, special: null },
      { bin: '0.55 – 0.70', bad_rate: 0.212, share: 0.09, special: null },
      { bin: '≥ 0.70', bad_rate: 0.41, share: 0.03, special: null },
      { bin: 'missing', bad_rate: 0.06, share: 0.05, special: 'missing' },
    ],
  },
  {
    attribute: 'inactive_days_prior',
    label: 'Days inactive before reactivation',
    sentence: 'Quiet days on the account immediately before the triggering burst.',
    iv: 0.11,
    seeds: [
      { bin: '< 8', bad_rate: 0.061, share: 0.54, special: null },
      { bin: '8 – 29', bad_rate: 0.115, share: 0.28, special: null },
      { bin: '30 – 90', bad_rate: 0.288, share: 0.11, special: null },
      { bin: '≥ 91', bad_rate: 0.352, share: 0.02, special: null },
      { bin: 'missing', bad_rate: 0.09, share: 0.05, special: 'missing' },
    ],
  },
  {
    attribute: 'night_volume_share',
    label: 'Quiet-hour volume share',
    sentence: 'Share of volume landing in the deployment timezone’s historically quiet hours.',
    iv: 0.04,
    seeds: [
      { bin: '< 0.10', bad_rate: 0.071, share: 0.58, special: null },
      { bin: '0.10 – 0.30', bad_rate: 0.118, share: 0.3, special: null },
      { bin: '≥ 0.30', bad_rate: 0.196, share: 0.06, special: null },
      { bin: 'missing', bad_rate: 0.08, share: 0.06, special: 'missing' },
    ],
  },
  {
    attribute: 'account_age_days',
    label: 'Account age',
    sentence: 'Days between opening and the as-of timestamp.',
    iv: 0.008,
    seeds: [
      { bin: '< 30', bad_rate: 0.204, share: 0.12, special: null },
      { bin: '≥ 30', bad_rate: 0.087, share: 0.88, special: null },
    ],
  },
  {
    attribute: 'counterparty_reuse_ratio',
    label: 'Counterparty reuse ratio',
    sentence: 'How often the account’s counterparties are accounts it has already met.',
    iv: 0.62,
    seeds: [
      { bin: '0.00 – 0.40', bad_rate: 0.048, share: 0.5, special: null },
      { bin: '≥ 0.40', bad_rate: 0.4, share: 0.2, special: null },
      { bin: 'missing', bad_rate: 0.1, share: 0.3, special: 'missing' },
    ],
  },
];

const attributes: ScorecardAttribute[] = ATTRIBUTE_SEEDS.map((entry) => ({
  attribute: entry.attribute,
  label: entry.label,
  sentence: entry.sentence,
  iv: entry.iv,
  admitted: entry.iv >= 0.02 && entry.iv <= 0.5,
  refusal_reason:
    entry.iv > 0.5
      ? 'IV 0.62 exceeds the 0.5 ceiling: suspected leakage, requires written justification before admission (config/scorecard.yaml iv_bounds.above_max_action)'
      : entry.iv < 0.02
        ? 'IV 0.008 below the 0.02 floor: excluded and recorded'
        : null,
  bins: binsFor(entry.seeds, BASE_RATE, TOTAL),
}));

export const scorecard: Scorecard = {
  scaling: {
    pdo: PDO,
    base_score: BASE_SCORE,
    base_odds: BASE_ODDS,
    factor: FACTOR,
    offset: OFFSET,
    formula: 'score = offset + factor · ln(odds), factor = PDO / ln(2), offset = base − factor · ln(base odds)',
  },
  admission_rule: {
    min_iv: 0.02,
    max_iv: 0.5,
    above_max_action: 'require_written_justification',
    source: 'config/scorecard.yaml',
  },
  attributes,
  bands: [
    { band: 'A', lower: 720, upper: 999, population_share: 0.62, observed_rate: 0.012, action: 'monitor', accounts: 319_350 },
    { band: 'B', lower: 640, upper: 719, population_share: 0.237, observed_rate: 0.051, action: 'monitor', accounts: 122_074 },
    { band: 'C', lower: 545, upper: 639, population_share: 0.084, observed_rate: 0.163, action: 'review', accounts: 43_267 },
    { band: 'D', lower: 430, upper: 544, population_share: 0.044, observed_rate: 0.402, action: 'review', accounts: 22_664 },
    { band: 'E', lower: 0, upper: 429, population_share: 0.015, observed_rate: 0.671, action: 'escalate', accounts: 7_725 },
  ],
  points_total_reconciles: true,
  source: 'config/scorecard.yaml',
};

export const drift: Drift = {
  psi_by_period: [
    { period: '2026-05', psi: 0.041, verdict: 'ok' },
    { period: '2026-06', psi: 0.088, verdict: 'ok' },
    { period: '2026-07', psi: 0.142, verdict: 'watch' },
    { period: '2026-08', psi: 0.268, verdict: 'action' },
  ],
  csi_by_feature: [
    { feature: 'pass_through_ratio_1h', label: 'Pass-through ratio, trailing hour', csi: 0.211 },
    { feature: 'night_volume_share', label: 'Quiet-hour volume share', csi: 0.174 },
    { feature: 'distinct_senders_24h', label: 'Distinct senders, 24 h', csi: 0.096 },
    { feature: 'account_age_days', label: 'Account age', csi: 0.041 },
  ],
  rating_migration: BAND_LETTERS.flatMap((from) =>
    BAND_LETTERS.map((to) => ({
      from,
      to,
      count: from === to ? 400 + Math.round((from.charCodeAt(0) % 7) * 60) : Math.round(((from.charCodeAt(0) - to.charCodeAt(0)) % 9) * 12 + 6),
    }))),
  downgrade_rate: 0.061,
  thresholds: { watch: 0.1, action: 0.25, source: 'config/scorecard.yaml' },
};

export const disagreement: DisagreementPage = {
  rows: (() => {
    const next = seeded(424);
    return [
      { key: 'ACC-2E91C4', sc: 0.212, gbm: 0.771 },
      { key: 'ACC-8C3F10', sc: 0.744, gbm: 0.198 },
      { key: 'ACC-55B2A7', sc: 0.301, gbm: 0.802 },
      { key: 'ACC-1D7E66', sc: 0.688, gbm: 0.224 },
      { key: 'ACC-A04C22', sc: 0.142, gbm: 0.596 },
      { key: 'ACC-73F9B1', sc: 0.651, gbm: 0.238 },
      { key: 'ACC-0B6A35', sc: 0.288, gbm: 0.674 },
      { key: 'ACC-9F2D08', sc: 0.612, gbm: 0.251 },
    ].map((entry) => {
      const delta = Math.abs(entry.sc - entry.gbm);
      const high = entry.sc > entry.gbm ? entry.sc : entry.gbm;
      return {
        account_key: entry.key,
        case_href: `/cases/${entry.key}`,
        scorecard_score: entry.sc,
        gbm_score: entry.gbm,
        delta: Math.round(delta * 1000) / 1000,
        band: high > 0.86 ? 'E' : high > 0.7 ? 'D' : high > 0.45 ? 'C' : high > 0.2 ? 'B' : 'A',
        exposure: figure(Math.round(40_000_000 + next() * 600_000_000)),
      };
    });
  })(),
  compared_accounts: 5_142,
  max_delta: 0.583,
  near_miss_threshold: 0.15,
  rows_at_threshold: 37,
};

/** The zero-disagreement variant, which is a finding rather than an absence. */
export const disagreementEmpty: DisagreementPage = {
  rows: [],
  compared_accounts: 4_918,
  max_delta: 0,
  near_miss_threshold: 0.15,
  rows_at_threshold: 37,
};
