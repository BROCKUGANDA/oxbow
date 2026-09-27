/* FIXTURE DATA — developer contract double (see common.fixture.ts).
   The hero case: a value-retaining 4-cycle mule, which is the case the 4-minute
   demo script opens (plan §15). Two other cases exist so the queue's deep links
   resolve, and an account with no counterparties in the window so the
   window-empty state is reachable from a real route rather than only the gallery. */

import type { CasePayload, Decision, EvidenceEvent, TransactionRow } from '../lib/api/contract';
import { figure, money, seeded } from './common.fixture';

const HERO = 'ACC-7F2A19';

function transactions(count: number): TransactionRow[] {
  const next = seeded(88);
  const out: TransactionRow[] = [];
  for (let index = 0; index < count; index += 1) {
    const inbound = index % 3 !== 0;
    const minor = Math.round((inbound ? 4_500_000 : 4_200_000) + next() * 3_000_000);
    out.push({
      txn_id: `ibmaml:${String(100_000 + index * 7)}`,
      ts_utc: new Date(
        Date.UTC(2026, 8, 11, 3 + Math.floor(index / 4), (index * 17) % 60, (index * 7) % 60),
      ).toISOString(),
      type: inbound ? 'TRANSFER' : 'CASH_OUT',
      channel: 'mobile_wallet',
      amount: money(minor),
      counterparty_key: `ACC-${(0x210000 + index * 0x1f3).toString(16).toUpperCase().slice(0, 6)}`,
      direction: inbound ? 'in' : 'out',
      src_balance_after: money(Math.round(12_000_000 + next() * 40_000_000)),
      dst_balance_after: null,
      balance_delta: money(Math.round(inbound ? minor : -minor)),
      is_zero_value: minor === 0,
      rule_codes: index < 3 ? ['R1', 'R4'] : index % 9 === 0 ? ['R10'] : [],
    });
  }
  return out;
}

function evidence(count: number): EvidenceEvent[] {
  const txns = transactions(count);
  const out: EvidenceEvent[] = txns.map((txn, index) => ({
    id: `ev-${String(index)}`,
    ts_utc: txn.ts_utc,
    kind: index % 7 === 0 ? 'rule_hit' : 'transaction',
    title:
      index % 7 === 0
        ? 'R4 CYCLE_MEMBER closed a 4-hop loop'
        : `${txn.type} ${txn.direction === 'in' ? 'in' : 'out'} · ${txn.counterparty_key ?? 'external'}`,
    detail:
      index % 7 === 0
        ? 'timestamps strictly increasing around the loop, retention 0.71 against the 0.60 floor'
        : `${txn.channel}, balance delta ${txn.balance_delta === null ? 'not observed' : 'recorded'}`,
    amount: txn.amount,
    rule_code: index % 7 === 0 ? 'R4' : null,
    typology: index % 7 === 0 ? 'R4' : null,
    txn_ids: [txn.txn_id],
  }));
  out.unshift({
    id: 'ev-window',
    ts_utc: new Date(Date.UTC(2026, 8, 11, 0, 0, 0)).toISOString(),
    kind: 'window',
    title: 'Scoring window opened',
    detail: 'exposure window 24 h, downstream hops 1, as-of the first triggering event',
    amount: null,
    rule_code: null,
    typology: null,
    txn_ids: [],
  });
  return out;
}

function decisions(): Decision[] {
  return [
    {
      seq: 1,
      decision: 'review',
      reason:
        'Pulled the account onto the desk: four-hop loop with retention above the floor, and the cash-out leg is inside the window.',
      actor: 'analyst.okello',
      role: 'analyst',
      recorded_at: new Date(Date.UTC(2026, 8, 24, 9, 41, 12)).toISOString(),
      hash: '9f14c2ab7d3e0c55a1b9d4e7f20c3a6b8d1e5f70a2c4e6f80b1d3f5a7c9e0f21',
      prev_hash: null,
      reversible_of: null,
      four_eyes_required: false,
      superseded_run: false,
    },
    {
      seq: 2,
      decision: 'escalate',
      reason:
        'Counterparty set repeats across three other flagged accounts; escalating with the subgraph attached rather than closing on the account alone.',
      actor: 'reviewer.nabirye',
      role: 'reviewer',
      recorded_at: new Date(Date.UTC(2026, 8, 24, 11, 6, 38)).toISOString(),
      hash: '31b7e0c4d9a2f6510c8b3e5d7f9a1c2e4b6d8f0a2c4e6f80b1d3f5a7c9e0f213',
      prev_hash: '9f14c2ab7d3e0c55a1b9d4e7f20c3a6b8d1e5f70a2c4e6f80b1d3f5a7c9e0f21',
      reversible_of: 1,
      four_eyes_required: true,
      superseded_run: false,
    },
  ];
}

