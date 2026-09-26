/* FIXTURE DATA — developer contract double (see common.fixture.ts).
   The Policy & Economics simulator.

   The fixture recomputes its answer from the four request parameters rather than
   returning one canned payload. That is deliberate: the screen's whole claim is
   "the server re-allocates for real", so a double that ignored its inputs would
   let the page pass its own test while being theatre. The arithmetic below is the
   greedy-by-EV-density rule from plan §11 over a cached account array, which is
   what P7 will do properly. */

import type { Allocation, PolicyDefaults } from '../lib/api/contract';
import { figure, money, seeded } from './common.fixture';
import { alertRows } from './alerts.fixture';

/** Per-account (p, exposure minor, review minutes) triples, from the queue fixture. */
const CANDIDATES = alertRows.map((row) => ({
  key: row.account_key,
  p: row.score,
  exposure: row.exposure.value.minor,
  minutes: row.review_minutes,
}));

export const policyDefaults: PolicyDefaults = {
  capacity_minutes: 12_000,
  capacity_bounds: { min: 0, max: 30_000, step: 250 },
  recovery_rate: 0.35,
  recovery_band: [0.2, 0.35, 0.5],
  analyst_cost_per_hour: money(900_000),
  friction_cost: money(2_500_000),
  currency: 'UGX',
  source: 'config/economics.yaml',
  period_label: 'week to 25 Sep 2026',
  baselines: [
    { label: 'score threshold', points: curve(0.62), is_current: false },
    { label: 'highest-amount-first', points: curve(0.41), is_current: false },
    { label: 'rules-only', points: curve(0.5), is_current: false },
    { label: 'random', points: curve(0.22), is_current: false },
  ],
};

function curve(multiplier: number): { x: string; y: number; label: string | null }[] {
  const next = seeded(Math.round(multiplier * 1000) + 7);
  let cumulative = 0;
  const points = [];
  for (let week = 1; week <= 12; week += 1) {
    cumulative += Math.round((9_000_000 + next() * 26_000_000) * multiplier);
    points.push({ x: new Date(Date.UTC(2026, 5, 8 + week * 7)).toISOString(), y: cumulative, label: `week ${String(week)}` });
  }
  return points;
}

export type AllocateParams = {
  capacity_minutes: number;
  recovery_rate: number;
  analyst_cost_per_hour: number;
  friction_cost: number;
};

/** Greedy knapsack by EV density over the cached candidate array. */
export function allocate(params: AllocateParams, exactSolve: boolean): Allocation {
  const costPerMinute = params.analyst_cost_per_hour / 60;
  const scored = CANDIDATES.map((candidate) => {
    const cost = candidate.minutes * costPerMinute;
    const ev =
      candidate.p * candidate.exposure * params.recovery_rate - cost - (1 - candidate.p) * params.friction_cost;
    return { ...candidate, ev, density: ev / Math.max(candidate.minutes, 1) };
  }).sort((a, b) => b.density - a.density);

  let budget = params.capacity_minutes;
  const chosen: typeof scored = [];
  for (const candidate of scored) {
    if (budget < candidate.minutes) continue;
    if (candidate.ev <= 0) continue;
    budget -= candidate.minutes;
    chosen.push(candidate);
  }

  const totalEv = chosen.reduce((sum, entry) => sum + entry.ev, 0);
  const forgone = CANDIDATES.filter((c) => !chosen.some((entry) => entry.key === c.key));
  const unreviewedExposure = forgone.reduce((sum, entry) => sum + entry.exposure, 0);
  const hours = params.capacity_minutes / 60;
  const gap = exactSolve
    ? { absolute: money(1_420_000), ratio: 0.0031, cpsat_objective: totalEv + 1_420_000, greedy_objective: totalEv }
    : null;

  const previous = allocateAt(12_000, params);
  const keys = new Set(chosen.map((entry) => entry.key));
  const before = new Set(previous);

  return {
    policy_label: exactSolve ? 'EV density, exact CP-SAT' : 'EV density, greedy allocator',
    allocator: exactSolve ? 'cpsat' : 'greedy',
    accounts_reviewed: chosen.length,
    expected_loss_avoided: figure(Math.max(0, Math.round(totalEv))),
    benefit_per_analyst_hour: figure(hours > 0 ? Math.round(totalEv / hours) : 0),
    customers_wrongly_touched: Math.round(chosen.length * 0.31),
    optimality_gap: gap,
    max_drawdown: figure(Math.round(Math.max(0, 6_400_000 * (12_000 / Math.max(params.capacity_minutes, 1)) - 4_100_000))),
    var95_unreviewed: figure(Math.round(unreviewedExposure * 0.21)),
    es975_unreviewed: figure(Math.round(unreviewedExposure * 0.34)),
    cumulative_curve: [
      { label: 'EV policy (this configuration)', points: curve(1), is_policy: true },
      ...policyDefaults.baselines.map((baseline) => ({
        label: baseline.label,
        points: baseline.points.map((point) => ({ ...point, y: Math.round(point.y * (totalEv / 98_000_000 || 1)) })),
        is_policy: false,
      })),
    ],
    frontier: {
      points: sweep(params),
      current_index: Math.min(
        30,
        Math.max(0, Math.round((params.capacity_minutes - 0) / (30_000 / 30))),
      ),
    },
    changed: {
      entered: [...keys].filter((key) => !before.has(key)).slice(0, 12),
      left: [...before].filter((key) => !keys.has(key)).slice(0, 12),
      entered_count: [...keys].filter((key) => !before.has(key)).length,
      left_count: [...before].filter((key) => !keys.has(key)).length,
    },
    solve_ms: exactSolve ? 3_412 : 41,
    degraded: !exactSolve,
  };
}

/** The active review set at a given capacity, for the "what changed" strip. */
function allocateAt(capacity: number, params: AllocateParams): string[] {
  const costPerMinute = params.analyst_cost_per_hour / 60;
  const scored = CANDIDATES.map((candidate) => ({
    ...candidate,
    density:
      (candidate.p * candidate.exposure * params.recovery_rate -
        candidate.minutes * costPerMinute -
        (1 - candidate.p) * params.friction_cost) /
      Math.max(candidate.minutes, 1),
  })).sort((a, b) => b.density - a.density);
  let budget = capacity;
  const out: string[] = [];
  for (const candidate of scored) {
    if (budget < candidate.minutes || candidate.density <= 0) continue;
    budget -= candidate.minutes;
    out.push(candidate.key);
  }
  return out;
}

function sweep(params: AllocateParams): Allocation['frontier']['points'] {
  const steps = 31;
  const points: Allocation['frontier']['points'] = [];
  for (let index = 0; index < steps; index += 1) {
    const capacity = Math.round((index / (steps - 1)) * 30_000);
    const chosen = allocateAt(capacity, params);
    const total = chosen.reduce(
      (sum, key) => sum + (CANDIDATES.find((entry) => entry.key === key)?.exposure ?? 0),
      0,
    );
    points.push({
      capacity_minutes: capacity,
      loss_avoided: money(Math.round(total * params.recovery_rate * 0.68)),
      wrongly_touched: Math.round(chosen.length * 0.31),
    });
  }
  // One dominated policy on the same axes, so the frontier means something visually:
  // highest-amount-first buys the same coverage for more wrongly-touched customers.
  points[15] = {
    capacity_minutes: points[15]?.capacity_minutes ?? 15_000,
    loss_avoided: money(Math.round((points[15]?.loss_avoided.minor ?? 0) * 0.61)),
    wrongly_touched: Math.round((points[15]?.wrongly_touched ?? 0) * 1.9),
  };
  return points;
}

