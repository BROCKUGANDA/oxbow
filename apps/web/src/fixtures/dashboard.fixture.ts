/* FIXTURE DATA — developer contract double. See common.fixture.ts for why this
   cannot reach a production build. Shapes are the P8a-1 command strip. */

import type { Dashboard } from '../lib/api/contract';
import { figure, seeded } from './common.fixture';

/** Latest-pattern feed rows, deterministic from seed 1337. */
function patterns(count: number): Dashboard['latest_patterns'] {
  const next = seeded(1337);
  const rules = [
    { typology: 'R4', code: 'CYCLE_MEMBER', label: 'time-respecting 4-cycle, retention 0.71' },
    { typology: 'R2', code: 'FAN_IN', label: '11 distinct senders in 24 h, median below p25' },
    { typology: 'R1', code: 'RAPID_PASS_THROUGH', label: 'outflow 0.94 of inflow within 22 min' },
    { typology: 'R10', code: 'FAST_CASH_OUT', label: '0.81 of inflow exited as cash-out in 3 h' },
    { typology: 'R12', code: 'CHAIN_MEMBER', label: '4-hop non-increasing chain, every hop under 24 h' },
    { typology: 'R5', code: 'STRUCTURING', label: '5 transfers inside 80–100 % of the configured threshold' },
  ] as const;
  const out: Dashboard['latest_patterns'] = [];
  for (let index = 0; index < count; index += 1) {
    const rule = rules[index % rules.length];
    if (rule === undefined) continue;
    const exposureMinor = Math.round(6_000_000 + next() * 240_000_000);
    out.push({
      typology: rule.typology,
      rule_code: rule.code,
      account_key: `ACC-${(0x7f2a19 + index * 0x1d).toString(16).toUpperCase()}`,
      case_href: `/cases/ACC-${(0x7f2a19 + index * 0x1d).toString(16).toUpperCase()}`,
      severity: Math.round((0.31 + next() * 0.66) * 100) / 100,
      exposure: figure(exposureMinor),
      observed: rule.label,
      first_seen: new Date(Date.UTC(2026, 8, 18 + (index % 7), 5 + index, 12 + index)).toISOString(),
      community_id: 1 + (index % 6),
    });
  }
  return out;
}

function series(
  label: string,
  multiplier: number,
  isPolicy: boolean,
): Dashboard['cumulative_benefit']['series'][number] {
  const next = seeded(label.length * 97 + 11);
  const points = [];
  let cumulative = 0;
  for (let week = 1; week <= 12; week += 1) {
    cumulative += Math.round((9_000_000 + next() * 26_000_000) * multiplier);
    points.push({
      x: new Date(Date.UTC(2026, 5, 8 + week * 7)).toISOString(),
      y: cumulative,
      label: `week ${String(week)}`,
    });
  }
  return { label, points, is_policy: isPolicy };
}

export const dashboard: Dashboard = {
  expected_loss_avoided: figure(412_000_000),
  benefit_per_analyst_hour: figure(10_300_000),
  residual_exposure: figure(96_400_000),
  alerts_generated: 1_412,
  high_risk_networks: 9,
  capacity: { reviewed: 200, available: 12_000, unit: 'analyst-minutes', period_label: 'week to 25 Sep 2026' },
  model_quality: {
    pr_auc: {
      value: 0.412,
      delta_vs_baseline: 0.147,
      baseline_label: 'rules-only',
      unit: 'PR-AUC',
      ci: [0.371, 0.455],
    },
    precision_at_budget: {
      value: 0.635,
      delta_vs_baseline: 0.211,
      baseline_label: 'highest-amount-first',
      unit: 'precision',
      ci: [0.588, 0.68],
    },
    brier: { value: 0.0318, delta_vs_baseline: -0.0142, baseline_label: 'uncalibrated GBM', unit: 'Brier', ci: null },
  },
  cumulative_benefit: {
    series: [
      series('EV policy (active)', 1, true),
      series('score threshold', 0.62, false),
      series('highest-amount-first', 0.41, false),
      series('random', 0.22, false),
    ],
    x_axis_label: 'walk-forward test fold',
    y_axis_label: 'cumulative net benefit',
    y_is_money: true,
  },
  band_distribution: [
    { band: 'A', accounts: 1_043, population_share: 0.62 },
    { band: 'B', accounts: 398, population_share: 0.237 },
    { band: 'C', accounts: 141, population_share: 0.084 },
    { band: 'D', accounts: 74, population_share: 0.044 },
    { band: 'E', accounts: 25, population_share: 0.015 },
  ],
  latest_patterns: patterns(6),
  economics_source: 'config/economics.yaml',
};