export const heroCase: CasePayload = {
  header: {
    account_key: HERO,
    run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D',
    score: 0.912,
    band: 'E',
    calibration: { bin_label: '0.90–1.00', observed_rate: 0.71, n: 432, expected_rate: 0.912 },
    typology: 'R4',
    points: [
      {
        attribute: 'pass_through_ratio_1h',
        label: 'Pass-through ratio, trailing hour',
        bin: '0.86 – 1.00',
        points: -48,
        population_share: 0.012,
        bad_rate: 0.63,
        is_reason_code: true,
      },
      {
        attribute: 'distinct_senders_24h',
        label: 'Distinct senders, 24 h',
        bin: '≥ 11',
        points: -31,
        population_share: 0.031,
        bad_rate: 0.44,
        is_reason_code: true,
      },
      {
        attribute: 'cycle_retention_score',
        label: 'Cycle value retention',
        bin: '0.60 – 0.85',
        points: -27,
        population_share: 0.018,
        bad_rate: 0.39,
        is_reason_code: true,
      },
      {
        attribute: 'median_txn_amount',
        label: 'Median transaction amount',
        bin: 'missing',
        points: -6,
        population_share: 0.061,
        bad_rate: 0.11,
        is_reason_code: false,
      },
      {
        attribute: 'account_age_days',
        label: 'Account age',
        bin: '≥ 720',
        points: 34,
        population_share: 0.28,
        bad_rate: 0.01,
        is_reason_code: false,
      },
    ],
    reasons: [
      {
        text: 'Pass-through ratio in top decile: minus 48 points',
        attribute: 'pass_through_ratio_1h',
        bin: '0.86 – 1.00',
        points: -48,
        contribution: -0.31,
      },
      {
        text: 'Distinct inbound counterparties above the ninth decile: minus 31 points',
        attribute: 'distinct_senders_24h',
        bin: '≥ 11',
        points: -31,
        contribution: -0.18,
      },
      {
        text: 'Member of a value-retaining cycle: minus 27 points',
        attribute: 'cycle_retention_score',
        bin: '0.60 – 0.85',
        points: -27,
        contribution: -0.14,
      },
    ],
    economics: {
      exposure: figure(412_000_000),
      expected_value: figure(98_050_000),
      review_minutes: 120,
      analyst_cost: figure(1_800_000),
      friction_cost: money(2_500_000),
      monte_carlo: {
        lower: money(190_000_000),
        upper: money(440_000_000),
        runs: 10_000,
        max_depth: 4,
        seed: 1337,
        interval: [0.05, 0.95],
      },
      recovery_rate: 0.35,
    },
    fusion: {
      p_scorecard: 0.864,
      p_gbm: 0.931,
      p_fused: 0.912,
      coefficients: [
        { input: 'p_scorecard', weight: 0.42 },
        { input: 'p_gbm', weight: 0.51 },
        { input: 'anomaly_norm', weight: 0.08 },
        { input: 'rule_severity_max', weight: 0.19 },
        { input: 'cycle_flag', weight: 0.11 },
      ],
    },
    decided_on_superseded_run: false,
    model_version: 'lgbm-fusion-4.5.3+isotonic',
    feature_spec_hash: 'sha256:6b1f0c4d9ae7',
    scored_at: new Date(Date.UTC(2026, 8, 25, 4, 20, 0)).toISOString(),
  },
  evidence: evidence(28),
  transactions: transactions(28),
  contributions: [
    {
      feature: 'pass_through_ratio_1h',
      label: 'Pass-through ratio, trailing hour',
      sentence: 'How much of what the account received it forwarded on inside the same hour.',
      value: -0.31,
      direction: 'decreases',
      rank: 1,
      txn_ids: ['ibmaml:100000', 'ibmaml:100007', 'ibmaml:100014'],
      evidence_ids: ['ev-1', 'ev-8', 'ev-15'],
      source: 'shap',
    },
    {
      feature: 'distinct_senders_24h',
      label: 'Distinct senders, 24 h',
      sentence: 'Count of different accounts that sent to this one inside the window.',
      value: -0.18,
      direction: 'decreases',
      rank: 2,
      txn_ids: ['ibmaml:100021', 'ibmaml:100028', 'ibmaml:100035', 'ibmaml:100042'],
      evidence_ids: ['ev-3', 'ev-10'],
      source: 'shap',
    },
    {
      feature: 'cycle_retention_score',
      label: 'Cycle value retention',
      sentence: 'Share of value that survives a round trip between accounts that keep transacting.',
      value: -0.14,
      direction: 'decreases',
      rank: 3,
      txn_ids: ['ibmaml:100049', 'ibmaml:100056'],
      evidence_ids: ['ev-0'],
      source: 'shap',
    },
    {
      feature: 'account_age_days',
      label: 'Account age',
      sentence: 'Days between opening and the as-of timestamp.',
      value: 0.09,
      direction: 'increases',
      rank: 4,
      txn_ids: [],
      evidence_ids: [],
      source: 'shap',
    },
  ],
  rule_hits: [
    {
      rule_code: 'R4',
      typology: 'R4',
      name: 'CYCLE_MEMBER',
      severity: 0.71,
      observed: '4-hop loop, retention 0.71 against a 0.60 floor',
      parameters: [
        { key: 'retention_floor', value: 0.6 },
        { key: 'max_length', value: 6 },
      ],
      overlap_group: 'layering',
      counted_once: true,
      txn_ids: ['ibmaml:100000', 'ibmaml:100007', 'ibmaml:100014', 'ibmaml:100021'],
      first_hit: new Date(Date.UTC(2026, 8, 11, 4, 12)).toISOString(),
      last_hit: new Date(Date.UTC(2026, 8, 20, 9, 31)).toISOString(),
    },
    {
      rule_code: 'R1',
      typology: 'R1',
      name: 'RAPID_PASS_THROUGH',
      severity: 0.88,
      observed: 'forwards 0.94 of inflow within 22 minutes',
      parameters: [
        { key: 'pass_ratio', value: 0.8 },
        { key: 'window_minutes', value: 60 },
        { key: 'min_inflow_minor', value: 10_000 },
      ],
      overlap_group: 'layering',
      counted_once: false,
      txn_ids: ['ibmaml:100000', 'ibmaml:100007'],
      first_hit: new Date(Date.UTC(2026, 8, 11, 4, 34)).toISOString(),
      last_hit: new Date(Date.UTC(2026, 8, 23, 22, 8)).toISOString(),
    },
    {
      rule_code: 'R10',
      typology: 'R10',
      name: 'FAST_CASH_OUT',
      severity: 0.52,
      observed: '0.81 of inflow exits as cash-out inside 3 h',
      parameters: [
        { key: 'cash_out_share', value: 0.7 },
        { key: 'holding_hours', value: 6 },
      ],
      overlap_group: 'extraction',
      counted_once: true,
      txn_ids: ['ibmaml:100049'],
      first_hit: new Date(Date.UTC(2026, 8, 14, 6, 2)).toISOString(),
      last_hit: new Date(Date.UTC(2026, 8, 24, 1, 47)).toISOString(),
    },
  ],
  counterfactual: {
    feature: 'pass_through_ratio_1h',
    label: 'Pass-through ratio, trailing hour',
    score_without: 0.462,
    band_without: 'C',
    expected_value_without: figure(21_400_000),
    statement:
      'Remove the pass-through ratio and this account drops from band E to band C, below the capacity cutoff: the queue would not have reached it this period.',
  },
  decisions: decisions(),
  decision_version: 2,
  narrative: {
    text: 'Money arrives from eleven senders, moves on inside the hour, and closes a loop back to one of them. No single transfer is unusual; the shape is.',
    source: 'template',
    degraded: true,
  },
};

/** A second, lower-band case so deep links from the queue all resolve. */
export const quietCase: CasePayload = {
  ...heroCase,
  header: {
    ...heroCase.header,
    account_key: 'ACC-4B1C07',
    score: 0.318,
    band: 'B',
    calibration: { bin_label: '0.30–0.40', observed_rate: 0.281, n: 168, expected_rate: 0.318 },
    typology: 'R6',
    reasons: heroCase.header.reasons.slice(1),
  },
  counterfactual: null,
  decisions: [],
  decision_version: 0,
};

/** The window-empty case: scored, but no counterparties inside the drawn window. */
export const isolatedCase: CasePayload = {
  ...quietCase,
  header: {
    ...quietCase.header,
    account_key: 'ACC-00DORM',
    score: 0.041,
    band: 'A',
    calibration: { bin_label: '0.00–0.10', observed_rate: 0.012, n: 1_043, expected_rate: 0.041 },
    typology: null,
    reasons: [],
  },
  evidence: [],
  transactions: [],
  contributions: [],
  rule_hits: [],
};

export const CASES: Record<string, CasePayload> = {
  [HERO]: heroCase,
  'ACC-4B1C07': quietCase,
  'ACC-00DORM': isolatedCase,
};
